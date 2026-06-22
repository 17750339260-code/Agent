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
import csv
import json
import math
import os
import re
import statistics
import subprocess
import tempfile
import threading
import time
import wave
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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
    import jieba
except ImportError:  # pragma: no cover
    jieba = None

try:
    from bert_score import score as bert_score_score
except ImportError:  # pragma: no cover
    bert_score_score = None


# ===================== 基础配置 =====================

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO_DIR = os.path.join(PROJECT_ROOT, "test_asr", "asr_test_audio")
DEFAULT_MANIFEST = os.path.join(PROJECT_ROOT, "test_asr", "asr_test_json", "asr_precision_manifest.json")
# ASR 服务地址、模型、超时均支持环境变量覆盖，便于测试不同环境。
API_URL = os.environ.get("ASR_API_URL", "http://36.111.82.53:10017/v1/audio/trans")
ASR_MODEL = os.environ.get("ASR_MODEL", "funasr-iic")
REQUEST_TIMEOUT_SEC = int(os.environ.get("ASR_TIMEOUT_SEC", "21600"))
LANGUAGE = os.environ.get("ASR_LANGUAGE", "zh")
ASR_PROGRESS_INTERVAL_SEC = float(os.environ.get("ASR_PROGRESS_INTERVAL_SEC", "10"))

# 一致性测试重复次数。大模型若存在采样随机性，同一音频多次结果不一致会被量化。短音频设置为 3，短音频设置为 1。
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

# 标点和 ITN 相关符号集合。ITN 主要关注数字、日期、百分比、金额、英文缩写等格式化结果。
PUNCTUATION_CHARS = set("，。！？；：、,.!?;:()（）《》“”\"'")
ITN_TOKEN_RE = re.compile(
    r"(\d+(?:[.,:：/-]\d+)*(?:%|％)?|[A-Za-z]+(?:-[A-Za-z]+)*|[￥$]\s*\d+(?:\.\d+)?)"
)


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


# ===================== 文本规范化与编辑距离 =====================


def normalize_for_cer(text: str) -> str:
    """CER 前处理：去空白和常见标点，保留中文、英文、数字等语义字符。"""
    text = text or ""
    chars = []
    for ch in text.lower():
        if ch.isspace() or ch in PUNCTUATION_CHARS:
            continue
        chars.append(ch)
    return "".join(chars)


def normalize_for_entity(text: str) -> str:
    """实体匹配前处理：去空白和标点，避免格式差异影响专名/否定词命中。"""
    return normalize_for_cer(text)


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
    """
    WER 分词：
    - 若文本本身有空格，按空格切词，适合英文或人工已分词文本；
    - 中文无空格时优先用 jieba，未安装时退化为字符级 token，并在报告中说明。
    """
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not normalized:
        return []
    if " " in normalized:
        return [t for t in normalized.split(" ") if t]
    if jieba is not None:
        return [t for t in jieba.lcut(normalized) if t.strip() and t not in PUNCTUATION_CHARS]
    return list(normalize_for_cer(normalized))


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


def calc_bertscore(reference: str, hypothesis: str) -> Tuple[Optional[float], str]:
    """
    BERTScore F1：调用成熟 bert_score 包计算语义相似度。
    未安装依赖或无参考文本时返回 N/A，而不是用其它指标冒充 BERTScore。
    """
    if not reference.strip():
        return None, "缺少人工参考文本"
    if not hypothesis.strip():
        return 0.0, "识别为空"
    if bert_score_score is None:
        return None, "未安装 bert_score，可执行 pip install bert-score"

    try:
        _, _, f1 = bert_score_score(
            [hypothesis],
            [reference],
            lang="zh",
            model_type=BERTSCORE_MODEL,
            verbose=False,
            rescale_with_baseline=False,
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
    return 1.0 if normalize_for_cer(hypothesis) else 0.0


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


def extract_itn_tokens(text: str) -> List[str]:
    """抽取 ITN 关注 token：数字、日期、百分比、金额、英文缩写等。"""
    return [m.group(0).replace(" ", "") for m in ITN_TOKEN_RE.finditer(text or "")]


def calc_itn_accuracy(reference: str, hypothesis: str) -> Optional[float]:
    """ITN 准确率：比较格式化 token 序列，参考中无 ITN token 时返回 N/A。"""
    return calc_sequence_accuracy(extract_itn_tokens(reference), extract_itn_tokens(hypothesis))


def calc_entity_accuracy(reference_entities: Sequence[str], hypothesis: str) -> Tuple[Optional[float], List[str]]:
    """
    关键实体准确率：按配置实体做精确包含匹配，适合专有名词、否定词、业务关键词。
    返回值为 (命中率, 未命中实体列表)。
    """
    entities = [e for e in reference_entities if e]
    if not entities:
        return None, []

    normalized_hyp = normalize_for_entity(hypothesis)
    missed = []
    for entity in entities:
        if normalize_for_entity(entity) not in normalized_hyp:
            missed.append(entity)
    return (len(entities) - len(missed)) / len(entities), missed


# ===================== 音频与资源采样 =====================


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
        return None


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
    signal_power = float(np.mean(audio ** 2))
    if signal_power <= 0:
        raise RuntimeError("源音频能量为 0，无法按 SNR 加噪")
    noise_power = signal_power / (10 ** (snr_db / 10.0))
    rng = np.random.default_rng(seed=20260605 + int(snr_db * 10))
    noise = rng.normal(0.0, math.sqrt(noise_power), size=audio.shape)
    write_wav_int16(output_path, audio + noise, sample_rate)


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
    return {
        "model": ASR_MODEL,
        "input_type": "stream",
        "input": encode_audio_as_base64(file_path),
        "hotwords": "",
        "language": LANGUAGE,
        "is_return_timestamp": True,
        "speaker_diarization": False,
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
    sampler = ResourceSampler()
    progress = AsrProgressReporter(file_path, get_wav_duration_sec(file_path))
    sampler.start()
    progress.start()
    start = time.perf_counter()
    try:
        response = requests.post(
            API_URL,
            json=build_payload(file_path),
            headers={"accept": "application/json", "Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT_SEC,
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
            error="" if response.status_code == 200 else response.text[:500],
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
                if s > 1000 or e > 1000:
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
                    if s > 1000 or e > 1000:
                        s /= 1000.0
                        e /= 1000.0
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
                value = float(value)
                return value / 1000.0 if value > 1000 else value
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
            "itn_acc": None,
            "entity_acc": None,
            "missed_entities": [],
        }

    cer = calc_cer(case.reference, result.text)
    wer = calc_wer(case.reference, result.text)
    bert_f1, bert_note = calc_bertscore(case.reference, result.text)
    hallucination = calc_hallucination(case.is_hallucination_probe, result.text)
    punctuation_acc = calc_punctuation_accuracy(case.reference, result.text)
    itn_acc = calc_itn_accuracy(case.reference, result.text)
    entity_acc, missed_entities = calc_entity_accuracy(case.key_entities, result.text)
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
        "case",
        "audio",
        "reference",
        "hypothesis",
        "duration_sec",
        "elapsed_sec",
        "cer",
        "wer",
        "bertscore_f1",
        "hallucination",
        "rtf",
        "first_char_latency_sec",
        "tail_latency_sec",
        "offline_tail_latency_sec",
        "punctuation_acc",
        "itn_acc",
        "entity_acc",
        "missed_entities",
        "peak_process_rss_mb",
        "peak_cpu_percent",
        "peak_gpu_memory_mb",
        "peak_gpu_util_percent",
    ]
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in metrics:
            case: AsrMetricCase = item["case"]
            result: TranscribeResult = item["result"]
            writer.writerow(
                {
                    "case": case.display_name,
                    "audio": case.audio_path,
                    "reference": case.reference,
                    "hypothesis": result.text,
                    "duration_sec": item["duration_sec"],
                    "elapsed_sec": result.elapsed_sec,
                    "cer": item["cer"],
                    "wer": item["wer"],
                    "bertscore_f1": item["bertscore_f1"],
                    "hallucination": item["hallucination"],
                    "rtf": item["rtf"],
                    "first_char_latency_sec": item["first_char_latency_sec"],
                    "tail_latency_sec": item["tail_latency_sec"],
                    "offline_tail_latency_sec": item["offline_tail_latency_sec"],
                    "punctuation_acc": item["punctuation_acc"],
                    "itn_acc": item["itn_acc"],
                    "entity_acc": item["entity_acc"],
                    "missed_entities": "；".join(item["missed_entities"]),
                    "peak_process_rss_mb": result.resource.peak_process_rss_mb,
                    "peak_cpu_percent": result.resource.peak_cpu_percent,
                    "peak_gpu_memory_mb": result.resource.peak_gpu_memory_mb,
                    "peak_gpu_util_percent": result.resource.peak_gpu_util_percent,
                }
            )


def run_precision_suite() -> List[Dict[str, Any]]:
    """执行完整指标套件，并在控制台输出汇总和明细。"""
    cases = load_cases()
    if not cases:
        pytest.skip("未配置 ASR 精度评测样本")

    print("\nASR 指标测试配置")
    print(f"API_URL={API_URL}")
    print(f"ASR_MODEL={ASR_MODEL}, LANGUAGE={LANGUAGE}, TIMEOUT={REQUEST_TIMEOUT_SEC}s")
    print(f"CONSISTENCY_RUNS={CONSISTENCY_RUNS}, RUN_SNR_ROBUSTNESS={RUN_SNR_ROBUSTNESS}")
    print(f"WER 分词器={'jieba' if jieba is not None else '字符级回退（建议安装 jieba）'}")
    print(f"BERTScore={'已启用' if bert_score_score is not None else '未安装 bert_score，相关列输出 N/A'}")

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
            "ITN准确率",
            "实体准确率",
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
        ["平均 ITN 准确率", fmt(mean_optional(item["itn_acc"] for item in metrics))],
        ["平均实体准确率", fmt(mean_optional(item["entity_acc"] for item in metrics))],
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
    run_precision_suite()
