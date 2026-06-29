# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import csv
import hashlib
import hmac
import json
import mimetypes
import os
import random
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import formatdate
from pathlib import Path
from typing import Any, Optional
#
# # 智学网级Qwen3-235B-A22B-w8a8大模型
# APP_KEY = os.getenv("APP_KEY", "1001300033")
# SECRET_KEY = os.getenv("SECRET_KEY", "24e74daf74124b0b96c9cb113162a976")
# URL = os.getenv("GATEWAY_URL", "https://10.10.65.213:18300/ai-inference-gateway/predict")
# COMPONENT_CODE = os.getenv("COMPONENT_CODE", "04100567")
# MODEL = os.getenv("MODEL", "Qwen3-235B-A22B-w8a8")

# 智学公司环境----Qwen3-235B-A22B-w8a8大模型
APP_KEY = os.getenv("APP_KEY", "1001300033")
SECRET_KEY = os.getenv("SECRET_KEY", "24e74daf74124b0b96c9cb113162a976")
URL = os.getenv("GATEWAY_URL", "https://192.168.0.213:18300/ai-inference-gateway/predict")
COMPONENT_CODE = os.getenv("COMPONENT_CODE", "04100567")
MODEL = os.getenv("MODEL", "Qwen3-235B-A22B-w8a8")


DEFAULT_CONCURRENCY_LEVELS = [1,2,4,8,12,16,20,22,24,26,28,30,32,34,36,38,40]
DEFAULT_QUESTIONS = [
    "新中国是什么时候成立的？",
    "项羽为什么会被刘邦打败？",
    "太阳为什么会发光？",
    "黑洞是什么？",
    "秦始皇统一六国有什么历史意义？",
    "为什么海水是咸的？",
    "人类为什么需要睡觉？",
    "唐朝为什么被认为是中国古代盛世之一？",
    "三国时期赤壁之战为什么重要？",
    "牛顿三大定律分别是什么？",
    "光合作用的原理是什么？",
    "为什么会有四季变化？",
    "拿破仑为什么最终失败？",
    "工业革命对世界产生了什么影响？",
    "《红楼梦》主要讲了什么？",
    "鲁迅为什么在中国现代文学史上很重要？",
    "人工智能和传统程序有什么区别？",
    "为什么飞机能飞起来？",
    "地震是怎么形成的？",
    "火山为什么会喷发？",
    "为什么月亮会有阴晴圆缺？",
    "长城最初修建的主要目的是什么？",
    "郑和下西洋有什么意义？",
    "第一次世界大战爆发的原因是什么？",
    "第二次世界大战为什么会爆发？",
    "相对论的核心思想是什么？",
    "量子力学为什么难以理解？",
    "DNA 的作用是什么？",
    "人类是如何进化来的？",
    "为什么恐龙会灭绝？"
]
DEFAULT_SYSTEM_PROMPT = "你是一个严谨的测试助手，请根据用户要求给出清晰、可验证的回答。"
DEFAULT_USER_PROMPT = " "


def build_default_messages() -> list[dict[str, str]]:
    question = random.choice(DEFAULT_QUESTIONS)
    return [
        {
            "role": "system",
            "content": "你是一本百科全书,熟知世界上所有问题。",
        },
        {"role": "user", "content": f"{question}/no_think"},
    ]


@dataclass
class RequestResult:
    concurrency: int
    request_id: int
    burst_id: int
    success: bool
    status_code: Optional[int]
    error: str
    start_epoch: float
    end_epoch: float
    send_perf: float
    total_ms: float
    header_ms: Optional[float] = None
    first_byte_ms: Optional[float] = None
    first_token_ms: Optional[float] = None
    response_bytes: int = 0
    stream_events: int = 0
    output_chars: int = 0
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


@dataclass
class StepResult:
    concurrency: int
    burst_rounds: int
    attempted_requests: int
    completed_requests: int
    success_count: int
    failed_count: int
    success_rate: float
    total_duration_s: float
    effective_duration_s: float
    success_qps: float
    total_qps: float
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
    avg_ttfb_ms: Optional[float]
    p95_ttfb_ms: Optional[float]
    avg_ttft_ms: Optional[float]
    p95_ttft_ms: Optional[float]
    total_completion_tokens: int
    token_usage_coverage: float
    output_token_throughput: Optional[float]
    error_summary: dict[str, int] = field(default_factory=dict)


def make_headers(app_key: str, secret_key: str) -> dict[str, str]:
    x_date = formatdate(timeval=time.time(), localtime=False, usegmt=True)
    sign_text = f"x-date: {x_date}"
    signature = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), sign_text.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "x-date": x_date,
        "authorization": (
            f'hmac username="{app_key}", algorithm="hmac-sha256", '
            f'headers="x-date", signature="{signature}"'
        ),
        "Content-Type": "application/json",
    }


def read_text_arg(value: str) -> str:
    if value.startswith("@"):
        return Path(value[1:]).read_text(encoding="utf-8")
    return value


def image_to_content_item(path: str) -> dict[str, Any]:
    image_path = Path(path)
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{data}"},
    }


def make_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "componentCode": args.component_code,
        "model": args.model,
        "messages": build_default_messages(),
        "stream": args.stream,
    }
    if args.max_tokens is not None:
        payload["max_tokens"] = args.max_tokens
    if args.temperature is not None:
        payload["temperature"] = args.temperature
    return payload


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def pick_text(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "".join(pick_text(item) for item in data)
    if not isinstance(data, dict):
        return ""

    choices = data.get("choices")
    if isinstance(choices, list):
        parts: list[str] = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            for key in ("delta", "message"):
                nested = choice.get(key)
                if isinstance(nested, dict):
                    content = nested.get("content")
                    if isinstance(content, str):
                        parts.append(content)
            if isinstance(choice.get("text"), str):
                parts.append(choice["text"])
        if parts:
            return "".join(parts)

    for key in ("content", "text", "answer", "result", "output", "response", "data"):
        text = pick_text(data.get(key))
        if text:
            return text
    return ""


def find_usage(data: Any) -> Optional[dict[str, Any]]:
    if isinstance(data, dict):
        usage = data.get("usage")
        if isinstance(usage, dict):
            return usage
        for value in data.values():
            nested = find_usage(value)
            if nested is not None:
                return nested
    elif isinstance(data, list):
        for item in data:
            nested = find_usage(item)
            if nested is not None:
                return nested
    return None


def to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_usage(data: Any) -> tuple[Optional[int], Optional[int], Optional[int]]:
    usage = find_usage(data) or {}
    prompt_tokens = first_int(usage, ("prompt_tokens", "input_tokens", "prompt_token_count"))
    completion_tokens = first_int(
        usage,
        ("completion_tokens", "output_tokens", "generated_tokens", "completion_token_count"),
    )
    total_tokens = first_int(usage, ("total_tokens", "total_token_count"))
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    if completion_tokens is None and prompt_tokens is not None and total_tokens is not None:
        completion_tokens = max(total_tokens - prompt_tokens, 0)
    return prompt_tokens, completion_tokens, total_tokens


def first_int(data: dict[str, Any], keys: tuple[str, ...]) -> Optional[int]:
    for key in keys:
        value = to_int(data.get(key))
        if value is not None:
            return value
    return None


def validate_body(data: Any) -> tuple[bool, str]:
    nested_error = find_business_error(data)
    if nested_error:
        return False, nested_error
    return True, ""


def find_business_error(data: Any) -> str:
    if not isinstance(data, dict):
        if isinstance(data, list):
            for item in data:
                nested = find_business_error(item)
                if nested:
                    return nested
        return ""

    if data.get("error"):
        return f"Business error: {str(data.get('error'))[:200]}"

    success = data.get("success")
    if isinstance(success, bool) and not success:
        return f"Business success=false: {str(data)[:200]}"

    for key in ("code", "status_code", "error_code"):
        if key in data:
            value = data.get(key)
            numeric = to_int(value)
            if numeric is not None and numeric not in (0, 200):
                return f"Business {key}: {value}"
            if numeric is None:
                text = str(value).strip().lower()
                if text and text not in {"0", "200", "ok", "success", "succeeded"}:
                    return f"Business {key}: {value}"

    status = str(data.get("status", "")).strip().lower()
    if status in {"error", "failed", "failure", "fail"}:
        return f"Business status: {data.get('status')}"

    for value in data.values():
        nested = find_business_error(value)
        if nested:
            return nested
    return ""


def percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    if pct < 0 or pct > 100:
        raise ValueError("percentile must be in [0, 100]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


def average(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def format_ms(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.2f}ms"


def format_number(value: Optional[float], suffix: str = "", digits: int = 2) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}{suffix}"


def is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    return "timed out" in str(exc).lower() or "timeout" in str(reason).lower()


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
        self.condition = threading.Condition()
        self.event = threading.Event()

    def ready_and_wait(self) -> None:
        with self.condition:
            self.ready += 1
            self.condition.notify_all()
        self.event.wait()

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


class GatewayConcurrentTester:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.payload = make_payload(args)
        self.ssl_context = None if args.verify_ssl else ssl._create_unverified_context()

    def send_request(
        self,
        request_id: int,
        concurrency: int,
        burst_id: int,
        start_gate: StartGate,
        inflight: InflightCounter,
    ) -> RequestResult:
        start_epoch = 0.0
        start_perf = 0.0
        entered_inflight = False
        status_code: Optional[int] = None
        header_ms: Optional[float] = None
        first_byte_ms: Optional[float] = None
        first_token_ms: Optional[float] = None
        response_bytes = 0
        stream_events = 0
        output_parts: list[str] = []
        usage_source: Any = None

        try:
            request = urllib.request.Request(
                self.args.url,
                data=json.dumps(self.payload, ensure_ascii=False).encode("utf-8"),
                headers=make_headers(self.args.app_key, self.args.secret_key),
                method="POST",
            )
            start_gate.ready_and_wait()

            inflight.enter()
            entered_inflight = True
            start_epoch = time.time()
            start_perf = time.perf_counter()

            try:
                response = urllib.request.urlopen(
                    request,
                    timeout=self.args.timeout,
                    context=self.ssl_context,
                )
            except urllib.error.HTTPError as exc:
                status_code = exc.code
                try:
                    error_body = exc.read(4096).decode("utf-8", errors="replace")
                    parsed_error = parse_json(error_body)
                    ok, business_error = validate_body(parsed_error)
                    error_message = business_error if not ok else f"HTTP {exc.code}: {error_body[:200]}"
                    return self._failure(
                        concurrency,
                        request_id,
                        start_epoch,
                        start_perf,
                        error_message,
                        status_code,
                        burst_id,
                    )
                finally:
                    exc.close()

            with response:
                status_code = response.getcode()
                header_ms = (time.perf_counter() - start_perf) * 1000
                if self.args.stream:
                    stream_result = self._read_stream(response, start_perf)
                    (
                        response_bytes,
                        stream_events,
                        first_byte_ms,
                        first_token_ms,
                        output_parts,
                        usage_source,
                    ) = stream_result
                else:
                    raw_body, response_bytes, first_byte_ms = self._read_body(response, start_perf)
                    text = raw_body.decode("utf-8", errors="replace")
                    usage_source = parse_json(text)
                    output_parts.append(pick_text(usage_source) or text)

            end_perf = time.perf_counter()
            end_epoch = time.time()
            data_for_validation = usage_source
            ok, error = validate_body(data_for_validation)
            if not ok:
                return self._failure(
                    concurrency,
                    request_id,
                    start_epoch,
                    start_perf,
                    error,
                    status_code,
                    burst_id,
                )

            prompt_tokens, completion_tokens, total_tokens = extract_usage(usage_source)
            output_text = "".join(output_parts).strip()
            success = 200 <= (status_code or 0) < 300
            error = "" if success else f"HTTP {status_code}"
            if success and not output_text:
                success = False
                error = "Empty output"
            if success and self.args.stream and stream_events <= 0:
                success = False
                error = "No stream events"

            return RequestResult(
                concurrency=concurrency,
                request_id=request_id,
                burst_id=burst_id,
                success=success,
                status_code=status_code,
                error=error,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                send_perf=start_perf,
                total_ms=(end_perf - start_perf) * 1000,
                header_ms=header_ms,
                first_byte_ms=first_byte_ms,
                first_token_ms=first_token_ms,
                response_bytes=response_bytes,
                stream_events=stream_events,
                output_chars=len(output_text),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        except (TimeoutError, socket.timeout) as exc:
            return self._failure(concurrency, request_id, start_epoch, start_perf, f"Timeout: {exc}", status_code, burst_id)
        except urllib.error.URLError as exc:
            if is_timeout_error(exc):
                return self._failure(concurrency, request_id, start_epoch, start_perf, f"Timeout: {exc}", status_code, burst_id)
            return self._failure(concurrency, request_id, start_epoch, start_perf, f"Connection error: {exc}", status_code, burst_id)
        except Exception as exc:
            return self._failure(concurrency, request_id, start_epoch, start_perf, f"Exception: {exc}", status_code, burst_id)
        finally:
            if entered_inflight:
                inflight.leave()

    def _read_body(self, response: Any, start_perf: float) -> tuple[bytes, int, Optional[float]]:
        chunks: list[bytes] = []
        response_bytes = 0
        first_byte_ms = None
        first = response.read(1)
        if first:
            first_byte_ms = (time.perf_counter() - start_perf) * 1000
            response_bytes += len(first)
            chunks.append(first)
        while True:
            chunk = response.read(8192)
            if not chunk:
                break
            if first_byte_ms is None:
                first_byte_ms = (time.perf_counter() - start_perf) * 1000
            response_bytes += len(chunk)
            chunks.append(chunk)
        return b"".join(chunks), response_bytes, first_byte_ms

    def _read_stream(
        self,
        response: Any,
        start_perf: float,
    ) -> tuple[int, int, Optional[float], Optional[float], list[str], Any]:
        response_bytes = 0
        stream_events = 0
        first_byte_ms = None
        first_token_ms = None
        output_parts: list[str] = []
        last_json: Any = None
        usage_source: Any = None

        first = response.read(1)
        if first:
            first_byte_ms = (time.perf_counter() - start_perf) * 1000
            first_line = first + response.readline()
        else:
            first_line = b""

        while True:
            raw_line = first_line if first_line else response.readline()
            first_line = b""
            if not raw_line:
                break
            response_bytes += len(raw_line)
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                break

            stream_events += 1
            event = parse_json(line)
            ok, error = validate_body(event)
            if not ok:
                raise RuntimeError(error)
            last_json = event
            if usage_source is None and find_usage(event) is not None:
                usage_source = event
            text = pick_text(event)
            if text:
                output_parts.append(text)
                if first_token_ms is None:
                    first_token_ms = (time.perf_counter() - start_perf) * 1000

        return response_bytes, stream_events, first_byte_ms, first_token_ms, output_parts, usage_source or last_json

    @staticmethod
    def _failure(
        concurrency: int,
        request_id: int,
        start_epoch: float,
        start_perf: float,
        error: str,
        status_code: Optional[int],
        burst_id: int,
    ) -> RequestResult:
        end_perf = time.perf_counter()
        started = start_perf > 0
        return RequestResult(
            concurrency=concurrency,
            request_id=request_id,
            burst_id=burst_id,
            success=False,
            status_code=status_code,
            error=error[:300],
            start_epoch=start_epoch if started else time.time(),
            end_epoch=time.time(),
            send_perf=start_perf if started else 0.0,
            total_ms=(end_perf - start_perf) * 1000 if started else 0.0,
        )

    def run_step(self, concurrency: int, total_requests: int) -> tuple[StepResult, list[RequestResult]]:
        results: list[RequestResult] = []
        completed = 0
        progress_every = max(1, total_requests // 10)
        burst_count = (total_requests + concurrency - 1) // concurrency
        peak_tracker = PeakTracker()

        print(f"\n{'=' * 72}")
        print(
            f"开始测试并发 {concurrency}: 请求数={total_requests}, "
            f"同步批次={burst_count}, stream={self.args.stream}"
        )
        print(f"{'=' * 72}")
        start = time.perf_counter()
        next_request_id = 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            for burst_id in range(1, burst_count + 1):
                burst_size = min(concurrency, total_requests - completed)
                start_gate = StartGate(burst_size)
                inflight = InflightCounter()
                futures = [
                    executor.submit(
                        self.send_request,
                        request_id,
                        concurrency,
                        burst_id,
                        start_gate,
                        inflight,
                    )
                    for request_id in range(next_request_id, next_request_id + burst_size)
                ]
                next_request_id += burst_size

                if not start_gate.wait_until_ready(timeout=self.args.start_timeout):
                    start_gate.release()
                    raise RuntimeError(
                        f"并发 {concurrency} 第 {burst_id} 批启动超时: "
                        f"仅 {start_gate.ready}/{burst_size} 个 worker 就绪"
                    )

                burst_start = time.perf_counter()
                print(f"第 {burst_id}/{burst_count} 批释放: {burst_size} 个请求同时发起")
                start_gate.release()

                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    results.append(result)
                    completed += 1
                    if completed % progress_every == 0 or completed == total_requests:
                        ok_count = sum(1 for item in results if item.success)
                        print(f"进度: {completed}/{total_requests}, 当前成功率={ok_count / completed * 100:.2f}%")

                peak_tracker.observe(inflight.peak)
                if burst_id != burst_count and self.args.burst_interval > 0:
                    elapsed = time.perf_counter() - burst_start
                    sleep_seconds = max(self.args.burst_interval - elapsed, 0.0)
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)

        total_duration = time.perf_counter() - start
        step = summarize_step(concurrency, total_requests, total_duration, peak_tracker.peak, burst_count, results)
        print_step_report(step)
        return step, sorted(results, key=lambda item: item.request_id)


def summarize_step(
    concurrency: int,
    total_requests: int,
    total_duration: float,
    observed_peak: int,
    burst_rounds: int,
    results: list[RequestResult],
) -> StepResult:
    success = [item for item in results if item.success]
    failed = [item for item in results if not item.success]
    sent_results = [item for item in results if item.send_perf > 0]
    if sent_results:
        effective_start = min(item.send_perf for item in sent_results)
        effective_end = max(item.send_perf + item.total_ms / 1000 for item in sent_results)
        effective_duration = max(effective_end - effective_start, 0.0)
    else:
        effective_duration = 0.0
    response_times = [item.total_ms for item in success]
    ttfb_times = [item.first_byte_ms for item in success if item.first_byte_ms is not None]
    ttft_times = [item.first_token_ms for item in success if item.first_token_ms is not None]
    completion_tokens = [
        item.completion_tokens for item in success if item.completion_tokens is not None
    ]
    total_completion_tokens = sum(completion_tokens)
    token_coverage = (len(completion_tokens) / len(success) * 100) if success else 0.0
    error_summary = Counter(normalize_error(item.error) for item in failed)
    burst_sizes = Counter(item.burst_id for item in results)

    return StepResult(
        concurrency=concurrency,
        burst_rounds=burst_rounds,
        attempted_requests=total_requests,
        completed_requests=len(results),
        success_count=len(success),
        failed_count=len(failed),
        success_rate=(len(success) / total_requests * 100) if total_requests else 0.0,
        total_duration_s=total_duration,
        effective_duration_s=effective_duration,
        success_qps=(len(success) / effective_duration) if effective_duration > 0 else 0.0,
        total_qps=(len(sent_results) / effective_duration) if effective_duration > 0 else 0.0,
        configured_concurrency=concurrency,
        observed_peak_inflight=observed_peak,
        full_concurrency_bursts=sum(
            1 for burst_id in range(1, burst_rounds + 1)
            if burst_sizes[burst_id] >= concurrency
        ),
        avg_response_ms=average(response_times),
        p50_response_ms=percentile(response_times, 50),
        p90_response_ms=percentile(response_times, 90),
        p95_response_ms=percentile(response_times, 95),
        p99_response_ms=percentile(response_times, 99),
        min_response_ms=min(response_times) if response_times else None,
        max_response_ms=max(response_times) if response_times else None,
        avg_ttfb_ms=average(ttfb_times),
        p95_ttfb_ms=percentile(ttfb_times, 95),
        avg_ttft_ms=average(ttft_times),
        p95_ttft_ms=percentile(ttft_times, 95),
        total_completion_tokens=total_completion_tokens,
        token_usage_coverage=token_coverage,
        output_token_throughput=(
            total_completion_tokens / effective_duration
            if effective_duration > 0 and completion_tokens
            else None
        ),
        error_summary=dict(error_summary),
    )


def normalize_error(error: str) -> str:
    if not error:
        return "Unknown"
    if ":" in error:
        return error.split(":", 1)[0]
    return error[:80]


def print_step_report(step: StepResult) -> None:
    print(f"\n并发 {step.concurrency} 测试结果")
    print(f"计划请求数: {step.attempted_requests}")
    print(f"完成请求数: {step.completed_requests}")
    print(f"成功/失败: {step.success_count}/{step.failed_count} ({step.success_rate:.2f}%)")
    print(
        f"同步批次: {step.burst_rounds}, "
        f"满并发批次: {step.full_concurrency_bursts}/{step.burst_rounds}"
    )
    print(f"目标并发/实际峰值并发: {step.configured_concurrency}/{step.observed_peak_inflight}")
    print(f"总耗时: {step.total_duration_s:.2f}s")
    print(f"有效压测耗时: {step.effective_duration_s:.2f}s")
    print(f"总QPS/成功QPS: {step.total_qps:.2f}/{step.success_qps:.2f} (基于有效压测耗时)")
    print(
        "响应耗时: "
        f"avg={format_ms(step.avg_response_ms)}, "
        f"p50={format_ms(step.p50_response_ms)}, "
        f"p95={format_ms(step.p95_response_ms)}, "
        f"p99={format_ms(step.p99_response_ms)}, "
        f"max={format_ms(step.max_response_ms)}"
    )
    print(f"TTFB: avg={format_ms(step.avg_ttfb_ms)}, p95={format_ms(step.p95_ttfb_ms)}")
    print(f"TTFT(stream文本首包): avg={format_ms(step.avg_ttft_ms)}, p95={format_ms(step.p95_ttft_ms)}")
    print(
        "输出 token 吞吐: "
        f"{format_number(step.output_token_throughput, ' tok/s')} "
        f"(基于有效压测耗时, usage覆盖率={step.token_usage_coverage:.2f}%)"
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
        and previous.p95_response_ms
        and current.p95_response_ms
        and current.p95_response_ms > previous.p95_response_ms * latency_growth_threshold
    ):
        return (
            True,
            f"P95 响应耗时从 {previous.p95_response_ms:.2f}ms 增长到 "
            f"{current.p95_response_ms:.2f}ms，超过 {latency_growth_threshold:.2f} 倍",
        )
    return False, ""


def build_final_report(
    args: argparse.Namespace,
    steps: list[StepResult],
    breaking: Optional[tuple[int, str]],
    report_files: dict[str, Path],
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if args.concurrent:
        level_text = f"指定并发: {args.concurrent}"
        effective_break_confirmations = 1
    else:
        level_text = ", ".join(str(step.concurrency) for step in steps)
        effective_break_confirmations = 1 if len(steps) == 1 else args.break_confirmations
    healthy_steps = [
        step for step in steps if step.success_rate >= args.success_threshold
    ]
    best_healthy_qps = max(healthy_steps, key=lambda item: item.success_qps) if healthy_steps else None
    best_observed_qps = max(steps, key=lambda item: item.success_qps) if steps else None
    last_healthy = healthy_steps[-1] if healthy_steps else None

    if breaking:
        limit_text = (
            f"首次拐点/极限风险出现在并发 {breaking[0]}：{breaking[1]}。"
            f"建议把稳定并发上限暂定为 {last_healthy.concurrency if last_healthy else 'N/A'}。"
        )
    else:
        limit_text = (
            f"本次范围内未触发拐点，稳定并发上限至少达到 {steps[-1].concurrency if steps else 'N/A'}。"
        )

    lines = [
        "# Qwen3-VL-32B-Instruct 阶梯并发测试报告",
        "",
        f"- 生成时间: {now}",
        f"- URL: {args.url}",
        f"- 模型: {args.model}",
        f"- componentCode: {args.component_code}",
        f"- stream: {args.stream}",
        f"- 请求计划: {format_request_plan(args)}",
        f"- 并发级别: {level_text}",
        f"- 成功率阈值: {args.success_threshold:.2f}%",
        f"- P95增长拐点阈值: {args.latency_growth_threshold:.2f}倍",
        "",
        "## 结论",
        "",
        f"- {limit_text}",
    ]
    if best_healthy_qps:
        lines.append(
            f"- 最佳达标成功QPS: 并发 {best_healthy_qps.concurrency}，"
            f"成功QPS={best_healthy_qps.success_qps:.2f}，"
            f"成功率={best_healthy_qps.success_rate:.2f}%，P95={format_ms(best_healthy_qps.p95_response_ms)}。"
        )
    elif best_observed_qps:
        lines.append(
            f"- 本次没有成功率达到阈值的阶梯；仅供观察的最高成功QPS出现在并发 "
            f"{best_observed_qps.concurrency}，成功QPS={best_observed_qps.success_qps:.2f}，"
            f"成功率={best_observed_qps.success_rate:.2f}%。"
        )
    lines.extend(
        [
            "- 口径说明: 每个同步批次会先创建好请求对象并等待全部 worker 就绪，再统一释放发起网络请求；"
            "响应耗时从释放后开始统计，包含网络传输、服务端处理和响应读取。",
            "- 总耗时包含同一阶梯内各 burst 之间的等待间隔；有效压测耗时从第一个请求实际发出到最后一个已发出请求完成，"
            "QPS 和 token 吞吐均基于有效压测耗时计算。",
            "- TTFB 为响应体首字节耗时；TTFT 仅在 stream=True 且首次出现有效文本增量时统计；"
            "token 吞吐只基于接口返回 usage 的请求统计，并用 usage 覆盖率说明可信度。",
            f"- 拐点确认: 本次按连续 {effective_break_confirmations} 个阶梯触发风险条件确认拐点；"
            "若成功率低于提前停止阈值，则直接确认当前风险点。",
            "",
            "## 阶梯结果",
            "",
            "| 并发 | 同步批次 | 满并发批次 | 实际峰值 | 请求数 | 成功率 | 总耗时 | 有效压测耗时 | 总QPS | 成功QPS | P95响应 | P99响应 | P95 TTFB | P95 TTFT | token吞吐 | usage覆盖率 |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for step in steps:
        lines.append(
            f"| {step.concurrency} | {step.burst_rounds} | "
            f"{step.full_concurrency_bursts} | {step.observed_peak_inflight} | {step.attempted_requests} | "
            f"{step.success_rate:.2f}% | {step.total_duration_s:.2f}s | {step.effective_duration_s:.2f}s | "
            f"{step.total_qps:.2f} | {step.success_qps:.2f} | {format_ms(step.p95_response_ms)} | "
            f"{format_ms(step.p99_response_ms)} | {format_ms(step.p95_ttfb_ms)} | "
            f"{format_ms(step.p95_ttft_ms)} | {format_number(step.output_token_throughput, ' tok/s')} | "
            f"{step.token_usage_coverage:.2f}% |"
        )

    lines.extend(["", "## 输出文件", ""])
    for name, path in report_files.items():
        lines.append(f"- {name}: {path}")
    return "\n".join(lines)


def format_request_plan(args: argparse.Namespace) -> str:
    if args.total is None:
        return f"每档按 并发数 x {args.rounds} 轮 自动计算"
    return "每档至少 {0} 个请求；不足满批次时自动补齐到当前并发整数倍".format(args.total)


def write_reports(
    args: argparse.Namespace,
    steps: list[StepResult],
    details: list[RequestResult],
    breaking: Optional[tuple[int, str]],
) -> dict[str, Path]:
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"qwen3_vl_concurrent_{timestamp}"
    summary_csv = report_dir / f"{prefix}_summary.csv"
    detail_csv = report_dir / f"{prefix}_details.csv"
    markdown = report_dir / f"{prefix}_report.md"

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

    files = {"Markdown报告": markdown, "汇总CSV": summary_csv, "明细CSV": detail_csv}
    report_text = build_final_report(args, steps, breaking, files)
    markdown.write_text(report_text + "\n", encoding="utf-8")
    return files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Qwen3-VL-32B-Instruct 阶梯并发压测脚本（仅使用 Python 标准库）")
    parser.add_argument("--app-key", default=APP_KEY)
    parser.add_argument("--secret-key", default=SECRET_KEY)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--component-code", default=COMPONENT_CODE)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT, help="系统提示词；以 @file.txt 形式读取文件")
    parser.add_argument("--user-prompt", default=DEFAULT_USER_PROMPT, help="用户提示词；以 @file.txt 形式读取文件")
    parser.add_argument("--image", action="append", help="可选图片路径，可重复传入；默认纯文本请求")
    parser.add_argument("--stream", dest="stream", action="store_true", default=False)
    parser.add_argument("--no-stream", dest="stream", action="store_false")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--verify-ssl", action="store_true", help="默认不校验证书；传入该参数后启用证书校验")
    parser.add_argument("--concurrent", type=int, default=None, help="只测试一个指定并发；不传则执行阶梯并发")
    parser.add_argument("--total", type=int, default=None, help="每个并发级别的最少请求总数；会自动补齐为当前并发的整数倍")
    parser.add_argument("--rounds", type=int, default=5, help="未指定 --total 时，每个并发级别执行多少轮同步 burst")
    parser.add_argument("--burst-interval", type=float, default=0.0, help="同一阶梯内两轮同步 burst 的最小间隔秒数")
    parser.add_argument("--start-timeout", type=float, default=30.0, help="等待一轮内所有 worker 就绪的超时时间")
    parser.add_argument("--start-concurrent", type=int, default=1)
    parser.add_argument("--max-concurrent", type=int, default=40) #最大并发数
    parser.add_argument("--step", type=int, default=1)
    parser.add_argument("--success-threshold", type=float, default=95.0, help="判定稳定并发的成功率阈值")
    parser.add_argument("--latency-growth-threshold", type=float, default=2.0, help="相邻阶梯 P95 增长倍数达到该值视为拐点")
    parser.add_argument("--stop-success-rate", type=float, default=50.0, help="成功率低于该值时提前停止")
    parser.add_argument("--break-confirmations", type=int, default=2, help="连续触发多少个阶梯后确认拐点；单阶测试自动按 1 处理")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--print-payload", action="store_true")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.concurrent is not None and args.concurrent <= 0:
        raise ValueError("--concurrent 必须大于 0")
    if args.total is not None and args.total <= 0:
        raise ValueError("--total 必须大于 0")
    if args.rounds <= 0:
        raise ValueError("--rounds 必须大于 0")
    if args.burst_interval < 0:
        raise ValueError("--burst-interval 不能小于 0")
    if args.start_timeout <= 0:
        raise ValueError("--start-timeout 必须大于 0")
    if args.start_concurrent <= 0 or args.max_concurrent <= 0 or args.step <= 0:
        raise ValueError("--start-concurrent、--max-concurrent、--step 必须大于 0")
    if args.start_concurrent > args.max_concurrent:
        raise ValueError("--start-concurrent 不能大于 --max-concurrent")
    if args.timeout <= 0:
        raise ValueError("--timeout 必须大于 0")
    if args.max_tokens is not None and args.max_tokens <= 0:
        raise ValueError("--max-tokens 必须大于 0")
    if not 0 <= args.success_threshold <= 100:
        raise ValueError("--success-threshold 必须在 0 到 100 之间")
    if not 0 <= args.stop_success_rate <= 100:
        raise ValueError("--stop-success-rate 必须在 0 到 100 之间")
    if args.latency_growth_threshold <= 1:
        raise ValueError("--latency-growth-threshold 必须大于 1")
    if args.break_confirmations <= 0:
        raise ValueError("--break-confirmations 必须大于 0")
    for value in args.image or []:
        if not Path(value).is_file():
            raise ValueError(f"--image 指定的文件不存在: {value}")
    for option_name in ("system_prompt", "user_prompt"):
        value = getattr(args, option_name)
        if isinstance(value, str) and value.startswith("@") and not Path(value[1:]).is_file():
            raise ValueError(f"--{option_name.replace('_', '-')} 指定的文件不存在: {value[1:]}")


def has_custom_concurrency_range(argv: list[str]) -> bool:
    range_options = ("--start-concurrent", "--max-concurrent", "--step")
    return any(
        arg == option or arg.startswith(f"{option}=")
        for arg in argv[1:]
        for option in range_options
    )


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

    tester = GatewayConcurrentTester(args)
    if args.print_payload:
        print("请求 payload:")
        print(json.dumps(tester.payload, ensure_ascii=False, indent=2))

    if args.concurrent:
        levels = [args.concurrent]
    elif has_custom_concurrency_range(sys.argv):
        levels = list(range(args.start_concurrent, args.max_concurrent + 1, args.step))
    else:
        levels = DEFAULT_CONCURRENCY_LEVELS

    print("\nQwen3-VL-32B-Instruct 阶梯并发测试")
    print(f"目标URL: {args.url}")
    print(f"并发级别: {levels}")
    print(f"请求计划: {format_request_plan(args)}")
    print(f"SSL证书校验: {args.verify_ssl}")

    all_steps: list[StepResult] = []
    all_details: list[RequestResult] = []
    previous: Optional[StepResult] = None
    breaking: Optional[tuple[int, str]] = None
    break_streak = 0
    first_break_candidate: Optional[tuple[int, str]] = None
    required_break_confirmations = 1 if len(levels) == 1 else args.break_confirmations

    for level in levels:
        total_requests = resolve_total_requests(args, level)
        if args.total is not None and total_requests != args.total:
            print(
                f"\n提示: 并发 {level} 的 --total={args.total} 不是并发整数倍，"
                f"已补齐为 {total_requests}，确保每轮都是满并发同步发起。"
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
            print(
                f"\n拐点风险候选: 并发 {level}, 原因: {reason} "
                f"({break_streak}/{required_break_confirmations})"
            )
            if break_streak >= required_break_confirmations and breaking is None:
                breaking = first_break_candidate
                print(f"确认拐点: 并发 {breaking[0]}, 原因: {breaking[1]}")
        else:
            break_streak = 0
            first_break_candidate = None

        previous = step
        if step.success_rate < args.stop_success_rate:
            if breaking is None and first_break_candidate is not None:
                breaking = first_break_candidate
                print(f"因成功率低于提前停止阈值，确认拐点: 并发 {breaking[0]}, 原因: {breaking[1]}")
            print(f"\n成功率 {step.success_rate:.2f}% 低于提前停止阈值 {args.stop_success_rate:.2f}%，停止后续阶梯。")
            break

        if level != levels[-1] and args.concurrent is None:
            time.sleep(2)

    files = write_reports(args, all_steps, all_details, breaking)
    report_text = build_final_report(args, all_steps, breaking, files)
    print("\n" + "=" * 72)
    print(report_text)
    print("=" * 72)
    return 0 if all_steps and all(step.success_count > 0 for step in all_steps) else 1


if __name__ == "__main__":
    sys.exit(main())
