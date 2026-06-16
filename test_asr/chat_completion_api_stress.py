#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Qwen3-ASR (chat/completions) 语音识别服务压力测试工具

阶段1: 单请求基准 — 延迟 / RTF / 识别速度
阶段2: 递增并发压测 — QPS、成功率、P50/P95 延迟
阶段3: 汇总 — 压力测试指标 + ASR 专项指标

说明:
  接口: POST /v1/chat/completions，音频通过 messages[].content[].audio_url 传入。
  响应文本: choices[0].message.content
  本接口为非流式 JSON 响应，「TTFB」= 收到 HTTP 响应头时刻，通常接近总延迟。
  RTF (Real-Time Factor) = 推理耗时 / 音频时长，<1 表示快于实时。

用法:

"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

import aiohttp

try:
    from jiwer import cer as jiwer_cer
except ImportError:
    jiwer_cer = None

# ==================== 默认配置 ====================

DEFAULT_API_URL = "http://36.111.82.53:10018/v1/chat/completions"
DEFAULT_AUDIO_URL = (
    "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-ASR-Repo/asr_en.wav"
)
DEFAULT_HEADERS = {"Content-Type": "application/json"}
DEFAULT_URL_OPEN_TIMEOUT = 10

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 并发 worker 错峰：总 ramp 时间下限/上限（秒），高并发拉长时间避免连接洪峰
STAGGER_RAMP_MIN_SEC = 2.0
STAGGER_RAMP_MAX_SEC = 8.0
STAGGER_RAMP_PER_WORKER_SEC = 0.12


# ==================== 数据结构 ====================


@dataclass
class AudioMeta:
    """远程音频 URL，对应请求体 messages[].content[].audio_url.url。"""
    audio_url: str
    duration_sec: float
    duration_source: str = "unknown"


@dataclass
class RequestResult:
    success: bool = False
    latency_sec: float = 0.0
    ttfb_sec: float = 0.0
    body_read_sec: float = 0.0
    ended_at: float = 0.0  # perf_counter，用于区分发压窗口内/外完成的请求
    text: str = ""
    text_chars: int = 0
    rtf: float = 0.0
    chars_per_sec: float = 0.0
    cer: Optional[float] = None
    error: Optional[str] = None
    error_type: Optional[str] = None


@dataclass
class AsrAggMetrics:
    """ASR 专项聚合指标"""
    avg_latency: float = 0.0
    p50_latency: float = 0.0
    p95_latency: float = 0.0
    avg_ttfb: float = 0.0
    p95_ttfb: float = 0.0
    avg_rtf: float = 0.0
    p95_rtf: float = 0.0
    avg_chars_per_sec: float = 0.0
    avg_text_chars: float = 0.0
    avg_cer: Optional[float] = None
    # 音频处理能力: 成功识别的音频总时长 / 指定统计窗口
    audio_throughput_x: float = 0.0


@dataclass
class StressAggMetrics:
    """压力测试聚合指标"""
    concurrency: int = 0
    wall_sec: float = 0.0
    issue_sec: float = 0.0
    drain_sec: float = 0.0
    total_requests: int = 0
    success_count: int = 0
    success_rate: float = 0.0
    attempt_qps: float = 0.0
    success_qps: float = 0.0
    avg_latency: float = 0.0
    p50_latency: float = 0.0
    p95_latency: float = 0.0
    p99_latency: float = 0.0
    avg_ttfb: float = 0.0
    p95_ttfb: float = 0.0
    error_types: dict[str, int] = field(default_factory=dict)
    asr: AsrAggMetrics = field(default_factory=AsrAggMetrics)


# ==================== 工具函数 ====================


def fmt_seconds(value: float, digits: int = 3, unknown: str = "-") -> str:
    return f"{value:.{digits}f}" if value > 0 else unknown


def fmt_duration(audio: AudioMeta, digits: int = 2) -> str:
    return f"{audio.duration_sec:.{digits}f}s" if audio.duration_sec > 0 else "未知"


def fmt_rtf(value: float) -> str:
    return f"{value:.3f}" if value > 0 else "-"


def fmt_rtf_x(value: float) -> str:
    return f"{value:.3f}x" if value > 0 else "-"


def infer_wav_duration(audio_url: str) -> Optional[float]:
    """从 WAV 头估算远程音频时长；失败时返回 None，不制造假时长。"""
    request = urllib.request.Request(
        audio_url,
        headers={
            "Range": "bytes=0-65535",
            "User-Agent": "asr-stress-audio-duration-probe/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_URL_OPEN_TIMEOUT) as response:
            header_bytes = response.read(65536)
        with wave.open(io.BytesIO(header_bytes), "rb") as wav:
            frame_rate = wav.getframerate()
            frame_count = wav.getnframes()
            if frame_rate > 0 and frame_count > 0:
                return frame_count / float(frame_rate)
    except (OSError, EOFError, wave.Error, urllib.error.URLError, TimeoutError):
        return None
    return None


def load_audio_meta(audio_url: str, audio_duration: Optional[float] = None) -> AudioMeta:
    if audio_duration and audio_duration > 0:
        return AudioMeta(audio_url=audio_url, duration_sec=audio_duration, duration_source="--audio-duration")

    inferred_duration = infer_wav_duration(audio_url)
    if inferred_duration and inferred_duration > 0:
        print(f"  ℹ️ 未指定 --audio-duration，已从 WAV 头自动识别音频时长: {inferred_duration:.2f}s")
        return AudioMeta(audio_url=audio_url, duration_sec=inferred_duration, duration_source="wav_header")

    print("  ⚠️ 未指定 --audio-duration，且无法自动识别音频时长；RTF/音频吞吐将显示为 - 或 0")
    return AudioMeta(audio_url=audio_url, duration_sec=0.0, duration_source="unknown")


def build_payload(audio_url: str) -> dict:
    """与公司 demo 一致的请求体：仅 messages + audio_url。"""
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "audio_url",
                        "audio_url": {"url": audio_url},
                    },
                ],
            },
        ],
    }


def extract_text(body: Any) -> str:
    if isinstance(body, dict):
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content:
                        return content
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
    if not reference or not hypothesis:
        return None
    if jiwer_cer is not None:
        return jiwer_cer(reference, hypothesis)
    # 无 jiwer 时用字符相等率近似
    ref, hyp = reference.replace(" ", ""), hypothesis.replace(" ", "")
    if not ref:
        return None
    matches = sum(1 for a, b in zip(ref, hyp) if a == b)
    return 1.0 - matches / max(len(ref), len(hyp))


def percentile(values: list[float], p: float) -> float:
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
    successes: list[RequestResult],
    audio_duration: float,
    wall_sec: Optional[float] = None,
) -> AsrAggMetrics:
    if not successes:
        return AsrAggMetrics()

    latencies = [r.latency_sec for r in successes]
    ttfbs = [r.ttfb_sec for r in successes]
    rtfs = [r.rtf for r in successes if r.rtf > 0]
    cps = [r.chars_per_sec for r in successes if r.chars_per_sec > 0]
    chars = [r.text_chars for r in successes]
    cers = [r.cer for r in successes if r.cer is not None]

    total_audio_sec = audio_duration * len(successes)
    audio_tp = total_audio_sec / wall_sec if wall_sec and wall_sec > 0 else 0.0

    return AsrAggMetrics(
        avg_latency=statistics.mean(latencies),
        p50_latency=percentile(latencies, 50),
        p95_latency=percentile(latencies, 95),
        avg_ttfb=statistics.mean(ttfbs),
        p95_ttfb=percentile(ttfbs, 95),
        avg_rtf=statistics.mean(rtfs) if rtfs else 0.0,
        p95_rtf=percentile(rtfs, 95) if rtfs else 0.0,
        avg_chars_per_sec=statistics.mean(cps) if cps else 0.0,
        avg_text_chars=statistics.mean(chars) if chars else 0.0,
        avg_cer=statistics.mean(cers) if cers else None,
        audio_throughput_x=audio_tp,
    )


def calc_stagger_delay(concurrency: int) -> float:
    """计算 worker 启动间隔，使高并发下总 ramp 时间落在 2~8 秒。"""
    if concurrency <= 1:
        return 0.0
    total_ramp = min(
        STAGGER_RAMP_MAX_SEC,
        max(STAGGER_RAMP_MIN_SEC, concurrency * STAGGER_RAMP_PER_WORKER_SEC),
    )
    return total_ramp / (concurrency - 1)


def aggregate_attempt_latency(results: list[RequestResult]) -> dict[str, float]:
    """压力测试延迟指标按全部请求统计，避免失败/超时请求被隐藏。"""
    if not results:
        return {
            "avg_latency": 0.0,
            "p50_latency": 0.0,
            "p95_latency": 0.0,
            "p99_latency": 0.0,
            "avg_ttfb": 0.0,
            "p95_ttfb": 0.0,
        }

    latencies = [r.latency_sec for r in results]
    ttfbs = [r.ttfb_sec for r in results]
    return {
        "avg_latency": statistics.mean(latencies),
        "p50_latency": percentile(latencies, 50),
        "p95_latency": percentile(latencies, 95),
        "p99_latency": percentile(latencies, 99),
        "avg_ttfb": statistics.mean(ttfbs),
        "p95_ttfb": percentile(ttfbs, 95),
    }


# ==================== 单次请求 ====================


async def send_one_request(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict,
    audio_duration: float,
    timeout_s: float,
    reference_text: Optional[str] = None,
    use_ssl: bool = False,
) -> RequestResult:
    result = RequestResult()
    start = time.perf_counter()

    try:
        async with session.post(
            url,
            json=payload,
            headers=DEFAULT_HEADERS,
            ssl=SSL_CTX if use_ssl else False,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
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
        result.error = f"Timeout ({timeout_s}s)"
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
    result.ended_at = start + result.latency_sec
    if result.ttfb_sec <= 0:
        result.ttfb_sec = result.latency_sec
    result.body_read_sec = max(result.latency_sec - result.ttfb_sec, 0.0)

    if result.success and audio_duration > 0:
        result.rtf = result.latency_sec / audio_duration
        if result.latency_sec > 0:
            result.chars_per_sec = result.text_chars / result.latency_sec
        if reference_text:
            result.cer = calc_cer(reference_text, result.text)

    return result


# ==================== 阶段1: 基准测试 ====================


async def phase1_baseline(
    url: str,
    audio: AudioMeta,
    rounds: int,
    timeout_s: float,
    reference_text: Optional[str],
    use_ssl: bool,
) -> dict[str, Any]:
    print("\n" + "=" * 100)
    print("阶段1: 单请求基准测试")
    print("=" * 100)
    print(f"  音频时长: {fmt_duration(audio)}")
    print(
        f"\n{'轮次':<5} {'音频(s)':<8} {'延迟(s)':<10} {'RTF':<8} {'字/秒':<10} "
        f"{'首包(s)':<10} {'字数':<6} {'CER':<8} {'状态'}"
    )
    print("-" * 100)

    payload = build_payload(audio.audio_url)
    successes: list[RequestResult] = []

    async with aiohttp.ClientSession() as session:
        for r in range(rounds):
            res = await send_one_request(
                session, url, payload, audio.duration_sec, timeout_s, reference_text, use_ssl
            )
            if res.success:
                successes.append(res)

            cer_str = f"{res.cer:.2%}" if res.cer is not None else "-"
            tag = "✓" if res.success else "✗"
            preview = (res.text[:28] + "…") if len(res.text) > 28 else res.text
            if not res.success:
                preview = (res.error or "")[:28]

            print(
                f"  {r + 1:<5} {fmt_seconds(audio.duration_sec, 2):<8} {res.latency_sec:<10.3f} {fmt_rtf(res.rtf):<8} "
                f"{res.chars_per_sec:<10.1f} {res.ttfb_sec:<10.3f} {res.text_chars:<6} "
                f"{cer_str:<8} {tag}  {preview}"
            )
            await asyncio.sleep(0.5)

    agg = aggregate_asr(successes, audio.duration_sec)
    sr = len(successes) / rounds * 100 if rounds else 0
    cer_avg = f"{agg.avg_cer:.2%}" if agg.avg_cer is not None else "-"
    print(
        f"  {'↳ 平均':<5} {fmt_seconds(audio.duration_sec, 2):<8} {agg.avg_latency:<10.3f} {fmt_rtf(agg.avg_rtf):<8} "
        f"{agg.avg_chars_per_sec:<10.1f} {agg.avg_ttfb:<10.3f} {agg.avg_text_chars:<6.0f} "
        f"{cer_avg:<8} {sr:.0f}%"
    )
    print(
        f"  ↳ 首包统计: 平均 {agg.avg_ttfb:.3f}s | P95 {agg.p95_ttfb:.3f}s"
    )

    return {
        "rounds": rounds,
        "success_rate": sr,
        "asr": asdict(agg),
        "sample_text": successes[-1].text if successes else "",
    }


# ==================== 阶段2: 并发压测 ====================


async def phase2_concurrency(
    url: str,
    audio: AudioMeta,
    concurrency_levels: list[int],
    duration_per_level: int,
    timeout_s: float,
    stop_success_rate: float,
    use_ssl: bool,
) -> list[StressAggMetrics]:
    print("\n" + "=" * 88)
    print(f"阶段2: 递增并发压测 (每级持续 {duration_per_level}s, 超时 {timeout_s}s)")
    print("=" * 88)
    print(
        "统计口径: 尝试QPS=发起请求数/发压窗口; 成功QPS和音频吞吐=成功完成数或成功音频秒/完整观测窗口; "
        "尾部耗时在总结中以 drain_sec 展示。"
    )

    print(f"  音频时长: {fmt_duration(audio)}")
    print(
        f"\n{'并发':<6} {'总数':<6} {'成功':<6} {'成功率':<8} {'成功QPS':<9} {'尝试QPS':<9} "
        f"{'音频(s)':<8} {'平均RTF':<9} {'首包均':<9} {'首包P95':<9} {'P95总延迟':<9} "
        f"{'音频吞吐x':<10} {'错误'}"
    )
    print("-" * 118)

    all_levels: list[StressAggMetrics] = []

    for concurrency in concurrency_levels:
        results: list[RequestResult] = []
        error_types: dict[str, int] = {}
        lock = asyncio.Lock()
        level_start = time.perf_counter()
        stop_at = level_start + duration_per_level
        first_request_start: Optional[float] = None
        last_response_end: Optional[float] = None

        async def worker(session: aiohttp.ClientSession) -> None:
            nonlocal first_request_start, last_response_end
            while True:
                request_start = time.perf_counter()
                if request_start >= stop_at:
                    break
                if first_request_start is None or request_start < first_request_start:
                    first_request_start = request_start
                payload = build_payload(audio.audio_url)
                res = await send_one_request(
                    session, url, payload, audio.duration_sec, timeout_s, None, use_ssl
                )
                request_end = res.ended_at or time.perf_counter()
                if last_response_end is None or request_end > last_response_end:
                    last_response_end = request_end
                async with lock:
                    results.append(res)
                    if not res.success and res.error_type:
                        error_types[res.error_type] = error_types.get(res.error_type, 0) + 1

        connector = aiohttp.TCPConnector(limit=max(concurrency + 5, 10))
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = []
            stagger_delay = calc_stagger_delay(concurrency)
            for i in range(concurrency):
                tasks.append(asyncio.create_task(worker(session)))
                if i < concurrency - 1:
                    await asyncio.sleep(stagger_delay)
            await asyncio.gather(*tasks)

        level_end = time.perf_counter()
        measure_start = first_request_start or level_start
        measure_end = last_response_end or level_end
        wall_sec = max(measure_end - measure_start, 0.001)
        issue_sec = max(stop_at - measure_start, 0.001)
        drain_sec = max(measure_end - stop_at, 0.0)
        successes = [r for r in results if r.success]
        total = len(results)
        success_count = len(successes)
        success_rate = (success_count / total * 100) if total else 0.0
        # 闭环压测口径：发起速率看发压窗口，完成吞吐看完整观测窗口，避免长音频全落在 drain 后变 0 或按发压窗口虚高。
        attempt_qps = total / issue_sec if issue_sec > 0 else 0.0
        success_qps = success_count / wall_sec if wall_sec > 0 else 0.0

        asr_agg = aggregate_asr(successes, audio.duration_sec, wall_sec)
        attempt_latency = aggregate_attempt_latency(results)
        level = StressAggMetrics(
            concurrency=concurrency,
            wall_sec=wall_sec,
            issue_sec=issue_sec,
            drain_sec=drain_sec,
            total_requests=total,
            success_count=success_count,
            success_rate=success_rate,
            attempt_qps=attempt_qps,
            success_qps=success_qps,
            avg_latency=attempt_latency["avg_latency"],
            p50_latency=attempt_latency["p50_latency"],
            p95_latency=attempt_latency["p95_latency"],
            p99_latency=attempt_latency["p99_latency"],
            avg_ttfb=attempt_latency["avg_ttfb"],
            p95_ttfb=attempt_latency["p95_ttfb"],
            error_types=error_types,
            asr=asr_agg,
        )
        all_levels.append(level)

        err_str = ", ".join(f"{k}:{v}" for k, v in sorted(error_types.items())) or "-"
        stable = success_rate >= stop_success_rate
        tag = "✓" if stable else "✗"
        print(
            f"{concurrency:<6} {total:<6} {success_count:<6} {success_rate:>6.1f}% "
            f"{success_qps:<9.3f} {attempt_qps:<9.3f} {fmt_seconds(audio.duration_sec, 2):<8} "
            f"{fmt_rtf(asr_agg.avg_rtf):<9} {level.avg_ttfb:<9.3f} {level.p95_ttfb:<9.3f} "
            f"{level.p95_latency:<9.3f} {asr_agg.audio_throughput_x:<10.3f} {err_str}  {tag}"
        )

        if success_rate < stop_success_rate:
            print(
                f"\n⚠️ 并发 {concurrency} 成功率 {success_rate:.1f}% < {stop_success_rate:.0f}%，停止递增"
            )
            break
        await asyncio.sleep(2)

    return all_levels


# ==================== 阶段3: 汇总 ====================


def print_phase3_summary(
    audio: AudioMeta,
    baseline: dict[str, Any],
    stress_levels: list[StressAggMetrics],
    stop_success_rate: float,
) -> dict[str, Any]:
    print("\n" + "=" * 88)
    print("阶段3: 测试总结")
    print("=" * 88)
    print(
        "  [统计口径] 尝试QPS=发起请求数/issue_sec(首次发压→停止发压); "
        "成功QPS=成功完成数/wall_sec(首次发压→最后响应); "
        "音频吞吐=成功音频秒/wall_sec; 延迟P95/P99: 全部请求; ASR的RTF/字速: 成功请求; "
        "尾部(s)=drain_sec(停止发压后等待在途请求结束)."
    )

    report: dict[str, Any] = {
        "metrics_legend": (
            "attempt_qps: started requests / issue_sec; success_qps: successful completions / wall_sec; "
            "audio_throughput_x: successful audio seconds / wall_sec; latency percentiles: all requests; "
            "asr metrics: successful requests; drain_sec: post-stop drain."
        ),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "audio": {
            "audio_url": audio.audio_url,
            "duration_sec": round(audio.duration_sec, 3),
            "duration_source": audio.duration_source,
        },
        "baseline": baseline,
        "stress": [asdict(s) for s in stress_levels],
    }

    # ----- ASR 专项 -----
    print("\n【ASR 专项指标 — 基准 (单请求)】")
    print(f"  音频 URL: {audio.audio_url}")
    print(f"  音频时长: {fmt_duration(audio)} (来源: {audio.duration_source})")
    a = baseline["asr"]
    cer_s = f"{a['avg_cer']:.2%}" if a.get("avg_cer") is not None else "N/A"
    realtime = "未知" if a["avg_rtf"] <= 0 else ("实时" if a["avg_rtf"] < 1 else "非实时")
    print(f"    延迟: 平均 {a['avg_latency']:.3f}s | P50 {a['p50_latency']:.3f}s | P95 {a['p95_latency']:.3f}s")
    print(
        f"    首包: 平均 {a['avg_ttfb']:.3f}s | P95 {a.get('p95_ttfb', 0.0):.3f}s "
        f"(收到响应头，非流式下通常接近总延迟)"
    )
    print(f"    RTF:  平均 {fmt_rtf_x(a['avg_rtf'])} ({realtime}) | P95 {fmt_rtf_x(a['p95_rtf'])}")
    print(f"    识别: {a['avg_chars_per_sec']:.1f} 字/秒 | 平均 {a['avg_text_chars']:.0f} 字 | CER {cer_s}")
    if baseline.get("sample_text"):
        preview = baseline["sample_text"][:60] + ("…" if len(baseline["sample_text"]) > 60 else "")
        print(f"    样例: {preview}")

    if stress_levels:
        print("\n【ASR 专项指标 — 并发压测趋势】")
        for lv in stress_levels:
            a = lv.asr
            print(
                f"  并发 {lv.concurrency:>2}: 音频={fmt_duration(audio)} | "
                f"RTF={fmt_rtf_x(a.avg_rtf)} | 首包均={lv.avg_ttfb:.3f}s 首包P95={lv.p95_ttfb:.3f}s | "
                f"成功P95延迟={a.p95_latency:.3f}s | "
                f"音频吞吐={a.audio_throughput_x:.3f}x (成功音频秒/完整观测秒) | "
                f"字/秒={a.avg_chars_per_sec:.1f}"
            )

    # ----- 压力测试 -----
    print("\n【压力测试指标】")
    if not stress_levels:
        print("  无并发数据")
        return report

    total_errors: dict[str, int] = {}
    for lv in stress_levels:
        for k, v in lv.error_types.items():
            total_errors[k] = total_errors.get(k, 0) + v

    best_stable: Optional[StressAggMetrics] = None
    best_qps = 0.0
    for lv in stress_levels:
        if lv.success_rate >= stop_success_rate:
            if best_stable is None or lv.concurrency > best_stable.concurrency:
                best_stable = lv
            if lv.success_qps > best_qps:
                best_qps = lv.success_qps

    print(
        f"  {'并发':<6} {'成功率':<8} {'成功QPS':<10} {'尝试QPS':<10} "
        f"{'音频(s)':<8} {'首包P95':<10} {'P95总延迟':<10} {'P99总延迟':<10} "
        f"{'尾部(s)':<8} {'错误'}"
    )
    print("  " + "-" * 108)
    for lv in stress_levels:
        err = ", ".join(f"{k}:{v}" for k, v in lv.error_types.items()) or "-"
        print(
            f"  {lv.concurrency:<6} {lv.success_rate:>6.1f}% {lv.success_qps:<10.3f} "
            f"{lv.attempt_qps:<10.3f} {fmt_seconds(audio.duration_sec, 2):<8} {lv.p95_ttfb:<10.3f} "
            f"{lv.p95_latency:<10.3f} {lv.p99_latency:<10.3f} "
            f"{lv.drain_sec:<8.3f} {err}"
        )

    if best_stable:
        print(
            f"\n  ✅ 最大稳定并发: {best_stable.concurrency} "
            f"(成功率≥{stop_success_rate:.0f}%)"
        )
        print(f"  ✅ 峰值成功 QPS: {best_qps:.3f}")
        print(f"  ✅ 稳定档音频吞吐: {best_stable.asr.audio_throughput_x:.3f}x (按完整观测窗口)")
    else:
        print("\n  ❌ 未达到稳定成功率阈值")

    if total_errors:
        print("\n【错误诊断】")
        for err, cnt in sorted(total_errors.items(), key=lambda x: -x[1]):
            print(f"  {err}: {cnt} 次")
        hints = {
            "Timeout": "请求超时 — 队列积压或超时设置过短，可增大 --timeout",
            "ConnError": "连接失败/断开 — 服务端过载或连接数上限",
        }
        for key, hint in hints.items():
            if key in total_errors or any(k.startswith("HTTP_5") for k in total_errors):
                if key == "ConnError" or key in total_errors:
                    print(f"  💡 {hint}")
        if any(k.startswith("HTTP_5") for k in total_errors):
            print("  💡 HTTP 5xx: 服务端内部错误，检查 NPU 显存与推理日志")

    # 结合用户实测数据的建议
    max_rtf = max((lv.asr.avg_rtf for lv in stress_levels if lv.success_count and lv.asr.avg_rtf > 0), default=0)
    if max_rtf > 1.5:
        print("\n  💡 RTF > 1.5: 推理明显慢于实时，优先优化模型/硬件或缩短音频")
    if best_qps < 0.5:
        print("  💡 成功 QPS < 0.5: 长音频 + 高延迟场景，可适当延长 --duration 观察稳态")

    print("\n" + "=" * 88)
    return report


# ==================== 入口 ====================


async def run(args: argparse.Namespace) -> None:
    audio_url = args.audio_url or DEFAULT_AUDIO_URL
    audio = load_audio_meta(audio_url, args.audio_duration)
    use_ssl = args.url.lower().startswith("https")

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ASR 压力测试启动")
    print(f"  目标:     {args.url}")
    print(f"  音频 URL: {audio.audio_url}")
    print(f"  音频时长: {fmt_duration(audio)} (来源: {audio.duration_source}，用于 RTF)")
    print(f"  并发级别: {args.concurrency}")
    print(f"  超时:     {args.timeout}s")
    if args.reference_text:
        print(f"  参考文本: {args.reference_text[:50]}{'…' if len(args.reference_text) > 50 else ''}")
    if not jiwer_cer and args.reference_text:
        print("  ⚠️ 未安装 jiwer，CER 使用简化算法；pip install jiwer 可获准确 CER")

    baseline = await phase1_baseline(
        args.url,
        audio,
        args.rounds,
        args.timeout,
        args.reference_text,
        use_ssl,
    )

    stress_levels = await phase2_concurrency(
        args.url,
        audio,
        args.concurrency,
        args.duration,
        args.timeout,
        args.stop_success_rate,
        use_ssl,
    )

    report = print_phase3_summary(audio, baseline, stress_levels, args.stop_success_rate)

    if args.output:
        out_path = os.path.abspath(args.output)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n📄 报告已保存: {out_path}")

    print("\n测试全部完成\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Qwen3-ASR chat/completions 压力测试 (含 ASR 专项指标)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", default=DEFAULT_API_URL, help="ASR 服务地址 (/v1/chat/completions)")
    parser.add_argument(
        "--audio-url",
        default=DEFAULT_AUDIO_URL,
        help="音频 URL，写入请求体 audio_url.url",
    )
    parser.add_argument(
        "--audio-duration",
        type=float,
        default=None,
        help="音频时长(秒)，用于 RTF 计算",
    )
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 2, 4, 8, 16, 32, 64])
    parser.add_argument("--duration", type=int, default=30, help="每个并发级别持续秒数")
    parser.add_argument("--rounds", type=int, default=3, help="基准重复次数")
    parser.add_argument("--timeout", type=float, default=300, help="单次请求超时(秒)")
    parser.add_argument(
        "--stop-success-rate",
        type=float,
        default=90.0,
        help="低于该成功率时停止递增并发",
    )
    parser.add_argument("--reference-text", default=None, help="参考文本，用于计算 CER")
    parser.add_argument("--output", default=None, help="JSON 报告输出路径")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
