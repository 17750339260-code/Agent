# -*- coding: utf-8 -*-
"""
严格按照文字字数要求
合成文本300字，稳定并发数 ≥ 10
合成文本300字、12并发，100请求，响应成功率 ≥ 90%
合成文本300字、12并发，100请求，后端响应耗时 ≤ 300 秒
合成文本300字、单并发请求-流式首字节/首段音频延迟（TTFB/TTFT） TTFB ≤ 3000ms
合成文本300字、单并发请求-非流式响应返回时间 ≤ 60s
合成文本300字、单并发请求，实时率  RTF≤ 1
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import csv
import glob
import hmac
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
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Optional

import requests
import urllib3
from requests.adapters import HTTPAdapter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 网关默认值：与 gateway_tts/tts_api_npu_gateway.py 的南网网关调用方式保持一致。
# 密钥类参数优先从环境变量读取，便于不同环境切换，不需要反复修改源码。
# URL = "https://10.10.65.213:18300/ai-inference-gateway/predict"
# DEFAULT_GATEWAY_APP_KEY = os.getenv("TTS_GATEWAY_APP_KEY", os.getenv("TTS_CUSTCODE", "1001300033"))
# DEFAULT_GATEWAY_SECRET_KEY = os.getenv(
#     "TTS_GATEWAY_SECRET_KEY",
#     os.getenv("TTS_BINDING_API_KEY", "24e74daf74124b0b96c9cb113162a976"),
# )
# DEFAULT_GATEWAY_COMPONENT_CODE = os.getenv("TTS_GATEWAY_COMPONENT_CODE", os.getenv("TTS_COMPONENTCODE", "04100945"))
# DEFAULT_GATEWAY_MODEL = os.getenv("TTS_GATEWAY_MODEL", "tts-v1")

#培训模型网关
URL = "https://10.134.252.232:5030/ai-gateway/predict"
DEFAULT_GATEWAY_APP_KEY = os.getenv("TTS_GATEWAY_APP_KEY", os.getenv("TTS_CUSTCODE", "1000400672300031"))
DEFAULT_GATEWAY_SECRET_KEY = os.getenv(
    "TTS_GATEWAY_SECRET_KEY",
    os.getenv("TTS_BINDING_API_KEY", "b899eef382324e8d8973493fb9c35998"),
)
DEFAULT_GATEWAY_COMPONENT_CODE = os.getenv("TTS_GATEWAY_COMPONENT_CODE", os.getenv("TTS_COMPONENTCODE", "04351372"))
DEFAULT_GATEWAY_MODEL = os.getenv("TTS_GATEWAY_MODEL", "tts-v1")

# 阶梯压测默认并发级别；如需指定单个并发或自定义阶梯，可使用命令行参数覆盖。
DEFAULT_CONCURRENT_LEVELS = [1,2,4,8,12,16,20,22,24,26,28,30,32,34,36,38,40]
DEFAULT_OUTPUT_DIR = "../test_tts/tts_output"
# Metric text length is counted by Han characters only; punctuation, digits, and letters are ignored.
DEFAULT_METRIC_TEXT_LENGTH = 300  # 验收指标：合成文本长度，单位为汉字数
DEFAULT_STABLE_CONCURRENCY_TARGET = 10  # 验收指标：稳定并发目标数
DEFAULT_LOAD_TARGET_CONCURRENCY = 12  # 验收指标：压测并发数
DEFAULT_LOAD_TARGET_REQUESTS = 100  # 验收指标：压测请求总数
DEFAULT_LOAD_SUCCESS_RATE = 90.0  # 验收指标：最低成功率，单位为百分比
DEFAULT_BACKEND_RESPONSE_LIMIT_S = 300.0  # 验收指标：后端最大响应时间，单位为秒
DEFAULT_STREAM_TTFB_LIMIT_MS = 3000.0  # 验收指标：流式首包最大耗时，单位为毫秒
DEFAULT_NON_STREAM_RESPONSE_LIMIT_S = 60.0  # 验收指标：非流式最大响应时间，单位为秒
DEFAULT_RTF_LIMIT = 1.0  # 验收指标：实时率 RTF 上限
DEFAULT_SYNTHESIS_TEXT_SEED = ("现在是凌晨两点四十七分，手机屏幕亮得刺眼。我又翻看了一遍那份辞职信的草稿，光标停在最后一行，一闪一闪，像我的心跳一样，焦躁不安。到底该不该按下发送键？我问自己第一百遍，还是没有答案。五年了，我在这家广告公司从实习生做到创意组长，薪资稳定，头衔体面。可每天面对改不完的PPT，应付甲方那些“高端大气上档次”的模糊需求，我就像一台被抽空电池的机器，纯粹为了运转而运转。我想起大学时，那个扛着二手相机满街拍纪录片的自己，那时候眼睛里有光，觉得能用镜头改变世界。现在呢，镜头的UV镜早不知道丢哪了，相机包里落满了灰。父母总说，稳定压倒一切。他们有他们的道理，我理解。可是，什么叫稳定？是每个月固定到账的工资，还是内心笃定的踏实感？如果是前者，那我算是稳定的。可为什么每次路过独立电影放映室，看见那些眼睛里闪着同样光芒的年轻人，我会下意识低下头，心里又酸又涩？那种感觉就像把自己的梦想关进了地下室，偶尔听见它拍门的声音，我却捂着耳朵不敢去开。前些天，老朋友阿哲辞职开了间摄影工作室，他发来邀请函，说：“来吧，这里缺一个真正会讲故事的人。”那句话像个钩子，把我心底沉下去的东西又钓了上来。我盯着邀请函看了半个钟头，然后鬼使神差地开始写辞职信。写着写着，竟热泪盈眶，仿佛每删一个字，就卸下一层枷锁。可当鼠标移到发送按钮时，所有现实的顾虑又潮水般涌回来：房租、社保、父母期待的目光、同事可能的闲言碎语……还有那个让人窒息的问题——万一失败了呢？万一两年后一事无成，灰溜溜地重新投简历，我还能回到原来的轨道吗？我起身走到窗边，城市灯火渐稀，远处高架桥上偶尔有车划过，像一颗颗流动的星"
)

# WAV 校验和音频时长估算使用的默认格式，TTS 服务通常返回 24kHz/单声道/16bit PCM WAV。
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
    print("\n[STOP] 收到退出信号：当前同步批次会尽量完成，后续阶梯将停止。\n")


signal.signal(signal.SIGINT, graceful_exit)
signal.signal(signal.SIGTERM, graceful_exit)


class TTSResponseError(Exception):
    """Raised when the TTS service returns invalid or unusable audio."""


@dataclass
class RequestResult:
    """单个 TTS 请求的原始结果。

    这里保留毫秒级耗时、HTTP 状态、音频时长和输出路径，后续阶梯统计全部基于这些原始样本计算。
    """
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
    scenario: str = "ladder"


@dataclass(frozen=True)
class WavFormatInfo:
    sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE
    channels: int = DEFAULT_AUDIO_CHANNELS
    sample_width: int = DEFAULT_AUDIO_SAMPLE_WIDTH
    data_offset: Optional[int] = None
    declared_data_bytes: Optional[int] = None


@dataclass
class StepResult:
    """单个并发阶梯的汇总指标。

    有效活跃耗时按每轮同步批次从释放到全部完成的窗口累计，避免把批次间隔等待时间算入 QPS；
    首尾窗口耗时则覆盖本阶梯第一笔请求开始到最后一笔请求结束，用来观察真实压测墙钟窗口。
    """
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


@dataclass(frozen=True)
class AcceptanceMetric:
    name: str
    threshold: str
    value: str
    passed: bool
    detail: str = ""


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
           " 语音合成技术，又称文语转换，是指通过计算机算法将输入的文本信息转换为自然流畅语音输出的技术。它让机器得以“开口说话”，是人机交互领域的关键一环。从早期冰冷的机械发音，到今天以假乱真、带有情感的语音，TTS技术已经走过了近百年的演进之路。最早的语音合成可以追溯到18世纪的机械发声装置，但真正意义上的电子合成则始于20世纪中期。当时的系统基于规则，依靠对发音器官的物理建模，例如共振峰合成。这种方法通过调整频率、带宽等参数模拟元音和辅音，生成的语音虽然可懂，但听起来机械感十足，像机器人说话，缺乏自然的韵律。到了20世纪90年代，拼接合成法成为主流。它的原理是先录制大量真人语音片段，建立一个庞大的音库，合成时根据输入文本从音库中选择合适的基元，再拼接成完整的语句。拼接合成的音质有了显著提升，但要做到高度自然，需要极大规模的音库，且拼接点容易产生不连贯的“咔嗒”声，语调调整也不够灵活。随后，参数统计合成方法，特别是隐马尔可夫模型的引入，让语音合成迈向了数据驱动。隐马尔可夫模型对语音的频谱、基频、时长等参数进行建模，合成时根据文本序列预测参数，再由声码器还原成波形。这种方式生成的语音平稳流畅，但音质偏“沉闷”，常被形容为带着一股挥之不去的“电子音”，与真实人声仍有明显差距。真正的变革来自深度学习。2016年，DeepMind推出的WaveNet模型直接对原始音频波形进行自回归建模，生成的高保真语音震惊了学术界。它模拟了每一个采样点的概率分布，使语音中的气声、语调细微变化都得以呈现，但也因为逐点生成导致速度极慢，无法满足实时需求。之后，端到端的序列到序列模型Tacotron横空出世，它直接将字符或音素序列映射到梅尔频谱图，再通过声码器合成语音。"

        ]
        self.medium_texts = [
           " 现在是凌晨两点四十七分，手机屏幕亮得刺眼。我又翻看了一遍那份辞职信的草稿，光标停在最后一行，一闪一闪，像我的心跳一样，焦躁不安。到底该不该按下发送键？我问自己第一百遍，还是没有答案。五年了，我在这家广告公司从实习生做到创意组长，薪资稳定，头衔体面。可每天面对改不完的PPT，应付甲方那些“高端大气上档次”的模糊需求，我就像一台被抽空电池的机器，纯粹为了运转而运转。我想起大学时，那个扛着二手相机满街拍纪录片的自己，那时候眼睛里有光，觉得能用镜头改变世界。现在呢，镜头的UV镜早不知道丢哪了，相机包里落满了灰。父母总说，稳定压倒一切。他们有他们的道理，我理解。可是，什么叫稳定？是每个月固定到账的工资，还是内心笃定的踏实感？如果是前者，那我算是稳定的。可为什么每次路过独立电影放映室，看见那些眼睛里闪着同样光芒的年轻人，我会下意识低下头，心里又酸又涩？那种感觉就像把自己的梦想关进了地下室，偶尔听见它拍门的声音，我却捂着耳朵不敢去开。前些天，老朋友阿哲辞职开了间摄影工作室，他发来邀请函，说：“来吧，这里缺一个真正会讲故事的人。”那句话像个钩子，把我心底沉下去的东西又钓了上来。我盯着邀请函看了半个钟头，然后鬼使神差地开始写辞职信。写着写着，竟热泪盈眶，仿佛每删一个字，就卸下一层枷锁。可当鼠标移到发送按钮时，所有现实的顾虑又潮水般涌回来：房租、社保、父母期待的目光、同事可能的闲言碎语……还有那个让人窒息的问题——万一失败了呢？万一两年后一事无成，灰溜溜地重新投简历，我还能回到原来的轨道吗？我起身走到窗边，城市灯火渐稀，远处高架桥上偶尔有车划过，像一颗颗流动的星"
        ]
        self.long_texts = [
            "现在是凌晨两点四十七分，手机屏幕亮得刺眼。我又翻看了一遍那份辞职信的草稿，光标停在最后一行，一闪一闪，像我的心跳一样，焦躁不安。到底该不该按下发送键？我问自己第一百遍，还是没有答案。五年了，我在这家广告公司从实习生做到创意组长，薪资稳定，头衔体面。可每天面对改不完的PPT，应付甲方那些“高端大气上档次”的模糊需求，我就像一台被抽空电池的机器，纯粹为了运转而运转。我想起大学时，那个扛着二手相机满街拍纪录片的自己，那时候眼睛里有光，觉得能用镜头改变世界。现在呢，镜头的UV镜早不知道丢哪了，相机包里落满了灰。父母总说，稳定压倒一切。他们有他们的道理，我理解。可是，什么叫稳定？是每个月固定到账的工资，还是内心笃定的踏实感？如果是前者，那我算是稳定的。可为什么每次路过独立电影放映室，看见那些眼睛里闪着同样光芒的年轻人，我会下意识低下头，心里又酸又涩？那种感觉就像把自己的梦想关进了地下室，偶尔听见它拍门的声音，我却捂着耳朵不敢去开。前些天，老朋友阿哲辞职开了间摄影工作室，他发来邀请函，说：“来吧，这里缺一个真正会讲故事的人。”那句话像个钩子，把我心底沉下去的东西又钓了上来。我盯着邀请函看了半个钟头，然后鬼使神差地开始写辞职信。写着写着，竟热泪盈眶，仿佛每删一个字，就卸下一层枷锁。可当鼠标移到发送按钮时，所有现实的顾虑又潮水般涌回来：房租、社保、父母期待的目光、同事可能的闲言碎语……还有那个让人窒息的问题——万一失败了呢？万一两年后一事无成，灰溜溜地重新投简历，我还能回到原来的轨道吗？我起身走到窗边，城市灯火渐稀，远处高架桥上偶尔有车划过，像一颗颗流动的星"
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


def build_gateway_auth_headers(app_key: str, secret_key: str, accept: str) -> dict[str, str]:
    """按网关要求生成 x-date 和 authorization 请求头。

    签名口径与 curl 示例保持一致：对字符串 "x-date: <GMT 时间>" 做 HMAC-SHA256，
    再进行 Base64 编码，最终放入 authorization 的 signature 字段。
    """
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


def decode_base64_audio(value: str) -> Optional[bytes]:
    """从 JSON 字符串字段中尝试提取 base64 音频内容。"""
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith("data:") and "," in candidate:
        candidate = candidate.split(",", 1)[1]
    if len(candidate) < 32:
        return None
    try:
        decoded = base64.b64decode(candidate, validate=True)
    except Exception:
        return None
    return decoded if len(decoded) >= 32 else None


def extract_audio_from_json(value: object) -> Optional[bytes]:
    """兼容网关 JSON 响应中常见的音频字段名，递归查找 base64/WAV 内容。"""
    if isinstance(value, str):
        return decode_base64_audio(value)
    if isinstance(value, list):
        if value and all(isinstance(item, int) and 0 <= item <= 255 for item in value):
            return bytes(value)
        chunks = [extract_audio_from_json(item) for item in value]
        chunks = [item for item in chunks if item]
        if not chunks:
            return None
        riff_chunks = [item for item in chunks if item.startswith(b"RIFF")]
        return riff_chunks[0] if riff_chunks else b"".join(chunks)
    if isinstance(value, dict):
        priority_keys = (
            "audio",
            "audio_data",
            "audioContent",
            "audio_content",
            "wav",
            "content",
            "data",
            "result",
            "response",
            "output",
        )
        for key in priority_keys:
            if key in value:
                extracted = extract_audio_from_json(value[key])
                if extracted:
                    return extracted
        for nested in value.values():
            extracted = extract_audio_from_json(nested)
            if extracted:
                return extracted
    return None


def describe_json_shape(value: object) -> str:
    if isinstance(value, dict):
        return "keys=" + ",".join(str(key) for key in list(value.keys())[:20])
    if isinstance(value, list):
        return f"list_len={len(value)}"
    return type(value).__name__


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


HAN_CODEPOINT_RANGES = (
    (0x3400, 0x4DBF),    # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
    (0x20000, 0x2A6DF),  # CJK Unified Ideographs Extension B
    (0x2A700, 0x2B73F),  # CJK Unified Ideographs Extension C
    (0x2B740, 0x2B81F),  # CJK Unified Ideographs Extension D
    (0x2B820, 0x2CEAF),  # CJK Unified Ideographs Extension E
    (0x2CEB0, 0x2EBEF),  # CJK Unified Ideographs Extension F
    (0x30000, 0x3134F),  # CJK Unified Ideographs Extension G
    (0x31350, 0x323AF),  # CJK Unified Ideographs Extension H
)


def is_han_char(char: str) -> bool:
    codepoint = ord(char)
    return any(start <= codepoint <= end for start, end in HAN_CODEPOINT_RANGES)


def count_han_chars(text: str) -> int:
    return sum(1 for char in text if is_han_char(char))


def truncate_to_han_length(text: str, target_length: int) -> str:
    if target_length <= 0:
        raise ValueError("target_length must be > 0")
    han_count = 0
    for index, char in enumerate(text):
        if is_han_char(char):
            han_count += 1
            if han_count == target_length:
                return text[: index + 1]
    return text


def normalize_synthesis_text(text: str, target_length: int) -> str:
    if target_length <= 0:
        raise ValueError("target_length must be > 0")
    source = text.strip()
    if not source:
        source = DEFAULT_SYNTHESIS_TEXT_SEED
    source_han_count = count_han_chars(source)
    if source_han_count <= 0:
        raise ValueError("synthesis text must contain at least one Han character")
    if source_han_count >= target_length:
        return truncate_to_han_length(source, target_length)
    repeats = math.ceil(target_length / source_han_count)
    return truncate_to_han_length(source * repeats, target_length)


def format_pass_fail(passed: bool) -> str:
    return "PASS" if passed else "FAIL"


def format_seconds_from_ms(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value / 1000:.2f}s"


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
            self.current_text = normalize_synthesis_text(read_text_arg(self.args.text), self.args.metric_text_length)
        else:
            self.current_text = normalize_synthesis_text(
                self.text_generator.get_random_text(self.current_text),
                self.args.metric_text_length,
            )
        return self.current_text

    def pick_request_text(self) -> str:
        if self.args.text:
            return normalize_synthesis_text(read_text_arg(self.args.text), self.args.metric_text_length)
        if self.args.random_per_request:
            return normalize_synthesis_text(self.text_generator.get_random_text(), self.args.metric_text_length)
        return self.current_text or self.refresh_step_text()

    def make_payload(self, text: str) -> dict[str, object]:
        # 网关统一入口参数：model 用于网关侧模型路由，function 用于 TTS 服务内部子功能分发。
        # zero_shot 场景使用 input_text/prompt_text/speaker_id；instruct2 额外补 instruct_text。
        tts_params = {
            "input_text": text,
            "prompt_text": self.args.prompt_text,
            "speaker_id": self.args.speaker_id or self.args.zero_shot_spk_id,
            "prompt_audio": self.args.prompt_audio,
            "speed": self.args.speed,
            "stream": self.args.stream,
            "background_audio": self.args.background_audio,
            "background_volume": self.args.background_volume,
            "background_loop": self.args.background_loop,
            "text_frontend": self.args.text_frontend,
            "seed": self.args.seed,
            "split": self.args.split,
            "res_content": self.args.res_content,
            "response_format": self.args.response_format,
        }
        if self.args.function == "instruct2":
            tts_params["instruct_text"] = self.args.instruct_text
        return {
            "componentCode": self.args.component_code,
            "model": self.args.model,
            "function": self.args.function,
            "tts_params": tts_params,
        }

    def make_headers(self) -> dict[str, str]:
        # 网关流式 WAV 一般使用 application/octet-stream；JSON 模式保留 application/json。
        accept = "application/json" if self.args.response_format == "json" else "application/octet-stream"
        return build_gateway_auth_headers(self.args.app_key, self.args.secret_key, accept)

    def read_audio_stream_response(
        self,
        response: requests.Response,
        save_path: Path,
        start_perf: float,
    ) -> tuple[Optional[float], Optional[float], int, int]:
        """读取网关或直连接口直接返回的 WAV/二进制音频流。"""
        output_bytes = 0
        response_bytes = 0
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

        return first_byte_ms, first_audio_ms, output_bytes, response_bytes

    def read_json_audio_response(
        self,
        response: requests.Response,
        save_path: Path,
        start_perf: float,
    ) -> tuple[Optional[float], Optional[float], int, int]:
        """读取 response_format=json 的网关响应，并把其中的音频内容落盘为 WAV。"""
        chunks: list[bytes] = []
        response_bytes = 0
        first_byte_ms: Optional[float] = None
        last_chunk_perf = time.perf_counter()

        for chunk in response.iter_content(chunk_size=self.args.chunk_size):
            now_perf = time.perf_counter()
            if now_perf - last_chunk_perf > self.args.chunk_timeout:
                raise TTSResponseError(f"JSON chunk timeout: no data for {self.args.chunk_timeout:.2f}s")
            if not chunk:
                continue
            if first_byte_ms is None:
                first_byte_ms = (now_perf - start_perf) * 1000
            last_chunk_perf = now_perf
            chunks.append(chunk)
            response_bytes += len(chunk)

        body = b"".join(chunks)
        if body.startswith(b"RIFF") and body[8:12] == b"WAVE":
            save_path.write_bytes(body)
            return first_byte_ms, first_byte_ms, len(body), response_bytes

        try:
            json_body = json.loads(body.decode(response.encoding or "utf-8"))
        except Exception as exc:
            snippet = body[:500].decode("utf-8", errors="replace")
            raise TTSResponseError(f"Invalid JSON response: {exc}; body={snippet}") from exc

        audio_bytes = extract_audio_from_json(json_body)
        if not audio_bytes:
            raise TTSResponseError(f"JSON response does not contain base64 audio: {describe_json_shape(json_body)}")
        save_path.write_bytes(audio_bytes)
        return first_byte_ms, first_byte_ms, len(audio_bytes), response_bytes

    def send_request(
        self,
        request_id: int,
        concurrency: int,
        burst_id: int,
        start_gate: StartGate,
        inflight: InflightCounter,
        scenario: str = "ladder",
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
                    scenario=scenario,
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
                    scenario=scenario,
                )

            inflight.enter()
            entered_inflight = True
            headers = self.make_headers()
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
            content_type = response.headers.get("Content-Type", "").lower()
            expects_json_audio = self.args.response_format == "json" or "application/json" in content_type
            if expects_json_audio:
                first_byte_ms, first_audio_ms, output_bytes, response_bytes = self.read_json_audio_response(
                    response,
                    save_path,
                    start_perf,
                )
            else:
                first_byte_ms, first_audio_ms, output_bytes, response_bytes = self.read_audio_stream_response(
                    response,
                    save_path,
                    start_perf,
                )

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
                text_len=count_han_chars(text),
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
                scenario=scenario,
            )
        except requests.exceptions.Timeout as exc:
            return self._failure(
                concurrency, request_id, burst_id, start_epoch, start_perf, f"Timeout: {exc}",
                status_code, text, model_wait_ms=model_wait_ms, http_started=http_started,
                http_start_perf=http_start_perf, output_bytes=output_bytes,
                response_bytes=response_bytes, save_path=str(save_path or ""), scenario=scenario
            )
        except requests.exceptions.ConnectionError as exc:
            return self._failure(
                concurrency, request_id, burst_id, start_epoch, start_perf, f"Connection error: {exc}",
                status_code, text, model_wait_ms=model_wait_ms, http_started=http_started,
                http_start_perf=http_start_perf, output_bytes=output_bytes,
                response_bytes=response_bytes, save_path=str(save_path or ""), scenario=scenario
            )
        except Exception as exc:
            detail = str(exc) or exc.__class__.__name__
            if self.args.debug_errors:
                detail = f"{detail} | {traceback.format_exc()[:1000]}"
            return self._failure(
                concurrency, request_id, burst_id, start_epoch, start_perf, detail,
                status_code, text, model_wait_ms=model_wait_ms, http_started=http_started,
                http_start_perf=http_start_perf, output_bytes=output_bytes,
                response_bytes=response_bytes, save_path=str(save_path or ""), scenario=scenario
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
        scenario: str = "ladder",
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
            text_len=count_han_chars(text),
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
            scenario=scenario,
        )

    def run_single_probe(self, stream: bool, scenario: str) -> RequestResult:
        original_stream = self.args.stream
        original_model_pool_size = self.current_model_pool_size
        original_semaphore = self.model_semaphore
        self.args.stream = stream
        self.current_model_pool_size = 1
        self.model_semaphore = threading.BoundedSemaphore(1)
        self.refresh_step_text()
        label = "stream" if stream else "non-stream"
        print(f"\nRunning single-request {label} metric probe: text_len={count_han_chars(self.current_text or '')}")
        start_gate = StartGate(1)
        inflight = InflightCounter()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self.send_request,
                    1,
                    1,
                    1,
                    start_gate,
                    inflight,
                    scenario,
                )
                if not start_gate.wait_until_ready(timeout=self.args.start_timeout):
                    start_gate.abort(f"metric probe start timeout: {scenario}")
                else:
                    start_gate.release()
                return future.result()
        finally:
            self.args.stream = original_stream
            self.current_model_pool_size = original_model_pool_size
            self.model_semaphore = original_semaphore

    def run_metric_probes(self) -> list[RequestResult]:
        return [
            self.run_single_probe(stream=True, scenario="metric_stream_single"),
            self.run_single_probe(stream=False, scenario="metric_non_stream_single"),
        ]

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
            f"开始并发阶梯：目标并发={concurrency}, HTTP放行池={self.current_model_pool_size}, "
            f"计划请求数={total_requests}, 同步批次数={burst_count}, 流式输出(stream)={self.args.stream}"
        )
        if self.args.random_per_request:
            print("文本策略：每个请求独立随机选择文本")
        elif self.args.text:
            print(f"文本策略：使用 --text 固定文本，汉字数={count_han_chars(self.current_text or '')}")
        else:
            print(f"文本策略：本阶梯固定随机文本，汉字数={count_han_chars(self.current_text or '')}")
        print(f"{'=' * 80}")

        start = time.perf_counter()
        next_request_id = 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            for burst_id in range(1, burst_count + 1):
                if not RUNNING:
                    print("检测到停止信号：不再启动新的同步批次。")
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
                        f"同步启动门等待超时: 并发={concurrency}, 批次={burst_id}, "
                        f"就绪线程={start_gate.ready}/{burst_size}, 超时={self.args.start_timeout:.2f}s"
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
                print(f"释放同步批次 {burst_id}/{burst_count}: {burst_size} 个请求同时发起")
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
    """汇总一个并发阶梯的成功率、吞吐、延迟分位数和音频生成效率。"""
    success = [item for item in results if item.success]
    failed = [item for item in results if not item.success]
    sent_results = [item for item in results if item.send_perf > 0]
    http_results = [item for item in sent_results if item.http_started]

    # 有效活跃耗时：每轮同步批次从统一释放到最后一个请求结束的耗时之和。
    # 这个口径用于计算服务真正承压期间的 QPS，避免同步批次间隔拉低吞吐。
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

    # 三类耗时口径：
    # 1. 成功响应耗时：只统计成功样本，用于用户可感知的稳定延迟。
    # 2. 全量端到端耗时：统计已进入 HTTP 阶段的所有样本，失败和超时也纳入容量判断。
    # 3. HTTP 阶段耗时：从 requests.post 发起到响应读完，不包含本地模型池等待。
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
    print(f"\n并发阶梯 {step.concurrency} 结果")
    print(f"请求数量（计划/完成）: {step.attempted_requests}/{step.completed_requests}")
    print(f"请求结果（成功/失败/成功率）: {step.success_count}/{step.failed_count}/{step.success_rate:.2f}%")
    print(
        f"同步批次: {step.burst_rounds} 轮, 满并发批次: "
        f"{step.full_concurrency_bursts}/{step.burst_rounds}"
    )
    print(
        "并发配置（目标并发/HTTP放行池/实际HTTP峰值）: "
        f"{step.configured_concurrency}/{step.model_pool_size}/{step.observed_peak_inflight}"
    )
    print(f"阶梯总耗时: {step.total_duration_s:.2f}s")
    print(
        f"压测时间窗口: 有效活跃耗时={step.effective_duration_s:.2f}s, "
        f"首尾墙钟窗口={step.wall_window_duration_s:.2f}s, "
        f"批次间空闲耗时={step.idle_between_bursts_s:.2f}s"
    )
    print(
        f"吞吐量（按有效活跃耗时）: 总请求QPS={step.total_qps:.2f}, "
        f"成功请求QPS={step.success_qps:.2f}, HTTP发送QPS={step.http_qps:.2f}"
    )
    print(
        f"吞吐量（按首尾墙钟窗口）: 总请求QPS={step.total_qps_wall:.2f}, "
        f"成功请求QPS={step.success_qps_wall:.2f}"
    )
    print(
        "成功响应耗时: "
        f"平均值={format_ms(step.avg_response_ms)}, "
        f"P50={format_ms(step.p50_response_ms)}, "
        f"P90={format_ms(step.p90_response_ms)}, "
        f"P95={format_ms(step.p95_response_ms)}, "
        f"P99={format_ms(step.p99_response_ms)}, "
        f"最小值={format_ms(step.min_response_ms)}, "
        f"最大值={format_ms(step.max_response_ms)}"
    )
    print(
        "全量端到端耗时: "
        f"平均值={format_ms(step.all_avg_response_ms)}, "
        f"P50={format_ms(step.all_p50_response_ms)}, "
        f"P95={format_ms(step.all_p95_response_ms)}, "
        f"P99={format_ms(step.all_p99_response_ms)}"
    )
    print(
        "HTTP阶段耗时（不含本地等待）: "
        f"平均值={format_ms(step.http_avg_response_ms)}, "
        f"P50={format_ms(step.http_p50_response_ms)}, "
        f"P95={format_ms(step.http_p95_response_ms)}, "
        f"P99={format_ms(step.http_p99_response_ms)}"
    )
    print(
        "首字节耗时（TTFB）: "
        f"平均值={format_ms(step.avg_ttfb_ms)}, P95={format_ms(step.p95_ttfb_ms)}"
    )
    print(
        "首段音频耗时（TTFT）: "
        f"平均值={format_ms(step.avg_ttft_ms)}, P95={format_ms(step.p95_ttft_ms)}"
    )
    print(
        "实时率（RTF，越低表示生成越快）: "
        f"平均值={format_number(step.avg_rtf)}, "
        f"P95={format_number(step.p95_rtf)}, "
        f"最大值={format_number(step.max_rtf)}"
    )
    print(
        f"音频总时长: {step.audio_total_duration_s:.2f}s, "
        f"音频吞吐量={step.audio_throughput:.2f} 音频秒/秒, "
        f"输出音频大小={step.total_output_bytes / 1024:.2f}KB"
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


def find_step(steps: list[StepResult], concurrency: int) -> Optional[StepResult]:
    matches = [step for step in steps if step.concurrency == concurrency]
    return matches[-1] if matches else None


def find_probe(probes: list[RequestResult], scenario: str) -> Optional[RequestResult]:
    matches = [item for item in probes if item.scenario == scenario]
    return matches[-1] if matches else None


def build_acceptance_metrics(
    args: argparse.Namespace,
    steps: list[StepResult],
    details: list[RequestResult],
    probes: list[RequestResult],
) -> list[AcceptanceMetric]:
    target_len = args.metric_text_length
    measured_lengths = [item.text_len for item in details if item.scenario.startswith(("ladder", "metric_"))]
    text_lengths_ok = bool(measured_lengths) and all(length == target_len for length in measured_lengths)
    unique_lengths = sorted(set(measured_lengths))

    backend_limit_ms = args.metric_backend_response_s * 1000
    stable_steps = [
        step for step in steps
        if step.success_rate >= args.metric_success_rate
        and step.http_max_response_ms is not None
        and step.http_max_response_ms <= backend_limit_ms
    ]
    stable_concurrency = max((step.concurrency for step in stable_steps), default=0)

    load_step = find_step(steps, args.metric_load_concurrency)
    load_has_requests = bool(load_step and load_step.attempted_requests >= args.metric_load_requests)
    load_suffix = (
        "N/A"
        if load_step is None
        else f"{load_step.attempted_requests} attempted/{load_step.completed_requests} completed"
    )

    stream_probe = find_probe(probes, "metric_stream_single")
    non_stream_probe = find_probe(probes, "metric_non_stream_single")
    rtf_probe = stream_probe if stream_probe and stream_probe.rtf is not None else non_stream_probe

    metrics = [
        AcceptanceMetric(
            name=f"Synthesis text length",
            threshold=f"all requests = {target_len} Han chars",
            value=", ".join(str(item) for item in unique_lengths) if unique_lengths else "N/A",
            passed=text_lengths_ok,
        ),
        AcceptanceMetric(
            name="Stable concurrency",
            threshold=f">= {args.metric_stable_concurrency}",
            value=str(stable_concurrency) if stable_concurrency else "N/A",
            passed=stable_concurrency >= args.metric_stable_concurrency,
            detail=f"success_rate>={args.metric_success_rate:.2f}% and backend_max<={args.metric_backend_response_s:.2f}s",
        ),
        AcceptanceMetric(
            name=f"{args.metric_load_concurrency} concurrency request count",
            threshold=f">= {args.metric_load_requests} requests",
            value=load_suffix,
            passed=load_has_requests,
        ),
        AcceptanceMetric(
            name=f"{args.metric_load_concurrency} concurrency success rate",
            threshold=f">= {args.metric_success_rate:.2f}%",
            value="N/A" if load_step is None else f"{load_step.success_rate:.2f}%",
            passed=bool(load_step and load_has_requests and load_step.success_rate >= args.metric_success_rate),
        ),
        AcceptanceMetric(
            name=f"{args.metric_load_concurrency} concurrency backend response time",
            threshold=f"max <= {args.metric_backend_response_s:.2f}s",
            value=(
                "N/A"
                if load_step is None
                else f"max={format_seconds_from_ms(load_step.http_max_response_ms)}, p95={format_seconds_from_ms(load_step.http_p95_response_ms)}"
            ),
            passed=bool(
                load_step
                and load_has_requests
                and load_step.http_max_response_ms is not None
                and load_step.http_max_response_ms <= backend_limit_ms
            ),
        ),
        AcceptanceMetric(
            name="Single stream TTFB/TTFT",
            threshold=f"TTFB <= {args.metric_stream_ttfb_ms:.0f}ms",
            value=(
                "N/A"
                if stream_probe is None
                else f"TTFB={format_ms(stream_probe.first_byte_ms)}, TTFT={format_ms(stream_probe.first_audio_ms)}"
            ),
            passed=bool(
                stream_probe
                and stream_probe.success
                and stream_probe.first_byte_ms is not None
                and stream_probe.first_byte_ms <= args.metric_stream_ttfb_ms
            ),
        ),
        AcceptanceMetric(
            name="Single non-stream response time",
            threshold=f"<= {args.metric_non_stream_response_s:.2f}s",
            value="N/A" if non_stream_probe is None else format_seconds_from_ms(non_stream_probe.total_ms),
            passed=bool(
                non_stream_probe
                and non_stream_probe.success
                and non_stream_probe.total_ms <= args.metric_non_stream_response_s * 1000
            ),
        ),
        AcceptanceMetric(
            name="Single request RTF",
            threshold=f"<= {args.metric_rtf_limit:.3f}",
            value="N/A" if rtf_probe is None else format_number(rtf_probe.rtf, digits=3),
            passed=bool(rtf_probe and rtf_probe.success and rtf_probe.rtf is not None and rtf_probe.rtf <= args.metric_rtf_limit),
        ),
    ]
    return metrics


def print_acceptance_report(metrics: list[AcceptanceMetric]) -> None:
    print("\nAcceptance metrics")
    for metric in metrics:
        detail = f" ({metric.detail})" if metric.detail else ""
        print(f"[{format_pass_fail(metric.passed)}] {metric.name}: {metric.value} / {metric.threshold}{detail}")


def build_final_report(
    args: argparse.Namespace,
    steps: list[StepResult],
    details: list[RequestResult],
    probes: list[RequestResult],
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
    acceptance_metrics = build_acceptance_metrics(args, steps, details, probes)

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
        "- 请求模式: 网关访问",
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
    lines[5:5] = [
        f"- 组件编码(componentCode): {args.component_code}",
        f"- 子功能(function): {args.function}",
        f"- 响应格式(response_format): {args.response_format}",
    ]
    if best_audio:
        lines.append(
            f"- 达标范围内最高音频吞吐出现在并发 {best_audio.concurrency}: "
            f"{best_audio.audio_throughput:.2f} 音频秒/秒，成功请求QPS={best_audio.success_qps:.2f}。"
        )
    if best_rtf:
        lines.append(
            f"- 达标范围内最低平均实时率（RTF）出现在并发 {best_rtf.concurrency}: "
            f"平均RTF={best_rtf.avg_rtf:.3f}, P95 RTF={format_number(best_rtf.p95_rtf, digits=3)}。"
        )

    lines.extend(
        [
            "",
            "## Acceptance Metrics",
            "",
            "| Metric | Threshold | Value | Result | Detail |",
            "|---|---:|---:|---:|---|",
        ]
    )
    for metric in acceptance_metrics:
        lines.append(
            f"| {metric.name} | {metric.threshold} | {metric.value} | "
            f"{format_pass_fail(metric.passed)} | {metric.detail} |"
        )

    lines.extend(
        [
            "- 分析口径: 响应耗时从 StartGate 释放请求开始计算，成功耗时包含模型池等待、网络传输、服务端处理和流式读取。",
            "- 有效活跃耗时 = 各同步批次从释放到最后一个请求完成的活跃窗口之和，不包含批次之间的等待间隔；首尾窗口耗时会单独列出用于观察间隔影响。",
            "- 全量端到端耗时只统计已经进入 HTTP 阶段的请求，但耗时从 StartGate 释放开始计算，包含本地模型池等待；HTTP 阶段耗时从实际发起 requests.post 开始计算。",
            "- 音频吞吐 = 成功请求音频总时长 / 有效活跃耗时；RTF = 单请求总耗时 / 音频时长。",
            "- 当成功率下降、全量 P95 成倍增长、音频吞吐不再提升且 RTF 上升时，通常说明系统已经接近容量上限。",
            "",
            "## 阶梯结果",
            "",
            "| 并发 | HTTP放行池 | 请求数 | HTTP数 | 成功率 | 成功请求QPS | 总请求QPS | 首尾成功QPS | 全量端到端P95 | HTTP阶段P95 | 成功P95 | 首字节P95(TTFB) | 首段音频P95(TTFT) | 平均RTF | P95 RTF | 音频吞吐 | 首尾音频吞吐 | 活跃耗时 | 空闲耗时 | 音频总时长 | HTTP峰值 |",
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
    probes: list[RequestResult],
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
    markdown.write_text(build_final_report(args, steps, details, probes, breaking, files) + "\n", encoding="utf-8")
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
        description="TTS 阶梯式并发压测脚本：ThreadPoolExecutor + StartGate 同步批次释放请求。"
    )
    # 网关访问参数：固定使用统一 /predict 入口，签名方式参考 gateway_tts/tts_api_npu_gateway.py。
    parser.add_argument("--app-key", default=DEFAULT_GATEWAY_APP_KEY, help="网关 HMAC username/app_key")
    parser.add_argument("--secret-key", default=DEFAULT_GATEWAY_SECRET_KEY, help="网关 HMAC secret_key")
    parser.add_argument("--component-code", default=DEFAULT_GATEWAY_COMPONENT_CODE, help="网关 componentCode")
    parser.add_argument("--function", default="zero_shot", choices=["zero_shot", "instruct2", "cross_lingual"], help="网关 function 字段")
    parser.add_argument("--model", default=DEFAULT_GATEWAY_MODEL, help="请求体中的 model 字段；网关默认 tts-v1")

    # TTS 业务参数：请求体会转换为 input_text/speaker_id 等网关字段。
    parser.add_argument("--text", default="", help="固定合成文本；也支持 @file.txt 从文件读取")
    parser.add_argument("--random-per-request", action="store_true", help="每个请求独立随机选择文本")
    parser.add_argument("--prompt-text", default="这是一段参考文本", help="网关 zero_shot 的 prompt_text")
    parser.add_argument("--speaker-id", default="", help="网关 speaker_id；为空时使用 --zero-shot-spk-id")
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
    parser.add_argument("--response-format", default="json", choices=["json", "wav"], help="网关 tts_params.response_format")

    parser.add_argument("--metric-text-length", type=int, default=DEFAULT_METRIC_TEXT_LENGTH, help="acceptance metric synthesis text Han character length")
    parser.add_argument("--metric-stable-concurrency", type=int, default=DEFAULT_STABLE_CONCURRENCY_TARGET, help="acceptance metric stable concurrency target")
    parser.add_argument("--metric-load-concurrency", type=int, default=DEFAULT_LOAD_TARGET_CONCURRENCY, help="acceptance metric load concurrency")
    parser.add_argument("--metric-load-requests", type=int, default=DEFAULT_LOAD_TARGET_REQUESTS, help="acceptance metric load request count")
    parser.add_argument("--metric-success-rate", type=float, default=DEFAULT_LOAD_SUCCESS_RATE, help="acceptance metric success rate percent")
    parser.add_argument("--metric-backend-response-s", type=float, default=DEFAULT_BACKEND_RESPONSE_LIMIT_S, help="acceptance metric backend response max seconds")
    parser.add_argument("--metric-stream-ttfb-ms", type=float, default=DEFAULT_STREAM_TTFB_LIMIT_MS, help="acceptance metric single stream TTFB ms")
    parser.add_argument("--metric-non-stream-response-s", type=float, default=DEFAULT_NON_STREAM_RESPONSE_LIMIT_S, help="acceptance metric single non-stream response seconds")
    parser.add_argument("--metric-rtf-limit", type=float, default=DEFAULT_RTF_LIMIT, help="acceptance metric RTF limit")
    parser.add_argument("--skip-metric-probes", action="store_true", help="skip single-request stream/non-stream acceptance probes")

    # 并发阶梯参数：每个阶梯按 StartGate 同步释放一批请求，用于制造更接近真实并发的瞬时压力。
    parser.add_argument("--concurrent-levels", type=parse_concurrent_levels, default=None, help="并发阶梯，如 1,2,4,8,16,32")
    parser.add_argument("--concurrent", type=int, default=None, help="只测试一个指定并发级别")
    parser.add_argument("--start-concurrent", type=int, default=1)
    parser.add_argument("--max-concurrent", type=int, default=32)
    parser.add_argument("--step", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=5, help="未指定 --total 时，每个阶梯执行多少轮同步批次")
    parser.add_argument("--total", type=int, default=None, help="每个阶梯最少请求数，会自动补齐为当前并发整数倍")
    parser.add_argument("--burst-interval", type=float, default=0.0, help="阶梯内两轮同步批次之间的最小间隔秒数")

    # 超时和连接池参数：connect/read 是 requests 超时，chunk-timeout 是流式响应两包之间的最大等待。
    parser.add_argument("--connect-timeout", type=float, default=5.0)
    parser.add_argument("--read-timeout", type=float, default=2100.0)
    parser.add_argument("--chunk-timeout", type=float, default=120.0, help="流式包间隔超时秒数")
    parser.add_argument("--model-acquire-timeout", type=float, default=1200.0, help="模型池信号量获取超时秒数")
    parser.add_argument("--model-pool-size", type=int, default=None, help="实际同时发出 HTTP 请求上限；默认等于当前阶梯并发")
    parser.add_argument("--start-timeout", type=float, default=30.0, help="等待同步批次内所有 worker 就绪的超时时间")
    parser.add_argument("--http-pool-size", type=int, default=200)
    parser.add_argument("--chunk-size", type=int, default=8192)
    parser.add_argument("--verify-ssl", action="store_true", help="默认不校验证书；传入后启用校验")

    # 拐点判定参数：成功率下降、P95 成倍增长或低于停止成功率时，可提前停止后续阶梯。
    parser.add_argument("--success-threshold", type=float, default=95.0, help="稳定并发成功率阈值")
    parser.add_argument("--latency-growth-threshold", type=float, default=2.0, help="相邻阶梯全量端到端 P95 增长倍数阈值")
    parser.add_argument("--break-confirmations", type=int, default=2, help="连续触发多少个阶梯后确认拐点")
    parser.add_argument("--stop-success-rate", type=float, default=50.0, help="成功率低于该值时立即停止后续阶梯")

    # 输出和校验参数：成功请求会保存 WAV，并基于音频时长计算 RTF 和音频吞吐。
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
    args = parser.parse_args()
    args.url = URL
    return args


def validate_args(args: argparse.Namespace) -> None:
    if not args.app_key:
        raise ValueError("--app-key 不能为空")
    if not args.secret_key:
        raise ValueError("--secret-key 不能为空")
    if not args.component_code:
        raise ValueError("--component-code 不能为空")
    if not args.model:
        raise ValueError("--model 不能为空")
    if not args.function:
        raise ValueError("--function 不能为空")
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
        raise ValueError("--burst-interval 不能为负数")
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
    if args.metric_text_length <= 0:
        raise ValueError("--metric-text-length must be > 0")
    if args.metric_stable_concurrency <= 0:
        raise ValueError("--metric-stable-concurrency must be > 0")
    if args.metric_load_concurrency <= 0 or args.metric_load_requests <= 0:
        raise ValueError("--metric-load-concurrency and --metric-load-requests must be > 0")
    if not 0 <= args.metric_success_rate <= 100:
        raise ValueError("--metric-success-rate must be between 0 and 100")
    if args.metric_backend_response_s <= 0:
        raise ValueError("--metric-backend-response-s must be > 0")
    if args.metric_stream_ttfb_ms <= 0:
        raise ValueError("--metric-stream-ttfb-ms must be > 0")
    if args.metric_non_stream_response_s <= 0:
        raise ValueError("--metric-non-stream-response-s must be > 0")
    if args.metric_rtf_limit <= 0:
        raise ValueError("--metric-rtf-limit must be > 0")


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
    if args.total is None and concurrency == args.metric_load_concurrency:
        requested = max(requested, args.metric_load_requests)
    requested = max(requested, concurrency)
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
        example_text = normalize_synthesis_text(
            read_text_arg(args.text) if args.text else tester.text_generator.get_random_text(),
            args.metric_text_length,
        )
        print(json.dumps(tester.make_payload(example_text), ensure_ascii=False, indent=2))

    levels = resolve_levels(args, sys.argv)
    print("\nTTS 阶梯式并发压测")
    print("请求模式: 网关访问")
    print(f"目标 URL: {args.url}")
    print(
        f"网关配置: 组件编码(componentCode)={args.component_code}, "
        f"模型(model)={args.model}, 子功能(function)={args.function}, "
        f"响应格式(response_format)={args.response_format}"
    )
    print(f"并发级别: {levels}")
    print(
        f"请求计划: "
        f"{'每阶梯至少 ' + str(args.total) + ' 个请求' if args.total else '每阶梯 ' + str(args.rounds) + ' 轮同步批次'}"
    )
    print(f"报告目录: {Path(args.report_dir).resolve()}")

    all_steps: list[StepResult] = []
    all_details: list[RequestResult] = []
    probe_results: list[RequestResult] = []
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
            if not args.skip_metric_probes and RUNNING:
                probe_results = tester.run_metric_probes()
                all_details.extend(probe_results)
            acceptance_metrics = build_acceptance_metrics(args, all_steps, all_details, probe_results)
            print_acceptance_report(acceptance_metrics)
            files = write_reports(args, all_steps, all_details, probe_results, breaking)
            print("\n报告已生成:")
            for name, path in files.items():
                print(f"  {name}: {path}")
        else:
            print("\n未完成任何阶梯，未生成报告。")

    return 0 if all_steps and any(step.success_count > 0 for step in all_steps) else 1


if __name__ == "__main__":
    sys.exit(main())
