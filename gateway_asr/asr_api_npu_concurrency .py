#!/usr/bin/env python3
# -*- coding: utf-8 -*-


from __future__ import annotations

# ---------- 标准库：解析命令行、异步、编解码、统计等 ----------
import argparse      # 解析命令行参数，如 --url --tests
import asyncio       # 异步并发，同时发很多 HTTP 请求
import base64        # 把 wav 二进制转成接口需要的 base64 字符串
import json          # 保存压测报告为 JSON
import math          # 固定 QPS 计划请求数计算
import os            # 路径、读文件
import re            # 文本归一化和中文数字处理
import ssl           # HTTPS 证书设置
import statistics    # 求平均数等
import sys           # 退出程序 sys.exit
import time          # 计时
import unicodedata   # 全角/半角归一化、标点分类
import wave          # 读取 wav 头信息（采样率、时长）
from collections import Counter
from dataclasses import asdict, dataclass, field  # 数据类，少写样板代码
from datetime import datetime, timezone
from enum import Enum
import hmac
import shutil
import subprocess
from hashlib import sha256
from typing import Any, Optional  # 类型标注，方便阅读

import aiohttp         # 异步 HTTP 客户端，用来调 ASR 接口

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# jiwer：可选库，用于精确计算 CER/WER；没装则用简化算法
try:
    from jiwer import cer as jiwer_cer
    from jiwer import wer as jiwer_wer
except ImportError:
    jiwer_cer = None
    jiwer_wer = None

# ==================== 默认配置（不改命令行时就用这些） ====================

# DEFAULT_GATEWAY_URL = "https://10.134.252.232:5030/ai-gateway/predict"
# DEFAULT_GATEWAY_APP_KEY = "1000400672300031"
# DEFAULT_GATEWAY_SECRET_KEY = "b899eef382324e8d8973493fb9c35998"
# DEFAULT_GATEWAY_COMPONENT_CODE = "04351378"
# DEFAULT_GATEWAY_FUNCTION = "funasr-iic"
# 评测网关
DEFAULT_GATEWAY_URL = "https://10.134.252.232:5030/ai-gateway/predict"
DEFAULT_GATEWAY_APP_KEY = "1000400672300031"
DEFAULT_GATEWAY_SECRET_KEY = "b899eef382324e8d8973493fb9c35998"
DEFAULT_GATEWAY_COMPONENT_CODE = "04351378"
DEFAULT_GATEWAY_FUNCTION = "funasr-iic"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)  # 项目根目录
DEFAULT_AUDIO = os.path.join(SCRIPT_DIR, "2.wav")
REFERENCE_TEXT_FILENAME = "asr_reference_text.txt"
DEFAULT_REFERENCE_TEXT = os.path.join(
    os.path.dirname(DEFAULT_AUDIO), REFERENCE_TEXT_FILENAME
)  # 默认参考文本，与默认音频放在同一目录，用于计算 CER/WER

# HTTPS 时跳过证书校验（内网/自签证书场景）
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# 部分损坏的 wav 头会写成超大时长，超过 1 小时则不可信
MAX_REASONABLE_AUDIO_SECONDS = 3600.0

class LoadTestHelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Avoid misleading defaults on mutually exclusive boolean switches."""

    def _get_help_string(self, action: argparse.Action) -> str:
        if action.dest in {"gateway", "use_data_uri"}:
            return action.help or ""
        return super()._get_help_string(action)


# --tests 参数可选的全部压测名称
ALL_TEST_NAMES = (
    "baseline",
    "ramp",
    "sustained",
    "spike",
    "soak",
    "fixed_qps",
    "accuracy_load",
    "fixed_requests",
    "acceptance",
)


class TestKind(str, Enum):
    """压测类型枚举，与 ALL_TEST_NAMES 一一对应"""
    BASELINE = "baseline"
    RAMP = "ramp"
    SUSTAINED = "sustained"
    SPIKE = "spike"
    SOAK = "soak"
    FIXED_QPS = "fixed_qps"
    ACCURACY_LOAD = "accuracy_load"
    FIXED_REQUESTS = "fixed_requests"
    ACCEPTANCE = "acceptance"


# ==================== 数据结构（用 dataclass 打包相关字段，方便传递） ====================


@dataclass
class AudioMeta:
    """测试音频的元信息（读文件后填充）"""
    path: str              # 文件路径
    duration_sec: float    # 音频时长（秒），用于算 RTF
    file_bytes: int        # 原始文件大小（字节）
    sample_rate: int       # 采样率，如 16000
    channels: int          # 声道数，1=单声道
    b64_data: str          # 整段音频的 base64，请求时放进 JSON

    media_format: str = ""
    codec: str = ""
    duration_source: str = ""

    @property
    def b64_kb(self) -> int:
        """base64 字符串大约多少 KB（仅展示用）"""
        return len(self.b64_data) // 1024

    @property
    def file_kb(self) -> int:
        """原始 wav 文件大约多少 KB"""
        return self.file_bytes // 1024


@dataclass
class RequestResult:
    """单次 ASR 请求的结果（发一次请求就产生一条）"""
    success: bool = False           # HTTP 200 且解析到结果
    start_ts: float = 0.0           # perf_counter 起始时间，用于按真实时间分桶
    end_ts: float = 0.0             # perf_counter 完成时间，用于按真实时间分桶
    http_status: Optional[int] = None
    latency_sec: float = 0.0        # 从发请求到收完响应的总耗时
    ttfb_sec: float = 0.0           # 从请求开始到收到响应体首字节
    body_read_sec: float = 0.0       # 兼容旧报告字段，等于 response_completion_sec
    response_completion_sec: Optional[float] = None  # 响应完成时间：首字节到最后字节
    network_total_sec: float = 0.0   # 完整网络传输总耗时：请求开始到最后字节
    response_bytes: int = 0          # HTTP 响应体字节数
    response_preview: str = ""       # 响应体摘要，便于排查 HTTP 200 业务错误
    text: str = ""                  # 识别出的文本
    text_chars: int = 0             # 识别文本字数
    rtf: float = 0.0                # latency / 音频时长
    chars_per_sec: float = 0.0      # 每秒识别多少字
    cer: Optional[float] = None     # 字错误率（有参考文本才算）
    wer: Optional[float] = None     # 词错误率
    error: Optional[str] = None     # 失败时的错误描述
    error_type: Optional[str] = None  # 错误分类：Timeout / ConnError / HTTP_5xx 等


@dataclass
class AsrAggMetrics:
    """多笔成功请求汇总后的 ASR 专项指标"""
    avg_latency: float = 0.0        # 成功请求平均延迟
    p50_latency: float = 0.0        # 成功请求 P50 延迟
    p95_latency: float = 0.0        # 成功请求 P95 延迟
    p99_latency: float = 0.0        # 成功请求 P99 延迟
    avg_ttfb: float = 0.0
    p95_ttfb: float = 0.0
    avg_rtf: float = 0.0            # 平均实时因子
    p50_rtf: float = 0.0
    p95_rtf: float = 0.0
    max_rtf: float = 0.0            # 最慢的一条 RTF
    avg_chars_per_sec: float = 0.0  # 请求内文本产出速率：总字数 / 总请求耗时
    text_throughput_cps: float = 0.0  # 系统文本吞吐：总字数 / 墙钟时间
    audio_chars_per_sec: float = 0.0  # 音频内容语速：总字数 / 总音频时长
    avg_text_chars: float = 0.0     # 平均每条返回多少字
    empty_text_rate: float = 0.0    # 返回空文本的比例（%）
    avg_cer: Optional[float] = None
    p95_cer: Optional[float] = None
    avg_wer: Optional[float] = None
    p95_wer: Optional[float] = None
    audio_throughput_x: float = 0.0  # 成功处理的「音频秒数」÷ 墙钟秒数
    realtime_ratio: float = 0.0      # RTF<1 的请求占比（%）


@dataclass
class StressAggMetrics:
    """一轮压测汇总后的「压力测试」指标（内含 asr 子对象）"""
    test_kind: str = ""             # 如 baseline / ramp / spike
    concurrency: int = 0            # 本轮并发数
    target_qps: float = 0.0         # fixed_qps 测试时的目标 QPS
    wall_sec: float = 0.0           # 本轮实际经过的墙钟时间
    total_requests: int = 0         # 发出请求总数
    success_count: int = 0
    success_rate: float = 0.0       # 成功率（%）
    attempt_qps: float = 0.0        # 总请求数 / 墙钟时间
    success_qps: float = 0.0        # 成功数 / 墙钟时间
    avg_response_latency: float = 0.0  # 全部请求平均响应耗时（成功+失败）
    p95_response_latency: float = 0.0  # 全部请求 P95 响应耗时（成功+失败）
    p99_response_latency: float = 0.0  # 全部请求 P99 响应耗时（成功+失败）
    avg_network_total_sec: float = 0.0  # 完整网络传输总耗时平均值
    p95_network_total_sec: float = 0.0  # 完整网络传输总耗时 P95
    p99_network_total_sec: float = 0.0  # 完整网络传输总耗时 P99
    avg_response_completion_sec: float = 0.0  # 首字节到最后字节平均耗时
    p95_response_completion_sec: float = 0.0  # 首字节到最后字节 P95
    p99_response_completion_sec: float = 0.0  # 首字节到最后字节 P99
    response_completion_samples: int = 0  # 成功收到首字节和最后字节的样本数
    response_completion_coverage: float = 0.0  # 响应完成时间样本覆盖率（%）
    error_types: dict[str, int] = field(default_factory=dict)  # 各类错误次数
    extra: dict[str, Any] = field(default_factory=dict)        # 额外说明项
    asr: AsrAggMetrics = field(default_factory=AsrAggMetrics)


@dataclass
class TestRunContext:
    """一次压测运行时的公共参数（传给每次 HTTP 请求）"""
    url: str                        # 接口地址
    audio: AudioMeta                # 测试音频
    model: str                      # 模型名，如 funasr-iic
    timeout_s: float                # 单次请求超时（秒）
    use_ssl: bool                   # 是否 HTTPS
    reference_text: Optional[str] = None  # 标准答案，用于算 CER/WER
    hotwords: Optional[str] = None    # 热词；未配置时传 null，与参考网关脚本保持一致
    gateway: bool = False           # 是否走 AI Gateway /predict 统一入口
    component_code: str = ""        # 网关组件编码
    app_key: str = ""               # 网关客户编码 / app key
    secret_key: str = ""            # 网关签名密钥
    gateway_function: str = ""      # 网关 function 字段，默认等于模型名
    use_data_uri: bool = False      # 网关 ASR 示例使用 data:audio/...;base64,... 输入
    include_stream_param: bool = False  # 参考网关协议不需要 stream 字段；默认按非流式 JSON 读取完整响应
    stream_param_name: str = "stream"
    is_return_timestamp: bool = False
    allow_empty_text: bool = False   # 是否允许 HTTP 200 但识别文本为空仍算成功


# ==================== 工具函数（音频、指标计算、拼请求体） ====================


def resolve_input_path(path: str, *, default_base: str = PROJECT_ROOT) -> str:
    """Resolve an input file path from cwd first, then script/project locations."""
    if os.path.isabs(path):
        return os.path.abspath(path)

    candidates = [
        os.path.abspath(path),
        os.path.abspath(os.path.join(SCRIPT_DIR, path)),
        os.path.abspath(os.path.join(default_base, path)),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def default_reference_path_for_audio(audio_path: str) -> str:
    """Default reference text lives next to the resolved audio file."""
    return os.path.join(os.path.dirname(os.path.abspath(audio_path)), REFERENCE_TEXT_FILENAME)


def resolve_reference_path(reference_file: Optional[str], audio_path: str) -> Optional[str]:
    """Resolve explicit reference file, or fall back to audio-directory default."""
    if reference_file == "":
        return None
    if reference_file:
        audio_dir = os.path.dirname(os.path.abspath(audio_path))
        return resolve_input_path(reference_file, default_base=audio_dir)
    return default_reference_path_for_audio(audio_path)


def _is_riff_wave(raw: bytes) -> bool:
    return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"


def _float_from_probe(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def _int_from_probe(value: Any) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def _probe_audio_with_ffprobe(audio_path: str) -> Optional[dict[str, Any]]:
    """Read duration/codec metadata for compressed or mislabeled audio files."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_name,codec_type,sample_rate,channels,duration:format=duration,format_name",
        "-of",
        "json",
        audio_path,
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if completed.returncode != 0:
        return None

    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return None

    streams = data.get("streams") or []
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})
    fmt = data.get("format") or {}
    duration_sec = _float_from_probe(audio_stream.get("duration")) or _float_from_probe(
        fmt.get("duration")
    )
    if duration_sec <= 0:
        return None

    return {
        "duration_sec": duration_sec,
        "sample_rate": _int_from_probe(audio_stream.get("sample_rate")),
        "channels": _int_from_probe(audio_stream.get("channels")),
        "codec": str(audio_stream.get("codec_name") or ""),
        "media_format": str(fmt.get("format_name") or ""),
        "duration_source": "ffprobe",
    }


def _abort_untrusted_audio_duration(audio_path: str, reason: str) -> None:
    print(f"❌ 无法可靠识别音频时长，已停止测试: {audio_path}")
    print(f"   原因: {reason}")
    print("   请安装 ffmpeg/ffprobe，或将测试音频转换为标准 PCM WAV 后重试。")
    sys.exit(1)


def load_audio_meta(audio_path: str) -> AudioMeta:
    """Read audio metadata and base64 payload using trusted duration sources."""
    audio_path = resolve_input_path(audio_path, default_base=SCRIPT_DIR)
    if not os.path.exists(audio_path):
        print(f"ERROR: audio file does not exist: {audio_path}")
        print(f"       default audio path: {DEFAULT_AUDIO}")
        sys.exit(1)

    with open(audio_path, "rb") as f:
        raw = f.read()

    if not raw:
        print(f"ERROR: audio file is empty: {audio_path}")
        sys.exit(1)

    duration_sec = 0.0
    sample_rate = 0
    channels = 0
    media_format = ""
    codec = ""
    duration_source = ""

    if _is_riff_wave(raw):
        try:
            with wave.open(audio_path, "rb") as wf:
                sample_rate = wf.getframerate()
                channels = wf.getnchannels()
                frames = wf.getnframes()
                header_duration = frames / float(sample_rate) if sample_rate else 0.0
                if 0 < header_duration <= MAX_REASONABLE_AUDIO_SECONDS:
                    duration_sec = header_duration
                    media_format = "wav"
                    codec = "pcm"
                    duration_source = "wav_header"
                else:
                    print(
                        f"WARNING: WAV header duration looks abnormal ({header_duration:.2f}s); trying ffprobe."
                    )
        except wave.Error as exc:
            print(f"WARNING: failed to parse RIFF/WAV header ({exc}); trying ffprobe.")
    else:
        print("WARNING: file content is not RIFF/WAV; trying ffprobe for real media duration.")

    if duration_sec <= 0:
        probe = _probe_audio_with_ffprobe(audio_path)
        if not probe:
            _abort_untrusted_audio_duration(
                audio_path,
                "not a reliable PCM WAV and ffprobe could not read media duration",
            )
        duration_sec = float(probe["duration_sec"])
        sample_rate = sample_rate or int(probe.get("sample_rate") or 0)
        channels = channels or int(probe.get("channels") or 0)
        media_format = str(probe.get("media_format") or media_format)
        codec = str(probe.get("codec") or codec)
        duration_source = str(probe.get("duration_source") or duration_source)

    return AudioMeta(
        path=audio_path,
        duration_sec=duration_sec,
        file_bytes=len(raw),
        sample_rate=sample_rate,
        channels=channels,
        b64_data=base64.b64encode(raw).decode("utf-8"),
        media_format=media_format,
        codec=codec,
        duration_source=duration_source,
    )


def load_reference_text(
    reference_file: Optional[str], inline_text: Optional[str], audio_path: str
) -> Optional[str]:
    """
    读取 CER/WER 参考文本。
    inline_text 保留向后兼容；日常使用推荐维护 reference_file，无需在命令行传长文本。
    """
    if inline_text and inline_text.strip():
        return inline_text.strip()

    path = resolve_reference_path(reference_file, audio_path)
    if not path:
        return None

    if not os.path.exists(path):
        print(f"  ⚠️ 参考文本文件不存在，CER/WER 将显示 N/A: {path}")
        return None

    with open(path, "r", encoding="utf-8") as f:
        text = f.read().strip()

    if not text:
        print(f"  ⚠️ 参考文本文件为空，CER/WER 将显示 N/A: {path}")
        return None

    return text


def resolve_reference_source(
    reference_file: Optional[str], inline_text: Optional[str], audio_path: str
) -> str:
    """返回当前参考文本来源，用于启动日志和 JSON 报告。"""
    if inline_text and inline_text.strip():
        return "命令行 --reference-text"
    return resolve_reference_path(reference_file, audio_path) or ""


def build_data_uri(audio_path: str, b64_data: str) -> str:
    """把 base64 音频包装成网关示例使用的 Data URI 格式。"""
    ext = os.path.splitext(audio_path)[1][1:].lower() or "wav"
    return f"data:audio/{ext};base64,{b64_data}"


def build_gateway_auth_headers(app_key: str, secret_key: str, accept: str = "application/json") -> dict[str, str]:
    """按 AI Gateway 要求生成 x-date 和 authorization 请求头。"""
    x_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %T GMT")
    sign_text = f"x-date: {x_date}"
    signature = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), sign_text.encode("utf-8"), sha256).digest()
    ).decode("utf-8")
    return {
        "x-date": x_date,
        "authorization": (
            f'hmac username="{app_key}", algorithm="hmac-sha256", '
            f'headers="x-date", signature="{signature}"'
        ),
        "Content-Type": "application/json",
        "Accept": accept,
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache",
    }


def build_request_headers(ctx: TestRunContext, *, response_stream: Optional[bool] = None) -> dict[str, str]:
    """构造单次请求头；网关模式下每次请求都重新签名。"""
    if not ctx.gateway:
        return {"Content-Type": "application/json", "Accept": "application/json"}
    accept = "text/event-stream" if response_stream else "application/json"
    return build_gateway_auth_headers(ctx.app_key, ctx.secret_key, accept=accept)


def build_request_payload(
    ctx: TestRunContext,
    *,
    model: Optional[str] = None,
    response_stream: Optional[bool] = None,
) -> dict:
    """构造 POST JSON body，兼容直连 ASR 和 AI Gateway 两种入口。"""
    effective_model = model or ctx.model
    input_value = (
        build_data_uri(ctx.audio.path, ctx.audio.b64_data)
        if ctx.gateway and ctx.use_data_uri
        else ctx.audio.b64_data
    )
    payload = {
        "model": effective_model,
        # input_type=stream 表示 input 携带 Base64/Data URI，不代表响应采用流式返回。
        "input_type": "stream",
        "input": input_value,
        "hotwords": ctx.hotwords,
        "language": "zh",
    }
    if ctx.gateway:
        payload = {
            "componentCode": ctx.component_code,
            "function": ctx.gateway_function or effective_model,
            **payload,
            "is_return_timestamp": ctx.is_return_timestamp,
        }
    if ctx.include_stream_param:
        # 仅供明确支持该响应开关的后端使用；当前网关默认不发送此字段。
        payload[ctx.stream_param_name] = bool(response_stream) if response_stream is not None else False
    return payload


def parse_response_body(raw: bytes, content_type: str = "") -> Any:
    """解析 JSON 或 SSE/plain text 响应，便于流式探测也能提取文本。"""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}

    looks_json = "json" in content_type.lower() or text[:1] in ("{", "[")
    if looks_json:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    stream_parts: list[Any] = []
    plain_parts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            continue
        try:
            stream_parts.append(json.loads(line))
        except json.JSONDecodeError:
            plain_parts.append(line)

    if stream_parts:
        return stream_parts
    return {"text": "\n".join(plain_parts) if plain_parts else text}


def extract_text(body: Any) -> str:
    """从接口返回的 JSON 里取出识别文本（兼容多种字段名和嵌套结构）"""
    if isinstance(body, dict):
        for key in ("text", "result", "transcript", "transcription", "recognized_text"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
        for key in (
            "result",
            "data",
            "results",
            "output",
            "response",
            "payload",
            "segments",
            "sentence_info",
        ):
            nested = extract_text(body.get(key))
            if nested:
                return nested
    if isinstance(body, list) and body:
        texts = [extract_text(item) for item in body]
        return " ".join(text for text in texts if text).strip()
    return ""


def extract_business_error(body: Any) -> Optional[str]:
    """识别 HTTP 200 响应中的网关/业务错误，避免把快速失败统计为成功。"""
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
        return message or "响应字段 success=false"

    if "code" in body and body.get("code") not in (None, ""):
        code = body.get("code")
        normalized_code = str(code).strip().lower()
        success_codes = {"0", "00", "00000", "000000", "200", "ok", "success", "true"}
        if normalized_code not in success_codes:
            return f"code={code}" + (f", {message}" if message else "")

    status = str(body.get("status", "")).strip().lower()
    if status in {"error", "failed", "failure", "fail", "rejected", "cancelled", "canceled"}:
        return f"status={body.get('status')}" + (f", {message}" if message else "")

    error_value = body.get("error")
    if error_value not in (None, "", False, 0, "0"):
        if isinstance(error_value, (dict, list)):
            error_text = json.dumps(error_value, ensure_ascii=False)
        else:
            error_text = str(error_value)
        return error_text[:500]

    for key in ("data", "result", "output"):
        nested = body.get(key)
        if isinstance(nested, dict):
            nested_error = extract_business_error(nested)
            if nested_error:
                return nested_error
    return None


_CHINESE_DATE_NUM_RE = re.compile(r"([零〇一二三四五六七八九两十]{1,8})(?=[年月日号])")


def _chinese_date_number_to_arabic(text: str) -> str:
    """把二零二五年、十月这类日期数字归一成 2025年、10月，减少 ITN 差异对 CER/WER 的污染。"""
    digit_map = {
        "零": "0", "〇": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
        "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
    }

    def repl(match: re.Match[str]) -> str:
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


def normalize_asr_text(text: str, *, keep_spaces: bool = False) -> str:
    """
    ASR 准确率前处理：
    - NFKC 统一全角/半角，英文转小写；
    - 去掉标点、符号和多余空白；
    - 日期场景下把中文数字归一为阿拉伯数字。
    """
    text = _chinese_date_number_to_arabic(unicodedata.normalize("NFKC", text or "").lower())
    out: list[str] = []
    last_space = False
    for ch in text:
        if ch.isspace():
            if keep_spaces and out and not last_space:
                out.append(" ")
                last_space = True
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("P") or cat.startswith("S"):
            continue
        out.append(ch)
        last_space = False
    return "".join(out).strip()


def _is_cjk_char(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x20000 <= code <= 0x2A6DF
        or 0x2A700 <= code <= 0x2B73F
        or 0x2B740 <= code <= 0x2B81F
        or 0x2B820 <= code <= 0x2CEAF
    )


def tokenize_for_wer(text: str) -> list[str]:
    """
    中文 ASR 没有天然空格分词。这里按中文单字、连续英文/数字词元切分，
    避免整段中文被 split() 当成一个词而导致 WER 经常显示 100%。
    """
    normalized = normalize_asr_text(text, keep_spaces=True)
    tokens: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            tokens.append("".join(buf))
            buf.clear()

    for ch in normalized:
        if ch.isspace():
            flush()
        elif _is_cjk_char(ch):
            flush()
            tokens.append(ch)
        elif ch.isalnum():
            buf.append(ch)
        else:
            flush()
    flush()
    return tokens


def edit_distance(ref: list[str] | str, hyp: list[str] | str) -> int:
    """标准 Levenshtein 编辑距离，用于 CER/WER。"""
    if len(ref) < len(hyp):
        ref, hyp = hyp, ref
    previous = list(range(len(hyp) + 1))
    for i, r_item in enumerate(ref, 1):
        current = [i]
        for j, h_item in enumerate(hyp, 1):
            insert_cost = current[j - 1] + 1
            delete_cost = previous[j] + 1
            replace_cost = previous[j - 1] + (0 if r_item == h_item else 1)
            current.append(min(insert_cost, delete_cost, replace_cost))
        previous = current
    return previous[-1]


def calc_cer(reference: str, hypothesis: str) -> Optional[float]:
    """字错误率 Character Error Rate：0 表示完全一致，越大越差"""
    if not reference:
        return None
    ref = normalize_asr_text(reference, keep_spaces=False)
    hyp = normalize_asr_text(hypothesis, keep_spaces=False)
    if not ref:
        return None
    if not hyp:
        return 1.0
    if jiwer_cer is not None:
        return float(jiwer_cer(ref, hyp))
    return edit_distance(ref, hyp) / len(ref)


def calc_wer(reference: str, hypothesis: str) -> Optional[float]:
    """词错误率 Word Error Rate：中文按单字、英文/数字按连续词元计算。"""
    if not reference:
        return None
    ref_words = tokenize_for_wer(reference)
    hyp_words = tokenize_for_wer(hypothesis)
    if not ref_words:
        return None
    if not hyp_words:
        return 1.0
    if jiwer_wer is not None:
        return float(jiwer_wer(" ".join(ref_words), " ".join(hyp_words)))
    return edit_distance(ref_words, hyp_words) / len(ref_words)


def percentile(values: list[float], p: float) -> float:
    """计算分位数，如 p=95 即 P95 延迟"""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    k = (len(ordered) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def aggregate_asr(
    successes: list[RequestResult], audio_duration: float, wall_sec: float
) -> AsrAggMetrics:
    """把多笔成功请求聚合成一份 AsrAggMetrics"""
    if not successes:
        return AsrAggMetrics()

    latencies = [r.latency_sec for r in successes]
    ttfbs = [r.ttfb_sec for r in successes]
    rtfs = [r.rtf for r in successes if r.rtf > 0]
    chars = [r.text_chars for r in successes]
    cers = [r.cer for r in successes if r.cer is not None]
    wers = [r.wer for r in successes if r.wer is not None]
    empty_count = sum(1 for r in successes if r.text_chars == 0)

    total_audio_sec = audio_duration * len(successes)
    total_latency_sec = sum(latencies)
    total_text_chars = sum(chars)
    audio_tp = total_audio_sec / wall_sec if wall_sec > 0 else 0.0
    realtime_ratio = (sum(1 for r in rtfs if r < 1.0) / len(rtfs) * 100) if rtfs else 0.0

    return AsrAggMetrics(
        avg_latency=statistics.mean(latencies),
        p50_latency=percentile(latencies, 50),
        p95_latency=percentile(latencies, 95),
        p99_latency=percentile(latencies, 99),
        avg_ttfb=statistics.mean(ttfbs),
        p95_ttfb=percentile(ttfbs, 95),
        avg_rtf=statistics.mean(rtfs) if rtfs else 0.0,
        p50_rtf=percentile(rtfs, 50) if rtfs else 0.0,
        p95_rtf=percentile(rtfs, 95) if rtfs else 0.0,
        max_rtf=max(rtfs) if rtfs else 0.0,
        avg_chars_per_sec=total_text_chars / total_latency_sec if total_latency_sec > 0 else 0.0,
        text_throughput_cps=total_text_chars / wall_sec if wall_sec > 0 else 0.0,
        audio_chars_per_sec=total_text_chars / total_audio_sec if total_audio_sec > 0 else 0.0,
        avg_text_chars=statistics.mean(chars) if chars else 0.0,
        empty_text_rate=empty_count / len(successes) * 100,
        avg_cer=statistics.mean(cers) if cers else None,
        p95_cer=percentile(cers, 95) if cers else None,
        avg_wer=statistics.mean(wers) if wers else None,
        p95_wer=percentile(wers, 95) if wers else None,
        audio_throughput_x=audio_tp,
        realtime_ratio=realtime_ratio,
    )


def build_stress_metrics(
    test_kind: str,
    results: list[RequestResult],
    wall_sec: float,
    audio_duration: float,
    concurrency: int = 0,
    target_qps: float = 0.0,
    extra: Optional[dict[str, Any]] = None,
) -> StressAggMetrics:
    """根据原始请求列表，统计成功率/QPS，并调用 aggregate_asr 填 asr 字段"""
    successes = [r for r in results if r.success]
    network_total_times = [
        r.network_total_sec if r.network_total_sec > 0 else r.latency_sec for r in results
    ]
    response_completion_times = [
        r.response_completion_sec
        for r in results
        if r.response_completion_sec is not None
    ]
    total = len(results)
    success_count = len(successes)
    success_rate = (success_count / total * 100) if total else 0.0
    error_types: dict[str, int] = {}
    error_samples: list[str] = []
    for r in results:
        if not r.success and r.error_type:
            error_types[r.error_type] = error_types.get(r.error_type, 0) + 1
            error_sample = (r.error or "")[:300]
            if error_sample and error_sample not in error_samples and len(error_samples) < 3:
                error_samples.append(error_sample)

    extra_metrics = dict(extra or {})
    if error_samples:
        extra_metrics["错误样例"] = " | ".join(error_samples)

    return StressAggMetrics(
        test_kind=test_kind,
        concurrency=concurrency,
        target_qps=target_qps,
        wall_sec=wall_sec,
        total_requests=total,
        success_count=success_count,
        success_rate=success_rate,
        attempt_qps=total / wall_sec if wall_sec > 0 else 0.0,
        success_qps=success_count / wall_sec if wall_sec > 0 else 0.0,
        avg_response_latency=statistics.mean(network_total_times) if network_total_times else 0.0,
        p95_response_latency=percentile(network_total_times, 95) if network_total_times else 0.0,
        p99_response_latency=percentile(network_total_times, 99) if network_total_times else 0.0,
        avg_network_total_sec=statistics.mean(network_total_times) if network_total_times else 0.0,
        p95_network_total_sec=percentile(network_total_times, 95) if network_total_times else 0.0,
        p99_network_total_sec=percentile(network_total_times, 99) if network_total_times else 0.0,
        avg_response_completion_sec=(
            statistics.mean(response_completion_times) if response_completion_times else 0.0
        ),
        p95_response_completion_sec=(
            percentile(response_completion_times, 95) if response_completion_times else 0.0
        ),
        p99_response_completion_sec=(
            percentile(response_completion_times, 99) if response_completion_times else 0.0
        ),
        response_completion_samples=len(response_completion_times),
        response_completion_coverage=(
            len(response_completion_times) / total * 100 if total else 0.0
        ),
        error_types=error_types,
        extra=extra_metrics,
        asr=aggregate_asr(successes, audio_duration, wall_sec),
    )


# ==================== 控制台指标输出（人类可读的表格） ====================


def _fmt_pct_opt(value: Optional[float]) -> str:
    """把 0.05 格式化成 5.00%，没有值则显示 N/A"""
    return f"{value:.2%}" if value is not None else "N/A"


def print_test_banner(kind: TestKind, description: str) -> None:
    """打印每个压测阶段开始时的标题横幅"""
    print("\n" + "=" * 92)
    print(f"【{kind.value}】{description}")
    print("=" * 92)


def print_stress_metrics(m: StressAggMetrics, *, focus: str = "") -> None:
    """压力测试核心指标"""
    print("\n  ┌─ 压力测试指标 " + (f"({focus})" if focus else "") + " ─┐")
    print(f"  │ 测试类型:     {m.test_kind}")
    if m.concurrency:
        print(f"  │ 并发数:       {m.concurrency}")
    if m.target_qps > 0:
        print(f"  │ 目标 QPS:     {m.target_qps:.2f}")
    print(f"  │ 墙钟时间:     {m.wall_sec:.2f}s")
    print(f"  │ 总请求:       {m.total_requests} | 成功: {m.success_count}")
    print(f"  │ 成功率:       {m.success_rate:.2f}%")
    print(f"  │ 尝试吞吐(QPS): {m.attempt_qps:.3f}")
    print(f"  │ 成功吞吐(QPS): {m.success_qps:.3f}")
    print(f"  │ 完整网络传输总耗时: 平均 {m.avg_network_total_sec:.3f}s | "
          f"P95 {m.p95_network_total_sec:.3f}s | P99 {m.p99_network_total_sec:.3f}s")
    print(
        f"  │ 响应完成时间(首字节→最后字节): 平均 {m.avg_response_completion_sec:.3f}s | "
        f"P95 {m.p95_response_completion_sec:.3f}s | "
        f"P99 {m.p99_response_completion_sec:.3f}s | "
        f"样本 {m.response_completion_samples}/{m.total_requests} "
        f"({m.response_completion_coverage:.1f}%)"
    )
    if m.error_types:
        err = ", ".join(f"{k}:{v}" for k, v in sorted(m.error_types.items(), key=lambda x: -x[1]))
        print(f"  │ 错误分布:     {err}")
    else:
        print("  │ 错误分布:     无")
    for key, val in m.extra.items():
        print(f"  │ {key}: {val}")
    print("  └" + "─" * 40 + "┘")


def print_asr_metrics(a: AsrAggMetrics, audio: AudioMeta, *, sample_text: str = "") -> None:
    """ASR 专项指标（准确度 + 实时性 + 吞吐）"""
    print("\n  ┌─ ASR 专项指标 ─┐")
    print(f"  │ 音频时长:     {audio.duration_sec:.2f}s ({audio.sample_rate}Hz, {audio.channels}ch)")
    realtime_tag = "✓ 实时" if a.avg_rtf < 1 else "✗ 非实时"
    print(f"  │ 成功请求总响应时间: 平均 {a.avg_latency:.3f}s | P50 {a.p50_latency:.3f}s | "
          f"P95 {a.p95_latency:.3f}s | P99 {a.p99_latency:.3f}s")
    print(f"  │ 首字节响应时间(TTFB): 平均 {a.avg_ttfb:.3f}s | P95 {a.p95_ttfb:.3f}s")
    print(f"  │ 实时因子(RTF): 平均 {a.avg_rtf:.3f}x ({realtime_tag}) | P50 {a.p50_rtf:.3f}x | "
          f"P95 {a.p95_rtf:.3f}x | 最大 {a.max_rtf:.3f}x")
    print(f"  │ 实时占比:     {a.realtime_ratio:.1f}% 请求 RTF<1")
    print(f"  │ 文本吞吐:     {a.text_throughput_cps:.1f} 字/墙钟秒 | 请求内 {a.avg_chars_per_sec:.1f} 字/请求秒")
    print(f"  │ 音频语速:     {a.audio_chars_per_sec:.1f} 字/音频秒 | 平均 {a.avg_text_chars:.0f} 字/条")
    print(f"  │ 空结果率:     {a.empty_text_rate:.2f}%")
    print(f"  │ 音频吞吐:     {a.audio_throughput_x:.3f}x (成功音频秒/墙钟秒)")
    print(f"  │ 字错率(CER):  平均 {_fmt_pct_opt(a.avg_cer)} | P95 {_fmt_pct_opt(a.p95_cer)}")
    print(f"  │ 词错率(WER):  平均 {_fmt_pct_opt(a.avg_wer)} | P95 {_fmt_pct_opt(a.p95_wer)}")
    if sample_text:
        preview = sample_text[:70] + ("…" if len(sample_text) > 70 else "")
        print(f"  │ 识别样例:     {preview}")
    print("  └" + "─" * 40 + "┘")


def print_combined_result(
    m: StressAggMetrics,
    audio: AudioMeta,
    *,
    focus: str = "",
    sample_text: str = "",
) -> None:
    """一轮压测结束：先打压力指标，再打 ASR 指标"""
    print_stress_metrics(m, focus=focus)
    if m.success_count > 0:
        print_asr_metrics(m.asr, audio, sample_text=sample_text)
    else:
        print("\n  ⚠️ 无成功请求，跳过 ASR 专项指标")


# ==================== HTTP 请求（核心：发一次 ASR 调用） ====================


async def send_one_request(
    session: aiohttp.ClientSession,
    ctx: TestRunContext,
    payload: dict,
    *,
    response_stream: Optional[bool] = None,
) -> RequestResult:
    """
    发送单次 POST 请求到 ASR 服务。
    session 可复用（连接池）；每次返回一个 RequestResult。
    """
    result = RequestResult()
    start = time.perf_counter()  # 高精度计时
    result.start_ts = start

    try:
        async with session.post(
            ctx.url,
            json=payload,
            headers=build_request_headers(ctx, response_stream=response_stream),
            ssl=SSL_CTX if ctx.use_ssl else False,
            timeout=aiohttp.ClientTimeout(total=ctx.timeout_s),
        ) as resp:
            result.http_status = resp.status
            first_body_byte_at: Optional[float] = None
            last_body_byte_at: Optional[float] = None
            chunks: list[bytes] = []
            async for chunk in resp.content.iter_chunked(8192):
                if chunk:
                    chunk_received_at = time.perf_counter()
                    if first_body_byte_at is None:
                        first_body_byte_at = chunk_received_at
                    last_body_byte_at = chunk_received_at
                    chunks.append(chunk)

            # JSON 解析前立即落点；有响应体时使用最后一块数据到达时间，空响应才回退到 EOF。
            response_eof_at = time.perf_counter()
            response_finished_at = last_body_byte_at or response_eof_at
            result.end_ts = response_finished_at
            result.network_total_sec = response_finished_at - start
            result.ttfb_sec = (
                first_body_byte_at - start
                if first_body_byte_at is not None
                else result.network_total_sec
            )
            result.response_completion_sec = (
                max(response_finished_at - first_body_byte_at, 0.0)
                if first_body_byte_at is not None and last_body_byte_at is not None
                else None
            )
            result.body_read_sec = result.response_completion_sec or 0.0
            raw_body = b"".join(chunks)
            result.response_bytes = len(raw_body)
            result.response_preview = raw_body.decode("utf-8", errors="replace")[:1000]

            if resp.status == 200:
                body = parse_response_body(raw_body, resp.headers.get("Content-Type", ""))
                result.text = extract_text(body)
                result.text_chars = len(result.text)
                business_error = extract_business_error(body)
                if business_error:
                    result.error = f"业务错误: {business_error}; 响应: {result.response_preview[:500]}"
                    result.error_type = "BusinessError"
                elif result.text or ctx.allow_empty_text:
                    result.success = True
                else:
                    result.error = f"空识别结果; HTTP 200 响应: {result.response_preview[:500]}"
                    result.error_type = "EmptyResult"
            else:
                result.error = f"HTTP {resp.status}: {result.response_preview[:500]}"
                result.error_type = f"HTTP_{resp.status}"

    except asyncio.TimeoutError:
        result.error = f"Timeout ({ctx.timeout_s}s)"
        result.error_type = "Timeout"
    except aiohttp.ServerDisconnectedError:
        result.error = "ServerDisconnected"
        result.error_type = "ConnError"
    except aiohttp.ClientConnectorError as e:
        result.error = f"ConnectorError: {str(e)[:120]}"
        result.error_type = "ConnError"
    except aiohttp.ClientError as e:
        result.error = str(e)[:120]
        result.error_type = "ConnError"
    except (json.JSONDecodeError, ValueError) as e:
        result.error = f"BadJSON: {str(e)[:120]}"
        result.error_type = "BadJSON"
    except Exception as e:
        result.error = str(e)[:120]
        result.error_type = "Other"

    if result.end_ts <= 0:
        result.end_ts = time.perf_counter()
    if result.network_total_sec <= 0:
        result.network_total_sec = result.end_ts - start
    result.latency_sec = result.network_total_sec
    if result.ttfb_sec <= 0:
        result.ttfb_sec = result.latency_sec

    # 成功时计算 RTF、识别速度、准确度
    dur = ctx.audio.duration_sec
    if result.success and dur > 0:
        result.rtf = result.latency_sec / dur
        if result.latency_sec > 0:
            result.chars_per_sec = result.text_chars / result.latency_sec
        if ctx.reference_text:
            result.cer = calc_cer(ctx.reference_text, result.text)
            result.wer = calc_wer(ctx.reference_text, result.text)

    return result


async def _run_workers_for_duration(
    ctx: TestRunContext,
    concurrency: int,
    duration_sec: float,
    *,
    reference: bool = False,
    stagger_sec: float = 0.1,
) -> list[RequestResult]:
    """
    通用并发压测引擎：启动 concurrency 个协程 worker，
    在 duration_sec 秒内循环发请求，直到时间到。
    只要 ctx.reference_text 有值，就会计算 CER/WER。
    """
    results: list[RequestResult] = []
    error_types: dict[str, int] = {}
    lock = asyncio.Lock()  # 多协程写 results 列表需要加锁
    level_start = time.perf_counter()
    stop_at = level_start + duration_sec

    ref_ctx = ctx

    async def worker(session: aiohttp.ClientSession, initial_delay: float = 0.0) -> None:
        """单个工人：时间没到就不断发请求"""
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        while time.perf_counter() < stop_at:
            payload = build_request_payload(ctx)
            res = await send_one_request(session, ref_ctx, payload)
            async with lock:
                results.append(res)
                if not res.success and res.error_type:
                    error_types[res.error_type] = error_types.get(res.error_type, 0) + 1

    connector = aiohttp.TCPConnector(limit=max(concurrency + 5, 10))
    async with aiohttp.ClientSession(connector=connector) as session:
        # 错峰启动：避免第一瞬间同时打出 concurrency 个请求
        tasks = [
            asyncio.create_task(worker(session, i * stagger_sec if stagger_sec > 0 else 0.0))
            for i in range(concurrency)
        ]
        await asyncio.gather(*tasks)  # 等待所有 worker 结束

    return results


# ==================== 七种压测场景（注意：函数名是 run_ 不是 test_，避免 pytest 误收集） ====================


async def run_baseline(
    ctx: TestRunContext,
    models: list[str],
    rounds: int,
) -> dict[str, StressAggMetrics]:
    """
    【基准测试】一次只发 1 个请求，不并发。
    对 models 里每个模型重复 rounds 轮，对比谁更快、谁更准。
    """
    print_test_banner(TestKind.BASELINE, "单请求基准 — 各模型响应时间 / 实时因子 / 准确度")
    print(
        f"\n  {'模型':<14} {'轮次':<5} {'完整网络(s)':<12} {'响应完成(s)':<12} "
        f"{'首字节(s)':<10} {'实时因子':<8} {'识别字/秒':<10} {'字错率':<8} {'词错率':<8} {'状态'}"
    )
    print("  " + "-" * 106)

    out: dict[str, StressAggMetrics] = {}
    async with aiohttp.ClientSession() as session:
        for model in models:
            model_ctx = TestRunContext(
                url=ctx.url,
                audio=ctx.audio,
                model=model,
                timeout_s=ctx.timeout_s,
                use_ssl=ctx.use_ssl,
                reference_text=ctx.reference_text,
                hotwords=ctx.hotwords,
                gateway=ctx.gateway,
                component_code=ctx.component_code,
                app_key=ctx.app_key,
                secret_key=ctx.secret_key,
                gateway_function=ctx.gateway_function,
                use_data_uri=ctx.use_data_uri,
                include_stream_param=ctx.include_stream_param,
                stream_param_name=ctx.stream_param_name,
                is_return_timestamp=ctx.is_return_timestamp,
                allow_empty_text=ctx.allow_empty_text,
            )
            results: list[RequestResult] = []
            print(f"\n  [{model}]")
            model_start = time.perf_counter()
            active_request_sec = 0.0
            for r in range(rounds):
                payload = build_request_payload(model_ctx, model=model)
                res = await send_one_request(session, model_ctx, payload)
                active_request_sec += res.latency_sec
                results.append(res)
                cer_s = _fmt_pct_opt(res.cer) if res.cer is not None else "-"
                wer_s = _fmt_pct_opt(res.wer) if res.wer is not None else "-"
                tag = "✓" if res.success else "✗"
                preview = (res.text[:26] + "…") if len(res.text) > 26 else res.text
                if not res.success:
                    preview = (res.error or "")[:26]
                response_completion_s = (
                    f"{res.response_completion_sec:.3f}"
                    if res.response_completion_sec is not None
                    else "不可用"
                )
                print(
                    f"  {model:<12} {r + 1:<5} {res.network_total_sec:<12.3f} "
                    f"{response_completion_s:<12} {res.ttfb_sec:<10.3f} {res.rtf:<8.3f} "
                    f"{res.chars_per_sec:<10.1f} "
                    f"{cer_s:<8} {wer_s:<8} {tag}  {preview}"
                )
                await asyncio.sleep(0.5)
            wall = time.perf_counter() - model_start
            m = build_stress_metrics(
                TestKind.BASELINE.value, results, wall, ctx.audio.duration_sec, concurrency=1
            )
            m.extra["model"] = model
            m.extra["rounds"] = rounds
            m.extra["请求活跃耗时"] = f"{active_request_sec:.3f}s"
            out[model] = m
            sample = results[-1].text if results else ""
            print_combined_result(m, ctx.audio, focus=model, sample_text=sample)
    return out


async def run_ramp(
    ctx: TestRunContext,
    levels: list[int],
    duration_per_level: int,
    stop_success_rate: float,
) -> list[StressAggMetrics]:
    """
    【递增并发】并发从 1→2→4→8… 逐级升高，每级跑 duration_per_level 秒。
    若成功率低于 stop_success_rate，停止继续加并发（认为已到极限）。
    """
    print_test_banner(
        TestKind.RAMP,
        f"递增并发 — 模型 {ctx.model}，每级 {duration_per_level}s，低于 {stop_success_rate:.0f}% 停止",
    )
    all_levels: list[StressAggMetrics] = []
    print(
        f"\n  {'并发':<6} {'总数':<6} {'成功':<6} {'成功率':<8} {'成功吞吐':<9} "
        f"{'完整网络P95':<13} {'响应完成P95':<13} {'成功P95':<9} "
        f"{'平均实时因子':<12} {'音频吞吐':<10}"
    )
    print("  " + "-" * 112)

    for concurrency in levels:
        t0 = time.perf_counter()
        results = await _run_workers_for_duration(ctx, concurrency, duration_per_level)
        wall = time.perf_counter() - t0
        m = build_stress_metrics(
            TestKind.RAMP.value, results, wall, ctx.audio.duration_sec, concurrency=concurrency
        )
        all_levels.append(m)
        stable = m.success_rate >= stop_success_rate
        tag = "✓" if stable else "✗"
        print(
            f"  {concurrency:<6} {m.total_requests:<6} {m.success_count:<6} {m.success_rate:<7.1f}% "
            f"{m.success_qps:<9.3f} {m.p95_network_total_sec:<13.3f} "
            f"{m.p95_response_completion_sec:<13.3f} "
            f"{m.asr.p95_latency:<9.3f} {m.asr.avg_rtf:<12.3f} "
            f"{m.asr.audio_throughput_x:<10.3f}  {tag}"
        )
        print_combined_result(m, ctx.audio, focus=f"并发={concurrency}")
        if not stable:
            print(f"\n  ⚠️ 并发 {concurrency} 成功率未达 {stop_success_rate:.0f}%，停止递增")
            break
        await asyncio.sleep(2)
    return all_levels


async def run_sustained(
    ctx: TestRunContext,
    concurrency: int,
    duration_sec: int,
) -> StressAggMetrics:
    """【恒定负载】固定 concurrency 个并发，持续打 duration_sec 秒，看稳态 QPS/延迟。"""
    print_test_banner(
        TestKind.SUSTAINED,
        f"恒定并发负载 — {concurrency} 并发持续 {duration_sec}s",
    )
    t0 = time.perf_counter()
    results = await _run_workers_for_duration(ctx, concurrency, duration_sec)
    wall = time.perf_counter() - t0
    m = build_stress_metrics(
        TestKind.SUSTAINED.value, results, wall, ctx.audio.duration_sec, concurrency=concurrency
    )
    print_combined_result(m, ctx.audio, focus="恒定负载稳态")
    return m


async def run_spike(
    ctx: TestRunContext,
    low_concurrency: int,
    spike_concurrency: int,
    low_sec: int,
    spike_sec: int,
    recovery_sec: int,
) -> StressAggMetrics:
    """
    【突发尖峰】模拟流量突变：低并发 → 突然高并发 → 再降回低并发。
    观察尖峰阶段成功率、P95 延迟是否恶化。
    """
    print_test_banner(
        TestKind.SPIKE,
        f"突发尖峰 — {low_concurrency}→{spike_concurrency}→{low_concurrency} 并发",
    )
    phases: list[tuple[str, int, int, float, list[RequestResult]]] = []
    all_results: list[RequestResult] = []
    total_wall = 0.0

    for phase_name, conc, dur in (
        ("预热", low_concurrency, low_sec),
        ("尖峰", spike_concurrency, spike_sec),
        ("恢复", low_concurrency, recovery_sec),
    ):
        print(f"\n  ▶ 阶段 [{phase_name}] 并发={conc}, 持续 {dur}s …")
        t0 = time.perf_counter()
        phase_results = await _run_workers_for_duration(ctx, conc, dur, stagger_sec=0.05)
        wall = time.perf_counter() - t0
        total_wall += wall
        all_results.extend(phase_results)
        pm = build_stress_metrics(
            f"{TestKind.SPIKE.value}_{phase_name}",
            phase_results,
            wall,
            ctx.audio.duration_sec,
            concurrency=conc,
        )
        phases.append((phase_name, conc, dur, wall, phase_results))
        print_stress_metrics(pm, focus=phase_name)
        if pm.success_count > 0:
            print_asr_metrics(pm.asr, ctx.audio)
        else:
            print("\n  ⚠️ 无成功请求，跳过 ASR 专项指标")

    m = build_stress_metrics(
        TestKind.SPIKE.value, all_results, total_wall, ctx.audio.duration_sec,
        concurrency=spike_concurrency,
    )
    spike_phase = next(p for p in phases if p[0] == "尖峰")
    spike_m = build_stress_metrics(
        "spike_peak", spike_phase[4], spike_phase[3], ctx.audio.duration_sec,
        concurrency=spike_concurrency,
    )
    m.extra["尖峰成功率"] = f"{spike_m.success_rate:.1f}%"
    m.extra["尖峰全部请求P95延迟"] = f"{spike_m.p95_response_latency:.3f}s"
    m.extra["尖峰成功请求P95延迟"] = f"{spike_m.asr.p95_latency:.3f}s"
    m.extra["尖峰平均RTF"] = f"{spike_m.asr.avg_rtf:.3f}x"
    print("\n  ── 尖峰测试汇总 ──")
    print_combined_result(m, ctx.audio, focus="全阶段合计")
    return m


async def run_soak(
    ctx: TestRunContext,
    concurrency: int,
    duration_sec: int,
    bucket_sec: int,
) -> StressAggMetrics:
    """
    【浸泡测试】长时间运行，每 bucket_sec 秒统计一桶延迟。
    若末桶 P95 比首桶高很多，可能存在内存泄漏或队列积压。
    """
    print_test_banner(
        TestKind.SOAK,
        f"长时间浸泡 — {concurrency} 并发 × {duration_sec}s，每 {bucket_sec}s 分桶",
    )
    results: list[RequestResult] = []
    bucket_all_lats_by_idx: dict[int, list[float]] = {}
    bucket_success_lats_by_idx: dict[int, list[float]] = {}
    bucket_success_by_idx: Counter[int] = Counter()
    bucket_total_by_idx: Counter[int] = Counter()
    lock = asyncio.Lock()
    start = time.perf_counter()
    stop_at = start + duration_sec

    async def worker(session: aiohttp.ClientSession) -> None:
        while time.perf_counter() < stop_at:
            payload = build_request_payload(ctx)
            res = await send_one_request(session, ctx, payload)
            async with lock:
                results.append(res)
                elapsed = max(res.end_ts - start, 0.0)
                bucket_idx = int(elapsed // bucket_sec) if bucket_sec > 0 else 0
                bucket_total_by_idx[bucket_idx] += 1
                bucket_all_lats_by_idx.setdefault(bucket_idx, []).append(res.latency_sec)
                if res.success:
                    bucket_success_by_idx[bucket_idx] += 1
                    bucket_success_lats_by_idx.setdefault(bucket_idx, []).append(res.latency_sec)

    connector = aiohttp.TCPConnector(limit=max(concurrency + 5, 10))
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*[worker(session) for _ in range(concurrency)])

    wall = time.perf_counter() - start
    if bucket_sec > 0:
        expected_last_bucket = max(0, math.ceil(duration_sec / bucket_sec) - 1)
        wall_last_bucket = max(0, math.ceil(wall / bucket_sec) - 1)
        seen_last_bucket = max(bucket_total_by_idx.keys(), default=0)
        max_bucket_idx = max(expected_last_bucket, wall_last_bucket, seen_last_bucket)
    else:
        max_bucket_idx = 0
    bucket_latencies: list[tuple[int, float, float, float, float, int, int]] = []
    for idx in range(max_bucket_idx + 1):
        all_lats = bucket_all_lats_by_idx.get(idx, [])
        success_lats = bucket_success_lats_by_idx.get(idx, [])
        if all_lats:
            all_avg_l = statistics.mean(all_lats)
            all_p95_l = percentile(all_lats, 95)
        else:
            all_avg_l = 0.0
            all_p95_l = 0.0
        if success_lats:
            success_avg_l = statistics.mean(success_lats)
            success_p95_l = percentile(success_lats, 95)
        else:
            success_avg_l = 0.0
            success_p95_l = 0.0
        bucket_latencies.append((
            idx,
            all_avg_l,
            all_p95_l,
            success_avg_l,
            success_p95_l,
            bucket_success_by_idx[idx],
            bucket_total_by_idx[idx],
        ))
    m = build_stress_metrics(
        TestKind.SOAK.value, results, wall, ctx.audio.duration_sec, concurrency=concurrency
    )

    non_empty_buckets = [b for b in bucket_latencies if b[6] > 0]
    if len(non_empty_buckets) >= 2:
        first_p95 = non_empty_buckets[0][2]
        last_p95 = non_empty_buckets[-1][2]
        drift = ((last_p95 - first_p95) / first_p95 * 100) if first_p95 > 0 else 0.0
        m.extra["首桶全部请求P95延迟"] = f"{first_p95:.3f}s"
        m.extra["末桶全部请求P95延迟"] = f"{last_p95:.3f}s"
        m.extra["全部请求P95漂移"] = f"{drift:+.1f}%"

    print("\n  分桶延迟趋势 (全部平均 / 全部P95 / 成功平均 / 成功P95 / 成功 / 总数):")
    for idx, all_avg_l, all_p95_l, success_avg_l, success_p95_l, success_n, total_n in bucket_latencies:
        print(
            f"    桶 {idx + 1:>2}: 全部平均 {all_avg_l:.3f}s | 全部P95 {all_p95_l:.3f}s | "
            f"成功平均 {success_avg_l:.3f}s | 成功P95 {success_p95_l:.3f}s | {success_n}/{total_n}"
        )
    print_combined_result(m, ctx.audio, focus="浸泡稳态")
    return m


async def run_fixed_qps(
    ctx: TestRunContext,
    target_qps: float,
    duration_sec: int,
    max_in_flight: int,
) -> StressAggMetrics:
    """
    【固定 QPS】按固定间隔发请求（如 2 QPS = 每 0.5 秒 1 个），
    不按并发数打满。max_in_flight 限制同时在飞的请求数。
    """
    print_test_banner(
        TestKind.FIXED_QPS,
        f"固定速率 — 目标 {target_qps} QPS × {duration_sec}s，最大在途 {max_in_flight}",
    )
    if target_qps <= 0 or duration_sec <= 0 or max_in_flight <= 0:
        print("\n  ⚠️ fixed_qps 参数非法：target_qps、duration_sec、max_in_flight 必须大于 0")
        return StressAggMetrics(test_kind=TestKind.FIXED_QPS.value, target_qps=target_qps)

    results: list[RequestResult] = []
    lock = asyncio.Lock()
    in_flight_lock = asyncio.Lock()
    in_flight = 0
    interval = 1.0 / target_qps  # 两次请求之间的间隔
    start = time.perf_counter()
    stop_at = start + duration_sec
    scheduled_count = 0
    dropped_by_limit = 0

    async def one_shot(session: aiohttp.ClientSession) -> None:
        nonlocal in_flight
        try:
            payload = build_request_payload(ctx)
            res = await send_one_request(session, ctx, payload)
            async with lock:
                results.append(res)
        finally:
            async with in_flight_lock:
                in_flight -= 1

    connector = aiohttp.TCPConnector(limit=max(max_in_flight + 5, 20))
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks: list[asyncio.Task[None]] = []
        next_send = start
        while time.perf_counter() < stop_at:
            now = time.perf_counter()
            if now >= next_send:
                while next_send <= now and next_send < stop_at:
                    async with in_flight_lock:
                        can_send = in_flight < max_in_flight
                        if can_send:
                            in_flight += 1
                    if can_send:
                        scheduled_count += 1
                        tasks.append(asyncio.create_task(one_shot(session)))
                    else:
                        dropped_by_limit += 1
                    next_send += interval
            else:
                await asyncio.sleep(next_send - now)
        send_window_end = time.perf_counter()
        if tasks:
            await asyncio.gather(*tasks)

    wall = time.perf_counter() - start
    send_window_sec = max(send_window_end - start, 0.0)
    drain_sec = max(wall - send_window_sec, 0.0)
    planned = math.ceil(duration_sec * target_qps)
    m = build_stress_metrics(
        TestKind.FIXED_QPS.value, results, wall, ctx.audio.duration_sec,
        target_qps=target_qps,
    )
    m.extra["计划请求数"] = planned
    m.extra["实际发起"] = scheduled_count
    m.extra["完成请求数"] = len(results)
    m.extra["在途上限跳过"] = dropped_by_limit
    m.extra["发起窗口"] = f"{send_window_sec:.3f}s"
    m.extra["排空耗时"] = f"{drain_sec:.3f}s"
    m.extra["发起QPS"] = f"{scheduled_count / send_window_sec:.3f}" if send_window_sec > 0 else "N/A"
    m.extra["完成QPS"] = f"{len(results) / wall:.3f}" if wall > 0 else "N/A"
    m.extra["达成率"] = f"{scheduled_count / planned * 100:.1f}%" if planned else "N/A"
    print_combined_result(m, ctx.audio, focus=f"目标QPS={target_qps}")
    return m


async def run_accuracy_load(
    ctx: TestRunContext,
    concurrency: int,
    duration_sec: int,
) -> StressAggMetrics:
    """
    【并发准确度】高并发下仍计算每条 CER，看压测是否导致识别变差或结果不一致。
    需要参考文本文件或 --reference-text。
    """
    if not ctx.reference_text:
        print("\n  ⚠️ accuracy_load 需要参考文本文件或 --reference-text，已跳过")
        return StressAggMetrics(test_kind=TestKind.ACCURACY_LOAD.value)

    print_test_banner(
        TestKind.ACCURACY_LOAD,
        f"并发准确度 — {concurrency} 并发 × {duration_sec}s，对比参考文本",
    )
    t0 = time.perf_counter()
    results = await _run_workers_for_duration(
        ctx, concurrency, duration_sec, reference=True, stagger_sec=0.1
    )
    wall = time.perf_counter() - t0
    m = build_stress_metrics(
        TestKind.ACCURACY_LOAD.value, results, wall, ctx.audio.duration_sec,
        concurrency=concurrency,
    )
    successes = [r for r in results if r.success]
    if successes:
        cers = [r.cer for r in successes if r.cer is not None]
        if cers:
            m.extra["CER标准差"] = f"{statistics.pstdev(cers):.4f}" if len(cers) > 1 else "0"
            m.extra["CER>10%占比"] = f"{sum(1 for c in cers if c > 0.1) / len(cers) * 100:.1f}%"
        texts = [r.text for r in successes if r.text]
        if texts:
            text_counter = Counter(texts)
            most_common_text, most_common_count = text_counter.most_common(1)[0]
            consistency = most_common_count / len(texts) * 100
            m.extra["结果一致性"] = f"{consistency:.1f}% 相同"
            m.extra["唯一结果数"] = len(text_counter)
            m.extra["主结果样例"] = most_common_text[:50] + ("…" if len(most_common_text) > 50 else "")
    sample = successes[0].text if successes else ""
    print_combined_result(m, ctx.audio, focus="并发准确度", sample_text=sample)
    return m


async def run_single_probe(
    ctx: TestRunContext,
    *,
    response_stream: bool,
    label: str,
) -> StressAggMetrics:
    """单请求探测，用于验收流式首字节和非流式完整响应耗时。"""
    mode = "流式" if response_stream else "非流式"
    print_test_banner(TestKind.ACCEPTANCE, f"{label} — 单并发{mode}探测")
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=2)) as session:
        payload = build_request_payload(ctx, response_stream=response_stream)
        t0 = time.perf_counter()
        res = await send_one_request(session, ctx, payload, response_stream=response_stream)
        wall = time.perf_counter() - t0
    m = build_stress_metrics(
        TestKind.ACCEPTANCE.value,
        [res],
        wall,
        ctx.audio.duration_sec,
        concurrency=1,
        extra={"模式": mode},
    )
    sample = res.text if res.success else res.error or ""
    print_combined_result(m, ctx.audio, focus=label, sample_text=sample)
    return m


async def run_fixed_requests(
    ctx: TestRunContext,
    concurrency: int,
    total_requests: int,
    *,
    response_stream: Optional[bool] = None,
) -> StressAggMetrics:
    """固定总请求数压测：最多 concurrency 个在途请求，总共只发 total_requests 条。"""
    print_test_banner(
        TestKind.FIXED_REQUESTS,
        f"固定请求数 — {concurrency} 并发 / {total_requests} 请求",
    )
    if concurrency <= 0 or total_requests <= 0:
        print("\n  ⚠️ fixed_requests 参数非法：concurrency、total_requests 必须大于 0")
        return StressAggMetrics(test_kind=TestKind.FIXED_REQUESTS.value, concurrency=concurrency)

    queue: asyncio.Queue[int] = asyncio.Queue()
    for request_id in range(total_requests):
        queue.put_nowait(request_id)

    results: list[RequestResult] = []
    lock = asyncio.Lock()
    start = time.perf_counter()

    async def worker(session: aiohttp.ClientSession) -> None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                payload = build_request_payload(ctx, response_stream=response_stream)
                res = await send_one_request(session, ctx, payload, response_stream=response_stream)
                async with lock:
                    results.append(res)
            finally:
                queue.task_done()

    connector = aiohttp.TCPConnector(limit=max(concurrency + 5, 10))
    async with aiohttp.ClientSession(connector=connector) as session:
        workers = [asyncio.create_task(worker(session)) for _ in range(min(concurrency, total_requests))]
        await asyncio.gather(*workers)

    wall = time.perf_counter() - start
    m = build_stress_metrics(
        TestKind.FIXED_REQUESTS.value,
        results,
        wall,
        ctx.audio.duration_sec,
        concurrency=concurrency,
        extra={"计划请求数": total_requests},
    )
    sample = next((r.text for r in results if r.success and r.text), "")
    print_combined_result(m, ctx.audio, focus=f"{concurrency}并发/{total_requests}请求", sample_text=sample)
    return m


def _pass_fail(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def print_acceptance_summary(
    *,
    audio: AudioMeta,
    stream_probe: Optional[StressAggMetrics],
    non_stream_probe: Optional[StressAggMetrics],
    fixed_8x10: StressAggMetrics,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """打印并返回本次音频验收指标判定。"""
    stream_ttfb = stream_probe.asr.avg_ttfb if stream_probe and stream_probe.success_count else None
    non_stream_latency = (
        non_stream_probe.asr.avg_latency if non_stream_probe and non_stream_probe.success_count else None
    )
    rtf_source = stream_probe or non_stream_probe
    single_rtf = rtf_source.asr.avg_rtf if rtf_source and rtf_source.success_count else None
    success_p95_latency = fixed_8x10.asr.p95_latency if fixed_8x10.success_count else None
    all_p95_latency = fixed_8x10.p95_response_latency if fixed_8x10.total_requests else None
    stable_ok = (
        fixed_8x10.concurrency >= args.metric_stable_concurrency
        and fixed_8x10.total_requests >= args.metric_total_requests
        and fixed_8x10.success_count > 0
    )
    success_ok = fixed_8x10.success_rate >= args.metric_success_rate
    success_p95_ok = success_p95_latency is not None and success_p95_latency <= args.metric_p95_latency
    all_p95_ok = all_p95_latency is not None and all_p95_latency <= args.metric_p95_latency

    checks = [
        {
            "name": "当前音频 单并发流式 TTFB",
            "threshold": f"<= {args.metric_stream_ttfb:.2f}s",
            "value": None if stream_ttfb is None else round(stream_ttfb, 3),
            "pass": stream_ttfb is not None and stream_ttfb <= args.metric_stream_ttfb,
        },
        {
            "name": "当前音频 单并发非流式完整响应",
            "threshold": f"<= {args.metric_non_stream_latency:.2f}s",
            "value": None if non_stream_latency is None else round(non_stream_latency, 3),
            "pass": non_stream_latency is not None and non_stream_latency <= args.metric_non_stream_latency,
        },
        {
            "name": "当前音频 单并发 RTF",
            "threshold": f"<= {args.metric_rtf:.2f}",
            "value": None if single_rtf is None else round(single_rtf, 3),
            "pass": single_rtf is not None and single_rtf <= args.metric_rtf,
        },
        {
            "name": "当前音频 稳定并发数",
            "threshold": f">= {args.metric_stable_concurrency}",
            "value": fixed_8x10.concurrency,
            "pass": stable_ok and success_ok and all_p95_ok,
        },
        {
            "name": f"当前音频 {args.metric_concurrency}并发{args.metric_total_requests}请求成功率",
            "threshold": f">= {args.metric_success_rate:.2f}%",
            "value": round(fixed_8x10.success_rate, 3),
            "pass": success_ok,
        },
        {
            "name": f"当前音频 {args.metric_concurrency}并发{args.metric_total_requests}成功请求P95响应耗时",
            "threshold": f"<= {args.metric_p95_latency:.2f}s",
            "value": None if success_p95_latency is None else round(success_p95_latency, 3),
            "pass": success_p95_ok,
        },
        {
            "name": f"当前音频 {args.metric_concurrency}并发{args.metric_total_requests}全部请求P95响应耗时",
            "threshold": f"<= {args.metric_p95_latency:.2f}s",
            "value": None if all_p95_latency is None else round(all_p95_latency, 3),
            "pass": all_p95_ok,
        },
    ]
    if not args.enable_stream_probe:
        checks = [item for item in checks if "流式 TTFB" not in item["name"]]

    print("\n" + "=" * 92)
    print("【验收指标汇总】")
    print("=" * 92)
    print(
        f"  音频: {audio.path} | {audio.duration_sec:.2f}s | "
        f"{audio.sample_rate}Hz | {audio.channels}ch | {audio.file_kb}KB"
    )
    for item in checks:
        value = "N/A" if item["value"] is None else item["value"]
        print(f"  {_pass_fail(item['pass']):<4} {item['name']:<28} {value!s:<10} {item['threshold']}")
    if not args.enable_stream_probe:
        print("\n  说明: 当前固定使用非流式响应；未执行流式 TTFB 探测。")
    print("=" * 92)

    return {
        "checks": checks,
        "passed": all(item["pass"] for item in checks),
    }


async def run_acceptance(ctx: TestRunContext, args: argparse.Namespace) -> dict[str, Any]:
    """一键执行用户关心的 ASR 验收指标。"""
    stream_probe = None
    non_stream_probe = None
    if not args.skip_metric_probes:
        if args.enable_stream_probe:
            stream_probe = await run_single_probe(ctx, response_stream=True, label="metric_stream_single")
        non_stream_probe = await run_single_probe(ctx, response_stream=False, label="metric_non_stream_single")

    fixed_8x10 = await run_fixed_requests(
        ctx,
        args.metric_concurrency,
        args.metric_total_requests,
        response_stream=args.metric_stream_concurrency,
    )
    summary = print_acceptance_summary(
        audio=ctx.audio,
        stream_probe=stream_probe,
        non_stream_probe=non_stream_probe,
        fixed_8x10=fixed_8x10,
        args=args,
    )
    return {
        "stream_single": _metrics_to_dict(stream_probe) if stream_probe else None,
        "non_stream_single": _metrics_to_dict(non_stream_probe) if non_stream_probe else None,
        "fixed_concurrency": _metrics_to_dict(fixed_8x10),
        "summary": summary,
    }


# ==================== 总汇总（全部跑完后的一页纸结论） ====================


def print_final_summary(
    audio: AudioMeta,
    report: dict[str, Any],
    stop_success_rate: float,
) -> None:
    """从 report 字典里提取各测试关键数字，打印简短对比"""
    print("\n" + "=" * 92)
    print("【汇总】全部测试结论")
    print("=" * 92)
    print(f"  音频: {audio.path} ({audio.duration_sec:.2f}s)")
    print(f"  时间: {report.get('timestamp', '')}")

    baseline = report.get("baseline", {})
    if baseline:
        print("\n  基准 (单请求):")
        for model, m in baseline.items():
            if not isinstance(m, dict):
                continue
            a = m.get("asr", {})
            stress = m.get("stress", {})
            cer = _fmt_pct_opt(a.get("avg_cer") if isinstance(a, dict) else None)
            network_avg = stress.get("avg_network_total_sec", 0) if isinstance(stress, dict) else 0
            completion_avg = stress.get("avg_response_completion_sec", 0) if isinstance(stress, dict) else 0
            print(
                f"    {model}: 完整网络总耗时 {network_avg:.3f}s | "
                f"响应完成时间 {completion_avg:.3f}s | 实时因子 {a.get('avg_rtf', 0):.3f}x | "
                f"字错率 {cer} | 成功率 {m.get('success_rate', 0):.1f}%"
            )

    ramp = report.get("ramp", [])
    if ramp:
        best_stable = None
        best_qps = 0.0
        for lv in ramp:
            sr = lv["success_rate"] if isinstance(lv, dict) else lv.success_rate
            conc = lv["concurrency"] if isinstance(lv, dict) else lv.concurrency
            sqps = lv["success_qps"] if isinstance(lv, dict) else lv.success_qps
            if sr >= stop_success_rate:
                if best_stable is None or conc > (best_stable["concurrency"] if isinstance(best_stable, dict) else best_stable.concurrency):
                    best_stable = lv
                if sqps > best_qps:
                    best_qps = sqps
        if best_stable:
            bc = best_stable["concurrency"] if isinstance(best_stable, dict) else best_stable.concurrency
            print(f"\n  递增并发: 最大稳定并发 ≈ {bc} (成功率≥{stop_success_rate:.0f}%)")
            print(f"  峰值成功 QPS: {best_qps:.3f}")

    for key in ("sustained", "spike", "soak", "fixed_qps", "accuracy_load", "fixed_requests"):
        entry = report.get(key)
        if not entry:
            continue
        if isinstance(entry, dict) and "success_rate" in entry:
            a = entry.get("asr", {})
            cer = _fmt_pct_opt(a.get("avg_cer") if isinstance(a, dict) else getattr(a, "avg_cer", None))
            print(
                f"\n  {key}: 成功率 {entry['success_rate']:.1f}% | "
                f"成功吞吐(QPS) {entry['success_qps']:.3f} | "
                f"完整网络总耗时P95 {entry.get('p95_network_total_sec', 0):.3f}s | "
                f"响应完成时间P95 {entry.get('p95_response_completion_sec', 0):.3f}s | "
                f"成功请求P95 {a.get('p95_latency', 0) if isinstance(a, dict) else 0:.3f}s | 字错率 {cer}"
            )

    acceptance = report.get("acceptance")
    if isinstance(acceptance, dict):
        summary = acceptance.get("summary", {})
        if isinstance(summary, dict):
            print(f"\n  acceptance: {'PASS' if summary.get('passed') else 'FAIL'}")

    print("\n" + "=" * 92)


def _metrics_to_dict(m: StressAggMetrics | dict) -> dict:
    """把 dataclass 转成 dict，方便写入 JSON 报告"""
    return asdict(m) if isinstance(m, StressAggMetrics) else m


# ==================== 程序入口 ====================


async def run(args: argparse.Namespace) -> None:
    """
    主流程：读音频 → 建上下文 → 按 --tests 依次执行各 run_* → 汇总 → 可选写 JSON。
    """
    audio = load_audio_meta(args.audio)
    use_ssl = args.url.lower().startswith("https")
    tests = parse_tests(args.tests)
    reference_text = load_reference_text(args.reference_file, args.reference_text, audio.path)
    reference_source = resolve_reference_source(args.reference_file, args.reference_text, audio.path)

    ctx = TestRunContext(
        url=args.url,
        audio=audio,
        model=args.stress_model or args.models[0],
        timeout_s=args.timeout,
        use_ssl=use_ssl,
        reference_text=reference_text,
        hotwords=args.hotwords,
        gateway=args.gateway,
        component_code=args.component_code,
        app_key=args.app_key,
        secret_key=args.secret_key,
        gateway_function=args.gateway_function or "",
        use_data_uri=args.use_data_uri,
        include_stream_param=args.include_stream_param,
        stream_param_name=args.stream_param_name,
        is_return_timestamp=args.timestamp,
        allow_empty_text=args.allow_empty_text,
    )

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ASR 压力测试全集启动")
    print(f"  目标:     {args.url}")
    estimated_payload_mb = (len(audio.b64_data) + 2048) / (1024 * 1024)
    print(
        f"  音频:     {audio.path} "
        f"({audio.duration_sec:.2f}s / {audio.duration_sec / 60:.2f}分钟, {audio.file_kb}KB)"
    )
    print(f"  请求载荷: Base64 JSON 约 {estimated_payload_mb:.2f}MB")
    if estimated_payload_mb >= 10:
        print("  ⚠️ 请求体超过 10MB，需确认网关/反向代理的请求体大小限制，否则可能快速返回业务错误")
    print(f"  压测模型: {ctx.model}")
    print(f"  调用模式: {'AI Gateway' if ctx.gateway else 'Direct ASR'}")
    print(
        f"  响应模式: 非流式"
        + (f" ({ctx.stream_param_name}=false)" if ctx.include_stream_param else " (未发送 stream 参数)")
    )
    if ctx.gateway:
        print(f"  组件编码: {ctx.component_code} | function: {ctx.gateway_function or ctx.model}")
    print(f"  测试项:   {', '.join(tests)}")
    if reference_text:
        print(f"  参考来源: {reference_source}")
        print(f"  参考文本: {reference_text[:50]}{'…' if len(reference_text) > 50 else ''}")
    if reference_text and not jiwer_cer:
        print("  ⚠️ 未安装 jiwer: pip install jiwer 可获得标准 CER/WER")

    report: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "audio": {
            "path": audio.path,
            "duration_sec": round(audio.duration_sec, 3),
            "sample_rate": audio.sample_rate,
            "channels": audio.channels,
            "codec": audio.codec,
            "media_format": audio.media_format,
            "duration_source": audio.duration_source,
            "file_kb": audio.file_kb,
            "b64_kb": audio.b64_kb,
        },
        "config": {
            "url": args.url,
            "model": ctx.model,
            "tests": tests,
            "reference_file": reference_source,
            "reference_source": reference_source,
            "gateway": ctx.gateway,
            "component_code": ctx.component_code if ctx.gateway else "",
            "gateway_function": (ctx.gateway_function or ctx.model) if ctx.gateway else "",
            "include_stream_param": ctx.include_stream_param,
            "stream_param_name": ctx.stream_param_name,
            "default_response_stream": False,
            "allow_empty_text": ctx.allow_empty_text,
            "estimated_payload_mb": round(estimated_payload_mb, 3),
        },
    }

    # 下面按用户选择的测试项，逐个执行（可只跑其中几项）
    if TestKind.BASELINE.value in tests:
        baseline_raw = await run_baseline(ctx, args.models, args.rounds)
        report["baseline"] = {
            model: {
                "success_rate": m.success_rate,
                "asr": asdict(m.asr),
                "stress": {k: v for k, v in asdict(m).items() if k != "asr"},
            }
            for model, m in baseline_raw.items()
        }

    if TestKind.RAMP.value in tests:
        ramp = await run_ramp(
            ctx, args.concurrency, args.duration, args.stop_success_rate
        )
        report["ramp"] = [_metrics_to_dict(m) for m in ramp]

    if TestKind.SUSTAINED.value in tests:
        m = await run_sustained(ctx, args.sustained_concurrency, args.sustained_duration)
        report["sustained"] = _metrics_to_dict(m)

    if TestKind.SPIKE.value in tests:
        m = await run_spike(
            ctx,
            args.spike_low,
            args.spike_high,
            args.spike_low_sec,
            args.spike_high_sec,
            args.spike_recovery_sec,
        )
        report["spike"] = _metrics_to_dict(m)

    if TestKind.SOAK.value in tests:
        m = await run_soak(
            ctx, args.soak_concurrency, args.soak_duration, args.soak_bucket_sec
        )
        report["soak"] = _metrics_to_dict(m)

    if TestKind.FIXED_QPS.value in tests:
        m = await run_fixed_qps(
            ctx, args.target_qps, args.fixed_qps_duration, args.max_in_flight
        )
        report["fixed_qps"] = _metrics_to_dict(m)

    if TestKind.ACCURACY_LOAD.value in tests:
        m = await run_accuracy_load(
            ctx, args.accuracy_concurrency, args.accuracy_duration
        )
        report["accuracy_load"] = _metrics_to_dict(m)

    if TestKind.FIXED_REQUESTS.value in tests:
        m = await run_fixed_requests(
            ctx,
            args.fixed_requests_concurrency,
            args.fixed_requests_total,
            response_stream=args.fixed_requests_stream,
        )
        report["fixed_requests"] = _metrics_to_dict(m)

    if TestKind.ACCEPTANCE.value in tests:
        report["acceptance"] = await run_acceptance(ctx, args)

    print_final_summary(audio, report, args.stop_success_rate)

    if args.output:
        out_path = os.path.abspath(args.output)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n📄 报告已保存: {out_path}")

    print("\n测试全部完成\n")


def parse_tests(raw: str) -> list[str]:
    """解析 --tests 参数，如 'baseline,ramp' 或 'all'"""
    if raw.strip().lower() in ("all", "*"):
        return list(ALL_TEST_NAMES)
    names = [t.strip().lower() for t in raw.split(",") if t.strip()]
    invalid = [n for n in names if n not in ALL_TEST_NAMES]
    if invalid:
        print(f"❌ 未知测试类型: {invalid}，可选: {', '.join(ALL_TEST_NAMES)}")
        sys.exit(1)
    if not names:
        print(f"❌ --tests 不能为空，可选: {', '.join(ALL_TEST_NAMES)} 或 all")
        sys.exit(1)
    return names


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Fail fast for invalid load-test settings before opening network sessions."""
    if not args.models:
        parser.error("--models 至少需要 1 个模型名")

    if any(value <= 0 for value in args.concurrency):
        parser.error("--concurrency 只能包含正整数")

    positive_int_fields = (
        "duration",
        "rounds",
        "sustained_concurrency",
        "sustained_duration",
        "spike_low",
        "spike_high",
        "spike_low_sec",
        "spike_high_sec",
        "spike_recovery_sec",
        "soak_concurrency",
        "soak_duration",
        "soak_bucket_sec",
        "fixed_qps_duration",
        "max_in_flight",
        "accuracy_concurrency",
        "accuracy_duration",
        "fixed_requests_concurrency",
        "fixed_requests_total",
        "metric_concurrency",
        "metric_total_requests",
        "metric_stable_concurrency",
    )
    for field_name in positive_int_fields:
        if getattr(args, field_name) <= 0:
            parser.error(f"--{field_name.replace('_', '-')} 必须大于 0")

    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    if args.target_qps <= 0:
        parser.error("--target-qps 必须大于 0")
    if not 0 < args.stop_success_rate <= 100:
        parser.error("--stop-success-rate 必须在 0 到 100 之间")
    if not 0 < args.metric_success_rate <= 100:
        parser.error("--metric-success-rate 必须在 0 到 100 之间")
    for field_name in ("metric_stream_ttfb", "metric_non_stream_latency", "metric_rtf", "metric_p95_latency"):
        if getattr(args, field_name) <= 0:
            parser.error(f"--{field_name.replace('_', '-')} 必须大于 0")
    if args.gateway and (not args.app_key or not args.secret_key or not args.component_code):
        parser.error("--gateway 模式必须提供 --app-key、--secret-key、--component-code")


def main() -> None:
    """命令行入口：定义所有参数，最后 asyncio.run(run(args)) 启动异步主流程"""
    parser = argparse.ArgumentParser(
        description="FunASR ASR 压力测试全集 (压力指标 + ASR 专项指标)",
        formatter_class=LoadTestHelpFormatter,
    )
    parser.add_argument("--url", default=DEFAULT_GATEWAY_URL, help="AI Gateway /predict 地址")
    parser.add_argument("--audio", default=DEFAULT_AUDIO)
    parser.add_argument("--models", nargs="+", default=["funasr-iic", "funasr-nano"])
    parser.add_argument("--stress-model", default=None, help="除 baseline 外压测使用的模型")
    parser.add_argument("--hotwords", default=None)
    parser.add_argument(
        "--tests",
        default="all",
        help=f"逗号分隔或 all。可选: {', '.join(ALL_TEST_NAMES)}",
    )
    parser.add_argument(
        "--reference-file",
        default=None,
        help=(
            "参考文本文件 (CER/WER/accuracy_load)；默认读取音频同目录下的 "
            f"{REFERENCE_TEXT_FILENAME}"
        ),
    )
    parser.add_argument("--reference-text", default=None, help="参考文本字符串；如提供则覆盖 --reference-file")
    parser.add_argument("--output", default=None, help="JSON 报告路径")

    # ---------- AI Gateway 统一入口 ----------
    gateway_group = parser.add_mutually_exclusive_group()
    gateway_group.add_argument("--gateway", dest="gateway", action="store_true", default=argparse.SUPPRESS, help="使用 AI Gateway /predict 入口和 HMAC 鉴权")
    gateway_group.add_argument("--direct", dest="gateway", action="store_false", default=argparse.SUPPRESS, help="使用直连 ASR 接口，不加 AI Gateway 鉴权")
    parser.set_defaults(gateway=True)
    parser.add_argument("--app-key", default=DEFAULT_GATEWAY_APP_KEY, help="网关客户编码 / app key")
    parser.add_argument("--secret-key", default=DEFAULT_GATEWAY_SECRET_KEY, help="网关签名密钥")
    parser.add_argument("--component-code", default=DEFAULT_GATEWAY_COMPONENT_CODE, help="网关 componentCode")
    parser.add_argument("--gateway-function", default=None, help="网关 function 字段；不传则跟随当前 model")
    parser.add_argument("--timestamp", action="store_true", help="请求返回时间戳")
    parser.add_argument("--use-data-uri", dest="use_data_uri", action="store_true", default=argparse.SUPPRESS, help="网关模式下 input 使用 data:audio/...;base64,...")
    parser.add_argument("--raw-base64", dest="use_data_uri", action="store_false", default=argparse.SUPPRESS, help="网关模式下 input 仅传 base64")
    parser.set_defaults(use_data_uri=True)
    stream_param_group = parser.add_mutually_exclusive_group()
    stream_param_group.add_argument(
        "--include-stream-param",
        dest="include_stream_param",
        action="store_true",
        default=argparse.SUPPRESS,
        help="payload 中明确写入响应模式参数；仅在后端文档明确支持时使用",
    )
    stream_param_group.add_argument(
        "--omit-stream-param",
        dest="include_stream_param",
        action="store_false",
        default=argparse.SUPPRESS,
        help="不在 payload 中发送 stream 参数；当前 AI Gateway 非流式调用的默认方式",
    )
    parser.set_defaults(include_stream_param=False)
    parser.add_argument("--stream-param-name", default="stream", help="响应流式开关字段名")
    parser.add_argument(
        "--allow-empty-text",
        action="store_true",
        help="允许 HTTP 200 且识别文本为空仍计为成功；默认按 EmptyResult 失败处理",
    )

    # ---------- ramp 递增并发相关参数 ----------
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1,4,8,12])
    parser.add_argument("--duration", type=int, default=30, help="ramp 每级持续秒数")
    parser.add_argument("--rounds", type=int, default=3, help="baseline 每模型轮次")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--stop-success-rate", type=float, default=90.0)

    # ---------- sustained 恒定负载 ----------
    parser.add_argument("--sustained-concurrency", type=int, default=8)
    parser.add_argument("--sustained-duration", type=int, default=60)

    # ---------- spike 突发尖峰 ----------
    parser.add_argument("--spike-low", type=int, default=2)
    parser.add_argument("--spike-high", type=int, default=32)
    parser.add_argument("--spike-low-sec", type=int, default=15)
    parser.add_argument("--spike-high-sec", type=int, default=30)
    parser.add_argument("--spike-recovery-sec", type=int, default=15)

    # ---------- soak 长时间浸泡 ----------
    parser.add_argument("--soak-concurrency", type=int, default=4)
    parser.add_argument("--soak-duration", type=int, default=120)
    parser.add_argument("--soak-bucket-sec", type=int, default=30)

    # ---------- fixed_qps 固定速率 ----------
    parser.add_argument("--target-qps", type=float, default=2.0)
    parser.add_argument("--fixed-qps-duration", type=int, default=60)
    parser.add_argument("--max-in-flight", type=int, default=16)

    # ---------- accuracy_load 并发准确度 ----------
    parser.add_argument("--accuracy-concurrency", type=int, default=8)
    parser.add_argument("--accuracy-duration", type=int, default=45)

    # ---------- fixed_requests 固定请求数 ----------
    parser.add_argument("--fixed-requests-concurrency", type=int, default=8)
    parser.add_argument("--fixed-requests-total", type=int, default=10)
    parser.add_argument("--fixed-requests-stream", action="store_true", help="fixed_requests 场景使用 stream=True")

    # ---------- acceptance 验收指标 ----------
    parser.add_argument("--metric-stream-ttfb", type=float, default=30.0, help="单并发流式 TTFB 阈值，秒")
    parser.add_argument("--enable-stream-probe", action="store_true", help="显式启用验收中的流式单请求探测；默认不启用")
    parser.add_argument("--metric-non-stream-latency", type=float, default=60.0, help="单并发非流式完整响应阈值，秒")
    parser.add_argument("--metric-rtf", type=float, default=1.0, help="单并发 RTF 阈值")
    parser.add_argument("--metric-stable-concurrency", type=int, default=8, help="稳定并发数阈值")
    parser.add_argument("--metric-concurrency", type=int, default=8, help="验收并发请求场景的并发数")
    parser.add_argument("--metric-total-requests", type=int, default=10, help="验收并发请求场景的总请求数")
    parser.add_argument("--metric-success-rate", type=float, default=90.0, help="验收并发请求成功率下限，百分比")
    parser.add_argument("--metric-p95-latency", type=float, default=300.0, help="验收并发请求 P95 响应耗时阈值，秒")
    parser.add_argument("--metric-stream-concurrency", action="store_true", help="验收并发请求场景使用 stream=True")
    parser.add_argument("--skip-metric-probes", action="store_true", help="跳过单请求流式/非流式探测，只跑并发验收")

    args = parser.parse_args()
    validate_args(args, parser)
    asyncio.run(run(args))


# 直接运行本文件时执行 main；被 import 时不会自动跑压测
if __name__ == "__main__":
    main()
