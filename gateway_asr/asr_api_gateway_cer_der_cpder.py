#!/usr/bin/env python3
"""Standalone ASR gateway evaluator: HTTP client, parsers, metrics and runner.

Requires scipy, rapidfuzz, httpx, requests and urllib3 (plus websockets for
legacy streaming). Execute this file directly; all runtime Python implementation
lives here. By default, scans the adjacent dataset ``asr_test_cer_der_cpder``.
Use --audio for multipart single-sample scoring or --legacy --audio for the
original HMAC JSON gateway client.
"""
from __future__ import annotations

# --- CER/WER/DER/cpDER/cpCER and annotation parsing -------------------------


import hashlib
import json
import math
import os
import re
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# ======================== ASR 网关配置 ========================
# 仅保留当前 Fun-ASR-Nano-2512 multipart/form-data 网关这一套配置。
# 后续切换环境时，只需要修改这里；命令行参数仍可临时覆盖 endpoint/model。
ASR_BINDING = "southgrid"
ASR_MODEL = "funasr-iic"
ASR_BINDING_HOST = "https://192.168.0.213:18300/ai-inference-gateway/predict"
ASR_BINDING_API_KEY = "24e74daf74124b0b96c9cb113162a976"
ASR_CUSTCODE = "1001300033"
ASR_COMPONENTCODE = "04101002"

# TLS 证书验证配置：False=关闭验证（适用于内网自签名证书），True=开启验证
ASR_VERIFY_TLS = False

# 调试模式标志，用于输出详细的错误信息
# 默认从环境变量读取，可通过 --debug 参数在运行时覆盖
# 注意：此标志在 evaluation_main 入口处修改，影响全局行为
DEBUG_MODE = os.getenv("ASR_EVAL_DEBUG", "").lower() in {"1", "true", "yes"}

# 脚本目录及默认数据位置集中管理，保持原有目录结构不变。
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET_DIR = SCRIPT_DIR / "asr_test_cer_der_cpder"
DEFAULT_AUDIO_FILE = SCRIPT_DIR / "asr_test_audio" / "2.ogg"

from scipy.optimize import linear_sum_assignment
from rapidfuzz.distance import Levenshtein as _RapidLevenshtein

SCORER_VERSION = "cer-der-cpder-v6"
NORMALIZATION_VERSION = "shared-semantic-symbols-v3"
# Conservative fail-closed bound on native edit-path matrix work.
MAX_ALIGNMENT_PRODUCT = 1_000_000_000
# Small annotation/audio-duration tail drift is common and safe to clip.
REFERENCE_CUTOFF_TOLERANCE_SEC = 0.05
SYSTEM_CUTOFF_TOLERANCE_SEC = 0.05
AUTO_TIMESTAMP_TAIL_TOLERANCE = 0.02
MAX_SPEAKERS = 256
MAX_RESPONSE_NESTING = 32
MAX_RESPONSE_NODES = 100_000
MAX_LEGACY_ALIGNMENT_PRODUCT = 1_000_000
_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma", ".webm", ".mp4"}
# Only remove annotation-like markers.  Do not treat arbitrary angle-bracket
# text or ampersand-delimited prose as a tag (for example ``<10元>`` or
# ``A & B & C``).
_TAG_RE = re.compile(r"<[A-Za-z][A-Za-z0-9_.:-]*>|&[^&\s]+&")
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_ALWAYS_SEMANTIC_SYMBOLS = frozenset("%‰$€¥￥+±=<>#@°℃")
_INFIX_SEMANTIC_SYMBOLS = frozenset(".-/:,'’&")


def _is_ascii_alnum_common(char: str) -> bool:
    return bool(char) and char.isascii() and char.isalnum()


def _is_ascii_digit(char: str) -> bool:
    return bool(char) and "0" <= char <= "9"


def _is_semantic_symbol_common(text: str, index: int) -> bool:
    char = text[index]
    if char in _ALWAYS_SEMANTIC_SYMBOLS:
        return True
    if char not in _INFIX_SEMANTIC_SYMBOLS:
        return False
    previous = text[index - 1] if index > 0 else ""
    following = text[index + 1] if index + 1 < len(text) else ""
    between_digits = _is_ascii_digit(previous) and _is_ascii_digit(following)
    if char in ",:":
        return between_digits
    if char == ".":
        return between_digits or (_is_ascii_alnum_common(previous) and _is_ascii_alnum_common(following))
    if char == "-" and _is_ascii_digit(following) and not _is_ascii_alnum_common(previous):
        return True
    return _is_ascii_alnum_common(previous) and _is_ascii_alnum_common(following)


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    speaker: str
    text: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class EditStats:
    substitutions: int
    deletions: int
    insertions: int
    reference_units: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def rate(self) -> float | None:
        return self.errors / self.reference_units if self.reference_units else None


@dataclass(frozen=True)
class TextScore:
    cer: EditStats
    wer: EditStats


@dataclass(frozen=True)
class DiarizationScore:
    der: float | None
    miss: float
    false_alarm: float
    confusion: float
    reference_speech_time: float
    system_speech_time: float
    correct_speech_time: float
    mapping: dict[str, str]
    reference_speakers: int
    system_speakers: int
    collar_sec: float = 0.0
    overlap_included: bool = True

    @property
    def cpder(self) -> float | None:
        """cpDER is DER after a globally optimal one-to-one speaker permutation.

        This is intentionally reported separately from ``der`` in the output so
        callers can see whether a tool used label-aware or permutation-invariant
        scoring.  Our production scorer uses the permutation-invariant definition
        for both; the mapping is always persisted for auditability.
        """
        return self.der


@dataclass(frozen=True)
class CpCerScore:
    cpcer: float | None
    errors: int
    reference_units: int
    mapping: dict[str, str]
    substitutions: int
    deletions: int
    insertions: int
    reference_speakers: int
    system_speakers: int


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metric_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must not be boolean")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be numeric, got {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return number


def normalize_text(text: Any, *, strip_punctuation: bool = True,
                   keep_spaces: bool = False) -> str:
    """NFKC, lowercase, remove annotation tags; optionally score punctuation.

    Spaces delimit Latin words in WER, but do not contribute CER units.
    """
    value = _TAG_RE.sub("", "" if text is None else str(text))
    value = unicodedata.normalize("NFKC", value).lower()
    output: list[str] = []
    for index, char in enumerate(value):
        # Unicode line/tab separators are also in the Cc/CZ categories.  Treat
        # all whitespace as a word delimiter before dropping other controls;
        # otherwise ``hello\nworld`` silently becomes ``helloworld``.
        if char.isspace():
            if keep_spaces and output and output[-1] != " ":
                output.append(" ")
            continue
        category = unicodedata.category(char)
        if category.startswith("C"):
            continue
        if strip_punctuation and category.startswith(("P", "S")) and not _is_semantic_symbol_common(value, index):
            # Punctuation separates words; "hello,world" must not become one word.
            if keep_spaces and output and output[-1] != " ":
                output.append(" ")
            continue
        output.append(char)
    return "".join(output).strip()


def tokenize_wer(text: Any, *, strip_punctuation: bool = True) -> list[str]:
    value = normalize_text(text, strip_punctuation=strip_punctuation, keep_spaces=True)
    tokens: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            tokens.append("".join(buffer))
            buffer.clear()

    for index, char in enumerate(value):
        code = ord(char)
        cjk = (0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or
               0x20000 <= code <= 0x2FA1F)
        if char.isspace():
            flush()
        elif cjk:
            flush()
            tokens.append(char)
        elif char.isalnum():
            buffer.append(char)
        else:
            # Keep semantic punctuation attached to the surrounding token so
            # the modern evaluator uses the same WER units as the legacy path:
            # 25%, -5℃, 3.14, 1-2 and A-12 remain single tokens.
            next_is_token = index + 1 < len(value) and value[index + 1].isalnum()
            if not strip_punctuation or _is_semantic_symbol_common(value, index):
                if buffer or next_is_token:
                    buffer.append(char)
                    if not next_is_token:
                        flush()
                else:
                    flush()
                    tokens.append(char)
            else:
                flush()
    flush()
    return tokens


def alignment_stats(reference: Sequence[Any], hypothesis: Sequence[Any]) -> EditStats:
    """Levenshtein S/D/I on the *original tokens*, computed in native code.

    Do not concatenate word tokens: ['ab', 'c'] and ['a', 'bc'] differ as
    words despite having identical concatenated characters.
    """
    if len(reference) * len(hypothesis) > MAX_ALIGNMENT_PRODUCT:
        raise ValueError("alignment too large; splitting the transcript with aligned reference timestamps is required")
    substitutions = deletions = insertions = 0
    for operation in _RapidLevenshtein.editops(reference, hypothesis):
        if operation.tag == "replace":
            substitutions += 1
        elif operation.tag == "delete":
            deletions += 1
        else:
            insertions += 1
    return EditStats(substitutions, deletions, insertions, len(reference))


def score_text(reference: str, hypothesis: str, *,
               strip_punctuation: bool = True) -> TextScore:
    ref_chars = normalize_text(reference, strip_punctuation=strip_punctuation)
    hyp_chars = normalize_text(hypothesis, strip_punctuation=strip_punctuation)
    ref_words = tokenize_wer(reference, strip_punctuation=strip_punctuation)
    hyp_words = tokenize_wer(hypothesis, strip_punctuation=strip_punctuation)
    return TextScore(alignment_stats(ref_chars, hyp_chars), alignment_stats(ref_words, hyp_words))


def _parse_quoted(value: str) -> str:
    """Praat strings escape quotes by doubling, NOT with Python escapes."""
    value = value.strip()
    if len(value) < 2 or not value.startswith('"') or not value.endswith('"'):
        raise ValueError("expected a quoted Praat string")
    inner = value[1:-1]
    if '"' in inner.replace('""', ''):
        raise ValueError("unescaped quote in Praat string")
    return inner.replace('""', '"')


def parse_rttm(path: str | Path, *, expected_file_id: str | None = None) -> list[Segment]:
    """Parse RTTM SPEAKER records and reject malformed intervals."""
    result: list[Segment] = []
    for line_no, raw in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"): continue
        fields = line.split()
        if len(fields) < 8 or fields[0].upper() != "SPEAKER":
            raise ValueError(f"{path}:{line_no}: expected SPEAKER RTTM row")
        if expected_file_id is not None and fields[1] != expected_file_id:
            raise ValueError(f"{path}:{line_no}: RTTM file ID {fields[1]!r} != {expected_file_id!r}")
        start, duration = _metric_number(fields[3], "start"), _metric_number(fields[4], "duration")
        if start < 0 or duration <= 0:
            raise ValueError(f"{path}:{line_no}: invalid interval {start}, {duration}")
        end = _metric_number(start + duration, "end")
        if fields[7] == "<NA>":
            raise ValueError(f"{path}:{line_no}: missing RTTM speaker label")
        result.append(Segment(start, end, fields[7], ""))
    return sorted(result, key=lambda item: (item.start, item.end, item.speaker))


def write_rttm(path: str | Path, file_id: str, segments: Iterable[Segment]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for segment in sorted(segments, key=lambda item: (item.start, item.end, item.speaker)):
        if segment.duration <= 0: continue
        lines.append(
            f"SPEAKER {file_id} 1 {segment.start:.6f} {segment.duration:.6f} "
            f"<NA> <NA> {segment.speaker} <NA> <NA>\n"
        )
    Path(path).write_text("".join(lines), encoding="utf-8")


def parse_textgrid(path: str | Path) -> list[Segment]:
    """Parse long/short Praat text TextGrid, with multiline and doubled quotes.

    Labels and array indices are comments in Praat's format. Only free-standing
    numbers, quoted strings and <exists> carry data. Reject truncated/malformed
    input instead of silently scoring the subset that happened to parse.
    """
    source = Path(path).read_text(encoding="utf-8-sig")
    token_re = re.compile(
        r'"(?:[^"]|"")*"|![^\n]*|<exists>|'
        r'(?<![^\s])(?:[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|[-+]?(?:nan|inf(?:inity)?))(?=\s|$)',
        re.IGNORECASE,
    )
    tokens = [m.group() for m in token_re.finditer(source) if not m.group().startswith("!")]
    cursor = 0

    def take() -> str:
        nonlocal cursor
        if cursor >= len(tokens):
            raise ValueError(f"{path}: truncated TextGrid")
        value = tokens[cursor]
        cursor += 1
        return value

    def number() -> float:
        return _metric_number(take(), "TextGrid timestamp")

    def count() -> int:
        value = number()
        if value < 0 or not value.is_integer() or value > len(tokens):
            raise ValueError(f"{path}: invalid TextGrid item count")
        return int(value)

    def quoted() -> str:
        return _parse_quoted(take())

    if quoted() not in {"ooTextFile", "ooTextFile short"} or quoted() != "TextGrid":
        raise ValueError(f"{path}: unsupported TextGrid file header")
    xmin, xmax = number(), number()
    if xmin < 0 or xmax <= xmin or take() != "<exists>":
        raise ValueError(f"{path}: invalid TextGrid domain or tiers marker")
    segments: list[Segment] = []
    names: set[str] = set()
    for _ in range(count()):
        tier_class, name = quoted(), quoted()
        if not name.strip() or name in names:
            raise ValueError(f"{path}: empty or duplicate tier name {name!r}")
        names.add(name)
        left, right = number(), number()
        if left < xmin - 1e-7 or right > xmax + 1e-7 or right <= left:
            raise ValueError(f"{path}: invalid tier domain")
        previous = left
        for _ in range(count()):
            start = number()
            if tier_class == "IntervalTier":
                end, text = number(), quoted()
                if start < previous - 1e-7 or end <= start or end > right + 1e-7:
                    raise ValueError(f"{path}: invalid or overlapping TextGrid interval")
                previous = end
                if normalize_text(text, strip_punctuation=False):
                    segments.append(Segment(start, end, name, text))
            elif tier_class == "TextTier":
                quoted()  # Point tiers are not speaker transcripts.
                if start < previous - 1e-7 or start > right + 1e-7:
                    raise ValueError(f"{path}: invalid TextGrid point")
                previous = start
            else:
                raise ValueError(f"{path}: unsupported TextGrid tier class {tier_class!r}")
        if tier_class not in {"IntervalTier", "TextTier"}:
            raise ValueError(f"{path}: unsupported TextGrid tier class {tier_class!r}")
    if cursor != len(tokens):
        raise ValueError(f"{path}: unexpected trailing TextGrid data")
    return sorted(segments, key=lambda item: (item.start, item.end, item.speaker))


def _clip(segments: Iterable[Segment], duration: float | None) -> list[Segment]:
    if duration is None or duration <= 0: return list(segments)
    clipped = []
    for segment in segments:
        start, end = max(0.0, segment.start), min(duration, segment.end)
        if end > start + 1e-9:
            clipped.append(Segment(start, end, segment.speaker, segment.text))
    return clipped


def merge_intervals(segments: Iterable[Segment]) -> list[Segment]:
    """Union intervals per speaker to prevent duplicate RTTM rows double-counting."""
    grouped: dict[str, list[Segment]] = defaultdict(list)
    for segment in segments:
        if segment.duration > 0: grouped[segment.speaker].append(segment)
    merged: list[Segment] = []
    for speaker, values in grouped.items():
        values.sort(key=lambda item: (item.start, item.end))
        current = values[0]
        for item in values[1:]:
            if item.start <= current.end + 1e-9:
                current = Segment(current.start, max(current.end, item.end), speaker, current.text)
            else:
                merged.append(current); current = item
        merged.append(current)
    return sorted(merged, key=lambda item: (item.start, item.end, item.speaker))


def _timeline(reference: Sequence[Segment], system: Sequence[Segment]):
    """Yield constant-active-speaker intervals using a sweep line, not O(B*S) scans."""
    events: list[tuple[float, int, str, int]] = []
    for group, segments in enumerate((reference, system)):
        for item in segments:
            events.append((item.start, group, item.speaker, 1))
            events.append((item.end, group, item.speaker, -1))
    events.sort()
    counts: list[dict[str, int]] = [{}, {}]
    previous: float | None = None
    index = 0
    while index < len(events):
        point = events[index][0]
        if previous is not None and point > previous:
            yield previous, point, set(counts[0]), set(counts[1])
        while index < len(events) and events[index][0] == point:
            _, group, speaker, delta = events[index]
            count = counts[group].get(speaker, 0) + delta
            if count:
                counts[group][speaker] = count
            else:
                counts[group].pop(speaker, None)
            index += 1
        previous = point


def _assignment(overlap: Mapping[tuple[str, str], float], refs: Sequence[str], syss: Sequence[str]) -> dict[str, str]:
    if not refs or not syss: return {}
    matrix = [[overlap.get((sys, ref), 0.0) for ref in refs] for sys in syss]
    rows, cols = linear_sum_assignment([[-value for value in row] for row in matrix])
    return {syss[row]: refs[col] for row, col in zip(rows, cols)}

def _collar_mask(reference: Sequence[Segment], collar_sec: float) -> list[tuple[float, float]]:
    """Return exclusion collar around speaker active-set change points.

    ``collar_sec`` is the distance on *each side* of a speaker change boundary,
    not the total width of the exclusion window.  In particular, a collar of
    ``0.25`` excludes ``[boundary - 0.25, boundary + 0.25]``.  Keeping this
    convention here is important because the excluded time is also removed
    from the reference-speech denominator.
    
    **Speaker-change-point collar (annotation-granularity-invariant variant)**:
    
    This implementation applies collar only at speaker active-set change points
    in the merged reference, NOT at every RTTM segment boundary. This makes the
    metric invariant to annotation granularity: splitting one reference turn into
    multiple segments does not change the DER.
    
    **Important**: This is a deliberate design choice that differs from NIST
    md-eval.pl / dscore, which apply collar at each RTTM turn boundary. For
    exact NIST md-eval numeric compatibility, use an external tool or set
    collar_sec=0.0 and post-process with md-eval.pl.
    
    Example: [0-1s r1, 1-2s r2] with collar=0.25
    - This implementation: excludes [0.75, 1.25] → 1.5s scoreable
    - NIST md-eval.pl: excludes [0, 0.25] ∪ [0.75, 1.25] ∪ [1.75, 2.0] → 1.0s scoreable
    """
    if not collar_sec:
        return []
    # Build timeline to find speaker active-set change points
    boundaries: list[float] = []
    previous_active: set[str] | None = None
    for start, _end, ref_active, _ in _timeline(reference, []):
        if previous_active is not None and ref_active != previous_active:
            boundaries.append(start)
        previous_active = ref_active
    
    windows = sorted((max(0.0, boundary - collar_sec), boundary + collar_sec)
                     for boundary in boundaries)
    merged: list[tuple[float, float]] = []
    for start, end in windows:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _scored_timeline(reference: Sequence[Segment], system: Sequence[Segment], collar_sec: float):
    """Yield timeline intervals excluding collar regions.
    
    Collar mask is computed from the merged reference (speaker active-set changes),
    not from raw RTTM segment boundaries.
    """
    mask = _collar_mask(reference, collar_sec)
    index = 0
    for start, end, ref_active, sys_active in _timeline(reference, system):
        while index < len(mask) and mask[index][1] <= start:
            index += 1
        cursor = start
        for left, right in mask[index:]:
            if left >= end:
                break
            if left > cursor:
                yield cursor, min(left, end), ref_active, sys_active
            cursor = max(cursor, right)
            if cursor >= end:
                break
        if cursor < end:
            yield cursor, end, ref_active, sys_active


def score_diarization(reference: Sequence[Segment], system: Sequence[Segment], *,
                      collar_sec: float = 0.0) -> DiarizationScore:
    """Permutation-invariant DER with speaker-change-point collar.

    ``collar_sec`` is applied on each side of every speaker active-set change
    point in the merged reference; the excluded regions are removed from the
    DER denominator. Overlap is included. The default is zero to preserve the
    historical collar-free score profile; pass ``--collar 0.25`` for a 250-ms
    per-side collar.
    
    **Important**: This uses speaker-change-point collar (annotation-granularity
    invariant), NOT the NIST md-eval.pl turn-boundary collar. For exact NIST
    numeric compatibility, use md-eval.pl or dscore directly, or set collar_sec=0.0.
    """
    if not math.isfinite(collar_sec) or collar_sec < 0:
        raise ValueError("collar_sec must be finite and >= 0")
    # De-duplicate/union reference RTTM rows, but preserve the system output
    # boundaries and overlaps exactly as returned by ASR.  Merging system rows
    # can erase short overlaps or turn adjacent streaming chunks into a falsely
    # continuous turn and therefore change DER.
    for item in (*reference, *system):
        if (not math.isfinite(item.start) or not math.isfinite(item.end) or
                item.start < 0 or item.end <= item.start or not str(item.speaker).strip()):
            raise ValueError("invalid diarization segment")
    reference = merge_intervals(reference)
    system = sorted((x for x in system if x.duration > 0),
                    key=lambda x: (x.start, x.end, x.speaker))
    refs, syss = sorted({x.speaker for x in reference}), sorted({x.speaker for x in system})
    if max(len(refs), len(syss)) > MAX_SPEAKERS:
        raise ValueError("too many diarization speakers")
    scored = list(_scored_timeline(reference, system, collar_sec))
    overlap: dict[tuple[str, str], float] = defaultdict(float)
    for start, end, ref_active, sys_active in scored:
        for sys in sys_active:
            for ref in ref_active:
                overlap[(sys, ref)] += end - start
    mapping = _assignment(overlap, refs, syss)
    miss = false_alarm = confusion = correct = ref_time = sys_time = 0.0
    for start, end, ref_active, sys_active in scored:
        width = end - start
        ref_count, sys_count = len(ref_active), len(sys_active)
        ref_time += ref_count * width
        sys_time += sys_count * width
        correct_here = sum(mapping.get(sys) in ref_active for sys in sys_active)
        correct += correct_here * width
        miss += max(ref_count - sys_count, 0) * width
        false_alarm += max(sys_count - ref_count, 0) * width
        confusion += (min(ref_count, sys_count) - correct_here) * width
    der = (miss + false_alarm + confusion) / ref_time if ref_time else None
    return DiarizationScore(der, miss, false_alarm, confusion, ref_time, sys_time,
                            correct, mapping, len(refs), len(syss), collar_sec, True)


def _speaker_texts(segments: Iterable[Segment], *, strip_punctuation: bool = True) -> dict[str, str]:
    grouped: dict[str, list[Segment]] = defaultdict(list)
    for segment in segments:
        if normalize_text(segment.text, strip_punctuation=strip_punctuation): grouped[segment.speaker].append(segment)
    # Segment boundaries are not guaranteed to carry a trailing space.  Join
    # with a delimiter so English words from adjacent segments cannot merge;
    # normalize_text removes/normalizes that delimiter consistently later.
    return {speaker: " ".join(item.text for item in sorted(values, key=lambda x: (x.start, x.end)))
            for speaker, values in grouped.items()}


def score_cpcer(reference: Sequence[Segment], system: Sequence[Segment], *,
                strip_punctuation: bool = True) -> CpCerScore:
    ref_texts = {key: normalize_text(value, strip_punctuation=strip_punctuation)
                 for key, value in _speaker_texts(reference, strip_punctuation=strip_punctuation).items()}
    sys_texts = {key: normalize_text(value, strip_punctuation=strip_punctuation)
                 for key, value in _speaker_texts(system, strip_punctuation=strip_punctuation).items()}
    refs, syss = sorted(ref_texts), sorted(sys_texts)
    nr, ns = len(refs), len(syss)
    if max(nr, ns) > MAX_SPEAKERS:
        raise ValueError("too many transcript speakers")
    # Dummy rows/columns account for unmatched reference deletions/system insertions.
    # Rectangular matching on raw pairwise distances is NOT globally optimal when
    # the unmatched speakers have different transcript lengths.
    if any(len(ref_texts[ref]) * len(sys_texts[sys]) > MAX_ALIGNMENT_PRODUCT
           for ref in refs for sys in syss):
        raise ValueError("speaker transcript alignment too large")
    costs = [[_RapidLevenshtein.distance(ref_texts[ref], sys_texts[sys])
              for ref in refs] + [len(sys_texts[sys])] * ns for sys in syss]
    costs += [[len(ref_texts[ref]) for ref in refs] + [0] * ns for _ in refs]
    mapping: dict[str, str] = {}
    if nr + ns:
        assignment_rows, assignment_cols = linear_sum_assignment(costs)
        # scipy/numpy stubs may infer ndarray elements as ``list | str`` in
        # editors.  Materialize scalar integer indices before indexing the
        # speaker lists; this is also clearer about the assignment contract.
        for row_value, col_value in zip(assignment_rows, assignment_cols):
            row = int(row_value)
            col = int(col_value)
            if row < ns and col < nr:
                mapping[syss[row]] = refs[col]
    stats = [alignment_stats(ref_texts[ref], sys_texts[sys])
             for sys, ref in mapping.items()]
    mapped_refs = set(mapping.values())
    stats += [EditStats(0, len(ref_texts[ref]), 0, len(ref_texts[ref]))
              for ref in refs if ref not in mapped_refs]
    stats += [EditStats(0, 0, len(sys_texts[sys]), 0)
              for sys in syss if sys not in mapping]
    total = EditStats(sum(x.substitutions for x in stats), sum(x.deletions for x in stats),
                      sum(x.insertions for x in stats), sum(x.reference_units for x in stats))
    return CpCerScore(total.rate, total.errors, total.reference_units, mapping, total.substitutions,
                      total.deletions, total.insertions, nr, ns)


def find_audio_files(root: str | Path, exclude: Sequence[str] = ()) -> list[Path]:
    root = Path(root)
    try:
        patterns = [re.compile(value) for value in exclude]
    except re.error as exc:
        raise ValueError(f"invalid --exclude regular expression: {exc}") from exc
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _AUDIO_EXTENSIONS
             and not any(pattern.search(p.name) for pattern in patterns)]
    return sorted(files, key=lambda p: (p.relative_to(root).as_posix().casefold(), p.relative_to(root).as_posix()))


def canonical_stem(audio: str | Path) -> str:
    stem = Path(audio).stem
    stem = re.sub(r"-converted(?:_\d+s)?$", "", stem, flags=re.IGNORECASE)
    return stem


def json_default(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, Path): return str(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=json_default, allow_nan=False)



# --- Audio duration parsing --------------------------------------------------

import json
import math
import shutil
import subprocess
import wave
from pathlib import Path


def audio_duration(path: Path) -> float | None:
    """Return measured duration; never infer it from compressed file size."""
    with path.open("rb") as handle:
        header = handle.read(12)
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        try:
            with wave.open(str(path), "rb") as audio:
                rate = audio.getframerate()
                duration = audio.getnframes() / rate if rate else 0.0
                if math.isfinite(duration) and 0 < duration:
                    return duration
        except (OSError, EOFError, wave.Error):
            pass
    probe = shutil.which("ffprobe")
    if not probe:
        return None
    try:
        result = subprocess.run([probe, "-v", "error", "-show_entries",
                                 "stream=codec_type,duration:format=duration", "-of", "json", str(path)],
                                capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            return None
        data = json.loads(result.stdout)
        audio = next((x for x in data.get("streams", []) if x.get("codec_type") == "audio"), {})
        for value in (audio.get("duration"), data.get("format", {}).get("duration")):
            try:
                duration = float(value)
            except (ValueError, TypeError):
                continue
            if math.isfinite(duration) and 0 < duration:
                return duration
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        pass
    return None



# --- Multipart gateway client ------------------------------------------------


import hashlib
import json
import mimetypes
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import httpx
except ImportError as exc:  # pragma: no cover - exercised in packaging/CI
    httpx = None
    _HTTPX_IMPORT_ERROR = exc
else:
    _HTTPX_IMPORT_ERROR = None


# Values supplied by研发.  Keep them in the evaluator so a result records the
# same transport contract that was used in production.
SERVER_LIMITS = {
    "concurrency": 1, "queue_max_size": 30, "queue_timeout_seconds": 7200,
    "request_body_idle_timeout_seconds": 30, "request_body_total_timeout_seconds": 900,
    "upload_chunk_bytes": 1048576,
    "max_request_body_bytes": 536936448, "max_encoded_audio_bytes": 536870912,
    "max_decoded_audio_bytes": 536870912, "max_asr_audio_seconds": 7200,
    "max_moss_audio_seconds": 3600, "ffmpeg_timeout_seconds": 120,
    "qwen_internal_connect_timeout_seconds": 10,
    "qwen_internal_write_timeout_seconds": 120, "qwen_internal_read_timeout_seconds": 900,
    "forced_aligner_timeout_seconds": 600, "moss_timeout_seconds": 1800,
    "qwen_max_new_tokens": 4096, "moss_max_new_tokens": 32768,
}

CLIENT_CONFIG = {
    "connect_timeout_seconds": 15, "upload_write_timeout_seconds": 900,
    "response_read_timeout_seconds": 3600, "total_timeout_seconds": 3600,
    "automatic_retries": 0, "follow_redirects": False, "http_version": "HTTP/1.1",
    "use_proxy": False, "expect_100_continue": False,
}


class AsrHttpError(RuntimeError):
    """A transport, HTTP, JSON, or response-size failure."""

    def __init__(self, message: str, *, stage: str, status_code: int | None = None):
        super().__init__(message)
        self.stage = stage
        self.status_code = status_code
        self.metadata: dict[str, Any] = {}


@dataclass(frozen=True)
class ClientConfig:
    endpoint: str
    model: str = "default"
    language: str = "zh"
    hotwords: str = ""
    speaker_diarization: bool = True
    is_return_timestamp: bool = True
    return_speaker_segments: bool = True
    connect_timeout_seconds: float = 15.0
    upload_write_timeout_seconds: float = 900.0
    response_read_timeout_seconds: float = 3600.0
    total_timeout_seconds: float = 3600.0
    automatic_retries: int = 0
    follow_redirects: bool = False
    http_version: str = "HTTP/1.1"
    use_proxy: bool = False
    verify_tls: bool = True
    max_response_bytes: int = 32 * 1024 * 1024

    def public_dict(self) -> dict[str, Any]:
        return {**asdict(self), "endpoint": _safe_endpoint(self.endpoint)}


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _safe_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host += f":{parts.port}"
    if not (parts.username or parts.password):
        host = parts.netloc
    query = urlencode([(key, "[REDACTED]" if any(
        secret in key.lower() for secret in ("token", "secret", "password", "key", "auth", "credential"))
        else value) for key, value in parse_qsl(parts.query, keep_blank_values=True)])
    return urlunsplit((parts.scheme, host, parts.path, query, ""))


def _endpoint_without_manual_id(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key != "manual_test_id"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _valid_speaker(value: Any) -> bool:
    return (isinstance(value, (str, int)) and not isinstance(value, bool)
            and bool(str(value).strip()) and not any(char.isspace() for char in str(value).strip()))


def validate_response_shape(body: Any, *, require_segments: bool = True) -> list[str]:
    """Return protocol errors without changing the response.

    CER can still be scored when only ``text`` is present.  Missing or malformed
    segments are therefore reported as validation errors and handled as a
    partial result by the runner instead of being silently converted to zero.
    """
    errors: list[str] = []
    if not isinstance(body, dict):
        return ["response JSON must be an object"]
    if "text" not in body:
        errors.append("response missing required field: text")
    elif not isinstance(body["text"], str):
        errors.append("response field text must be a string")
    if not require_segments:
        return errors
    if "segments" not in body:
        if require_segments:
            errors.append("response missing required field: segments")
        return errors
    if not isinstance(body["segments"], list):
        errors.append("response field segments must be a list")
        return errors
    for index, item in enumerate(body["segments"]):
        if not isinstance(item, dict):
            errors.append(f"segments[{index}] must be an object")
            continue
        if not _valid_speaker(item.get("spk")):
            errors.append(f"segments[{index}] missing non-empty spk")
        numeric_values: dict[str, float | None] = {}
        for key in ("start", "end"):
            try:
                number = _number(item.get(key))
            except (TypeError, ValueError, OverflowError):
                number = None
            if number is None or not math.isfinite(number):
                errors.append(f"segments[{index}].{key} must be finite numeric")
            numeric_values[key] = number
        start, end = numeric_values["start"], numeric_values["end"]
        if start is None or end is None:
            continue
        if start < 0 or end <= start:
            errors.append(f"segments[{index}] end must be greater than start")
        if "text" not in item:
            errors.append(f"segments[{index}] missing text")
        elif not isinstance(item["text"], str):
            errors.append(f"segments[{index}].text must be a string")
    return errors


def post_audio(audio_path: str | Path, manual_test_id: str, config: ClientConfig) -> tuple[Any, dict[str, Any]]:
    """POST one audio file and return ``(JSON body, audit metadata)``.

    ``httpx`` gives us independent connect/write/read timeouts and HTTP/1.1.
    ``total_timeout_seconds`` is checked after response chunks, but the
    underlying socket phase budgets are idle timeouts, not a hard deadline.  No automatic transport retry is
    configured; business-level retry is deliberately owned by the runner.
    """
    if httpx is None:  # pragma: no cover
        raise AsrHttpError(f"httpx is required: {_HTTPX_IMPORT_ERROR}", stage="client_config")
    
    audio = Path(audio_path)
    if not audio.is_file():
        raise AsrHttpError(f"audio file not found: {audio}", stage="client_config")
    audio_bytes = audio.stat().st_size
    
    if DEBUG_MODE:
        LOG.info("  → POST audio: %s (%.2f MB)", audio.name, audio_bytes / (1024 * 1024))
        LOG.info("  → Endpoint: %s", _safe_endpoint(config.endpoint))
        LOG.info("  → TLS verification: %s", config.verify_tls)
    
    if audio_bytes == 0:
        raise AsrHttpError("audio file is empty", stage="client_validation")
    if audio_bytes > SERVER_LIMITS["max_encoded_audio_bytes"]:
        raise AsrHttpError(
            f"audio is {audio_bytes} bytes, above server max_encoded_audio_bytes={SERVER_LIMITS['max_encoded_audio_bytes']}",
            stage="client_validation")
    if audio_bytes + 65536 > SERVER_LIMITS["max_request_body_bytes"]:
        raise AsrHttpError(
            f"multipart request would exceed server max_request_body_bytes={SERVER_LIMITS['max_request_body_bytes']}",
            stage="client_validation")
    if config.automatic_retries != 0:
        raise AsrHttpError("automatic_retries must be 0 for production evaluation", stage="client_config")
    if config.http_version != "HTTP/1.1":
        raise AsrHttpError("only HTTP/1.1 is supported by the production profile", stage="client_config")
    for name in ("connect_timeout_seconds", "upload_write_timeout_seconds",
                 "response_read_timeout_seconds", "total_timeout_seconds"):
        if not math.isfinite(getattr(config, name)) or getattr(config, name) <= 0:
            raise AsrHttpError(f"{name} must be > 0", stage="client_config")
    if config.max_response_bytes <= 0:
        raise AsrHttpError("max_response_bytes must be > 0", stage="client_config")

    started = time.monotonic()
    # Keep phase budgets inside the wall-clock budget.  HTTPX timeouts are
    # per socket operation, so without this cap connect + upload + read could
    # exceed the configured total timeout.  When the configured phase sum is
    # larger than total, scale all three proportionally and persist both values.
    raw_budgets = (config.connect_timeout_seconds, config.upload_write_timeout_seconds,
                   config.response_read_timeout_seconds)
    budget_sum = sum(raw_budgets)
    scale = min(1.0, config.total_timeout_seconds / budget_sum)
    effective_connect, effective_write, effective_read = (max(0.001, x * scale) for x in raw_budgets)
    timeout = httpx.Timeout(connect=effective_connect, write=effective_write,
                            read=effective_read, pool=effective_connect)
    params = {"manual_test_id": manual_test_id}
    # 研发 multipart 协议要求这些文本字段；Content-Type 不手工设置，
    # 交给 httpx 自动生成带 boundary 的 multipart/form-data。
    data = {
        "componentCode": ASR_COMPONENTCODE,
        "function": config.model,
        "model": config.model,
        "language": config.language,
        "hotwords": config.hotwords,
        "speaker_diarization": _bool_text(config.speaker_diarization),
        "is_return_timestamp": _bool_text(config.is_return_timestamp),
        "return_speaker_segments": _bool_text(config.return_speaker_segments),
    }
    request_meta: dict[str, Any] = {
        "manual_test_id": manual_test_id,
        "endpoint": _safe_endpoint(config.endpoint),
        "method": "POST",
        "server_limits": SERVER_LIMITS,
        "client_contract": CLIENT_CONFIG,
        "http_version": config.http_version,
        "follow_redirects": config.follow_redirects,
        "use_proxy": config.use_proxy,
        "automatic_retries": config.automatic_retries,
        "form": data,
        "audio_bytes": audio_bytes,
        "timeout": {
            "connect_seconds": config.connect_timeout_seconds,
            "upload_write_seconds": config.upload_write_timeout_seconds,
            "response_read_seconds": config.response_read_timeout_seconds,
            "total_seconds": config.total_timeout_seconds,
            "effective_socket_budgets": {
                "connect_seconds": effective_connect,
                "upload_write_seconds": effective_write,
                "response_read_seconds": effective_read,
            },
        },
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # 网关采用 x-date + HMAC-SHA256 鉴权。每次请求重新签名，避免复用过期签名。
    x_date, authorization = getLocalAuthInfo(ASR_CUSTCODE, ASR_BINDING_API_KEY)
    headers = {
        "x-date": x_date,
        "authorization": authorization,
        "Accept": "application/json",
        "Expect": "",
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache",
    }
    
    if DEBUG_MODE:
        LOG.info("  → Connecting to server...")
    
    file_handle = audio.open("rb")
    try:
        files = {"file": (audio.name, file_handle, _content_type(audio))}
        try:
            client_kwargs = {
                "http2": False,
                "follow_redirects": config.follow_redirects,
                "timeout": timeout,
                "verify": config.verify_tls,
                "trust_env": config.use_proxy,
            }
            # trust_env=False is the important production default: no proxy or
            # ambient HTTP(S)_PROXY can silently change the route.
            with httpx.Client(**client_kwargs) as client:
                if DEBUG_MODE:
                    LOG.info("  → Sending multipart request...")
                with client.stream("POST", _endpoint_without_manual_id(config.endpoint), params=params, data=data,
                                   files=files, headers=headers) as response:
                    headers_elapsed = time.monotonic() - started
                    request_meta["time_to_response_headers_sec"] = headers_elapsed
                    request_meta["status_code"] = response.status_code
                    
                    if DEBUG_MODE:
                        LOG.info("  → Response headers received (%.2fs, HTTP %d)", headers_elapsed, response.status_code)
                    
                    if headers_elapsed > config.total_timeout_seconds:
                        raise AsrHttpError("client total timeout exceeded", stage="client_total_timeout",
                                           status_code=response.status_code)
                    request_meta["response_headers"] = {
                        "content_type": response.headers.get("content-type", ""),
                        "content_length": response.headers.get("content-length", ""),
                    }
                    chunks: list[bytes] = []
                    total_bytes = 0
                    first_body_elapsed: float | None = None
                    for chunk in response.iter_bytes():
                        if first_body_elapsed is None:
                            first_body_elapsed = time.monotonic() - started
                            if DEBUG_MODE:
                                LOG.info("  → First response byte received (%.2fs)", first_body_elapsed)
                        total_bytes += len(chunk)
                        request_meta["response_bytes"] = total_bytes
                        request_meta["time_to_first_body_byte_sec"] = first_body_elapsed
                        if total_bytes > config.max_response_bytes:
                            raise AsrHttpError(
                                f"response exceeds max_response_bytes={config.max_response_bytes}",
                                stage="response_body_size", status_code=response.status_code)
                        if time.monotonic() - started > config.total_timeout_seconds:
                            raise AsrHttpError("client total timeout exceeded", stage="client_total_timeout",
                                               status_code=response.status_code)
                        chunks.append(chunk)
                    if time.monotonic() - started > config.total_timeout_seconds:
                        raise AsrHttpError("client total timeout exceeded", stage="client_total_timeout",
                                           status_code=response.status_code)
                    raw = b"".join(chunks)
                    request_meta["time_to_first_body_byte_sec"] = first_body_elapsed
                    request_meta["response_bytes"] = len(raw)
                    request_meta["response_sha256"] = _sha256_bytes(raw)
                    request_meta["total_elapsed_sec"] = time.monotonic() - started
                    
                    if DEBUG_MODE:
                        LOG.info("  → Response complete: %d bytes in %.2fs", len(raw), request_meta["total_elapsed_sec"])
                    
                    if not 200 <= response.status_code < 300:
                        excerpt = raw[:4096].decode("utf-8", errors="replace")
                        raise AsrHttpError(
                            f"HTTP {response.status_code}: {excerpt}",
                            stage="http_status", status_code=response.status_code)
                    try:
                        body = json.loads(raw.decode(response.encoding or "utf-8"), parse_constant=_reject_json_constant)
                        _validate_json_complexity(body)
                    except (UnicodeDecodeError, ValueError, RecursionError, LookupError) as exc:
                        raise AsrHttpError(f"response is not valid JSON: {exc}", stage="json_decode",
                                           status_code=response.status_code) from exc
                    request_meta["protocol_errors"] = validate_response_shape(
                        body, require_segments=config.return_speaker_segments)
                    return body, request_meta
        except AsrHttpError:
            raise
        except httpx.ConnectTimeout as exc:
            error_msg = str(exc) or "connect timeout"
            if DEBUG_MODE:
                LOG.error("  ✗ Connect timeout: %s", error_msg)
            raise AsrHttpError(error_msg, stage="connect_timeout") from exc
        except httpx.WriteTimeout as exc:
            error_msg = str(exc) or "upload write timeout"
            if DEBUG_MODE:
                LOG.error("  ✗ Write timeout: %s", error_msg)
            raise AsrHttpError(error_msg, stage="upload_write_timeout") from exc
        except httpx.ReadTimeout as exc:
            stage = "client_total_timeout" if time.monotonic() - started >= config.total_timeout_seconds else "response_read_timeout"
            error_msg = str(exc) or "response read timeout"
            if DEBUG_MODE:
                LOG.error("  ✗ Read timeout [%s]: %s", stage, error_msg)
            raise AsrHttpError(error_msg, stage=stage) from exc
        except httpx.TimeoutException as exc:
            error_msg = str(exc) or "HTTP timeout"
            if DEBUG_MODE:
                LOG.error("  ✗ Timeout exception: %s", error_msg)
            raise AsrHttpError(error_msg, stage="timeout") from exc
        except httpx.HTTPError as exc:
            message = str(exc) or "HTTP client error"
            if "CERTIFICATE_VERIFY_FAILED" in message or "certificate verify failed" in message:
                message += (
                    "; endpoint certificate does not match the configured host/IP; "
                    "use the certificate hostname or trusted CA, or pass --insecure only for a trusted internal test"
                )
            if DEBUG_MODE:
                LOG.error("  ✗ HTTP error: %s", message)
            raise AsrHttpError(message, stage="http_client") from exc
    except AsrHttpError as exc:
        request_meta.setdefault("total_elapsed_sec", time.monotonic() - started)
        exc.metadata.update(request_meta)
        raise
    finally:
        file_handle.close()
        request_meta.setdefault("total_elapsed_sec", time.monotonic() - started)




# --- Dataset runner and reporting --------------------------------------------


import argparse
import hashlib
import math
from collections import Counter
import threading
from contextlib import contextmanager
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOG = logging.getLogger("asr-eval")
TIMESTAMP_UNITS = ("auto", "ms", "s")


def percentile(values: list[float], percent: float) -> float:
    """使用 nearest-rank 返回真实观测样本，避免小样本插值低估尾延迟。
    
    这是生产契约定义：ordered[max(1, ceil(percent/100 * N)) - 1]
    """
    if not values:
        raise ValueError("values must not be empty")
    if not 0 <= percent <= 100:
        raise ValueError("percent must be in [0, 100]")
    
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _json_file(path: Path, value: Any) -> None:
    _atomic_write(path, as_json(value) + "\n")


def _artifact_name(audio: Path, dataset: Path) -> str:
    try:
        relative = audio.relative_to(dataset).as_posix()
    except ValueError:
        relative = audio.name
    # A readable prefix alone is not injective ("a/b" and "a__b" collide).
    prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", relative.replace("/", "__"))
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
    return f"{prefix[:160]}-{digest}"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def _extract_text(body: Any) -> str:
    # Do not score a nested error message or another envelope field as transcript.
    return body["text"] if isinstance(body, dict) and isinstance(body.get("text"), str) else ""


def _extract_segment_list(body: Any) -> list[dict[str, Any]]:
    return body["segments"] if isinstance(body, dict) and isinstance(body.get("segments"), list) else []


def _system_segments(body: Any, duration: float | None, timestamp_unit: str) -> tuple[list[Segment], dict[str, Any], list[str]]:
    """Validate timestamps and convert the API contract to seconds.

    ``auto`` compares both interpretations against the measured audio duration.
    Explicit units are still checked for an implausibly tiny/large converted
    timeline so a seconds-vs-milliseconds mistake cannot silently produce DER=1.
    """
    raw = _extract_segment_list(body)
    result: list[Segment] = []
    errors: list[str] = []
    warnings: list[str] = []
    invalid_count = 0
    raw_max_end = 0.0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            invalid_count += 1; errors.append(f"segment[{index}] is not an object"); continue
        start = _number(item.get("start", item.get("begin", item.get("start_time"))))
        end = _number(item.get("end", item.get("finish", item.get("end_time"))))
        speaker = item.get("spk", item.get("speaker", item.get("speaker_id", item.get("speakerId", ""))))
        text = item.get("text", item.get("sentence", item.get("content", "")))
        if start is None or end is None:
            invalid_count += 1; errors.append(f"segment[{index}] has non-numeric start/end"); continue
        if not _valid_speaker(speaker):
            invalid_count += 1; errors.append(f"segment[{index}] has empty speaker"); continue
        if not isinstance(text, str):
            invalid_count += 1; errors.append(f"segment[{index}] text is not a string"); continue
        if start < 0 or end <= start:
            invalid_count += 1; errors.append(f"segment[{index}] invalid interval [{start}, {end}]"); continue
        raw_max_end = max(raw_max_end, end)
        result.append(Segment(start, end, str(speaker).strip(), text))

    used = timestamp_unit
    if timestamp_unit == "auto":
        if not result:
            used = "s"
            warnings.append("timestamp_unit=auto with no segments; assumed seconds")
        elif duration is None:
            used = "ms" if raw_max_end >= 1000.0 else "s"
            errors.append(
                f"timestamp_unit=auto requires audio duration for validation; assumed {used} but DER/cpDER are blocked"
            )
        else:
            sec_error = abs(raw_max_end - duration) / max(duration, 1.0)
            ms_error = abs(raw_max_end / 1000.0 - duration) / max(duration, 1.0)
            used = "ms" if ms_error < sec_error else "s"
            if min(sec_error, ms_error) > AUTO_TIMESTAMP_TAIL_TOLERANCE:
                errors.append("timestamp_unit=auto: neither unit matches the audio tail within 2%; use explicit ms/s; speaker metrics blocked")
            if abs(ms_error - sec_error) < 0.05:
                # A close tie means the evaluator cannot establish the unit
                # from evidence.  Do not publish DER/cpDER for an arbitrary
                # interpretation; text metrics may still be retained.
                errors.append("timestamp_unit=auto was ambiguous; DER/cpDER blocked until the unit is explicit")
    if used not in {"ms", "s"}:
        errors.append(f"unsupported timestamp unit: {used}")
        used = "s"
    if used == "ms":
        result = [Segment(x.start / 1000.0, x.end / 1000.0, x.speaker, x.text) for x in result]
    converted_max_end = max((x.end for x in result), default=0.0)

    # Strong sanity checks catch orders-of-magnitude unit mismatches while
    # allowing most short utterances to occupy only part of the file.
    if duration is not None and converted_max_end > 0:
        if converted_max_end > duration * 10.0 + 1.0:
            errors.append(f"converted timestamps exceed audio duration by >10x: {converted_max_end:.6f}s > {duration:.6f}s")
        elif duration >= 5.0 and converted_max_end < max(0.05, duration * 0.005):
            warnings.append("very short speech coverage; explicit timestamp unit is trusted, not inferred from coverage")

    clipped_count = 0
    if duration is not None:
        clipped: list[Segment] = []
        for item in result:
            start_value, end_value = item.start, item.end
            if start_value >= duration or end_value > duration:
                clipped_count += 1
                message = f"segment clipped to audio duration: [{start_value:.6f}, {end_value:.6f}] > {duration:.6f}"
                if (end_value - duration > SYSTEM_CUTOFF_TOLERANCE_SEC or
                        (start_value >= duration and normalize_text(item.text))):
                    errors.append(message + "; speaker metrics blocked")
                else:
                    warnings.append(message)
            start_value = max(0.0, min(start_value, duration))
            end_value = max(0.0, min(end_value, duration))
            if end_value > start_value:
                clipped.append(Segment(start_value, end_value, item.speaker, item.text))
        result = clipped
    metadata = {
        "timestamp_unit_requested": timestamp_unit,
        "timestamp_unit_used": used,
        "timestamp_conversion": "milliseconds_to_seconds" if used == "ms" else "none",
        "raw_segment_count": len(raw), "valid_segment_count": len(result),
        "invalid_segment_count": invalid_count, "clipped_segment_count": clipped_count,
        "raw_max_end": raw_max_end, "converted_max_end_sec": converted_max_end,
        "duration_sec": duration, "validation": "error" if errors else ("warning" if warnings else "ok"),
        "warnings": warnings,
    }
    return sorted(result, key=lambda x: (x.start, x.end, x.speaker)), metadata, errors


def _reference_paths(dataset: Path, stem: str, reference_dir: Path | None, rttm_dir: Path | None) -> tuple[Path | None, Path | None]:
    text_root, rttm_root = reference_dir or dataset / "TextGrid", rttm_dir or dataset / "TextGrid"
    textgrid, rttm = text_root / f"{stem}.TextGrid", rttm_root / f"{stem}.rttm"
    return (textgrid if textgrid.exists() else None, rttm if rttm.exists() else None)


def _load_offline_response(offline_dir: Path, audio: Path, dataset: Path, stem: str, *,
                           allow_legacy_names: bool = True, max_response_bytes: int = 32 * 1024 * 1024) -> Any | None:
    candidates = [offline_dir / "raw" / f"{_artifact_name(audio, dataset)}.json"]
    if allow_legacy_names:
        candidates += [offline_dir / "raw" / f"{audio.name}.json", offline_dir / "raw" / f"{stem}.json",
                       offline_dir / f"{audio.name}.json", offline_dir / f"{stem}.json"]
    for candidate in candidates:
        if candidate.is_file():
            with candidate.open("rb") as handle:
                raw = handle.read(max_response_bytes + 1)
            if len(raw) > max_response_bytes:
                raise ValueError("offline response exceeds max-response-bytes")
            body = json.loads(raw.decode("utf-8-sig"), parse_constant=_reject_json_constant)
            _validate_json_complexity(body)
            return body
    raise FileNotFoundError(f"offline response missing or legacy filename ambiguous for {audio}")


def _audio_duration(audio: Path) -> float | None:
    try:
        return audio_duration(audio)
    except Exception:
        return None


def _request_one(audio: Path, args: argparse.Namespace, index: int, limiter: "RateLimiter | None" = None) -> tuple[Any | None, dict[str, Any]]:
    digest = sha256_file(audio)
    manual_test_id_base = f"manual-{uuid.uuid4().hex[:12]}-{index:04d}-{digest[:12]}"
    config = ClientConfig(
        endpoint=args.endpoint, model=args.model, language=args.language,
        hotwords=args.hotwords, return_speaker_segments=args.return_speaker_segments,
        connect_timeout_seconds=args.connect_timeout,
        upload_write_timeout_seconds=args.upload_write_timeout,
        response_read_timeout_seconds=args.response_read_timeout,
        total_timeout_seconds=args.total_timeout, automatic_retries=0,
        follow_redirects=False, http_version="HTTP/1.1", use_proxy=args.use_proxy,
        verify_tls=not args.insecure, max_response_bytes=args.max_response_bytes,
    )
    audio_duration = _audio_duration(audio)
    LOG.info("  → Requesting ASR for %s (duration: %s)", audio.name, f"{audio_duration:.2f}s" if audio_duration else "unknown")
    if audio_duration is not None and audio_duration > SERVER_LIMITS["max_asr_audio_seconds"]:
        return None, {
            "succeeded": False, "manual_test_id_base": manual_test_id_base,
            "error": f"audio duration {audio_duration:.3f}s exceeds server max_asr_audio_seconds={SERVER_LIMITS['max_asr_audio_seconds']}",
            "error_stage": "client_validation", "audio_duration_sec": audio_duration,
            "client_config": config.public_dict(),
        }
    last: dict[str, Any] = {"succeeded": False, "manual_test_id_base": manual_test_id_base,
                            "audio_duration_sec": audio_duration,
                            "error": "not attempted", "client_config": config.public_dict()}
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, args.retries + 2):
        manual_test_id = f"{manual_test_id_base}-a{attempt:02d}"
        try:
            if limiter is not None:
                limiter.acquire()
            LOG.info("  → Uploading audio (size: %.2f MB)...", audio.stat().st_size / (1024 * 1024))
            body, metadata = post_audio(audio, manual_test_id, config)
            metadata["audio_duration_sec"] = audio_duration
            attempts.append({"manual_test_id": manual_test_id, "succeeded": True, **metadata})
            LOG.info("  ✓ Request succeeded (response time: %.2fs)", metadata.get("total_elapsed_sec", 0))
            return body, {"attempt": attempt, "attempts": attempts, "succeeded": True, **metadata,
                          "client_config": config.public_dict()}
        except AsrHttpError as exc:
            attempts.append({**exc.metadata, "manual_test_id": manual_test_id, "succeeded": False,
                             "error_stage": exc.stage, "error": str(exc)})
            last = {**last, **exc.metadata, "attempt": attempt, "manual_test_id": manual_test_id, "succeeded": False, "error": str(exc),
                    "error_stage": exc.stage, "status_code": exc.status_code}
            if attempt <= args.retries:
                delay = args.retry_backoff * (2 ** (attempt - 1))
                LOG.warning("%s failed (%d/%d) stage=%s, retrying in %.1fs: %s", audio.name, attempt, args.retries + 1, exc.stage, delay, exc)
                time.sleep(delay)
            else:
                LOG.error("  ✗ All retries exhausted for %s: %s", audio.name, exc)
    return None, {**last, "attempts": attempts}


class RateLimiter:
    """Global start-rate limit shared by all request workers, including retries."""

    def __init__(self, qps: float):
        self.interval = 1.0 / qps if qps else 0.0
        self.lock = threading.Lock()
        self.next_start = 0.0

    def acquire(self) -> None:
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            scheduled = max(now, self.next_start)
            self.next_start = scheduled + self.interval
        while (delay := scheduled - time.monotonic()) > 0:
            time.sleep(delay)


def _score_one(audio: Path, body: Any, dataset: Path, args: argparse.Namespace, output: Path) -> dict[str, Any]:
    stem = canonical_stem(audio)
    speaker_mode = args.return_speaker_segments
    textgrid_path, rttm_path = _reference_paths(dataset, stem, args.reference_dir, args.rttm_dir)
    expected = ["cer", "wer", "der", "cpder", "cpcer"] if speaker_mode else ["cer", "wer"]
    record: dict[str, Any] = {
        "audio": audio.name, "audio_path": str(audio.resolve()), "session_id": stem,
        "audio_sha256": sha256_file(audio), "scorer_version": SCORER_VERSION,
        "normalization_version": NORMALIZATION_VERSION, "status": "failed", "errors": [],
        "warnings": [], "evaluation_mode": "full" if speaker_mode else "text_only",
        "expected_metrics": expected, "metric_unavailable": {},
        "reference_textgrid": str(textgrid_path) if textgrid_path else None,
        "reference_rttm": str(rttm_path) if rttm_path else None,
        "reference_textgrid_sha256": sha256_file(textgrid_path) if textgrid_path else None,
        "reference_rttm_sha256": sha256_file(rttm_path) if rttm_path else None,
    }

    def unavailable(keys: Sequence[str], reason: str) -> None:
        for key in keys:
            record["metric_unavailable"][key] = reason

    if not speaker_mode:
        unavailable(["der", "cpder", "cpcer"], "disabled by --no-return-speaker-segments")
    if body is not None:
        raw_path = output / "raw" / f"{_artifact_name(audio, dataset)}.json"
        _json_file(raw_path, body)
        record["raw_response"] = str(raw_path.relative_to(output))
    # Score/offline validation is authoritative; transport metadata is audit-only.
    protocol_errors = validate_response_shape(body, require_segments=speaker_mode)
    record["response_protocol_errors"] = protocol_errors
    record["errors"].extend(f"response protocol: {error}" for error in protocol_errors)
    if not isinstance(body, dict):
        # Single clear error message
        if body is None:
            record["errors"].append("no ASR response")
        else:
            record["errors"].append("response body is not a JSON object")
        unavailable(expected, "no valid JSON object response")
        return record
    business_error = extract_business_error(body)
    if business_error:
        record["errors"].append(f"ASR business failure: {business_error}")
        unavailable(expected, "business failure, not an ASR hypothesis")
        return record

    if textgrid_path is None:
        record["errors"].append(f"missing reference TextGrid for {stem}")
    if speaker_mode and rttm_path is None:
        record["errors"].append(f"missing reference RTTM for {stem}")
    if (textgrid_path is None or (speaker_mode and rttm_path is None)) and not args.allow_missing_reference:
        unavailable(expected, "missing required reference; use --allow-missing-reference for partial scoring")
        return record
    duration = _audio_duration(audio)
    record["duration_sec"] = duration
    if duration is None:
        record["warnings"].append("audio duration unavailable; timestamp range cannot be fully validated")
    ref_text_segments: list[Segment] = []
    ref_rttm_segments: list[Segment] = []
    reference_text_valid = textgrid_path is not None
    reference_rttm_valid = rttm_path is not None
    try:
        if textgrid_path:
            ref_text_segments = parse_textgrid(textgrid_path)
    except Exception as exc:
        reference_text_valid = False
        record["errors"].append(f"reference TextGrid parse failed: {exc}")
    if speaker_mode:
        try:
            if rttm_path:
                ref_rttm_segments = parse_rttm(rttm_path, expected_file_id=stem)
        except Exception as exc:
            reference_rttm_valid = False
            record["errors"].append(f"reference RTTM parse failed: {exc}")
    if duration is not None:
        clip_reference = getattr(args, "clip_reference_to_audio", False)
        for label, intervals in (("TextGrid", ref_text_segments), ("RTTM", ref_rttm_segments)):
            for item in intervals:
                if item.end <= duration + 1e-6:
                    continue
                overrun = item.end - duration
                # Explicit excerpt mode can clip RTTM and omit text wholly outside
                # the excerpt. A cut THROUGH a text interval remains invalid: the
                # surviving spoken characters cannot be inferred from its duration.
                intentional_cut = clip_reference and (label == "RTTM" or item.start >= duration)
                if overrun <= REFERENCE_CUTOFF_TOLERANCE_SEC or intentional_cut:
                    record["warnings"].append(
                        f"reference {label} exceeds audio by {overrun:.3f}s; clipped" +
                        (" (explicit excerpt mode)" if intentional_cut else " (tail tolerance)"))
                else:
                    record["errors"].append(f"reference {label} exceeds audio by {overrun:.3f}s; metric blocked")
                    if label == "TextGrid":
                        reference_text_valid = False
                    else:
                        reference_rttm_valid = False
            # Never silently delete an entire reference text interval within the
            # tail tolerance: its text cannot belong to the evaluated audio.
            if label == "TextGrid" and not clip_reference and any(x.start >= duration for x in intervals):
                reference_text_valid = False
                record["errors"].append("reference TextGrid contains text wholly outside the audio")
        ref_text_segments = _clip(ref_text_segments, duration)
        ref_rttm_segments = _clip(ref_rttm_segments, duration)
    record["reference_text_valid"] = reference_text_valid
    record["reference_rttm_valid"] = reference_rttm_valid if speaker_mode else None

    text_valid = isinstance(body.get("text"), str)
    text = _extract_text(body)
    record["recognized_text"] = text
    if text_valid and not text.strip() and not args.allow_empty_text:
        record["errors"].append("empty ASR text (scored as deletions, not discarded)")
    system_segments: list[Segment] = []
    segments_valid = False
    cpcer_text_valid = False
    if speaker_mode:
        segments_valid = text_valid and isinstance(body.get("segments"), list) and not any(
            error.startswith("segments[") or error.startswith("response field segments") or
            error.startswith("response missing required field: segments") for error in protocol_errors)
        if text.strip() and body.get("segments") == []:
            record["errors"].append("non-empty transcript with empty speaker segments")
            segments_valid = False
        system_segments, ts_meta, ts_errors = _system_segments(body, duration, args.timestamp_unit)
        segments_valid = segments_valid and not ts_errors
        record["timestamp"] = ts_meta
        record["warnings"].extend(ts_meta.get("warnings", []))
        record["errors"].extend(ts_errors)
        
        # Add segment validity summary for audit
        record["segment_validity_summary"] = {
            "raw_count": ts_meta.get("raw_segment_count", 0),
            "valid_count": ts_meta.get("valid_segment_count", 0),
            "invalid_count": ts_meta.get("invalid_segment_count", 0),
            "clipped_count": ts_meta.get("clipped_segment_count", 0),
        }
        
        # Timing order can differ for overlapping speakers. Compare character
        # multisets, not a forced chronology, to detect missing/duplicated text.
        segment_chars = "".join(normalize_text(x.text, strip_punctuation=args.strip_punctuation)
                                for x in system_segments)
        transcript_chars = normalize_text(text, strip_punctuation=args.strip_punctuation)
        
        # 使用Counter比较字符分布，允许微小差异（<1%）
        counter_segments = Counter(segment_chars)
        counter_transcript = Counter(transcript_chars)
        
        # 计算差异程度
        total_chars = len(transcript_chars)
        char_diff = sum(abs(counter_transcript.get(c, 0) - counter_segments.get(c, 0)) 
                       for c in set(counter_transcript) | set(counter_segments))
        char_diff_rate = char_diff / total_chars if total_chars > 0 else 0
        
        # 严格匹配或微小差异都允许
        cpcer_text_valid = (counter_segments == counter_transcript) or (char_diff_rate < 0.01)
        
        if segments_valid and not cpcer_text_valid:
            record["errors"].append(f"segment text does not account for full transcript (diff: {char_diff}/{total_chars} = {char_diff_rate:.2%}); cpCER blocked")
        elif segments_valid and char_diff > 0:
            record["warnings"].append(f"minor text mismatch between segments and transcript ({char_diff} chars, {char_diff_rate:.2%}), cpCER computed with segments")
        
        record["system_segment_count"] = len(system_segments)
        record["system_speaker_count"] = len({x.speaker for x in system_segments})
        if system_segments and segments_valid:
            system_rttm = output / "system_rttm" / f"{_artifact_name(audio, dataset)}.rttm"
            write_rttm(system_rttm, stem, system_segments)
            record["system_rttm"] = str(system_rttm.relative_to(output))

    reference_text = " ".join(x.text for x in sorted(ref_text_segments, key=lambda x: (x.start, x.end)))
    if reference_text_valid and text_valid and normalize_text(reference_text, strip_punctuation=args.strip_punctuation):
        try:
            text_score = score_text(reference_text, text, strip_punctuation=args.strip_punctuation)
            record["cer"] = {**asdict(text_score.cer), "errors": text_score.cer.errors, "rate": text_score.cer.rate}
            record["wer"] = {**asdict(text_score.wer), "errors": text_score.wer.errors, "rate": text_score.wer.rate}
        except Exception as exc:
            unavailable(["cer", "wer"], f"text scoring failed: {exc}")
    else:
        unavailable(["cer", "wer"], "invalid/missing reference transcript or response text; denominator may be empty")
    if speaker_mode:
        if reference_rttm_valid and ref_rttm_segments and segments_valid and duration is not None:
            try:
                der = score_diarization(ref_rttm_segments, system_segments, collar_sec=args.collar)
                record["der"] = asdict(der)
                record["cpder"] = {**asdict(der), "cpder": der.cpder}
                if der.der is None:
                    unavailable(["der", "cpder"], "no reference speech remains after collar exclusion")
            except Exception as exc:
                unavailable(["der", "cpder"], f"diarization scoring failed: {exc}")
        else:
            unavailable(["der", "cpder"], "invalid/missing reference RTTM, system segments or measured duration")
        # cpCER depends on TextGrid speaker text, NOT RTTM. Bad RTTM alone cannot
        # invalidate otherwise sound cpCER; timing DER is independently blocked.
        if ref_text_segments and segments_valid and reference_text_valid and cpcer_text_valid:
            try:
                cpcer = score_cpcer(ref_text_segments, system_segments, strip_punctuation=args.strip_punctuation)
                record["cpcer"] = asdict(cpcer)
                if cpcer.cpcer is None:
                    unavailable(["cpcer"], "cpCER reference has no scoreable units")
            except Exception as exc:
                unavailable(["cpcer"], f"cpCER scoring failed: {exc}")
        else:
            unavailable(["cpcer"], "invalid/missing reference speaker text or consistent system segments")
    for key in expected:
        if key in record["metric_unavailable"]:
            record["errors"].append(f"{key} unavailable: {record['metric_unavailable'][key]}")
    available = [key for key in expected if key in record and key not in record["metric_unavailable"]]
    record["available_metrics"] = available
    record["status"] = "ok" if not record["errors"] and len(available) == len(expected) else "partial" if available else "failed"
    return record


def _aggregate(records: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    rows = [x for x in records if x.get("status") in {"ok", "partial"}]
    def value(row: dict[str, Any], key: str) -> float | None:
        obj = row.get(key)
        if not isinstance(obj, dict): return None
        for field in ("rate", "cpcer", "cpder", "der"):
            if obj.get(field) is not None: return float(obj[field])
        return None
    def vals(key: str) -> list[float]: return [v for x in rows if (v := value(x, key)) is not None]
    def mean(key: str) -> float | None:
        v = vals(key); return sum(v) / len(v) if v else None
    def p95(v: list[float]) -> float | None:
        return percentile(v, 95) if v else None
    def micro(key: str) -> float | None:
        """Compute micro-average for CER/WER/cpCER only.
        
        DER/cpDER use reference_speech_time as denominator, not reference_units,
        so they are computed separately below.
        """
        if key in ("der", "cpder"):
            raise ValueError(f"micro() does not support {key}; use explicit der_micro/cpder_micro computation")
        items = [x[key] for x in rows if isinstance(x.get(key), dict) and value(x, key) is not None]
        den = sum(int(x.get("reference_units", 0)) for x in items)
        num = sum(int(x.get("errors", x.get("substitutions", 0) + x.get("deletions", 0) + x.get("insertions", 0))) for x in items)
        return num / den if den else None
    der_rows = [x["der"] for x in rows if isinstance(x.get("der"), dict) and x["der"].get("der") is not None]
    den = sum(float(x["reference_speech_time"]) for x in der_rows)
    miss = sum(float(x["miss"]) for x in der_rows); fa = sum(float(x["false_alarm"]) for x in der_rows); conf = sum(float(x["confusion"]) for x in der_rows)
    manual_ids = [entry["manual_test_id"] for row in records
                  if isinstance((request := row.get("request")), dict)
                  for entry in (request.get("attempts") or [request])
                  if isinstance(entry, dict) and entry.get("manual_test_id")]
    id_counts = Counter(manual_ids)
    timestamp_rows = [x.get("timestamp") for x in records if isinstance(x.get("timestamp"), dict)]
    timestamp_units = Counter(x.get("timestamp_unit_used") for x in timestamp_rows if x.get("timestamp_unit_used"))
    timestamp_validations = Counter(x.get("validation") for x in timestamp_rows if x.get("validation"))
    expected = ("cer", "wer", "der", "cpder", "cpcer") if args.return_speaker_segments else ("cer", "wer")
    counts = {key: len(vals(key)) for key in ("cer", "wer", "der", "cpder", "cpcer")}
    request_attempts = [attempt for row in records if isinstance(row.get("request"), dict)
                        for attempt in (row["request"].get("attempts") or [row["request"]])
                        if isinstance(attempt, dict)]
    return {
        "scorer_version": SCORER_VERSION, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "evaluation_mode": "full" if args.return_speaker_segments else "text_only",
        "metrics_complete": (bool(records) and all(x.get("status") == "ok" for x in records)
                             and all(counts[key] == len(records) for key in expected)
                             and len(manual_ids) == len(set(manual_ids))),
        "aggregation_scope": "per-metric valid samples only; failures/N/A are NOT zero-error samples; check coverage before acceptance",
        "percentile_method": "nearest-rank: ordered[max(1, ceil(percent/100 * N)) - 1]",
        "total_response_bytes": sum(int(x.get("response_bytes", 0)) for x in request_attempts),
        "response_protocol_invalid_count": sum(bool(x.get("response_protocol_errors")) for x in records),
        "request_failed_count": sum(x.get("request", {}).get("succeeded") is False for x in records),
        "manual_test_id_count": len(manual_ids),
        "manual_test_id_unique": len(manual_ids) == len(set(manual_ids)),
        "duplicate_manual_test_ids": sorted(x for x, count in id_counts.items() if count > 1),
        "dataset_dir": str(args.dataset_dir), "audio_count": len(records),
        "ok_count": sum(x.get("status") == "ok" for x in records),
        "partial_count": sum(x.get("status") == "partial" for x in records),
        "failed_count": sum(x.get("status") == "failed" for x in records),
        "cer_macro": mean("cer"), "cer_p95": p95(vals("cer")), "cer_micro": micro("cer"),
        "wer_macro": mean("wer"), "wer_p95": p95(vals("wer")), "wer_micro": micro("wer"),
        "cpcer_macro": mean("cpcer"), "cpcer_p95": p95(vals("cpcer")), "cpcer_micro": micro("cpcer"),
        "der_macro": sum(float(x["der"]) for x in der_rows) / len(der_rows) if der_rows else None,
        "der_micro": (miss + fa + conf) / den if den else None,
        "cpder_macro": mean("cpder"),
        "cpder_micro": (miss + fa + conf) / den if den else None,
        "metric_sample_counts": counts,
        "metric_coverage": {key: counts[key] / len(records) if records else None for key in counts},
        "metric_coverage_within_scored": {key: counts[key] / len(rows) if rows else None for key in counts},
        "expected_metrics": list(expected),
        "reference_speech_time_sec": den, "miss_sec": miss, "false_alarm_sec": fa, "confusion_sec": conf,
        "normalization_version": NORMALIZATION_VERSION,
        "normalization": "NFKC + lowercase + remove annotation tags; whitespace delimits words; semantic symbols retained; punctuation " + ("removed" if args.strip_punctuation else "retained"),
        "timestamp_unit_requested": args.timestamp_unit,
        "timestamp_unit_used_counts": dict(timestamp_units),
        "timestamp_validation_counts": dict(timestamp_validations),
        "strip_punctuation": args.strip_punctuation,
        "max_alignment_product": MAX_ALIGNMENT_PRODUCT,
        "collar_sec": args.collar,
        "collar_definition": "speaker-change-point collar: collar_sec excluded on each side of every speaker active-set change in the merged reference; NOT numerically identical to NIST md-eval.pl which applies collar at every RTTM turn boundary; this variant is annotation-granularity-invariant",
        "clip_reference_to_audio": getattr(args, "clip_reference_to_audio", False),
        "overlap_included": True,
        "cpder_definition": "Same numeric score as DER in this evaluator: globally optimal one-to-one speaker permutation; collar configured by collar_sec; overlap included",
        "cpcer_definition": "speaker-concatenated CER with a globally optimal one-to-one permutation",
        "records": [{"audio": x.get("audio"), "status": x.get("status"), "errors": x.get("errors", [])} for x in records],
    }


def run_evaluation_self_test() -> None:
    """Small golden checks for normalization, timestamps and diarization math."""
    assert normalize_text("hello\n\tworld", keep_spaces=True) == "hello world"
    assert tokenize_wer("hello\rworld") == ["hello", "world"]
    assert normalize_asr_text("hello\nworld", keep_spaces=True) == "hello world"
    text = score_text("今天天气\n很好", "今天天气 很好")
    assert text.cer.errors == 0 and text.wer.errors == 0
    segments, meta, errors = _system_segments(
        {"segments": [{"start": 0, "end": 2000, "spk": "A", "text": "x"}]},
        2.0, "auto")
    assert not errors and meta["timestamp_unit_used"] == "ms" and segments[0].end == 2.0
    segments, meta, errors = _system_segments(
        {"segments": [{"start": 0, "end": 2, "spk": "A", "text": "x"}]},
        2.0, "auto")
    assert not errors and meta["timestamp_unit_used"] == "s" and segments[0].end == 2.0
    
    # Test basic DER without collar
    no_collar = score_diarization(
        [Segment(0, 1, "r1"), Segment(1, 2, "r2")],
        [Segment(0, 1, "s1"), Segment(1, 2, "s2")], collar_sec=0)
    assert no_collar.reference_speech_time == 2.0
    
    # Test DER with collar at speaker change boundary
    # [r1][r2]: speaker changes at t=1
    # collar=0.25 excludes [0.75, 1.25]
    # denominator = 2.0 - 0.5 = 1.5
    with_collar = score_diarization(
        [Segment(0, 1, "r1"), Segment(1, 2, "r2")],
        [Segment(0, 1, "s1"), Segment(1, 2, "s2")], collar_sec=0.25)
    assert abs(with_collar.reference_speech_time - 1.5) < 1e-9, \
        f"expected 1.5, got {with_collar.reference_speech_time}"
    
    # Critical test: same speaker internal split should NOT add collar
    # [r1, r1, r2] has speaker changes at t=2 only (r1 continues from 0-2)
    internal_split = score_diarization(
        [Segment(0, 1, "r1"), Segment(1, 2, "r1"), Segment(2, 3, "r2")],
        [Segment(0, 1, "s1"), Segment(1, 2, "s1"), Segment(2, 3, "s2")],
        collar_sec=0.25)
    # Collar only at t=2: [1.75, 2.25], denominator = 3.0 - 0.5 = 2.5
    assert abs(internal_split.reference_speech_time - 2.5) < 1e-9, \
        f"internal split collar test failed: expected 2.5, got {internal_split.reference_speech_time}"
    
    # Test multiple mask windows across timeline intervals
    # Reference: [r1 0-0.5], [r2 0.6-1.0], [r1 1.1-1.5]
    # System: [s1 0-0.5], [s2 0.6-1.0], [s1 1.1-1.5]
    # Speaker changes at t=0.6 and t=1.1, collar=0.05 each side
    # Excluded: [0.55, 0.65] and [1.05, 1.15] = 0.2s total
    multi_mask = score_diarization(
        [Segment(0, 0.5, "r1"), Segment(0.6, 1.0, "r2"), Segment(1.1, 1.5, "r1")],
        [Segment(0, 0.5, "s1"), Segment(0.6, 1.0, "s2"), Segment(1.1, 1.5, "s1")],
        collar_sec=0.05)
    # Total reference time: 0.5 + 0.4 + 0.4 = 1.3s, excluded 0.2s = 1.1s
    assert abs(multi_mask.reference_speech_time - 1.1) < 1e-9, \
        f"multi-mask test failed: expected 1.1, got {multi_mask.reference_speech_time}"
    
    assert score_cpcer(
        [Segment(0, 1, "r1", "甲"), Segment(1, 2, "r2", "乙")],
        [Segment(0, 1, "s2", "乙"), Segment(1, 2, "s1", "甲")],
    ).cpcer == 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Batch ASR CER/DER/cpDER evaluator")
    p.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    p.add_argument("--audio", type=Path, help="score only this audio file")
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--offline-dir", type=Path, default=None)
    p.add_argument("--reference-dir", type=Path, default=None)
    p.add_argument("--rttm-dir", type=Path, default=None)
    p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--endpoint", default=ASR_BINDING_HOST)
    p.add_argument("--model", default=ASR_MODEL)
    p.add_argument("--language", default=os.getenv("ASR_LANGUAGE", "zh"))
    p.add_argument("--hotwords", default="")
    p.add_argument(
        "--timestamp-unit", choices=TIMESTAMP_UNITS, default="auto",
        help="生产验收请显式指定 ms/s；auto 仅在语音末端距音频末端不超过 2%% 时评分，否则阻止 speaker 指标",
    )
    p.add_argument(
        "--collar", type=float, default=0.0,
        help="DER collar 每侧宽度（秒，NIST/md-eval 口径）；默认 0 保持历史无 collar 分数，传 0.25 表示每侧 250 ms",
    )
    p.add_argument(
        "--return-speaker-segments", action=argparse.BooleanOptionalAction, default=True,
        help="是否要求服务端返回 speaker segments；关闭时只评估文本指标",
    )
    p.add_argument("--connect-timeout", type=float, default=15.0)
    p.add_argument("--upload-write-timeout", type=float, default=900.0)
    p.add_argument("--response-read-timeout", type=float, default=3600.0)
    p.add_argument("--total-timeout", type=float, default=3600.0)
    p.add_argument("--max-response-bytes", type=int, default=32 * 1024 * 1024)
    p.add_argument("--retries", type=int, default=0)
    p.add_argument("--retry-backoff", type=float, default=2.0)
    p.add_argument("--use-proxy", action="store_true")
    p.add_argument(
        "--insecure", action=argparse.BooleanOptionalAction,
        default=not ASR_VERIFY_TLS if os.getenv("ASR_GATEWAY_INSECURE", "").lower() not in {"1", "true", "yes", "0", "false", "no"} else os.getenv("ASR_GATEWAY_INSECURE", "").lower() in {"1", "true", "yes"},
        help="禁用 TLS 证书校验；仅限受信内网临时测试，也可用 ASR_GATEWAY_INSECURE=1 或修改代码中的 ASR_VERIFY_TLS",
    )
    p.add_argument("--max-in-flight", type=int, default=1)
    p.add_argument("--qps", type=float, default=0.0, help="maximum request starts per second; 0 disables rate limit")
    p.add_argument("--strip-punctuation", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--allow-empty-text", action="store_true")
    p.add_argument("--allow-missing-reference", action="store_true")
    p.add_argument("--clip-reference-to-audio", action="store_true",
                   help="显式截取音频评测：裁剪 RTTM，移除截点后文本；切穿文本区间仍拒绝文本评分")
    p.add_argument("--debug", action=argparse.BooleanOptionalAction, default=False,
                   help="启用调试模式，输出详细的请求和错误信息；也可用 ASR_EVAL_DEBUG=1")
    p.add_argument("--self-test", action="store_true", help="运行新版 CER/WER/DER/cpDER/cpCER 黄金用例")
    return p


@contextmanager
def _exclusive_run_output(output: Path):
    lock_path = output / ".asr-eval.lock"
    try:
        lock = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ValueError(f"output directory is in use or has a stale lock: {lock_path}; choose a new directory") from exc
    try:
        with lock:
            lock.write(f"pid={os.getpid()}\n")
            lock.flush()
            if (output / "summary.json").exists():
                raise ValueError("output-dir already contains a completed run; choose a new directory")
            yield
    finally:
        lock_path.unlink(missing_ok=True)


def evaluation_main(argv: list[str] | None = None) -> int:
    parser = build_parser(); args = parser.parse_args(argv)
    
    # Apply --debug flag to override environment variable
    # Note: Modifies global DEBUG_MODE at entry point before any parallel execution
    global DEBUG_MODE
    if hasattr(args, 'debug') and args.debug is not None:
        DEBUG_MODE = args.debug
    
    if args.self_test:
        run_evaluation_self_test()
        print("evaluation self-test passed")
        return 0
    if (any(not math.isfinite(x) or x <= 0 for x in
            (args.connect_timeout, args.upload_write_timeout, args.response_read_timeout, args.total_timeout))
            or args.retries < 0 or not math.isfinite(args.retry_backoff) or args.retry_backoff < 0):
        parser.error("all timeouts must be finite and > 0; retries/backoff must be finite and >= 0")
    if args.max_response_bytes <= 0 or not 1 <= args.max_in_flight <= 30 or not math.isfinite(args.qps) or args.qps < 0:
        parser.error("max-response-bytes > 0; max-in-flight in [1,30]; qps finite and >= 0")
    if not math.isfinite(args.collar) or args.collar < 0:
        parser.error("--collar must be finite and >= 0")
    if args.audio is not None:
        args.audio = args.audio.resolve()
        if not args.audio.is_file():
            parser.error(f"audio file not found: {args.audio}")
        if args.dataset_dir == parser.get_default("dataset_dir"):
            args.dataset_dir = args.audio.parent
    args.dataset_dir = args.dataset_dir.resolve()
    args.reference_dir = args.reference_dir.resolve() if args.reference_dir else None
    args.rttm_dir = args.rttm_dir.resolve() if args.rttm_dir else None
    args.offline_dir = args.offline_dir.resolve() if args.offline_dir else None
    args.hotwords = args.hotwords.strip()
    try:
        audios = [args.audio] if args.audio is not None else find_audio_files(args.dataset_dir, args.exclude)
    except ValueError as exc:
        parser.error(str(exc))
    if not audios: parser.error(f"no supported audio files found in {args.dataset_dir}")
    output = (args.output_dir or args.dataset_dir / "runs" / (datetime.now().strftime("%Y%m%dT%H%M%S_%f") + "-" + uuid.uuid4().hex[:8])).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "summary.json").exists():
        parser.error("output-dir already contains a completed run; choose a new directory to preserve audit history")
    audio_name_counts = Counter(x.name for x in audios)
    stem_counts = Counter(canonical_stem(x) for x in audios)
    try:
        with _exclusive_run_output(output):
            if not LOG.handlers: LOG.addHandler(logging.StreamHandler())
            LOG.setLevel(logging.INFO)
            
            # Print startup information
            print("\n" + "=" * 80)
            print("ASR GATEWAY EVALUATION STARTED")
            print("=" * 80)
            print(f"Dataset:     {args.dataset_dir}")
            print(f"Audio files: {len(audios)}")
            print(f"Endpoint:    {_safe_endpoint(args.endpoint) if not args.offline_dir else 'offline mode'}")
            print(f"Model:       {args.model}")
            print(f"Output:      {output}")
            print(f"TLS verify:  {not args.insecure}")
            if args.offline_dir:
                print(f"Mode:        Offline (using cached responses)")
            else:
                print(f"Timeout:     connect={args.connect_timeout}s, upload={args.upload_write_timeout}s, read={args.response_read_timeout}s, total={args.total_timeout}s")
                print(f"Concurrency: {args.max_in_flight} parallel request(s)")
            print("=" * 80)
            print("")
            
            records: list[dict[str, Any]] = []
            limiter = RateLimiter(args.qps)
        
            def requests():
                if args.offline_dir:
                    for index, audio in enumerate(audios, 1):
                        try:
                            body = _load_offline_response(
                                args.offline_dir, audio, args.dataset_dir, canonical_stem(audio),
                                allow_legacy_names=audio_name_counts[audio.name] == 1 and stem_counts[canonical_stem(audio)] == 1,
                                max_response_bytes=args.max_response_bytes)
                            request = {"mode": "offline"}
                        except Exception as exc:
                            body, request = None, {"mode": "offline", "succeeded": False,
                                                   "error_stage": "offline_json", "error": str(exc)}
                        yield index, audio, body, request
                elif args.max_in_flight == 1:
                    for index, audio in enumerate(audios, 1):
                        try:
                            body, request = _request_one(audio, args, index, limiter)
                        except Exception as exc:
                            body, request = None, {"succeeded": False, "error_stage": "client_failure", "error": str(exc)}
                        yield index, audio, body, request
                else:
                    # Bound outstanding tasks to worker count; scoring/output remains serialized.
                    with ThreadPoolExecutor(max_workers=args.max_in_flight) as pool:
                        pending = {}
                        source = iter(enumerate(audios, 1))
                        def submit_next():
                            try:
                                index, audio = next(source)
                            except StopIteration:
                                return
                            pending[pool.submit(_request_one, audio, args, index, limiter)] = (index, audio)
                        for _ in range(args.max_in_flight):
                            submit_next()
                        while pending:
                            done, _ = wait(pending, return_when=FIRST_COMPLETED)
                            for future in done:
                                index, audio = pending.pop(future)
                                try:
                                    body, request = future.result()
                                except Exception as exc:
                                    body, request = None, {"succeeded": False, "error_stage": "client_failure", "error": str(exc)}
                                submit_next()
                                yield index, audio, body, request
        
            LOG.info("=" * 80)
            LOG.info("Starting evaluation: %d audio files", len(audios))
            LOG.info("Endpoint: %s", _safe_endpoint(args.endpoint) if not args.offline_dir else "offline")
            LOG.info("Output directory: %s", output)
            LOG.info("=" * 80)
            
            for index, audio, body, request in requests():
                LOG.info("\n[%d/%d] Processing: %s", index, len(audios), audio.name)
                LOG.info("  Audio size: %.2f MB", audio.stat().st_size / (1024 * 1024))
                try:
                    LOG.info("  → Scoring metrics...")
                    record = _score_one(audio, body, args.dataset_dir, args, output)
                    LOG.info("  ✓ Scoring completed")
                except Exception as exc:
                    LOG.error("  ✗ Scoring failed: %s", exc)
                    record = {"audio": audio.name, "status": "failed", "errors": [f"scoring failed: {exc}"]}
                if isinstance(request, dict) and request.get("succeeded") is False and request.get("error"):
                    request_error = f"request failed [{request.get('error_stage', 'unknown')}]: {request['error']}"
                    if request_error not in record["errors"]:
                        record["errors"].append(request_error)
                if isinstance(request, dict) and request.get("succeeded") is False:
                    record["status"] = "failed"
                    for key in ("cer", "wer", "der", "cpder", "cpcer"):
                        record.pop(key, None)
                # Scoring validation is authoritative. Do not overwrite its error list
                # with independently collected HTTP audit metadata.
                record["request"] = request
                _json_file(output / "per_file" / f"{_artifact_name(audio, args.dataset_dir)}.json", record)
                records.append(record)
                
                # Enhanced status output
                cer_val = record.get("cer", {}).get("rate")
                der_val = record.get("der", {}).get("der")
                cpder_val = record.get("cpder", {}).get("der")
                cpcer_val = record.get("cpcer", {}).get("cpcer")
                
                LOG.info("  📊 Results:")
                LOG.info("     Status: %s", record.get("status"))
                LOG.info("     CER:    %s", f"{cer_val:.2%}" if cer_val is not None else "N/A")
                LOG.info("     DER:    %s", f"{der_val:.2%}" if der_val is not None else "N/A")
                LOG.info("     cpDER:  %s", f"{cpder_val:.2%}" if cpder_val is not None else "N/A")
                LOG.info("     cpCER:  %s", f"{cpcer_val:.2%}" if cpcer_val is not None else "N/A")
                if record.get("errors"):
                    LOG.warning("  ⚠ Errors: %s", "; ".join(record["errors"][:3]))
                LOG.info("  Progress: %.1f%% complete", (index / len(audios)) * 100)
            records.sort(key=lambda row: row.get("audio_path", row.get("audio", "")))
            LOG.info("\n" + "=" * 80)
            LOG.info("Generating summary report...")
            summary = _aggregate(records, args)
            summary["run_profile"] = {
                "endpoint": _safe_endpoint(args.endpoint) if not args.offline_dir else None,
                "timestamp_unit_requested": args.timestamp_unit,
                "collar_sec": args.collar,
                "collar_definition": "speaker-change-point collar: collar_sec excluded on each side of every speaker active-set change in the merged reference; NOT numerically identical to NIST md-eval.pl which applies collar at every RTTM turn boundary; this variant is annotation-granularity-invariant",
                "clip_reference_to_audio": getattr(args, "clip_reference_to_audio", False),
                "overlap_included": True,
                "normalization_version": NORMALIZATION_VERSION,
                "model": args.model, "language": args.language, "hotwords": args.hotwords,
                "verify_tls": not args.insecure,
                "max_in_flight": args.max_in_flight,
                "qps": args.qps,
                "strip_punctuation": args.strip_punctuation,
                "max_alignment_product": MAX_ALIGNMENT_PRODUCT,
                "client_config": {
                    **CLIENT_CONFIG,
                    "connect_timeout_seconds": args.connect_timeout,
                    "upload_write_timeout_seconds": args.upload_write_timeout,
                    "response_read_timeout_seconds": args.response_read_timeout,
                    "total_timeout_seconds": args.total_timeout,
                    "automatic_retries": 0,
                    "use_proxy": args.use_proxy,
                    "return_speaker_segments": args.return_speaker_segments,
                    "max_response_bytes": args.max_response_bytes,
                    "verify_tls": not args.insecure,
                },
                "server_limits": SERVER_LIMITS,
            }
            # Add warnings if TLS verification is disabled
            if args.insecure:
                summary["run_warnings"] = summary.get("run_warnings", []) + [
                    "TLS certificate verification disabled (--insecure); suitable only for trusted internal testing"
                ]
            _json_file(output / "summary.json", summary)
            rows = ["audio\tstatus\tcer\tder\tcpder\tcpcer\terrors"]
            for row in records:
                rows.append("\t".join([str(row.get("audio", "")), str(row.get("status", "")), str(row.get("cer", {}).get("rate", "")), str(row.get("der", {}).get("der", "")), str(row.get("cpder", {}).get("der", "")), str(row.get("cpcer", {}).get("cpcer", "")), " | ".join(row.get("errors", []))]))
            _atomic_write(output / "per_file.tsv", "\n".join(rows) + "\n")
            
            LOG.info("\n" + "=" * 80)
            LOG.info("EVALUATION COMPLETE")
            LOG.info("=" * 80)
            LOG.info("Output directory: %s", output)
            LOG.info("Total files:      %d", len(records))
            LOG.info("OK:               %d", summary.get("ok_count", 0))
            LOG.info("Partial:          %d", summary.get("partial_count", 0))
            LOG.info("Failed:           %d", summary.get("failed_count", 0))
            LOG.info("")
            LOG.info("Metrics (macro averages):")
            LOG.info("  CER:   %s", f"{summary.get('cer_macro'):.2%}" if summary.get("cer_macro") is not None else "N/A")
            LOG.info("  WER:   %s", f"{summary.get('wer_macro'):.2%}" if summary.get("wer_macro") is not None else "N/A")
            LOG.info("  DER:   %s", f"{summary.get('der_macro'):.2%}" if summary.get("der_macro") is not None else "N/A")
            LOG.info("  cpDER: %s", f"{summary.get('cpder_macro'):.2%}" if summary.get("cpder_macro") is not None else "N/A")
            LOG.info("  cpCER: %s", f"{summary.get('cpcer_macro'):.2%}" if summary.get("cpcer_macro") is not None else "N/A")
            LOG.info("=" * 80)
            return 0 if summary["metrics_complete"] else 2
    except ValueError as exc:
        parser.error(str(exc))




# --- Original HMAC/streaming gateway compatibility ---------------------------
# Kept in this same executable: old HMAC JSON requests and performance reporting
# use a different wire protocol from the multipart diarization evaluator above.
#!/usr/bin/env python3
"""
通过 AI Gateway 访问 ASR 服务的测试程序
参考 gateway_api_npu.py 的认证方式，访问 ASR 接口
"""
import argparse
import asyncio
import codecs
import requests
import urllib3

# 禁用 InsecureRequestWarning 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import os
import base64
import json
import math
import re
import shutil
import statistics
import subprocess
import time
import unicodedata
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
from hashlib import sha256
from typing import Any, Optional

# 兼容旧版 --legacy/--stream 参数；主网关配置统一使用文件顶部这一套。
ASR_STREAM_WS_URL = os.getenv("ASR_STREAM_WS_URL", "ws://127.0.0.1:10017/v1/ws/stream")
ASR_STREAM_CONFIG = {
    "model": "default", "chunk_size_ms": 3000,
    "sample_rate": 16000, "sample_width": 2, "channels": 1,
}
ASR_STREAM_SAMPLE_RATE = 16000
ASR_STREAM_FRAME_BYTES = ASR_STREAM_SAMPLE_RATE * 2 * 1 // 50  # 20 ms s16le mono
ASR_STREAM_FRAME_INTERVAL_SEC = 0.02

MAX_REASONABLE_AUDIO_SECONDS = 7200.0
REFERENCE_TEXT_FILENAME = "asr_reference_text.txt"
ACCURACY_SCORING_VERSION = "enterprise-v2"


@dataclass(frozen=True)
class AsrRequestMetrics:
    """一次已发出请求的客户端观测指标。"""

    total_response_sec: float
    time_to_response_headers_sec: Optional[float]
    time_to_first_body_byte_sec: Optional[float]
    audio_duration_sec: Optional[float]
    status_code: Optional[int]
    succeeded: bool


@dataclass(frozen=True)
class AsrRequestOutcome:
    result: Any
    metrics: Optional[AsrRequestMetrics]
    error: Optional[str] = None


@dataclass(frozen=True)
class LegacyEditStats:
    """一次 reference -> hypothesis Levenshtein 对齐的编辑明细。"""

    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0
    reference_units: int = 0

    @property
    def errors(self):
        return self.substitutions + self.deletions + self.insertions

    @property
    def rate(self):
        return self.errors / self.reference_units if self.reference_units else None


@dataclass(frozen=True)
class AccuracyScore:
    cer: LegacyEditStats
    strict_cer: LegacyEditStats
    wer: LegacyEditStats


# Note: percentile is now defined in the runner section (line ~1122)
# to avoid fragile implicit dependency. Legacy code uses the same definition.


# ---------------- ASR 准确率（与并发测试脚本相同的 enterprise-v2 口径） ----------------
_CHINESE_DATE_NUM_RE = re.compile(r"([零〇一二三四五六七八九两十]{1,8})(?=[年月日号])")
def _chinese_date_number_to_arabic(text):
    digit_map = {
        "零": "0", "〇": "0", "一": "1", "二": "2", "两": "2",
        "三": "3", "四": "4", "五": "5", "六": "6", "七": "7",
        "八": "8", "九": "9",
    }

    def repl(match):
        token = match.group(1)
        if token == "十":
            return "10"
        if "十" in token and len(token) <= 3:
            left, _, right = token.partition("十")
            tens = int(digit_map.get(left, "1")) if left else 1
            ones = int(digit_map.get(right, "0")) if right else 0
            return str(tens * 10 + ones)
        if all(ch in digit_map for ch in token):
            return "".join(digit_map[ch] for ch in token)
        return token

    return _CHINESE_DATE_NUM_RE.sub(repl, text)


def _is_ascii_alnum(ch):
    return _is_ascii_alnum_common(ch)


def _is_semantic_symbol(text, index):
    return _is_semantic_symbol_common(text, index)


def _normalize_unicode_surface(text):
    normalized = unicodedata.normalize("NFKC", _TAG_RE.sub("", text or "")).lower()
    # Keep whitespace for the next stage; Cc line breaks/tabs must delimit words,
    # while non-whitespace controls remain removable.
    return "".join(
        ch for ch in normalized
        if ch.isspace() or not unicodedata.category(ch).startswith("C")
    )


def normalize_strict_cer_text(text):
    return "".join(ch for ch in _normalize_unicode_surface(text) if not ch.isspace())


def normalize_asr_text(text, *, keep_spaces=False, normalize_dates=False):
    normalized = _normalize_unicode_surface(text)
    if normalize_dates:
        normalized = _chinese_date_number_to_arabic(normalized)
    out = []

    def append_space():
        if keep_spaces and out and out[-1] != " ":
            out.append(" ")

    for index, ch in enumerate(normalized):
        if ch.isspace():
            append_space()
            continue
        category = unicodedata.category(ch)
        if category.startswith(("P", "S")) and not _is_semantic_symbol(normalized, index):
            append_space()
            continue
        out.append(ch)
    return "".join(out).strip()


def _is_cjk_char(ch):
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF
        or 0x20000 <= code <= 0x2A6DF or 0x2A700 <= code <= 0x2B73F
        or 0x2B740 <= code <= 0x2B81F or 0x2B820 <= code <= 0x2CEAF
    )


def tokenize_for_wer(text, *, normalize_dates=False):
    normalized = normalize_asr_text(text, keep_spaces=True, normalize_dates=normalize_dates)
    tokens, buf = [], []

    def flush():
        if buf:
            tokens.append("".join(buf))
            buf.clear()

    for index, ch in enumerate(normalized):
        if ch.isspace():
            flush()
        elif _is_cjk_char(ch):
            flush()
            tokens.append(ch)
        elif ch.isalnum():
            buf.append(ch)
        else:
            next_is_token = index + 1 < len(normalized) and normalized[index + 1].isalnum()
            if buf or next_is_token:
                buf.append(ch)
                if not next_is_token:
                    flush()
            else:
                flush()
                tokens.append(ch)
    flush()
    return tokens


def legacy_alignment_stats(ref, hyp):
    """标准 Levenshtein；平局稳定按替换、删除、插入。"""
    if len(ref) * len(hyp) > MAX_LEGACY_ALIGNMENT_PRODUCT:
        raise ValueError("legacy Python alignment too large; use the native multipart scorer or split the transcript")
    previous = [(j, 0, 0, j) for j in range(len(hyp) + 1)]
    for i, ref_item in enumerate(ref, 1):
        current = [(i, 0, i, 0)]
        for j, hyp_item in enumerate(hyp, 1):
            if ref_item == hyp_item:
                current.append(previous[j - 1])
                continue
            diagonal, above, left = previous[j - 1], previous[j], current[j - 1]
            candidates = (
                (0, (diagonal[0] + 1, diagonal[1] + 1, diagonal[2], diagonal[3])),
                (1, (above[0] + 1, above[1], above[2] + 1, above[3])),
                (2, (left[0] + 1, left[1], left[2], left[3] + 1)),
            )
            _, selected = min(candidates, key=lambda candidate: (candidate[1][0], candidate[0]))
            current.append(selected)
        previous = current
    _, substitutions, deletions, insertions = previous[-1]
    return LegacyEditStats(substitutions, deletions, insertions, len(ref))


def edit_distance(ref, hyp):
    """兼容并发脚本接口：返回 reference -> hypothesis 总编辑距离。"""
    return legacy_alignment_stats(ref, hyp).errors


def calc_cer(reference, hypothesis, *, normalize_dates=False):
    return legacy_alignment_stats(
        normalize_asr_text(reference, normalize_dates=normalize_dates),
        normalize_asr_text(hypothesis, normalize_dates=normalize_dates),
    ).rate


def calc_wer(reference, hypothesis, *, normalize_dates=False):
    return legacy_alignment_stats(
        tokenize_for_wer(reference, normalize_dates=normalize_dates),
        tokenize_for_wer(hypothesis, normalize_dates=normalize_dates),
    ).rate


def score_text_accuracy(reference, hypothesis, *, normalize_dates=False):
    return AccuracyScore(
        cer=legacy_alignment_stats(
            normalize_asr_text(reference, normalize_dates=normalize_dates),
            normalize_asr_text(hypothesis, normalize_dates=normalize_dates),
        ),
        strict_cer=legacy_alignment_stats(
            normalize_strict_cer_text(reference), normalize_strict_cer_text(hypothesis)
        ),
        wer=legacy_alignment_stats(
            tokenize_for_wer(reference, normalize_dates=normalize_dates),
            tokenize_for_wer(hypothesis, normalize_dates=normalize_dates),
        ),
    )


def load_reference_text(reference_file=None, reference_text=None, audio_file=None):
    """读取显式参考文本；未指定时读取音频同目录 asr_reference_text.txt。"""
    if reference_text is not None:
        text = str(reference_text).lstrip("\ufeff").strip()
        return text or None
    if reference_file == "":
        return None
    path = reference_file
    if not path and audio_file:
        path = os.path.join(os.path.dirname(os.path.abspath(audio_file)), REFERENCE_TEXT_FILENAME)
    elif path and audio_file and not os.path.isabs(path):
        # 与并发脚本一致：相对参考路径优先按音频所在目录解析。
        audio_relative = os.path.join(os.path.dirname(os.path.abspath(audio_file)), path)
        if os.path.exists(audio_relative):
            path = audio_relative
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as file:
            text = file.read().strip()
    except OSError:
        return None
    return text or None


def _outcome_text(outcome):
    if not outcome or not outcome.metrics or not outcome.metrics.succeeded:
        return ""
    _, text = extract_asr_text(outcome.result)
    return text if isinstance(text, str) else ""


def populate_accuracy_metrics(results, reference_text, *, normalize_dates=False,
                              usable_cer_threshold=0.20):
    """为结果对象写入 CER/WER 字段（兼容第二个脚本的调用口径）。"""
    if not reference_text or not normalize_asr_text(reference_text, normalize_dates=normalize_dates):
        return
    cache = {}
    for result in results:
        if hasattr(result, "usable_transcript"):
            result.usable_transcript = False
        success = bool(getattr(result, "success", False))
        text = getattr(result, "text", "")
        if not success:
            continue
        score = cache.get(text)
        if score is None:
            score = score_text_accuracy(reference_text, text, normalize_dates=normalize_dates)
            cache[text] = score
        for prefix, stats in (("cer", score.cer), ("strict_cer", score.strict_cer), ("wer", score.wer)):
            setattr(result, prefix, stats.rate)
            if prefix == "cer":
                for name in ("substitutions", "deletions", "insertions", "reference_units"):
                    setattr(result, f"cer_{name}", getattr(stats, name))
            elif prefix == "strict_cer":
                setattr(result, "strict_cer_errors", stats.errors)
                setattr(result, "strict_cer_reference_units", stats.reference_units)
            else:
                for name in ("substitutions", "deletions", "insertions", "reference_units"):
                    setattr(result, f"wer_{name}", getattr(stats, name))
        if hasattr(result, "usable_transcript"):
            result.usable_transcript = score.cer.rate is not None and score.cer.rate <= usable_cer_threshold


def aggregate_accuracy(outcomes, reference_text, *, normalize_dates=False,
                       usable_cer_threshold=0.20):
    """汇总单文件/多文件请求的宏平均、微平均、P95 及 S/D/I 明细。"""
    if not reference_text or not normalize_asr_text(reference_text, normalize_dates=normalize_dates):
        return None
    attempts = [item for item in outcomes if item.metrics is not None]
    scored = []
    cache = {}
    for outcome in attempts:
        if not outcome.metrics.succeeded:
            continue
        text = _outcome_text(outcome)
        if text not in cache:
            cache[text] = score_text_accuracy(reference_text, text, normalize_dates=normalize_dates)
        score = cache[text]
        scored.append(score)

    def stat_values(name):
        return [getattr(item, name).rate for item in scored if getattr(item, name).rate is not None]

    cers, strict_cers, wers = stat_values("cer"), stat_values("strict_cer"), stat_values("wer")
    cer_stats = [item.cer for item in scored]
    strict_stats = [item.strict_cer for item in scored]
    wer_stats = [item.wer for item in scored]
    cer_errors = sum(item.errors for item in cer_stats)
    strict_errors = sum(item.errors for item in strict_stats)
    wer_errors = sum(item.errors for item in wer_stats)
    usable = sum(1 for item in cer_stats if item.rate is not None and item.rate <= usable_cer_threshold)
    result = {
        "accuracy_scoring_version": ACCURACY_SCORING_VERSION,
        "reference_text": reference_text,
        "accuracy_attempts": len(attempts),
        "accuracy_failed_requests": len(attempts) - len(scored),
        "cer_samples": len(cers), "wer_samples": len(wers),
        "avg_cer": statistics.mean(cers) if cers else None,
        "p95_cer": percentile(cers, 95) if cers else None,
        "micro_cer": cer_errors / sum(s.reference_units for s in cer_stats) if cer_stats and sum(s.reference_units for s in cer_stats) else None,
        "avg_strict_cer": statistics.mean(strict_cers) if strict_cers else None,
        "p95_strict_cer": percentile(strict_cers, 95) if strict_cers else None,
        "micro_strict_cer": strict_errors / sum(s.reference_units for s in strict_stats) if strict_stats and sum(s.reference_units for s in strict_stats) else None,
        "avg_wer": statistics.mean(wers) if wers else None,
        "p95_wer": percentile(wers, 95) if wers else None,
        "micro_wer": wer_errors / sum(s.reference_units for s in wer_stats) if wer_stats and sum(s.reference_units for s in wer_stats) else None,
        "cer_substitutions": sum(s.substitutions for s in cer_stats),
        "cer_deletions": sum(s.deletions for s in cer_stats),
        "cer_insertions": sum(s.insertions for s in cer_stats),
        "cer_reference_units": sum(s.reference_units for s in cer_stats),
        "strict_cer_errors": strict_errors,
        "strict_cer_reference_units": sum(s.reference_units for s in strict_stats),
        "wer_substitutions": sum(s.substitutions for s in wer_stats),
        "wer_deletions": sum(s.deletions for s in wer_stats),
        "wer_insertions": sum(s.insertions for s in wer_stats),
        "wer_reference_units": sum(s.reference_units for s in wer_stats),
        "exact_match_count": sum(1 for s in cer_stats if s.errors == 0),
        "usable_transcript_count": usable,
        "usable_transcript_rate": usable / len(attempts) * 100 if attempts else 0.0,
        "conditional_usable_transcript_rate": usable / len(scored) * 100 if scored else 0.0,
        "wer_definition": "CJK单字；连续英文/数字及内部语义符号为词元",
        "normalize_dates": normalize_dates,
    }
    result["exact_match_rate"] = result["exact_match_count"] / len(cers) * 100 if cers else 0.0
    return result


def print_accuracy_metrics(accuracy):
    if accuracy is None:
        print("\n[ACCURACY] 未提供有效参考文本，CER/WER: N/A")
        return
    fmt = lambda value: f"{value:.2%}" if value is not None else "N/A"
    print("\n[ASR ACCURACY METRICS]")
    print(f"参考文本: {accuracy['reference_text'][:80]}")
    print(f"归一化CER: 微平均 {fmt(accuracy['micro_cer'])} | 宏平均 {fmt(accuracy['avg_cer'])} | P95 {fmt(accuracy['p95_cer'])} | 样本 {accuracy['cer_samples']}")
    print(f"CER编辑明细: S={accuracy['cer_substitutions']} D={accuracy['cer_deletions']} I={accuracy['cer_insertions']} / N={accuracy['cer_reference_units']} | 完全匹配 {accuracy['exact_match_count']}/{accuracy['cer_samples']} ({accuracy['exact_match_rate']:.2f}%)")
    print(f"严格CER: 微平均 {fmt(accuracy['micro_strict_cer'])} | 宏平均 {fmt(accuracy['avg_strict_cer'])} | P95 {fmt(accuracy['p95_strict_cer'])}")
    print(f"混合词元WER: 微平均 {fmt(accuracy['micro_wer'])} | 宏平均 {fmt(accuracy['avg_wer'])} | P95 {fmt(accuracy['p95_wer'])} | 样本 {accuracy['wer_samples']}")
    print(f"WER编辑明细: S={accuracy['wer_substitutions']} D={accuracy['wer_deletions']} I={accuracy['wer_insertions']} / N={accuracy['wer_reference_units']} | {accuracy['wer_definition']}")
    print(f"端到端可用率: {accuracy['usable_transcript_count']}/{accuracy['accuracy_attempts']} ({accuracy['usable_transcript_rate']:.2f}%，分母含请求失败) | 成功样本内 {accuracy['conditional_usable_transcript_rate']:.2f}% | 未评分/失败 {accuracy['accuracy_failed_requests']}")


def run_accuracy_self_test():
    """只验证准确率边界，不访问 ASR 服务。"""
    checks = {
        "标点忽略": calc_cer("标题：今天天气。", "标题今天天气") == 0,
        "替换": math.isclose(calc_cer("今天天气", "今天天汽"), 0.25),
        "插入": math.isclose(calc_cer("天气", "今天天气"), 1.0),
        "删除": math.isclose(calc_cer("今天天气", "天气"), 0.5),
        "语义符号保留": calc_cer("增长25%", "增长25") > 0,
        "混合WER": tokenize_for_wer("请参考U.S.A标准v1.2.3") == ["请", "参", "考", "u.s.a", "标", "准", "v1.2.3"],
        "空参考无分母": calc_cer("   ", "文本") is None and calc_wer("", "文本") is None,
    }
    for name, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    if not all(checks.values()):
        raise AssertionError("准确率自检失败")
    print(f"准确率自检通过：{len(checks)}/{len(checks)}")


def _positive_finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _reasonable_audio_duration(value):
    duration = _positive_finite_float(value)
    if duration is None or duration > MAX_REASONABLE_AUDIO_SECONDS:
        return None
    return duration


def get_audio_duration(audio_file):
    """读取真实媒体时长；不使用文件大小/码率等可能失真的估算值。"""
    try:
        with open(audio_file, "rb") as audio_stream:
            header = audio_stream.read(12)
    except OSError:
        return None

    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        try:
            with wave.open(audio_file, "rb") as wav_file:
                sample_rate = wav_file.getframerate()
                if sample_rate > 0:
                    duration = wav_file.getnframes() / float(sample_rate)
                    if (
                        math.isfinite(duration)
                        and 0 < duration <= MAX_REASONABLE_AUDIO_SECONDS
                    ):
                        return duration
        except (OSError, EOFError, wave.Error):
            pass

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None

    command = [
        ffprobe,
        "-v", "error",
        "-show_entries", "stream=codec_type,duration:format=duration",
        "-of", "json",
        audio_file,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            return None
        probe = json.loads(completed.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None

    audio_stream = next(
        (stream for stream in probe.get("streams", [])
         if stream.get("codec_type") == "audio"),
        {},
    )
    return (
        _reasonable_audio_duration(audio_stream.get("duration"))
        or _reasonable_audio_duration(probe.get("format", {}).get("duration"))
    )


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON numeric constant: {value}")


def _validate_json_complexity(body: Any) -> None:
    """Validate JSON response complexity without cycle detection.
    
    JSON deserialization cannot produce cyclic structures, so we only check
    depth and node count. The 'seen' set is removed as it would cause false
    positives if the same dict/list object appears multiple times in traversal
    (which json.loads never produces, but could happen in modified objects).
    """
    stack = [(body, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_RESPONSE_NODES or depth > MAX_RESPONSE_NESTING:
            raise ValueError("response JSON exceeds nesting/node limit")
        if isinstance(item, (dict, list)):
            if nodes + len(stack) + len(item) > MAX_RESPONSE_NODES:
                raise ValueError("response JSON exceeds node limit")
            values = item.values() if isinstance(item, dict) else item
            stack.extend((value, depth + 1) for value in values)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("response JSON contains non-finite numeric value")


def extract_asr_text(body, *, _depth=0):

    """返回 (是否存在文本字段, 文本)，兼容常见的网关嵌套结构。"""
    if _depth == 0:
        _validate_json_complexity(body)
    if _depth > MAX_RESPONSE_NESTING:
        raise ValueError("ASR text nesting limit exceeded")
    if isinstance(body, dict):
        for key in (
            "text", "content", "answer", "result", "transcript", "transcription",
            "recognized_text",
        ):
            value = body.get(key)
            if isinstance(value, str):
                return True, value

        for key in (
            "result", "data", "results", "output", "response", "payload",
            "choices", "delta", "message", "segments", "sentence_info",
        ):
            found, text = extract_asr_text(body.get(key), _depth=_depth + 1)
            if found:
                return True, text

    if isinstance(body, list):
        texts = []
        found_any = False
        for item in body:
            found, text = extract_asr_text(item, _depth=_depth + 1)
            if found:
                found_any = True
                if text:
                    texts.append(text)
        return found_any, " ".join(texts).strip()

    return False, ""


def extract_business_error(body, *, _depth=0):
    """识别 HTTP 200 中的网关业务错误，避免把快速失败计为成功。"""
    if _depth == 0:
        try:
            _validate_json_complexity(body)
        except ValueError as exc:
            return str(exc)
    if _depth > MAX_RESPONSE_NESTING:
        return "business error nesting limit exceeded"
    if isinstance(body, list):
        return next((error for item in body
                     if (error := extract_business_error(item, _depth=_depth + 1))), None)
    if not isinstance(body, dict):
        return None

    message = next(
        (
            str(body[key]).strip()
            for key in ("message", "msg", "detail")
            if body.get(key) not in (None, "")
        ),
        "",
    )
    if body.get("success") is False:
        return message or "success=false"

    if body.get("code") not in (None, ""):
        code = body["code"]
        normalized_code = str(code).strip().lower()
        success_codes = {
            "0", "00", "00000", "000000", "200", "ok", "success", "true",
        }
        is_all_zero_code = normalized_code.isdigit() and not normalized_code.strip("0")
        if normalized_code not in success_codes and not is_all_zero_code:
            return f"code={code}" + (f", {message}" if message else "")

    status = str(body.get("status", "")).strip().lower()
    if status in {
        "error", "failed", "failure", "fail", "rejected", "cancelled", "canceled",
    }:
        return f"status={body.get('status')}" + (f", {message}" if message else "")

    error_value = body.get("error")
    if error_value not in (None, "", False, 0, "0"):
        if isinstance(error_value, (dict, list)):
            return json.dumps(error_value, ensure_ascii=False)[:500]
        return str(error_value)[:500]

    for key in ("data", "result", "output"):
        nested_error = extract_business_error(body.get(key), _depth=_depth + 1)
        if nested_error:
            return nested_error
    return None


def decode_response_body(response_body, encoding=None):
    """将完整响应体解码为文本，不依赖 requests.Response 的私有缓存。"""
    preferred_encoding = encoding or "utf-8"
    try:
        return response_body.decode(preferred_encoding, errors="replace")
    except LookupError:
        return response_body.decode("utf-8", errors="replace")


def parse_response_body(response_body, content_type="", encoding=None):
    """解析普通 JSON、SSE 或 newline-delimited JSON 响应。"""
    text = decode_response_body(response_body, encoding).strip()
    if not text:
        return {}
    if text == "[DONE]":
        return {}

    if "json" in content_type.lower() or text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    events = []
    plain_lines = []
    event_data_lines = []

    def flush_event():
        if not event_data_lines:
            return
        payload_text = "\n".join(event_data_lines).strip()
        event_data_lines.clear()
        if not payload_text or payload_text == "[DONE]":
            return
        try:
            events.append(json.loads(payload_text))
        except json.JSONDecodeError:
            events.append({"text": payload_text})

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush_event()
        elif stripped == "[DONE]":
            continue
        elif stripped.startswith("data:"):
            event_data_lines.append(stripped[5:].lstrip())
        elif stripped.startswith((":", "event:", "id:", "retry:")):
            continue
        else:
            flush_event()
            try:
                events.append(json.loads(stripped))
            except json.JSONDecodeError:
                plain_lines.append(stripped)
    flush_event()

    if events:
        return events
    return {"text": "\n".join(plain_lines) if plain_lines else text}


def build_performance_summary(metrics):
    """同时汇总全部尝试和业务成功样本，避免成功者偏差。"""
    if not metrics:
        return None

    successful_metrics = [item for item in metrics if item.succeeded]
    attempt_times = [item.total_response_sec for item in metrics]
    response_times = [item.total_response_sec for item in successful_metrics]
    response_header_times = [
        item.time_to_response_headers_sec for item in metrics
        if item.time_to_response_headers_sec is not None
    ]
    first_body_byte_times = [
        item.time_to_first_body_byte_sec for item in metrics
        if item.time_to_first_body_byte_sec is not None
    ]
    rtf_metrics = [
        item for item in successful_metrics
        if item.audio_duration_sec is not None and item.audio_duration_sec > 0
    ]
    rtfs = [
        item.total_response_sec / item.audio_duration_sec
        for item in rtf_metrics
    ]

    summary = {
        "attempt_count": len(metrics),
        "success_count": len(successful_metrics),
        "failure_count": len(metrics) - len(successful_metrics),
        "success_rate": len(successful_metrics) / len(metrics) * 100.0,
        "avg_attempt_sec": math.fsum(attempt_times) / len(attempt_times),
        "p95_attempt_sec": percentile(attempt_times, 95),
        "sample_count": len(successful_metrics),
        "response_header_sample_count": len(response_header_times),
        "response_header_coverage": len(response_header_times) / len(metrics) * 100.0,
        "first_body_byte_sample_count": len(first_body_byte_times),
        "first_body_byte_coverage": len(first_body_byte_times) / len(metrics) * 100.0,
        "rtf_sample_count": len(rtfs),
        "rtf_coverage": (
            len(rtfs) / len(successful_metrics) * 100.0
            if successful_metrics else 0.0
        ),
    }

    if response_times:
        summary.update({
            "avg_response_sec": math.fsum(response_times) / len(response_times),
            "p50_response_sec": percentile(response_times, 50),
            "p95_response_sec": percentile(response_times, 95),
            "p99_response_sec": percentile(response_times, 99),
            "max_response_sec": max(response_times),
            "min_response_sec": min(response_times),
        })

    if response_header_times:
        summary.update({
            "avg_time_to_response_headers_sec": (
                math.fsum(response_header_times) / len(response_header_times)
            ),
            "p95_time_to_response_headers_sec": percentile(response_header_times, 95),
        })

    if first_body_byte_times:
        summary.update({
            "avg_time_to_first_body_byte_sec": (
                math.fsum(first_body_byte_times) / len(first_body_byte_times)
            ),
            "p95_time_to_first_body_byte_sec": percentile(first_body_byte_times, 95),
        })

    if rtfs:
        total_response_sec = math.fsum(
            item.total_response_sec for item in rtf_metrics
        )
        total_audio_sec = math.fsum(
            item.audio_duration_sec for item in rtf_metrics
        )
        summary.update({
            "avg_request_rtf": math.fsum(rtfs) / len(rtfs),
            "corpus_rtf": total_response_sec / total_audio_sec,
            "p50_rtf": percentile(rtfs, 50),
            "p95_rtf": percentile(rtfs, 95),
            "max_rtf": max(rtfs),
            "total_rtf_response_sec": total_response_sec,
            "total_rtf_audio_sec": total_audio_sec,
        })
    return summary


def print_performance_metrics(metrics):
    summary = build_performance_summary(metrics)
    if summary is None:
        print("\n[METRICS] 无已发出请求样本，无法计算")
        return

    print("\n[ASR METRICS]")
    print(
        "请求结果: "
        f"尝试 {summary['attempt_count']} | 成功 {summary['success_count']} | "
        f"失败 {summary['failure_count']} | 成功率 {summary['success_rate']:.1f}%"
    )
    print(
        "全部尝试耗时(含失败): "
        f"平均 {summary['avg_attempt_sec']:.3f}s | "
        f"P95 {summary['p95_attempt_sec']:.3f}s"
    )

    if summary["success_count"]:
        print(
            "成功请求总响应时间: "
            f"平均 {summary['avg_response_sec']:.3f}s | "
            f"P50 {summary['p50_response_sec']:.3f}s | "
            f"P95 {summary['p95_response_sec']:.3f}s | "
            f"P99 {summary['p99_response_sec']:.3f}s | "
            f"最大 {summary['max_response_sec']:.3f}s | "
            f"最小 {summary['min_response_sec']:.3f}s"
        )
    else:
        print("成功请求总响应时间: 无业务成功样本")

    if summary["response_header_sample_count"]:
        print(
            "收到响应头耗时(全部请求，含失败): "
            f"平均 {summary['avg_time_to_response_headers_sec']:.3f}s | "
            f"P95 {summary['p95_time_to_response_headers_sec']:.3f}s | "
            f"样本 {summary['response_header_sample_count']} "
            f"({summary['response_header_coverage']:.1f}%)"
        )
    else:
        print("收到响应头耗时: 无样本 (0.0%)")

    if summary["first_body_byte_sample_count"]:
        print(
            "收到响应体首字节耗时(全部请求，含失败): "
            f"平均 {summary['avg_time_to_first_body_byte_sec']:.3f}s | "
            f"P95 {summary['p95_time_to_first_body_byte_sec']:.3f}s | "
            f"样本 {summary['first_body_byte_sample_count']} "
            f"({summary['first_body_byte_coverage']:.1f}%)"
        )
    else:
        print("收到响应体首字节耗时: 无样本 (0.0%)")

    if (
        summary["attempt_count"] < 20
        or 0 < summary["success_count"] < 20
    ):
        print(
            "样本提示: "
            f"请求 {summary['attempt_count']}、成功 {summary['success_count']}；"
            "少于 20 个样本的 P95/P99 通常接近或等于最大值，请谨慎解读"
        )

    if not summary["success_count"]:
        return

    if summary["rtf_sample_count"]:
        realtime_tag = "P95实时" if summary["p95_rtf"] <= 1.0 else "P95非实时"
        print(
            "实时因子(RTF): "
            f"单请求平均 {summary['avg_request_rtf']:.3f}x ({realtime_tag}) | "
            f"P50 {summary['p50_rtf']:.3f}x | "
            f"P95 {summary['p95_rtf']:.3f}x | "
            f"最大 {summary['max_rtf']:.3f}x | "
            f"样本 {summary['rtf_sample_count']} "
            f"(占成功请求 {summary['rtf_coverage']:.1f}%)"
        )
        print(
            "RTF精确计算:  "
            f"{summary['total_rtf_response_sec']:.6f}s / "
            f"{summary['total_rtf_audio_sec']:.6f}s = "
            f"{summary['corpus_rtf']:.6f}x "
            "(成功完整响应总耗时/成功音频总时长)"
        )
    else:
        print("实时因子(RTF): 无法可靠读取音频时长，未计算")
        print("RTF精确计算:  请安装 ffmpeg/ffprobe，或使用标准 RIFF/WAV 音频")


def getLocalAuthInfo(customerCode, secretKey):
    """生成认证信息"""
    # 使用Python内置datetime生成正确格式的GMT日期，确保英文格式
    date_value = datetime.now(timezone.utc).strftime('%a, %d %b %Y %T GMT')
    date_str = f"x-date: {date_value}"

    # 使用HMAC-SHA256生成签名
    message_bytes = date_str.encode('utf-8')
    secret_bytes = secretKey.encode('utf-8')

    h = hmac.new(secret_bytes, message_bytes, sha256)
    signature_bytes = h.digest()
    signature = base64.b64encode(signature_bytes).decode('utf-8')

    authorization = f'hmac username="{customerCode}", algorithm="hmac-sha256", headers="x-date", signature="{signature}"'
    return date_value, authorization


def _decode_audio_to_stream_pcm(audio_file: str | Path) -> bytes:
    """Decode any supported container/codec to 16 kHz, 16-bit mono PCM."""
    decoder = shutil.which("ffmpeg")
    if not decoder:
        raise RuntimeError("流式模式需要 ffmpeg，将音频解码为 16kHz/16bit/mono PCM")
    command = [
        decoder, "-nostdin", "-v", "error", "-i", str(audio_file),
        "-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(ASR_STREAM_SAMPLE_RATE),
        "-ac", "1", "pipe:1",
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=SERVER_LIMITS.get("ffmpeg_timeout_seconds", 120), check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"ffmpeg 音频解码失败: {exc}") from exc
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg 音频解码失败: {detail or 'empty PCM output'}")
    return result.stdout


async def _asr_via_websocket_async(
        audio_data, audio_duration_sec, timeout, allow_empty_text, websockets,
        websocket_exception):
    """按流式 ASR 协议发送配置、PCM 音频帧和结束指令。"""
    print("\n[REQUEST] Connecting WebSocket...")
    start_time = time.perf_counter()
    connected_at = None
    first_message_at = None
    events = []

    try:
        async with websockets.connect(
                ASR_STREAM_WS_URL,
                open_timeout=timeout,
                close_timeout=min(timeout, 10.0),
        ) as ws:
            connected_at = time.perf_counter()

            # 1. 发送附件示例指定的流式配置。
            await ws.send(json.dumps(ASR_STREAM_CONFIG, ensure_ascii=False))
            print(f"[STREAM] SENT config: {ASR_STREAM_CONFIG}")

            # 2. 收到服务端确认后再开始发送音频。
            confirmation = await asyncio.wait_for(ws.recv(), timeout=timeout)
            first_message_at = time.perf_counter()
            if isinstance(confirmation, bytes):
                confirmation = confirmation.decode("utf-8", errors="replace")
            print(f"[STREAM] RECV confirmation: {confirmation}")

            # 3. 这里收到的已经是 ffmpeg 解码后的 16k/16bit/mono PCM。
            #    每帧 640 bytes=20ms，绝不直接发送容器/压缩文件字节。
            frame_count = 0
            for offset in range(0, len(audio_data), ASR_STREAM_FRAME_BYTES):
                await ws.send(audio_data[offset:offset + ASR_STREAM_FRAME_BYTES])
                frame_count += 1
                await asyncio.sleep(ASR_STREAM_FRAME_INTERVAL_SEC)
            print(
                f"[STREAM] SENT audio: {len(audio_data)} bytes, "
                f"{frame_count} frames"
            )

            # 4. 通知服务端音频发送完毕。
            await ws.send(json.dumps({"type": "end"}))
            print("[STREAM] SENT end")

            # 5. 持续接收识别结果，直到服务端返回 ended。
            print("\n[STREAM RESPONSE] Receiving messages:")
            while True:
                message = await asyncio.wait_for(ws.recv(), timeout=timeout)
                if message in (None, ""):
                    break
                if first_message_at is None:
                    first_message_at = time.perf_counter()
                if isinstance(message, bytes):
                    message = message.decode("utf-8", errors="replace")
                if message.strip() == "[DONE]":
                    break

                parsed = parse_response_body(message.encode("utf-8"))
                if isinstance(parsed, list):
                    events.extend(parsed)
                else:
                    events.append(parsed)
                print(message, flush=True)

                if (
                    isinstance(parsed, dict)
                    and str(parsed.get("type", "")).lower() == "ended"
                ):
                    break

        elapsed = time.perf_counter() - start_time
        if not events:
            result = {}
        elif len(events) == 1:
            result = events[0]
        else:
            result = events

        business_error = next(
            (error for event in events if (error := extract_business_error(event))),
            None,
        )
        has_text_field, recognized_text = extract_asr_text(result)
        succeeded = (
            business_error is None
            and has_text_field
            and (bool(recognized_text.strip()) or allow_empty_text)
        )
        metrics = AsrRequestMetrics(
            total_response_sec=elapsed,
            time_to_response_headers_sec=connected_at - start_time,
            time_to_first_body_byte_sec=(
                first_message_at - start_time if first_message_at else None
            ),
            audio_duration_sec=audio_duration_sec,
            status_code=101,
            succeeded=succeeded,
        )

        print("\n[RESPONSE] 服务端完整响应结果:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if succeeded:
            print(f"\n[OK] Success! (耗时: {elapsed:.2f}s)")
            print(f"\n[TEXT] 识别结果: {recognized_text}")
            return AsrRequestOutcome(result=result, metrics=metrics)

        error = (
            f"业务错误: {business_error}"
            if business_error else "WebSocket 响应中没有有效识别文本"
        )
        print(f"\n[ERROR] {error} (耗时: {elapsed:.2f}s)")
        return AsrRequestOutcome(result=result, metrics=metrics, error=error)

    except (asyncio.TimeoutError, OSError, websocket_exception, ValueError, TypeError, RecursionError) as exc:
        elapsed = time.perf_counter() - start_time
        error = "等待 WebSocket 响应超时" if isinstance(
            exc, asyncio.TimeoutError
        ) else (str(exc) or exc.__class__.__name__)
        print(f"\n[ERROR] WebSocket request failed: {error}")
        return AsrRequestOutcome(
            result=None,
            metrics=AsrRequestMetrics(
                total_response_sec=elapsed,
                time_to_response_headers_sec=(
                    connected_at - start_time if connected_at else None
                ),
                time_to_first_body_byte_sec=(
                    first_message_at - start_time if first_message_at else None
                ),
                audio_duration_sec=audio_duration_sec,
                status_code=101 if connected_at else None,
                succeeded=False,
            ),
            error=error,
        )


def _asr_via_websocket_with_metrics(
        audio_data, audio_duration_sec, timeout, allow_empty_text):
    """同步入口：使用附件示例中的 asyncio + websockets 流式协议。"""
    try:
        import websockets
        from websockets.exceptions import WebSocketException
    except ImportError:
        error = "流式请求需要安装 websockets"
        print(f"[ERROR] {error}")
        return AsrRequestOutcome(result=None, metrics=None, error=error)
    return asyncio.run(_asr_via_websocket_async(
        audio_data=audio_data,
        audio_duration_sec=audio_duration_sec,
        timeout=timeout,
        allow_empty_text=allow_empty_text,
        websockets=websockets,
        websocket_exception=WebSocketException,
    ))


def _asr_via_gateway_with_metrics(
        audio_file, model="funasr-iic", hotwords=None, is_return_timestamp=False,
        timeout=120, allow_empty_text=False, response_stream=False, verify_tls=True):
    """
    通过 Gateway 测试 ASR 服务

    Args:
        audio_file: 音频文件路径
        model: 模型名称 (funasr-iic, funasr-nano, default)
        hotwords: 热词列表或字符串
        is_return_timestamp: 是否返回时间戳
        timeout: 连接和响应读取超时（秒）
        allow_empty_text: 是否将带 text 字段的空识别结果视为成功
        response_stream: 是否请求流式响应
        verify_tls: 是否验证 TLS 证书
    """
    print("[ASR] Testing ASR via Gateway")
    print(f"   Host: {ASR_STREAM_WS_URL if response_stream else ASR_BINDING_HOST}")
    print(
        "   Model: "
        f"{ASR_STREAM_CONFIG['model'] if response_stream else model}"
    )
    print(f"   Audio: {audio_file}")
    print(
        "   Input encoding: "
        + ("PCM binary frames (640 bytes / 20ms)" if response_stream
           else "Base64 audio (single JSON request)")
    )
    print(f"   Response mode: {'stream' if response_stream else 'non-stream'}")

    if not response_stream:
        missing = [name for name, value in (
            ("ASR_BINDING_HOST", ASR_BINDING_HOST),
            ("ASR_BINDING_API_KEY", ASR_BINDING_API_KEY),
            ("ASR_CUSTCODE", ASR_CUSTCODE),
            ("ASR_COMPONENTCODE", ASR_COMPONENTCODE),
        ) if not value]
        if missing:
            error = "missing required gateway environment variables: " + ", ".join(missing)
            print(f"[ERROR] {error}")
            return AsrRequestOutcome(result=None, metrics=None, error=error)

    # 读取音频文件
    if not os.path.exists(audio_file):
        print(f"[ERROR] Audio file not found: {audio_file}")
        return AsrRequestOutcome(result=None, metrics=None)

    try:
        with open(audio_file, "rb") as f:
            audio_data = f.read()
    except OSError as exc:
        error = f"读取音频失败: {exc}"
        print(f"[ERROR] {error}")
        return AsrRequestOutcome(result=None, metrics=None, error=error)

    if not audio_data:
        error = "音频文件为空"
        print(f"[ERROR] {error}: {audio_file}")
        return AsrRequestOutcome(result=None, metrics=None, error=error)

    # Base64 编码
    b64_data = base64.b64encode(audio_data).decode("utf-8")

    # 获取认证信息
    x_date, authorization = getLocalAuthInfo(ASR_CUSTCODE, ASR_BINDING_API_KEY)

    # 构造请求头
    headers = {
        "x-date": x_date,
        "authorization": authorization,
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if response_stream else "application/json",
    }

    # 有热词时发送 JSON 数组；未配置时按网关协议发送 null。
    if isinstance(hotwords, str):
        hotwords = [item.strip() for item in hotwords.split(",") if item.strip()]

    payload = {
        "componentCode": ASR_COMPONENTCODE,
        "function": model,
        "model": model,
        # input_type 描述 Base64 音频输入，不控制 HTTP 响应读取方式。
        "input_type": "stream",
        "input": b64_data,
        "hotwords": hotwords,
        "speaker_diarization": False,
        "language": "zh",
        "is_return_timestamp": is_return_timestamp,
        "stream": response_stream,
    }

    audio_duration_sec = get_audio_duration(audio_file)
    if audio_duration_sec is None:
        print("[WARN] 无法可靠读取音频时长，本次请求将不计入 RTF")
    else:
        print(f"   Duration: {audio_duration_sec:.3f}s")

    if response_stream:
        try:
            stream_pcm = _decode_audio_to_stream_pcm(audio_file)
        except (OSError, RuntimeError) as exc:
            error = str(exc)
            print(f"[ERROR] {error}")
            return AsrRequestOutcome(result=None, metrics=None, error=error)
        return _asr_via_websocket_with_metrics(
            audio_data=stream_pcm,
            audio_duration_sec=audio_duration_sec,
            timeout=timeout,
            allow_empty_text=allow_empty_text,
        )

    print("\n[REQUEST] Sending request...")
    start_time = time.perf_counter()
    time_to_response_headers_sec = None
    first_byte_at = None
    status_code = None

    try:
        response = requests.post(
            ASR_BINDING_HOST,
            json=payload,
            headers=headers,
            verify=verify_tls,
            timeout=timeout,
            stream=response_stream,
        )
        status_code = response.status_code
        if response_stream:
            time_to_response_headers_sec = time.perf_counter() - start_time

        try:
            if response_stream:
                response_encoding = getattr(response, "encoding", None) or "utf-8"
                try:
                    decoder = codecs.getincrementaldecoder(response_encoding)(
                        errors="replace"
                    )
                except LookupError:
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

                chunks = []
                print("\n[STREAM RESPONSE] Receiving chunks:")
                for chunk in response.iter_content(chunk_size=128):
                    if not chunk:
                        continue
                    if first_byte_at is None:
                        first_byte_at = time.perf_counter()
                    chunks.append(chunk)
                    decoded_chunk = decoder.decode(chunk)
                    if decoded_chunk:
                        print(decoded_chunk, end="", flush=True)
                decoded_tail = decoder.decode(b"", final=True)
                if decoded_tail:
                    print(decoded_tail, end="", flush=True)
                print()
                response_body = b"".join(chunks)
            else:
                response_body = response.content
            elapsed = time.perf_counter() - start_time
        finally:
            response.close()

        time_to_first_body_byte_sec = (
            first_byte_at - start_time if first_byte_at is not None else None
        )
        response_encoding = getattr(response, "encoding", None)
        response_body_text = decode_response_body(
            response_body,
            response_encoding,
        )

        if response.status_code == 200:
            result = parse_response_body(
                response_body,
                response.headers.get("Content-Type", ""),
                response_encoding,
            )
            response_content = (
                json.dumps(result, indent=2, ensure_ascii=False)
                if isinstance(result, (dict, list))
                else str(result)
            )

            print("\n[RESPONSE] 服务端完整响应结果:")
            print(response_content)

            business_error = extract_business_error(result)
            has_text_field, recognized_text = extract_asr_text(result)
            succeeded = (
                isinstance(result, (dict, list))
                and business_error is None
                and has_text_field
                and (bool(recognized_text.strip()) or allow_empty_text)
            )

            metrics = AsrRequestMetrics(
                total_response_sec=elapsed,
                time_to_response_headers_sec=time_to_response_headers_sec,
                time_to_first_body_byte_sec=time_to_first_body_byte_sec,
                audio_duration_sec=audio_duration_sec,
                status_code=response.status_code,
                succeeded=succeeded,
            )

            if not succeeded:
                if business_error:
                    error = f"业务错误: {business_error}"
                elif not isinstance(result, (dict, list)):
                    error = "HTTP 200 但响应体不是 JSON"
                elif has_text_field:
                    error = "HTTP 200 但识别文本为空"
                else:
                    error = "HTTP 200 但响应中没有识别文本字段"
                print(f"\n[ERROR] {error} (耗时: {elapsed:.2f}s)")
                return AsrRequestOutcome(result=result, metrics=metrics, error=error)

            print(f"\n[OK] Success! (耗时: {elapsed:.2f}s)")

            # 额外打印便于查看的识别结果摘要；空文本字段可表示静音识别成功。
            if has_text_field:
                text = recognized_text
                print(f"\n[TEXT] 识别结果: {text}")

                if is_return_timestamp and "segments" in result:
                    print("\n[TIMESTAMP] 时间戳信息:")
                    for seg in result.get("segments", []):
                        if not isinstance(seg, dict):
                            continue
                        start = seg.get("start")
                        end = seg.get("end")
                        segment_text = seg.get("text", "")
                        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                            print(f"   [{start:.2f}s - {end:.2f}s] {segment_text}")

            return AsrRequestOutcome(
                result=result,
                metrics=metrics,
            )
        else:
            print(f"\n[ERROR] HTTP {response.status_code}")
            print("\n[RESPONSE] 服务端完整响应结果:")
            print(response_body_text)
            return AsrRequestOutcome(
                result=None,
                metrics=AsrRequestMetrics(
                    total_response_sec=elapsed,
                    time_to_response_headers_sec=time_to_response_headers_sec,
                    time_to_first_body_byte_sec=time_to_first_body_byte_sec,
                    audio_duration_sec=audio_duration_sec,
                    status_code=response.status_code,
                    succeeded=False,
                ),
                error=f"HTTP {response.status_code}",
            )

    except requests.exceptions.Timeout as exc:
        elapsed = time.perf_counter() - start_time
        print(f"\n[TIMEOUT] Request timeout after {elapsed:.2f}s")
        return AsrRequestOutcome(
            result=None,
            metrics=AsrRequestMetrics(
                total_response_sec=elapsed,
                time_to_response_headers_sec=time_to_response_headers_sec,
                time_to_first_body_byte_sec=(
                    first_byte_at - start_time if first_byte_at else None
                ),
                audio_duration_sec=audio_duration_sec,
                status_code=status_code,
                succeeded=False,
            ),
            error=str(exc) or "请求超时",
        )
    except (requests.exceptions.RequestException, urllib3.exceptions.HTTPError, OSError, ValueError, TypeError, RecursionError) as exc:
        elapsed = time.perf_counter() - start_time
        print(f"\n[ERROR] Request failed: {exc}")
        return AsrRequestOutcome(
            result=None,
            metrics=AsrRequestMetrics(
                total_response_sec=elapsed,
                time_to_response_headers_sec=time_to_response_headers_sec,
                time_to_first_body_byte_sec=(
                    first_byte_at - start_time if first_byte_at else None
                ),
                audio_duration_sec=audio_duration_sec,
                status_code=status_code,
                succeeded=False,
            ),
            error=str(exc),
        )


def asr_via_gateway(
        audio_file, model="funasr-iic", hotwords=None, is_return_timestamp=False,
        timeout=120, allow_empty_text=False, response_stream=False,
        reference_file=None, reference_text=None, normalize_dates=False,
        usable_cer_threshold=0.20, verify_tls=True):
    """发送单次请求，保持原有返回值，并在控制台输出该请求的性能指标。"""
    outcome = _asr_via_gateway_with_metrics(
        audio_file=audio_file,
        model=model,
        hotwords=hotwords,
        is_return_timestamp=is_return_timestamp,
        timeout=timeout,
        allow_empty_text=allow_empty_text,
        response_stream=response_stream,
        verify_tls=verify_tls,
    )
    print_performance_metrics(
        [outcome.metrics] if outcome.metrics is not None else []
    )
    reference = load_reference_text(reference_file, reference_text, audio_file)
    print_accuracy_metrics(aggregate_accuracy(
        [outcome], reference, normalize_dates=normalize_dates,
        usable_cer_threshold=usable_cer_threshold,
    ))
    return outcome.result if outcome.metrics and outcome.metrics.succeeded else None


def run_multiple_files(
        audio_files, model="funasr-iic", hotwords=None, is_return_timestamp=False,
        timeout=120, allow_empty_text=False, response_stream=False,
        reference_file=None, reference_text=None, normalize_dates=False,
        usable_cer_threshold=0.20, verify_tls=True):
    """测试多个音频文件"""
    print(f"\n{'=' * 60}")
    print(f"Testing {len(audio_files)} audio files")
    print(f"{'=' * 60}")

    results = []
    request_metrics = []
    all_outcomes = []
    for audio_file in audio_files:
        if not os.path.exists(audio_file):
            print(f"\n[ERROR] Audio file not found: {audio_file}")
            results.append({"file": audio_file, "success": False, "result": None, "error": "audio file not found"})
            continue

        outcome = _asr_via_gateway_with_metrics(
            audio_file=audio_file,
            model=model,
            hotwords=hotwords,
            is_return_timestamp=is_return_timestamp,
            timeout=timeout,
            allow_empty_text=allow_empty_text,
            response_stream=response_stream,
            verify_tls=verify_tls,
        )
        result = outcome.result
        if outcome.metrics is not None:
            request_metrics.append(outcome.metrics)
        all_outcomes.append((audio_file, outcome))
        results.append({
            "file": audio_file,
            "success": bool(outcome.metrics and outcome.metrics.succeeded),
            "result": result,
            "error": outcome.error,
        })
        time.sleep(0.5)  # 避免请求过快

    print_performance_metrics(request_metrics)
    # 显式参考文本可用于整批文件；未指定时按每个音频旁的参考文件分别输出。
    if reference_file is not None or reference_text is not None:
        reference = load_reference_text(reference_file, reference_text, audio_files[0] if audio_files else None)
        print_accuracy_metrics(aggregate_accuracy(
            [outcome for _, outcome in all_outcomes], reference, normalize_dates=normalize_dates,
            usable_cer_threshold=usable_cer_threshold,
        ))
    else:
        for audio_file, outcome in all_outcomes:
            reference = load_reference_text(None, None, audio_file)
            if reference:
                print(f"\n[ACCURACY] {audio_file}")
                print_accuracy_metrics(aggregate_accuracy(
                    [outcome], reference, normalize_dates=normalize_dates,
                    usable_cer_threshold=usable_cer_threshold,
                ))
    return results


def legacy_main(argv=None):
    parser = argparse.ArgumentParser(description="通过 AI Gateway 测试 ASR 服务")
    parser.add_argument("--audio", type=str, help="音频文件路径 (支持 mp3, wav, mp4 等)")
    parser.add_argument("--model", type=str, default=ASR_MODEL,
                        choices=["funasr-iic", "funasr-nano", "default"],
                        help="模型名称")
    parser.add_argument("--hotwords", type=str, help="热词，用逗号分隔")
    parser.add_argument("--timestamp", action="store_true", help="返回时间戳")
    parser.add_argument("--multiple", type=str, help="测试多个文件，文件路径用逗号分隔")
    parser.add_argument(
        "--stream",
        action="store_true",
        help="请求流式响应（默认为非流式）",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="单次请求超时秒数")
    parser.add_argument(
        "--allow-empty-text",
        action="store_true",
        help="允许 HTTP 200 的空识别文本计为成功（适用于静音测试）",
    )
    parser.add_argument(
        "--reference-file",
        type=str,
        default=None,
        help=f"CER/WER 参考文本文件；默认读取音频同目录 {REFERENCE_TEXT_FILENAME}，传空字符串可禁用",
    )
    parser.add_argument(
        "--reference-text",
        type=str,
        default=None,
        help="CER/WER 参考文本字符串（优先于 --reference-file）",
    )
    parser.add_argument(
        "--accuracy-normalize-dates",
        action="store_true",
        help="将二零二五年/2025年等日期数字写法归一后计算准确率",
    )
    parser.add_argument(
        "--usable-cer-threshold",
        type=float,
        default=0.20,
        help="端到端可用转写的最大归一化 CER，取值 0~1",
    )
    parser.add_argument(
        "--accuracy-self-test",
        action="store_true",
        help="只运行 CER/WER 边界自检，不连接 ASR 服务",
    )
    parser.add_argument(
        "--insecure", action=argparse.BooleanOptionalAction,
        default=not ASR_VERIFY_TLS if os.getenv("ASR_GATEWAY_INSECURE", "").lower() not in {"1", "true", "yes", "0", "false", "no"} else os.getenv("ASR_GATEWAY_INSECURE", "").lower() in {"1", "true", "yes"},
        help="禁用 TLS 证书校验；仅限受信内网临时测试，也可用 ASR_GATEWAY_INSECURE=1 或修改代码中的 ASR_VERIFY_TLS",
    )
    args = parser.parse_args(argv)
    if args.accuracy_self_test:
        run_accuracy_self_test()
        return 0

    # 处理热词
    hotwords = None
    if args.hotwords:
        hotwords = [hw.strip() for hw in args.hotwords.split(",") if hw.strip()]

    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须为有限数且大于 0")
    if not 0 <= args.usable_cer_threshold <= 1:
        parser.error("--usable-cer-threshold 必须在 0 到 1 之间")

    # 确定音频文件
    # 默认音频位置与文件顶部配置保持一致；也可通过 --audio 显式指定。
    default_audio = str(DEFAULT_AUDIO_FILE)

    if args.multiple:
        # 测试多个文件
        audio_files = [f.strip() for f in args.multiple.split(",")]
        results = run_multiple_files(
            audio_files,
            model=args.model,
            hotwords=hotwords,
            is_return_timestamp=args.timestamp,
            timeout=args.timeout,
            allow_empty_text=args.allow_empty_text,
            response_stream=args.stream,
            reference_file=args.reference_file,
            reference_text=args.reference_text,
            normalize_dates=args.accuracy_normalize_dates,
            usable_cer_threshold=args.usable_cer_threshold,
            verify_tls=not args.insecure,
        )
        return 0 if results and all(item["success"] for item in results) else 2
    elif args.audio:
        # 测试单个文件
        result = asr_via_gateway(
            audio_file=args.audio,
            model=args.model,
            hotwords=hotwords,
            is_return_timestamp=args.timestamp,
            timeout=args.timeout,
            allow_empty_text=args.allow_empty_text,
            response_stream=args.stream,
            reference_file=args.reference_file,
            reference_text=args.reference_text,
            normalize_dates=args.accuracy_normalize_dates,
            usable_cer_threshold=args.usable_cer_threshold,
            verify_tls=not args.insecure,
        )
        return 0 if result is not None else 2
    else:
        # 使用默认文件
        print(f"[INFO] 使用默认音频文件: {default_audio}")
        result = asr_via_gateway(
            audio_file=default_audio,
            model=args.model,
            hotwords=hotwords,
            is_return_timestamp=args.timestamp,
            timeout=args.timeout,
            allow_empty_text=args.allow_empty_text,
            response_stream=args.stream,
            reference_file=args.reference_file,
            reference_text=args.reference_text,
            normalize_dates=args.accuracy_normalize_dates,
            usable_cer_threshold=args.usable_cer_threshold,
            verify_tls=not args.insecure,
        )
        return 0 if result is not None else 2


def main(argv: list[str] | None = None) -> int:
    """Select the legacy gateway protocol or the multipart dataset evaluator."""
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    legacy_flags = {"--multiple", "--stream", "--timestamp", "--timeout",
                    "--reference-file", "--reference-text", "--accuracy-normalize-dates",
                    "--usable-cer-threshold", "--accuracy-self-test"}
    if "--legacy" in args or any(arg.split("=", 1)[0] in legacy_flags for arg in args):
        args = [arg for arg in args if arg != "--legacy"]
        return legacy_main(args)
    return evaluation_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
