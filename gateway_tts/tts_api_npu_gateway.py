import argparse
import base64
import binascii
import hmac
import json
import os
import queue
import shutil
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from http.client import IncompleteRead
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from urllib3.exceptions import ProtocolError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# #省测-环境
# TTS_BINDING = "southgrid"
# TTS_MODEL = "TTS-v1"
# TTS_BINDING_HOST = os.getenv("TTS_BINDING_HOST", "https://10.134.252.232:5030/ai-gateway/predict")
# TTS_BINDING_API_KEY = os.getenv("TTS_BINDING_API_KEY", "b899eef382324e8d8973493fb9c35998")
# TTS_CUSTCODE = os.getenv("TTS_CUSTCODE", "1000400672300031")
# TTS_COMPONENTCODE = os.getenv("TTS_COMPONENTCODE", "04351372")

# #网级-生产环境
# TTS_BINDING = "southgrid"
# TTS_MODEL = "TTS-v1"
# TTS_BINDING_HOST = os.getenv("TTS_BINDING_HOST", "https://10.10.65.213:18300/ai-inference-gateway/predict")
# TTS_BINDING_API_KEY = os.getenv("TTS_BINDING_API_KEY", "24e74daf74124b0b96c9cb113162a976")
# TTS_CUSTCODE = os.getenv("TTS_CUSTCODE", "1001300033")
# TTS_COMPONENTCODE = os.getenv("TTS_COMPONENTCODE", "04100945")

#网级-测试环境
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
        stream = p.open(format=sample_format, channels=channels, rate=sample_rate, output=True)
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
                    with state_lock:
                        playback_state["error"] = (
                            "playback received incomplete PCM frame: "
                            f"{len(pending)} trailing bytes"
                        )
                    stop_event.set()
                break
            pending.extend(data)
            aligned_size = len(pending) - (len(pending) % (channels * sample_width))
            if aligned_size <= 0:
                continue
            aligned_data = bytes(pending[:aligned_size])
            del pending[:aligned_size]
            write_started_at = time.perf_counter()
            stream.write(aligned_data)
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


def build_headers():
    x_date, authorization = getLocalAuthInfo(TTS_CUSTCODE, TTS_BINDING_API_KEY)
    return {
        "x-date": x_date,
        "authorization": authorization,
        "Content-Type": "application/json",
        "Accept": "application/octet-stream, audio/wav",
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache",
    }


def build_payload(args):
    return {
        "componentCode": TTS_COMPONENTCODE,
        "model": TTS_MODEL,
        "function": args.target_func,
        "tts_params": {
            "input_text": args.text,
            "speaker_id": args.zero_shot_spk_id,
            "prompt_audio": args.prompt_audio,
            "prompt_text": getattr(args, "prompt_text", ""),
            "instruct_text": args.instruct_text if args.target_func == "instruct2" else "",
            "stream": args.stream,
            "speed": args.speed,
            # 与 instruct2_npu_new.py 保持一致；除 stream 外，两种模式使用
            # 完全相同的 TTS 参数，确保性能结果可以直接对比。
            "background_audio": getattr(args, "background_audio", ""),
            "background_volume": getattr(args, "background_volume", 0.0),
            "background_loop": getattr(args, "background_loop", True),
            "text_frontend": getattr(args, "text_frontend", True),
            "seed": getattr(args, "seed", 0),
            "split": getattr(args, "split", True),
            # 性能指标必须基于真实 WAV/PCM 字节，禁止只返回服务端文件元数据。
            "res_content": True,
            "response_format": "wav",
        },
    }


def _read_response_chunk(response, size):
    """从 urllib3 原始响应读取已解码字节，并保持 requests 的异常语义。"""
    try:
        return response.raw.read(size, decode_content=True)
    except urllib3.exceptions.ReadTimeoutError as exc:
        raise requests.exceptions.ReadTimeout(str(exc)) from exc
    except urllib3.exceptions.ProtocolError as exc:
        raise requests.exceptions.ConnectionError(str(exc)) from exc


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


def request_tts(args) -> dict:
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

    playback_queue_capacity = min(
        args.playback_queue_chunks,
        max(1, MAX_PLAYBACK_BUFFER_BYTES // args.chunk_size),
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

    start_time = time.perf_counter()

    try:
        request_timeout = urllib3.util.Timeout(
            total=args.total_timeout,
            connect=min(args.connect_timeout, args.total_timeout),
            read=min(args.read_timeout, args.total_timeout),
        )
        response = requests.post(
            args.url,
            json=build_payload(args),
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
                                                    "playback queue is full; playback stopped "
                                                    "to avoid distorting RT"
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
                                try:
                                    audio_queue.put_nowait(
                                        audio_payload[offset:offset + args.chunk_size]
                                    )
                                except queue.Full:
                                    playback_enabled = False
                                    playback_stop.set()
                                    metrics["playback_error"] = (
                                        "playback queue is full; playback stopped to avoid distorting RT"
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
                                            "playback queue is full; playback stopped to avoid distorting RT"
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
                    try:
                        audio_queue.put(None, timeout=0.5)
                    except queue.Full:
                        playback_stop.set()
                player_thread.join(timeout=args.playback_join_timeout)
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
    if metrics.get("service_request_id"):
        print(f"  服务请求ID        : {metrics['service_request_id']}")
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
    if not 0 < args.speed <= 4.0:
        raise ValueError("--speed must be in the range (0, 4]")
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
        default=(
        "认知的套利者，在这个世界大有搞头的逻辑里，最高级的财富，是你的选择权，它是智力资本在时间复利中的悄然绽放，它是认知高地对低洼地带的温柔俯瞰。当别人在存量博弈里拼刺刀，你已在 this is the begin 正确非共识 this is the end 的无人区，种下了属于未来的森林。自由的代价，从来不是不被强迫，而是你看得见，万千条通往星辰的隐秘路径。"
        ),
        help="Text to synthesize",
    )
    parser.add_argument("--instruct_text", type=str, default="You are a helpful assistant. 很自然地说<|endofprompt|>")
    parser.add_argument("--zero_shot_spk_id", type=str, default="kehu_female_b")
    parser.add_argument("--prompt_audio", type=str, default="kehu_female_b")
    parser.add_argument("--prompt_text", type=str, default="")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--stream", type=str2bool, default=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--split", type=str2bool, default=True)
    parser.add_argument("--background_audio", type=str, default="")
    parser.add_argument("--background_volume", type=float, default=0.0)
    parser.add_argument("--background_loop", type=str2bool, default=True)
    parser.add_argument("--text_frontend", type=str2bool, default=True)
    parser.add_argument("--res_content", type=str2bool, default=True)
    parser.add_argument("--response_format", type=str, default="wav", choices=["wav"])
    parser.add_argument("--playback", type=str2bool, default=False)
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
    try:
        _validate_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main() -> int:
    args = parse_args()

    print(f"Sending request to {args.url}...")
    print(f"text: {args.text[:40]}...")
    print(f"target_func (model): {args.target_func}")
    print(
        "metric request params: "
        f"stream={args.stream}, res_content=True, response_format=wav, "
        f"playback={args.playback}"
    )

    metrics = request_tts(args)

    scene_name = f"{args.target_func.capitalize()}语音字幕同步"
    print_tts_key_metrics(scene_name, metrics, args.text, args.ttft_threshold)
    return 0 if metrics.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
