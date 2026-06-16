# -*- coding: utf-8 -*-
# !/usr/bin/env python3
"""
AI Gateway 全指标压力测试脚本 V3.2
覆盖: 32K长文本 + 1024短文本 + 高并发 + 资源监控
支持自定义并发数和请求数
修复: concurrent变量名冲突
"""

import time
import threading
import concurrent.futures
import requests
import hashlib
import hmac
import base64
import argparse
import json
import csv
import random
import psutil
import subprocess
import math
import re
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, Any, Optional, Tuple, Sequence
import numpy as np
from tqdm import tqdm


DEFAULT_SYSTEM_PROMPT = "你是一个关键词提取专家。"
MAX_32K_TOKENS = 32768
TARGET_32K_INPUT_TOKENS = 32000
SINGLE_1024_REQUESTS = 100
CONCURRENT_LATENCY_REQUESTS = 100
# CONCURRENT_LATENCY = 100
CONCURRENT_LATENCY = 40
CAPACITY_CONCURRENCY = 160
CAPACITY_REQUESTS = 1000
CONTEXT_32K_CONCURRENCY = 160
CONTEXT_32K_REQUESTS = 1000
HTTP_POOL_CONNECTIONS = 200
HTTP_POOL_MAXSIZE = 200
DRY_RUN_REQUESTS = 5
DRY_RUN_MAX_TOKENS = 8
SUCCESS_RATE_SLA_PCT = 90.0
STREAM_FIRST_TOKEN_SLA_MS = 7000.0
NON_STREAM_TOTAL_SLA_MS = 30000.0
CONCURRENT_LATENCY_SLA_MS = 30000.0
MODEL_THROUGHPUT_SLA_TOK_PER_SEC = 15.55
CPU_SLA_PCT = 65.0
MEMORY_SLA_PCT = 70.0
ALLOW_PARTIAL_THROUGHPUT_ESTIMATION = False
SERVER_LATENCY_HEADER_CANDIDATES = (
    "x-server-latency-ms",
    "x-model-latency-ms",
    "x-upstream-latency-ms",
    "x-inference-latency-ms",
    "x-process-time-ms",
    "x-process-time",
    "server-timing",
)


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """返回分位值；没有样本时返回 None，避免把 0 误判为真实指标。"""
    if not values:
        return None
    sorted_values = sorted(float(v) for v in values)
    rank = max(1, math.ceil(len(sorted_values) * pct / 100.0))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def average(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return float(np.mean([float(v) for v in values]))


def format_ms(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.0f}ms"


def format_pct(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.1f}%"


def pass_fail(value: Optional[float], operator: str, threshold: float) -> str:
    if value is None:
        return "N/A"
    if operator == "<=":
        return "通过" if value <= threshold else "失败"
    if operator == ">=":
        return "通过" if value >= threshold else "失败"
    raise ValueError(f"Unsupported operator: {operator}")


def pass_fail_mark(value: Optional[float], operator: str, threshold: float) -> str:
    if value is None:
        return "N/A"
    if operator == "<=":
        return "✅" if value <= threshold else "❌"
    if operator == ">=":
        return "✅" if value >= threshold else "❌"
    raise ValueError(f"Unsupported operator: {operator}")


def parse_latency_header_value(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    text = str(value).strip().lower()
    if not text:
        return None

    server_timing_match = re.search(r"(?:dur=)([0-9]+(?:\.[0-9]+)?)", text)
    if server_timing_match:
        return float(server_timing_match.group(1))

    number_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(ms|s)?", text)
    if not number_match:
        return None
    latency = float(number_match.group(1))
    unit = number_match.group(2)
    return latency * 1000.0 if unit == "s" else latency


def extract_server_latency_ms(headers: Any, preferred_header: Optional[str] = None) -> Optional[float]:
    header_names = []
    if preferred_header:
        header_names.append(preferred_header)
    header_names.extend(name for name in SERVER_LATENCY_HEADER_CANDIDATES if name not in header_names)

    for header_name in header_names:
        parsed = parse_latency_header_value(headers.get(header_name))
        if parsed is not None:
            return parsed
    return None


def nested_get(data: Dict[str, Any], paths: Sequence[Tuple[str, ...]]) -> Any:
    for path in paths:
        current: Any = data
        found = True
        for key in path:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                found = False
                break
        if found:
            return current
    return None


def extract_usage(data: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """从常见 OpenAI/Gateway 响应结构中提取真实 token usage。"""
    usage = nested_get(
        data,
        (
            ("usage",),
            ("token_usage",),
            ("response_metadata", "usage"),
            ("response_metadata", "token_usage"),
        ),
    )
    if not isinstance(usage, dict):
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    def first_int(keys: Sequence[str]) -> Optional[int]:
        for key in keys:
            value = usage.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    prompt_tokens = first_int(("prompt_tokens", "input_tokens", "prompt_token_count"))
    completion_tokens = first_int(
        ("completion_tokens", "output_tokens", "generated_tokens", "completion_token_count")
    )
    total_tokens = first_int(("total_tokens", "total_token_count"))

    if completion_tokens is None and total_tokens is not None and prompt_tokens is not None:
        completion_tokens = max(0, total_tokens - prompt_tokens)
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def merge_usage(current: Dict[str, Optional[int]], new_usage: Dict[str, Optional[int]]) -> Dict[str, Optional[int]]:
    return {
        key: new_usage.get(key) if new_usage.get(key) is not None else current.get(key)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


def extract_delta_content(data: Dict[str, Any]) -> str:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            delta = choice.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                return delta["content"]
            message = choice.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
            if isinstance(choice.get("text"), str):
                return choice["text"]

    for key in ("content", "text", "output_text"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return ""


def parse_sse_json(data_text: str) -> Optional[Dict[str, Any]]:
    data_text = data_text.strip()
    if not data_text or data_text == "[DONE]":
        return None
    try:
        parsed = json.loads(data_text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class SystemResourceMonitor:
    """系统资源监控器"""

    def __init__(self, interval=1.0):
        self.interval = interval
        self.monitoring = False
        self.monitor_thread = None
        self.cpu_samples = []
        self.memory_samples = []
        self.gpu_samples = []
        self.lock = threading.Lock()

    def start(self):
        """开始监控"""
        with self.lock:
            self.cpu_samples.clear()
            self.memory_samples.clear()
            self.gpu_samples.clear()
        self.monitoring = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        print("📊 系统资源监控已启动")

    def stop(self) -> Dict[str, Any]:
        """停止监控并返回统计"""
        self.monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=2)

        with self.lock:
            cpu_data = self.cpu_samples.copy()
            mem_data = self.memory_samples.copy()
            gpu_data = self.gpu_samples.copy()

        return {
            'cpu_avg': average(cpu_data),
            'cpu_max': float(np.max(cpu_data)) if cpu_data else None,
            'cpu_p95': percentile(cpu_data, 95),
            'memory_avg': average(mem_data),
            'memory_max': float(np.max(mem_data)) if mem_data else None,
            'memory_p95': percentile(mem_data, 95),
            'gpu_avg': average(gpu_data),
            'gpu_max': float(np.max(gpu_data)) if gpu_data else None,
            'gpu_p95': percentile(gpu_data, 95),
            'cpu_samples': len(cpu_data),
            'memory_samples': len(mem_data),
            'gpu_samples': len(gpu_data)
        }

    def _monitor_loop(self):
        """监控循环"""
        while self.monitoring:
            try:
                # CPU使用率
                cpu_percent = psutil.cpu_percent(interval=0.5)

                # 内存使用率
                memory = psutil.virtual_memory()
                memory_percent = memory.percent

                with self.lock:
                    self.cpu_samples.append(cpu_percent)
                    self.memory_samples.append(memory_percent)

                # 尝试获取GPU使用率
                try:
                    result = subprocess.run(
                        ['nvidia-smi', '--query-gpu=utilization.gpu', '--format=csv,noheader,nounits'],
                        capture_output=True, text=True, timeout=2
                    )
                    if result.returncode == 0:
                        gpu_values = [
                            float(line.strip())
                            for line in result.stdout.splitlines()
                            if line.strip()
                        ]
                        if gpu_values:
                            with self.lock:
                                self.gpu_samples.append(float(np.mean(gpu_values)))
                except:
                    pass

                time.sleep(self.interval)
            except:
                break


class PreciseTokenCalculator:
    """精确的token计算器"""

    def __init__(self, url, app_key, secret_key, component_code, model, session=None):
        self.url = url
        self.app_key = app_key
        self.secret_key = secret_key
        self.component_code = component_code
        self.model = model
        self.session = session or requests.Session()

    def generate_auth_headers(self) -> Dict[str, str]:
        curl_date = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
        date_str = f"x-date: {curl_date}"

        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            date_str.encode('utf-8'),
            hashlib.sha256
        ).digest()
        date_base = base64.b64encode(signature).decode('utf-8')

        authorization = f'hmac username="{self.app_key}", algorithm="hmac-sha256", headers="x-date", signature="{date_base}"'

        return {
            'x-date': curl_date,
            'Authorization': authorization,
            'Content-Type': 'application/json'
        }

    def get_actual_token_count(self, text: str) -> Optional[int]:
        """通过非流式请求获取实际token计数"""
        payload = {
            "componentCode": self.component_code,
            "model": self.model,
            "messages": [{"role": "user", "content": text}],
            "stream": False,
            "max_tokens": 1,
            "temperature": 0.0
        }

        try:
            headers = self.generate_auth_headers()
            response = self.session.post(
                self.url, headers=headers, json=payload,
                verify=False, timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                if 'usage' in data:
                    return data['usage']['prompt_tokens']
        except:
            pass

        return None

    def generate_text_with_exact_tokens(self, target_tokens: int) -> str:
        """生成恰好指定token数的测试文本"""
        base_paragraphs = [
            "人工智能技术正在深刻改变着我们的生活方式和工作模式。从智能语音助手到自动驾驶汽车，从医疗诊断到金融分析，AI的应用领域不断扩展。深度学习、自然语言处理、计算机视觉等核心技术的发展，为各行各业带来了革命性的变化。",
            "气候变化是当今世界面临的最严峻挑战之一。全球变暖导致极端天气事件频发，海平面上升威胁沿海城市。各国政府正在积极制定减排目标，推动能源转型。可再生能源技术如太阳能、风能的成本持续下降，为实现碳中和提供了技术支撑。",
            "数字化转型已成为企业发展的必然趋势。云计算、大数据、物联网等技术的融合应用，帮助企业优化业务流程、提升运营效率。数据驱动的决策模式正在取代传统的经验判断，为企业创造新的竞争优势。",
            "生物医药领域正在经历前所未有的创新浪潮。基因编辑技术CRISPR为遗传病治疗带来希望，mRNA疫苗在新冠疫情中展现出巨大潜力。精准医疗通过基因组测序和大数据分析，为患者提供个性化的治疗方案。",
            "量子计算作为下一代计算范式，有望在密码学、材料科学、药物研发等领域实现突破。虽然目前仍处于早期阶段，但各国政府和企业都在加大投入，争抢量子计算的制高点。量子比特的稳定性和纠错技术是当前研究的重点。"
        ]

        # 测试token比率
        sample_text = base_paragraphs[0]
        sample_tokens = self.get_actual_token_count(sample_text)

        if sample_tokens is None:
            sample_tokens = len(sample_text) * 1.8

        tokens_per_char = sample_tokens / len(sample_text)
        target_chars = int(target_tokens / tokens_per_char)

        # 生成文本
        generated = ""
        while len(generated) < target_chars:
            generated += random.choice(base_paragraphs) + "\n"

        generated = generated[:target_chars]

        # 验证并调整
        actual_tokens = self.get_actual_token_count(generated)

        if actual_tokens and abs(actual_tokens - target_tokens) > target_tokens * 0.1:
            adjustment_ratio = target_tokens / actual_tokens
            adjusted_chars = int(len(generated) * adjustment_ratio)
            generated = generated[:adjusted_chars]

        return generated


class FullMetricsTester:
    """全指标测试器"""

    def __init__(self, url, app_key, secret_key, component_code="04350558",
                 model="Qwen3-14B", concurrent_low=CONCURRENT_LATENCY,
                 concurrent_high=CONTEXT_32K_CONCURRENCY, total_requests=CONTEXT_32K_REQUESTS,
                 server_latency_header=None, allow_partial_throughput_estimation=ALLOW_PARTIAL_THROUGHPUT_ESTIMATION):
        self.url = url
        self.app_key = app_key
        self.secret_key = secret_key
        self.component_code = component_code
        self.model = model
        self.concurrent_low = concurrent_low
        self.concurrent_high = concurrent_high
        self.total_requests = total_requests
        self.server_latency_header = server_latency_header
        self.allow_partial_throughput_estimation = allow_partial_throughput_estimation

        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=HTTP_POOL_CONNECTIONS,
            pool_maxsize=HTTP_POOL_MAXSIZE,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.token_calculator = PreciseTokenCalculator(
            url, app_key, secret_key, component_code, model, session=self.session
        )

        self.results = defaultdict(list)
        self.lock = threading.Lock()
        self.resource_monitor = SystemResourceMonitor()

    def generate_auth_headers(self) -> Dict[str, str]:
        return self.token_calculator.generate_auth_headers()

    def get_prompt_token_count(self, user_content: str, system_content: str = DEFAULT_SYSTEM_PROMPT) -> Optional[int]:
        payload = {
            "componentCode": self.component_code,
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content}
            ],
            "stream": False,
            "max_tokens": 1,
            "temperature": 0.0
        }

        try:
            response = self.session.post(
                self.url,
                headers=self.generate_auth_headers(),
                json=payload,
                verify=False,
                timeout=60
            )
            if response.status_code != 200:
                return None
            data = response.json()
            return extract_usage(data).get("prompt_tokens")
        except Exception:
            return None

    def build_prompt_under_token_limit(
            self,
            prefix: str,
            suffix: str,
            target_input_tokens: int
    ) -> Tuple[str, Optional[int]]:
        """
        按完整 messages 的 prompt_tokens 控制输入长度。
        原脚本只控制正文 token，会遗漏 system/user 模板和提示词开销。
        """
        test_text = self.token_calculator.generate_text_with_exact_tokens(target_input_tokens)
        prompt = f"{prefix}{test_text}{suffix}"
        actual_tokens = self.get_prompt_token_count(prompt)

        if actual_tokens is None or actual_tokens <= target_input_tokens:
            return prompt, actual_tokens

        low, high = 0, len(test_text)
        best_prompt = f"{prefix}{suffix}"
        best_tokens = self.get_prompt_token_count(best_prompt)

        while low <= high:
            mid = (low + high) // 2
            candidate_prompt = f"{prefix}{test_text[:mid]}{suffix}"
            candidate_tokens = self.get_prompt_token_count(candidate_prompt)
            if candidate_tokens is None:
                break
            if candidate_tokens <= target_input_tokens:
                best_prompt = candidate_prompt
                best_tokens = candidate_tokens
                low = mid + 1
            else:
                high = mid - 1

        return best_prompt, best_tokens

    def build_1024_prompt(self) -> Tuple[str, Optional[int]]:
        return self.build_prompt_under_token_limit(
            prefix="请从以下文本中提取5个关键词，用逗号分隔。\n\n",
            suffix="\n\n关键词：",
            target_input_tokens=1024
        )

    def build_32k_prompt(self) -> Tuple[str, Optional[int]]:
        return self.build_prompt_under_token_limit(
            prefix="请从以下长文本中提取15-30个关键词，用逗号分隔。\n\n",
            suffix="\n\n关键词：",
            target_input_tokens=TARGET_32K_INPUT_TOKENS
        )

    def warmup(self):
        """预热模型"""
        print("🔥 预热模型...")
        warmup_text = "人工智能是计算机科学的一个分支。" * 10

        for i in range(3):
            payload = {
                "componentCode": self.component_code,
                "model": self.model,
                "messages": [{"role": "user", "content": warmup_text}],
                "stream": False,
                "max_tokens": 50,
                "temperature": 0.0
            }

            try:
                headers = self.generate_auth_headers()
                response = self.session.post(
                    self.url, headers=headers, json=payload,
                    verify=False, timeout=60
                )
                if response.status_code == 200:
                    print(f"  预热请求 {i + 1}/3 成功")
            except:
                print(f"  预热请求 {i + 1}/3 失败")

            time.sleep(1)

        print("✅ 预热完成\n")

    def test_single_concurrent_1024(self):
        """
        测试指标: 单并发，输入1024 tokens
        - 流式首token延迟 ≤ STREAM_FIRST_TOKEN_SLA_MS
        - 非流式总响应时间 P99 ≤ NON_STREAM_TOTAL_SLA_MS
        """
        print("=" * 80)
        print("📊 测试: 单并发 1024 tokens")
        print("=" * 80)

        prompt, actual_tokens = self.build_1024_prompt()
        print(f"实际输入tokens: {actual_tokens}")

        results = {
            'streaming': [],
            'non_streaming': [],
            'input_tokens': actual_tokens,
            'request_count': SINGLE_1024_REQUESTS
        }

        # 测试流式 (10次以获得稳定的P99)
        print(f"\n🌊 测试流式模式 ({SINGLE_1024_REQUESTS}次)...")
        print(f"\nStreaming test ({SINGLE_1024_REQUESTS} requests, concurrency=1)...")
        for i in tqdm(range(SINGLE_1024_REQUESTS), desc="streaming"):
            metrics = self._send_streaming_request(prompt, request_id=i + 1)
            if metrics:
                results['streaming'].append(metrics)
            time.sleep(0.5)

        # 测试非流式
        print(f"\n📦 测试非流式模式 ({SINGLE_1024_REQUESTS}次)...")
        print(f"\nNon-streaming test ({SINGLE_1024_REQUESTS} requests, concurrency=1)...")
        for i in tqdm(range(SINGLE_1024_REQUESTS), desc="non-streaming"):
            metrics = self._send_non_streaming_request(prompt, request_id=i + 1)
            if metrics:
                results['non_streaming'].append(metrics)
            time.sleep(0.5)

        self._print_1024_results(results)
        return results

    def _send_streaming_request(self, user_content: str, request_id: int, max_tokens: int = 200) -> Optional[Dict]:
        """发送流式请求并返回指标"""
        start_time = time.perf_counter()
        first_token_time = None
        first_content_time = None
        response_header_time = None
        output_parts = []
        usage = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
        stream_event_count = 0

        try:
            payload = {
                "componentCode": self.component_code,
                "model": self.model,
                "messages": [
                    {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content}
                ],
                "stream": True,
                "temperature": 0.2,
                "max_tokens": max_tokens,
                "top_p": 0.9
            }

            headers = self.generate_auth_headers()

            response = self.session.post(
                self.url, headers=headers, json=payload,
                verify=False, stream=True, timeout=120
            )
            response_header_time = time.perf_counter()
            server_latency_ms = extract_server_latency_ms(response.headers, self.server_latency_header)

            if response.status_code == 200:
                for raw_line in response.iter_lines(decode_unicode=False):
                    if not raw_line:
                        continue

                    if first_token_time is None:
                        first_token_time = time.perf_counter()

                    line = raw_line.decode('utf-8', errors='ignore').strip()
                    if not line.startswith('data:'):
                        continue

                    event = parse_sse_json(line[5:].strip())
                    if event is None:
                        continue

                    stream_event_count += 1
                    usage = merge_usage(usage, extract_usage(event))
                    content = extract_delta_content(event)
                    if content:
                        if first_content_time is None:
                            first_content_time = time.perf_counter()
                        output_parts.append(content)

                end_time = time.perf_counter()
                output_text = "".join(output_parts)
                completion_tokens = usage["completion_tokens"]

                return {
                    'request_id': request_id,
                    'total_time_ms': (end_time - start_time) * 1000,
                    'response_header_ms': (response_header_time - start_time) * 1000 if response_header_time else None,
                    'server_latency_ms': server_latency_ms,
                    'ttfb_ms': (first_token_time - start_time) * 1000 if first_token_time else None,
                    'first_token_ms': (first_content_time - start_time) * 1000 if first_content_time else None,
                    'prompt_tokens': usage["prompt_tokens"],
                    'completion_tokens': completion_tokens,
                    'total_tokens': usage["total_tokens"],
                    'stream_event_count': stream_event_count,
                    'output_chars': len(output_text),
                    'success': True
                }
            else:
                return {
                    'request_id': request_id,
                    'total_time_ms': (time.perf_counter() - start_time) * 1000,
                    'server_latency_ms': server_latency_ms,
                    'success': False,
                    'error': f"HTTP {response.status_code}"
                }

        except Exception as e:
            return {
                'request_id': request_id,
                'total_time_ms': (time.perf_counter() - start_time) * 1000,
                'success': False,
                'error': str(e)[:50]
            }

    def _send_non_streaming_request(self, user_content: str, request_id: int, max_tokens: int = 200) -> Optional[Dict]:
        """发送非流式请求并返回指标"""
        start_time = time.perf_counter()

        try:
            payload = {
                "componentCode": self.component_code,
                "model": self.model,
                "messages": [
                    {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content}
                ],
                "stream": False,
                "temperature": 0.2,
                "max_tokens": max_tokens,
                "top_p": 0.9
            }

            headers = self.generate_auth_headers()

            response = self.session.post(
                self.url, headers=headers, json=payload,
                verify=False, timeout=120
            )

            end_time = time.perf_counter()
            server_latency_ms = extract_server_latency_ms(response.headers, self.server_latency_header)

            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                usage = extract_usage(data if isinstance(data, dict) else {})
                return {
                    'request_id': request_id,
                    'total_time_ms': (end_time - start_time) * 1000,
                    'server_latency_ms': server_latency_ms,
                    # 非流式接口不暴露首 token，只能把完整响应耗时作为等价口径输出。
                    'ttft_ms': (end_time - start_time) * 1000,
                    'prompt_tokens': usage["prompt_tokens"],
                    'completion_tokens': usage["completion_tokens"],
                    'total_tokens': usage["total_tokens"],
                    'success': True
                }
            else:
                return {
                    'request_id': request_id,
                    'total_time_ms': (end_time - start_time) * 1000,
                    'server_latency_ms': server_latency_ms,
                    'success': False,
                    'error': f"HTTP {response.status_code}"
                }

        except Exception as e:
            return {
                'request_id': request_id,
                'total_time_ms': (time.perf_counter() - start_time) * 1000,
                'success': False,
                'error': str(e)[:50]
            }

    def _print_1024_results(self, results: Dict):
        """打印1024 tokens测试结果"""
        print("\n" + "=" * 80)
        print("📊 1024 tokens 测试结果")
        print("=" * 80)

        for mode in ('streaming', 'non_streaming'):
            data = results.get(mode, [])
            if not data:
                print(f"\n❌ {mode}: 无有效数据")
                continue

            success_data = [d for d in data if d.get('success')]
            if not success_data:
                print(f"\n❌ {mode}: 所有请求失败")
                continue

            mode_name = "流式" if mode == 'streaming' else "非流式"
            print(f"\n📌 {mode_name}模式:")
            print(f"   成功率: {len(success_data)}/{len(data)} ({len(success_data) / len(data) * 100:.1f}%)")

            if mode == 'streaming':
                ttfb_times = [d.get('ttfb_ms') for d in success_data if (d.get('ttfb_ms') or 0) > 0]
                first_token_times = [d.get('first_token_ms') for d in success_data if (d.get('first_token_ms') or 0) > 0]
                total_times = [d.get('total_time_ms') for d in success_data if d.get('total_time_ms') is not None]

                if ttfb_times:
                    print(
                        f"   TTFB: Avg={format_ms(average(ttfb_times))}, P50={format_ms(percentile(ttfb_times, 50))}, P99={format_ms(percentile(ttfb_times, 99))}")

                if first_token_times:
                    p99_first = percentile(first_token_times, 99)
                    print(
                        f"   首Token: Avg={format_ms(average(first_token_times))}, P50={format_ms(percentile(first_token_times, 50))}, P99={format_ms(p99_first)}")
                    print(f"   ✅ 流式首Token P99 ≤ {STREAM_FIRST_TOKEN_SLA_MS:.0f}ms: {pass_fail(p99_first, '<=', STREAM_FIRST_TOKEN_SLA_MS)} (要求≤{STREAM_FIRST_TOKEN_SLA_MS:.0f}ms)")

                if total_times:
                    p99_total = percentile(total_times, 99)
                    print(f"   总响应: Avg={format_ms(average(total_times))}, P99={format_ms(p99_total)}")
                    print(f"   ✅ 流式总响应P99 ≤ {NON_STREAM_TOTAL_SLA_MS:.0f}ms: {pass_fail(p99_total, '<=', NON_STREAM_TOTAL_SLA_MS)} (参考指标)")

            else:
                ttft_times = [d.get('ttft_ms') for d in success_data if d.get('ttft_ms') is not None]
                total_times = [d.get('total_time_ms') for d in success_data if d.get('total_time_ms') is not None]

                if ttft_times:
                    p99_ttft = percentile(ttft_times, 99)
                    print(f"   TTFT P99: {format_ms(p99_ttft)}")
                    print(f"   非流式TTFT不单独验收；非流式接口只能准确测完整响应时间。")

                if total_times:
                    p99_total = percentile(total_times, 99)
                    print(f"   总响应: Avg={format_ms(average(total_times))}, P99={format_ms(p99_total)}")
                    print(f"   ✅ 非流式总响应P99 ≤ {NON_STREAM_TOTAL_SLA_MS:.0f}ms: {pass_fail(p99_total, '<=', NON_STREAM_TOTAL_SLA_MS)} (要求≤{NON_STREAM_TOTAL_SLA_MS:.0f}ms)")

    def test_high_concurrency(self):
        """
        测试指标: 高并发
        - 流式/非流式 P99 ≤ CONCURRENT_LATENCY_SLA_MS
        - 模型吞吐量 QPS ≥ MODEL_THROUGHPUT_SLA_TOK_PER_SEC tok/s
        - 请求成功率 ≥ SUCCESS_RATE_SLA_PCT
        """
        print("\n" + "=" * 80)
        print(f"📊 测试: {self.concurrent_low}并发高负载")
        print("=" * 80)

        prompt, actual_tokens = self.build_1024_prompt()
        print(f"实际输入tokens: {actual_tokens}")

        results = {}

        # 测试流式
        print(f"\n🌊 测试流式 {self.concurrent_low}并发 {self.total_requests}请求...")
        self.resource_monitor.start()
        streaming_results = self._run_concurrent_test(
            prompt, concurrency=self.concurrent_low, total_requests=self.total_requests, stream_mode=True
        )
        resource_stats = self.resource_monitor.stop()

        results['streaming'] = {
            'metrics': streaming_results,
            'resources': resource_stats
        }

        self._print_high_concurrency_results(f'streaming_{self.concurrent_low}并发', streaming_results, resource_stats)

        # 等待系统恢复
        print("\n⏳ 等待30秒系统恢复...")
        time.sleep(30)

        # 测试非流式
        print(f"\n📦 测试非流式 {self.concurrent_low}并发 {self.total_requests}请求...")
        self.resource_monitor.start()
        non_streaming_results = self._run_concurrent_test(
            prompt, concurrency=self.concurrent_low, total_requests=self.total_requests, stream_mode=False
        )
        resource_stats = self.resource_monitor.stop()

        results['non_streaming'] = {
            'metrics': non_streaming_results,
            'resources': resource_stats
        }

        self._print_high_concurrency_results(f'non_streaming_{self.concurrent_low}并发', non_streaming_results,
                                             resource_stats)

        return results

    def test_target_100_concurrency(self):
        """100 concurrency, 100 requests, streaming and non-streaming latency/throughput."""
        print("\n" + "=" * 80)
        print(f"测试: {CONCURRENT_LATENCY}并发 {CONCURRENT_LATENCY_REQUESTS}请求，1024 tokens")
        print("=" * 80)

        prompt, actual_tokens = self.build_1024_prompt()
        print(f"实际输入tokens: {actual_tokens}")

        results = {'input_tokens': actual_tokens}

        print(f"\nStreaming {CONCURRENT_LATENCY} concurrency / {CONCURRENT_LATENCY_REQUESTS} requests...")
        self.resource_monitor.start()
        streaming_results = self._run_concurrent_test(
            prompt,
            concurrency=CONCURRENT_LATENCY,
            total_requests=CONCURRENT_LATENCY_REQUESTS,
            stream_mode=True
        )
        resource_stats = self.resource_monitor.stop()
        results['streaming'] = {'metrics': streaming_results, 'resources': resource_stats}
        self._print_sla_concurrency_results(
            f"streaming_{CONCURRENT_LATENCY}并发_{CONCURRENT_LATENCY_REQUESTS}请求",
            streaming_results,
            resource_stats
        )

        print("\n等待30秒系统恢复...")
        time.sleep(30)

        print(f"\nNon-streaming {CONCURRENT_LATENCY} concurrency / {CONCURRENT_LATENCY_REQUESTS} requests...")
        self.resource_monitor.start()
        non_streaming_results = self._run_concurrent_test(
            prompt,
            concurrency=CONCURRENT_LATENCY,
            total_requests=CONCURRENT_LATENCY_REQUESTS,
            stream_mode=False
        )
        resource_stats = self.resource_monitor.stop()
        results['non_streaming'] = {'metrics': non_streaming_results, 'resources': resource_stats}
        self._print_sla_concurrency_results(
            f"non_streaming_{CONCURRENT_LATENCY}并发_{CONCURRENT_LATENCY_REQUESTS}请求",
            non_streaming_results,
            resource_stats
        )

        return results

    def test_capacity_160_concurrency(self):
        """Capacity verification using CAPACITY_CONCURRENCY and CAPACITY_REQUESTS."""
        print("\n" + "=" * 80)
        print(f"测试: 目标并发{CAPACITY_CONCURRENCY}，{CAPACITY_REQUESTS}请求成功率")
        print("=" * 80)

        prompt, actual_tokens = self.build_1024_prompt()
        print(f"实际输入tokens: {actual_tokens}")

        self.resource_monitor.start()
        streaming_results = self._run_concurrent_test(
            prompt,
            concurrency=CAPACITY_CONCURRENCY,
            total_requests=CAPACITY_REQUESTS,
            stream_mode=True
        )
        resource_stats = self.resource_monitor.stop()
        self._print_sla_concurrency_results(
            f"capacity_streaming_{CAPACITY_CONCURRENCY}并发_{CAPACITY_REQUESTS}请求",
            streaming_results,
            resource_stats
        )

        return {
            'streaming': {'metrics': streaming_results, 'resources': resource_stats},
            'input_tokens': actual_tokens,
        }

    def test_high_concurrency_32k(self):
        """
        测试指标: 高并发 32K上下文
        - 请求成功率 ≥ SUCCESS_RATE_SLA_PCT
        - 输入≤MAX_32K_TOKENS
        """
        print("\n" + "=" * 80)
        print(f"📊 测试: {self.concurrent_high}并发 32K上下文")
        print("=" * 80)

        # 生成总输入不超过 32K tokens 的完整 prompt，避免正文 32K 加提示词后超限。
        print("生成32K tokens测试文本...")
        prompt, actual_tokens = self.build_32k_prompt()
        print(f"实际输入tokens: {actual_tokens}")

        # 测试流式
        print(f"\n🌊 测试流式 {self.concurrent_high}并发 {self.total_requests}请求 32K上下文...")
        self.resource_monitor.start()
        streaming_results = self._run_concurrent_test(
            prompt, concurrency=self.concurrent_high, total_requests=self.total_requests, stream_mode=True
        )
        resource_stats = self.resource_monitor.stop()

        print(f"\n✅ 输入tokens ≤ 32K: {'通过' if actual_tokens is not None and actual_tokens <= MAX_32K_TOKENS else '失败'}")
        self._print_sla_concurrency_results(f'streaming_32k_{self.concurrent_high}并发', streaming_results,
                                            resource_stats)

        return {
            'streaming': streaming_results,
            'resources': resource_stats,
            'input_tokens': actual_tokens
        }

    def dry_run_check(self):
        """快速预检: 连通性、usage 和服务端延迟头存在性"""
        print("\n" + "=" * 80)
        print("快速预检模式")
        print("=" * 80)

        prompt = "请回复：预检成功"
        results = []
        for i in range(DRY_RUN_REQUESTS):
            stream_mode = (i % 2 == 0)
            if stream_mode:
                result = self._send_streaming_request(prompt, i + 1, max_tokens=DRY_RUN_MAX_TOKENS)
            else:
                result = self._send_non_streaming_request(prompt, i + 1, max_tokens=DRY_RUN_MAX_TOKENS)
            results.append({
                "mode": "streaming" if stream_mode else "non_streaming",
                "success": bool(result and result.get("success")),
                "usage_present": bool(result and (
                    result.get("prompt_tokens") is not None
                    or result.get("completion_tokens") is not None
                    or result.get("total_tokens") is not None
                )),
                "server_latency_present": bool(result and result.get("server_latency_ms") is not None),
                "error": None if not result else result.get("error"),
            })

        success_count = sum(1 for item in results if item["success"])
        usage_count = sum(1 for item in results if item["usage_present"])
        latency_header_count = sum(1 for item in results if item["server_latency_present"])

        print(f"请求数: {DRY_RUN_REQUESTS}")
        print(f"成功数: {success_count}")
        print(f"usage返回数: {usage_count}")
        print(f"服务端耗时头返回数: {latency_header_count}")
        if latency_header_count == 0:
            print("⚠️ 服务端未返回处理耗时头，无法计算不含网络延迟，请确认网关是否支持 x-server-latency-ms 等头")
        return results

    def _run_concurrent_test(self, prompt: str, concurrency: int,
                             total_requests: int, stream_mode: bool) -> Dict[str, Any]:
        """运行并发测试"""

        start_time = time.perf_counter()
        results = {
            'success': [],
            'failed': [],
            'latencies': [],
            'server_latencies': [],
            'first_token_latencies': [],
            'completion_tokens': [],
            'prompt_tokens': [],
            'total_tokens': []
        }
        lock = threading.Lock()

        def worker(request_id):
            result = self._send_streaming_request(prompt, request_id) if stream_mode else self._send_non_streaming_request(prompt, request_id)

            with lock:
                if not result:
                    results['failed'].append({
                        'request_id': request_id,
                        'success': False,
                        'error': 'empty result'
                    })
                    return

                if result['success']:
                    results['success'].append(result)
                    results['latencies'].append(result['total_time_ms'])
                    server_latency_ms = result.get('server_latency_ms')
                    if server_latency_ms is not None:
                        results['server_latencies'].append(server_latency_ms)
                    first_token_ms = result.get('first_token_ms')
                    if first_token_ms is not None:
                        results['first_token_latencies'].append(first_token_ms)
                    completion_tokens = result.get('completion_tokens')
                    if completion_tokens is not None:
                        results['completion_tokens'].append(completion_tokens)
                    prompt_tokens = result.get('prompt_tokens')
                    if prompt_tokens is not None:
                        results['prompt_tokens'].append(prompt_tokens)
                    total_tokens = result.get('total_tokens')
                    if total_tokens is not None:
                        results['total_tokens'].append(total_tokens)
                else:
                    results['failed'].append(result)

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(worker, i + 1): i + 1 for i in range(total_requests)}

            for future in tqdm(concurrent.futures.as_completed(futures),
                               total=total_requests,
                               desc=f"并发{concurrency}",
                               unit="req"):
                request_id = futures[future]
                try:
                    future.result()
                except Exception as e:
                    with lock:
                        results['failed'].append({
                            'request_id': request_id,
                            'success': False,
                            'error': f"Thread exception: {str(e)[:100]}"
                        })

        end_time = time.perf_counter()
        results['total_duration'] = end_time - start_time
        results['total_requests'] = total_requests
        results['completed_requests'] = len(results['success']) + len(results['failed'])
        results['concurrent'] = concurrency

        return results

    def _print_high_concurrency_results(self, test_name: str, results: Dict,
                                        resources: Dict):
        """打印高并发测试结果"""
        print(f"\n" + "=" * 80)
        print(f"📊 {test_name} 测试结果")
        print("=" * 80)

        success_count = len(results['success'])
        failed_count = len(results['failed'])
        total = results['total_requests']
        completed = results.get('completed_requests', success_count + failed_count)
        success_rate = (success_count / total * 100) if total > 0 else 0

        print(f"\n📈 请求统计:")
        print(f"   总请求: {total}")
        print(f"   完成请求: {completed}")
        print(f"   成功: {success_count}")
        print(f"   失败: {failed_count}")
        print(f"   成功率: {success_rate:.2f}%")
        print(f"   ✅ 成功率≥{SUCCESS_RATE_SLA_PCT:.0f}%: {'通过' if success_rate >= SUCCESS_RATE_SLA_PCT else '失败'}")

        if results['latencies']:
            p99_latency = percentile(results['latencies'], 99)
            print(f"\n⚡ 延迟:")
            print(f"   P50: {format_ms(percentile(results['latencies'], 50))}")
            print(f"   P90: {format_ms(percentile(results['latencies'], 90))}")
            print(f"   P99: {format_ms(p99_latency)}")
            print(f"   ✅ P99 ≤ {CONCURRENT_LATENCY_SLA_MS:.0f}ms: {'通过' if p99_latency is not None and p99_latency <= CONCURRENT_LATENCY_SLA_MS else '失败'}")

        # 计算QPS
        if results['total_duration'] > 0:
            known_completion_tokens = results.get('completion_tokens', [])
            total_tokens = sum(known_completion_tokens)
            token_coverage = (len(known_completion_tokens) / success_count * 100) if success_count > 0 else 0
            model_qps = total_tokens / results['total_duration'] if known_completion_tokens else None
            request_qps = completed / results['total_duration']
            success_qps = success_count / results['total_duration']

            print(f"\n🚀 吞吐量:")
            print(f"   总请求QPS: {request_qps:.2f} req/s")
            print(f"   成功请求QPS: {success_qps:.2f} req/s")
            if model_qps is None:
                print("   模型QPS: N/A (响应未返回 completion_tokens，不能准确计算)")
                print(f"   ✅ 模型QPS ≥ {MODEL_THROUGHPUT_SLA_TOK_PER_SEC:.2f} tok/s: N/A")
            else:
                print(f"   模型QPS: {model_qps:.2f} tok/s (usage覆盖率 {token_coverage:.1f}%)")
                print(f"   ✅ 模型QPS ≥ {MODEL_THROUGHPUT_SLA_TOK_PER_SEC:.2f} tok/s: {'通过' if model_qps >= MODEL_THROUGHPUT_SLA_TOK_PER_SEC else '失败'}")
            print(f"   总耗时: {results['total_duration']:.2f}s")

        # 系统资源
        print(f"\n💻 系统资源:")
        print(
            f"   CPU: Avg={format_pct(resources.get('cpu_avg'))}, Max={format_pct(resources.get('cpu_max'))}, P95={format_pct(resources.get('cpu_p95'))}")
        cpu_avg = resources.get('cpu_avg')
        print(f"   ✅ CPU ≤ {CPU_SLA_PCT:.0f}%: {'N/A' if cpu_avg is None else ('通过' if cpu_avg <= CPU_SLA_PCT else '失败')}")
        print(
            f"   内存: Avg={format_pct(resources.get('memory_avg'))}, Max={format_pct(resources.get('memory_max'))}, P95={format_pct(resources.get('memory_p95'))}")
        memory_avg = resources.get('memory_avg')
        print(f"   ✅ 内存 ≤ {MEMORY_SLA_PCT:.0f}%: {'N/A' if memory_avg is None else ('通过' if memory_avg <= MEMORY_SLA_PCT else '失败')}")
        if resources.get('gpu_samples', 0) > 0:
            print(
                f"   GPU: Avg={format_pct(resources.get('gpu_avg'))}, Max={format_pct(resources.get('gpu_max'))}, P95={format_pct(resources.get('gpu_p95'))}")

    def _metrics_summary(self, results: Dict[str, Any]) -> Dict[str, Any]:
        success_count = len(results.get('success', []))
        failed_count = len(results.get('failed', []))
        total = results.get('total_requests', success_count + failed_count)
        completed = results.get('completed_requests', success_count + failed_count)
        duration = results.get('total_duration', 0) or 0
        completion_tokens = results.get('completion_tokens', [])
        token_coverage_pct = (len(completion_tokens) / success_count * 100) if success_count else 0
        token_coverage_fraction = token_coverage_pct / 100.0 if token_coverage_pct else 0
        model_tps = None
        model_tps_estimated = False

        if duration > 0 and completion_tokens:
            if token_coverage_pct >= 100.0:
                model_tps = sum(completion_tokens) / duration
            elif self.allow_partial_throughput_estimation and token_coverage_fraction > 0:
                model_tps = (sum(completion_tokens) / duration) / token_coverage_fraction
                model_tps_estimated = True

        return {
            'total': total,
            'completed': completed,
            'success_count': success_count,
            'failed_count': failed_count,
            'success_rate': (success_count / total * 100) if total else None,
            'e2e_p50_ms': percentile(results.get('latencies', []), 50),
            'e2e_p90_ms': percentile(results.get('latencies', []), 90),
            'e2e_p99_ms': percentile(results.get('latencies', []), 99),
            'server_p99_ms': percentile(results.get('server_latencies', []), 99),
            'first_token_p99_ms': percentile(results.get('first_token_latencies', []), 99),
            'model_tps': model_tps,
            'model_tps_estimated': model_tps_estimated,
            'request_qps': (completed / duration) if duration > 0 else None,
            'success_qps': (success_count / duration) if duration > 0 else None,
            'token_coverage_pct': token_coverage_pct,
            'duration_sec': duration,
        }

    def _format_rate(self, value: Optional[float], unit: str) -> str:
        return "N/A" if value is None else f"{value:.2f} {unit}"

    def _print_sla_concurrency_results(self, test_name: str, results: Dict, resources: Dict):
        summary = self._metrics_summary(results)
        print("\n" + "=" * 80)
        print(f"{test_name} 测试结果")
        print("=" * 80)
        print("\n请求统计:")
        print(f"   总请求: {summary['total']}")
        print(f"   完成请求: {summary['completed']}")
        print(f"   成功: {summary['success_count']}")
        print(f"   失败: {summary['failed_count']}")
        print(f"   成功率: {format_pct(summary['success_rate'])}")
        print(f"   成功率≥{SUCCESS_RATE_SLA_PCT:.0f}%: {pass_fail(summary['success_rate'], '>=', SUCCESS_RATE_SLA_PCT)}")

        print("\n延迟:")
        print(f"   端到端P50/P90/P99: {format_ms(summary['e2e_p50_ms'])} / {format_ms(summary['e2e_p90_ms'])} / {format_ms(summary['e2e_p99_ms'])}")
        print(f"   端到端P99≤{CONCURRENT_LATENCY_SLA_MS:.0f}ms: {pass_fail(summary['e2e_p99_ms'], '<=', CONCURRENT_LATENCY_SLA_MS)}")
        print(f"   服务端P99(不含网络): {format_ms(summary['server_p99_ms'])}")
        print(f"   服务端P99(不含网络)≤{CONCURRENT_LATENCY_SLA_MS:.0f}ms: {pass_fail(summary['server_p99_ms'], '<=', CONCURRENT_LATENCY_SLA_MS)}")
        if summary['server_p99_ms'] is None:
            print("   ⚠️ 服务端未返回处理耗时头，无法计算不含网络延迟，请确认网关是否支持 x-server-latency-ms 等头")
            print("   说明: 未从响应头读取到服务端耗时，无法准确计算“不含网络”的延迟。")

        print("\n吞吐量:")
        print(f"   总请求QPS: {self._format_rate(summary['request_qps'], 'req/s')}")
        print(f"   成功请求QPS: {self._format_rate(summary['success_qps'], 'req/s')}")
        print(f"   模型吞吐量: {self._format_rate(summary['model_tps'], 'tok/s')}")
        print(f"   usage覆盖率: {summary['token_coverage_pct']:.1f}%")
        print(f"   模型吞吐量≥{MODEL_THROUGHPUT_SLA_TOK_PER_SEC:.2f} tok/s: {pass_fail(summary['model_tps'], '>=', MODEL_THROUGHPUT_SLA_TOK_PER_SEC)}")
        if summary['model_tps'] is None:
            if summary['token_coverage_pct'] < 100:
                print("   说明: usage覆盖率不足100%，如需允许部分数据估算吞吐量，请启用 --allow-partial-throughput")
            else:
                print("   说明: 响应未返回 completion_tokens，不能准确计算模型 token 吞吐量。")
        elif summary.get('model_tps_estimated'):
            print(f"   说明: 吞吐量为部分usage估算值，coverage={summary['token_coverage_pct']:.1f}%")
        print(f"   总耗时: {summary['duration_sec']:.2f}s")

        cpu_avg = resources.get('cpu_avg')
        memory_avg = resources.get('memory_avg')
        print("\n系统资源:")
        print(f"   CPU: Avg={format_pct(cpu_avg)}, Max={format_pct(resources.get('cpu_max'))}, P95={format_pct(resources.get('cpu_p95'))}")
        print(f"   CPU≤{CPU_SLA_PCT:.0f}%: {pass_fail(cpu_avg, '<=', CPU_SLA_PCT)}")
        print(f"   内存: Avg={format_pct(memory_avg)}, Max={format_pct(resources.get('memory_max'))}, P95={format_pct(resources.get('memory_p95'))}")
        print(f"   内存≤{MEMORY_SLA_PCT:.0f}%: {pass_fail(memory_avg, '<=', MEMORY_SLA_PCT)}")
        if resources.get('gpu_samples', 0) > 0:
            print(f"   GPU: Avg={format_pct(resources.get('gpu_avg'))}, Max={format_pct(resources.get('gpu_max'))}, P95={format_pct(resources.get('gpu_p95'))}")

    def run_all_tests(self):
        """运行所有测试"""
        print("\n" + "=" * 40)
        print("全指标压力测试开始")
        print(f"配置: 普通并发={self.concurrent_low}, 32K并发={self.concurrent_high}, 请求数={self.total_requests}")
        print("=" * 40)

        all_results = {}

        # 1. 预热
        self.warmup()

        # 2. 单并发1024 tokens测试
        print("\n" + "=" * 80)
        print("第一阶段: 单并发 1024 tokens 测试")
        print("=" * 80)
        all_results['single_1024'] = self.test_single_concurrent_1024()

        # 3. 高并发测试
        print("\n" + "=" * 80)
        print(f"第二阶段: {self.concurrent_low}并发 {self.total_requests}请求 测试")
        print("=" * 80)
        all_results['concurrent_test'] = self.test_target_100_concurrency()

        print("\n" + "=" * 80)
        print(f"目标并发容量: {CAPACITY_CONCURRENCY}并发 {CAPACITY_REQUESTS}请求 成功率测试")
        print("=" * 80)
        all_results['capacity_160'] = self.test_capacity_160_concurrency()

        # 4. 高并发 32K测试
        print("\n" + "=" * 80)
        print(f"第三阶段: {self.concurrent_high}并发 32K上下文 {self.total_requests}请求 测试")
        print("=" * 80)
        all_results['concurrent_32k'] = self.test_high_concurrency_32k()

        # 5. 生成总结报告
        self._generate_summary_report(all_results)

        return all_results

    def _append_report_row(self, report: list, metric: str, value: str, status: str):
        report.append({'指标': metric, '实际值': value, '是否通过': status})

    def _generate_target_summary_report(self, all_results: Dict):
        print("\n" + "=" * 80)
        print("全指标测试总结报告")
        print("=" * 80)

        report = []

        single = all_results.get('single_1024', {})
        input_tokens = single.get('input_tokens')
        self._append_report_row(
            report,
            "单并发1024输入实际token数",
            "N/A" if input_tokens is None else f"{input_tokens} tokens",
            "参考" if input_tokens is not None else "N/A"
        )

        streaming_single = [item for item in single.get('streaming', []) if item.get('success')]
        first_token_p99 = percentile(
            [item.get('first_token_ms') for item in streaming_single if item.get('first_token_ms') is not None],
            99
        )
        self._append_report_row(
            report,
            f"单并发{SINGLE_1024_REQUESTS}请求1024输入流式首Token P99 ≤ {STREAM_FIRST_TOKEN_SLA_MS:.0f}ms",
            format_ms(first_token_p99),
            pass_fail_mark(first_token_p99, "<=", STREAM_FIRST_TOKEN_SLA_MS)
        )

        non_streaming_single = [item for item in single.get('non_streaming', []) if item.get('success')]
        non_stream_total_p99 = percentile(
            [item.get('total_time_ms') for item in non_streaming_single if item.get('total_time_ms') is not None],
            99
        )
        self._append_report_row(
            report,
            f"单并发{SINGLE_1024_REQUESTS}请求1024输入非流式总响应P99 ≤ {NON_STREAM_TOTAL_SLA_MS:.0f}ms",
            format_ms(non_stream_total_p99),
            pass_fail_mark(non_stream_total_p99, "<=", NON_STREAM_TOTAL_SLA_MS)
        )

        concurrent = all_results.get('concurrent_test', {})
        for mode in ('streaming', 'non_streaming'):
            data = concurrent.get(mode)
            if not data:
                continue
            metrics = data['metrics']
            resources = data['resources']
            summary = self._metrics_summary(metrics)
            mode_label = "流式" if mode == "streaming" else "非流式"

            self._append_report_row(
                report,
                f"{CONCURRENT_LATENCY}并发{CONCURRENT_LATENCY_REQUESTS}请求{mode_label}成功率 ≥ {SUCCESS_RATE_SLA_PCT:.0f}%",
                format_pct(summary['success_rate']),
                pass_fail_mark(summary['success_rate'], ">=", SUCCESS_RATE_SLA_PCT)
            )
            self._append_report_row(
                report,
                f"{CONCURRENT_LATENCY}并发{CONCURRENT_LATENCY_REQUESTS}请求{mode_label}服务端P99(不含网络) ≤ {CONCURRENT_LATENCY_SLA_MS:.0f}ms",
                format_ms(summary['server_p99_ms']),
                pass_fail_mark(summary['server_p99_ms'], "<=", CONCURRENT_LATENCY_SLA_MS)
            )
            self._append_report_row(
                report,
                f"{CONCURRENT_LATENCY}并发{CONCURRENT_LATENCY_REQUESTS}请求{mode_label}端到端P99参考",
                format_ms(summary['e2e_p99_ms']),
                "参考"
            )
            self._append_report_row(
                report,
                f"{CONCURRENT_LATENCY}并发{CONCURRENT_LATENCY_REQUESTS}请求{mode_label}模型吞吐量 ≥ {MODEL_THROUGHPUT_SLA_TOK_PER_SEC:.2f} tok/s",
                (
                    f"{self._format_rate(summary['model_tps'], 'tok/s')} (估算)"
                    if summary.get('model_tps_estimated')
                    else self._format_rate(summary['model_tps'], "tok/s")
                ),
                pass_fail_mark(summary['model_tps'], ">=", MODEL_THROUGHPUT_SLA_TOK_PER_SEC)
            )
            self._append_report_row(
                report,
                f"{CONCURRENT_LATENCY}并发{CONCURRENT_LATENCY_REQUESTS}请求{mode_label}usage覆盖率",
                format_pct(summary['token_coverage_pct']),
                "参考"
            )
            self._append_report_row(
                report,
                f"{CONCURRENT_LATENCY}并发{CONCURRENT_LATENCY_REQUESTS}请求{mode_label}CPU ≤ {CPU_SLA_PCT:.0f}%",
                format_pct(resources.get("cpu_avg")),
                pass_fail_mark(resources.get("cpu_avg"), "<=", CPU_SLA_PCT)
            )
            self._append_report_row(
                report,
                f"{CONCURRENT_LATENCY}并发{CONCURRENT_LATENCY_REQUESTS}请求{mode_label}内存 ≤ {MEMORY_SLA_PCT:.0f}%",
                format_pct(resources.get("memory_avg")),
                pass_fail_mark(resources.get("memory_avg"), "<=", MEMORY_SLA_PCT)
            )

        capacity = all_results.get('capacity_160', {}).get('streaming')
        if capacity:
            summary = self._metrics_summary(capacity['metrics'])
            self._append_report_row(
                report,
                f"目标模型并发≥{CAPACITY_CONCURRENCY}，{CAPACITY_REQUESTS}请求成功率 ≥ {SUCCESS_RATE_SLA_PCT:.0f}%",
                format_pct(summary['success_rate']),
                pass_fail_mark(summary['success_rate'], ">=", SUCCESS_RATE_SLA_PCT)
            )

        context_32k = all_results.get('concurrent_32k', {})
        input_32k_tokens = context_32k.get('input_tokens')
        self._append_report_row(
            report,
            f"输入上下文token长度 ≤ {MAX_32K_TOKENS}",
            "N/A" if input_32k_tokens is None else f"{input_32k_tokens} tokens",
            pass_fail_mark(input_32k_tokens, "<=", MAX_32K_TOKENS) if input_32k_tokens is not None else "N/A"
        )
        if context_32k.get('streaming'):
            summary = self._metrics_summary(context_32k['streaming'])
            self._append_report_row(
                report,
                f"{self.concurrent_high}并发32K上下文{self.total_requests}请求成功率 ≥ {SUCCESS_RATE_SLA_PCT:.0f}%",
                format_pct(summary['success_rate']),
                pass_fail_mark(summary['success_rate'], ">=", SUCCESS_RATE_SLA_PCT)
            )

        print(f"\n{'指标':<70} {'实际值':<20} {'结果'}")
        print("-" * 110)
        for item in report:
            print(f"{item['指标']:<70} {item['实际值']:<20} {item['是否通过']}")

        filename = f"test_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=['指标', '实际值', '是否通过'])
            writer.writeheader()
            writer.writerows(report)

        print(f"\n报告已保存: {filename}")

    def _generate_summary_report(self, all_results: Dict):
        return self._generate_target_summary_report(all_results)


def main():
    parser = argparse.ArgumentParser(description='AI Gateway 全指标压力测试 V3.2')
    parser.add_argument('--url', default='https://192.168.0.213:18300/ai-inference-gateway/predict')
    parser.add_argument('--app-key', default='1000401100004')
    parser.add_argument('--secret-key', default='c560cdb7d37240fab373d9f8a536a146')
    parser.add_argument('--component-code', default='04350559')
    parser.add_argument('--model', default='Qwen3-14B')

    # 并发和请求数配置
    parser.add_argument('--concurrent-low', type=int, default=CONCURRENT_LATENCY,
                        help=f'普通场景并发数 (默认{CONCURRENT_LATENCY})')
    parser.add_argument('--concurrent-high', type=int, default=CONTEXT_32K_CONCURRENCY,
                        help=f'32K场景并发数 (默认{CONTEXT_32K_CONCURRENCY})')
    parser.add_argument('--total-requests', type=int, default=CONTEXT_32K_REQUESTS,
                        help=f'32K场景请求数 (默认{CONTEXT_32K_REQUESTS})')
    parser.add_argument('--server-latency-header', default=None,
                        help='服务端耗时响应头名称，用于准确计算不含网络延迟；不传则自动识别常见header')
    parser.add_argument('--allow-partial-throughput', action='store_true',
                        help='usage覆盖率不足100%%时，允许使用部分数据估算模型吞吐量，并在报告中注明覆盖率')

    # 测试选择
    parser.add_argument('--all', action='store_true', help='运行所有测试')
    parser.add_argument('--test-1024', action='store_true', help='仅测试1024 tokens单并发')
    parser.add_argument('--test-concurrent', action='store_true', help='仅测试高并发')
    parser.add_argument('--test-capacity', action='store_true',
                        help=f'仅测试{CAPACITY_CONCURRENCY}并发{CAPACITY_REQUESTS}请求成功率')
    parser.add_argument('--test-32k', action='store_true', help='仅测试32K上下文')
    parser.add_argument('--dry-run', action='store_true', help='仅执行少量请求，预检连通性、usage返回和服务端延迟头')

    args = parser.parse_args()

    requests.packages.urllib3.disable_warnings()

    tester = FullMetricsTester(
        url=args.url,
        app_key=args.app_key,
        secret_key=args.secret_key,
        component_code=args.component_code,
        model=args.model,
        concurrent_low=args.concurrent_low,
        concurrent_high=args.concurrent_high,
        total_requests=args.total_requests,
        server_latency_header=args.server_latency_header,
        allow_partial_throughput_estimation=args.allow_partial_throughput
    )

    if args.dry_run:
        tester.dry_run_check()
    elif args.test_1024:
        tester.warmup()
        tester.test_single_concurrent_1024()
    elif args.test_concurrent:
        tester.warmup()
        tester.test_target_100_concurrency()
    elif args.test_capacity:
        tester.warmup()
        tester.test_capacity_160_concurrency()
    elif args.test_32k:
        tester.warmup()
        tester.test_high_concurrency_32k()
    else:
        # 默认运行所有测试
        tester.run_all_tests()


if __name__ == "__main__":
    main()
