#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FunASR 语音识别服务 — ASR 压力测试全集

【新手导读 — 这个脚本在干什么？】
  1. 读取一段 wav 测试音频，转成 base64
  2. 用 aiohttp 异步向 ASR 接口发 HTTP POST 请求（可高并发）
  3. 记录每次请求的：是否成功、延迟、识别文本、RTF、CER 等
  4. 汇总后打印两类指标：
     - 压力测试指标：成功率、QPS、并发、错误类型
     - ASR 专项指标：RTF、延迟分位数、CER/WER、音频吞吐量

【核心名词】
  QPS    = 每秒请求数（Queries Per Second）
  RTF    = 推理耗时 ÷ 音频时长；<1 表示比实时更快
  TTFB   = 收到 HTTP 响应头的时间（本接口非流式，通常≈总延迟）
  CER    = 字错误率（需 --reference-text 参考文本）
  WER    = 词错误率
  并发   = 同时有多少个“工人”在发请求

【代码结构（从上到下读即可）】
  默认配置 → 数据结构 → 工具函数 → 打印指标 → 发 HTTP 请求
  → run_* 七种压测 → 总汇总 → main 入口解析命令行

【七种测试类型】
  baseline          单请求基准 (多模型延迟 / RTF / CER)
  ramp              递增并发压测 (找稳定并发上限)
  sustained         恒定并发持续负载
  spike             突发并发尖峰 (低→高→低)
  soak              长时间浸泡 (观察延迟漂移)
  fixed_qps         固定请求速率压测
  accuracy_load     并发下准确度 (CER/WER/空结果率)

【重要】函数名用 run_ 开头，不要用 pytest 运行本文件！

用法 (请用 python 直接运行):
  python "test_asr/asr_api_npu_concurrency .py"
  python "test_asr/asr_api_npu_concurrency .py" --tests baseline,ramp
  python "test_asr/asr_api_npu_concurrency .py" --tests all --reference-text "参考文本"
"""

from __future__ import annotations

# ---------- 标准库：解析命令行、异步、编解码、统计等 ----------
import argparse      # 解析命令行参数，如 --url --tests
import asyncio       # 异步并发，同时发很多 HTTP 请求
import base64        # 把 wav 二进制转成接口需要的 base64 字符串
import json          # 保存压测报告为 JSON
import os            # 路径、读文件
import ssl           # HTTPS 证书设置
import statistics    # 求平均数等
import sys           # 退出程序 sys.exit
import time          # 计时
import wave          # 读取 wav 头信息（采样率、时长）
from dataclasses import asdict, dataclass, field  # 数据类，少写样板代码
from datetime import datetime
from enum import Enum
from typing import Any, Optional  # 类型标注，方便阅读

import aiohttp         # 异步 HTTP 客户端，用来调 ASR 接口

# jiwer：可选库，用于精确计算 CER/WER；没装则用简化算法
try:
    from jiwer import cer as jiwer_cer
    from jiwer import wer as jiwer_wer
except ImportError:
    jiwer_cer = None
    jiwer_wer = None

# ==================== 默认配置（不改命令行时就用这些） ====================

DEFAULT_API_URL = "http://36.111.82.53:10017/v1/audio/trans"  # ASR 服务地址
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根目录
DEFAULT_AUDIO = os.path.join(PROJECT_ROOT, "test_asr", "asr_test_audio", "1.wav")  # 默认测试音频

# HTTPS 时跳过证书校验（内网/自签证书场景）
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# 部分损坏的 wav 头会写成超大时长，超过 1 小时则不可信
MAX_REASONABLE_AUDIO_SECONDS = 3600.0

# --tests 参数可选的全部压测名称
ALL_TEST_NAMES = (
    "baseline",
    "ramp",
    "sustained",
    "spike",
    "soak",
    "fixed_qps",
    "accuracy_load",
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
    latency_sec: float = 0.0        # 从发请求到收完响应的总耗时
    ttfb_sec: float = 0.0           # 首字节时间（收到响应头）
    body_read_sec: float = 0.0        # 读响应体耗时 ≈ latency - ttfb
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
    avg_latency: float = 0.0        # 平均延迟
    p50_latency: float = 0.0        # 50% 的请求延迟低于此值（中位数）
    p95_latency: float = 0.0        # 95% 的请求延迟低于此值
    p99_latency: float = 0.0        # 99 分位延迟
    avg_ttfb: float = 0.0
    p95_ttfb: float = 0.0
    avg_rtf: float = 0.0            # 平均实时因子
    p50_rtf: float = 0.0
    p95_rtf: float = 0.0
    max_rtf: float = 0.0            # 最慢的一条 RTF
    avg_chars_per_sec: float = 0.0  # 平均识别速度（字/秒）
    avg_text_chars: float = 0.0     # 平均每条返回多少字
    empty_text_rate: float = 0.0    # 返回空文本的比例（%）
    avg_cer: Optional[float] = None
    p95_cer: Optional[float] = None
    avg_wer: Optional[float] = None
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
    hotwords: str = ""              # 热词，传给接口


# ==================== 工具函数（音频、指标计算、拼请求体） ====================


def _duration_from_pcm_bytes(
    pcm_bytes: int, sample_rate: int, channels: int, sample_width: int
) -> float:
    """根据 PCM 字节数估算音频时长（秒）"""
    denom = sample_rate * channels * sample_width
    if denom <= 0 or pcm_bytes <= 0:
        return 0.0
    return pcm_bytes / float(denom)


def load_audio_meta(audio_path: str) -> AudioMeta:
    """
    读取 wav 文件：解析时长、采样率，并生成 base64。
    若 wav 头损坏，会按文件大小估算时长。
    """
    if not os.path.exists(audio_path):
        print(f"❌ 音频文件不存在: {audio_path}")
        sys.exit(1)

    with open(audio_path, "rb") as f:
        raw = f.read()

    duration_sec = 0.0
    sample_rate = 0
    channels = 0
    sample_width = 2
    header_duration = 0.0
    size_duration = 0.0
    try:
        with wave.open(audio_path, "rb") as wf:
            sample_rate = wf.getframerate()
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            frames = wf.getnframes()
            header_duration = frames / float(sample_rate) if sample_rate else 0.0
            pcm_bytes = max(len(raw) - 44, 0)
            size_duration = _duration_from_pcm_bytes(
                pcm_bytes, sample_rate, channels, sample_width
            )
            header_ok = (
                0 < header_duration <= MAX_REASONABLE_AUDIO_SECONDS
                and size_duration > 0
                and abs(header_duration - size_duration) / size_duration < 0.25
            )
            duration_sec = header_duration if header_ok else size_duration
    except wave.Error:
        sample_rate = 16000
        channels = 1
        sample_width = 2
        duration_sec = max(len(raw) / (sample_rate * channels * sample_width), 0.001)

    if duration_sec <= 0:
        duration_sec = 0.001

    if header_duration > MAX_REASONABLE_AUDIO_SECONDS and size_duration > 0:
        print(
            f"  ⚠️ WAV 头时长异常 ({header_duration:.1f}s)，已按文件大小估算为 {duration_sec:.2f}s"
        )

    return AudioMeta(
        path=audio_path,
        duration_sec=duration_sec,
        file_bytes=len(raw),
        sample_rate=sample_rate,
        channels=channels,
        b64_data=base64.b64encode(raw).decode("utf-8"),
    )


def build_payload(b64_data: str, model: str = "funasr-iic", hotwords: str = "") -> dict:
    """构造 POST 请求的 JSON  body，与 FunASR 接口约定一致"""
    return {
        "model": model,
        "input_type": "stream",
        "input": b64_data,
        "hotwords": hotwords,
        "language": "zh",
    }


def extract_text(body: Any) -> str:
    """从接口返回的 JSON 里取出识别文本（兼容多种字段名和嵌套结构）"""
    if isinstance(body, dict):
        for key in ("text", "result", "transcription"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
        data = body.get("data")
        if isinstance(data, (dict, list)):
            nested = extract_text(data)
            if nested:
                return nested
    if isinstance(body, list) and body:
        return " ".join(extract_text(item) for item in body).strip()
    return ""


def calc_cer(reference: str, hypothesis: str) -> Optional[float]:
    """字错误率 Character Error Rate：0 表示完全一致，越大越差"""
    if not reference or not hypothesis:
        return None
    if jiwer_cer is not None:
        return float(jiwer_cer(reference, hypothesis))
    ref, hyp = reference.replace(" ", ""), hypothesis.replace(" ", "")
    if not ref:
        return None
    matches = sum(1 for a, b in zip(ref, hyp) if a == b)
    return 1.0 - matches / max(len(ref), len(hyp))


def calc_wer(reference: str, hypothesis: str) -> Optional[float]:
    """词错误率 Word Error Rate：按空格分词后比较"""
    if not reference or not hypothesis:
        return None
    if jiwer_wer is not None:
        return float(jiwer_wer(reference, hypothesis))
    ref_words = reference.split()
    hyp_words = hypothesis.split()
    if not ref_words:
        return None
    matches = sum(1 for a, b in zip(ref_words, hyp_words) if a == b)
    return 1.0 - matches / max(len(ref_words), len(hyp_words))


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
    cps = [r.chars_per_sec for r in successes if r.chars_per_sec > 0]
    chars = [r.text_chars for r in successes]
    cers = [r.cer for r in successes if r.cer is not None]
    wers = [r.wer for r in successes if r.wer is not None]
    empty_count = sum(1 for r in successes if r.text_chars == 0)

    total_audio_sec = audio_duration * len(successes)
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
        avg_chars_per_sec=statistics.mean(cps) if cps else 0.0,
        avg_text_chars=statistics.mean(chars) if chars else 0.0,
        empty_text_rate=empty_count / len(successes) * 100,
        avg_cer=statistics.mean(cers) if cers else None,
        p95_cer=percentile(cers, 95) if cers else None,
        avg_wer=statistics.mean(wers) if wers else None,
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
    total = len(results)
    success_count = len(successes)
    success_rate = (success_count / total * 100) if total else 0.0
    error_types: dict[str, int] = {}
    for r in results:
        if not r.success and r.error_type:
            error_types[r.error_type] = error_types.get(r.error_type, 0) + 1

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
        error_types=error_types,
        extra=extra or {},
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
    print(f"  │ 尝试 QPS:     {m.attempt_qps:.3f}")
    print(f"  │ 成功 QPS:     {m.success_qps:.3f}")
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
    print(f"  │ 延迟:         平均 {a.avg_latency:.3f}s | P50 {a.p50_latency:.3f}s | "
          f"P95 {a.p95_latency:.3f}s | P99 {a.p99_latency:.3f}s")
    print(f"  │ TTFB:         平均 {a.avg_ttfb:.3f}s | P95 {a.p95_ttfb:.3f}s")
    print(f"  │ RTF:          平均 {a.avg_rtf:.3f}x ({realtime_tag}) | P50 {a.p50_rtf:.3f}x | "
          f"P95 {a.p95_rtf:.3f}x | 最大 {a.max_rtf:.3f}x")
    print(f"  │ 实时占比:     {a.realtime_ratio:.1f}% 请求 RTF<1")
    print(f"  │ 识别速度:     {a.avg_chars_per_sec:.1f} 字/秒 | 平均 {a.avg_text_chars:.0f} 字/条")
    print(f"  │ 空结果率:     {a.empty_text_rate:.2f}%")
    print(f"  │ 音频吞吐:     {a.audio_throughput_x:.3f}x (成功音频秒/墙钟秒)")
    print(f"  │ CER:          平均 {_fmt_pct_opt(a.avg_cer)} | P95 {_fmt_pct_opt(a.p95_cer)}")
    print(f"  │ WER:          平均 {_fmt_pct_opt(a.avg_wer)}")
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
) -> RequestResult:
    """
    发送单次 POST 请求到 ASR 服务。
    session 可复用（连接池）；每次返回一个 RequestResult。
    """
    result = RequestResult()
    start = time.perf_counter()  # 高精度计时

    try:
        async with session.post(
            ctx.url,
            json=payload,
            ssl=SSL_CTX if ctx.use_ssl else False,
            timeout=aiohttp.ClientTimeout(total=ctx.timeout_s),
        ) as resp:
            # 进入 async with 且拿到 resp 时，响应头已到 → 记 TTFB
            ttfb = time.perf_counter() - start
            result.ttfb_sec = ttfb

            if resp.status == 200:
                body = await resp.json()
                result.success = True
                result.text = extract_text(body)
                result.text_chars = len(result.text)
            else:
                body_text = await resp.text()
                result.error = f"HTTP {resp.status}: {body_text[:200]}"
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
    except Exception as e:
        result.error = str(e)[:120]
        result.error_type = "Other"

    result.latency_sec = time.perf_counter() - start
    if result.ttfb_sec <= 0:
        result.ttfb_sec = result.latency_sec
    result.body_read_sec = max(result.latency_sec - result.ttfb_sec, 0.0)

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
    reference=True 时会带参考文本算 CER（accuracy_load 用）。
    """
    results: list[RequestResult] = []
    error_types: dict[str, int] = {}
    lock = asyncio.Lock()  # 多协程写 results 列表需要加锁
    level_start = time.perf_counter()
    stop_at = level_start + duration_sec

    # 大部分压测不需要每条都算 CER，可关掉以省 CPU
    ref_ctx = ctx
    if not reference:
        ref_ctx = TestRunContext(
            url=ctx.url,
            audio=ctx.audio,
            model=ctx.model,
            timeout_s=ctx.timeout_s,
            use_ssl=ctx.use_ssl,
            reference_text=None,
            hotwords=ctx.hotwords,
        )

    async def worker(session: aiohttp.ClientSession) -> None:
        """单个工人：时间没到就不断发请求"""
        while time.perf_counter() < stop_at:
            payload = build_payload(ctx.audio.b64_data, model=ctx.model, hotwords=ctx.hotwords)
            res = await send_one_request(session, ref_ctx, payload)
            async with lock:
                results.append(res)
                if not res.success and res.error_type:
                    error_types[res.error_type] = error_types.get(res.error_type, 0) + 1

    connector = aiohttp.TCPConnector(limit=max(concurrency + 5, 10))
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [asyncio.create_task(worker(session)) for _ in range(concurrency)]
        # 错峰启动：避免第一瞬间同时打出 concurrency 个请求
        for i, t in enumerate(tasks):
            if i < concurrency - 1 and stagger_sec > 0:
                await asyncio.sleep(stagger_sec)
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
    print_test_banner(TestKind.BASELINE, "单请求基准 — 各模型延迟 / RTF / 准确度")
    print(
        f"\n  {'模型':<14} {'轮次':<5} {'延迟(s)':<10} {'RTF':<8} {'字/秒':<10} "
        f"{'TTFB(s)':<10} {'CER':<8} {'WER':<8} {'状态'}"
    )
    print("  " + "-" * 86)

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
            )
            results: list[RequestResult] = []
            print(f"\n  [{model}]")
            t0 = time.perf_counter()
            for r in range(rounds):
                payload = build_payload(ctx.audio.b64_data, model=model, hotwords=ctx.hotwords)
                res = await send_one_request(session, model_ctx, payload)
                if res.success:
                    results.append(res)
                cer_s = _fmt_pct_opt(res.cer) if res.cer is not None else "-"
                wer_s = _fmt_pct_opt(res.wer) if res.wer is not None else "-"
                tag = "✓" if res.success else "✗"
                preview = (res.text[:26] + "…") if len(res.text) > 26 else res.text
                if not res.success:
                    preview = (res.error or "")[:26]
                print(
                    f"  {model:<12} {r + 1:<5} {res.latency_sec:<10.3f} {res.rtf:<8.3f} "
                    f"{res.chars_per_sec:<10.1f} {res.ttfb_sec:<10.3f} {cer_s:<8} {wer_s:<8} {tag}  {preview}"
                )
                await asyncio.sleep(0.5)
            wall = time.perf_counter() - t0
            m = build_stress_metrics(
                TestKind.BASELINE.value, results, wall, ctx.audio.duration_sec, concurrency=1
            )
            m.extra["model"] = model
            m.extra["rounds"] = rounds
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
        f"\n  {'并发':<6} {'总数':<6} {'成功':<6} {'成功率':<8} {'成功QPS':<9} "
        f"{'P95延迟':<9} {'平均RTF':<9} {'音频吞吐x':<10}"
    )
    print("  " + "-" * 78)

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
            f"{m.success_qps:<9.3f} {m.asr.p95_latency:<9.3f} {m.asr.avg_rtf:<9.3f} "
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
    phases: list[tuple[str, int, int, list[RequestResult]]] = []
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
        phases.append((phase_name, conc, dur, phase_results))
        print_stress_metrics(pm, focus=phase_name)
        print_asr_metrics(pm.asr, ctx.audio)

    m = build_stress_metrics(
        TestKind.SPIKE.value, all_results, total_wall, ctx.audio.duration_sec,
        concurrency=spike_concurrency,
    )
    spike_phase = next(p for p in phases if p[0] == "尖峰")
    spike_m = build_stress_metrics(
        "spike_peak", spike_phase[3], spike_phase[2], ctx.audio.duration_sec,
        concurrency=spike_concurrency,
    )
    m.extra["尖峰成功率"] = f"{spike_m.success_rate:.1f}%"
    m.extra["尖峰P95延迟"] = f"{spike_m.asr.p95_latency:.3f}s"
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
    bucket_latencies: list[tuple[int, float, float]] = []
    lock = asyncio.Lock()
    start = time.perf_counter()
    stop_at = start + duration_sec
    bucket_idx = 0
    bucket_start = start
    bucket_lats: list[float] = []

    async def worker(session: aiohttp.ClientSession) -> None:
        nonlocal bucket_idx, bucket_start, bucket_lats
        while time.perf_counter() < stop_at:
            payload = build_payload(ctx.audio.b64_data, model=ctx.model, hotwords=ctx.hotwords)
            ref_ctx = TestRunContext(
                url=ctx.url, audio=ctx.audio, model=ctx.model,
                timeout_s=ctx.timeout_s, use_ssl=ctx.use_ssl,
            )
            res = await send_one_request(session, ref_ctx, payload)
            async with lock:
                results.append(res)
                if res.success:
                    bucket_lats.append(res.latency_sec)
                now = time.perf_counter()
                if now - bucket_start >= bucket_sec:
                    if bucket_lats:
                        bucket_latencies.append((
                            bucket_idx,
                            statistics.mean(bucket_lats),
                            percentile(bucket_lats, 95),
                        ))
                    bucket_idx += 1
                    bucket_start = now
                    bucket_lats = []

    connector = aiohttp.TCPConnector(limit=max(concurrency + 5, 10))
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*[worker(session) for _ in range(concurrency)])

    wall = time.perf_counter() - start
    m = build_stress_metrics(
        TestKind.SOAK.value, results, wall, ctx.audio.duration_sec, concurrency=concurrency
    )

    if len(bucket_latencies) >= 2:
        first_p95 = bucket_latencies[0][2]
        last_p95 = bucket_latencies[-1][2]
        drift = ((last_p95 - first_p95) / first_p95 * 100) if first_p95 > 0 else 0.0
        m.extra["首桶P95延迟"] = f"{first_p95:.3f}s"
        m.extra["末桶P95延迟"] = f"{last_p95:.3f}s"
        m.extra["P95漂移"] = f"{drift:+.1f}%"

    print("\n  分桶延迟趋势 (平均 / P95):")
    for idx, avg_l, p95_l in bucket_latencies:
        print(f"    桶 {idx + 1:>2}: 平均 {avg_l:.3f}s | P95 {p95_l:.3f}s")
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
    results: list[RequestResult] = []
    lock = asyncio.Lock()
    interval = 1.0 / target_qps if target_qps > 0 else 1.0  # 两次请求之间的间隔
    start = time.perf_counter()
    stop_at = start + duration_sec
    sem = asyncio.Semaphore(max_in_flight)  # 信号量：超过 max_in_flight 会等待

    async def one_shot(session: aiohttp.ClientSession) -> None:
        async with sem:
            payload = build_payload(ctx.audio.b64_data, model=ctx.model, hotwords=ctx.hotwords)
            ref_ctx = TestRunContext(
                url=ctx.url, audio=ctx.audio, model=ctx.model,
                timeout_s=ctx.timeout_s, use_ssl=ctx.use_ssl,
            )
            res = await send_one_request(session, ref_ctx, payload)
            async with lock:
                results.append(res)

    connector = aiohttp.TCPConnector(limit=max(max_in_flight + 5, 20))
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks: list[asyncio.Task[None]] = []
        next_send = start
        while time.perf_counter() < stop_at:
            now = time.perf_counter()
            if now >= next_send:
                tasks.append(asyncio.create_task(one_shot(session)))
                next_send += interval
            else:
                await asyncio.sleep(min(0.001, next_send - now))
        if tasks:
            await asyncio.gather(*tasks)

    wall = time.perf_counter() - start
    planned = int(duration_sec * target_qps)
    m = build_stress_metrics(
        TestKind.FIXED_QPS.value, results, wall, ctx.audio.duration_sec,
        target_qps=target_qps,
    )
    m.extra["计划请求数"] = planned
    m.extra["实际发出"] = len(results)
    m.extra["达成率"] = f"{len(results) / planned * 100:.1f}%" if planned else "N/A"
    print_combined_result(m, ctx.audio, focus=f"目标QPS={target_qps}")
    return m


async def run_accuracy_load(
    ctx: TestRunContext,
    concurrency: int,
    duration_sec: int,
) -> StressAggMetrics:
    """
    【并发准确度】高并发下仍计算每条 CER，看压测是否导致识别变差或结果不一致。
    必须提供 --reference-text。
    """
    if not ctx.reference_text:
        print("\n  ⚠️ accuracy_load 需要 --reference-text，已跳过")
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
            unique_ratio = len(set(texts)) / len(texts) * 100
            m.extra["结果一致性"] = f"{100 - unique_ratio:.1f}% 相同"
    sample = successes[0].text if successes else ""
    print_combined_result(m, ctx.audio, focus="并发准确度", sample_text=sample)
    return m


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
            cer = _fmt_pct_opt(a.get("avg_cer") if isinstance(a, dict) else None)
            print(
                f"    {model}: 延迟 {a.get('avg_latency', 0):.3f}s | RTF {a.get('avg_rtf', 0):.3f}x | "
                f"CER {cer} | 成功率 {m.get('success_rate', 0):.1f}%"
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

    for key in ("sustained", "spike", "soak", "fixed_qps", "accuracy_load"):
        entry = report.get(key)
        if not entry:
            continue
        if isinstance(entry, dict) and "success_rate" in entry:
            a = entry.get("asr", {})
            cer = _fmt_pct_opt(a.get("avg_cer") if isinstance(a, dict) else getattr(a, "avg_cer", None))
            print(
                f"\n  {key}: 成功率 {entry['success_rate']:.1f}% | "
                f"成功QPS {entry['success_qps']:.3f} | "
                f"P95延迟 {a.get('p95_latency', 0) if isinstance(a, dict) else 0:.3f}s | CER {cer}"
            )

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

    ctx = TestRunContext(
        url=args.url,
        audio=audio,
        model=args.stress_model or args.models[0],
        timeout_s=args.timeout,
        use_ssl=use_ssl,
        reference_text=args.reference_text,
        hotwords=args.hotwords,
    )

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ASR 压力测试全集启动")
    print(f"  目标:     {args.url}")
    print(f"  音频:     {audio.path} ({audio.duration_sec:.2f}s, {audio.file_kb}KB)")
    print(f"  压测模型: {ctx.model}")
    print(f"  测试项:   {', '.join(tests)}")
    if args.reference_text:
        print(f"  参考文本: {args.reference_text[:50]}{'…' if len(args.reference_text) > 50 else ''}")
    if args.reference_text and not jiwer_cer:
        print("  ⚠️ 未安装 jiwer: pip install jiwer 可获得标准 CER/WER")

    report: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "audio": {
            "path": audio.path,
            "duration_sec": round(audio.duration_sec, 3),
            "file_kb": audio.file_kb,
            "b64_kb": audio.b64_kb,
        },
        "config": {
            "url": args.url,
            "model": ctx.model,
            "tests": tests,
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
    return names


def main() -> None:
    """命令行入口：定义所有参数，最后 asyncio.run(run(args)) 启动异步主流程"""
    parser = argparse.ArgumentParser(
        description="FunASR ASR 压力测试全集 (压力指标 + ASR 专项指标)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", default=DEFAULT_API_URL)
    parser.add_argument("--audio", default=DEFAULT_AUDIO)
    parser.add_argument("--models", nargs="+", default=["funasr-iic", "funasr-nano"])
    parser.add_argument("--stress-model", default=None, help="除 baseline 外压测使用的模型")
    parser.add_argument("--hotwords", default="")
    parser.add_argument(
        "--tests",
        default="all",
        help=f"逗号分隔或 all。可选: {', '.join(ALL_TEST_NAMES)}",
    )
    parser.add_argument("--reference-text", default=None, help="参考文本 (CER/WER/accuracy_load)")
    parser.add_argument("--output", default=None, help="JSON 报告路径")

    # ---------- ramp 递增并发相关参数 ----------
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 2, 4, 8, 16, 32])
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

    args = parser.parse_args()
    asyncio.run(run(args))


# 直接运行本文件时执行 main；被 import 时不会自动跑压测
if __name__ == "__main__":
    main()
