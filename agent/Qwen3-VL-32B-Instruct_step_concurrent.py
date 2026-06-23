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


APP_KEY = os.getenv("APP_KEY", "1001300033")
SECRET_KEY = os.getenv("SECRET_KEY", "24e74daf74124b0b96c9cb113162a976")
URL = os.getenv("GATEWAY_URL", "https://10.10.65.213:18300/ai-inference-gateway/predict")
COMPONENT_CODE = os.getenv("COMPONENT_CODE", "04100565")
MODEL = os.getenv("MODEL", "Qwen3-VL-32B-Instruct")

DEFAULT_SYSTEM_PROMPT = "你是一个严谨的测试助手，请根据用户要求给出清晰、可验证的回答。"
DEFAULT_USER_PROMPT = (
    "请用 5 条要点说明电网设备巡检中如何发现、定位并闭环处理隐患。"
    "要求回答结构清晰，内容具体。"
)


@dataclass
class RequestResult:
    concurrency: int
    request_id: int
    success: bool
    status_code: Optional[int]
    error: str
    start_epoch: float
    end_epoch: float
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
    attempted_requests: int
    completed_requests: int
    success_count: int
    failed_count: int
    success_rate: float
    total_duration_s: float
    success_qps: float
    total_qps: float
    configured_concurrency: int
    observed_peak_inflight: int
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
    user_prompt = read_text_arg(args.user_prompt)
    system_prompt = read_text_arg(args.system_prompt)
    user_content: Any = user_prompt

    if args.image:
        user_content = [{"type": "text", "text": user_prompt}]
        user_content.extend(image_to_content_item(path) for path in args.image)

    payload: dict[str, Any] = {
        "componentCode": args.component_code,
        "model": args.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
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

    def wait_until_ready(self, timeout: float = 30.0) -> None:
        deadline = time.perf_counter() + timeout
        with self.condition:
            while self.ready < self.target_ready:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)

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
        start_gate: StartGate,
        inflight: InflightCounter,
    ) -> RequestResult:
        start_gate.ready_and_wait()

        inflight.enter()
        start_epoch = time.time()
        start_perf = time.perf_counter()
        status_code: Optional[int] = None
        header_ms: Optional[float] = None
        first_byte_ms: Optional[float] = None
        first_token_ms: Optional[float] = None
        response_bytes = 0
        stream_events = 0
        output_parts: list[str] = []
        usage_source: Any = None

        try:
            body = json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                self.args.url,
                data=body,
                headers=make_headers(self.args.app_key, self.args.secret_key),
                method="POST",
            )

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
                )

            prompt_tokens, completion_tokens, total_tokens = extract_usage(usage_source)
            return RequestResult(
                concurrency=concurrency,
                request_id=request_id,
                success=200 <= (status_code or 0) < 300,
                status_code=status_code,
                error="" if 200 <= (status_code or 0) < 300 else f"HTTP {status_code}",
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                total_ms=(end_perf - start_perf) * 1000,
                header_ms=header_ms,
                first_byte_ms=first_byte_ms,
                first_token_ms=first_token_ms,
                response_bytes=response_bytes,
                stream_events=stream_events,
                output_chars=len("".join(output_parts)),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        except (TimeoutError, socket.timeout) as exc:
            return self._failure(concurrency, request_id, start_epoch, start_perf, f"Timeout: {exc}", status_code)
        except urllib.error.URLError as exc:
            if is_timeout_error(exc):
                return self._failure(concurrency, request_id, start_epoch, start_perf, f"Timeout: {exc}", status_code)
            return self._failure(concurrency, request_id, start_epoch, start_perf, f"Connection error: {exc}", status_code)
        except Exception as exc:
            return self._failure(concurrency, request_id, start_epoch, start_perf, f"Exception: {exc}", status_code)
        finally:
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
    ) -> RequestResult:
        end_perf = time.perf_counter()
        return RequestResult(
            concurrency=concurrency,
            request_id=request_id,
            success=False,
            status_code=status_code,
            error=error[:300],
            start_epoch=start_epoch,
            end_epoch=time.time(),
            total_ms=(end_perf - start_perf) * 1000,
        )

    def run_step(self, concurrency: int, total_requests: int) -> tuple[StepResult, list[RequestResult]]:
        actual_first_wave = min(concurrency, total_requests)
        start_gate = StartGate(actual_first_wave)
        inflight = InflightCounter()
        results: list[RequestResult] = []
        completed = 0
        progress_every = max(1, total_requests // 10)

        print(f"\n{'=' * 72}")
        print(f"开始测试并发 {concurrency}: 请求数={total_requests}, stream={self.args.stream}")
        print(f"{'=' * 72}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(self.send_request, request_id, concurrency, start_gate, inflight)
                for request_id in range(1, total_requests + 1)
            ]
            start_gate.wait_until_ready()
            start = time.perf_counter()
            start_gate.release()
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                results.append(result)
                completed += 1
                if completed % progress_every == 0 or completed == total_requests:
                    ok_count = sum(1 for item in results if item.success)
                    print(f"进度: {completed}/{total_requests}, 当前成功率={ok_count / completed * 100:.2f}%")

        duration = time.perf_counter() - start
        step = summarize_step(concurrency, total_requests, duration, inflight.peak, results)
        print_step_report(step)
        return step, sorted(results, key=lambda item: item.request_id)


def summarize_step(
    concurrency: int,
    total_requests: int,
    duration: float,
    observed_peak: int,
    results: list[RequestResult],
) -> StepResult:
    success = [item for item in results if item.success]
    failed = [item for item in results if not item.success]
    response_times = [item.total_ms for item in success]
    ttfb_times = [item.first_byte_ms for item in success if item.first_byte_ms is not None]
    ttft_times = [item.first_token_ms for item in success if item.first_token_ms is not None]
    completion_tokens = [
        item.completion_tokens for item in success if item.completion_tokens is not None
    ]
    total_completion_tokens = sum(completion_tokens)
    token_coverage = (len(completion_tokens) / len(success) * 100) if success else 0.0
    error_summary = Counter(normalize_error(item.error) for item in failed)

    return StepResult(
        concurrency=concurrency,
        attempted_requests=total_requests,
        completed_requests=len(results),
        success_count=len(success),
        failed_count=len(failed),
        success_rate=(len(success) / total_requests * 100) if total_requests else 0.0,
        total_duration_s=duration,
        success_qps=(len(success) / duration) if duration > 0 else 0.0,
        total_qps=(len(results) / duration) if duration > 0 else 0.0,
        configured_concurrency=concurrency,
        observed_peak_inflight=observed_peak,
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
        output_token_throughput=(total_completion_tokens / duration) if duration > 0 and completion_tokens else None,
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
    print(f"目标并发/实际峰值并发: {step.configured_concurrency}/{step.observed_peak_inflight}")
    print(f"总耗时: {step.total_duration_s:.2f}s")
    print(f"总QPS/成功QPS: {step.total_qps:.2f}/{step.success_qps:.2f}")
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
        f"(usage覆盖率={step.token_usage_coverage:.2f}%)"
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
        level_text = f"{args.start_concurrent} -> {args.max_concurrent}, step={args.step}"
        effective_break_confirmations = 1 if args.start_concurrent == args.max_concurrent else args.break_confirmations
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
        f"- 每阶请求数: {args.total}",
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
            "- 口径说明: 响应耗时为客户端端到端耗时，包含每次请求的 JSON 序列化、HMAC 头生成、"
            "Request 对象创建、网络传输和响应读取；不包含脚本启动阶段的 payload 准备和图片 Base64 编码。",
            "- TTFB 为响应体首字节耗时；TTFT 仅在 stream=True 且首次出现有效文本增量时统计；"
            "token 吞吐只基于接口返回 usage 的请求统计，并用 usage 覆盖率说明可信度。",
            f"- 拐点确认: 本次按连续 {effective_break_confirmations} 个阶梯触发风险条件确认拐点；"
            "若成功率低于提前停止阈值，则直接确认当前风险点。",
            "",
            "## 阶梯结果",
            "",
            "| 并发 | 实际峰值 | 请求数 | 成功率 | 成功QPS | P95响应 | P99响应 | P95 TTFB | P95 TTFT | token吞吐 | usage覆盖率 |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for step in steps:
        lines.append(
            f"| {step.concurrency} | {step.observed_peak_inflight} | {step.attempted_requests} | "
            f"{step.success_rate:.2f}% | {step.success_qps:.2f} | {format_ms(step.p95_response_ms)} | "
            f"{format_ms(step.p99_response_ms)} | {format_ms(step.p95_ttfb_ms)} | "
            f"{format_ms(step.p95_ttft_ms)} | {format_number(step.output_token_throughput, ' tok/s')} | "
            f"{step.token_usage_coverage:.2f}% |"
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
    parser.add_argument("--stream", dest="stream", action="store_true", default=True)
    parser.add_argument("--no-stream", dest="stream", action="store_false")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--verify-ssl", action="store_true", help="默认不校验证书；传入该参数后启用证书校验")
    parser.add_argument("--concurrent", type=int, default=None, help="只测试一个指定并发；不传则执行阶梯并发")
    parser.add_argument("--total", type=int, default=20, help="每个并发级别的请求总数，建议不小于最大并发")
    parser.add_argument("--start-concurrent", type=int, default=1)
    parser.add_argument("--max-concurrent", type=int, default=20)
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
    if args.total <= 0:
        raise ValueError("--total 必须大于 0")
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
    else:
        levels = list(range(args.start_concurrent, args.max_concurrent + 1, args.step))

    print("\nQwen3-VL-32B-Instruct 阶梯并发测试")
    print(f"目标URL: {args.url}")
    print(f"并发级别: {levels}")
    print(f"每阶请求数: {args.total}")
    print(f"SSL证书校验: {args.verify_ssl}")
    if args.total < max(levels):
        print("提示: --total 小于最大并发，最高实际峰值并发会受请求总数限制。")

    all_steps: list[StepResult] = []
    all_details: list[RequestResult] = []
    previous: Optional[StepResult] = None
    breaking: Optional[tuple[int, str]] = None
    break_streak = 0
    first_break_candidate: Optional[tuple[int, str]] = None
    required_break_confirmations = 1 if len(levels) == 1 else args.break_confirmations

    for level in levels:
        step, details = tester.run_step(level, args.total)
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
