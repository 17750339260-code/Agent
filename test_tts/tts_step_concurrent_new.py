# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import glob
import json
import math
import os
import random
import signal
import sys
import threading
import time
import traceback
import wave
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import urllib3
from requests.adapters import HTTPAdapter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

URL = "http://36.111.82.53:10015/api/tts/instruct2"
DEFAULT_CONCURRENT_LEVELS = [1, 2, 4, 8, 16, 32]
DEFAULT_OUTPUT_DIR = "tts_output"

DEFAULT_AUDIO_SAMPLE_RATE = 24000
DEFAULT_AUDIO_CHANNELS = 1
DEFAULT_AUDIO_SAMPLE_WIDTH = 2
MIN_VALID_SAMPLE_RATE = 8000
MAX_VALID_SAMPLE_RATE = 192000
MAX_REASONABLE_AUDIO_SECONDS = 7200
MAX_HEADER_SIZE_DURATION_DRIFT = 0.05
MAX_WAV_PREFIX_BYTES = 4096

RUNNING = True
SESSION_LOCAL = threading.local()


def graceful_exit(signum: Optional[int] = None, frame: object = None) -> None:
    global RUNNING
    RUNNING = False
    print("\n[STOP] 收到退出信号：当前 burst 会尽量完成，后续阶梯将停止。\n")


signal.signal(signal.SIGINT, graceful_exit)
signal.signal(signal.SIGTERM, graceful_exit)


class TTSResponseError(Exception):
    """Raised when the TTS service returns invalid or unusable audio."""


@dataclass
class RequestResult:
    concurrency: int
    model_pool_size: int
    request_id: int
    burst_id: int
    success: bool
    status_code: Optional[int]
    error: str
    model: str
    text_len: int
    text_preview: str
    start_epoch: float
    end_epoch: float
    send_perf: float
    total_ms: float
    http_started: bool = False
    http_start_perf: float = 0.0
    http_total_ms: Optional[float] = None
    model_wait_ms: Optional[float] = None
    first_byte_ms: Optional[float] = None
    first_audio_ms: Optional[float] = None
    audio_duration: Optional[float] = None
    audio_duration_source: str = ""
    rtf: Optional[float] = None
    output_bytes: int = 0
    response_bytes: int = 0
    save_path: str = ""


@dataclass(frozen=True)
class WavFormatInfo:
    sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE
    channels: int = DEFAULT_AUDIO_CHANNELS
    sample_width: int = DEFAULT_AUDIO_SAMPLE_WIDTH
    data_offset: Optional[int] = None
    declared_data_bytes: Optional[int] = None


@dataclass
class StepResult:
    concurrency: int
    model_pool_size: int
    burst_rounds: int
    attempted_requests: int
    completed_requests: int
    success_count: int
    failed_count: int
    success_rate: float
    total_duration_s: float
    effective_duration_s: float
    wall_window_duration_s: float
    idle_between_bursts_s: float
    success_qps: float
    total_qps: float
    success_qps_wall: float
    total_qps_wall: float
    http_sent_count: int
    http_qps: float
    configured_concurrency: int
    observed_peak_inflight: int
    full_concurrency_bursts: int
    avg_response_ms: Optional[float]
    p50_response_ms: Optional[float]
    p90_response_ms: Optional[float]
    p95_response_ms: Optional[float]
    p99_response_ms: Optional[float]
    min_response_ms: Optional[float]
    max_response_ms: Optional[float]
    all_avg_response_ms: Optional[float]
    all_p50_response_ms: Optional[float]
    all_p90_response_ms: Optional[float]
    all_p95_response_ms: Optional[float]
    all_p99_response_ms: Optional[float]
    all_min_response_ms: Optional[float]
    all_max_response_ms: Optional[float]
    http_avg_response_ms: Optional[float]
    http_p50_response_ms: Optional[float]
    http_p90_response_ms: Optional[float]
    http_p95_response_ms: Optional[float]
    http_p99_response_ms: Optional[float]
    http_min_response_ms: Optional[float]
    http_max_response_ms: Optional[float]
    avg_ttfb_ms: Optional[float]
    p95_ttfb_ms: Optional[float]
    avg_ttft_ms: Optional[float]
    p95_ttft_ms: Optional[float]
    avg_rtf: Optional[float]
    p95_rtf: Optional[float]
    min_rtf: Optional[float]
    max_rtf: Optional[float]
    audio_total_duration_s: float
    avg_audio_duration_s: Optional[float]
    p95_audio_duration_s: Optional[float]
    audio_throughput: float
    audio_throughput_wall: float
    total_output_bytes: int
    total_response_bytes: int
    error_summary: dict[str, int] = field(default_factory=dict)


class InflightCounter:
    def __init__(self) -> None:
        self.current = 0
        self.peak = 0
        self.lock = threading.Lock()

    def enter(self) -> None:
        with self.lock:
            self.current += 1
            self.peak = max(self.peak, self.current)

    def leave(self) -> None:
        with self.lock:
            self.current -= 1


class PeakTracker:
    def __init__(self) -> None:
        self.peak = 0

    def observe(self, value: int) -> None:
        self.peak = max(self.peak, value)


class StartGate:
    def __init__(self, target_ready: int) -> None:
        self.target_ready = target_ready
        self.ready = 0
        self.aborted = False
        self.abort_reason = ""
        self.condition = threading.Condition()
        self.event = threading.Event()

    def ready_and_wait(self) -> tuple[bool, str]:
        with self.condition:
            self.ready += 1
            self.condition.notify_all()
        self.event.wait()
        return not self.aborted, self.abort_reason

    def wait_until_ready(self, timeout: float = 30.0) -> bool:
        deadline = time.perf_counter() + timeout
        with self.condition:
            while self.ready < self.target_ready:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
            return True

    def release(self) -> None:
        self.event.set()

    def abort(self, reason: str) -> None:
        with self.condition:
            self.aborted = True
            self.abort_reason = reason
            self.condition.notify_all()
        self.event.set()


class TextGenerator:
    def __init__(self) -> None:
        self.short_texts = [
            "今天是个好天气，适合做一次短文本语音合成压测。",
            "您好，欢迎使用智能语音服务，请保持电话畅通。",
            "请确认您的业务信息，系统将继续为您办理。",
        ]
        self.medium_texts = [
            (
                "通知用户查询银行卡账单，确认退费是否已经到账。若未到账，应向客户解释说明办理流程，"
                "并提示客户留意银行短信、电子账单和账户余额变化。"
            ),
            (
                "每个收费员在当班收费完毕后，必须对现金、支票、POS 机交易回单和电子票据存根进行清点，"
                "形成报表并按日结要求提交给负责人。"
            ),
            (
                "窗口人员接到业务审批完成信息后，应核对客户身份材料、银行卡信息和申请单内容，"
                "确保资料一致后再进入后续处理环节。"
            ),
        ]
        self.long_texts = [
            (
                "在前两节课里，我们学习了培训教材规划和培训基地规划。本节课将从概念、工具和关注点三个方面，"
                "了解培训师资规划的有关内容。培训师资规划是指依据企业发展战略和新业务发展需求，结合师资现状"
                "和师资需求分析结果，对企业师资队伍的选用、培养、预留进行整体规划，并制定可操作、可实施的"
                "发展方案。一般来说，师资规划需要关注师资数量、专业结构、课程覆盖、授课质量以及后续培养路径。"
            ),
            (
                "在受理客户退费申请时，工作人员应收集退费所属用电期间的电费发票、客户身份证明、银行卡信息"
                "以及其他必要依据。对于居民客户或非居民中的自然人，需客户在退费审批表中签字确认；对于企业客户，"
                "还应收集营业执照、法人身份证明、银行账户信息等材料。所有资料核验无误后，才能进入追收或退款流程。"
            ),
            (
                "稳定性压测不只是观察请求是否成功，还要关注服务在压力上升时的响应时间、首包时间、音频生成速度"
                "和错误类型变化。对于 TTS 系统而言，单纯比较总耗时容易受到文本长度影响，因此需要同时统计音频时长"
                "和实时率 RTF。RTF 越低，说明系统生成同样时长音频所需时间越短；当并发升高后，如果成功率下降、"
                "全量 P95 明显变长或 RTF 快速恶化，就说明系统可能接近容量拐点。"
            ),
        ]

    def get_random_text(self, exclude: Optional[str] = None) -> str:
        candidates: list[str]
        rand = random.random()
        if rand < 0.2:
            candidates = self.short_texts
        elif rand < 0.4:
            candidates = self.medium_texts
        else:
            candidates = self.long_texts
        filtered = [item for item in candidates if item != exclude]
        return random.choice(filtered or candidates)


def create_session(pool_size: int) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.proxies = {}
    adapter = HTTPAdapter(max_retries=0, pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_session(pool_size: int) -> requests.Session:
    session = getattr(SESSION_LOCAL, "session", None)
    current_size = getattr(SESSION_LOCAL, "pool_size", None)
    if session is None or current_size != pool_size:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        SESSION_LOCAL.session = create_session(pool_size)
        SESSION_LOCAL.pool_size = pool_size
    return SESSION_LOCAL.session


def percentile(values: list[float], pct: float, method: str = "nearest_rank") -> Optional[float]:
    if not values:
        return None
    if pct < 0 or pct > 100:
        raise ValueError("percentile must be in [0, 100]")
    ordered = sorted(values)
    if method == "nearest_rank":
        if pct == 0:
            return ordered[0]
        index = math.ceil(len(ordered) * pct / 100) - 1
        return ordered[min(max(index, 0), len(ordered) - 1)]
    if method != "linear":
        raise ValueError("percentile method must be 'nearest_rank' or 'linear'")
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def average(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def format_ms(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.2f}ms"


def format_number(value: Optional[float], suffix: str = "", digits: int = 2) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}{suffix}"


def str2bool(value: str) -> bool:
    lowered = value.lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean value: {value}")


def read_text_arg(value: str) -> str:
    if value.startswith("@"):
        return Path(value[1:]).read_text(encoding="utf-8")
    return value


def duration_from_size(byte_count: int, sample_rate: int, channels: int, sample_width: int) -> float:
    bytes_per_second = sample_rate * channels * sample_width
    return byte_count / bytes_per_second if byte_count > 0 and bytes_per_second > 0 else 0.0


def estimate_audio_duration_by_size(file_path: Path) -> float:
    audio_bytes = max(file_path.stat().st_size - 44, 0)
    return duration_from_size(
        audio_bytes,
        DEFAULT_AUDIO_SAMPLE_RATE,
        DEFAULT_AUDIO_CHANNELS,
        DEFAULT_AUDIO_SAMPLE_WIDTH,
    )


def find_wav_chunk(audio_bytes: bytes, chunk_id: bytes) -> Optional[tuple[int, int, int]]:
    if len(chunk_id) != 4 or len(audio_bytes) < 12:
        return None
    if audio_bytes[:4] != b"RIFF" or audio_bytes[8:12] != b"WAVE":
        return None

    offset = 12
    while offset + 8 <= len(audio_bytes):
        current_id = audio_bytes[offset:offset + 4]
        chunk_size = int.from_bytes(audio_bytes[offset + 4:offset + 8], "little", signed=False)
        data_start = offset + 8
        if current_id == chunk_id:
            return offset, data_start, chunk_size
        next_offset = data_start + chunk_size + (chunk_size % 2)
        if next_offset <= offset or next_offset > len(audio_bytes):
            break
        offset = next_offset
    return None


def parse_wav_format_prefix(audio_bytes: bytes) -> WavFormatInfo:
    if len(audio_bytes) < 12 or audio_bytes[:4] != b"RIFF" or audio_bytes[8:12] != b"WAVE":
        return WavFormatInfo()

    sample_rate = DEFAULT_AUDIO_SAMPLE_RATE
    channels = DEFAULT_AUDIO_CHANNELS
    sample_width = DEFAULT_AUDIO_SAMPLE_WIDTH

    fmt_chunk = find_wav_chunk(audio_bytes, b"fmt ")
    if fmt_chunk:
        _, fmt_start, fmt_size = fmt_chunk
        if fmt_size >= 16 and fmt_start + 16 <= len(audio_bytes):
            parsed_channels = int.from_bytes(audio_bytes[fmt_start + 2:fmt_start + 4], "little", signed=False)
            parsed_rate = int.from_bytes(audio_bytes[fmt_start + 4:fmt_start + 8], "little", signed=False)
            bits_per_sample = int.from_bytes(audio_bytes[fmt_start + 14:fmt_start + 16], "little", signed=False)
            if 1 <= parsed_channels <= 8:
                channels = parsed_channels
            if MIN_VALID_SAMPLE_RATE <= parsed_rate <= MAX_VALID_SAMPLE_RATE:
                sample_rate = parsed_rate
            if 8 <= bits_per_sample <= 32 and bits_per_sample % 8 == 0:
                sample_width = bits_per_sample // 8

    data_chunk = find_wav_chunk(audio_bytes, b"data")
    if data_chunk:
        _, data_offset, data_size = data_chunk
        return WavFormatInfo(sample_rate, channels, sample_width, data_offset, data_size)

    return WavFormatInfo(sample_rate, channels, sample_width)


def get_audio_data_byte_count(audio_bytes: bytes, info: WavFormatInfo) -> int:
    if info.data_offset is None:
        return max(len(audio_bytes) - 44, 0)
    actual_data_bytes = max(len(audio_bytes) - info.data_offset, 0)
    if info.declared_data_bytes is not None and 0 < info.declared_data_bytes <= actual_data_bytes:
        return info.declared_data_bytes
    return actual_data_bytes


def is_reasonable_duration(duration: float) -> bool:
    return 0 < duration <= MAX_REASONABLE_AUDIO_SECONDS


def duration_drift_ok(header_duration: float, size_duration: float) -> bool:
    if size_duration <= 0:
        return True
    drift = abs(header_duration - size_duration) / max(size_duration, 0.001)
    return drift <= MAX_HEADER_SIZE_DURATION_DRIFT


def estimate_audio_duration_from_bytes(audio_bytes: bytes, info: WavFormatInfo) -> float:
    data_bytes = get_audio_data_byte_count(audio_bytes, info)
    return duration_from_size(data_bytes, info.sample_rate, info.channels, info.sample_width)


def get_wav_duration(file_path: Path, allow_size_estimate: bool = False) -> tuple[bool, float, str]:
    try:
        audio_bytes = file_path.read_bytes()
    except OSError:
        return False, 0.0, "read_failed"

    info = parse_wav_format_prefix(audio_bytes[:MAX_WAV_PREFIX_BYTES])
    size_duration = estimate_audio_duration_from_bytes(audio_bytes, info)
    header_duration = 0.0
    header_valid = False

    try:
        with wave.open(str(file_path), "rb") as file:
            frames = file.getnframes()
            frame_rate = file.getframerate()
            channels = file.getnchannels()
            sample_width = file.getsampwidth()
            header_duration = frames / frame_rate if frame_rate else 0.0
            header_valid = (
                frames > 100
                and MIN_VALID_SAMPLE_RATE <= frame_rate <= MAX_VALID_SAMPLE_RATE
                and 1 <= channels <= 8
                and 1 <= sample_width <= 4
                and is_reasonable_duration(header_duration)
            )
    except Exception:
        pass

    if header_valid and duration_drift_ok(header_duration, size_duration):
        return True, header_duration, "wav_header"

    if is_reasonable_duration(size_duration) and info.data_offset is not None:
        return True, size_duration, "wav_data_size"

    if not allow_size_estimate:
        return False, 0.0, "invalid_wav_header"

    fallback = size_duration if size_duration > 0 else estimate_audio_duration_by_size(file_path)
    return fallback > 0, fallback, "size_estimate"


def cleanup_tts_output(output_dir: Path, keep_wav_files: int) -> None:
    if keep_wav_files < 0:
        return
    files = [Path(item) for item in glob.glob(str(output_dir / "tts_c*.wav"))]
    if len(files) <= keep_wav_files:
        return
    files.sort(key=lambda path: path.stat().st_mtime)
    for file_path in files[:-keep_wav_files]:
        try:
            file_path.unlink()
        except OSError:
            pass


def normalize_error(error: str) -> str:
    if not error:
        return "Unknown"
    lowered = error.lower()
    if "model pool acquire timeout" in lowered:
        return "ModelPoolTimeout"
    if "read timed out" in lowered or "timeout" in lowered:
        return "Timeout"
    if "connection" in lowered:
        return "ConnectionError"
    if error.startswith("HTTP "):
        return error.split(":", 1)[0]
    return error.split(":", 1)[0][:80]


class TTSLadderTester:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.text_generator = TextGenerator()
        self.current_text: Optional[str] = None
        self.model_semaphore: threading.BoundedSemaphore = threading.BoundedSemaphore(1)
        self.current_model_pool_size = 1
        self.output_dir = Path(args.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def resolve_step_model_pool_size(self, concurrency: int) -> int:
        return max(1, self.args.model_pool_size or concurrency)

    def refresh_step_text(self) -> str:
        if self.args.text:
            self.current_text = read_text_arg(self.args.text)
        else:
            self.current_text = self.text_generator.get_random_text(self.current_text)
        return self.current_text

    def pick_request_text(self) -> str:
        if self.args.text:
            return read_text_arg(self.args.text)
        if self.args.random_per_request:
            return self.text_generator.get_random_text()
        return self.current_text or self.refresh_step_text()

    def make_payload(self, text: str) -> dict[str, object]:
        tts_params = {
            "instruct_text": self.args.instruct_text,
            "prompt_audio": self.args.prompt_audio,
            "zero_shot_spk_id": self.args.zero_shot_spk_id,
            "speed": self.args.speed,
            "stream": self.args.stream,
            "background_audio": self.args.background_audio,
            "background_volume": self.args.background_volume,
            "background_loop": self.args.background_loop,
            "text_frontend": self.args.text_frontend,
            "seed": self.args.seed,
            "split": self.args.split,
            "res_content": self.args.res_content,
            "text": text,
        }
        return {"model": self.args.model, "tts_params": tts_params}

    def send_request(
        self,
        request_id: int,
        concurrency: int,
        burst_id: int,
        start_gate: StartGate,
        inflight: InflightCounter,
    ) -> RequestResult:
        text = self.pick_request_text()
        payload = self.make_payload(text)
        start_epoch = 0.0
        start_perf = 0.0
        status_code: Optional[int] = None
        response: Optional[requests.Response] = None
        acquired_model = False
        entered_inflight = False
        model_wait_ms: Optional[float] = None
        http_started = False
        http_start_perf = 0.0
        save_path: Optional[Path] = None
        output_bytes = 0
        response_bytes = 0

        try:
            gate_open, gate_reason = start_gate.ready_and_wait()
            if not gate_open:
                return self._failure(
                    concurrency,
                    request_id,
                    burst_id,
                    start_epoch,
                    start_perf,
                    gate_reason or "StartGate aborted before request release",
                    status_code,
                    text,
                )
            start_epoch = time.time()
            start_perf = time.perf_counter()

            acquire_started = time.perf_counter()
            acquired_model = self.model_semaphore.acquire(timeout=self.args.model_acquire_timeout)
            model_wait_ms = (time.perf_counter() - acquire_started) * 1000
            if not acquired_model:
                return self._failure(
                    concurrency,
                    request_id,
                    burst_id,
                    start_epoch,
                    start_perf,
                    f"Model pool acquire timeout > {self.args.model_acquire_timeout:.2f}s",
                    status_code,
                    text,
                    model_wait_ms=model_wait_ms,
                )

            inflight.enter()
            entered_inflight = True
            headers = {"accept": "application/json", "Content-Type": "application/json"}
            socket_read_timeout = min(self.args.read_timeout, self.args.chunk_timeout)
            http_started = True
            http_start_perf = time.perf_counter()
            response = get_session(self.args.http_pool_size).post(
                self.args.url,
                json=payload,
                headers=headers,
                stream=True,
                verify=self.args.verify_ssl,
                timeout=(self.args.connect_timeout, socket_read_timeout),
            )
            status_code = response.status_code
            if status_code != 200:
                body = response.text[:500]
                response_bytes = len(response.content or b"")
                raise TTSResponseError(f"HTTP {status_code}: {body}")

            save_path = self.output_dir / (
                f"tts_c{concurrency}_b{burst_id}_r{request_id}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.wav"
            )
            first_byte_ms: Optional[float] = None
            first_audio_ms: Optional[float] = None
            last_chunk_perf = time.perf_counter()
            prefix_buffer = bytearray()
            wav_info = WavFormatInfo()

            with save_path.open("wb") as file:
                for chunk in response.iter_content(chunk_size=self.args.chunk_size):
                    now_perf = time.perf_counter()
                    if now_perf - last_chunk_perf > self.args.chunk_timeout:
                        raise TTSResponseError(f"Stream chunk timeout: no data for {self.args.chunk_timeout:.2f}s")
                    if not chunk:
                        continue
                    if first_byte_ms is None:
                        first_byte_ms = (now_perf - start_perf) * 1000
                    last_chunk_perf = now_perf
                    received_after = output_bytes + len(chunk)
                    if len(prefix_buffer) < MAX_WAV_PREFIX_BYTES:
                        prefix_room = MAX_WAV_PREFIX_BYTES - len(prefix_buffer)
                        prefix_buffer.extend(chunk[:prefix_room])
                        wav_info = parse_wav_format_prefix(bytes(prefix_buffer))
                    if first_audio_ms is None:
                        if wav_info.data_offset is not None and received_after > wav_info.data_offset:
                            first_audio_ms = (now_perf - start_perf) * 1000
                        elif not self.args.require_wav:
                            first_audio_ms = (now_perf - start_perf) * 1000
                    file.write(chunk)
                    output_bytes += len(chunk)
                    response_bytes += len(chunk)

            if output_bytes < self.args.min_audio_bytes:
                raise TTSResponseError(
                    f"Empty or too small audio: bytes={output_bytes}, "
                    f"content_type={response.headers.get('Content-Type', 'N/A')}, "
                    f"worker={response.headers.get('X-Worker-ID', 'N/A')}, "
                    f"pid={response.headers.get('X-Process-ID', 'N/A')}"
                )

            with save_path.open("rb") as audio_file:
                header = audio_file.read(12)
            if self.args.require_wav and not (header.startswith(b"RIFF") and header[8:12] == b"WAVE"):
                raise TTSResponseError(
                    f"Non-WAV response: bytes={output_bytes}, "
                    f"content_type={response.headers.get('Content-Type', 'N/A')}"
                )

            wav_valid, audio_duration, duration_source = get_wav_duration(
                save_path,
                allow_size_estimate=self.args.allow_duration_size_estimate,
            )
            if not wav_valid or audio_duration <= 0:
                raise TTSResponseError("Invalid audio duration")

            end_perf = time.perf_counter()
            end_epoch = time.time()
            total_ms = (end_perf - start_perf) * 1000
            http_total_ms = (end_perf - http_start_perf) * 1000 if http_start_perf > 0 else None
            rtf = (total_ms / 1000) / audio_duration if audio_duration > 0 else None
            return RequestResult(
                concurrency=concurrency,
                model_pool_size=self.current_model_pool_size,
                request_id=request_id,
                burst_id=burst_id,
                success=True,
                status_code=status_code,
                error="",
                model=self.args.model,
                text_len=len(text),
                text_preview=text[:80],
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                send_perf=start_perf,
                total_ms=total_ms,
                http_started=http_started,
                http_start_perf=http_start_perf,
                http_total_ms=http_total_ms,
                model_wait_ms=model_wait_ms,
                first_byte_ms=first_byte_ms,
                first_audio_ms=first_audio_ms,
                audio_duration=audio_duration,
                audio_duration_source=duration_source,
                rtf=rtf,
                output_bytes=output_bytes,
                response_bytes=response_bytes,
                save_path=str(save_path),
            )
        except requests.exceptions.Timeout as exc:
            return self._failure(
                concurrency, request_id, burst_id, start_epoch, start_perf, f"Timeout: {exc}",
                status_code, text, model_wait_ms=model_wait_ms, http_started=http_started,
                http_start_perf=http_start_perf, output_bytes=output_bytes,
                response_bytes=response_bytes, save_path=str(save_path or "")
            )
        except requests.exceptions.ConnectionError as exc:
            return self._failure(
                concurrency, request_id, burst_id, start_epoch, start_perf, f"Connection error: {exc}",
                status_code, text, model_wait_ms=model_wait_ms, http_started=http_started,
                http_start_perf=http_start_perf, output_bytes=output_bytes,
                response_bytes=response_bytes, save_path=str(save_path or "")
            )
        except Exception as exc:
            detail = str(exc) or exc.__class__.__name__
            if self.args.debug_errors:
                detail = f"{detail} | {traceback.format_exc()[:1000]}"
            return self._failure(
                concurrency, request_id, burst_id, start_epoch, start_perf, detail,
                status_code, text, model_wait_ms=model_wait_ms, http_started=http_started,
                http_start_perf=http_start_perf, output_bytes=output_bytes,
                response_bytes=response_bytes, save_path=str(save_path or "")
            )
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            if entered_inflight:
                inflight.leave()
            if acquired_model:
                self.model_semaphore.release()

    def _failure(
        self,
        concurrency: int,
        request_id: int,
        burst_id: int,
        start_epoch: float,
        start_perf: float,
        error: str,
        status_code: Optional[int],
        text: str,
        model_wait_ms: Optional[float] = None,
        http_started: bool = False,
        http_start_perf: float = 0.0,
        output_bytes: int = 0,
        response_bytes: int = 0,
        save_path: str = "",
    ) -> RequestResult:
        end_perf = time.perf_counter()
        started = start_perf > 0
        now_epoch = time.time()
        http_total_ms = (end_perf - http_start_perf) * 1000 if http_started and http_start_perf > 0 else None
        return RequestResult(
            concurrency=concurrency,
            model_pool_size=self.current_model_pool_size,
            request_id=request_id,
            burst_id=burst_id,
            success=False,
            status_code=status_code,
            error=error[:500],
            model=self.args.model,
            text_len=len(text),
            text_preview=text[:80],
            start_epoch=start_epoch if started else now_epoch,
            end_epoch=now_epoch,
            send_perf=start_perf if started else 0.0,
            total_ms=(end_perf - start_perf) * 1000 if started else 0.0,
            http_started=http_started,
            http_start_perf=http_start_perf if http_started else 0.0,
            http_total_ms=http_total_ms,
            model_wait_ms=model_wait_ms,
            output_bytes=output_bytes,
            response_bytes=response_bytes,
            save_path=save_path,
        )

    def run_step(self, concurrency: int, total_requests: int) -> tuple[StepResult, list[RequestResult]]:
        self.current_model_pool_size = self.resolve_step_model_pool_size(concurrency)
        self.model_semaphore = threading.BoundedSemaphore(self.current_model_pool_size)
        self.refresh_step_text()

        results: list[RequestResult] = []
        completed = 0
        scheduled = 0
        progress_every = max(1, total_requests // 10)
        burst_count = (total_requests + concurrency - 1) // concurrency
        peak_tracker = PeakTracker()

        print(f"\n{'=' * 80}")
        print(
            f"开始阶梯：并发={concurrency}, 模型池={self.current_model_pool_size}, "
            f"计划请求={total_requests}, 同步 burst={burst_count}, stream={self.args.stream}"
        )
        if self.args.random_per_request:
            print("文本策略：每个请求独立随机选择文本")
        elif self.args.text:
            print(f"文本策略：使用 --text 固定文本，长度={len(self.current_text or '')}")
        else:
            print(f"文本策略：本阶梯固定随机文本，长度={len(self.current_text or '')}")
        print(f"{'=' * 80}")

        start = time.perf_counter()
        next_request_id = 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            for burst_id in range(1, burst_count + 1):
                if not RUNNING:
                    print("检测到停止信号：不再启动新的 burst。")
                    break

                burst_size = min(concurrency, total_requests - scheduled)
                if burst_size <= 0:
                    break
                start_gate = StartGate(burst_size)
                inflight = InflightCounter()
                request_ids = range(next_request_id, next_request_id + burst_size)
                futures = [
                    executor.submit(self.send_request, request_id, concurrency, burst_id, start_gate, inflight)
                    for request_id in request_ids
                ]
                next_request_id += burst_size
                scheduled += burst_size

                gate_ready = start_gate.wait_until_ready(timeout=self.args.start_timeout)
                if not gate_ready:
                    reason = (
                        f"StartGate timeout: concurrency={concurrency}, burst={burst_id}, "
                        f"ready={start_gate.ready}/{burst_size}, timeout={self.args.start_timeout:.2f}s"
                    )
                    print(f"启动超时: {reason}")
                    start_gate.abort(reason)
                    for future in concurrent.futures.as_completed(futures):
                        result = future.result()
                        results.append(result)
                        completed += 1
                    peak_tracker.observe(inflight.peak)
                    break

                burst_start = time.perf_counter()
                print(f"释放 burst {burst_id}/{burst_count}: {burst_size} 个请求同时发起")
                start_gate.release()

                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    results.append(result)
                    completed += 1
                    if completed % progress_every == 0 or completed == scheduled:
                        ok_count = sum(1 for item in results if item.success)
                        print(f"进度: {completed}/{scheduled}, 当前成功率 {ok_count / completed * 100:.2f}%")

                peak_tracker.observe(inflight.peak)
                if burst_id != burst_count and self.args.burst_interval > 0:
                    elapsed = time.perf_counter() - burst_start
                    sleep_seconds = max(self.args.burst_interval - elapsed, 0.0)
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)

        total_duration = time.perf_counter() - start
        step = summarize_step(
            concurrency=concurrency,
            model_pool_size=self.current_model_pool_size,
            attempted_requests=scheduled,
            total_duration=total_duration,
            observed_peak=peak_tracker.peak,
            burst_rounds=(scheduled + concurrency - 1) // concurrency if scheduled else 0,
            results=results,
        )
        print_step_report(step)
        cleanup_tts_output(self.output_dir, self.args.keep_wav_files)
        return step, sorted(results, key=lambda item: (item.burst_id, item.request_id))


def summarize_step(
    concurrency: int,
    model_pool_size: int,
    attempted_requests: int,
    total_duration: float,
    observed_peak: int,
    burst_rounds: int,
    results: list[RequestResult],
) -> StepResult:
    success = [item for item in results if item.success]
    failed = [item for item in results if not item.success]
    sent_results = [item for item in results if item.send_perf > 0]
    http_results = [item for item in sent_results if item.http_started]

    burst_windows: list[float] = []
    for burst_id in sorted({item.burst_id for item in sent_results}):
        burst_items = [item for item in sent_results if item.burst_id == burst_id]
        burst_start = min(item.send_perf for item in burst_items)
        burst_end = max(item.send_perf + item.total_ms / 1000 for item in burst_items)
        burst_windows.append(max(burst_end - burst_start, 0.0))
    effective_duration = sum(burst_windows)

    if sent_results:
        wall_start = min(item.send_perf for item in sent_results)
        wall_end = max(item.send_perf + item.total_ms / 1000 for item in sent_results)
        wall_window_duration = max(wall_end - wall_start, 0.0)
    else:
        wall_window_duration = 0.0
    idle_between_bursts = max(wall_window_duration - effective_duration, 0.0)

    response_times = [item.total_ms for item in success]
    all_response_times = [item.total_ms for item in http_results]
    http_response_times = [item.http_total_ms for item in http_results if item.http_total_ms is not None]
    ttfb_times = [item.first_byte_ms for item in success if item.first_byte_ms is not None]
    ttft_times = [item.first_audio_ms for item in success if item.first_audio_ms is not None]
    rtf_values = [item.rtf for item in success if item.rtf is not None]
    audio_durations = [item.audio_duration for item in success if item.audio_duration is not None]
    audio_total = sum(audio_durations)
    error_summary = Counter(normalize_error(item.error) for item in failed)

    return StepResult(
        concurrency=concurrency,
        model_pool_size=model_pool_size,
        burst_rounds=burst_rounds,
        attempted_requests=attempted_requests,
        completed_requests=len(results),
        success_count=len(success),
        failed_count=len(failed),
        success_rate=(len(success) / attempted_requests * 100) if attempted_requests else 0.0,
        total_duration_s=total_duration,
        effective_duration_s=effective_duration,
        wall_window_duration_s=wall_window_duration,
        idle_between_bursts_s=idle_between_bursts,
        success_qps=(len(success) / effective_duration) if effective_duration > 0 else 0.0,
        total_qps=(len(sent_results) / effective_duration) if effective_duration > 0 else 0.0,
        success_qps_wall=(len(success) / wall_window_duration) if wall_window_duration > 0 else 0.0,
        total_qps_wall=(len(sent_results) / wall_window_duration) if wall_window_duration > 0 else 0.0,
        http_sent_count=len(http_results),
        http_qps=(len(http_results) / effective_duration) if effective_duration > 0 else 0.0,
        configured_concurrency=concurrency,
        observed_peak_inflight=observed_peak,
        full_concurrency_bursts=sum(
            1 for burst_id in range(1, burst_rounds + 1)
            if sum(1 for item in sent_results if item.burst_id == burst_id) >= concurrency
        ),
        avg_response_ms=average(response_times),
        p50_response_ms=percentile(response_times, 50),
        p90_response_ms=percentile(response_times, 90),
        p95_response_ms=percentile(response_times, 95),
        p99_response_ms=percentile(response_times, 99),
        min_response_ms=min(response_times) if response_times else None,
        max_response_ms=max(response_times) if response_times else None,
        all_avg_response_ms=average(all_response_times),
        all_p50_response_ms=percentile(all_response_times, 50),
        all_p90_response_ms=percentile(all_response_times, 90),
        all_p95_response_ms=percentile(all_response_times, 95),
        all_p99_response_ms=percentile(all_response_times, 99),
        all_min_response_ms=min(all_response_times) if all_response_times else None,
        all_max_response_ms=max(all_response_times) if all_response_times else None,
        http_avg_response_ms=average(http_response_times),
        http_p50_response_ms=percentile(http_response_times, 50),
        http_p90_response_ms=percentile(http_response_times, 90),
        http_p95_response_ms=percentile(http_response_times, 95),
        http_p99_response_ms=percentile(http_response_times, 99),
        http_min_response_ms=min(http_response_times) if http_response_times else None,
        http_max_response_ms=max(http_response_times) if http_response_times else None,
        avg_ttfb_ms=average(ttfb_times),
        p95_ttfb_ms=percentile(ttfb_times, 95),
        avg_ttft_ms=average(ttft_times),
        p95_ttft_ms=percentile(ttft_times, 95),
        avg_rtf=average(rtf_values),
        p95_rtf=percentile(rtf_values, 95),
        min_rtf=min(rtf_values) if rtf_values else None,
        max_rtf=max(rtf_values) if rtf_values else None,
        audio_total_duration_s=audio_total,
        avg_audio_duration_s=average(audio_durations),
        p95_audio_duration_s=percentile(audio_durations, 95),
        audio_throughput=(audio_total / effective_duration) if effective_duration > 0 else 0.0,
        audio_throughput_wall=(audio_total / wall_window_duration) if wall_window_duration > 0 else 0.0,
        total_output_bytes=sum(item.output_bytes for item in results),
        total_response_bytes=sum(item.response_bytes for item in results),
        error_summary=dict(error_summary),
    )


def print_step_report(step: StepResult) -> None:
    print(f"\n并发 {step.concurrency} 阶梯结果")
    print(f"计划/完成请求: {step.attempted_requests}/{step.completed_requests}")
    print(f"成功/失败: {step.success_count}/{step.failed_count} ({step.success_rate:.2f}%)")
    print(
        f"同步 burst: {step.burst_rounds}, 满并发 burst: "
        f"{step.full_concurrency_bursts}/{step.burst_rounds}"
    )
    print(f"目标并发/模型池/实际 HTTP 峰值: {step.configured_concurrency}/{step.model_pool_size}/{step.observed_peak_inflight}")
    print(f"总耗时: {step.total_duration_s:.2f}s")
    print(
        f"有效活跃耗时: {step.effective_duration_s:.2f}s, "
        f"首尾窗口: {step.wall_window_duration_s:.2f}s, "
        f"burst 空闲: {step.idle_between_bursts_s:.2f}s"
    )
    print(
        f"总 QPS/成功 QPS: {step.total_qps:.2f}/{step.success_qps:.2f}, "
        f"HTTP QPS: {step.http_qps:.2f}, "
        f"首尾窗口总 QPS/成功 QPS: {step.total_qps_wall:.2f}/{step.success_qps_wall:.2f}"
    )
    print(
        "成功响应耗时: "
        f"avg={format_ms(step.avg_response_ms)}, "
        f"p50={format_ms(step.p50_response_ms)}, "
        f"p90={format_ms(step.p90_response_ms)}, "
        f"p95={format_ms(step.p95_response_ms)}, "
        f"p99={format_ms(step.p99_response_ms)}, "
        f"min={format_ms(step.min_response_ms)}, "
        f"max={format_ms(step.max_response_ms)}"
    )
    print(
        "全量端到端耗时: "
        f"avg={format_ms(step.all_avg_response_ms)}, "
        f"p50={format_ms(step.all_p50_response_ms)}, "
        f"p95={format_ms(step.all_p95_response_ms)}, "
        f"p99={format_ms(step.all_p99_response_ms)}"
    )
    print(
        "HTTP阶段耗时: "
        f"avg={format_ms(step.http_avg_response_ms)}, "
        f"p50={format_ms(step.http_p50_response_ms)}, "
        f"p95={format_ms(step.http_p95_response_ms)}, "
        f"p99={format_ms(step.http_p99_response_ms)}"
    )
    print(f"TTFB: avg={format_ms(step.avg_ttfb_ms)}, p95={format_ms(step.p95_ttfb_ms)}")
    print(f"TTFT: avg={format_ms(step.avg_ttft_ms)}, p95={format_ms(step.p95_ttft_ms)}")
    print(f"RTF: avg={format_number(step.avg_rtf)}, p95={format_number(step.p95_rtf)}, max={format_number(step.max_rtf)}")
    print(
        f"音频总时长: {step.audio_total_duration_s:.2f}s, "
        f"音频吞吐: {step.audio_throughput:.2f} audio-s/s, "
        f"输出: {step.total_output_bytes / 1024:.2f}KB"
    )
    if step.error_summary:
        print("失败原因统计:")
        for error, count in sorted(step.error_summary.items(), key=lambda item: item[1], reverse=True):
            print(f"  {error}: {count}")


def is_breaking_point(
    current: StepResult,
    previous: Optional[StepResult],
    success_threshold: float,
    latency_growth_threshold: float,
) -> tuple[bool, str]:
    if current.success_rate < success_threshold:
        return True, f"成功率 {current.success_rate:.2f}% < 阈值 {success_threshold:.2f}%"
    if (
        previous
        and previous.all_p95_response_ms is not None
        and current.all_p95_response_ms is not None
        and current.all_p95_response_ms > previous.all_p95_response_ms * latency_growth_threshold
    ):
        return (
            True,
            f"全量端到端 P95 从 {previous.all_p95_response_ms:.2f}ms 增长到 "
            f"{current.all_p95_response_ms:.2f}ms，超过 {latency_growth_threshold:.2f} 倍",
        )
    return False, ""


def build_final_report(
    args: argparse.Namespace,
    steps: list[StepResult],
    breaking: Optional[tuple[int, str]],
    report_files: dict[str, Path],
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    healthy_steps = [step for step in steps if step.success_rate >= args.success_threshold]
    last_healthy_before_break = None
    if breaking:
        for step in steps:
            if step.concurrency < breaking[0] and step.success_rate >= args.success_threshold:
                last_healthy_before_break = step
    stable_step = last_healthy_before_break or (healthy_steps[-1] if healthy_steps else None)
    best_audio = max(healthy_steps or steps, key=lambda item: item.audio_throughput) if steps else None
    best_rtf = min(
        [step for step in (healthy_steps or steps) if step.avg_rtf is not None],
        key=lambda item: item.avg_rtf or float("inf"),
        default=None,
    )

    if breaking:
        conclusion = (
            f"首次确认拐点在并发 {breaking[0]}：{breaking[1]}。"
            f"建议稳定并发上限为 {stable_step.concurrency if stable_step else 'N/A'}。"
        )
    else:
        conclusion = (
            f"本次范围内未确认拐点，建议稳定并发上限至少为 "
            f"{stable_step.concurrency if stable_step else 'N/A'}。"
        )

    lines = [
        "# TTS 阶梯式并发压测报告",
        "",
        f"- 生成时间: {now}",
        f"- URL: {args.url}",
        f"- 模型: {args.model}",
        f"- stream: {args.stream}",
        f"- 并发级别: {', '.join(str(step.concurrency) for step in steps)}",
        f"- 成功率阈值: {args.success_threshold:.2f}%",
        f"- 全量端到端 P95 增长拐点阈值: {args.latency_growth_threshold:.2f} 倍",
        f"- 拐点确认级数: {args.break_confirmations}",
        "",
        "## 测试结论",
        "",
        f"- {conclusion}",
    ]
    if best_audio:
        lines.append(
            f"- 达标范围内最高音频吞吐出现在并发 {best_audio.concurrency}: "
            f"{best_audio.audio_throughput:.2f} audio-s/s，成功 QPS={best_audio.success_qps:.2f}。"
        )
    if best_rtf:
        lines.append(
            f"- 达标范围内最低平均 RTF 出现在并发 {best_rtf.concurrency}: "
            f"avg RTF={best_rtf.avg_rtf:.3f}, P95 RTF={format_number(best_rtf.p95_rtf, digits=3)}。"
        )

    lines.extend(
        [
            "- 分析口径: 响应耗时从 StartGate 释放请求开始计算，成功耗时包含模型池等待、网络传输、服务端处理和流式读取。",
            "- 有效活跃耗时 = 各 burst 从释放到最后一个请求完成的活跃窗口之和，不包含 burst 之间的等待间隔；首尾窗口耗时会单独列出用于观察间隔影响。",
            "- 全量端到端耗时只统计已经进入 HTTP 阶段的请求，但耗时从 StartGate 释放开始计算，包含本地模型池等待；HTTP 阶段耗时从实际发起 requests.post 开始计算。",
            "- 音频吞吐 = 成功请求音频总时长 / 有效活跃耗时；RTF = 单请求总耗时 / 音频时长。",
            "- 当成功率下降、全量 P95 成倍增长、音频吞吐不再提升且 RTF 上升时，通常说明系统已经接近容量上限。",
            "",
            "## 阶梯结果",
            "",
            "| 并发 | 模型池 | 请求数 | HTTP数 | 成功率 | 成功QPS | 总QPS | 首尾成功QPS | 全量端到端P95 | HTTP阶段P95 | 成功P95 | TTFB P95 | TTFT P95 | avg RTF | P95 RTF | 音频吞吐 | 首尾音频吞吐 | 活跃耗时 | 空闲耗时 | 音频总时长 | HTTP峰值 |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for step in steps:
        lines.append(
            f"| {step.concurrency} | {step.model_pool_size} | {step.attempted_requests} | "
            f"{step.http_sent_count} | {step.success_rate:.2f}% | "
            f"{step.success_qps:.2f} | {step.total_qps:.2f} | {step.success_qps_wall:.2f} | "
            f"{format_ms(step.all_p95_response_ms)} | {format_ms(step.http_p95_response_ms)} | "
            f"{format_ms(step.p95_response_ms)} | "
            f"{format_ms(step.p95_ttfb_ms)} | {format_ms(step.p95_ttft_ms)} | "
            f"{format_number(step.avg_rtf, digits=3)} | {format_number(step.p95_rtf, digits=3)} | "
            f"{step.audio_throughput:.2f} | {step.audio_throughput_wall:.2f} | "
            f"{step.effective_duration_s:.2f}s | {step.idle_between_bursts_s:.2f}s | "
            f"{step.audio_total_duration_s:.2f}s | {step.observed_peak_inflight} |"
        )

    lines.extend(["", "## 输出文件", ""])
    for name, path in report_files.items():
        lines.append(f"- {name}: {path}")
    return "\n".join(lines)


def write_reports(
    args: argparse.Namespace,
    steps: list[StepResult],
    details: list[RequestResult],
    breaking: Optional[tuple[int, str]],
) -> dict[str, Path]:
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    summary_csv = report_dir / "tts_step_summary.csv"
    detail_csv = report_dir / "tts_request_detail.csv"
    markdown = report_dir / "tts_report.md"

    with summary_csv.open("w", newline="", encoding="utf-8-sig") as file:
        fieldnames = list(StepResult.__dataclass_fields__.keys())
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for step in steps:
            row = step.__dict__.copy()
            row["error_summary"] = json.dumps(row["error_summary"], ensure_ascii=False)
            writer.writerow(row)

    with detail_csv.open("w", newline="", encoding="utf-8-sig") as file:
        fieldnames = list(RequestResult.__dataclass_fields__.keys())
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for item in details:
            writer.writerow(item.__dict__)

    files = {"Markdown报告": markdown, "阶梯汇总CSV": summary_csv, "请求明细CSV": detail_csv}
    markdown.write_text(build_final_report(args, steps, breaking, files) + "\n", encoding="utf-8")
    return files


def parse_concurrent_levels(value: str) -> list[int]:
    levels = []
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        number = int(item)
        if number <= 0:
            raise argparse.ArgumentTypeError("concurrent levels must be positive")
        levels.append(number)
    if not levels:
        raise argparse.ArgumentTypeError("--concurrent-levels cannot be empty")
    return levels


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="TTS 阶梯式并发压测脚本：ThreadPoolExecutor + StartGate 同步 burst 释放请求。"
    )
    parser.add_argument("--url", default=URL, help="TTS instruct2 接口地址")
    parser.add_argument("--model", default="instruct2", help="请求体中的 model 字段")
    parser.add_argument("--text", default="", help="固定合成文本；也支持 @file.txt 从文件读取")
    parser.add_argument("--random-per-request", action="store_true", help="每个请求独立随机选择文本")
    parser.add_argument("--prompt-audio", default="kehu_female_b", help="prompt_audio 音色 ID")
    parser.add_argument("--zero-shot-spk-id", default="kehu_female_b", help="zero_shot_spk_id 音色 ID")
    parser.add_argument("--instruct-text", default="You are a helpful assistant. 很自然地说|endofprompt|>", help="instruct_text")
    parser.add_argument("--speed", type=float, default=1.0, help="语速")
    parser.add_argument("--split", type=str2bool, default=True, help="是否分割文本，默认 True")
    parser.add_argument("--stream", dest="stream", action="store_true", default=True, help="TTS stream 参数，默认开启")
    parser.add_argument("--no-stream", dest="stream", action="store_false", help="关闭 TTS stream 参数")
    parser.add_argument("--background-audio", default="")
    parser.add_argument("--background-volume", type=float, default=0.0)
    parser.add_argument("--background-loop", type=str2bool, default=True)
    parser.add_argument("--text-frontend", type=str2bool, default=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--res-content", type=str2bool, default=True)

    parser.add_argument("--concurrent-levels", type=parse_concurrent_levels, default=None, help="并发阶梯，如 1,2,4,8,16,32")
    parser.add_argument("--concurrent", type=int, default=None, help="只测试一个指定并发级别")
    parser.add_argument("--start-concurrent", type=int, default=1)
    parser.add_argument("--max-concurrent", type=int, default=32)
    parser.add_argument("--step", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=5, help="未指定 --total 时，每个阶梯执行多少轮同步 burst")
    parser.add_argument("--total", type=int, default=None, help="每个阶梯最少请求数，会自动补齐为当前并发整数倍")
    parser.add_argument("--burst-interval", type=float, default=0.0, help="阶梯内两轮 burst 之间的最小间隔秒数")

    parser.add_argument("--connect-timeout", type=float, default=5.0)
    parser.add_argument("--read-timeout", type=float, default=2100.0)
    parser.add_argument("--chunk-timeout", type=float, default=120.0, help="流式包间隔超时秒数")
    parser.add_argument("--model-acquire-timeout", type=float, default=1200.0, help="模型池信号量获取超时秒数")
    parser.add_argument("--model-pool-size", type=int, default=None, help="实际同时发出 HTTP 请求上限；默认等于当前阶梯并发")
    parser.add_argument("--start-timeout", type=float, default=30.0, help="等待 burst 内所有 worker 就绪的超时时间")
    parser.add_argument("--http-pool-size", type=int, default=200)
    parser.add_argument("--chunk-size", type=int, default=8192)
    parser.add_argument("--verify-ssl", action="store_true", help="默认不校验证书；传入后启用校验")

    parser.add_argument("--success-threshold", type=float, default=95.0, help="稳定并发成功率阈值")
    parser.add_argument("--latency-growth-threshold", type=float, default=2.0, help="相邻阶梯全量端到端 P95 增长倍数阈值")
    parser.add_argument("--break-confirmations", type=int, default=2, help="连续触发多少个阶梯后确认拐点")
    parser.add_argument("--stop-success-rate", type=float, default=50.0, help="成功率低于该值时立即停止后续阶梯")

    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="WAV 输出目录")
    parser.add_argument("--report-dir", default="reports", help="报告输出目录")
    parser.add_argument("--keep-wav-files", type=int, default=30, help="自动清理时保留最近 N 个 WAV；负数表示不清理")
    parser.add_argument("--min-audio-bytes", type=int, default=1024)
    parser.add_argument("--require-wav", dest="require_wav", action="store_true", default=True)
    parser.add_argument("--no-require-wav", dest="require_wav", action="store_false")
    parser.add_argument(
        "--allow-duration-size-estimate",
        action="store_true",
        help="WAV 头解析失败时按 24kHz/mono/16-bit 的固定假设用文件大小估算音频时长",
    )
    parser.add_argument("--debug-errors", action="store_true", help="失败原因中追加短 traceback")
    parser.add_argument("--print-payload", action="store_true", help="打印示例 payload 后继续执行")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.concurrent is not None and args.concurrent <= 0:
        raise ValueError("--concurrent must be > 0")
    if args.start_concurrent <= 0 or args.max_concurrent <= 0 or args.step <= 0:
        raise ValueError("--start-concurrent, --max-concurrent and --step must be > 0")
    if args.start_concurrent > args.max_concurrent:
        raise ValueError("--start-concurrent cannot be greater than --max-concurrent")
    if args.rounds <= 0:
        raise ValueError("--rounds must be > 0")
    if args.total is not None and args.total <= 0:
        raise ValueError("--total must be > 0")
    if args.burst_interval < 0:
        raise ValueError("--burst-interval cannot be negative")
    if args.connect_timeout <= 0 or args.read_timeout <= 0 or args.chunk_timeout <= 0:
        raise ValueError("timeouts must be > 0")
    if args.model_acquire_timeout <= 0 or args.start_timeout <= 0:
        raise ValueError("--model-acquire-timeout and --start-timeout must be > 0")
    if args.model_pool_size is not None and args.model_pool_size <= 0:
        raise ValueError("--model-pool-size must be > 0")
    if args.http_pool_size <= 0 or args.chunk_size <= 0:
        raise ValueError("--http-pool-size and --chunk-size must be > 0")
    if not 0 <= args.success_threshold <= 100:
        raise ValueError("--success-threshold must be between 0 and 100")
    if not 0 <= args.stop_success_rate <= 100:
        raise ValueError("--stop-success-rate must be between 0 and 100")
    if args.latency_growth_threshold <= 1:
        raise ValueError("--latency-growth-threshold must be > 1")
    if args.break_confirmations <= 0:
        raise ValueError("--break-confirmations must be > 0")
    if args.min_audio_bytes < 0:
        raise ValueError("--min-audio-bytes cannot be negative")
    if args.text and args.text.startswith("@") and not Path(args.text[1:]).is_file():
        raise ValueError(f"--text file does not exist: {args.text[1:]}")


def has_custom_concurrency_range(argv: list[str]) -> bool:
    range_options = ("--start-concurrent", "--max-concurrent", "--step")
    return any(
        arg == option or arg.startswith(f"{option}=")
        for arg in argv[1:]
        for option in range_options
    )


def resolve_levels(args: argparse.Namespace, argv: list[str]) -> list[int]:
    if args.concurrent is not None:
        return [args.concurrent]
    if args.concurrent_levels is not None:
        return args.concurrent_levels
    if has_custom_concurrency_range(argv):
        return list(range(args.start_concurrent, args.max_concurrent + 1, args.step))
    return DEFAULT_CONCURRENT_LEVELS


def resolve_total_requests(args: argparse.Namespace, concurrency: int) -> int:
    requested = concurrency * args.rounds if args.total is None else args.total
    requested = max(requested, concurrency)
    remainder = requested % concurrency
    if remainder:
        requested += concurrency - remainder
    return requested


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2

    tester = TTSLadderTester(args)
    if args.print_payload:
        example_text = read_text_arg(args.text) if args.text else tester.text_generator.get_random_text()
        print(json.dumps(tester.make_payload(example_text), ensure_ascii=False, indent=2))

    levels = resolve_levels(args, sys.argv)
    print("\nTTS 阶梯式并发压测")
    print(f"目标 URL: {args.url}")
    print(f"并发级别: {levels}")
    print(
        f"请求计划: "
        f"{'每阶梯至少 ' + str(args.total) + ' 个请求' if args.total else '每阶梯 ' + str(args.rounds) + ' 轮 burst'}"
    )
    print(f"报告目录: {Path(args.report_dir).resolve()}")

    all_steps: list[StepResult] = []
    all_details: list[RequestResult] = []
    previous: Optional[StepResult] = None
    breaking: Optional[tuple[int, str]] = None
    break_streak = 0
    first_break_candidate: Optional[tuple[int, str]] = None
    required_break_confirmations = 1 if len(levels) == 1 else args.break_confirmations

    try:
        for level in levels:
            if not RUNNING:
                break
            total_requests = resolve_total_requests(args, level)
            if args.total is not None and total_requests != args.total:
                print(
                    f"\n提示: 并发 {level} 的 --total={args.total} 不是并发整数倍，"
                    f"已补齐为 {total_requests}。"
                )

            step, details = tester.run_step(level, total_requests)
            all_steps.append(step)
            all_details.extend(details)

            is_break, reason = is_breaking_point(
                step,
                previous,
                args.success_threshold,
                args.latency_growth_threshold,
            )
            if is_break:
                if break_streak == 0:
                    first_break_candidate = (level, reason)
                break_streak += 1
                print(f"\n拐点候选: 并发 {level}, 原因: {reason} ({break_streak}/{required_break_confirmations})")
                if break_streak >= required_break_confirmations and breaking is None:
                    breaking = first_break_candidate
                    print(f"确认拐点: 并发 {breaking[0]}, 原因: {breaking[1]}")
            else:
                break_streak = 0
                first_break_candidate = None

            previous = step
            if step.success_rate < args.stop_success_rate:
                if breaking is None:
                    breaking = first_break_candidate or (level, f"成功率 {step.success_rate:.2f}% < 停止阈值 {args.stop_success_rate:.2f}%")
                print(f"\n成功率 {step.success_rate:.2f}% 低于停止阈值 {args.stop_success_rate:.2f}%，停止后续阶梯。")
                break

            if level != levels[-1] and args.concurrent is None:
                time.sleep(2)
    except KeyboardInterrupt:
        graceful_exit()
    finally:
        if all_steps:
            files = write_reports(args, all_steps, all_details, breaking)
            print("\n报告已生成:")
            for name, path in files.items():
                print(f"  {name}: {path}")
        else:
            print("\n未完成任何阶梯，未生成报告。")

    return 0 if all_steps and any(step.success_count > 0 for step in all_steps) else 1


if __name__ == "__main__":
    sys.exit(main())
