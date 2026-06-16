# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
高并发压力测试脚本 - 长上下文极限测试

注意：--context-length 生成的是测试文本字符数，不是模型 token 数。
真实 prompt/output token 数只以接口返回的 usage 字段为准。
"""

import argparse
import base64
import concurrent.futures
import csv
import hashlib
import hmac
import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests
from tqdm import tqdm


class AIGatewayStressTester:
    def __init__(
            self,
            url,
            app_key,
            secret_key,
            concurrent,
            total_requests,
            context_length=32000,
            output_file="stress_test_results.csv",
            stream=False,
            max_tokens=100,
    ):
        self.url = url
        self.app_key = app_key
        self.secret_key = secret_key
        self.concurrent = concurrent
        self.total_requests = total_requests
        self.context_length = context_length
        self.output_file = output_file
        self.stream = stream
        self.max_tokens = max_tokens

        self.results = self._empty_results()
        self.lock = threading.Lock()
        self.test_context = self._generate_long_context(context_length)

    @staticmethod
    def _empty_results() -> Dict[str, List[Dict[str, Any]]]:
        return {
            "success": [],
            "failed": [],
            "timing": [],
        }

    def _generate_long_context(self, length: int) -> str:
        """生成指定字符数的测试上下文。"""
        base_text = "这是一个用于测试的长文本片段。我们需要模拟长上下文的环境。"
        base_text += "在压力测试中，长上下文会对系统造成更大的负载。"
        base_text += "特别是当并发请求增加时，内存和计算资源的消耗会显著增加。"
        base_text += "让我们重复这个文本片段来构建足够长的上下文。"

        repeats = length // len(base_text) + 1
        return (base_text * repeats)[:length]

    def generate_auth_headers(self) -> Dict[str, str]:
        """生成HMAC认证头。"""
        curl_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        date_str = f"x-date: {curl_date}"

        h = hmac.new(
            self.secret_key.encode("utf-8"),
            date_str.encode("utf-8"),
            hashlib.sha256,
        )
        date_base = base64.b64encode(h.digest()).decode("utf-8")

        authorization = (
            f'hmac username="{self.app_key}", algorithm="hmac-sha256", '
            f'headers="x-date", signature="{date_base}"'
        )

        return {
            "x-date": curl_date,
            "authorization": authorization,
            "Content-Type": "application/json",
        }

    def _build_payload(self) -> Dict[str, Any]:
        return {
            "componentCode": "04100567",
            "model": "Qwen3-235B-A22B-w8a8",
            "messages": [
                {"role": "system", "content": "你是一个优秀的历史学家"},
                {"role": "user", "content": self.test_context + "\n请总结以上内容。"},
            ],
            "stream": self.stream,
            "max_tokens": self.max_tokens,
        }

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _find_usage(self, body: Any) -> Optional[Dict[str, Any]]:
        if isinstance(body, dict):
            usage = body.get("usage")
            if isinstance(usage, dict):
                return usage

            for value in body.values():
                nested = self._find_usage(value)
                if nested:
                    return nested

        if isinstance(body, list):
            for item in body:
                nested = self._find_usage(item)
                if nested:
                    return nested

        return None

    def _extract_token_usage(self, body: Any) -> Dict[str, Optional[int]]:
        usage = self._find_usage(body) or {}

        prompt_tokens = None
        for key in ("prompt_tokens", "input_tokens", "prompt_token_count"):
            prompt_tokens = self._to_int(usage.get(key))
            if prompt_tokens is not None:
                break

        completion_tokens = None
        for key in (
                "completion_tokens",
                "output_tokens",
                "generated_tokens",
                "completion_token_count",
        ):
            completion_tokens = self._to_int(usage.get(key))
            if completion_tokens is not None:
                break

        total_tokens = None
        for key in ("total_tokens", "total_token_count"):
            total_tokens = self._to_int(usage.get(key))
            if total_tokens is not None:
                break

        if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
            total_tokens = prompt_tokens + completion_tokens
        if completion_tokens is None and prompt_tokens is not None and total_tokens is not None:
            completion_tokens = max(total_tokens - prompt_tokens, 0)

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    def _extract_text(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(self._extract_text(item) for item in value)
        if not isinstance(value, dict):
            return ""

        chunks: List[str] = []
        choices = value.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue

                for key in ("delta", "message"):
                    nested = choice.get(key)
                    if isinstance(nested, dict):
                        content = nested.get("content")
                        if isinstance(content, str):
                            chunks.append(content)

                text = choice.get("text")
                if isinstance(text, str):
                    chunks.append(text)

        for key in ("content", "text", "result", "answer", "output"):
            item = value.get(key)
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, (dict, list)):
                chunks.append(self._extract_text(item))

        data = value.get("data")
        if isinstance(data, (dict, list)):
            chunks.append(self._extract_text(data))

        return "".join(chunks)

    @staticmethod
    def _validate_response_body(body: Any) -> Tuple[bool, str]:
        if not isinstance(body, dict):
            return True, ""

        error = body.get("error")
        if error:
            return False, f"Business error: {str(error)[:200]}"

        for key in ("code", "status_code", "error_code"):
            if key not in body:
                continue
            value = body.get(key)
            number_value = AIGatewayStressTester._to_int(value)
            if number_value is not None and number_value not in (0, 200):
                return False, f"Business {key}: {value}"

        status = str(body.get("status", "")).strip().lower()
        if status in {"error", "failed", "failure"}:
            return False, f"Business status: {body.get('status')}"

        return True, ""

    @staticmethod
    def _parse_stream_line(line: Any) -> Tuple[str, Any]:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        elif not isinstance(line, str):
            line = str(line)

        line = line.strip()
        if not line:
            return "empty", None

        payload = line[5:].strip() if line.startswith("data:") else line
        if payload == "[DONE]":
            return "done", None

        try:
            return "json", json.loads(payload)
        except json.JSONDecodeError:
            return "raw", payload

    def _record_success(self, result: Dict[str, Any]) -> None:
        with self.lock:
            self.results["success"].append(result)
            self.results["timing"].append(
                {
                    "request_id": result["request_id"],
                    "start_epoch": result["start_epoch"],
                    "end_epoch": result["end_epoch"],
                    "first_token_epoch": result["first_token_epoch"],
                }
            )

    def _record_failure(
            self,
            request_id: int,
            start_epoch: float,
            start_perf: float,
            error_message: str,
            status_code: Optional[int] = None,
    ) -> Dict[str, Any]:
        end_perf = time.perf_counter()
        end_epoch = time.time()
        response_time_ms = (end_perf - start_perf) * 1000
        record = {
            "request_id": request_id,
            "response_time_ms": response_time_ms,
            "error_message": error_message,
            "status_code": status_code,
            "start_epoch": start_epoch,
            "end_epoch": end_epoch,
        }
        with self.lock:
            self.results["failed"].append(record)
        return record

    def _send_non_stream_request(
            self,
            request_id: int,
            start_epoch: float,
            start_perf: float,
            payload: Dict[str, Any],
            headers: Dict[str, str],
    ) -> None:
        response = requests.post(
            self.url,
            headers=headers,
            json=payload,
            verify=False,
            stream=False,
            timeout=300,
        )
        end_perf = time.perf_counter()
        end_epoch = time.time()
        response_time_ms = (end_perf - start_perf) * 1000

        if response.status_code != 200:
            error_content = response.text[:200]
            self._record_failure(
                request_id,
                start_epoch,
                start_perf,
                f"HTTP {response.status_code}, Response: {error_content}",
                response.status_code,
            )
            print(f"✗ Request {request_id} failed: HTTP {response.status_code}")
            return

        try:
            body = response.json()
        except ValueError:
            self._record_failure(
                request_id,
                start_epoch,
                start_perf,
                f"Invalid JSON response: {response.text[:200]}",
                response.status_code,
            )
            print(f"✗ Request {request_id} failed: invalid JSON response")
            return

        ok, error_message = self._validate_response_body(body)
        if not ok:
            self._record_failure(
                request_id,
                start_epoch,
                start_perf,
                error_message,
                response.status_code,
            )
            print(f"✗ Request {request_id} failed: {error_message}")
            return

        usage = self._extract_token_usage(body)
        response_text = self._extract_text(body)
        self._record_success(
            {
                "request_id": request_id,
                "response_time_ms": response_time_ms,
                "response_header_ms": None,
                "first_token_latency_ms": None,
                "first_token_epoch": None,
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "total_tokens": usage["total_tokens"],
                "stream_event_count": 0,
                "output_chars": len(response_text),
                "start_epoch": start_epoch,
                "end_epoch": end_epoch,
            }
        )

        if request_id % max(1, self.total_requests // 20) == 0:
            print(
                f"✓ Request {request_id}: total={response_time_ms:.2f}ms, "
                f"completion_tokens={usage['completion_tokens']}"
            )

    def _send_stream_request(
            self,
            request_id: int,
            start_epoch: float,
            start_perf: float,
            payload: Dict[str, Any],
            headers: Dict[str, str],
    ) -> None:
        response = requests.post(
            self.url,
            headers=headers,
            json=payload,
            verify=False,
            stream=True,
            timeout=300,
        )
        header_perf = time.perf_counter()
        response_header_ms = (header_perf - start_perf) * 1000

        if response.status_code != 200:
            error_content = response.text[:200]
            self._record_failure(
                request_id,
                start_epoch,
                start_perf,
                f"HTTP {response.status_code}, Response: {error_content}",
                response.status_code,
            )
            print(f"✗ Request {request_id} failed: HTTP {response.status_code}")
            return

        first_token_perf = None
        first_token_epoch = None
        stream_event_count = 0
        output_chunks: List[str] = []
        usage = {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
        failure_message = None

        with response:
            for line in response.iter_lines(decode_unicode=True):
                event_type, event_value = self._parse_stream_line(line or "")
                if event_type in {"empty", "done"}:
                    if event_type == "done":
                        break
                    continue

                stream_event_count += 1
                now_perf = time.perf_counter()

                if event_type == "json":
                    ok, error_message = self._validate_response_body(event_value)
                    if not ok:
                        failure_message = error_message
                        break

                    event_usage = self._extract_token_usage(event_value)
                    if any(value is not None for value in event_usage.values()):
                        usage = event_usage

                    text = self._extract_text(event_value)
                else:
                    text = str(event_value)

                if text:
                    output_chunks.append(text)
                    if first_token_perf is None:
                        first_token_perf = now_perf
                        first_token_epoch = time.time()

        end_perf = time.perf_counter()
        end_epoch = time.time()
        response_time_ms = (end_perf - start_perf) * 1000

        if failure_message:
            self._record_failure(
                request_id,
                start_epoch,
                start_perf,
                failure_message,
                response.status_code,
            )
            print(f"✗ Request {request_id} failed: {failure_message}")
            return

        first_token_latency_ms = (
            (first_token_perf - start_perf) * 1000 if first_token_perf is not None else None
        )
        self._record_success(
            {
                "request_id": request_id,
                "response_time_ms": response_time_ms,
                "response_header_ms": response_header_ms,
                "first_token_latency_ms": first_token_latency_ms,
                "first_token_epoch": first_token_epoch,
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "total_tokens": usage["total_tokens"],
                "stream_event_count": stream_event_count,
                "output_chars": len("".join(output_chunks)),
                "start_epoch": start_epoch,
                "end_epoch": end_epoch,
            }
        )

        if request_id % max(1, self.total_requests // 20) == 0:
            print(
                f"✓ Request {request_id}: total={response_time_ms:.2f}ms, "
                f"ttft={self._format_ms(first_token_latency_ms)}, "
                f"completion_tokens={usage['completion_tokens']}"
            )

    def send_request(self, request_id: int) -> None:
        """发送单个请求，测量可准确获得的时间和 token 指标。"""
        start_epoch = time.time()
        start_perf = time.perf_counter()

        try:
            payload = self._build_payload()
            headers = self.generate_auth_headers()

            if self.stream:
                self._send_stream_request(request_id, start_epoch, start_perf, payload, headers)
            else:
                self._send_non_stream_request(request_id, start_epoch, start_perf, payload, headers)

        except requests.exceptions.Timeout:
            failed = self._record_failure(request_id, start_epoch, start_perf, "Timeout")
            print(
                f"✗ Request {request_id} timeout after "
                f"{failed['response_time_ms']:.2f}ms"
            )
        except Exception as e:
            self._record_failure(request_id, start_epoch, start_perf, str(e)[:200])
            print(f"✗ Request {request_id} exception: {str(e)[:200]}")

    @staticmethod
    def _percentile(values: List[float], percentile: int) -> Optional[float]:
        if not values:
            return None
        return float(np.percentile(values, percentile))

    @classmethod
    def _latency_stats(cls, prefix: str, values: List[float]) -> Dict[str, Optional[float]]:
        return {
            f"avg_{prefix}_ms": float(np.mean(values)) if values else None,
            f"p50_{prefix}_ms": cls._percentile(values, 50),
            f"p90_{prefix}_ms": cls._percentile(values, 90),
            f"p95_{prefix}_ms": cls._percentile(values, 95),
            f"p99_{prefix}_ms": cls._percentile(values, 99),
            f"max_{prefix}_ms": float(np.max(values)) if values else None,
            f"min_{prefix}_ms": float(np.min(values)) if values else None,
        }

    def run_concurrent_test(self, concurrent_level: int) -> Dict[str, Any]:
        """运行特定并发级别的测试。"""
        print(f"\n{'=' * 60}")
        print(f"测试并发级别: {concurrent_level}")
        print(f"{'=' * 60}")

        self.results = self._empty_results()
        start_time = time.perf_counter()

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrent_level) as executor:
            futures = {
                executor.submit(self.send_request, i + 1): i + 1
                for i in range(self.total_requests)
            }

            for future in tqdm(
                    concurrent.futures.as_completed(futures),
                    total=self.total_requests,
                    desc=f"Concurrent {concurrent_level}",
            ):
                request_id = futures[future]
                try:
                    future.result()
                except Exception as e:
                    with self.lock:
                        self.results["failed"].append(
                            {
                                "request_id": request_id,
                                "response_time_ms": None,
                                "error_message": f"Thread exception: {str(e)[:200]}",
                                "status_code": None,
                                "start_epoch": None,
                                "end_epoch": None,
                            }
                        )

        total_duration = time.perf_counter() - start_time

        success_count = len(self.results["success"])
        failed_count = len(self.results["failed"])
        completed_count = success_count + failed_count
        attempted_count = self.total_requests
        success_rate = (success_count / attempted_count) * 100 if attempted_count > 0 else 0

        response_times = [
            result["response_time_ms"]
            for result in self.results["success"]
            if result["response_time_ms"] is not None
        ]
        first_token_times = [
            result["first_token_latency_ms"]
            for result in self.results["success"]
            if result["first_token_latency_ms"] is not None
        ]
        known_completion_tokens = [
            result["completion_tokens"]
            for result in self.results["success"]
            if result["completion_tokens"] is not None
        ]

        total_completion_tokens = sum(known_completion_tokens)
        token_usage_request_count = len(known_completion_tokens)
        token_usage_coverage = (
            (token_usage_request_count / success_count) * 100 if success_count > 0 else 0
        )

        success_qps = success_count / total_duration if total_duration > 0 else 0
        total_qps = completed_count / total_duration if total_duration > 0 else 0
        token_throughput = (
            total_completion_tokens / total_duration
            if total_duration > 0 and token_usage_request_count > 0
            else None
        )

        response_stats = self._latency_stats("response", response_times)
        first_token_stats = self._latency_stats("first_token", first_token_times)

        result = {
            "concurrent": concurrent_level,
            "attempted_requests": attempted_count,
            "completed_requests": completed_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "success_rate": success_rate,
            "total_duration": total_duration,
            "qps": success_qps,
            "success_qps": success_qps,
            "total_qps": total_qps,
            "total_completion_tokens": total_completion_tokens,
            "token_usage_request_count": token_usage_request_count,
            "token_usage_coverage": token_usage_coverage,
            "token_throughput": token_throughput,
            "first_token_sample_count": len(first_token_times),
            **response_stats,
            **first_token_stats,
        }

        self._print_results(result)
        return result

    @staticmethod
    def _format_number(value: Optional[float], suffix: str = "", precision: int = 2) -> str:
        if value is None:
            return "N/A"
        return f"{value:.{precision}f}{suffix}"

    @classmethod
    def _format_ms(cls, value: Optional[float]) -> str:
        return cls._format_number(value, "ms")

    def _print_results(self, result: Dict[str, Any]) -> None:
        """打印测试结果。"""
        print(f"\n测试结果 - 并发 {result['concurrent']}:")
        print(f"{'=' * 60}")
        print(f"计划请求数: {result['attempted_requests']}")
        print(f"完成请求数: {result['completed_requests']}")
        print(f"成功数: {result['success_count']} ({result['success_rate']:.2f}%)")
        print(f"失败数: {result['failed_count']}")
        print(f"总耗时: {result['total_duration']:.2f}s")
        print(f"总QPS(含失败): {result['total_qps']:.2f}")
        print(f"成功QPS: {result['success_qps']:.2f}")

        token_throughput = result["token_throughput"]
        if token_throughput is None:
            print("输出Token吞吐量: N/A (响应未返回 completion token usage)")
        else:
            print(
                f"输出Token吞吐量: {token_throughput:.2f} tokens/s "
                f"(usage覆盖率 {result['token_usage_coverage']:.1f}%)"
            )

        print("\n响应时间 (完整响应):")
        print(f"  平均: {self._format_ms(result['avg_response_ms'])}")
        print(f"  P50: {self._format_ms(result['p50_response_ms'])}")
        print(f"  P90: {self._format_ms(result['p90_response_ms'])}")
        print(f"  P95: {self._format_ms(result['p95_response_ms'])}")
        print(f"  P99: {self._format_ms(result['p99_response_ms'])}")
        print(f"  最大: {self._format_ms(result['max_response_ms'])}")
        print(f"  最小: {self._format_ms(result['min_response_ms'])}")

        print("\n首Token延迟 (仅 stream=True 且返回文本增量时可测):")
        if result["first_token_sample_count"] == 0:
            print("  N/A")
        else:
            print(f"  样本数: {result['first_token_sample_count']}")
            print(f"  平均: {self._format_ms(result['avg_first_token_ms'])}")
            print(f"  P50: {self._format_ms(result['p50_first_token_ms'])}")
            print(f"  P95: {self._format_ms(result['p95_first_token_ms'])}")

        if self.results["failed"]:
            print("\n失败原因统计:")
            error_counts = defaultdict(int)
            for item in self.results["failed"]:
                error = item["error_message"]
                error_type = error.split(":")[0] if ":" in error else error[:50]
                error_counts[error_type] += 1

            for error, count in error_counts.items():
                print(f"  {error}: {count}")

    def save_results_to_csv(self, all_results: List[Dict[str, Any]]) -> None:
        """保存所有测试结果到CSV文件。"""
        if not all_results:
            return

        root, ext = os.path.splitext(self.output_file)
        ext = ext or ".csv"
        filename = f"{root}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"

        with open(filename, "w", newline="", encoding="utf-8-sig") as csvfile:
            fieldnames = list(all_results[0].keys())
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

            writer.writeheader()
            for result in all_results:
                writer.writerow(result)

        print(f"\n结果已保存到: {filename}")

    def find_breaking_point(self, start_concurrent: int, max_concurrent: int, step: int):
        """逐步增加并发，找到性能拐点。"""
        print(f"\n{'=' * 60}")
        print("开始极限并发测试 - 寻找性能拐点")
        print(f"起始并发: {start_concurrent}, 最大并发: {max_concurrent}, 步长: {step}")
        print(f"测试上下文字符数: {self.context_length}")
        print(f"stream模式: {self.stream}")
        print(f"{'=' * 60}")

        all_results = []
        previous_p95 = None
        breaking_point = None

        for concurrent_level in range(start_concurrent, max_concurrent + 1, step):
            result = self.run_concurrent_test(concurrent_level)
            all_results.append(result)

            if result["success_rate"] < 80:
                print(f"\n⚠️ 警告: 成功率降至 {result['success_rate']:.2f}%")
                if breaking_point is None:
                    breaking_point = concurrent_level

            current_p95 = result["p95_response_ms"]
            if (
                    current_p95 is not None
                    and previous_p95 is not None
                    and previous_p95 > 0
                    and current_p95 > previous_p95 * 2
            ):
                print(
                    f"\n⚠️ 警告: P95响应时间翻倍 "
                    f"({previous_p95:.2f}ms -> {current_p95:.2f}ms)"
                )
                if breaking_point is None:
                    breaking_point = concurrent_level

            previous_p95 = current_p95

            if result["success_rate"] < 50:
                print("\n⚠️ 成功率低于50%，测试终止")
                break

            if concurrent_level < max_concurrent:
                print("\n等待5秒后继续下一轮测试...")
                time.sleep(5)

        self.save_results_to_csv(all_results)
        self.print_summary(all_results, breaking_point)

        return all_results, breaking_point

    def print_summary(self, all_results: List[Dict[str, Any]], breaking_point: Optional[int]) -> None:
        """打印测试总结。"""
        print(f"\n{'=' * 60}")
        print("测试总结")
        print(f"{'=' * 60}")

        if not all_results:
            print("没有可汇总的测试结果")
            return

        if breaking_point:
            print(f"性能拐点出现在并发 {breaking_point} 左右")
        else:
            print("在测试范围内未发现明显性能拐点")

        print("\n各并发级别性能对比:")
        print(f"{'并发':^8} {'成功率%':^10} {'成功QPS':^10} {'P95(ms)':^12} {'TTFT P95(ms)':^15}")
        print(f"{'-' * 68}")

        for result in all_results:
            p95_response = self._format_number(result["p95_response_ms"], precision=1)
            p95_ttft = self._format_number(result["p95_first_token_ms"], precision=1)
            print(
                f"{result['concurrent']:^8} "
                f"{result['success_rate']:^10.1f} "
                f"{result['success_qps']:^10.1f} "
                f"{p95_response:^12} "
                f"{p95_ttft:^15}"
            )

        best_qps = max(all_results, key=lambda item: item["success_qps"])
        print(
            f"\n最佳成功吞吐量: 并发 {best_qps['concurrent']} 时 "
            f"成功QPS = {best_qps['success_qps']:.2f}"
        )

        comparable = [
            (result["concurrent"], result["p95_response_ms"])
            for result in all_results
            if result["p95_response_ms"] is not None
        ]
        for i in range(1, len(comparable)):
            previous_concurrent, previous_p95 = comparable[i - 1]
            current_concurrent, current_p95 = comparable[i]
            if current_p95 > previous_p95 * 1.5:
                print(
                    f"响应时间拐点: 并发从 {previous_concurrent} 到 {current_concurrent} "
                    f"时 P95 从 {previous_p95:.1f}ms 增长到 {current_p95:.1f}ms"
                )
                break


def main():
    parser = argparse.ArgumentParser(description="AI Gateway 长上下文极限压力测试")
    parser.add_argument(
        "--url",
        default="https://192.168.0.213:18300/ai-inference-gateway/predict",
        help="API endpoint URL",
    )
    parser.add_argument(
        "--app-key",
        default="1001300033",
        help="Application key for HMAC authentication",
    )
    parser.add_argument(
        "--secret-key",
        default="24e74daf74124b0b96c9cb113162a976",
        help="Secret key for HMAC authentication",
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=None,
        help="单次测试的并发数；如果不指定，则执行自动递增测试",
    )
    parser.add_argument(
        "--total",
        type=int,
        default=50,
        help="每个并发级别的请求总数",
    )
    parser.add_argument(
        "--context-length",
        type=int,
        default=32000,
        help="测试上下文字符数，不等于模型 token 数",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=100,
        help="接口最大输出 token 数",
    )
    parser.add_argument(
        "--no-stream",
        action="store_false",
        dest="stream",
        default=True,
        help="禁用流式请求（默认流式开启）",
    )
    parser.add_argument(
        "--start-concurrent",
        type=int,
        default=1,
        help="起始并发数（自动测试模式）",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=20,
        help="最大并发数（自动测试模式）",
    )
    parser.add_argument(
        "--step",
        type=int,
        default=2,
        help="并发数步进（自动测试模式）",
    )
    parser.add_argument("--output", default="stress_test_results.csv", help="输出文件名")

    args = parser.parse_args()

    if args.total <= 0:
        parser.error("--total 必须大于 0")
    if args.concurrent is not None and args.concurrent <= 0:
        parser.error("--concurrent 必须大于 0")
    if args.start_concurrent <= 0 or args.max_concurrent <= 0 or args.step <= 0:
        parser.error("--start-concurrent、--max-concurrent、--step 必须大于 0")
    if args.context_length <= 0:
        parser.error("--context-length 必须大于 0")
    if args.max_tokens <= 0:
        parser.error("--max-tokens 必须大于 0")

    requests.packages.urllib3.disable_warnings()

    print(f"\n{'=' * 60}")
    print("AI Gateway 极限压力测试工具")
    print(f"{'=' * 60}")
    print(f"目标URL: {args.url}")
    print(f"测试上下文字符数: {args.context_length}")
    print("真实prompt/output token数: 以接口返回 usage 为准")
    print(f"每个并发级别请求数: {args.total}")
    print(f"stream模式: {args.stream}")

    tester = AIGatewayStressTester(
        url=args.url,
        app_key=args.app_key,
        secret_key=args.secret_key,
        concurrent=args.concurrent or 1,
        total_requests=args.total,
        context_length=args.context_length,
        output_file=args.output,
        stream=args.stream,
        max_tokens=args.max_tokens,
    )

    if args.concurrent:
        tester.run_concurrent_test(args.concurrent)
    else:
        tester.find_breaking_point(
            start_concurrent=args.start_concurrent,
            max_concurrent=args.max_concurrent,
            step=args.step,
        )


if __name__ == "__main__":
    main()
