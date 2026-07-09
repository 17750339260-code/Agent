# -*- coding: utf-8 -*-
"""
ASR 大模型组件精度/性能/稳定性指标测试脚本。

运行方式：
  pytest test_asr/test_asr_precision.py -s -v
  python test_asr/test_asr_precision.py

推荐提供人工标注文件，才能精确计算 CER/WER/BERTScore/实体/标点/ITN 等依赖
参考文本的指标。默认读取：
  test_asr/asr_precision_manifest.json

manifest 示例：
[
  {
    "audio": "test_asr/asr_test_audio/123.wav",
    "reference": "这里填写人工校对后的标准转写文本",
    "key_entities": ["南方电网", "不在岗", "35岁"],
    "is_hallucination_probe": false
  },
  {
    "audio": "test_asr/asr_test_audio/silence.wav",
    "reference": "",
    "key_entities": [],
    "is_hallucination_probe": true
  }
]
"""

from __future__ import annotations

import base64
import contextlib
import csv
import io
import json
import math
import mimetypes
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import warnings
import wave
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import string
import pytest
import requests

try:
    import numpy as np
except ImportError:  # pragma: no cover - 依赖缺失时运行期会给出 N/A
    np = None

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import jieba
except ImportError:  # pragma: no cover
    jieba = None

bert_score_score = None
BERTSCORE_IMPORT_ERROR = ""
BERTSCORE_IMPORT_ATTEMPTED = False


# ===================== 基础配置 =====================

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO_DIR = os.path.join(PROJECT_ROOT, "test_asr", "asr_test_audio")
DEFAULT_MANIFEST = os.path.join(PROJECT_ROOT, "test_asr", "asr_test_json", "asr_precision_manifest.json")
# ASR 服务地址、模型、超时均支持环境变量覆盖，便于测试不同环境。
API_URL = os.environ.get("ASR_API_URL", "http://36.111.82.53:10017/v1/audio/trans")
ASR_MODEL = os.environ.get("ASR_MODEL", "funasr-iic")
REQUEST_TIMEOUT_SEC = int(os.environ.get("ASR_TIMEOUT_SEC", "21600"))
ASR_CONNECT_TIMEOUT_SEC = float(os.environ.get("ASR_CONNECT_TIMEOUT_SEC", "10"))
LANGUAGE = os.environ.get("ASR_LANGUAGE", "zh")
ASR_PROGRESS_INTERVAL_SEC = float(os.environ.get("ASR_PROGRESS_INTERVAL_SEC", "10"))
ASR_EXTRACT_AUDIO_ONLY = os.environ.get("ASR_EXTRACT_AUDIO_ONLY", "1") == "1"
ASR_USE_DATA_URI = os.environ.get("ASR_USE_DATA_URI", "0") == "1"
ASR_REQUEST_MODE = os.environ.get("ASR_REQUEST_MODE", "json").strip().lower()
if ASR_REQUEST_MODE not in {"multipart", "json"}:
    warnings.warn(f"未知 ASR_REQUEST_MODE={ASR_REQUEST_MODE}，已回退到 json", RuntimeWarning)
    ASR_REQUEST_MODE = "json"
ASR_PREFLIGHT = os.environ.get("ASR_PREFLIGHT", "1") == "1"
ASR_PREFLIGHT_AUDIO = os.environ.get("ASR_PREFLIGHT_AUDIO", "")

# 一致性测试重复次数。大模型若存在采样随机性，同一音频多次结果不一致会被量化。短音频建议设置 3，长音频建议设置 1。
CONSISTENCY_RUNS = int(os.environ.get("ASR_CONSISTENCY_RUNS", "1"))

# SNR 鲁棒性测试默认开启。仅 WAV 音频且安装 numpy 时可生成加噪临时文件。
RUN_SNR_ROBUSTNESS = os.environ.get("ASR_RUN_SNR_ROBUSTNESS", "1") == "1"
SNR_LEVELS_DB = [
    float(x.strip())
    for x in os.environ.get("ASR_SNR_LEVELS_DB", "20,10,5,0").split(",")
    if x.strip()
]

# BERTScore 模型可按现场离线模型位置替换。默认模型适合中文语义相似度评估。
BERTSCORE_MODEL = os.environ.get("ASR_BERTSCORE_MODEL", "bert-base-chinese")

# 实体匹配默认保持历史 substring 行为；如需降低包含误判，可设为 exact_token。
ENTITY_MATCH_MODE = os.environ.get("ASR_ENTITY_MATCH_MODE", "substring")

# 标点和 ITN 相关符号集合。ITN 主要关注数字、日期、百分比、金额、英文缩写等格式化结果。
PUNCTUATION_CHARS = set("，。！？；：、,.!?;:()（）《》“”\"'")
ITN_TOKEN_RE = re.compile(
    r"(\d+(?:[.,:：/-]\d+)*(?:%|％)?|[A-Za-z]+(?:-[A-Za-z]+)*|[￥$]\s*\d+(?:\.\d+)?)"
)
# 在 import jieba 之后，配置区附近添加
_CUSTOM_DICT = os.environ.get("ASR_JIEBA_DICT", os.path.join(PROJECT_ROOT, "test_asr", "asr_test_json", "asr_dict.txt"))
if jieba is not None:
    if os.path.exists(_CUSTOM_DICT):
        jieba.load_userdict(_CUSTOM_DICT)
        print(f"[WER] 已加载自定义词典：{_CUSTOM_DICT}")
    else:
        print(f"[WER] 自定义词典未找到，将使用默认分词：{_CUSTOM_DICT}")

@dataclass
class AsrMetricCase:
    """单条 ASR 评测样本配置。"""

    audio: str
    reference: str = ""
    key_entities: List[str] = field(default_factory=list)
    is_hallucination_probe: bool = False
    label: str = ""

    @property
    def audio_path(self) -> str:
        # manifest 中允许写相对项目根目录路径，也允许写绝对路径。
        return self.audio if os.path.isabs(self.audio) else os.path.join(PROJECT_ROOT, self.audio)

    @property
    def display_name(self) -> str:
        return self.label or os.path.basename(self.audio_path)


@dataclass
class ResourceSnapshot:
    """一次请求期间采样到的本机资源峰值。远程 API 无法直接量化服务端显存。"""

    peak_process_rss_mb: Optional[float] = None
    peak_cpu_percent: Optional[float] = None
    peak_gpu_memory_mb: Optional[float] = None
    peak_gpu_util_percent: Optional[float] = None
    note: str = ""


@dataclass
class TranscribeResult:
    """ASR 调用结果及请求级性能信息。"""

    ok: bool
    text: str
    elapsed_sec: float
    response_json: Any
    status_code: Optional[int] = None
    error: str = ""
    resource: ResourceSnapshot = field(default_factory=ResourceSnapshot)


@dataclass
class MediaInfo:
    """本地媒体探测结果；优先用于诊断和长音频进度展示。"""

    duration_sec: Optional[float] = None
    format_name: str = ""
    audio_codec: str = ""
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    bits_per_sample: Optional[int] = None
    has_audio: bool = False
    has_video: bool = False
    note: str = ""


@dataclass
class PreparedAsrInput:
    """一次 ASR 请求实际上传的文件。temp_dir 存在时由调用方负责清理。"""

    path: str
    media_info: MediaInfo = field(default_factory=MediaInfo)
    note: str = ""
    temp_dir: Any = None

    def cleanup(self) -> None:
        if self.temp_dir is not None:
            self.temp_dir.cleanup()
            self.temp_dir = None


# ===================== 文本规范化与编辑距离 =====================


# 保留数字、字母、中文、以及数字相关符号，只去掉纯中文标点和空白
_CHINESE_PUNCT = set("，。！？；：、”“‘’（）《》【】…—～")
# 这些符号在语义上有用，要保留（如小数点、连接符）
_KEEP_SYMBOLS = set(".-/%$€¥")

def normalize_for_cer(text: str) -> str:
    """CER 专用归一化：去掉空白和纯中文标点，保留数字/字母/有意义符号"""
    text = text or ""
    chars = []
    for ch in text.lower():
        if ch.isspace() or ch in _CHINESE_PUNCT:
            continue
        if ch in string.ascii_letters or ch in string.digits or ch in _KEEP_SYMBOLS or '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
            chars.append(ch)
        elif ch not in string.punctuation:  # 其他标点也去掉，但保留中日韩字符
            chars.append(ch)
    return "".join(chars)


def normalize_for_entity(text: str) -> str:
    """实体匹配专用：仅去空白，保留所有标点，因为实体可能含数字符号"""
    return re.sub(r'\s+', '', (text or "").lower())


def normalize_for_hallucination(text: str) -> str:
    """幻觉检测专用：只去掉空白，避免误删纯标点输出"""
    return re.sub(r'\s+', '', (text or ""))

def levenshtein_distance(ref: Sequence[Any], hyp: Sequence[Any]) -> int:
    """标准动态规划编辑距离，用于 CER/WER/序列准确率。"""
    if not ref:
        return len(hyp)
    if not hyp:
        return len(ref)

    prev = list(range(len(hyp) + 1))
    for i, ref_item in enumerate(ref, start=1):
        curr = [i]
        for j, hyp_item in enumerate(hyp, start=1):
            cost = 0 if ref_item == hyp_item else 1
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def safe_error_rate(ref_items: Sequence[Any], hyp_items: Sequence[Any]) -> Optional[float]:
    """编辑错误率：edit_distance / reference_length。参考为空时返回 None，避免伪造 0。"""
    if not ref_items:
        return None
    return levenshtein_distance(ref_items, hyp_items) / len(ref_items)


def tokenize_words(text: str) -> List[str]:
    # 统一小写，压缩所有空白字符为一个空格
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not normalized:
        return []

    # 如果 jieba 可用，始终优先使用 jieba 分词（适合中文）
    if jieba is not None:
        tokens = [t for t in jieba.lcut(normalized) if t.strip()]
        if tokens:   # 正常情况下 tokens 不为空
            return tokens
        # 极端情况 tokens 全为空？继续尝试后序方法

    # 若无 jieba，且文本含有空格，则按空格分词（主要用于英文）
    if " " in normalized:
        return [t for t in normalized.split(" ") if t.strip()]

    # 否则字符级退化（例如中文无空格且无 jieba）
    return [ch for ch in normalized if not ch.isspace()]


def calc_cer(reference: str, hypothesis: str) -> Optional[float]:
    """字错误率 CER，适合中文 ASR 准确性基线。"""
    ref = list(normalize_for_cer(reference))
    hyp = list(normalize_for_cer(hypothesis))
    return safe_error_rate(ref, hyp)


def calc_wer(reference: str, hypothesis: str) -> Optional[float]:
    """词错误率 WER，中文场景依赖分词器质量。"""
    return safe_error_rate(tokenize_words(reference), tokenize_words(hypothesis))


def calc_text_similarity_by_cer(text_a: str, text_b: str) -> Optional[float]:
    """用于一致性对比：1 - CER，越接近 1 表示两次输出越一致。"""
    err = calc_cer(text_a, text_b)
    if err is None:
        return None
    return max(0.0, 1.0 - err)


# ===================== 语义、幻觉、格式化、实体指标 =====================


def get_bertscore_score():
    """按需加载 BERTScore，避免 ASR 请求阶段被 torch/numpy 兼容警告干扰。"""
    global bert_score_score, BERTSCORE_IMPORT_ERROR, BERTSCORE_IMPORT_ATTEMPTED
    if BERTSCORE_IMPORT_ATTEMPTED:
        return bert_score_score

    BERTSCORE_IMPORT_ATTEMPTED = True
    stderr_buffer = io.StringIO()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with contextlib.redirect_stderr(stderr_buffer):
                from bert_score import score as imported_score
        bert_score_score = imported_score
        return bert_score_score
    except Exception as exc:  # pragma: no cover - 依赖/torch/numpy 兼容问题
        captured = stderr_buffer.getvalue().strip()
        BERTSCORE_IMPORT_ERROR = f"{exc}"
        if captured:
            BERTSCORE_IMPORT_ERROR = f"{BERTSCORE_IMPORT_ERROR}；{captured[:300]}"
        bert_score_score = None
        return None


def calc_bertscore(reference: str, hypothesis: str) -> Tuple[Optional[float], str]:
    """
    BERTScore F1：调用成熟 bert_score 包计算语义相似度。
    未安装依赖或无参考文本时返回 N/A，而不是用其它指标冒充 BERTScore。
    rescale_with_baseline=True 会按基线重缩放分数，通常更贴近人工评判尺度。
    """
    if not reference.strip():
        return None, "缺少人工参考文本"
    if not hypothesis.strip():
        return 0.0, "识别为空"
    scorer = get_bertscore_score()
    if scorer is None:
        detail = f"：{BERTSCORE_IMPORT_ERROR}" if BERTSCORE_IMPORT_ERROR else ""
        return None, f"未安装或无法加载 bert_score，可执行 pip install bert-score{detail}"

    try:
        _, _, f1 = scorer(
            [hypothesis],
            [reference],
            lang="zh",
            model_type=BERTSCORE_MODEL,
            verbose=False,
            # 审查修正：默认启用 baseline 重缩放，使 BERTScore 分数更贴近人工评判。
            rescale_with_baseline=True,
        )
        return float(f1[0].item()), ""
    except Exception as exc:  # pragma: no cover - 模型下载/离线环境差异
        return None, f"BERTScore 计算失败：{exc}"


def calc_hallucination(is_probe: bool, hypothesis: str) -> Optional[float]:
    """
    幻觉率样本级定义：
    - 静音/纯噪声样本预期应无转写；
    - 若模型输出非空文本，则该样本幻觉率记为 1，否则为 0；
    - 普通语音样本不参与幻觉率统计，返回 None。
    """
    if not is_probe:
        return None
    return 1.0 if normalize_for_hallucination(hypothesis) else 0.0


def extract_punctuation_sequence(text: str) -> List[str]:
    """提取标点序列，用编辑距离衡量标点类型和顺序是否一致。"""
    return [ch for ch in (text or "") if ch in PUNCTUATION_CHARS]


def calc_sequence_accuracy(reference_items: Sequence[Any], hypothesis_items: Sequence[Any]) -> Optional[float]:
    """序列准确率：1 - 编辑距离 / 参考序列长度，参考为空时返回 None。"""
    if not reference_items:
        return None
    return max(0.0, 1.0 - levenshtein_distance(reference_items, hypothesis_items) / len(reference_items))


def calc_punctuation_accuracy(reference: str, hypothesis: str) -> Optional[float]:
    """标点准确率：比较参考文本与识别文本的标点序列。"""
    return calc_sequence_accuracy(extract_punctuation_sequence(reference), extract_punctuation_sequence(hypothesis))


def extract_punctuation_labels(text: str) -> List[Tuple[int, str]]:
    """
    提取带位置的标点标签。
    审查新增：标点 F1 需要统计预测标点是否出现在相同文本位置；这里用非空白、非标点字符计数作为位置索引。
    """
    labels = []
    char_pos = 0
    for ch in text or "":
        if ch in PUNCTUATION_CHARS:
            labels.append((char_pos, ch))
        elif not ch.isspace():
            char_pos += 1
    return labels


def calc_punctuation_f1(reference: str, hypothesis: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    标点精确率/召回率/F1。
    审查新增：保留原有序列准确率，同时补充行业常用的标点 F1；参考文本无标点时返回 N/A。
    """
    ref_counter = Counter(extract_punctuation_labels(reference))
    hyp_counter = Counter(extract_punctuation_labels(hypothesis))
    ref_total = sum(ref_counter.values())
    if ref_total == 0:
        return None, None, None

    hyp_total = sum(hyp_counter.values())
    true_positive = sum((ref_counter & hyp_counter).values())
    precision = true_positive / hyp_total if hyp_total else 0.0
    recall = true_positive / ref_total
    if precision + recall == 0:
        return precision, recall, 0.0
    return precision, recall, 2 * precision * recall / (precision + recall)


def extract_itn_tokens(text: str) -> List[str]:
    """
    抽取数字/金额/日期等格式化 token。
    审查修正：该抽取仅用于比较特定格式化 token 序列，不代表真正的 ITN 归一化效果。
    """
    return [m.group(0).replace(" ", "") for m in ITN_TOKEN_RE.finditer(text or "")]


def calc_itn_accuracy(reference: str, hypothesis: str) -> Optional[float]:
    """
    数字/金额/日期序列准确率：比较参考文本与识别文本中特定格式化 token 序列的编辑距离。
    风险说明：保持原接口名以兼容历史调用，但该指标不包含中文数词到阿拉伯数字等真正 ITN 归一化能力评估。
    """
    return calc_sequence_accuracy(extract_itn_tokens(reference), extract_itn_tokens(hypothesis))


def calc_entity_accuracy(
    reference_entities: Sequence[str],
    hypothesis: str,
    match_mode: str = "substring",
) -> Tuple[Optional[float], List[str]]:
    """
    关键实体准确率：按配置实体做匹配，适合专有名词、否定词、业务关键词。
    match_mode="substring" 保持历史包含匹配；match_mode="exact_token" 要求实体作为完整连续 token 序列出现。
    返回值为 (命中率, 未命中实体列表)。
    """
    entities = [e for e in reference_entities if e]
    if not entities:
        return None, []

    if match_mode not in {"substring", "exact_token"}:
        warnings.warn(f"未知实体匹配模式 {match_mode}，已回退到 substring", RuntimeWarning)
        match_mode = "substring"

    missed = []
    if match_mode == "exact_token":
        if jieba is None:
            warnings.warn("实体 exact_token 匹配需要 jieba，当前已回退到 substring", RuntimeWarning)
            match_mode = "substring"
        else:
            # 审查新增：exact_token 通过 jieba 分词后做连续 token 序列匹配，避免纯子串包含造成误判。
            hyp_tokens = [normalize_for_entity(t) for t in jieba.lcut((hypothesis or "").lower()) if normalize_for_entity(t)]
            for entity in entities:
                entity_tokens = [normalize_for_entity(t) for t in jieba.lcut(entity.lower()) if normalize_for_entity(t)]
                matched = any(
                    hyp_tokens[idx:idx + len(entity_tokens)] == entity_tokens
                    for idx in range(0, len(hyp_tokens) - len(entity_tokens) + 1)
                ) if entity_tokens else False
                if not matched:
                    missed.append(entity)
            return (len(entities) - len(missed)) / len(entities), missed

    normalized_hyp = normalize_for_entity(hypothesis)
    for entity in entities:
        if normalize_for_entity(entity) not in normalized_hyp:
            missed.append(entity)
    return (len(entities) - len(missed)) / len(entities), missed


# ===================== 音频与资源采样 =====================


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def probe_media_info(file_path: str) -> MediaInfo:
    """用 ffprobe 识别真实媒体类型；失败时返回带 note 的空结果。"""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,size,format_name",
                "-show_entries",
                "stream=codec_name,codec_type,channels,sample_rate,bits_per_sample",
                "-of",
                "json",
                file_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return MediaInfo(note="ffprobe 不可用，无法识别真实媒体类型")
    except Exception as exc:
        return MediaInfo(note=f"ffprobe 探测失败：{exc}")

    if proc.returncode != 0:
        return MediaInfo(note=(proc.stderr or "ffprobe 探测失败").strip()[:300])

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return MediaInfo(note=f"ffprobe 输出无法解析：{exc}")

    fmt_obj = data.get("format") if isinstance(data, dict) else {}
    streams = data.get("streams") if isinstance(data, dict) else []
    info = MediaInfo(format_name=str(fmt_obj.get("format_name") or ""))
    try:
        duration = fmt_obj.get("duration")
        info.duration_sec = float(duration) if duration not in (None, "N/A", "") else None
    except (TypeError, ValueError):
        info.duration_sec = None

    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            codec_type = stream.get("codec_type")
            if codec_type == "video":
                info.has_video = True
            elif codec_type == "audio" and not info.has_audio:
                info.has_audio = True
                info.audio_codec = str(stream.get("codec_name") or "")
                info.sample_rate = _safe_int(stream.get("sample_rate"))
                info.channels = _safe_int(stream.get("channels"))
                info.bits_per_sample = _safe_int(stream.get("bits_per_sample"))
    return info


def is_readable_wav(file_path: str) -> bool:
    try:
        with wave.open(file_path, "rb"):
            return True
    except Exception:
        return False


def is_pcm_16bit_wav(file_path: str) -> bool:
    try:
        with wave.open(file_path, "rb") as wav:
            return wav.getsampwidth() == 2
    except Exception:
        return False


def guess_audio_mime_type(file_path: str) -> str:
    mime, _ = mimetypes.guess_type(file_path)
    if mime:
        return mime
    ext = os.path.splitext(file_path)[1].lower()
    fallback = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
    }
    return fallback.get(ext, "application/octet-stream")


def format_mb(byte_count: int) -> str:
    return f"{byte_count / 1024 / 1024:.2f}MB"


def prepare_asr_input_file(file_path: str) -> PreparedAsrInput:
    """
    准备实际上传给 ASR 的文件。
    对含视频轨或扩展名伪装成 WAV 的媒体，默认抽取音频轨，避免上传整段视频。
    """
    info = probe_media_info(file_path)
    ext = os.path.splitext(file_path)[1].lower()
    wav_ok = is_readable_wav(file_path) if ext == ".wav" else False
    needs_audio_extract = (
        ASR_EXTRACT_AUDIO_ONLY
        and info.has_audio
        and (info.has_video or (ext == ".wav" and not wav_ok))
    )

    if not needs_audio_extract:
        if info.has_video:
            print(
                "[ASR预处理] 检测到视频轨，但 ASR_EXTRACT_AUDIO_ONLY=0，仍按原文件上传。",
                flush=True,
            )
        return PreparedAsrInput(path=file_path, media_info=info)

    tmp_dir = tempfile.TemporaryDirectory(prefix="asr_audio_only_")
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.splitext(os.path.basename(file_path))[0]).strip("_") or "input"

    copy_audio = info.audio_codec.lower() in {"aac", "mp3", "alac"}
    if copy_audio:
        out_ext = ".m4a" if info.audio_codec.lower() in {"aac", "alac"} else ".mp3"
        out_path = os.path.join(tmp_dir.name, f"{base}{out_ext}")
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", file_path, "-map", "0:a:0", "-vn", "-c:a", "copy", out_path]
    else:
        out_path = os.path.join(tmp_dir.name, f"{base}.wav")
        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            file_path,
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            out_path,
        ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
    except FileNotFoundError:
        tmp_dir.cleanup()
        print("[ASR预处理] ffmpeg 不可用，无法抽取音频轨，仍按原文件上传。", flush=True)
        return PreparedAsrInput(path=file_path, media_info=info, note="ffmpeg 不可用，未抽取音频轨")
    except Exception as exc:
        tmp_dir.cleanup()
        print(f"[ASR预处理] 抽取音频轨异常：{exc}，仍按原文件上传。", flush=True)
        return PreparedAsrInput(path=file_path, media_info=info, note=f"抽取音频轨异常：{exc}")

    if proc.returncode != 0 or not os.path.exists(out_path):
        tmp_dir.cleanup()
        err = (proc.stderr or "ffmpeg 未生成输出文件").strip()[:300]
        print(f"[ASR预处理] 抽取音频轨失败：{err}，仍按原文件上传。", flush=True)
        return PreparedAsrInput(path=file_path, media_info=info, note=f"抽取音频轨失败：{err}")

    extracted_info = probe_media_info(out_path)
    print(
        "[ASR预处理] 已抽取音频轨用于识别："
        f"{os.path.basename(file_path)} ({format_mb(os.path.getsize(file_path))}) -> "
        f"{os.path.basename(out_path)} ({format_mb(os.path.getsize(out_path))}) | "
        f"format={info.format_name or '未知'}, audio={info.audio_codec or '未知'}, video={info.has_video}",
        flush=True,
    )
    return PreparedAsrInput(
        path=out_path,
        media_info=extracted_info,
        note="已抽取音频轨用于 ASR 请求",
        temp_dir=tmp_dir,
    )


def get_wav_duration_sec(file_path: str) -> Optional[float]:
    """
    读取 WAV 时长；非 WAV 或无法读取时返回 None。
    某些测试 WAV 的 header 中 nframes 异常，此时按文件大小估算 PCM 时长兜底。
    """
    try:
        with wave.open(file_path, "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            header_duration = frames / float(rate) if rate else None

        if not rate or not channels or not sample_width:
            return header_duration

        # 44 字节是常见 PCM WAV header 大小；这里仅作为 header 明显异常时的保守兜底。
        estimated_data_bytes = max(0, os.path.getsize(file_path) - 44)
        estimated_duration = estimated_data_bytes / float(rate * channels * sample_width)
        if header_duration and estimated_duration > 0:
            header_is_unreasonable = (
                header_duration > estimated_duration * 10
                and header_duration - estimated_duration > 60
            )
            return estimated_duration if header_is_unreasonable else header_duration
        return header_duration or estimated_duration
    except Exception:
        media_info = probe_media_info(file_path)
        return media_info.duration_sec


def describe_media_for_log(file_path: str, info: MediaInfo) -> str:
    parts = [
        f"文件={format_mb(os.path.getsize(file_path))}" if os.path.exists(file_path) else "文件=未知",
        f"时长={format_duration(info.duration_sec)}",
    ]
    if info.format_name:
        parts.append(f"format={info.format_name}")
    if info.audio_codec:
        audio = info.audio_codec
        if info.sample_rate:
            audio += f"/{info.sample_rate}Hz"
        if info.channels:
            audio += f"/{info.channels}ch"
        parts.append(f"audio={audio}")
    if info.has_video:
        parts.append("含视频轨=True")
    if info.note:
        parts.append(f"探测说明={info.note}")
    return " | ".join(parts)


def read_wav_mono_float32(file_path: str) -> Tuple[Any, int, int, int]:
    """读取 PCM WAV 为 mono float32，用于按目标 SNR 生成加噪样本。"""
    if np is None:
        raise RuntimeError("numpy 未安装，无法生成 SNR 加噪音频")
    with wave.open(file_path, "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())

    if sample_width != 2:
        raise RuntimeError(f"仅支持 16-bit PCM WAV，加噪样本当前位宽为 {sample_width * 8} bit")

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, sample_rate, channels, sample_width


def write_wav_int16(file_path: str, audio: Any, sample_rate: int) -> None:
    """写出 mono 16-bit PCM WAV 临时文件。"""
    clipped = np.clip(audio, -32768, 32767).astype(np.int16)
    with wave.open(file_path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(clipped.tobytes())


def make_noisy_wav(source_path: str, snr_db: float, output_path: str) -> None:
    """
    生成指定 SNR 的白噪声样本。
    计算公式：noise_power = signal_power / 10^(SNR/10)。
    """
    audio, sample_rate, _, _ = read_wav_mono_float32(source_path)
    # 审查修正：先去除直流偏移再计算信号功率，避免静态偏置让 SNR 估计偏乐观。
    audio_ac = audio - np.mean(audio)
    signal_power = float(np.mean(audio_ac ** 2))
    if signal_power <= 0:
        raise RuntimeError("源音频能量为 0，无法按 SNR 加噪")
    noise_power = signal_power / (10 ** (snr_db / 10.0))
    rng = np.random.default_rng(seed=20260605 + int(snr_db * 10))
    noise = rng.normal(0.0, math.sqrt(noise_power), size=audio.shape).astype(np.float32)
    noisy_audio = audio.astype(np.float32) + noise
    write_wav_int16(output_path, noisy_audio, sample_rate)


def query_gpu_by_nvidia_smi() -> Tuple[Optional[float], Optional[float], str]:
    """
    查询本机 GPU 显存和利用率。
    注意：如果 ASR 服务部署在远程机器，本机 nvidia-smi 只能代表客户端环境。
    """
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception as exc:
        return None, None, f"无法执行 nvidia-smi：{exc}"

    if proc.returncode != 0 or not proc.stdout.strip():
        return None, None, "nvidia-smi 不可用或未检测到 NVIDIA GPU"

    mem_values = []
    util_values = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            mem_values.append(float(parts[0]))
            util_values.append(float(parts[1]))
    if not mem_values:
        return None, None, "nvidia-smi 输出无法解析"
    return max(mem_values), max(util_values), ""


class ResourceSampler:
    """请求期间定时采样本机 CPU、进程内存、GPU。"""

    def __init__(self, interval_sec: float = 0.2) -> None:
        self.interval_sec = interval_sec
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._rss_samples: List[float] = []
        self._cpu_samples: List[float] = []
        self._gpu_mem_samples: List[float] = []
        self._gpu_util_samples: List[float] = []
        self._notes: List[str] = []

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> ResourceSnapshot:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

        return ResourceSnapshot(
            peak_process_rss_mb=max(self._rss_samples) if self._rss_samples else None,
            peak_cpu_percent=max(self._cpu_samples) if self._cpu_samples else None,
            peak_gpu_memory_mb=max(self._gpu_mem_samples) if self._gpu_mem_samples else None,
            peak_gpu_util_percent=max(self._gpu_util_samples) if self._gpu_util_samples else None,
            note="；".join(sorted(set(self._notes))),
        )

    def _run(self) -> None:
        proc = psutil.Process(os.getpid()) if psutil is not None else None
        if proc:
            proc.cpu_percent(interval=None)
        elif psutil is None:
            self._notes.append("未安装 psutil，无法采样 CPU/RSS")

        while not self._stop_event.is_set():
            if proc:
                try:
                    self._rss_samples.append(proc.memory_info().rss / 1024 / 1024)
                    self._cpu_samples.append(proc.cpu_percent(interval=None))
                except Exception as exc:
                    self._notes.append(f"CPU/RSS 采样失败：{exc}")

            gpu_mem, gpu_util, gpu_note = query_gpu_by_nvidia_smi()
            if gpu_mem is not None:
                self._gpu_mem_samples.append(gpu_mem)
            if gpu_util is not None:
                self._gpu_util_samples.append(gpu_util)
            if gpu_note:
                self._notes.append(gpu_note)

            self._stop_event.wait(self.interval_sec)


def format_duration(seconds: Optional[float]) -> str:
    """将秒数格式化为 HH:MM:SS，便于长音频控制台进度查看。"""
    if seconds is None:
        return "未知"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class AsrProgressReporter:
    """ASR 长请求期间的控制台心跳，避免长音频识别时看起来像卡住。"""

    def __init__(
        self,
        file_path: str,
        audio_duration_sec: Optional[float],
        interval_sec: float = ASR_PROGRESS_INTERVAL_SEC,
    ) -> None:
        self.file_path = file_path
        self.audio_duration_sec = audio_duration_sec
        self.interval_sec = max(1.0, interval_sec)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start = 0.0

    def start(self) -> None:
        self._start = time.perf_counter()
        file_mb = os.path.getsize(self.file_path) / 1024 / 1024 if os.path.exists(self.file_path) else 0
        print(
            f"\n[ASR进度] 开始识别：{os.path.basename(self.file_path)} | "
            f"音频时长={format_duration(self.audio_duration_sec)} | 文件={file_mb:.2f}MB",
            flush=True,
        )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, status: str) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
        elapsed = time.perf_counter() - self._start if self._start else 0.0
        print(f"[ASR进度] 结束：{status} | 总耗时={format_duration(elapsed)} ({elapsed:.2f}s)", flush=True)

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_sec):
            elapsed = time.perf_counter() - self._start
            if self.audio_duration_sec and self.audio_duration_sec > 0:
                approx_audio_sec = min(elapsed, self.audio_duration_sec)
                percent = min(99.0, approx_audio_sec / self.audio_duration_sec * 100)
                print(
                    f"[ASR进度] 等待响应中：已耗时={format_duration(elapsed)} | "
                    f"按耗时估算已处理音频≈{format_duration(approx_audio_sec)}/"
                    f"{format_duration(self.audio_duration_sec)} ({percent:.1f}%) "
                    f"（按耗时估算，非服务端真实进度）",
                    flush=True,
                )
            else:
                print(f"[ASR进度] 等待响应中：已耗时={format_duration(elapsed)}", flush=True)


# ===================== ASR 请求与响应解析 =====================


def encode_audio_as_base64(file_path: str) -> str:
    """读取音频文件并转换为 Base64，符合当前 /v1/audio/trans JSON stream 入参。"""
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_payload(file_path: str) -> Dict[str, Any]:
    """构造 ASR 请求体；打开时间戳开关，以便尽可能解析尾字时间信息。"""
    audio_input = encode_audio_as_base64(file_path)
    if ASR_USE_DATA_URI:
        audio_input = f"data:{guess_audio_mime_type(file_path)};base64,{audio_input}"
    return {
        "model": ASR_MODEL,
        "input_type": "stream",
        "input": audio_input,
        "hotwords": "",
        "language": LANGUAGE,
        "is_return_timestamp": True,
        "speaker_diarization": False,
    }


def build_multipart_form_data() -> Dict[str, str]:
    """构造 multipart/form-data 表单字段；指标语义与 JSON 模式保持一致。"""
    return {
        "model": ASR_MODEL,
        "hotwords": "",
        "language": LANGUAGE,
        "is_return_timestamp": "true",
        "speaker_diarization": "false",
    }


def extract_text(response_json: Any) -> str:
    """兼容常见 ASR 返回结构，优先读取 text 字段。"""
    if isinstance(response_json, dict):
        for key in ("text", "result", "transcript", "transcription"):
            value = response_json.get(key)
            if isinstance(value, str):
                return value
        for key in ("data", "results"):
            value = response_json.get(key)
            nested = extract_text(value)
            if nested:
                return nested
    if isinstance(response_json, list):
        texts = [extract_text(item) for item in response_json]
        return "".join(t for t in texts if t)
    return ""


def transcribe_audio(file_path: str) -> TranscribeResult:
    """发送一次 ASR 请求，并记录耗时和本机资源峰值。"""
    prepared = prepare_asr_input_file(file_path)
    request_file = prepared.path
    if prepared.path != file_path:
        print(f"[ASR请求] 原始样本：{describe_media_for_log(file_path, probe_media_info(file_path))}", flush=True)
    print(f"[ASR请求] 实际上传：{describe_media_for_log(request_file, prepared.media_info)}", flush=True)

    sampler = ResourceSampler()
    progress = AsrProgressReporter(request_file, get_wav_duration_sec(request_file))
    sampler.start()
    progress.start()
    start = time.perf_counter()
    try:
        if ASR_REQUEST_MODE == "multipart":
            form_data = build_multipart_form_data()
            print(
                f"[ASR请求] POST {API_URL} | mode=multipart | upload={format_mb(os.path.getsize(request_file))} | "
                f"connect_timeout={ASR_CONNECT_TIMEOUT_SEC}s, read_timeout={REQUEST_TIMEOUT_SEC}s",
                flush=True,
            )
            with open(request_file, "rb") as f:
                response = requests.post(
                    API_URL,
                    headers={"accept": "application/json"},
                    files={"file": (os.path.basename(request_file), f, guess_audio_mime_type(request_file))},
                    data=form_data,
                    timeout=(ASR_CONNECT_TIMEOUT_SEC, REQUEST_TIMEOUT_SEC),
                )
        else:
            payload = build_payload(request_file)
            request_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            print(
                f"[ASR请求] POST {API_URL} | mode=json | JSON={format_mb(len(request_body))} | "
                f"connect_timeout={ASR_CONNECT_TIMEOUT_SEC}s, read_timeout={REQUEST_TIMEOUT_SEC}s | "
                f"data_uri={ASR_USE_DATA_URI}",
                flush=True,
            )
            response = requests.post(
                API_URL,
                data=request_body,
                headers={"accept": "application/json", "Content-Type": "application/json"},
                timeout=(ASR_CONNECT_TIMEOUT_SEC, REQUEST_TIMEOUT_SEC),
            )
        elapsed = time.perf_counter() - start
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = response.text
        resource = sampler.stop()
        progress.stop(f"HTTP {response.status_code}")

        return TranscribeResult(
            ok=response.status_code == 200,
            text=extract_text(body),
            elapsed_sec=elapsed,
            response_json=body,
            status_code=response.status_code,
            error="" if response.status_code == 200 else (response.text[:500] or response.reason or f"HTTP {response.status_code}"),
            resource=resource,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - start
        resource = sampler.stop()
        progress.stop("异常")
        return TranscribeResult(
            ok=False,
            text="",
            elapsed_sec=elapsed,
            response_json=None,
            error=str(exc),
            resource=resource,
        )
    finally:
        prepared.cleanup()


def find_first_last_timestamp(response_json: Any) -> Tuple[Optional[float], Optional[float]]:
    """
    尝试从常见字段解析字/词时间戳，单位统一换算为秒。
    注意：这代表音频内时间位置，不等同于流式首字网络延迟。
    """
    candidates: List[Tuple[float, float]] = []

    def first_present(obj: Dict[str, Any], keys: Sequence[str]) -> Any:
        for key in keys:
            if key in obj and obj[key] is not None:
                return obj[key]
        return None

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            start = first_present(obj, ("start", "begin", "start_time"))
            end = first_present(obj, ("end", "stop", "end_time"))
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                s = float(start)
                e = float(end)
                # 如果数值超过一天的长度（86400秒），几乎可以肯定是毫秒
                if s > 86400 or e > 86400:
                    s /= 1000.0
                    e /= 1000.0
                candidates.append((s, e))

            timestamp = obj.get("timestamp")
            if isinstance(timestamp, list):
                parse_timestamp_list(timestamp)

            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            parse_timestamp_list(obj)
            for item in obj:
                walk(item)

    def parse_timestamp_list(items: Sequence[Any]) -> None:
        for item in items:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                if isinstance(item[0], (int, float)) and isinstance(item[1], (int, float)):
                    s = float(item[0])
                    e = float(item[1])
                    # 如果数值超过一天的长度（86400秒），几乎可以肯定是毫秒
                    if s > 86400 or e > 86400:
                        s /= 1000.0
                        e /= 1000.0
                    # 此处不再对 1000~86400 之间的值自动转换，避免误判
                    candidates.append((s, e))

    walk(response_json)
    if not candidates:
        return None, None
    starts = [s for s, _ in candidates]
    ends = [e for _, e in candidates]
    return min(starts), max(ends)


def extract_latency_fields(response_json: Any) -> Tuple[Optional[float], Optional[float], str]:
    """解析服务端若显式返回的首字/尾字流式延迟字段。"""
    if not isinstance(response_json, dict):
        return None, None, "响应无显式流式延迟字段"

    first_keys = ("first_char_latency", "first_token_latency", "first_byte_latency", "ttft")
    tail_keys = ("last_char_latency", "tail_latency", "final_latency")

    def pick(keys: Sequence[str]) -> Optional[float]:
        for key in keys:
            value = response_json.get(key)
            if isinstance(value, (int, float)):
                v = float(value)
                # 若值 > 86400（超过一天），极可能是毫秒，转成秒
                if v > 86400:
                    v /= 1000.0
                return v
        return None

    first = pick(first_keys)
    tail = pick(tail_keys)
    note = "" if first is not None or tail is not None else "响应无显式流式延迟字段"
    return first, tail, note


# ===================== 用例加载与指标聚合 =====================


def load_cases() -> List[AsrMetricCase]:
    """加载评测样本；没有 manifest 时使用现有音频做演示，但准确率指标会因无标注输出 N/A。"""
    manifest_path = os.environ.get("ASR_PRECISION_MANIFEST", DEFAULT_MANIFEST)
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            raw_cases = json.load(f)
        return [
            AsrMetricCase(
                audio=item["audio"],
                reference=item.get("reference", ""),
                key_entities=list(item.get("key_entities", [])),
                is_hallucination_probe=bool(item.get("is_hallucination_probe", False)),
                label=item.get("label", ""),
            )
            for item in raw_cases
        ]

    return [
        # AsrMetricCase(audio=os.path.join("test_asr", "asr_test_audio", "123.wav"), label="default-123"),
        # AsrMetricCase(audio=os.path.join("test_asr", "asr_test_audio", "4-111.wav"), label="default-4-111"),
        AsrMetricCase(audio=os.path.join("test_asr", "asr_test_audio", "上市公司治理要求及实践.wav"), label="default-1")
    ]


def find_preflight_audio(cases: Sequence[AsrMetricCase]) -> Optional[str]:
    """选择一个小文件做接口冒烟，避免服务不可用时直接跑长音频。"""
    if ASR_PREFLIGHT_AUDIO:
        return ASR_PREFLIGHT_AUDIO if os.path.isabs(ASR_PREFLIGHT_AUDIO) else os.path.join(PROJECT_ROOT, ASR_PREFLIGHT_AUDIO)

    candidates = []
    for case in cases:
        if os.path.exists(case.audio_path):
            candidates.append(case.audio_path)
    if os.path.isdir(AUDIO_DIR):
        for name in os.listdir(AUDIO_DIR):
            path = os.path.join(AUDIO_DIR, name)
            if os.path.isfile(path):
                candidates.append(path)
    if not candidates:
        return None
    return min(set(candidates), key=os.path.getsize)


def run_asr_preflight(cases: Sequence[AsrMetricCase]) -> None:
    """正式指标前的接口可用性检查；失败时提前中止，避免长时间空等。"""
    if not ASR_PREFLIGHT:
        return

    audio_path = find_preflight_audio(cases)
    if not audio_path or not os.path.exists(audio_path):
        print("\n[ASR预检] 未找到可用短音频，跳过接口预检。", flush=True)
        return

    print(f"\n[ASR预检] 使用短样本检查接口：{os.path.basename(audio_path)}", flush=True)
    result = transcribe_audio(audio_path)
    if result.ok:
        print(f"[ASR预检] 通过：HTTP {result.status_code}，耗时={result.elapsed_sec:.2f}s", flush=True)
        return

    raise AssertionError(
        "ASR 接口预检失败，已停止正式长音频评测："
        f"status={result.status_code}, elapsed={result.elapsed_sec:.2f}s, error={result.error or '无响应体'}"
    )


def fmt(value: Optional[float], digits: int = 4) -> str:
    """统一格式化浮点指标，None 输出 N/A。"""
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}"


def mean_optional(values: Iterable[Optional[float]]) -> Optional[float]:
    """忽略 N/A 后求均值；全为空返回 None。"""
    valid = [v for v in values if v is not None]
    return statistics.mean(valid) if valid else None


def pct_optional(values: Iterable[Optional[float]], percentile: float) -> Optional[float]:
    """忽略 N/A 后求百分位。"""
    valid = sorted(v for v in values if v is not None)
    if not valid:
        return None
    index = (len(valid) - 1) * percentile / 100.0
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return valid[int(index)]
    return valid[low] * (high - index) + valid[high] * (index - low)


def print_table(title: str, headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    """控制台表格输出，便于 pytest -s 或 python 直接运行查看。"""
    print(f"\n========== {title} ==========")
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(str(cell)))

    header_line = " | ".join(str(h).ljust(widths[idx]) for idx, h in enumerate(headers))
    sep_line = "-+-".join("-" * w for w in widths)
    print(header_line)
    print(sep_line)
    for row in rows:
        print(" | ".join(str(cell).ljust(widths[idx]) for idx, cell in enumerate(row)))


def evaluate_case(case: AsrMetricCase) -> Dict[str, Any]:
    """评估单条样本的核心指标。"""
    if not os.path.exists(case.audio_path):
        pytest.skip(f"音频文件不存在：{case.audio_path}")

    duration = get_wav_duration_sec(case.audio_path)
    result = transcribe_audio(case.audio_path)
    if not result.ok:
        return {
            "case": case,
            "result": result,
            "duration_sec": duration,
            "cer": None,
            "wer": None,
            "bertscore_f1": None,
            "bertscore_note": "ASR 请求失败，未计算 BERTScore",
            "hallucination": None,
            "rtf": None,
            "first_char_latency_sec": None,
            "tail_latency_sec": None,
            "first_timestamp_sec": None,
            "last_timestamp_sec": None,
            "offline_tail_latency_sec": None,
            "latency_note": f"ASR 请求失败：status={result.status_code}, error={result.error}",
            "punctuation_acc": None,
            "punctuation_precision": None,
            "punctuation_recall": None,
            "punctuation_f1": None,
            "itn_acc": None,
            "entity_acc": None,
            "missed_entities": [],
        }

    cer = calc_cer(case.reference, result.text)
    wer = calc_wer(case.reference, result.text)
    bert_f1, bert_note = calc_bertscore(case.reference, result.text)
    hallucination = calc_hallucination(case.is_hallucination_probe, result.text)
    punctuation_acc = calc_punctuation_accuracy(case.reference, result.text)
    punctuation_precision, punctuation_recall, punctuation_f1 = calc_punctuation_f1(case.reference, result.text)
    itn_acc = calc_itn_accuracy(case.reference, result.text)
    entity_acc, missed_entities = calc_entity_accuracy(case.key_entities, result.text, match_mode=ENTITY_MATCH_MODE)
    first_ts, last_ts = find_first_last_timestamp(result.response_json)
    first_latency, tail_latency, latency_note = extract_latency_fields(result.response_json)
    offline_tail_latency = max(0.0, result.elapsed_sec - duration) if duration else None
    rtf = result.elapsed_sec / duration if duration else None

    return {
        "case": case,
        "result": result,
        "duration_sec": duration,
        "cer": cer,
        "wer": wer,
        "bertscore_f1": bert_f1,
        "bertscore_note": bert_note,
        "hallucination": hallucination,
        "rtf": rtf,
        "first_char_latency_sec": first_latency,
        "tail_latency_sec": tail_latency,
        "first_timestamp_sec": first_ts,
        "last_timestamp_sec": last_ts,
        "offline_tail_latency_sec": offline_tail_latency,
        "latency_note": latency_note,
        "punctuation_acc": punctuation_acc,
        "punctuation_precision": punctuation_precision,
        "punctuation_recall": punctuation_recall,
        "punctuation_f1": punctuation_f1,
        "itn_acc": itn_acc,
        "entity_acc": entity_acc,
        "missed_entities": missed_entities,
    }


def evaluate_consistency(case: AsrMetricCase) -> Dict[str, Any]:
    """同一音频重复请求，计算输出一致性。"""
    texts = []
    elapsed = []
    for _ in range(max(1, CONSISTENCY_RUNS)):
        result = transcribe_audio(case.audio_path)
        if not result.ok:
            raise AssertionError(
                f"一致性请求失败：case={case.display_name}, status={result.status_code}, error={result.error}"
            )
        texts.append(result.text)
        elapsed.append(result.elapsed_sec)

    pair_scores = [
        calc_text_similarity_by_cer(left, right)
        for left, right in combinations(texts, 2)
    ]
    exact_pairs = [1.0 if normalize_for_cer(left) == normalize_for_cer(right) else 0.0 for left, right in combinations(texts, 2)]

    return {
        "case": case.display_name,
        "runs": len(texts),
        "consistency_score": mean_optional(pair_scores),
        "exact_match_rate": mean_optional(exact_pairs),
        "avg_elapsed_sec": statistics.mean(elapsed) if elapsed else None,
        "unique_outputs": len({normalize_for_cer(t) for t in texts}),
    }


def evaluate_snr_robustness(case: AsrMetricCase) -> List[Dict[str, Any]]:
    """生成不同 SNR 加噪音频并计算 WER/CER，验证鲁棒性。"""
    rows = []
    if not RUN_SNR_ROBUSTNESS:
        return rows
    if not case.reference.strip():
        return rows
    if not case.audio_path.lower().endswith(".wav"):
        return rows
    if not is_pcm_16bit_wav(case.audio_path):
        return rows
    if np is None:
        return rows

    with tempfile.TemporaryDirectory(prefix="asr_snr_") as tmp_dir:
        for snr_db in SNR_LEVELS_DB:
            noisy_path = os.path.join(tmp_dir, f"{os.path.splitext(os.path.basename(case.audio_path))[0]}_{snr_db:g}db.wav")
            make_noisy_wav(case.audio_path, snr_db, noisy_path)
            result = transcribe_audio(noisy_path)
            if not result.ok:
                rows.append(
                    {
                        "case": case.display_name,
                        "snr_db": snr_db,
                        "cer": None,
                        "wer": None,
                        "rtf": None,
                        "error": result.error,
                    }
                )
                continue
            duration = get_wav_duration_sec(noisy_path)
            rows.append(
                {
                    "case": case.display_name,
                    "snr_db": snr_db,
                    "cer": calc_cer(case.reference, result.text),
                    "wer": calc_wer(case.reference, result.text),
                    "rtf": result.elapsed_sec / duration if duration else None,
                    "error": "",
                }
            )
    return rows


def save_result_csv(metrics: Sequence[Dict[str, Any]], output_path: str) -> None:
    """保存明细结果，便于后续做趋势分析。"""
    fields = [
        "样本名称",
        "音频路径",
        "参考文本",
        "识别文本",
        "音频时长(秒)",
        "请求耗时(秒)",
        "字错误率(CER)",
        "词错误率(WER)",
        "BERTScore-F1",
        "幻觉率",
        "实时率(RTF)",
        "首字延迟(秒)",
        "尾字延迟(秒)",
        "离线尾延迟(秒)",
        "标点序列准确率",
        "标点精确率",
        "标点召回率",
        "标点F1",
        "数字/金额token准确率",
        "实体准确率",
        "未命中实体",
        "进程内存峰值(MB)",
        "CPU峰值(%)",
        "GPU显存峰值(MB)",
        "GPU利用率峰值(%)",
    ]
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in metrics:
            case: AsrMetricCase = item["case"]
            result: TranscribeResult = item["result"]
            writer.writerow({
                "样本名称": case.display_name,
                "音频路径": case.audio_path,
                "参考文本": case.reference,
                "识别文本": result.text,
                "音频时长(秒)": item["duration_sec"],
                "请求耗时(秒)": result.elapsed_sec,
                "字错误率(CER)": item["cer"],
                "词错误率(WER)": item["wer"],
                "BERTScore-F1": item["bertscore_f1"],
                "幻觉率": item["hallucination"],
                "实时率(RTF)": item["rtf"],
                "首字延迟(秒)": item["first_char_latency_sec"],
                "尾字延迟(秒)": item["tail_latency_sec"],
                "离线尾延迟(秒)": item["offline_tail_latency_sec"],
                "标点序列准确率": item["punctuation_acc"],
                "标点精确率": item["punctuation_precision"],
                "标点召回率": item["punctuation_recall"],
                "标点F1": item["punctuation_f1"],
                "数字/金额token准确率": item["itn_acc"],
                "实体准确率": item["entity_acc"],
                "未命中实体": "；".join(item["missed_entities"]),
                "进程内存峰值(MB)": result.resource.peak_process_rss_mb,
                "CPU峰值(%)": result.resource.peak_cpu_percent,
                "GPU显存峰值(MB)": result.resource.peak_gpu_memory_mb,
                "GPU利用率峰值(%)": result.resource.peak_gpu_util_percent,
            })


def run_precision_suite() -> List[Dict[str, Any]]:
    """执行完整指标套件，并在控制台输出汇总和明细。"""
    cases = load_cases()
    if not cases:
        pytest.skip("未配置 ASR 精度评测样本")

    print("\nASR 指标测试配置")
    print(f"API_URL={API_URL}")
    print(
        f"ASR_MODEL={ASR_MODEL}, LANGUAGE={LANGUAGE}, "
        f"CONNECT_TIMEOUT={ASR_CONNECT_TIMEOUT_SEC}s, READ_TIMEOUT={REQUEST_TIMEOUT_SEC}s"
    )
    print(
        f"ASR_REQUEST_MODE={ASR_REQUEST_MODE}, "
        f"ASR_EXTRACT_AUDIO_ONLY={ASR_EXTRACT_AUDIO_ONLY}, ASR_USE_DATA_URI={ASR_USE_DATA_URI}"
    )
    print(f"ASR_PREFLIGHT={ASR_PREFLIGHT}, ASR_PREFLIGHT_AUDIO={ASR_PREFLIGHT_AUDIO or '自动选择最小音频'}")
    print(f"CONSISTENCY_RUNS={CONSISTENCY_RUNS}, RUN_SNR_ROBUSTNESS={RUN_SNR_ROBUSTNESS}")
    print(f"WER 分词器={'jieba' if jieba is not None else '字符级回退（建议安装 jieba）'}")
    print("BERTScore=按需加载（仅在 ASR 成功且需要计算语义指标时加载）")
    print(f"实体匹配模式={ENTITY_MATCH_MODE}（substring 为兼容模式，exact_token 可降低子串误判）")
    print("格式化序列准确率说明：仅衡量数字/金额/日期等 token 序列编辑距离，不代表真正 ITN 转化效果。")

    run_asr_preflight(cases)

    metrics = [evaluate_case(case) for case in cases]

    detail_rows = []
    for item in metrics:
        case: AsrMetricCase = item["case"]
        result: TranscribeResult = item["result"]
        detail_rows.append(
            [
                case.display_name,
                result.status_code if result.status_code is not None else "ERR",
                fmt(item["duration_sec"], 3),
                fmt(result.elapsed_sec, 3),
                fmt(item["rtf"], 3),
                fmt(item["cer"]),
                fmt(item["wer"]),
                fmt(item["bertscore_f1"]),
                fmt(item["hallucination"]),
                fmt(item["punctuation_acc"]),
                fmt(item["punctuation_f1"]),
                fmt(item["itn_acc"]),
                fmt(item["entity_acc"]),
            ]
        )

    print_table(
        "单样本核心指标",
        [
            "样本",
            "HTTP",
            "音频时长(s)",
            "耗时(s)",
            "RTF",
            "CER",
            "WER",
            "BERTScore-F1",
            "幻觉",
            "标点准确率",
            "标点F1",
            "格式化序列准确率",
            f"实体准确率({ENTITY_MATCH_MODE})",
        ],
        detail_rows,
    )

    latency_rows = []
    resource_rows = []
    for item in metrics:
        case: AsrMetricCase = item["case"]
        result: TranscribeResult = item["result"]
        latency_rows.append(
            [
                case.display_name,
                fmt(item["first_char_latency_sec"], 3),
                fmt(item["tail_latency_sec"], 3),
                fmt(item["offline_tail_latency_sec"], 3),
                fmt(item["first_timestamp_sec"], 3),
                fmt(item["last_timestamp_sec"], 3),
                item["latency_note"],
            ]
        )
        resource_rows.append(
            [
                case.display_name,
                fmt(result.resource.peak_process_rss_mb, 1),
                fmt(result.resource.peak_cpu_percent, 1),
                fmt(result.resource.peak_gpu_memory_mb, 1),
                fmt(result.resource.peak_gpu_util_percent, 1),
                result.resource.note or "",
            ]
        )

    print_table(
        "首字/尾字延迟与时间戳",
        ["样本", "首字延迟(s)", "尾字延迟(s)", "离线尾延迟(s)", "首字音频时间(s)", "尾字音频时间(s)", "说明"],
        latency_rows,
    )
    print_table(
        "资源占用峰值（本机采样）",
        ["样本", "进程RSS(MB)", "CPU(%)", "GPU显存(MB)", "GPU利用率(%)", "说明"],
        resource_rows,
    )

    summary_rows = [
        ["样本数", len(metrics)],
        ["平均 CER", fmt(mean_optional(item["cer"] for item in metrics))],
        ["平均 WER", fmt(mean_optional(item["wer"] for item in metrics))],
        ["平均 BERTScore-F1", fmt(mean_optional(item["bertscore_f1"] for item in metrics))],
        ["幻觉率", fmt(mean_optional(item["hallucination"] for item in metrics))],
        ["平均 RTF", fmt(mean_optional(item["rtf"] for item in metrics), 3)],
        ["P95 耗时(s)", fmt(pct_optional((item["result"].elapsed_sec for item in metrics), 95), 3)],
        ["平均标点准确率", fmt(mean_optional(item["punctuation_acc"] for item in metrics))],
        ["平均标点F1", fmt(mean_optional(item["punctuation_f1"] for item in metrics))],
        ["平均格式化序列准确率", fmt(mean_optional(item["itn_acc"] for item in metrics))],
        [f"平均实体准确率({ENTITY_MATCH_MODE})", fmt(mean_optional(item["entity_acc"] for item in metrics))],
    ]
    print_table("汇总指标", ["指标", "值"], summary_rows)

    consistency_rows = []
    ok_cases = [item["case"] for item in metrics if item["result"].ok]
    for case in ok_cases:
        consistency = evaluate_consistency(case)
        consistency_rows.append(
            [
                consistency["case"],
                consistency["runs"],
                fmt(consistency["consistency_score"]),
                fmt(consistency["exact_match_rate"]),
                consistency["unique_outputs"],
                fmt(consistency["avg_elapsed_sec"], 3),
            ]
        )
    if consistency_rows:
        print_table(
            "输出一致性",
            ["样本", "次数", "一致性分数", "完全一致率", "不同输出数", "平均耗时(s)"],
            consistency_rows,
        )
    else:
        print("\n========== 输出一致性 ==========")
        print("N/A：主请求失败，跳过一致性复测。")

    snr_rows = []
    for case in ok_cases:
        for row in evaluate_snr_robustness(case):
            snr_rows.append(
                [
                    row["case"],
                    fmt(row["snr_db"], 1),
                    fmt(row["cer"]),
                    fmt(row["wer"]),
                    fmt(row["rtf"], 3),
                    row["error"],
                ]
            )
    if snr_rows:
        print_table("不同信噪比下的 WER/CER", ["样本", "SNR(dB)", "CER", "WER", "RTF", "错误"], snr_rows)
    else:
        print("\n========== 不同信噪比下的 WER/CER ==========")
        print("N/A：需要 manifest 提供 reference，且样本为 16-bit PCM WAV，并安装 numpy。")

    for item in metrics:
        if item["bertscore_note"]:
            print(f"\nBERTScore 说明 [{item['case'].display_name}]：{item['bertscore_note']}")
        if item["missed_entities"]:
            print(f"实体漏识别 [{item['case'].display_name}]：{item['missed_entities']}")

    output_csv = os.path.join(PROJECT_ROOT, "test_asr", "asr_precision_metrics.csv")
    save_result_csv(metrics, output_csv)
    print(f"\n明细结果已保存：{output_csv}")
    return metrics


@pytest.mark.asr
@pytest.mark.performance
def test_asr_precision_metrics(capsys: Any) -> None:
    """pytest 入口：运行后所有指标会输出到控制台。"""
    with capsys.disabled():
        metrics = run_precision_suite()
    assert metrics, "至少应完成一条 ASR 指标评测"
    failed = [
        item
        for item in metrics
        if not item["result"].ok
    ]
    assert not failed, "存在 ASR 请求失败样本：" + "；".join(
        f"{item['case'].display_name}(status={item['result'].status_code}, error={item['result'].error})"
        for item in failed
    )


if __name__ == "__main__":
    try:
        run_precision_suite()
    except AssertionError as exc:
        print(f"\n[ASR测试失败] {exc}", file=sys.stderr)
        sys.exit(1)
