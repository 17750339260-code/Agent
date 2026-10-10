import argparse
import base64
import binascii
import hmac
import json
import os
import queue
import shutil
import struct
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from http.client import IncompleteRead
from typing import Optional
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import requests
import urllib3
from urllib3.exceptions import ProtocolError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _configure_windows_utf8_console() -> None:
    """避免 Windows 终端以 GBK 解码 UTF-8 日志，导致中文设备名乱码。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleCP(65001)
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        # 非交互终端或受限环境可能没有 Windows 控制台 API，忽略即可。
        pass
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


_configure_windows_utf8_console()


def _repair_device_name_encoding(name: str) -> str:
    """修复少数 Windows 音频驱动把 UTF-8 名称按 GBK 解码的显示乱码。"""
    if not isinstance(name, str):
        return str(name)
    try:
        repaired = name.encode("gbk").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name
    return repaired if repaired != name else name

# #省测-环境
# TTS_BINDING = "southgrid"
# TTS_MODEL = "TTS-v1"
# TTS_BINDING_HOST = os.getenv("TTS_BINDING_HOST", "https://10.134.252.232:5030/ai-gateway/predict")
# TTS_BINDING_API_KEY = os.getenv("TTS_BINDING_API_KEY", "b899eef382324e8d8973493fb9c35998")
# TTS_CUSTCODE = os.getenv("TTS_CUSTCODE", "1000400672300031")
# TTS_COMPONENTCODE = os.getenv("TTS_COMPONENTCODE", "04351372")

# # #网级-生产环境
# TTS_BINDING = "southgrid"
# TTS_MODEL = "TTS-v1"
# TTS_BINDING_HOST = os.getenv("TTS_BINDING_HOST", "https://10.10.65.213:18300/ai-inference-gateway/predict")
# TTS_BINDING_API_KEY = os.getenv("TTS_BINDING_API_KEY", "24e74daf74124b0b96c9cb113162a976")
# TTS_CUSTCODE = os.getenv("TTS_CUSTCODE", "1001300033")
# TTS_COMPONENTCODE = os.getenv("TTS_COMPONENTCODE", "04100945")

# 网级-测试环境（新版 OpenAI 兼容 TTS 接口）
TTS_BINDING = "southgrid"
TTS_MODEL = "tts-v1"
TTS_BINDING_HOST = os.getenv("TTS_BINDING_HOST", "https://192.168.0.213:18300/ai-inference-gateway/predict")
TTS_BINDING_API_KEY = os.getenv("TTS_BINDING_API_KEY", "24e74daf74124b0b96c9cb113162a976")
TTS_CUSTCODE = os.getenv("TTS_CUSTCODE", "1001300033")
TTS_COMPONENTCODE = os.getenv("TTS_COMPONENTCODE", "04100945")

DEFAULT_SAMPLE_RATE = 24000
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH = 2
MAX_WAV_PREFIX_BYTES = 64 * 1024
MIN_VALID_SAMPLE_RATE = 8000
MAX_VALID_SAMPLE_RATE = 384000
# RIFF size=0 is not treated as a valid streaming sentinel: a zero-length
# RIFF header is malformed and must fail integrity validation.
RIFF_STREAM_SIZE_SENTINELS = {0x7FFFFFFF, 0xFFFFFFFF}
DATA_STREAM_SIZE_SENTINELS = {0x7FFFFFFF, 0xFFFFFFFF}
PRODUCTION_STREAM_DATA_SIZE = 0x7D000000
PRODUCTION_STREAM_RIFF_SIZE = PRODUCTION_STREAM_DATA_SIZE + 36
SUPPORTED_PCM_SAMPLE_WIDTHS = {1, 2, 3, 4}
DEFAULT_MAX_RESPONSE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_AUDIO_SECONDS = 600.0
DEFAULT_MIN_AUDIO_BYTES = 2
DEFAULT_PLAYBACK_QUEUE_CHUNKS = 256
MAX_PLAYBACK_BUFFER_BYTES = 8 * 1024 * 1024
DEFAULT_PLAYBACK_JOIN_TIMEOUT = 10.0
DEFAULT_ERROR_BODY_BYTES = 4096
DEFAULT_JSON_PREVIEW_CHARS = 4000
MIN_MAX_NEW_TOKENS = 4
MAX_MAX_NEW_TOKENS = 4096
# Qwen TTS 的音频 token 数量与文本长度近似正相关。使用一个略保守的
# 字符/token 比例，为长文本预留足够的生成空间，最终仍由上限保护请求。
AUTO_MAX_NEW_TOKENS_PER_CHAR = 4


def getLocalAuthInfo(customerCode, secretKey):
    """生成南网网关HMAC认证信息。"""
    date_value = datetime.now(timezone.utc).strftime("%a, %d %b %Y %T GMT")
    date_str = f"x-date: {date_value}"
    signature = base64.b64encode(
        hmac.new(secretKey.encode("utf-8"), date_str.encode("utf-8"), sha256).digest()
    ).decode("utf-8")
    authorization = (
        f'hmac username="{customerCode}", algorithm="hmac-sha256", '
        f'headers="x-date", signature="{signature}"'
    )
    return date_value, authorization


def str2bool(value):
    if isinstance(value, bool):
        return value
    normalized = value.lower()
    if normalized in ("yes", "true", "t", "y", "1"):
        return True
    if normalized in ("no", "false", "f", "n", "0"):
        return False
    raise argparse.ArgumentTypeError("Boolean value expected.")


def _format_seconds(value):
    return f"{value:.3f}s" if value is not None and value >= 0 else "N/A"


def _format_ratio(value):
    return f"{value:.3f}" if value is not None and value >= 0 else "N/A"


class WavParseError(ValueError):
    """WAV 头部不完整、非法或当前脚本不支持。"""


class TotalRequestTimeout(TimeoutError):
    """请求超过配置的端到端墙钟时限。"""


class ResponseLimitError(ValueError):
    """响应体大小或音频时长超过单次调用安全上限。"""


class AudioNotReturnedError(ValueError):
    """服务端合成成功，但网关响应中没有可保存的音频字节。"""


@dataclass
class WavFormatInfo:
    declared_riff_size: Optional[int] = None
    audio_format: int = 0
    sample_rate: int = 0
    channels: int = 0
    sample_width: int = 0
    byte_rate: int = 0
    block_align: int = 0
    data_chunk_start: Optional[int] = None
    data_offset: Optional[int] = None
    declared_data_size: Optional[int] = None


class IncrementalWavParser:
    """按 RIFF 块边界增量解析 WAV 头，避免逐字节重复复制和全量扫描。"""

    def __init__(self, max_prefix_bytes: int = MAX_WAV_PREFIX_BYTES):
        self.buffer = bytearray()
        self.max_prefix_bytes = max_prefix_bytes
        self.next_chunk_offset = 12
        self.riff_checked = False
        self.fmt_found = False
        self.info = WavFormatInfo()

    def feed(self, data: bytes) -> None:
        if self.first_frame_ready:
            return
        allowed_bytes = self.max_prefix_bytes + (
            self.info.block_align if self.info.data_offset is not None else 0
        )
        if len(self.buffer) + len(data) > allowed_bytes:
            raise WavParseError(
                f"WAV header exceeds {self.max_prefix_bytes} bytes before the data chunk"
            )
        self.buffer.extend(data)
        if self.info.data_offset is None:
            self._advance()

    def _advance(self) -> None:
        if not self.riff_checked:
            if len(self.buffer) < 12:
                return
            if self.buffer[:4] != b"RIFF" or self.buffer[8:12] != b"WAVE":
                raise WavParseError("response is not a RIFF/WAVE stream")
            self.info.declared_riff_size = int.from_bytes(
                self.buffer[4:8], "little", signed=False
            )
            self.riff_checked = True

        while self.info.data_offset is None:
            if len(self.buffer) < self.next_chunk_offset + 8:
                return

            chunk_start = self.next_chunk_offset
            chunk_id = bytes(self.buffer[chunk_start:chunk_start + 4])
            chunk_size = int.from_bytes(
                self.buffer[chunk_start + 4:chunk_start + 8], "little", signed=False
            )
            chunk_data_offset = chunk_start + 8

            if chunk_id == b"data":
                if not self.fmt_found:
                    raise WavParseError("WAV data chunk appeared before a valid fmt chunk")
                self.info.data_chunk_start = chunk_start
                self.info.data_offset = chunk_data_offset
                self.info.declared_data_size = chunk_size
                return

            next_offset = chunk_data_offset + chunk_size + (chunk_size % 2)
            if next_offset <= chunk_start or next_offset > self.max_prefix_bytes:
                raise WavParseError("invalid or excessively large WAV chunk before data")
            if len(self.buffer) < next_offset:
                return

            if chunk_id == b"fmt ":
                self._parse_fmt(chunk_data_offset, chunk_size)
            self.next_chunk_offset = next_offset

    def _parse_fmt(self, offset: int, size: int) -> None:
        if self.fmt_found:
            raise WavParseError("multiple fmt chunks are not supported")
        if size < 16:
            raise WavParseError(f"WAV fmt chunk is too short: {size} bytes")

        audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample = (
            struct.unpack_from("<HHIIHH", self.buffer, offset)
        )
        if audio_format != 1:
            raise WavParseError(f"unsupported WAV audio format: {audio_format}; PCM(1) required")
        if not 1 <= channels <= 8:
            raise WavParseError(f"invalid WAV channels: {channels}")
        if not MIN_VALID_SAMPLE_RATE <= sample_rate <= MAX_VALID_SAMPLE_RATE:
            raise WavParseError(f"invalid WAV sample rate: {sample_rate}")
        if bits_per_sample % 8 != 0 or bits_per_sample // 8 not in SUPPORTED_PCM_SAMPLE_WIDTHS:
            raise WavParseError(f"unsupported PCM bits per sample: {bits_per_sample}")

        sample_width = bits_per_sample // 8
        expected_block_align = channels * sample_width
        expected_byte_rate = sample_rate * expected_block_align
        if block_align != expected_block_align:
            raise WavParseError(
                f"invalid WAV block align: {block_align}, expected {expected_block_align}"
            )
        if byte_rate != expected_byte_rate:
            raise WavParseError(
                f"invalid WAV byte rate: {byte_rate}, expected {expected_byte_rate}"
            )

        self.info = WavFormatInfo(
            declared_riff_size=self.info.declared_riff_size,
            audio_format=audio_format,
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
            byte_rate=byte_rate,
            block_align=block_align,
        )
        self.fmt_found = True

    def bytes_needed_for_progress(self, normal_chunk_size: int) -> int:
        if self.info.data_offset is not None:
            first_frame_end = self.info.data_offset + self.info.block_align
            return max(first_frame_end - len(self.buffer), 0)
        if len(self.buffer) < 12:
            return 12 - len(self.buffer)
        if len(self.buffer) < self.next_chunk_offset + 8:
            return self.next_chunk_offset + 8 - len(self.buffer)

        chunk_start = self.next_chunk_offset
        chunk_id = bytes(self.buffer[chunk_start:chunk_start + 4])
        if chunk_id == b"data":
            self._advance()
            return self.bytes_needed_for_progress(normal_chunk_size)

        chunk_size = int.from_bytes(
            self.buffer[chunk_start + 4:chunk_start + 8], "little", signed=False
        )
        next_offset = chunk_start + 8 + chunk_size + (chunk_size % 2)
        if next_offset > self.max_prefix_bytes:
            raise WavParseError("invalid or excessively large WAV chunk before data")
        return max(1, min(next_offset - len(self.buffer), normal_chunk_size))

    @property
    def first_frame_ready(self) -> bool:
        return (
            self.info.data_offset is not None
            and len(self.buffer) >= self.info.data_offset + self.info.block_align
        )


def _is_streaming_data_size_placeholder(info: WavFormatInfo, total_bytes: int) -> bool:
    """判断 data 长度是否为流式占位值，而不是合法的空 data 块。"""
    declared_data_size = info.declared_data_size
    if _is_production_stream_size_pair(info):
        return True
    if declared_data_size in DATA_STREAM_SIZE_SENTINELS:
        return True
    if declared_data_size != 0:
        return False

    declared_riff_size = info.declared_riff_size
    return (
        declared_riff_size in RIFF_STREAM_SIZE_SENTINELS
        or (
            declared_riff_size is not None
            and total_bytes != declared_riff_size + 8
        )
    )


def _is_production_stream_size_pair(info: WavFormatInfo) -> bool:
    """识别生产服务固定使用的 RIFF/data 流式占位长度组合。"""
    return (
        info.declared_riff_size == PRODUCTION_STREAM_RIFF_SIZE
        and info.declared_data_size == PRODUCTION_STREAM_DATA_SIZE
    )


def _is_streaming_riff_size_placeholder(info: WavFormatInfo) -> bool:
    return (
        info.declared_riff_size in RIFF_STREAM_SIZE_SENTINELS
        or _is_production_stream_size_pair(info)
    )


def _audio_data_limit_while_streaming(
    info: WavFormatInfo, received_total_bytes: int
) -> Optional[int]:
    """返回当前可确认的 data 块长度；None 表示流式未知长度。"""
    declared_data_size = info.declared_data_size
    if _is_production_stream_size_pair(info):
        return None
    if declared_data_size in DATA_STREAM_SIZE_SENTINELS:
        return None
    if declared_data_size is None:
        return 0
    if declared_data_size > 0:
        return declared_data_size

    declared_riff_size = info.declared_riff_size
    if declared_riff_size in RIFF_STREAM_SIZE_SENTINELS:
        return None
    if (
        declared_riff_size is not None
        and received_total_bytes > declared_riff_size + 8
    ):
        return None
    return 0


def _audio_bytes_from_chunk(
    chunk: bytes,
    chunk_start_offset: int,
    received_total_bytes: int,
    info: WavFormatInfo,
) -> bytes:
    """只截取当前响应块中真正属于 WAV data 块的 PCM 字节。"""
    if info.data_offset is None:
        return b""

    audio_start = max(chunk_start_offset, info.data_offset)
    audio_end = received_total_bytes
    declared_limit = _audio_data_limit_while_streaming(info, received_total_bytes)
    if declared_limit is not None:
        audio_end = min(audio_end, info.data_offset + declared_limit)
    if audio_end <= audio_start:
        return b""

    relative_start = audio_start - chunk_start_offset
    relative_end = audio_end - chunk_start_offset
    return chunk[relative_start:relative_end]


def _populate_derived_metrics(metrics: dict, text: str) -> None:
    """集中计算派生指标，保证打印结果和程序化消费使用同一套公式。"""
    rt = metrics.get("rt")
    response_header_time = metrics.get("response_header_time")
    ttft = metrics.get("ttft")
    audio_duration = metrics.get("audio_duration", 0.0)
    valid_sample = (
        metrics.get("success", False)
        and metrics.get("complete", False)
        and isinstance(rt, (int, float))
        and rt > 0
        and isinstance(audio_duration, (int, float))
        and audio_duration > 0
    )

    metrics["response_body_time"] = (
        max(rt - response_header_time, 0.0)
        if isinstance(rt, (int, float))
        and isinstance(response_header_time, (int, float))
        else None
    )
    ttfb = metrics.get("ttfb")
    metrics["ttfb_after_headers"] = (
        max(ttfb - response_header_time, 0.0)
        if isinstance(ttfb, (int, float))
        and isinstance(response_header_time, (int, float))
        else None
    )
    metrics["ttft_after_headers"] = (
        max(ttft - response_header_time, 0.0)
        if isinstance(ttft, (int, float))
        and isinstance(response_header_time, (int, float))
        else None
    )
    first_audio_byte_time = metrics.get("first_audio_byte_time")
    metrics["first_audio_after_headers"] = (
        max(first_audio_byte_time - response_header_time, 0.0)
        if isinstance(first_audio_byte_time, (int, float))
        and isinstance(response_header_time, (int, float))
        else None
    )
    synthesis_response_time = metrics.get("synthesis_response_time")
    audio_download_header_time = metrics.get("audio_download_header_time")
    metrics["gateway_response_body_time"] = (
        max(synthesis_response_time - response_header_time, 0.0)
        if isinstance(synthesis_response_time, (int, float))
        and isinstance(response_header_time, (int, float))
        else None
    )
    metrics["audio_download_header_latency"] = (
        max(audio_download_header_time - synthesis_response_time, 0.0)
        if isinstance(audio_download_header_time, (int, float))
        and isinstance(synthesis_response_time, (int, float))
        else None
    )
    metrics["audio_download_body_time"] = (
        max(rt - audio_download_header_time, 0.0)
        if isinstance(rt, (int, float))
        and isinstance(audio_download_header_time, (int, float))
        else None
    )
    metrics["first_audio_after_download_headers"] = (
        max(first_audio_byte_time - audio_download_header_time, 0.0)
        if isinstance(first_audio_byte_time, (int, float))
        and isinstance(audio_download_header_time, (int, float))
        else None
    )
    metrics["ttft_after_download_headers"] = (
        max(ttft - audio_download_header_time, 0.0)
        if isinstance(ttft, (int, float))
        and isinstance(audio_download_header_time, (int, float))
        else None
    )
    metrics["rtf"] = rt / audio_duration if valid_sample else None
    metrics["synthesis_speed"] = audio_duration / rt if valid_sample else None

    last_audio_byte_time = metrics.get("last_audio_byte_time")
    audio_receive_time = (
        max(last_audio_byte_time - first_audio_byte_time, 0.0)
        if isinstance(last_audio_byte_time, (int, float))
        and isinstance(first_audio_byte_time, (int, float))
        else None
    )
    metrics["audio_receive_time"] = audio_receive_time
    metrics["audio_receive_rtf"] = (
        audio_receive_time / audio_duration
        if valid_sample
        and isinstance(audio_receive_time, (int, float))
        and audio_receive_time > 0
        else None
    )
    metrics["audio_receive_speed"] = (
        audio_duration / audio_receive_time
        if valid_sample
        and isinstance(audio_receive_time, (int, float))
        and audio_receive_time > 0
        else None
    )

    library_header_time = metrics.get("requests_response_header_time")
    metrics["response_header_timer_delta"] = (
        abs(response_header_time - library_header_time)
        if isinstance(response_header_time, (int, float))
        and isinstance(library_header_time, (int, float))
        else None
    )

    timing_diagnosis = ""
    if metrics.get("requested_stream") and isinstance(response_header_time, (int, float)):
        first_body_after_headers = metrics.get("ttfb_after_headers")
        if (
            response_header_time >= 1.0
            and isinstance(first_body_after_headers, (int, float))
            and first_body_after_headers <= 0.5
        ):
            timer_note = ""
            timer_delta = metrics.get("response_header_timer_delta")
            if (
                isinstance(timer_delta, (int, float))
                and timer_delta <= max(0.050, response_header_time * 0.02)
            ):
                timer_note = "，且 perf_counter 与 requests 内部计时一致"
            timing_diagnosis = (
                "响应头到达较晚，但首个 PCM 在响应头后很快到达"
                f"{timer_note}；优先排查连接/TLS、网关排队、上游首包生成或代理在发响应头前缓冲，"
                "而不是客户端读取响应体的计时公式。"
            )
    metrics["timing_diagnosis"] = timing_diagnosis

    effective_text_chars = sum(1 for char in (text or "") if not char.isspace())
    metrics["text_chars"] = effective_text_chars
    metrics["chars_per_second"] = effective_text_chars / rt if valid_sample else None


def _get_playback_failure(
    player_thread: Optional[threading.Thread],
    stop_event: threading.Event,
    playback_state: dict,
    state_lock: threading.Lock,
) -> str:
    """读取播放线程故障；正常存活或尚未启动时返回空字符串。"""
    if player_thread is None:
        return ""
    with state_lock:
        error = playback_state.get("error", "")
    if error:
        return error
    if stop_event.is_set():
        return "playback stopped unexpectedly"
    if not player_thread.is_alive():
        return "playback thread exited unexpectedly"
    return ""


def fix_wav_header(filepath: str) -> bool:
    """修复RIFF和data块大小；兼容data块不在固定40偏移的WAV。"""
    try:
        file_size = os.path.getsize(filepath)
        if file_size < 44:
            return False
        if file_size - 8 > 0xFFFFFFFF:
            raise ValueError("WAV file exceeds RIFF 32-bit size limit; RF64 is required")

        with open(filepath, "r+b") as f:
            prefix = f.read(MAX_WAV_PREFIX_BYTES)
            parser = IncrementalWavParser()
            parser.feed(prefix)
            info = parser.info
            if info.data_chunk_start is None or info.data_offset is None:
                raise WavParseError("WAV data chunk was not found; header not modified")

            riff_size = max(file_size - 8, 0)
            available_data_size = max(file_size - info.data_offset, 0)
            declared_data_size = info.declared_data_size
            data_size_placeholder = _is_streaming_data_size_placeholder(info, file_size)
            data_size = (
                available_data_size
                if data_size_placeholder
                else min(declared_data_size or 0, available_data_size)
            )
            f.seek(4)
            f.write(struct.pack("<I", riff_size))
            f.seek(info.data_chunk_start + 4)
            f.write(struct.pack("<I", data_size))
        print(f"WAV header fixed: data size = {data_size} bytes")
        return True
    except Exception as exc:
        print(f"Warning: failed to fix WAV header: {exc}")
        return False


def write_pcm_wav_header(file_obj, sample_rate: int, channels: int, sample_width: int) -> None:
    """写入可在流式接收期间持续追加 PCM 的 WAV 占位头。"""
    block_align = channels * sample_width
    byte_rate = sample_rate * block_align
    file_obj.write(
        struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            0x7FFFFFFF,
            b"WAVE",
            b"fmt ",
            16,
            1,
            channels,
            sample_rate,
            byte_rate,
            block_align,
            sample_width * 8,
            b"data",
            0x7FFFFFFF,
        )
    )


def play_thread_func(
    audio_queue: queue.Queue,
    sample_rate: int,
    channels: int,
    sample_width: int,
    stop_event: threading.Event,
    playback_state: dict,
    state_lock: threading.Lock,
) -> None:
    """后台播放线程，从队列中取PCM音频数据并播放。"""
    p = None
    stream = None
    try:
        import pyaudio

        sample_format = {
            1: pyaudio.paInt8,
            2: pyaudio.paInt16,
            3: pyaudio.paInt24,
            4: pyaudio.paInt32,
        }.get(sample_width, pyaudio.paInt16)

        p = pyaudio.PyAudio()
        output_device = p.get_default_output_device_info()
        output_device_index = int(output_device["index"])
        output_device_name = _repair_device_name_encoding(
            str(output_device.get("name", ""))
        )

        # Bluetooth 的 Hands-Free 配置经常不接受 24kHz TTS PCM，表现为滋滋声。
        # 如果存在同名 Stereo 输出，优先使用 Stereo；这只影响本地播放设备。
        if "hands-free" in output_device_name.lower() or "handsfree" in output_device_name.lower():
            default_prefix = output_device_name.lower().split("hands")[0].strip()
            for device_index in range(p.get_device_count()):
                candidate = p.get_device_info_by_index(device_index)
                candidate_name = _repair_device_name_encoding(
                    str(candidate.get("name", ""))
                )
                if (
                    candidate.get("maxOutputChannels", 0) > 0
                    and "stereo" in candidate_name.lower()
                    and default_prefix
                    and default_prefix in candidate_name.lower()
                ):
                    output_device_index = device_index
                    output_device_name = candidate_name
                    break

        stream = p.open(
            format=sample_format,
            channels=channels,
            rate=sample_rate,
            output=True,
            output_device_index=output_device_index,
            frames_per_buffer=1920,
        )
        print(
            f"playback device: {output_device_name} "
            f"({sample_rate}Hz, {channels}ch, {sample_width * 8}bit)",
            flush=True,
        )
    except Exception as exc:
        with state_lock:
            playback_state["error"] = f"playback unavailable: {exc}"
        stop_event.set()

    try:
        playback_started = False
        pending = bytearray()
        while stream is not None and not stop_event.is_set():
            try:
                data = audio_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if data is None:
                if pending:
                    # 网络分块边界不一定落在 PCM 帧边界；播放端补齐最后一帧，
                    # 避免声卡因 1 个尾部字节产生爆音。完整性仍由 WAV 校验负责。
                    frame_size = channels * sample_width
                    padding = (-len(pending)) % frame_size
                    if padding:
                        pending.extend(b"\x00" * padding)
                    pending_data = bytes(pending)
                    if pending_data:
                        stream.write(pending_data, exception_on_underflow=False)
                break
            pending.extend(data)
            aligned_size = len(pending) - (len(pending) % (channels * sample_width))
            if aligned_size <= 0:
                continue
            aligned_data = bytes(pending[:aligned_size])
            del pending[:aligned_size]
            write_started_at = time.perf_counter()
            stream.write(aligned_data, exception_on_underflow=False)
            if not playback_started:
                playback_started = True
                with state_lock:
                    # write 成功后才提交指标，但时间点取真正调用设备写入之前。
                    playback_state["start_perf"] = write_started_at
    except Exception as exc:
        with state_lock:
            playback_state["error"] = f"playback failed: {exc}"
        stop_event.set()
    finally:
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass


def build_headers(request_id: Optional[str] = None):
    x_date, authorization = getLocalAuthInfo(TTS_CUSTCODE, TTS_BINDING_API_KEY)
    headers = {
        "x-date": x_date,
        "authorization": authorization,
        "Content-Type": "application/json",
        "Accept": "application/octet-stream, audio/wav",
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache",
    }
    if request_id:
        headers["X-Request-ID"] = request_id
    return headers


def calculate_max_new_tokens(text: str) -> int:
    """根据文本长度计算 ``max_new_tokens``，并限制在 4–4096。"""
    effective_chars = sum(1 for char in (text or "") if not char.isspace())
    estimated = effective_chars * AUTO_MAX_NEW_TOKENS_PER_CHAR
    return max(MIN_MAX_NEW_TOKENS, min(MAX_MAX_NEW_TOKENS, estimated))


def build_payload(args):
    """构造新版 /v1/audio/speech 接口所需的扁平请求体。

    ``voice``/``instructions`` 同时兼容旧版命令行参数名，方便已有脚本
    继续调用本模块；旧网关专用字段不再发送给新版服务。
    """
    voice = (
        getattr(args, "voice", None)
        or getattr(args, "zero_shot_spk_id", None)
        or "serena"
    )
    instructions = getattr(args, "instructions", None)
    if instructions is None:
        instructions = getattr(args, "instruct_text", "")
    stream = bool(getattr(args, "stream", False))
    # componentCode 是南网 AI 网关的路由/鉴权元数据，必须放在 JSON
    # 顶层请求体中；仅定义 TTS_COMPONENTCODE 不会自动发送给服务端。
    component_code = getattr(args, "component_code", None) or TTS_COMPONENTCODE
    if not isinstance(component_code, str) or not component_code.strip():
        raise ValueError(
            "componentCode 不能为空，请设置 --component-code 或 TTS_COMPONENTCODE"
        )

    payload = {
        "componentCode": component_code.strip(),
        "input": args.text,
        "voice": voice,
        "model": getattr(args, "model", TTS_MODEL),
        "instructions": instructions,
        "task_type": getattr(args, "task_type", "CustomVoice"),
        "language": getattr(args, "language", "Chinese"),
        # 流式播放必须请求裸 PCM；WAV 头只适合完整文件，不能把每个网络块
        # 当作独立音频帧解析，否则播放端会把 WAV 元数据/分块边界播成杂音。
        "response_format": "pcm" if stream else getattr(args, "response_format", "wav"),
        "stream": stream,
        "speed": getattr(args, "speed", 1.0),
        "sample_rate": getattr(args, "sample_rate", 24000),
        "res_content": bool(getattr(args, "res_content", True)),
    }
    if stream:
        payload["stream_format"] = getattr(args, "stream_format", "audio")
    # seed 可选；未指定 max_new_tokens 时按输入文本长度本地计算，确保
    # 新版接口始终收到一个合法的 4–4096 范围值。
    seed = getattr(args, "seed", 42)
    if seed is not None:
        payload["seed"] = seed
    max_new_tokens = getattr(args, "max_new_tokens", None)
    payload["max_new_tokens"] = (
        max_new_tokens
        if max_new_tokens is not None
        else calculate_max_new_tokens(args.text)
    )
    return payload


def _read_response_chunk(response, size):
    """从 urllib3 原始响应读取已解码字节，并保持 requests 的异常语义。"""
    try:
        return response.raw.read(size, decode_content=True)
    except urllib3.exceptions.ReadTimeoutError as exc:
        raise requests.exceptions.ReadTimeout(str(exc)) from exc
    except urllib3.exceptions.ProtocolError as exc:
        raise requests.exceptions.ConnectionError(str(exc)) from exc


def _read_available_response_chunk(response, size):
    """读取当前可用的流数据，不为凑满固定长度而额外等待。

    urllib3 的 ``read1`` 更接近一次底层可用数据读取；它仍不是服务端 HTTP
    chunk 边界，因此这里只把结果称为“客户端可用字节读取”。旧版 urllib3
    没有 ``read1`` 时再退回 ``read``。
    """
    try:
        read1 = getattr(response.raw, "read1", None)
        if callable(read1):
            return read1(size, decode_content=True)
        return response.raw.read(size, decode_content=True)
    except urllib3.exceptions.ReadTimeoutError as exc:
        raise requests.exceptions.ReadTimeout(str(exc)) from exc
    except urllib3.exceptions.ProtocolError as exc:
        raise requests.exceptions.ConnectionError(str(exc)) from exc


def _record_stream_read_timing(
    metrics: dict,
    *,
    read_index: int,
    chunk_size: int,
    received_bytes_before: int,
    byte_rate: int,
    read_started_at: float,
    read_finished_at: float,
    previous_read_finished_at: Optional[float],
) -> None:
    """把网络阻塞等待与两次读取之间的本地处理时间分开记录。"""
    read_wait = max(0.0, read_finished_at - read_started_at)
    detail = {
        "read_index": read_index,
        "chunk_size": chunk_size,
        "byte_offset": received_bytes_before,
        "audio_offset_seconds": (
            received_bytes_before / byte_rate if byte_rate > 0 else 0.0
        ),
    }
    metrics["stream_read_count"] = metrics.get("stream_read_count", 0) + 1
    # 保留旧字段供已有调用方读取，但明确它现在统计真实读取次数，不是 HTTP chunk 数。
    metrics["stream_chunk_count"] = metrics["stream_read_count"]
    if (
        metrics.get("stream_max_read_wait") is None
        or read_wait > metrics["stream_max_read_wait"]
    ):
        metrics["stream_max_read_wait"] = read_wait
        metrics["stream_max_read_wait_detail"] = detail

    if previous_read_finished_at is None:
        return

    local_processing = max(0.0, read_started_at - previous_read_finished_at)
    completion_gap = max(0.0, read_finished_at - previous_read_finished_at)
    if (
        metrics.get("stream_max_local_processing_time") is None
        or local_processing > metrics["stream_max_local_processing_time"]
    ):
        metrics["stream_max_local_processing_time"] = local_processing
        metrics["stream_max_local_processing_detail"] = detail
    if (
        metrics.get("stream_max_completion_gap") is None
        or completion_gap > metrics["stream_max_completion_gap"]
    ):
        metrics["stream_max_completion_gap"] = completion_gap
        # 兼容旧报告字段；该值是读取完成间隔，并非服务端分块间隔。
        metrics["stream_max_chunk_gap"] = completion_gap


def _classify_body_prefix(prefix: bytes) -> Optional[str]:
    """Content-Type 缺失时，根据响应体首个有效字节区分 JSON 与 WAV。"""
    candidate = prefix.lstrip(b" \t\r\n")
    if candidate.startswith(b"\xef\xbb\xbf"):
        candidate = candidate[3:].lstrip(b" \t\r\n")
    elif candidate.startswith((b"\xef", b"\xef\xbb")):
        return None
    if not candidate:
        return None
    return "json" if candidate[:1] in (b"{", b"[") else "wav_stream"


def _set_metric_error(
    metrics: dict,
    exc: Exception,
    category: str,
    start_time: Optional[float] = None,
) -> None:
    metrics["success"] = False
    metrics["complete"] = False
    metrics["error_type"] = category
    metrics["error"] = f"{type(exc).__name__}: {exc}"
    if metrics.get("rt") is None and start_time is not None:
        metrics["rt"] = max(time.perf_counter() - start_time, 0.0)


def _read_error_body(response, limit: int = DEFAULT_ERROR_BODY_BYTES) -> str:
    try:
        body = _read_response_chunk(response, limit + 1)
        truncated = len(body) > limit
        body = body[:limit]
        text = body.decode(getattr(response, "encoding", None) or "utf-8", errors="replace")
        return text + ("...<truncated>" if truncated else "")
    except Exception as exc:
        return f"<failed to read error body: {exc}>"


def _decode_json_audio_candidate(value) -> Optional[bytes]:
    """从常见 JSON 字段中提取 Base64/WAV 音频，只接受 RIFF/WAVE 数据。"""
    if isinstance(value, (bytes, bytearray)):
        candidate = bytes(value)
        return candidate if candidate.startswith(b"RIFF") and candidate[8:12] == b"WAVE" else None
    if isinstance(value, list) and all(isinstance(item, int) and 0 <= item <= 255 for item in value):
        candidate = bytes(value)
        return candidate if candidate.startswith(b"RIFF") and candidate[8:12] == b"WAVE" else None
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("data:") and "," in text:
            text = text.split(",", 1)[1]
        text = "".join(text.split())
        if len(text) < 16:
            return None
        try:
            padding = "=" * (-len(text) % 4)
            candidate = base64.b64decode(text + padding, validate=True)
        except (ValueError, binascii.Error):
            try:
                candidate = base64.urlsafe_b64decode(text + padding)
            except (ValueError, binascii.Error):
                return None
        return candidate if candidate.startswith(b"RIFF") and candidate[8:12] == b"WAVE" else None
    if isinstance(value, dict):
        preferred_keys = (
            "audio",
            "audio_data",
            "audio_base64",
            "wav",
            "wav_base64",
            "base64",
            "data",
            "result",
            "output",
        )
        for key in preferred_keys:
            if key in value:
                candidate = _decode_json_audio_candidate(value[key])
                if candidate is not None:
                    return candidate
        for nested in value.values():
            candidate = _decode_json_audio_candidate(nested)
            if candidate is not None:
                return candidate
    return None


def _parse_json_response(body: bytes, encoding: Optional[str]):
    """解析 JSON 响应并返回结构化数据与可读 JSON 文本。"""
    text = body.decode(encoding or "utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WavParseError(f"JSON response is invalid: {exc}") from exc
    return payload, text


def _extract_json_string(payload, keys: tuple[str, ...]) -> str:
    """递归提取生产 JSON 中的字符串字段。"""
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for nested in payload.values():
            value = _extract_json_string(nested, keys)
            if value:
                return value
    elif isinstance(payload, list):
        for nested in payload:
            value = _extract_json_string(nested, keys)
            if value:
                return value
    return ""


def _extract_json_audio_url(payload) -> str:
    """提取生产网关使用的 audio_url 字段，兼容常见大小写命名。"""
    return _extract_json_string(payload, ("audio_url", "audioUrl", "audioURL"))


def _resolve_audio_download_url(request_url: str, audio_url: str, audio_path: str) -> str:
    """将网关返回的绝对/相对音频引用解析为可下载的 HTTP(S) URL。"""
    candidates = _resolve_audio_download_urls(request_url, audio_url, audio_path)
    return candidates[0] if candidates else ""


def _resolve_audio_download_urls(
    request_url: str, audio_url: str, audio_path: str
) -> list[str]:
    """生成标准 URL 与保留网关路径前缀的候选下载地址。"""
    reference = (audio_url or audio_path or "").strip()
    if not reference:
        return []

    parsed = urlparse(reference)
    if parsed.scheme:
        if parsed.scheme.lower() not in ("http", "https"):
            raise AudioNotReturnedError(
                f"unsupported audio reference scheme: {parsed.scheme}"
            )
        return [reference]

    candidates = [urljoin(request_url, reference)]
    request_parts = urlparse(request_url)
    gateway_prefix = request_parts.path.rsplit("/", 1)[0].rstrip("/")
    if reference.startswith("/") and gateway_prefix:
        prefixed_path = f"{gateway_prefix}/{reference.lstrip('/')}"
        prefixed_url = request_parts._replace(
            path=prefixed_path,
            params="",
            query="",
            fragment="",
        ).geturl()
        if prefixed_url not in candidates:
            candidates.append(prefixed_url)
    return candidates


def _build_audio_download_headers(request_url: str, download_url: str) -> dict:
    """同源网关下载沿用 HMAC；跨源签名 URL 不附加网关 Authorization。"""
    request_origin = urlparse(request_url)
    download_origin = urlparse(download_url)
    if (
        request_origin.scheme.lower() == download_origin.scheme.lower()
        and request_origin.netloc.lower() == download_origin.netloc.lower()
    ):
        headers = build_headers()
        headers.pop("Content-Type", None)
        return headers
    return {
        "Accept": "application/octet-stream, audio/wav",
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache",
    }


def _extract_json_number(payload, keys: tuple[str, ...]) -> Optional[float]:
    """递归提取生产 JSON 中的 duration、sample_rate 等数值元数据。"""
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        for nested in payload.values():
            value = _extract_json_number(nested, keys)
            if value is not None:
                return value
    elif isinstance(payload, list):
        for nested in payload:
            value = _extract_json_number(nested, keys)
            if value is not None:
                return value
    return None


def _extract_json_success(payload) -> Optional[bool]:
    """提取生产 JSON 中的 success 状态。"""
    if isinstance(payload, dict):
        success = payload.get("success")
        if isinstance(success, bool):
            return success
        for nested in payload.values():
            success = _extract_json_success(nested)
            if success is not None:
                return success
    elif isinstance(payload, list):
        for nested in payload:
            success = _extract_json_success(nested)
            if success is not None:
                return success
    return None


def _extract_json_audio(payload, body_preview: str = "") -> bytes:
    """从已解析 JSON 中提取 Base64/WAV 音频。"""
    audio_bytes = _decode_json_audio_candidate(payload)
    if audio_bytes is None:
        shape = body_preview[:DEFAULT_JSON_PREVIEW_CHARS] or repr(payload)[:DEFAULT_JSON_PREVIEW_CHARS]
        raise WavParseError(
            "JSON response does not contain a Base64/WAV audio field; "
            f"body={shape}"
        )
    return audio_bytes


def _finalize_wav_metrics(metrics: dict, wav_parser: IncrementalWavParser, total_bytes: int, args) -> None:
    """校验完整 WAV，并根据 PCM data 块计算时长。"""
    if not wav_parser.first_frame_ready or wav_parser.info.data_offset is None:
        raise WavParseError("response completed without a full PCM sample frame")

    wav_info = wav_parser.info
    actual_data_size = max(total_bytes - wav_info.data_offset, 0)
    declared_data_size = wav_info.declared_data_size
    declared_riff_size = wav_info.declared_riff_size
    if (
        not _is_streaming_riff_size_placeholder(wav_info)
        and declared_riff_size is not None
        and total_bytes != declared_riff_size + 8
    ):
        raise WavParseError(
            "RIFF size does not match response size: "
            f"declared={declared_riff_size + 8} bytes, actual={total_bytes} bytes"
        )
    data_size_placeholder = _is_streaming_data_size_placeholder(wav_info, total_bytes)
    if not data_size_placeholder and (
        declared_data_size is None or actual_data_size < declared_data_size
    ):
        raise WavParseError(
            "WAV audio data is truncated: "
            f"declared={declared_data_size} bytes, actual={actual_data_size} bytes"
        )

    audio_data_bytes = actual_data_size if data_size_placeholder else declared_data_size
    if audio_data_bytes < args.min_audio_bytes:
        raise WavParseError(f"audio payload is too small: {audio_data_bytes} bytes")
    if audio_data_bytes % wav_info.block_align != 0:
        raise WavParseError(
            "audio payload is not aligned to PCM sample frames: "
            f"bytes={audio_data_bytes}, block_align={wav_info.block_align}"
        )

    audio_duration = audio_data_bytes / wav_info.byte_rate
    if audio_duration > args.max_audio_seconds:
        raise ResponseLimitError(
            f"audio duration exceeds limit: {audio_duration:.3f}s"
        )

    metrics["audio_data_bytes"] = audio_data_bytes
    metrics["audio_duration"] = audio_duration
    if metrics.get("response_mode") == "json_base64":
        metrics["audio_duration_source"] = "decoded Base64 WAV data size / WAV byte rate"
    else:
        metrics["audio_duration_source"] = "stream data size / WAV byte rate"
    metrics["sample_rate"] = wav_info.sample_rate
    metrics["channels"] = wav_info.channels
    metrics["sample_width"] = wav_info.sample_width
    metrics["success"] = True
    metrics["service_success"] = True
    metrics["audio_artifact_available"] = True


def _parse_wav_bytes_prefix(audio_bytes: bytes, chunk_size: int) -> IncrementalWavParser:
    """按解析器所需的最小字节数解析内存中的 WAV，避免复制完整音频。"""
    parser = IncrementalWavParser()
    offset = 0
    while offset < len(audio_bytes) and not parser.first_frame_ready:
        read_size = parser.bytes_needed_for_progress(chunk_size)
        if read_size <= 0:
            break
        chunk = audio_bytes[offset:offset + read_size]
        if not chunk:
            break
        parser.feed(chunk)
        offset += len(chunk)
    return parser


def request_pcm_stream_tts(args) -> dict:
    """按参考客户端方式请求裸 PCM 流，并同时播放和保存为 WAV。"""
    payload = build_payload(args)
    metrics = {
        "success": False,
        "complete": False,
        "service_success": False,
        "audio_artifact_available": False,
        "status_code": None,
        "content_type": "",
        "response_header_time": None,
        "requests_response_header_time": None,
        "response_header_timer_delta": None,
        "transfer_encoding": "",
        "content_length": "",
        "response_x_accel_buffering": "",
        "ttfb": None,
        "first_audio_byte_time": None,
        "last_audio_byte_time": None,
        "ttft": None,
        "ttfa": None,
        "rt": None,
        "client_total_time": None,
        "response_body_time": None,
        "ttfb_after_headers": None,
        "ttft_after_headers": None,
        "rtf": None,
        "synthesis_speed": None,
        "text_chars": 0,
        "chars_per_second": None,
        "size": 0,
        "path": args.output,
        "audio_duration": 0.0,
        "audio_duration_source": "N/A",
        "audio_data_bytes": 0,
        "sample_rate": args.sample_rate,
        "channels": DEFAULT_CHANNELS,
        "sample_width": DEFAULT_SAMPLE_WIDTH,
        "header_fixed": False,
        "error_type": "",
        "error": "",
        "warning": "",
        "playback_error": "",
        "response_body_bytes": 0,
        "response_mode": "unknown",
        "latency_metrics_note": "",
        "audio_url": "",
        "audio_path": "",
        "client_request_id": "",
        "service_request_id": "",
        "worker_id": "",
        "worker_process_id": "",
        "requested_stream": True,
        "timing_diagnosis": "",
        "stream_chunk_count": 0,
        "stream_read_count": 0,
        "stream_first_chunk_time": None,
        "stream_last_chunk_time": None,
        "stream_max_chunk_gap": None,
        "stream_max_read_wait": None,
        "stream_max_local_processing_time": None,
        "stream_max_completion_gap": None,
        "stream_max_read_wait_detail": None,
        "stream_max_local_processing_detail": None,
        "stream_uniformity_status": "pending",
        "stream_uniformity_reason": "",
    }
    playback_queue_capacity = max(
        args.playback_queue_chunks,
        MAX_PLAYBACK_BUFFER_BYTES // max(args.chunk_size, 1),
    )
    audio_queue = queue.Queue(maxsize=playback_queue_capacity)
    playback_stop = threading.Event()
    playback_state = {"start_perf": None, "error": ""}
    playback_state_lock = threading.Lock()
    playback_enabled = args.playback
    playback_error_reported = False
    player_thread = None
    response = None
    output_written = False
    pcm_bytes = 0
    frame_size = metrics["channels"] * metrics["sample_width"]
    pending = bytearray()
    byte_rate = metrics["sample_rate"] * metrics["channels"] * metrics["sample_width"]
    previous_read_finished_at = None
    first_read_finished_at = None
    stream_read_index = 0
    received_stream_bytes = 0
    client_request_id = f"tts-client-{uuid4().hex}"
    metrics["client_request_id"] = client_request_id

    def refresh_playback_health() -> None:
        """播放线程故障时立即停用本地播放，但不影响网络流测量。"""
        nonlocal playback_enabled, playback_error_reported
        failure = _get_playback_failure(
            player_thread,
            playback_stop,
            playback_state,
            playback_state_lock,
        )
        if not failure:
            return
        playback_enabled = False
        playback_stop.set()
        metrics["playback_error"] = metrics["playback_error"] or failure
        if not playback_error_reported:
            print(f"Warning: {failure}", flush=True)
            playback_error_reported = True

    try:
        print(
            f"max_new_tokens: {payload['max_new_tokens']}"
            + (" (auto)" if args.max_new_tokens is None else " (指定)"),
            flush=True,
        )
        start_time = time.perf_counter()
        request_timeout = urllib3.util.Timeout(
            total=args.total_timeout,
            connect=min(args.connect_timeout, args.total_timeout),
            read=min(args.read_timeout, args.total_timeout),
        )
        response = requests.post(
            args.url,
            json=payload,
            headers=build_headers(client_request_id),
            stream=True,
            verify=False,
            timeout=request_timeout,
        )
        metrics["response_header_time"] = time.perf_counter() - start_time
        metrics["status_code"] = response.status_code
        metrics["content_type"] = response.headers.get("Content-Type", "")
        metrics["transfer_encoding"] = response.headers.get("Transfer-Encoding", "")
        metrics["content_length"] = response.headers.get("Content-Length", "")
        metrics["response_x_accel_buffering"] = response.headers.get("X-Accel-Buffering", "")
        metrics["service_request_id"] = (
            response.headers.get("X-Request-ID")
            or response.headers.get("Request-ID")
            or client_request_id
        )
        metrics["worker_id"] = (
            response.headers.get("X-TTS-Worker")
            or response.headers.get("X-Worker-ID")
            or ""
        )
        metrics["worker_process_id"] = response.headers.get("X-Process-ID", "")
        response_elapsed = getattr(response, "elapsed", None)
        if response_elapsed is not None:
            try:
                metrics["requests_response_header_time"] = max(
                    float(response_elapsed.total_seconds()), 0.0
                )
            except (AttributeError, TypeError, ValueError):
                pass
        if response.status_code != 200:
            body = _read_error_body(response)
            raise requests.exceptions.HTTPError(
                f"HTTP {response.status_code}: {body}"
            )

        content_type = metrics["content_type"].lower()
        probe = bytearray()
        while len(probe) < 12:
            if time.perf_counter() - start_time >= args.total_timeout:
                raise TotalRequestTimeout(
                    f"request exceeded total timeout: {args.total_timeout:.3f}s"
                )
            read_started_at = time.perf_counter()
            probe_part = _read_available_response_chunk(response, 12 - len(probe))
            read_finished_at = time.perf_counter()
            if not probe_part:
                break
            stream_read_index += 1
            _record_stream_read_timing(
                metrics,
                read_index=stream_read_index,
                chunk_size=len(probe_part),
                received_bytes_before=received_stream_bytes,
                byte_rate=byte_rate,
                read_started_at=read_started_at,
                read_finished_at=read_finished_at,
                previous_read_finished_at=previous_read_finished_at,
            )
            if first_read_finished_at is None:
                first_read_finished_at = read_finished_at
            previous_read_finished_at = read_finished_at
            received_stream_bytes += len(probe_part)
            probe.extend(probe_part)
        if probe[:4] == b"RIFF" and len(probe) >= 12 and probe[8:12] == b"WAVE":
            raise WavParseError(
                "stream response is WAV, not PCM; refusing to play WAV bytes as raw PCM"
            )
        if "wav" in content_type:
            raise WavParseError(
                f"stream Content-Type is {metrics['content_type']!r}, expected audio/pcm"
            )
        probe_candidate = bytes(probe).lstrip(b" \t\r\n")
        if probe_candidate.startswith(b"\xef\xbb\xbf"):
            probe_candidate = probe_candidate[3:].lstrip(b" \t\r\n")
        if probe_candidate[:1] in (b"{", b"["):
            metrics["response_mode"] = "json_metadata"
            metrics["latency_metrics_note"] = "流式请求收到 JSON，而不是可播放的 PCM 音频。"
            raise AudioNotReturnedError(
                "streaming request returned JSON instead of raw PCM"
            )
        if probe_candidate.startswith(b"data:") or "text/event-stream" in content_type:
            metrics["response_mode"] = "sse"
            metrics["latency_metrics_note"] = "流式请求收到 SSE 文本，而不是可播放的 PCM 音频。"
            raise AudioNotReturnedError(
                "streaming request returned SSE instead of raw PCM"
            )
        if "json" in content_type:
            metrics["response_mode"] = "json_metadata"
            metrics["latency_metrics_note"] = "流式请求 Content-Type 为 JSON，而不是可播放的 PCM 音频。"
            raise AudioNotReturnedError(
                "streaming request returned JSON Content-Type instead of raw PCM"
            )
        if "pcm" not in content_type and "octet-stream" not in content_type:
            metrics["warning"] = (
                f"unexpected stream Content-Type: {metrics['content_type'] or 'missing'}; "
                "validated that the body is not a WAV header"
            )

        metrics["response_mode"] = "pcm_stream"
        metrics["latency_metrics_note"] = (
            "流式请求使用裸 PCM；网络读取等待与本地处理分别统计，"
            "结果是客户端观测，不等同于服务端 HTTP chunk 边界。"
        )
        with open(args.output, "wb") as f_save:
            output_written = True
            write_pcm_wav_header(
                f_save,
                metrics["sample_rate"],
                metrics["channels"],
                metrics["sample_width"],
            )
            pending_chunks = (
                [(bytes(probe), first_read_finished_at or time.perf_counter())]
                if probe
                else []
            )
            while True:
                refresh_playback_health()
                if time.perf_counter() - start_time >= args.total_timeout:
                    raise TotalRequestTimeout(
                        f"request exceeded total timeout: {args.total_timeout:.3f}s"
                    )
                if pending_chunks:
                    chunk, now = pending_chunks.pop(0)
                else:
                    read_started_at = time.perf_counter()
                    chunk = _read_available_response_chunk(
                        response, max(args.chunk_size, 3840)
                    )
                    read_finished_at = time.perf_counter()
                    if chunk:
                        stream_read_index += 1
                        _record_stream_read_timing(
                            metrics,
                            read_index=stream_read_index,
                            chunk_size=len(chunk),
                            received_bytes_before=received_stream_bytes,
                            byte_rate=byte_rate,
                            read_started_at=read_started_at,
                            read_finished_at=read_finished_at,
                            previous_read_finished_at=previous_read_finished_at,
                        )
                        previous_read_finished_at = read_finished_at
                        received_stream_bytes += len(chunk)
                    now = read_finished_at
                if not chunk:
                    break
                metrics["stream_first_chunk_time"] = (
                    metrics["stream_first_chunk_time"]
                    if metrics["stream_first_chunk_time"] is not None
                    else now - start_time
                )
                metrics["stream_last_chunk_time"] = now - start_time
                if metrics["ttfb"] is None:
                    metrics["ttfb"] = now - start_time
                pending.extend(chunk)
                playable_size = len(pending) - len(pending) % frame_size
                if playable_size <= 0:
                    continue
                pcm_part = bytes(pending[:playable_size])
                del pending[:playable_size]
                f_save.write(pcm_part)
                pcm_bytes += len(pcm_part)
                if metrics["first_audio_byte_time"] is None:
                    metrics["first_audio_byte_time"] = now - start_time
                    metrics["ttft"] = now - start_time
                metrics["last_audio_byte_time"] = now - start_time
                if playback_enabled:
                    if player_thread is None:
                        player_thread = threading.Thread(
                            target=play_thread_func,
                            args=(
                                audio_queue,
                                metrics["sample_rate"],
                                metrics["channels"],
                                metrics["sample_width"],
                                playback_stop,
                                playback_state,
                                playback_state_lock,
                            ),
                            daemon=True,
                        )
                        player_thread.start()
                        refresh_playback_health()
                    try:
                        if playback_enabled:
                            audio_queue.put_nowait(pcm_part)
                    except queue.Full:
                        playback_enabled = False
                        playback_stop.set()
                        metrics["playback_error"] = (
                            "playback queue reached its safety limit; "
                            "network measurement continued without playback"
                        )

            if pending:
                padding = (-len(pending)) % frame_size
                pcm_part = bytes(pending) + b"\x00" * padding
                f_save.write(pcm_part)
                pcm_bytes += len(pcm_part)
                if playback_enabled and player_thread is not None:
                    try:
                        audio_queue.put_nowait(pcm_part)
                    except queue.Full:
                        playback_enabled = False
                        playback_stop.set()
                        metrics["playback_error"] = (
                            "playback queue reached its safety limit; "
                            "network measurement continued without playback"
                        )

        if pcm_bytes < args.min_audio_bytes:
            raise WavParseError(f"PCM response is too small: {pcm_bytes} bytes")
        metrics["complete"] = True
        metrics["success"] = True
        metrics["service_success"] = True
        metrics["audio_artifact_available"] = True
        metrics["rt"] = time.perf_counter() - start_time
        metrics["audio_data_bytes"] = pcm_bytes
        metrics["audio_duration"] = pcm_bytes / (
            metrics["sample_rate"] * metrics["channels"] * metrics["sample_width"]
        )
        metrics["audio_duration_source"] = "PCM stream data size / PCM byte rate"
        metrics["response_body_bytes"] = pcm_bytes
    except TotalRequestTimeout as exc:
        metrics["error_type"] = "total_timeout"
        metrics["error"] = f"{type(exc).__name__}: {exc}"
        metrics["rt"] = time.perf_counter() - start_time
    except AudioNotReturnedError as exc:
        metrics["error_type"] = "audio_not_returned"
        metrics["error"] = f"{type(exc).__name__}: {exc}"
        metrics["rt"] = time.perf_counter() - start_time
    except WavParseError as exc:
        metrics["error_type"] = "invalid_wav"
        metrics["error"] = f"{type(exc).__name__}: {exc}"
        metrics["rt"] = time.perf_counter() - start_time
    except requests.exceptions.RequestException as exc:
        metrics["error_type"] = "network_error"
        metrics["error"] = f"{type(exc).__name__}: {exc}"
        metrics["rt"] = time.perf_counter() - start_time
    except Exception as exc:
        metrics["error_type"] = "unexpected_error"
        metrics["error"] = f"{type(exc).__name__}: {exc}"
        metrics["rt"] = time.perf_counter() - start_time
    finally:
        if response is not None:
            response.close()
        if player_thread is not None:
            if not playback_stop.is_set():
                try:
                    audio_queue.put(None, timeout=1.0)
                except queue.Full:
                    playback_stop.set()
            player_thread.join(
                timeout=max(
                    args.playback_join_timeout,
                    metrics.get("audio_duration", 0.0) + 1.0,
                )
            )
            with playback_state_lock:
                playback_error = playback_state.get("error", "")
                if playback_state.get("start_perf") is not None:
                    metrics["ttfa"] = playback_state["start_perf"] - start_time
            if playback_error:
                metrics["playback_error"] = playback_error
        if metrics["stream_read_count"] < 2:
            metrics["stream_uniformity_status"] = "insufficient_data"
            metrics["stream_uniformity_reason"] = "有效读取样本少于 2 次"
        elif args.playback or metrics.get("playback_error"):
            metrics["stream_uniformity_status"] = "contaminated"
            metrics["stream_uniformity_reason"] = (
                "启用了本地播放，客户端音频设备/线程调度可能影响读取计时"
                + (
                    f"；playback_error={metrics['playback_error']}"
                    if metrics.get("playback_error")
                    else ""
                )
            )
        else:
            metrics["stream_uniformity_status"] = "valid"
            metrics["stream_uniformity_reason"] = (
                "无本地播放干扰；指标仍是客户端可用字节读取等待，不等于服务端 HTTP chunk 间隔"
            )
        if metrics["rt"] is None:
            metrics["rt"] = time.perf_counter() - start_time
        if output_written:
            metrics["header_fixed"] = fix_wav_header(args.output)
            metrics["size"] = os.path.getsize(args.output)
        metrics["client_total_time"] = time.perf_counter() - start_time
        _populate_derived_metrics(metrics, args.text)
    return metrics


def request_tts(args) -> dict:
    if args.stream:
        return request_pcm_stream_tts(args)
    metrics = {
        "success": False,
        "status_code": None,
        "content_type": "",
        "response_header_time": None,
        "requests_response_header_time": None,
        "response_header_timer_delta": None,
        "transfer_encoding": "",
        "content_length": "",
        "response_x_accel_buffering": "",
        "ttfb": None,
        "first_audio_byte_time": None,
        "last_audio_byte_time": None,
        "ttft": None,
        "ttfa": None,
        "rt": None,
        "client_total_time": None,
        "response_body_time": None,
        "ttfb_after_headers": None,
        "ttft_after_headers": None,
        "rtf": None,
        "synthesis_speed": None,
        "audio_receive_time": None,
        "audio_receive_rtf": None,
        "audio_receive_speed": None,
        "text_chars": 0,
        "chars_per_second": None,
        "size": 0,
        "path": args.output,
        "complete": False,
        "audio_duration": 0.0,
        "audio_duration_source": "N/A",
        "audio_data_bytes": 0,
        "sample_rate": 0,
        "channels": 0,
        "sample_width": 0,
        "header_fixed": False,
        "error_type": "",
        "error": "",
        "warning": "",
        "playback_error": "",
        "response_json_preview": "",
        "json_response_path": "",
        "response_body_bytes": 0,
        "response_mode": "unknown",
        "latency_metrics_note": "",
        "service_success": False,
        "audio_artifact_available": False,
        "audio_url": "",
        "audio_path": "",
        "service_request_id": "",
        "response_message": "",
        "reported_audio_duration": None,
        "reported_sample_rate": None,
        "duration_difference": None,
        "synthesis_response_time": None,
        "audio_download_url": "",
        "audio_download_header_time": None,
        "audio_download_ttfb": None,
        "audio_download_time": None,
        "audio_download_status_code": None,
        "audio_download_content_type": "",
        "audio_download_attempts": [],
        "requested_stream": bool(args.stream),
        "timing_diagnosis": "",
    }

    total_bytes = 0
    header_read = False
    output_written = False
    wav_metrics_finalized = False
    wav_parser = IncrementalWavParser()

    # 队列设置内存上限；队列满时只停止本地播放，不暂停网络接收，
    # 从而避免把客户端播放速度误判为服务端流式输出。
    playback_queue_capacity = max(
        args.playback_queue_chunks,
        MAX_PLAYBACK_BUFFER_BYTES // max(args.chunk_size, 1),
    )
    audio_queue = queue.Queue(maxsize=playback_queue_capacity)
    player_thread = None
    playback_enabled = args.playback
    playback_stop = threading.Event()
    playback_state = {"start_perf": None, "error": ""}
    playback_state_lock = threading.Lock()
    deadline_reached = threading.Event()
    deadline_timer = None
    response = None
    audio_response = None
    playback_error_reported = False

    def refresh_playback_health() -> None:
        nonlocal playback_enabled, playback_error_reported
        failure = _get_playback_failure(
            player_thread,
            playback_stop,
            playback_state,
            playback_state_lock,
        )
        if not failure:
            return
        playback_enabled = False
        playback_stop.set()
        metrics["playback_error"] = metrics["playback_error"] or failure
        if not playback_error_reported:
            print(f"Warning: {failure}", flush=True)
            playback_error_reported = True

    payload = build_payload(args)
    print(
        f"max_new_tokens: {payload['max_new_tokens']}"
        + (" (auto)" if getattr(args, "max_new_tokens", None) is None else " (指定)"),
        flush=True,
    )
    start_time = time.perf_counter()
    try:
        request_timeout = urllib3.util.Timeout(
            total=args.total_timeout,
            connect=min(args.connect_timeout, args.total_timeout),
            read=min(args.read_timeout, args.total_timeout),
        )
        response = requests.post(
            args.url,
            json=payload,
            headers=build_headers(),
            stream=True,
            verify=False,
            timeout=request_timeout,
        )
        metrics["response_header_time"] = time.perf_counter() - start_time
        metrics["status_code"] = response.status_code
        response_headers = getattr(response, "headers", {})
        metrics["content_type"] = response_headers.get("Content-Type", "")
        metrics["transfer_encoding"] = response_headers.get("Transfer-Encoding", "")
        metrics["content_length"] = response_headers.get("Content-Length", "")
        metrics["response_x_accel_buffering"] = response_headers.get(
            "X-Accel-Buffering", ""
        )
        response_elapsed = getattr(response, "elapsed", None)
        if response_elapsed is not None:
            try:
                metrics["requests_response_header_time"] = max(
                    float(response_elapsed.total_seconds()), 0.0
                )
            except (AttributeError, TypeError, ValueError):
                pass

        if metrics["response_header_time"] >= args.total_timeout:
            raise TotalRequestTimeout(
                f"request exceeded total timeout before body read: {args.total_timeout:.3f}s"
            )

        remaining_timeout = args.total_timeout - (time.perf_counter() - start_time)
        if remaining_timeout <= 0:
            raise TotalRequestTimeout(
                f"request exceeded total timeout: {args.total_timeout:.3f}s"
            )

        def abort_response_on_deadline() -> None:
            deadline_reached.set()
            for active_response in (response, audio_response):
                if active_response is not None:
                    try:
                        active_response.close()
                    except Exception:
                        pass

        deadline_timer = threading.Timer(remaining_timeout, abort_response_on_deadline)
        deadline_timer.daemon = True
        deadline_timer.start()

        with response:
            if response.status_code != 200:
                error_body = _read_error_body(response)
                if deadline_reached.is_set():
                    raise TotalRequestTimeout(
                        f"request exceeded total timeout: {args.total_timeout:.3f}s"
                    )
                metrics["rt"] = time.perf_counter() - start_time
                metrics["error_type"] = "http_status"
                metrics["error"] = f"HTTP {response.status_code}: {error_body}"
                deadline_timer.cancel()
                deadline_timer = None
            else:
                content_type = metrics["content_type"].lower()
                is_json_response = "json" in content_type
                prefetched_body = bytearray()
                prefetched_first_byte_elapsed = None
                if not content_type:
                    detected_mode = None
                    while detected_mode is None and len(prefetched_body) < 64:
                        prefix_chunk = _read_response_chunk(response, 1)
                        prefix_now = time.perf_counter()
                        if not prefix_chunk:
                            break
                        if prefetched_first_byte_elapsed is None:
                            prefetched_first_byte_elapsed = prefix_now - start_time
                        prefetched_body.extend(prefix_chunk)
                        detected_mode = _classify_body_prefix(bytes(prefetched_body))
                    is_json_response = detected_mode == "json"
                    detected_label = "JSON" if is_json_response else "WAV"
                    metrics["warning"] = (
                        f"Content-Type is missing; response mode detected as {detected_label} "
                        "from the body prefix"
                    )
                metrics["response_mode"] = "json" if is_json_response else "wav_stream"
                if content_type and not is_json_response and not any(
                    marker in content_type
                    for marker in ("audio/", "application/octet-stream", "application/wav")
                ):
                    metrics["warning"] = (
                        f"unexpected Content-Type for WAV response: {metrics['content_type']}"
                    )
                if is_json_response:
                    json_chunks = [bytes(prefetched_body)] if prefetched_body else []
                    json_total_bytes = len(prefetched_body)
                    json_first_byte_seen = bool(prefetched_body)
                    if json_first_byte_seen:
                        metrics["ttfb"] = prefetched_first_byte_elapsed
                    metrics["latency_metrics_note"] = (
                        "TTFB 按网关 JSON 首字节计算；音频相关指标按客户端首次获得并解析 WAV "
                        "PCM 数据的时间计算。"
                    )
                    while True:
                        refresh_playback_health()
                        if deadline_reached.is_set():
                            raise TotalRequestTimeout(
                                f"request exceeded total timeout: {args.total_timeout:.3f}s"
                            )
                        read_size = 1 if not json_first_byte_seen else args.chunk_size
                        chunk = _read_response_chunk(response, read_size)
                        now = time.perf_counter()
                        if not chunk:
                            metrics["complete"] = True
                            metrics["rt"] = now - start_time
                            metrics["synthesis_response_time"] = metrics["rt"]
                            break
                        if json_total_bytes + len(chunk) > args.max_response_bytes:
                            raise ResponseLimitError(
                                "JSON response exceeds configured byte limit: "
                                f"{args.max_response_bytes} bytes"
                            )
                        if not json_first_byte_seen:
                            json_first_byte_seen = True
                            metrics["ttfb"] = now - start_time
                        json_chunks.append(chunk)
                        json_total_bytes += len(chunk)

                    raw_json = b"".join(json_chunks)
                    metrics["response_body_bytes"] = len(raw_json)
                    metrics["response_json_preview"] = raw_json.decode(
                        getattr(response, "encoding", None) or "utf-8", errors="replace"
                    )[:DEFAULT_JSON_PREVIEW_CHARS]
                    json_path = f"{args.output}.response.json"
                    with open(json_path, "wb") as json_file:
                        json_file.write(raw_json)
                    metrics["json_response_path"] = json_path

                    json_payload, json_text = _parse_json_response(
                        raw_json, getattr(response, "encoding", None)
                    )
                    metrics["reported_audio_duration"] = _extract_json_number(
                        json_payload, ("duration", "audio_duration", "audioDuration")
                    )
                    reported_sample_rate = _extract_json_number(
                        json_payload, ("sample_rate", "sampleRate")
                    )
                    metrics["reported_sample_rate"] = (
                        int(reported_sample_rate) if reported_sample_rate is not None else None
                    )
                    json_service_success = _extract_json_success(json_payload)
                    metrics["service_success"] = json_service_success is True
                    decoded_audio = _decode_json_audio_candidate(json_payload)
                    audio_url = _extract_json_audio_url(json_payload)
                    audio_path = _extract_json_string(json_payload, ("audio_path", "audioPath"))
                    metrics["audio_path"] = audio_path
                    metrics["service_request_id"] = _extract_json_string(
                        json_payload, ("request_id", "requestId")
                    )
                    metrics["response_message"] = _extract_json_string(
                        json_payload, ("message", "msg")
                    )
                    if decoded_audio is not None:
                        audio_bytes = decoded_audio
                        metrics["response_mode"] = "json_base64"
                        metrics["latency_metrics_note"] = (
                            "网关返回 JSON Base64 而非直接 WAV；实际时长和端到端 RTF 可计算，"
                            "但首 PCM/TTFT 无法代表网络首音频延迟，因此保持 N/A，"
                            "不可与流式 WAV 指标比较。"
                        )
                    elif audio_url or audio_path:
                        metrics["response_mode"] = "json_metadata"
                        metrics["audio_url"] = audio_url
                        if args.stream:
                            metrics["latency_metrics_note"] = (
                                "流式请求收到的是 audio_url/audio_path JSON 元数据而非 WAV 字节；"
                                "为保持流式测量口径，不执行二次下载。"
                            )
                            raise AudioNotReturnedError(
                                "streaming request returned only audio metadata instead of a WAV stream"
                            )

                        download_urls = _resolve_audio_download_urls(
                            args.url, audio_url, audio_path
                        )
                        if not download_urls:
                            raise AudioNotReturnedError(
                                "TTS synthesis succeeded, but no downloadable audio URL was returned"
                            )

                        metrics["response_mode"] = "json_audio_url"
                        metrics["latency_metrics_note"] = (
                            "非流式网关先返回 JSON，再下载 audio_url/audio_path 指向的 WAV；"
                            "TTFB 为 JSON 首字节，首 PCM/TTFT/RT/RTF 均按原始请求开始后的端到端时间计算。"
                        )
                        download_attempts = []
                        prefetched_audio = bytearray()
                        download_started_at = None
                        for download_url in download_urls:
                            remaining_timeout = args.total_timeout - (
                                time.perf_counter() - start_time
                            )
                            if deadline_reached.is_set() or remaining_timeout <= 0:
                                raise TotalRequestTimeout(
                                    f"request exceeded total timeout: {args.total_timeout:.3f}s"
                                )

                            attempt_started_at = time.perf_counter()
                            download_timeout = urllib3.util.Timeout(
                                total=remaining_timeout,
                                connect=min(args.connect_timeout, remaining_timeout),
                                read=min(args.read_timeout, remaining_timeout),
                            )
                            try:
                                audio_response = requests.get(
                                    download_url,
                                    headers=_build_audio_download_headers(
                                        args.url, download_url
                                    ),
                                    stream=True,
                                    verify=False,
                                    timeout=download_timeout,
                                )
                                header_elapsed = time.perf_counter() - start_time
                                content_type = audio_response.headers.get(
                                    "Content-Type", ""
                                ).lower()
                                if audio_response.status_code != 200:
                                    error_body = _read_error_body(audio_response)
                                    download_attempts.append(
                                        f"{download_url} -> HTTP {audio_response.status_code}: "
                                        f"{error_body[:200]}"
                                    )
                                    audio_response.close()
                                    audio_response = None
                                    continue
                                if "json" in content_type or "text/" in content_type:
                                    error_body = _read_error_body(audio_response)
                                    download_attempts.append(
                                        f"{download_url} -> Content-Type="
                                        f"{content_type or 'missing'}: {error_body[:200]}"
                                    )
                                    audio_response.close()
                                    audio_response = None
                                    continue

                                first_byte = _read_response_chunk(audio_response, 1)
                                first_byte_elapsed = time.perf_counter() - start_time
                                if not first_byte:
                                    download_attempts.append(
                                        f"{download_url} -> HTTP 200 but response body is empty"
                                    )
                                    audio_response.close()
                                    audio_response = None
                                    continue

                                prefix = bytearray(first_byte)
                                while len(prefix) < 12:
                                    prefix_part = _read_response_chunk(
                                        audio_response, 12 - len(prefix)
                                    )
                                    if not prefix_part:
                                        break
                                    prefix.extend(prefix_part)
                                if not (
                                    len(prefix) >= 12
                                    and prefix[:4] == b"RIFF"
                                    and prefix[8:12] == b"WAVE"
                                ):
                                    preview = bytes(prefix[:32]).hex(" ")
                                    download_attempts.append(
                                        f"{download_url} -> response is not RIFF/WAVE; "
                                        f"Content-Type={content_type or 'missing'}, prefix={preview}"
                                    )
                                    audio_response.close()
                                    audio_response = None
                                    continue

                                metrics["audio_download_url"] = download_url
                                metrics["audio_download_header_time"] = header_elapsed
                                metrics["audio_download_ttfb"] = first_byte_elapsed
                                metrics["audio_download_status_code"] = (
                                    audio_response.status_code
                                )
                                metrics["audio_download_content_type"] = content_type
                                prefetched_audio.extend(prefix)
                                download_started_at = attempt_started_at
                                break
                            except requests.exceptions.RequestException as exc:
                                download_attempts.append(
                                    f"{download_url} -> {type(exc).__name__}: {exc}"
                                )
                                if audio_response is not None:
                                    audio_response.close()
                                    audio_response = None

                        metrics["audio_download_attempts"] = download_attempts
                        if audio_response is None or not prefetched_audio:
                            detail = "; ".join(download_attempts) or "no usable response"
                            raise AudioNotReturnedError(
                                "all audio download candidates failed: " + detail
                            )

                        with audio_response:

                            total_bytes = 0
                            header_read = False
                            wav_parser = IncrementalWavParser()
                            with open(args.output, "wb") as f_save:
                                output_written = True
                                while True:
                                    refresh_playback_health()
                                    if deadline_reached.is_set():
                                        raise TotalRequestTimeout(
                                            f"request exceeded total timeout: {args.total_timeout:.3f}s"
                                        )

                                    if not header_read:
                                        if (
                                            wav_parser.info.data_offset is not None
                                            and metrics["first_audio_byte_time"] is None
                                        ):
                                            read_size = 1
                                        else:
                                            read_size = wav_parser.bytes_needed_for_progress(
                                                args.chunk_size
                                            )
                                    else:
                                        read_size = args.chunk_size

                                    chunk_start_offset = total_bytes
                                    if prefetched_audio:
                                        chunk = bytes(prefetched_audio[:read_size])
                                        del prefetched_audio[:read_size]
                                    else:
                                        chunk = _read_response_chunk(
                                            audio_response, read_size
                                        )
                                    now = time.perf_counter()
                                    if not chunk:
                                        if deadline_reached.is_set():
                                            raise TotalRequestTimeout(
                                                f"request exceeded total timeout: {args.total_timeout:.3f}s"
                                            )
                                        metrics["complete"] = True
                                        metrics["rt"] = now - start_time
                                        metrics["audio_download_time"] = (
                                            now - download_started_at
                                        )
                                        if deadline_timer is not None:
                                            deadline_timer.cancel()
                                            deadline_timer = None
                                        break

                                    if total_bytes + len(chunk) > args.max_response_bytes:
                                        raise ResponseLimitError(
                                            "downloaded audio exceeds configured byte limit: "
                                            f"{args.max_response_bytes} bytes"
                                        )

                                    f_save.write(chunk)
                                    total_bytes += len(chunk)

                                    if not header_read:
                                        wav_parser.feed(chunk)
                                        if (
                                            metrics["first_audio_byte_time"] is None
                                            and wav_parser.info.data_offset is not None
                                            and total_bytes > wav_parser.info.data_offset
                                        ):
                                            metrics["first_audio_byte_time"] = now - start_time
                                        if not wav_parser.first_frame_ready:
                                            continue

                                        header_read = True
                                        metrics["ttft"] = now - start_time
                                        wav_info = wav_parser.info
                                        metrics["sample_rate"] = wav_info.sample_rate
                                        metrics["channels"] = wav_info.channels
                                        metrics["sample_width"] = wav_info.sample_width

                                    wav_info = wav_parser.info
                                    audio_part = _audio_bytes_from_chunk(
                                        chunk,
                                        chunk_start_offset,
                                        total_bytes,
                                        wav_info,
                                    )
                                    if audio_part:
                                        metrics["last_audio_byte_time"] = now - start_time
                                    received_audio_bytes = total_bytes - wav_info.data_offset
                                    declared_audio_limit = _audio_data_limit_while_streaming(
                                        wav_info, total_bytes
                                    )
                                    if declared_audio_limit is not None:
                                        received_audio_bytes = min(
                                            received_audio_bytes, declared_audio_limit
                                        )
                                    if (
                                        received_audio_bytes / wav_info.byte_rate
                                        > args.max_audio_seconds
                                    ):
                                        raise ResponseLimitError(
                                            "audio duration exceeds configured limit: "
                                            f"{args.max_audio_seconds:.3f}s"
                                        )

                                    if playback_enabled and audio_part:
                                        if player_thread is None:
                                            player_thread = threading.Thread(
                                                target=play_thread_func,
                                                args=(
                                                    audio_queue,
                                                    wav_info.sample_rate,
                                                    wav_info.channels,
                                                    wav_info.sample_width,
                                                    playback_stop,
                                                    playback_state,
                                                    playback_state_lock,
                                                ),
                                                daemon=True,
                                            )
                                            player_thread.start()
                                            refresh_playback_health()
                                        if playback_enabled:
                                            try:
                                                audio_queue.put_nowait(audio_part)
                                            except queue.Full:
                                                playback_enabled = False
                                                playback_stop.set()
                                                metrics["playback_error"] = (
                                                    "playback queue reached its safety limit; "
                                                    "network measurement continued without playback"
                                                )
                                            refresh_playback_health()

                        metrics["response_body_bytes"] = json_total_bytes + total_bytes
                        _finalize_wav_metrics(metrics, wav_parser, total_bytes, args)
                        metrics["audio_duration_source"] = (
                            "downloaded WAV data size / WAV byte rate"
                        )
                        wav_metrics_finalized = True
                        audio_bytes = None
                    else:
                        audio_bytes = _extract_json_audio(json_payload, json_text)
                    if audio_bytes is not None and len(audio_bytes) > args.max_response_bytes:
                        raise ResponseLimitError(
                            "decoded JSON audio exceeds configured byte limit: "
                            f"{args.max_response_bytes} bytes"
                        )
                    if audio_bytes is not None:
                        with open(args.output, "wb") as f_save:
                            output_written = True
                            f_save.write(audio_bytes)
                        total_bytes = len(audio_bytes)
                        wav_parser = _parse_wav_bytes_prefix(audio_bytes, args.chunk_size)
                        header_read = wav_parser.first_frame_ready
                        metrics["first_audio_byte_time"] = None
                        metrics["ttft"] = None
                        _finalize_wav_metrics(metrics, wav_parser, total_bytes, args)
                        wav_metrics_finalized = True
                    if audio_bytes is not None and playback_enabled:
                        try:
                            wav_info = wav_parser.info
                            player_thread = threading.Thread(
                                target=play_thread_func,
                                args=(
                                    audio_queue,
                                    wav_info.sample_rate,
                                    wav_info.channels,
                                    wav_info.sample_width,
                                    playback_stop,
                                    playback_state,
                                    playback_state_lock,
                                ),
                                daemon=True,
                            )
                            player_thread.start()
                            refresh_playback_health()
                            audio_end = wav_info.data_offset + metrics["audio_data_bytes"]
                            audio_payload = audio_bytes[wav_info.data_offset:audio_end]
                            for offset in range(0, len(audio_payload), args.chunk_size):
                                if not playback_enabled:
                                    break
                                audio_chunk = audio_payload[offset:offset + args.chunk_size]
                                try:
                                    audio_queue.put_nowait(audio_chunk)
                                except queue.Full:
                                    playback_enabled = False
                                    playback_stop.set()
                                    metrics["playback_error"] = (
                                        "playback queue reached its safety limit; "
                                        "network measurement continued without playback"
                                    )
                                    break
                                refresh_playback_health()
                        except Exception as exc:
                            playback_enabled = False
                            playback_stop.set()
                            metrics["playback_error"] = (
                                f"playback setup failed for JSON audio: {exc}"
                            )
                    if metrics["complete"] and deadline_timer is not None:
                        deadline_timer.cancel()
                        deadline_timer = None
                else:
                    with open(args.output, "wb") as f_save:
                        output_written = True
                        while True:
                            refresh_playback_health()
                            if deadline_reached.is_set():
                                raise TotalRequestTimeout(
                                    f"request exceeded total timeout: {args.total_timeout:.3f}s"
                                )

                            if metrics["ttfb"] is None:
                                read_size = 1
                            elif not header_read:
                                if (
                                    wav_parser.info.data_offset is not None
                                    and metrics["first_audio_byte_time"] is None
                                ):
                                    read_size = 1
                                else:
                                    read_size = wav_parser.bytes_needed_for_progress(
                                        args.chunk_size
                                    )
                            else:
                                read_size = args.chunk_size

                            chunk_start_offset = total_bytes
                            if prefetched_body:
                                chunk = bytes(prefetched_body[:read_size])
                                del prefetched_body[:read_size]
                                now = (
                                    start_time + prefetched_first_byte_elapsed
                                    if metrics["ttfb"] is None
                                    and prefetched_first_byte_elapsed is not None
                                    else time.perf_counter()
                                )
                            else:
                                chunk = _read_response_chunk(response, read_size)
                                now = time.perf_counter()
                            if not chunk:
                                if deadline_reached.is_set():
                                    raise TotalRequestTimeout(
                                        f"request exceeded total timeout: {args.total_timeout:.3f}s"
                                    )
                                metrics["complete"] = True
                                metrics["rt"] = now - start_time
                                if deadline_timer is not None:
                                    deadline_timer.cancel()
                                    deadline_timer = None
                                break

                            if total_bytes + len(chunk) > args.max_response_bytes:
                                raise ResponseLimitError(
                                    "response exceeds configured byte limit: "
                                    f"{args.max_response_bytes} bytes"
                                )

                            if metrics["ttfb"] is None:
                                metrics["ttfb"] = now - start_time

                            f_save.write(chunk)
                            total_bytes += len(chunk)

                            if not header_read:
                                wav_parser.feed(chunk)
                                if (
                                    metrics["first_audio_byte_time"] is None
                                    and wav_parser.info.data_offset is not None
                                    and total_bytes > wav_parser.info.data_offset
                                ):
                                    metrics["first_audio_byte_time"] = now - start_time
                                if not wav_parser.first_frame_ready:
                                    continue

                                header_read = True
                                metrics["ttft"] = now - start_time
                                wav_info = wav_parser.info
                                metrics["sample_rate"] = wav_info.sample_rate
                                metrics["channels"] = wav_info.channels
                                metrics["sample_width"] = wav_info.sample_width

                            wav_info = wav_parser.info
                            audio_part = (
                                _audio_bytes_from_chunk(
                                    chunk,
                                    chunk_start_offset,
                                    total_bytes,
                                    wav_info,
                                )
                                if header_read
                                else b""
                            )
                            if audio_part:
                                metrics["last_audio_byte_time"] = now - start_time
                            if header_read and wav_info.data_offset is not None:
                                received_audio_bytes = total_bytes - wav_info.data_offset
                                declared_audio_limit = _audio_data_limit_while_streaming(
                                    wav_info, total_bytes
                                )
                                if declared_audio_limit is not None:
                                    received_audio_bytes = min(
                                        received_audio_bytes, declared_audio_limit
                                    )
                                if received_audio_bytes / wav_info.byte_rate > args.max_audio_seconds:
                                    raise ResponseLimitError(
                                        "audio duration exceeds configured limit: "
                                        f"{args.max_audio_seconds:.3f}s"
                                    )

                            if playback_enabled and audio_part:
                                if player_thread is None:
                                    try:
                                        player_thread = threading.Thread(
                                            target=play_thread_func,
                                            args=(
                                                audio_queue,
                                                wav_info.sample_rate,
                                                wav_info.channels,
                                                wav_info.sample_width,
                                                playback_stop,
                                                playback_state,
                                                playback_state_lock,
                                            ),
                                            daemon=True,
                                        )
                                        player_thread.start()
                                        refresh_playback_health()
                                    except Exception as exc:
                                        playback_enabled = False
                                        metrics["playback_error"] = f"playback thread start failed: {exc}"

                                if playback_enabled:
                                    try:
                                        audio_queue.put_nowait(audio_part)
                                    except queue.Full:
                                        playback_enabled = False
                                        playback_stop.set()
                                        metrics["playback_error"] = (
                                            "playback queue reached its safety limit; "
                                            "network measurement continued without playback"
                                        )
                                    refresh_playback_health()

                if metrics["complete"] and not wav_metrics_finalized:
                    _finalize_wav_metrics(metrics, wav_parser, total_bytes, args)

    except TotalRequestTimeout as exc:
        _set_metric_error(metrics, exc, "total_timeout", start_time)
    except (IncompleteRead, ProtocolError, requests.exceptions.ChunkedEncodingError) as exc:
        if deadline_reached.is_set():
            _set_metric_error(
                metrics,
                TotalRequestTimeout(
                    f"request exceeded total timeout: {args.total_timeout:.3f}s"
                ),
                "total_timeout",
                start_time,
            )
        else:
            _set_metric_error(metrics, exc, "stream_truncated", start_time)
    except requests.exceptions.Timeout as exc:
        if deadline_reached.is_set():
            _set_metric_error(
                metrics,
                TotalRequestTimeout(
                    f"request exceeded total timeout: {args.total_timeout:.3f}s"
                ),
                "total_timeout",
                start_time,
            )
        else:
            _set_metric_error(metrics, exc, "network_timeout", start_time)
    except requests.exceptions.RequestException as exc:
        if deadline_reached.is_set():
            _set_metric_error(
                metrics,
                TotalRequestTimeout(
                    f"request exceeded total timeout: {args.total_timeout:.3f}s"
                ),
                "total_timeout",
                start_time,
            )
        else:
            _set_metric_error(metrics, exc, "network_error", start_time)
    except AudioNotReturnedError as exc:
        metrics["audio_artifact_available"] = False
        if metrics.get("service_success") and args.stream:
            metrics["success"] = True
            metrics["complete"] = True
            metrics["error_type"] = ""
            metrics["error"] = ""
            metadata_warning = (
                "TTS服务合成成功，但网关只返回音频地址元数据；"
                "未下载音频，因此实际音频时长、TTFT和RTF为N/A"
            )
            metrics["warning"] = "; ".join(
                item for item in (metrics.get("warning", ""), metadata_warning) if item
            )
        else:
            metrics["success"] = False
            metrics["complete"] = False
            metrics["error_type"] = "audio_not_returned"
            metrics["error"] = f"{type(exc).__name__}: {exc}"
            metrics["rt"] = max(time.perf_counter() - start_time, 0.0)
        if metrics.get("rt") is None:
            metrics["rt"] = max(time.perf_counter() - start_time, 0.0)
    except ResponseLimitError as exc:
        _set_metric_error(metrics, exc, "response_limit", start_time)
    except WavParseError as exc:
        _set_metric_error(metrics, exc, "invalid_wav", start_time)
    except OSError as exc:
        _set_metric_error(metrics, exc, "file_error", start_time)
    except Exception as exc:
        _set_metric_error(metrics, exc, "unexpected_error", start_time)
    finally:
        if deadline_timer is not None:
            deadline_timer.cancel()

        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if audio_response is not None:
            try:
                audio_response.close()
            except Exception:
                pass

        if metrics["rt"] is None:
            metrics["rt"] = time.perf_counter() - start_time

        if player_thread is not None:
            try:
                if not playback_stop.is_set():
                    # 队列有积压时等待播放线程消费完再发送结束标记，避免截断尾音。
                    while not playback_stop.is_set():
                        try:
                            audio_queue.put(None, timeout=0.2)
                            break
                        except queue.Full:
                            if not player_thread.is_alive():
                                playback_stop.set()
                playback_join_timeout = args.playback_join_timeout
                if isinstance(metrics.get("audio_duration"), (int, float)):
                    # 服务端可能瞬间推完响应，播放队列仍需按音频实际时长播放完，
                    # 否则进程提前退出会截断尾音。
                    playback_join_timeout = max(
                        playback_join_timeout,
                        metrics["audio_duration"] + 1.0,
                    )
                player_thread.join(timeout=playback_join_timeout)
                if player_thread.is_alive():
                    playback_stop.set()
                    player_thread.join(timeout=1.0)
                    if player_thread.is_alive():
                        metrics["playback_error"] = (
                            metrics["playback_error"] or "playback thread did not stop cleanly"
                        )
            except Exception as exc:
                metrics["playback_error"] = f"playback cleanup failed: {exc}"

            with playback_state_lock:
                playback_start_perf = playback_state.get("start_perf")
                playback_error = playback_state.get("error")
            if playback_start_perf is not None:
                metrics["ttfa"] = playback_start_perf - start_time
            if playback_error:
                metrics["playback_error"] = playback_error

    if metrics["success"] and metrics.get("audio_artifact_available"):
        reported_duration = metrics.get("reported_audio_duration")
        if reported_duration is not None and reported_duration >= 0:
            duration_difference = abs(metrics["audio_duration"] - reported_duration)
            metrics["duration_difference"] = duration_difference
            duration_tolerance = max(0.05, metrics["audio_duration"] * 0.01)
            if duration_difference > duration_tolerance:
                duration_warning = (
                    "JSON duration differs from actual WAV duration: "
                    f"reported={reported_duration:.3f}s, actual={metrics['audio_duration']:.3f}s"
                )
                metrics["warning"] = "; ".join(
                    item for item in (metrics["warning"], duration_warning) if item
                )
        reported_sample_rate = metrics.get("reported_sample_rate")
        if reported_sample_rate and reported_sample_rate != metrics.get("sample_rate"):
            sample_rate_warning = (
                "JSON sample_rate differs from actual WAV: "
                f"reported={reported_sample_rate}, actual={metrics.get('sample_rate')}"
            )
            metrics["warning"] = "; ".join(
                item for item in (metrics["warning"], sample_rate_warning) if item
            )

    if metrics["success"] and output_written:
        metrics["header_fixed"] = fix_wav_header(args.output)
        if not metrics["header_fixed"]:
            header_warning = "audio metrics are valid, but the saved WAV header was not finalized"
            metrics["warning"] = "; ".join(
                item for item in (metrics["warning"], header_warning) if item
            )

    metrics["size"] = (
        os.path.getsize(args.output)
        if output_written and os.path.exists(args.output)
        else total_bytes
    )
    if not metrics.get("response_body_bytes"):
        metrics["response_body_bytes"] = total_bytes
    metrics["client_total_time"] = time.perf_counter() - start_time
    _populate_derived_metrics(metrics, args.text)
    return metrics


def print_tts_key_metrics(scene_name, metrics, text, ttft_threshold):
    audio_duration = metrics.get("audio_duration", 0.0)
    rt = metrics.get("rt")
    raw_text_len = len(text or "")
    text_len = metrics.get("text_chars", 0)
    rtf = metrics.get("rtf")
    synth_speed = metrics.get("synthesis_speed")
    chars_per_sec = metrics.get("chars_per_second")
    audio_kb = metrics.get("size", 0) / 1024 if metrics.get("size") else 0.0
    if metrics.get("service_success") and not metrics.get("audio_artifact_available"):
        sample_status = "服务端成功/未返回音频"
    elif metrics.get("success"):
        sample_status = "成功/完整"
    else:
        sample_status = "失败/无效"

    print("\n" + "=" * 60)
    print(f"[Metrics] {scene_name} TTS关键指标")
    print(f"  样本状态          : {sample_status}")
    print(f"  HTTP状态码        : {metrics.get('status_code') or 'N/A'}")
    if metrics.get("service_request_id"):
        print(f"  请求ID            : {metrics['service_request_id']}")
    if metrics.get("worker_id"):
        print(f"  后端Worker        : {metrics['worker_id']}")
    if metrics.get("worker_process_id"):
        print(f"  后端进程          : {metrics['worker_process_id']}")
    if metrics.get("content_type"):
        print(f"  Content-Type      : {metrics['content_type']}")
    if metrics.get("error"):
        print(f"  错误分类          : {metrics.get('error_type') or 'unknown'}")
        print(f"  异常原因          : {metrics['error']}")
    if metrics.get("warning"):
        print(f"  警告              : {metrics['warning']}")
    if metrics.get("response_json_preview"):
        print(f"  JSON响应文件      : {metrics.get('json_response_path') or 'N/A'}")
        print("  JSON响应内容(最多4000字符):")
        print(metrics["response_json_preview"])
    if metrics.get("latency_metrics_note"):
        print(f"  延迟指标说明      : {metrics['latency_metrics_note']}")
    if metrics.get("audio_url"):
        if metrics.get("audio_artifact_available"):
            reference_status = "已下载并验证"
        elif metrics.get("response_mode") == "json_audio_url":
            reference_status = "下载失败或未获得有效WAV"
        else:
            reference_status = "未下载"
        print(f"  服务端音频引用    : {metrics['audio_url']}（{reference_status}）")
    if metrics.get("audio_download_url"):
        print(f"  实际音频下载地址  : {metrics['audio_download_url']}")
    if metrics.get("audio_download_attempts"):
        print("  音频下载失败尝试  :")
        for attempt in metrics["audio_download_attempts"]:
            print(f"    - {attempt}")
    if metrics.get("audio_path"):
        print(f"  服务端音频路径    : {metrics['audio_path']}")
    if metrics.get("response_message"):
        print(f"  服务端消息        : {metrics['response_message']}")
    if metrics.get("reported_audio_duration") is not None:
        print(
            f"  JSON报告音频时长  : {_format_seconds(metrics['reported_audio_duration'])} "
            f"(与实际差 {_format_seconds(metrics.get('duration_difference'))})"
        )
    if metrics.get("playback_error"):
        print(f"  播放状态          : {metrics['playback_error']}")
    print("  延迟:")
    print(
        "    请求开始至HTTP响应头: "
        f"{_format_seconds(metrics.get('response_header_time'))} (client perf_counter)"
    )
    if metrics.get("requests_response_header_time") is not None:
        print(
            "    requests内部响应头计时: "
            f"{_format_seconds(metrics.get('requests_response_header_time'))}"
        )
        print(
            "    两套响应头计时差值  : "
            f"{_format_seconds(metrics.get('response_header_timer_delta'))}"
        )
    if (
        metrics.get("transfer_encoding")
        or metrics.get("content_length")
        or metrics.get("response_x_accel_buffering")
    ):
        print(
            "    HTTP响应传输声明    : "
            f"Transfer-Encoding={metrics.get('transfer_encoding') or 'N/A'}, "
            f"Content-Length={metrics.get('content_length') or 'N/A'}, "
            f"X-Accel-Buffering={metrics.get('response_x_accel_buffering') or 'N/A'}"
        )
    print(f"    TTFB首响应体字节  : {_format_seconds(metrics.get('ttfb'))}")
    print(
        "    响应头后至TTFB    : "
        f"{_format_seconds(metrics.get('ttfb_after_headers'))}"
    )
    if metrics.get("synthesis_response_time") is not None:
        print(
            "    合成JSON响应完成  : "
            f"{_format_seconds(metrics.get('synthesis_response_time'))}"
        )
        print(
            "    网关JSON体接收耗时: "
            f"{_format_seconds(metrics.get('gateway_response_body_time'))}"
        )
    if metrics.get("audio_download_header_time") is not None:
        print(
            "    音频下载响应头(E2E): "
            f"{_format_seconds(metrics.get('audio_download_header_time'))}"
        )
        print(
            "    下载请求至响应头  : "
            f"{_format_seconds(metrics.get('audio_download_header_latency'))}"
        )
        print(
            "    音频下载首字节(E2E): "
            f"{_format_seconds(metrics.get('audio_download_ttfb'))}"
        )
    print(f"    首个PCM音频字节   : {_format_seconds(metrics.get('first_audio_byte_time'))}")
    if metrics.get("response_mode") == "pcm_stream":
        print(f"    TTFT首个完整PCM帧 : {_format_seconds(metrics.get('ttft'))}")
    else:
        print(f"    TTFT首个完整音频帧: {_format_seconds(metrics.get('ttft'))}")
    print(f"    响应头后至首PCM字节: {_format_seconds(metrics.get('first_audio_after_headers'))}")
    print(f"    响应头后至首音频帧: {_format_seconds(metrics.get('ttft_after_headers'))}")
    if metrics.get("last_audio_byte_time") is not None:
        print(
            "    末个PCM音频字节    : "
            f"{_format_seconds(metrics.get('last_audio_byte_time'))}"
        )
        print(
            "    PCM接收窗口(首至末): "
            f"{_format_seconds(metrics.get('audio_receive_time'))}"
        )
    if metrics.get("audio_download_header_time") is not None:
        print(
            "    下载头后至首PCM字节: "
            f"{_format_seconds(metrics.get('first_audio_after_download_headers'))}"
        )
        print(
            "    下载头后至首音频帧: "
            f"{_format_seconds(metrics.get('ttft_after_download_headers'))}"
        )
        print(
            "    音频下载体接收耗时: "
            f"{_format_seconds(metrics.get('audio_download_body_time'))}"
        )
    print(f"    TTFA首次写入播放设备: {_format_seconds(metrics.get('ttfa'))}")
    if metrics.get("requested_stream") and metrics.get("stream_chunk_count") is not None:
        print(
            "    客户端可用字节读取  : "
            f"{metrics.get('stream_read_count')} 次（不等于服务端 HTTP chunk 数）"
        )
        print(
            "    最大网络读取等待    : "
            f"{_format_seconds(metrics.get('stream_max_read_wait'))}"
        )
        read_detail = metrics.get("stream_max_read_wait_detail") or {}
        if read_detail:
            print(
                "    最大等待所在位置    : "
                f"读取#{read_detail.get('read_index')}, "
                f"音频约 {read_detail.get('audio_offset_seconds', 0.0):.3f}s, "
                f"返回 {read_detail.get('chunk_size')} bytes"
            )
        print(
            "    最大本地处理间隔    : "
            f"{_format_seconds(metrics.get('stream_max_local_processing_time'))}"
        )
        print(
            "    最大读取完成间隔    : "
            f"{_format_seconds(metrics.get('stream_max_completion_gap'))}"
        )
        print(
            "    流式均匀性口径      : "
            f"{metrics.get('stream_uniformity_status') or 'N/A'}；"
            f"{metrics.get('stream_uniformity_reason') or '无'}"
        )
    rt_label = (
        "RT合成JSON响应完成"
        if metrics.get("response_mode") == "json_metadata"
        else "RT完整获得音频"
        if metrics.get("response_mode") == "json_audio_url"
        else "RT完整接收HTTP响应"
    )
    print(f"    {rt_label}: {_format_seconds(rt)}")
    response_body_label = (
        "初始响应头后端到端耗时"
        if metrics.get("response_mode") == "json_audio_url"
        else "响应体接收阶段耗时"
    )
    print(f"    {response_body_label}: {_format_seconds(metrics.get('response_body_time'))}")
    print(f"    客户端完成总耗时  : {_format_seconds(metrics.get('client_total_time'))}")
    print("  实时性:")
    print(
        f"    音频时长          : {_format_seconds(audio_duration if audio_duration > 0 else None)} "
        f"({metrics.get('audio_duration_source', 'N/A')})"
    )
    if metrics.get("sample_rate"):
        print(
            "    音频格式          : "
            f"{metrics['sample_rate']}Hz, {metrics['channels']}ch, "
            f"{metrics['sample_width'] * 8}bit PCM"
        )
    print(f"    RTF=RT/音频时长   : {_format_ratio(rtf)}")
    print(
        f"    端到端产出速度=1/RTF: {synth_speed:.2f}x实时"
        if synth_speed is not None
        else "    端到端产出速度=1/RTF: N/A"
    )
    if metrics.get("audio_receive_rtf") is not None:
        print(
            "    PCM接收RTF=接收窗口/时长: "
            f"{_format_ratio(metrics.get('audio_receive_rtf'))}"
        )
        print(
            "    PCM接收速度        : "
            f"{metrics['audio_receive_speed']:.2f}x实时"
        )
    print("  吞吐与产物:")
    print(f"    文本长度          : {text_len}有效字符（原始{raw_text_len}字符）")
    print(
        f"    单请求字符处理速率: {chars_per_sec:.1f}字符/s"
        if chars_per_sec is not None
        else "    单请求字符处理速率: N/A"
    )
    print(f"    文件大小          : {audio_kb:.1f}KB")
    if metrics.get("audio_data_bytes"):
        print(
            "    纯PCM数据大小    : "
            f"{metrics['audio_data_bytes'] / 1024:.1f}KB"
        )
    print("  判定参考:")
    ttft = metrics.get("ttft")
    if ttft is None:
        ttft_result = "N/A"
    elif metrics.get("success") and ttft <= ttft_threshold:
        ttft_result = "PASS"
    else:
        ttft_result = "FAIL"
    print(f"    TTFT参考阈值      : {ttft_threshold:.3f}s ({ttft_result})")
    rtf_description = (
        "JSON元数据模式未收到音频字节，不能验证实际时长，因此不计算RTF"
        if metrics.get("response_mode") == "json_metadata"
        else "当前为客户端端到端RTF，包含网关、网络和本地接收开销"
    )
    print(f"    RTF说明           : {rtf_description}")
    if metrics.get("timing_diagnosis"):
        print(f"    计时诊断           : {metrics['timing_diagnosis']}")
    print("=" * 60)


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return parsed


def _validate_args(args) -> None:
    if not args.text or not args.text.strip():
        raise ValueError("--text must not be empty")
    if len(args.text) > args.max_text_chars:
        raise ValueError(
            f"text length {len(args.text)} exceeds --max_text_chars={args.max_text_chars}"
        )
    if not 0.25 <= args.speed <= 4.0:
        raise ValueError("--speed must be in the range [0.25, 4]")
    if args.max_new_tokens is not None and not (
        MIN_MAX_NEW_TOKENS <= args.max_new_tokens <= MAX_MAX_NEW_TOKENS
    ):
        raise ValueError(
            f"--max_new_tokens must be in the range "
            f"[{MIN_MAX_NEW_TOKENS}, {MAX_MAX_NEW_TOKENS}]"
        )
    if args.stream and args.stream_format != "audio":
        raise ValueError("--stream_format sse is not supported by this script")
    if args.sample_rate != 24000:
        raise ValueError("--sample_rate must be 24000")
    if not args.res_content:
        raise ValueError(
            "--res_content must be true: accurate duration/TTFT/RTF metrics require "
            "the gateway to return actual WAV bytes"
        )
    if args.response_format.lower() != "wav":
        raise ValueError("--response_format must be wav for PCM/WAV metric calculation")
    if not 1 <= args.chunk_size <= 1024 * 1024:
        raise ValueError("--chunk_size must be between 1 and 1048576 bytes")
    if args.max_response_bytes > 0xFFFFFFFF - 8:
        raise ValueError("--max_response_bytes exceeds the RIFF 32-bit size limit")
    if args.min_audio_bytes >= args.max_response_bytes:
        raise ValueError("--min_audio_bytes must be smaller than --max_response_bytes")
    if args.total_timeout < args.connect_timeout:
        raise ValueError("--total_timeout must be greater than or equal to --connect_timeout")

    output_path = os.path.abspath(args.output)
    if os.path.isdir(output_path):
        raise ValueError(f"--output points to a directory: {output_path}")
    output_dir = os.path.dirname(output_path) or os.getcwd()
    os.makedirs(output_dir, exist_ok=True)
    if not os.access(output_dir, os.W_OK):
        raise ValueError(f"output directory is not writable: {output_dir}")
    free_bytes = shutil.disk_usage(output_dir).free
    if free_bytes < args.max_response_bytes:
        raise ValueError(
            "insufficient free disk space for configured response limit: "
            f"free={free_bytes}, required={args.max_response_bytes}"
        )
    args.output = output_path


def parse_args():
    parser = argparse.ArgumentParser(description="Test AI Gateway /predict endpoint for TTS")
    parser.add_argument(
        "--text",
        type=str,
        default="""
        清晨五点四十分，城市还没有完全醒来。薄薄的雾像一层半透明的纱，轻轻盖在街道、屋顶和树梢上。路灯一盏一盏地熄灭，仿佛有人沿着长长的马路，把夜晚最后的星火收进衣袖。林知远推开窗户，冷空气涌进来，带着湿润的泥土味和远处早餐铺的油条香。他深吸一口气，对自己说：“今天，要把那段语音合成程序调试好。”
林知远是一名语音算法工程师。过去三个月，他和团队一直在训练一个全新的中文语音合成模型。模型能学会不同人的音色，也能模仿不同的情绪：平静、欢快、悲伤、严肃、温柔，甚至还能在句子里加入恰到好处的停顿。可越是接近完成，他越觉得心里没底。因为文字是安静的，声音却是有温度的；屏幕上的一行行符号，要变成一个人耳中自然、流畅、富有情感的话语，中间隔着无数细节。
他坐到书桌前，打开电脑，屏幕上跳出一份测试稿。测试稿的标题叫《给TTS的一封长信》。他笑了笑，心想，这名字倒是贴切。语音合成，英文叫Text-to-Speech，简称TTS。把文字变成声音，看似简单，其实要面对许多挑战：多音字、数字、日期、英文缩写、标点停顿、语气变化、长句呼吸、专有名词、轻声儿化，还有那些只可意会不可言传的情绪。
他点下播放键，房间里响起一个温和的女声。第一段是：“2026年9月22日，星期二，农历八月初一，气温23.5℃，湿度68%，东南风3到4级。请注意，数字、日期、温度、百分比和风向都要读得自然。”声音读得很准确，但“23.5℃”稍微有点生硬，像机器人在念实验报告。林知远在笔记本上写下：温度符号前后需要更顺滑。
第二段是：“他的银行卡里还剩123,456.78元，可他却说：‘没关系，钱乃身外之物。’”这一次，声音把“123,456.78”读成了“十二万三千四百五十六点七八”，金额单位也处理得不错。林知远点点头，又写下：大额数字没问题，但语气还可以更轻松。
第三段是：“银行行长在行里行走，行行出状元，但行行都辛苦。”这句话里，“银行”的“行”读háng，“行走”的“行”读xíng，“行行出状元”的“行行”读háng háng。女声犹豫了半秒，还是读对了。林知远松了口气。多音字是中文TTS的老大难，一个“行”字，就能让模型原形毕露。
第四段是：“你好，Hello，Bonjour，こんにちは，안녕하세요。”中文、英文、法文、日文、韩文混在一起，像一场小小的联合国会议。女声用标准普通话读了“你好”，又用略带口音的英文读了“Hello”，后面的法文、日文和韩文则有些含糊。林知远没有苛责，他知道，多语言混合本来就不容易。
第五段是：“他说：‘我不知道……真的不知道。’她问：‘你确定吗？’他答：‘确定！百分之百确定！’”这段测试的是标点和情绪。省略号要停顿，问号要上扬，感叹号要加重。女声处理得不错，尤其是最后一句，竟然带了一点倔强。林知远忍不住笑了一下。
第六段是绕口令：“四是四，十是十，十四是十四，四十是四十；谁能说准四十、十四、四十四，谁来试一试。”女声读得字正腔圆，但速度稍快，像在赶时间。林知远想，也许应该加一个“慢速模式”，让用户自己调节。
第七段是描写：“蓝蓝的天上白云飘，白云下面马儿跑。风吹草低见牛羊，天苍苍，野茫茫。”这一次，声音变得柔和、悠远，仿佛真的有一阵风从草原吹来。林知远闭上眼睛，脑海里浮现出辽阔的天地。他忽然明白，好的语音合成不只是读对字，还要让人听见画面。
第八段是标点测试：“逗号，句号。问号？感叹号！分号；冒号：顿号、引号“”‘’、括号（）、破折号——、省略号……、书名号《》、间隔号·、连接号-。”这段文字几乎没有情感，却像一把精密的手术刀，切开了语音合成的每一个停顿。女声一一读过，偶尔在破折号和省略号处停顿过长。林知远记录：长停顿需要根据上下文动态调整。
他继续听下去。测试稿里还有一段关于“重音”的句子：“我知道你会来，我知道你不会来，我知道你一定会来。”同样的字，重音不同，意思就完全不同。女声第一次把重音放在“知道”，第二次放在“不会”，第三次放在“一定”。林知远觉得，这已经不只是技术，更像表演。
接着是“儿化音”测试：“今天天气不错，咱们去公园玩儿，顺便买点儿水果，吃点儿小吃，再喝点儿汽水儿。”女声读得自然，但“汽水儿”稍微有点刻意。林知远写下：儿化音要轻，不能太重。
然后是“轻声”测试：“妈妈，爸爸，哥哥，姐姐，弟弟，妹妹，桌子，椅子，房子，车子，包子，饺子。”女声读得温柔，像在和一个孩子说话。林知远想，如果这段声音用在有声书里，应该很合适。
还有“数字”测试：“1月1日，星期一，1点11分，111路公交车，1111号房间，电话是010-8888-6666，邮箱是tts_test@example.com，网址是www.example.com。”这段里，数字、日期、时间、电话号码、邮箱和网址混在一起。女声读得基本准确，但“www”读成了“达不溜达不溜达不溜”，有点滑稽。林知远笑了，写下：网址读法需要可配置。
最后，测试稿的结尾是一段抒情文字：
“声音是时间的形状。它从一个人的唇齿之间出发，穿过空气，落在另一个人的耳朵里，然后变成记忆。有人听见母亲在厨房里喊吃饭，有人听见老师在黑板上写字，有人听见恋人在电话里说晚安。文字是沉默的，但声音让它醒来。我们希望机器学会说话，不是为了代替人，而是为了帮助人，让看不见的人听见世界，让远行的人听见故乡，让遗忘的故事重新被讲述。”
女声读完最后一句，房间里安静了几秒。林知远坐在椅子上，没有立刻动。他忽然觉得，自己做的这件事，比想象中更重要。屏幕上的波形还在轻轻起伏，像一条小小的河流。河流里流淌的，是文字，也是人心。
他合上笔记本，望向窗外。天已经亮了，早餐铺前排起了队，孩子们背着书包跑过斑马线，公交车缓缓进站。城市发出了自己的声音：喇叭声、脚步声、笑声、风声、鸟鸣声，还有无数说不清道不明的细小声响。林知远想，也许有一天，TTS会把这些声音也装进文字里，让每一个句子都有呼吸，让每一个停顿都有意义。
他重新点下播放键，决定再听一遍。这一次，他不再只关注错误，而是闭上眼睛，认真倾听。他听见数字在跳舞，标点在呼吸，多音字在捉迷藏，儿化音在轻轻打转。他听见一个声音，正在努力变得像人一样自然。而他知道，这条路还很长，但每一次测试，都是向前的一小步。
“你好，世界。”他轻声说。然后，屏幕里的女声也轻轻回答：“你好，世界。”
        """.strip(),
        help="Text to synthesize",
    )
    parser.add_argument(
        "--instructions",
        "--instruct_text",
        dest="instructions",
        type=str,
        default="像日常交流一样平和自然地朗读，语速稳定，不要夸张重读。",
        help="Voice/style instructions for the speech endpoint",
    )
    parser.add_argument(
        "--voice",
        "--zero_shot_spk_id",
        dest="voice",
        type=str,
        default="serena",
        help="Voice name supported by the speech endpoint",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=TTS_MODEL,
        help="Speech model name",
    )
    parser.add_argument(
        "--component_code",
        "--component-code",
        dest="component_code",
        type=str,
        default=TTS_COMPONENTCODE,
        help="Southgrid gateway componentCode",
    )
    parser.add_argument(
        "--task_type",
        type=str,
        default="CustomVoice",
        choices=["CustomVoice"],
        help="Speech task type",
    )
    parser.add_argument(
        "--language",
        type=str,
        default="Chinese",
        choices=[
            "Auto", "Chinese", "English", "French", "German", "Italian",
            "Japanese", "Korean", "Portuguese", "Russian", "Spanish",
            "Beijing_Dialect", "Sichuan_Dialect",
        ],
    )
    parser.add_argument("--prompt_audio", type=str, default="kehu_female_b")
    parser.add_argument("--prompt_text", type=str, default="")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--stream", type=str2bool, default=False)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--max_new_tokens",
        type=int,
        default=None,
        help="Maximum generated tokens (4-4096); omitted auto-calculates from text length",
    )
    parser.add_argument("--sample_rate", type=int, default=24000, choices=[24000])
    parser.add_argument(
        "--stream_format",
        type=str,
        default="audio",
        choices=["audio", "sse"],
    )
    parser.add_argument("--split", type=str2bool, default=True)
    parser.add_argument("--background_audio", type=str, default="")
    parser.add_argument("--background_volume", type=float, default=0.0)
    parser.add_argument("--background_loop", type=str2bool, default=True)
    parser.add_argument("--text_frontend", type=str2bool, default=True)
    parser.add_argument("--res_content", type=str2bool, default=True)
    parser.add_argument("--response_format", type=str, default="wav", choices=["wav"])
    parser.add_argument(
        "--playback",
        type=str2bool,
        default=False,
        help="Play received audio through the default device (disabled by default for clean timing)",
    )
    parser.add_argument("--target_func", type=str, default="instruct2", choices=["zero_shot", "instruct2", "cross_lingual"])
    parser.add_argument("--url", type=str, default=TTS_BINDING_HOST)
    parser.add_argument("--output", type=str, default="gateway_test_output.wav")
    parser.add_argument("--chunk_size", type=_positive_int, default=1024)
    parser.add_argument("--connect_timeout", type=_positive_float, default=1800)
    parser.add_argument("--read_timeout", type=_positive_float, default=1800)
    parser.add_argument("--total_timeout", type=_positive_float, default=1800)
    parser.add_argument(
        "--max_response_bytes",
        type=_positive_int,
        default=DEFAULT_MAX_RESPONSE_BYTES,
    )
    parser.add_argument(
        "--max_audio_seconds",
        type=_positive_float,
        default=DEFAULT_MAX_AUDIO_SECONDS,
    )
    parser.add_argument(
        "--min_audio_bytes",
        type=_positive_int,
        default=DEFAULT_MIN_AUDIO_BYTES,
    )
    parser.add_argument(
        "--playback_queue_chunks",
        type=_positive_int,
        default=DEFAULT_PLAYBACK_QUEUE_CHUNKS,
    )
    parser.add_argument(
        "--playback_join_timeout",
        type=_positive_float,
        default=DEFAULT_PLAYBACK_JOIN_TIMEOUT,
    )
    parser.add_argument("--ttft_threshold", type=_positive_float, default=0.35)
    parser.add_argument("--max_text_chars", type=_positive_int, default=20000)
    args = parser.parse_args()
    # 保留旧属性名，便于调用方在迁移期间继续读取命令行配置。
    args.instruct_text = args.instructions
    args.zero_shot_spk_id = args.voice
    try:
        _validate_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main() -> int:
    args = parse_args()

    print(f"Sending request to {args.url}...")
    print(f"text: {args.text[:40]}...")
    print(f"model: {args.model}")
    print(f"componentCode: {args.component_code}")
    print(f"voice: {args.voice}")
    print(
        "metric request params: "
        f"stream={args.stream}, res_content=True, "
        f"response_format={'pcm' if args.stream else args.response_format}, "
        f"playback={args.playback}"
    )

    metrics = request_tts(args)

    scene_name = f"{args.task_type}语音字幕同步"
    print_tts_key_metrics(scene_name, metrics, args.text, args.ttft_threshold)
    test_success = bool(
        metrics.get("success") and metrics.get("audio_artifact_available")
    )
    return 0 if test_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
