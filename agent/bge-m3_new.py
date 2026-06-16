# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
bge-m3 embeddings performance and SLA test.

Default API mode is direct /v1/embeddings access, not gateway access.
Default endpoint:
    http://36.111.82.20:10020/v1/embeddings

Metric calculation notes:
1. Percentiles use nearest-rank: sorted_values[ceil(p/100*n)-1].
   For 1000 samples, P99 is the 990th sorted sample.
2. Request QPS means successful requests per second. Embedding QPS means
   successful embedding items per second and is the same as request QPS when
   batch_size=1.
3. TTFT is not a native embeddings concept because embeddings do not stream
   generated tokens. This script measures TTFB (time to first response byte)
   and reports it as "TTFT/TTFB" for the requested SLA item.
4. "Server latency excluding network" can only be exact when the service
   returns a server latency header. If no such header exists, the script uses
   end-to-end latency as a conservative upper bound and says so in output.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from requests.adapters import HTTPAdapter


# 默认配置：不传命令行参数时，脚本会使用这些值去请求 bge-m3 接口。
DEFAULT_URL = "http://36.111.82.20:10020/v1/embeddings"
DEFAULT_MODEL = "bge-m3"
EXPECTED_DIM = 1024
DEFAULT_CONCURRENCY = 20

# SLA 阈值：后面会把实际测试结果和这些目标值做 PASS/FAIL 对比。
SLA_SUCCESS_RATE_PCT = 90.0
SLA_P99_LATENCY_MS = 3000.0
SLA_MAX_RESPONSE_MS = 5000.0
SLA_TTFT_TTFB_MS = 3000.0
SLA_REQUEST_QPS = 10.0
SLA_CPU_MAX_PCT = 65.0
SLA_MEMORY_MAX_PCT = 70.0

# 服务端如果在响应头里返回耗时，通常会使用下面这些 header 名称。
COMMON_SERVER_LATENCY_HEADERS = (
    "x-server-latency-ms",
    "x-process-time-ms",
    "x-processing-time-ms",
    "x-request-duration-ms",
    "server-timing",
)


@dataclass(frozen=True)
class Sample:
    """单次请求的结果记录，后续统计成功率、耗时、维度等指标都依赖它。"""

    request_id: int
    batch_size: int
    status_code: int
    ok: bool
    e2e_ms: float
    ttfb_ms: Optional[float]
    server_latency_ms: Optional[float]
    embedding_count: int
    embedding_dims: Tuple[int, ...]
    error_type: str = ""
    error_detail: str = ""


@dataclass(frozen=True)
class ScenarioResult:
    """一个测试场景的汇总结果，例如延迟测试、吞吐测试。"""

    name: str
    concurrency: int
    batch_size: int
    input_tokens: int
    total_requests: int
    success_requests: int
    failed_requests: int
    success_rate_pct: float
    error_rate_pct: float
    duration_s: float
    request_qps: float
    embedding_qps: float
    embedding_items: int
    e2e_avg_ms: Optional[float]
    e2e_p50_ms: Optional[float]
    e2e_p90_ms: Optional[float]
    e2e_p95_ms: Optional[float]
    e2e_p99_ms: Optional[float]
    e2e_max_ms: Optional[float]
    ttfb_p99_ms: Optional[float]
    server_latency_p99_ms: Optional[float]
    all_dims_ok: bool
    min_dim: Optional[int]
    max_dim: Optional[int]
    resource_cpu_max_pct: Optional[float]
    resource_memory_max_pct: Optional[float]
    errors: Dict[str, int]


@dataclass(frozen=True)
class ResourceSnapshot:
    """测试期间采集到的机器资源峰值。"""

    cpu_max_pct: Optional[float]
    memory_max_pct: Optional[float]


class ResourceSampler:
    """后台采样 CPU 和内存；如果没有安装 psutil，就自动跳过资源采集。"""

    def __init__(self, interval_s: float = 0.5) -> None:
        self.interval_s = interval_s
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cpu_values: List[float] = []
        self._memory_values: List[float] = []
        self._psutil: Any = None

    def start(self) -> None:
        # psutil 是可选依赖，没装时不影响接口压测，只是 CPU/内存显示为 N/A。
        try:
            import psutil  # type: ignore
        except ImportError:
            return

        self._psutil = psutil
        psutil.cpu_percent(interval=None)
        self._thread = threading.Thread(target=self._run, name="resource-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> ResourceSnapshot:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s * 2)
        return ResourceSnapshot(
            cpu_max_pct=max(self._cpu_values) if self._cpu_values else None,
            memory_max_pct=max(self._memory_values) if self._memory_values else None,
        )

    def _run(self) -> None:
        # 单独线程循环采样，直到 stop() 设置停止信号。
        while not self._stop_event.wait(self.interval_s):
            self._cpu_values.append(float(self._psutil.cpu_percent(interval=None)))
            self._memory_values.append(float(self._psutil.virtual_memory().percent))


class BgeM3EmbeddingTester:
    """封装 bge-m3 embedding 接口请求、压测场景和基础校验。"""

    def __init__(
        self,
        url: str,
        model: str,
        expected_dim: int,
        timeout_s: float,
        api_key: Optional[str] = None,
        server_latency_header: Optional[str] = None,
        tokenizer_name: Optional[str] = None,
        seed: int = 20260612,
    ) -> None:
        self.url = url
        self.model = model
        self.expected_dim = expected_dim
        self.timeout_s = timeout_s
        self.api_key = api_key
        self.server_latency_header = server_latency_header
        self.random = random.Random(seed)
        # threading.local() 让每个压测线程各用自己的 HTTP Session，避免线程之间互相影响。
        self.local = threading.local()
        self.tokenizer_name = tokenizer_name
        self.tokenizer = self._load_tokenizer(tokenizer_name)
        self.text_pool = self._build_text_pool()

    @staticmethod
    def _load_tokenizer(tokenizer_name: Optional[str]) -> Any:
        # 不传 tokenizer 时，用简单英文单词近似 token 数；传了才会精确按 tokenizer 截断。
        if not tokenizer_name:
            return None
        try:
            from transformers import AutoTokenizer  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "--tokenizer-name requires the transformers package. "
                "Install it or omit --tokenizer-name to use approximate token input."
            ) from exc
        return AutoTokenizer.from_pretrained(tokenizer_name)

    def _session(self) -> requests.Session:
        # requests.Session 会复用连接，比每次重新建连接更适合并发压测。
        session = getattr(self.local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=256, pool_maxsize=256, max_retries=0)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self.local.session = session
        return session

    def _headers(self) -> Dict[str, str]:
        # OpenAI 风格接口常用 JSON 请求；如果传了 api_key，就加 Bearer 鉴权头。
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _make_128_token_text(index: int) -> str:
        # 构造稳定的 128 个英文单词，方便多次测试结果更可比。
        words = [
            "power", "grid", "inspection", "automation", "quality", "latency",
            "embedding", "retrieval", "vector", "semantic", "benchmark",
            "stability", "throughput", "request", "response", "dimension",
        ]
        tokens = [words[(index + i) % len(words)] for i in range(128)]
        return " ".join(tokens)

    def _make_token_text(self, index: int, token_count: int) -> str:
        # 根据 input_tokens 参数生成指定长度的输入文本。
        if token_count <= 0:
            raise ValueError("token_count must be > 0")

        if self.tokenizer is None:
            if token_count == 128:
                return self._make_128_token_text(index)
            words = [
                "power", "grid", "inspection", "automation", "quality", "latency",
                "embedding", "retrieval", "vector", "semantic", "benchmark",
                "stability", "throughput", "request", "response", "dimension",
            ]
            return " ".join(words[(index + i) % len(words)] for i in range(token_count))

        base = (
            f"测试样本{index}: 自动化测试关注模型接口的向量维度、响应延迟、"
            "首字节耗时、端到端耗时、吞吐量和错误率。"
        )
        text = base
        token_ids = self.tokenizer.encode(text, add_special_tokens=False)
        while len(token_ids) < token_count:
            text = f"{text} {base}"
            token_ids = self.tokenizer.encode(text, add_special_tokens=False)
        return self.tokenizer.decode(token_ids[:token_count], skip_special_tokens=True)

    @staticmethod
    def _make_long_text(index: int, target_chars: int = 1200) -> str:
        base = (
            "自动化测试需要覆盖功能正确性、性能稳定性、并发吞吐量、异常处理和结果一致性。"
            "向量模型常用于知识库检索、语义匹配、文本聚类和推荐召回。"
            "本段文本用于构造稳定的嵌入测试输入，避免请求内容过短导致测试结果缺乏代表性。"
            "测试过程应记录请求耗时、状态码、返回结构、向量维度、错误类型和吞吐量。"
        )
        prefix = f"测试样本{index:04d}: "
        text = prefix + base
        while len(text) < target_chars:
            text += base
        return text[:target_chars]

    def _build_text_pool(self) -> List[str]:
        # 预先准备一批短文本和长文本，input_tokens<=0 时会从这里循环取样。
        pool = [self._make_128_token_text(i) for i in range(64)]
        pool.extend(self._make_long_text(i) for i in range(64))
        return pool

    def _inputs(self, batch_size: int, input_tokens: int, request_id: int) -> List[str]:
        # batch_size 表示一次请求里放几条文本；接口会返回同样数量的向量。
        if input_tokens > 0:
            return [self._make_token_text(request_id + i, input_tokens) for i in range(batch_size)]
        return [self.text_pool[(request_id + i) % len(self.text_pool)] for i in range(batch_size)]

    def send_embedding_request(
        self,
        request_id: int,
        batch_size: int,
        input_tokens: int = 128,
    ) -> Sample:
        # 这是核心函数：发送一次 embedding 请求，并把耗时、状态码、向量维度等信息整理成 Sample。
        inputs = self._inputs(batch_size, input_tokens, request_id)
        payload: Dict[str, Any] = {
            "model": self.model,
            "input": inputs[0] if batch_size == 1 else inputs,
        }

        start_ns = time.perf_counter_ns()
        first_byte_ns: Optional[int] = None
        status_code = 0
        response_text = ""
        server_latency_ms: Optional[float] = None

        try:
            # stream=True 是为了能记录“第一个响应字节到达”的时间，也就是 TTFB。
            response = self._session().post(
                self.url,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout_s,
                stream=True,
            )
            status_code = response.status_code
            server_latency_ms = self._extract_server_latency_ms(response.headers)

            chunks: List[bytes] = []
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    if first_byte_ns is None:
                        # 第一次拿到响应内容的时刻，用来计算 ttfb_ms。
                        first_byte_ns = time.perf_counter_ns()
                    chunks.append(chunk)
            end_ns = time.perf_counter_ns()
            response_body = b"".join(chunks)
            response_text = response_body.decode(response_decode_encoding(response), errors="replace")
        except requests.Timeout as exc:
            end_ns = time.perf_counter_ns()
            return self._failed_sample(
                request_id, batch_size, status_code, start_ns, end_ns, first_byte_ns,
                server_latency_ms, "timeout", str(exc)
            )
        except requests.RequestException as exc:
            end_ns = time.perf_counter_ns()
            return self._failed_sample(
                request_id, batch_size, status_code, start_ns, end_ns, first_byte_ns,
                server_latency_ms, "request_exception", str(exc)
            )

        e2e_ms = ns_to_ms(end_ns - start_ns)
        ttfb_ms = ns_to_ms(first_byte_ns - start_ns) if first_byte_ns else None

        # HTTP 状态码不是 2xx，说明接口层面失败，直接记录错误。
        if not 200 <= status_code < 300:
            return Sample(
                request_id=request_id,
                batch_size=batch_size,
                status_code=status_code,
                ok=False,
                e2e_ms=e2e_ms,
                ttfb_ms=ttfb_ms,
                server_latency_ms=server_latency_ms,
                embedding_count=0,
                embedding_dims=(),
                error_type=f"http_{status_code}",
                error_detail=response_text[:500],
            )

        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as exc:
            return Sample(
                request_id=request_id,
                batch_size=batch_size,
                status_code=status_code,
                ok=False,
                e2e_ms=e2e_ms,
                ttfb_ms=ttfb_ms,
                server_latency_ms=server_latency_ms,
                embedding_count=0,
                embedding_dims=(),
                error_type="invalid_json",
                error_detail=str(exc),
            )

        embeddings = extract_embeddings(data)
        dims = tuple(len(item) for item in embeddings)
        # 校验返回的向量数量和维度是否符合预期：数量等于 batch_size，维度等于 expected_dim。
        dims_ok = bool(dims) and len(dims) == batch_size and all(dim == self.expected_dim for dim in dims)
        if not dims_ok:
            return Sample(
                request_id=request_id,
                batch_size=batch_size,
                status_code=status_code,
                ok=False,
                e2e_ms=e2e_ms,
                ttfb_ms=ttfb_ms,
                server_latency_ms=server_latency_ms,
                embedding_count=len(embeddings),
                embedding_dims=dims,
                error_type="dimension_or_count_mismatch",
                error_detail=f"expected_count={batch_size}, actual_count={len(dims)}, dims={list(dims[:10])}",
            )

        return Sample(
            request_id=request_id,
            batch_size=batch_size,
            status_code=status_code,
            ok=True,
            e2e_ms=e2e_ms,
            ttfb_ms=ttfb_ms,
            server_latency_ms=server_latency_ms,
            embedding_count=len(embeddings),
            embedding_dims=dims,
        )

    def _failed_sample(
        self,
        request_id: int,
        batch_size: int,
        status_code: int,
        start_ns: int,
        end_ns: int,
        first_byte_ns: Optional[int],
        server_latency_ms: Optional[float],
        error_type: str,
        error_detail: str,
    ) -> Sample:
        # 统一包装超时、网络异常等失败结果，避免主流程里重复写同样的字段。
        return Sample(
            request_id=request_id,
            batch_size=batch_size,
            status_code=status_code,
            ok=False,
            e2e_ms=ns_to_ms(end_ns - start_ns),
            ttfb_ms=ns_to_ms(first_byte_ns - start_ns) if first_byte_ns else None,
            server_latency_ms=server_latency_ms,
            embedding_count=0,
            embedding_dims=(),
            error_type=error_type,
            error_detail=error_detail[:500],
        )

    def _extract_server_latency_ms(self, headers: requests.structures.CaseInsensitiveDict) -> Optional[float]:
        # 优先使用用户指定的响应头；没指定时尝试几个常见的服务端耗时 header。
        if self.server_latency_header:
            return parse_latency_header_value(headers.get(self.server_latency_header))
        for header_name in COMMON_SERVER_LATENCY_HEADERS:
            value = headers.get(header_name)
            parsed = parse_latency_header_value(value)
            if parsed is not None:
                return parsed
        return None

    def warmup(self, count: int, batch_size: int, input_tokens: int) -> None:
        # 预热请求不计入正式统计，主要用于让连接池、服务缓存等先稳定下来。
        if count <= 0:
            return
        print(f"\nWarmup: {count} requests, batch_size={batch_size}")
        for i in range(count):
            sample = self.send_embedding_request(-i - 1, batch_size, input_tokens)
            print(
                f"  warmup {i + 1}/{count}: status={sample.status_code}, "
                f"ok={sample.ok}, e2e={sample.e2e_ms:.2f} ms"
            )

    def run_scenario(
        self,
        name: str,
        total_requests: int,
        concurrency: int,
        batch_size: int,
        input_tokens: int,
        progress_every: int,
    ) -> Tuple[ScenarioResult, List[Sample]]:
        # 按指定并发数发起 total_requests 个请求，并在结束后汇总成 ScenarioResult。
        if total_requests <= 0:
            raise ValueError("total_requests must be > 0")
        if concurrency <= 0:
            raise ValueError("concurrency must be > 0")
        if batch_size <= 0:
            raise ValueError("batch_size must be > 0")

        print(
            f"\nScenario: {name}\n"
            f"  total_requests={total_requests}, concurrency={concurrency}, "
            f"batch_size={batch_size}, input_tokens={input_tokens}"
        )

        samples: List[Sample] = []
        resource_sampler = ResourceSampler()
        start_ns = time.perf_counter_ns()
        resource_sampler.start()
        # ThreadPoolExecutor 用线程池实现并发请求；max_workers 就是并发数。
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(self.send_embedding_request, i + 1, batch_size, input_tokens)
                for i in range(total_requests)
            ]
            completed = 0
            for future in as_completed(futures):
                # as_completed 会在任意请求完成后立刻返回，不要求按 request_id 顺序等待。
                sample = future.result()
                samples.append(sample)
                completed += 1
                if progress_every > 0 and (completed % progress_every == 0 or completed == total_requests):
                    ok_count = sum(1 for item in samples if item.ok)
                    print(f"  progress: {completed}/{total_requests}, ok={ok_count}, fail={completed - ok_count}")
        end_ns = time.perf_counter_ns()
        resource_snapshot = resource_sampler.stop()

        # 把所有单次请求样本计算成平均耗时、P99、QPS、成功率等汇总指标。
        result = summarize_scenario(
            name=name,
            samples=samples,
            duration_s=ns_to_s(end_ns - start_ns),
            expected_dim=self.expected_dim,
            concurrency=concurrency,
            batch_size=batch_size,
            input_tokens=input_tokens,
            resource_snapshot=resource_snapshot,
        )
        print_scenario_result(result)
        return result, sorted(samples, key=lambda item: item.request_id)

    def check_dimension(self, batch_size: int = 1, input_tokens: int = 128) -> Sample:
        # 单独发一次请求，快速确认接口返回的向量维度是否为 expected_dim。
        print(f"\nDimension check: expected_dim={self.expected_dim}, batch_size={batch_size}")
        sample = self.send_embedding_request(1, batch_size, input_tokens)
        dims_preview = list(sample.embedding_dims[:10])
        print(
            f"  status={sample.status_code}, ok={sample.ok}, "
            f"embedding_count={sample.embedding_count}, dims={dims_preview}"
        )
        if not sample.ok:
            print(f"  error={sample.error_type}: {sample.error_detail}")
        return sample


def ns_to_ms(value: int) -> float:
    # perf_counter_ns 返回纳秒，这里统一换算成毫秒，方便打印和判断 SLA。
    return value / 1_000_000.0


def ns_to_s(value: int) -> float:
    # QPS 需要用“秒”作为分母。
    return value / 1_000_000_000.0


def parse_latency_header_value(value: Optional[str]) -> Optional[float]:
    # 把响应头里的耗时字符串解析成毫秒，例如 12.3ms、0.012s、dur=12.3。
    if not value:
        return None

    raw = value.strip()
    lower = raw.lower()

    # Examples:
    #   "12.3"
    #   "12.3ms"
    #   "0.012s"
    #   Server-Timing: "app;dur=12.3"
    if "dur=" in lower:
        try:
            raw = lower.split("dur=", 1)[1].split(",", 1)[0].split(";", 1)[0].strip()
        except IndexError:
            return None

    multiplier = 1.0
    if raw.endswith("ms"):
        raw = raw[:-2].strip()
    elif raw.endswith("s"):
        raw = raw[:-1].strip()
        multiplier = 1000.0

    try:
        return float(raw) * multiplier
    except ValueError:
        return None


def response_decode_encoding(response: requests.Response) -> str:
    # 优先使用 Content-Type 里的 charset，避免响应体解码时中文或错误信息乱码。
    content_type = response.headers.get("Content-Type", "")
    for part in content_type.split(";"):
        key_value = part.strip().split("=", 1)
        if len(key_value) == 2 and key_value[0].strip().lower() == "charset":
            charset = key_value[1].strip().strip('"')
            if charset:
                return charset
    return response.encoding or "utf-8"


def extract_embeddings(response_data: Any) -> List[Sequence[Any]]:
    # 兼容几种常见返回格式，最终统一提取成“向量列表”。
    if isinstance(response_data, dict):
        data = response_data.get("data")
        if isinstance(data, list):
            embeddings: List[Sequence[Any]] = []
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("embedding"), list):
                    embeddings.append(item["embedding"])
                elif isinstance(item, list):
                    embeddings.append(item)
            return embeddings

        embedding = response_data.get("embedding")
        if isinstance(embedding, list):
            return [embedding]

        embeddings_value = response_data.get("embeddings")
        if isinstance(embeddings_value, list):
            if embeddings_value and all(isinstance(item, list) for item in embeddings_value):
                return embeddings_value
            if embeddings_value and all(isinstance(item, (int, float)) for item in embeddings_value):
                return [embeddings_value]

    if isinstance(response_data, list):
        if response_data and all(isinstance(item, list) for item in response_data):
            return response_data
        if response_data and all(isinstance(item, (int, float)) for item in response_data):
            return [response_data]

    return []


def nearest_rank_percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    # nearest-rank 百分位算法：先排序，再取 ceil(p/100*n) 对应的位置。
    if not values:
        return None
    if percentile < 0 or percentile > 100:
        raise ValueError("percentile must be between 0 and 100")
    sorted_values = sorted(values)
    if percentile == 0:
        return sorted_values[0]
    index = math.ceil((percentile / 100.0) * len(sorted_values)) - 1
    index = min(max(index, 0), len(sorted_values) - 1)
    return sorted_values[index]


def summarize_scenario(
    name: str,
    samples: Sequence[Sample],
    duration_s: float,
    expected_dim: int,
    concurrency: int,
    batch_size: int,
    input_tokens: int,
    resource_snapshot: ResourceSnapshot,
) -> ScenarioResult:
    # 把一堆 Sample 明细汇总成场景指标：成功率、错误率、P50/P99、QPS、维度范围等。
    total = len(samples)
    success = sum(1 for item in samples if item.ok)
    failed = total - success
    success_rate = (success / total * 100.0) if total else 0.0
    error_rate = 100.0 - success_rate
    all_e2e_values = [item.e2e_ms for item in samples]
    e2e_values = [item.e2e_ms for item in samples if item.ok]
    ttfb_values = [item.ttfb_ms for item in samples if item.ok and item.ttfb_ms is not None]
    server_latency_values = [
        item.server_latency_ms for item in samples if item.ok and item.server_latency_ms is not None
    ]
    # embedding_items 是成功返回的向量条数；batch_size>1 时它可能大于成功请求数。
    embedding_items = sum(item.embedding_count for item in samples if item.ok)
    all_dims = [dim for item in samples for dim in item.embedding_dims]
    errors = Counter(item.error_type or "unknown" for item in samples if not item.ok)

    return ScenarioResult(
        name=name,
        concurrency=concurrency,
        batch_size=batch_size,
        input_tokens=input_tokens,
        total_requests=total,
        success_requests=success,
        failed_requests=failed,
        success_rate_pct=success_rate,
        error_rate_pct=error_rate,
        duration_s=duration_s,
        # request_qps 只按成功请求数算；失败请求不会计入吞吐。
        request_qps=success / duration_s if duration_s > 0 else 0.0,
        embedding_qps=embedding_items / duration_s if duration_s > 0 else 0.0,
        embedding_items=embedding_items,
        e2e_avg_ms=statistics.fmean(e2e_values) if e2e_values else None,
        e2e_p50_ms=nearest_rank_percentile(e2e_values, 50),
        e2e_p90_ms=nearest_rank_percentile(e2e_values, 90),
        e2e_p95_ms=nearest_rank_percentile(e2e_values, 95),
        e2e_p99_ms=nearest_rank_percentile(e2e_values, 99),
        e2e_max_ms=max(all_e2e_values) if all_e2e_values else None,
        ttfb_p99_ms=nearest_rank_percentile(ttfb_values, 99),
        server_latency_p99_ms=nearest_rank_percentile(server_latency_values, 99),
        all_dims_ok=bool(all_dims) and all(dim == expected_dim for dim in all_dims),
        min_dim=min(all_dims) if all_dims else None,
        max_dim=max(all_dims) if all_dims else None,
        resource_cpu_max_pct=resource_snapshot.cpu_max_pct,
        resource_memory_max_pct=resource_snapshot.memory_max_pct,
        errors=dict(errors),
    )


def format_ms(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.2f} ms"


def format_number(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.2f}"


def format_pct(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.2f}%"


def pass_fail(value: Optional[float], op: str, threshold: float) -> str:
    # 根据目标阈值返回 PASS/FAIL；没有采集到的数据返回 N/A。
    if value is None:
        return "N/A"
    if op == "<=":
        return "PASS" if value <= threshold else "FAIL"
    if op == ">=":
        return "PASS" if value >= threshold else "FAIL"
    raise ValueError(f"unsupported op: {op}")


def print_scenario_result(result: ScenarioResult) -> None:
    # 打印单个场景的详细结果，方便直接在控制台查看。
    print(f"\nResult: {result.name}")
    print(f"  concurrency          : {result.concurrency}")
    print(f"  batch_size           : {result.batch_size}")
    print(f"  input_tokens         : {result.input_tokens}")
    print(f"  duration_s           : {result.duration_s:.3f}")
    print(f"  requests             : {result.success_requests}/{result.total_requests} success")
    print(f"  success_rate         : {result.success_rate_pct:.4f}%")
    print(f"  error_rate           : {result.error_rate_pct:.4f}%")
    print(f"  request_qps          : {result.request_qps:.2f}")
    print(f"  embedding_item_qps   : {result.embedding_qps:.2f}")
    print(f"  embedding_items      : {result.embedding_items}")
    print(f"  e2e_avg              : {format_ms(result.e2e_avg_ms)}")
    print(f"  e2e_p50/p90/p95/p99  : {format_ms(result.e2e_p50_ms)} / {format_ms(result.e2e_p90_ms)} / "
          f"{format_ms(result.e2e_p95_ms)} / {format_ms(result.e2e_p99_ms)}")
    print(f"  e2e_max              : {format_ms(result.e2e_max_ms)}")
    print(f"  ttft_ttfb_p99        : {format_ms(result.ttfb_p99_ms)}")
    print(f"  server_latency_p99   : {format_ms(result.server_latency_p99_ms)}")
    print(f"  cpu_max              : {format_pct(result.resource_cpu_max_pct)}")
    print(f"  memory_max           : {format_pct(result.resource_memory_max_pct)}")
    print(f"  embedding_dim_ok     : {result.all_dims_ok} (min={result.min_dim}, max={result.max_dim})")
    if result.errors:
        print(f"  errors               : {result.errors}")


def print_threshold_report(
    dimension_sample: Optional[Sample],
    latency_result: Optional[ScenarioResult],
    throughput_result: Optional[ScenarioResult],
    max_batch_sample: Optional[Sample],
    expected_dim: int,
) -> bool:
    # 汇总所有关键 SLA 指标，逐项和阈值比较，并返回整体是否通过。
    print("\n" + "=" * 96)
    print("SLA THRESHOLD REPORT")
    print("=" * 96)
    print("Percentile method: nearest-rank. Request QPS: successful requests per second.")
    print("TTFT is not native to embeddings; the script reports TTFB as TTFT/TTFB.")

    all_pass = True

    def row(metric: str, measured: str, target: str, status: str) -> None:
        # 每打印一行阈值结果，如果有 FAIL，就把整体结果标记为失败。
        nonlocal all_pass
        if status == "FAIL":
            all_pass = False
        print(f"{metric:<34} {measured:<24} {target:<24} {status}")

    print(f"{'Metric':<34} {'Measured':<24} {'Target':<24} Result")
    print("-" * 96)

    if dimension_sample is not None:
        dims = list(dimension_sample.embedding_dims)
        dim_ok = (
            dimension_sample.ok
            and dimension_sample.embedding_count == dimension_sample.batch_size
            and bool(dims)
            and all(dim == expected_dim for dim in dims)
        )
        measured = f"count={dimension_sample.embedding_count}, dims={dims[:5]}"
        row("Vector dimension and count", measured, f"all == {expected_dim}", "PASS" if dim_ok else "FAIL")

    if latency_result is not None:
        server_p99 = latency_result.server_latency_p99_ms
        if server_p99 is not None:
            row(
                "20-concurrency server P99",
                format_ms(server_p99),
                f"<= {SLA_P99_LATENCY_MS:.0f} ms",
                pass_fail(server_p99, "<=", SLA_P99_LATENCY_MS),
            )
        else:
            # 没有服务端耗时 header 时，只能用端到端耗时 E2E 作为保守参考。
            status = pass_fail(latency_result.e2e_p99_ms, "<=", SLA_P99_LATENCY_MS)
            measured = f"{format_ms(latency_result.e2e_p99_ms)} E2E"
            row(
                "20-concurrency latency P99",
                measured,
                f"<= {SLA_P99_LATENCY_MS:.0f} ms, no server header",
                status,
            )

        row(
            "TTFT/TTFB P99",
            format_ms(latency_result.ttfb_p99_ms),
            f"<= {SLA_TTFT_TTFB_MS:.0f} ms, 128-token input",
            pass_fail(latency_result.ttfb_p99_ms, "<=", SLA_TTFT_TTFB_MS),
        )
        row(
            "Max response time",
            format_ms(latency_result.e2e_max_ms),
            f"<= {SLA_MAX_RESPONSE_MS:.0f} ms",
            pass_fail(latency_result.e2e_max_ms, "<=", SLA_MAX_RESPONSE_MS),
        )
        row(
            "Request success rate",
            f"{latency_result.success_rate_pct:.4f}%",
            f">= {SLA_SUCCESS_RATE_PCT:.0f}%",
            pass_fail(latency_result.success_rate_pct, ">=", SLA_SUCCESS_RATE_PCT),
        )
        row(
            "20-concurrency request QPS",
            f"{latency_result.request_qps:.2f}",
            f">= {SLA_REQUEST_QPS:.0f}",
            pass_fail(latency_result.request_qps, ">=", SLA_REQUEST_QPS),
        )
        row(
            "20-concurrency setting",
            f"concurrency={latency_result.concurrency}",
            f">= {DEFAULT_CONCURRENCY}",
            pass_fail(float(latency_result.concurrency), ">=", float(DEFAULT_CONCURRENCY)),
        )
        row(
            "CPU usage max",
            format_pct(latency_result.resource_cpu_max_pct),
            f"<= {SLA_CPU_MAX_PCT:.0f}%",
            pass_fail(latency_result.resource_cpu_max_pct, "<=", SLA_CPU_MAX_PCT),
        )
        row(
            "Memory usage max",
            format_pct(latency_result.resource_memory_max_pct),
            f"<= {SLA_MEMORY_MAX_PCT:.0f}%",
            pass_fail(latency_result.resource_memory_max_pct, "<=", SLA_MEMORY_MAX_PCT),
        )

    if throughput_result is not None:
        row(
            "Throughput request QPS",
            f"{throughput_result.request_qps:.2f}",
            f">= {SLA_REQUEST_QPS:.0f}",
            pass_fail(throughput_result.request_qps, ">=", SLA_REQUEST_QPS),
        )
        row(
            "Throughput embedding QPS",
            f"{throughput_result.embedding_qps:.2f}",
            f">= {SLA_REQUEST_QPS:.0f}",
            pass_fail(throughput_result.embedding_qps, ">=", SLA_REQUEST_QPS),
        )
        row(
            "Throughput success rate",
            f"{throughput_result.success_rate_pct:.4f}%",
            f">= {SLA_SUCCESS_RATE_PCT:.0f}%",
            pass_fail(throughput_result.success_rate_pct, ">=", SLA_SUCCESS_RATE_PCT),
        )

    if max_batch_sample is not None:
        max_batch_ok = max_batch_sample.ok and max_batch_sample.embedding_count >= 64
        row(
            "Max batch size",
            f"batch_size=64, ok={max_batch_sample.ok}",
            ">= 64, no OOM",
            "PASS" if max_batch_ok else "FAIL",
        )

    print("=" * 96)
    print(f"OVERALL: {'PASS' if all_pass else 'FAIL'}")
    return all_pass


def write_samples_csv(path: Path, samples: Iterable[Sample]) -> None:
    # 保存每一次请求的明细，后续可以用 Excel 或脚本继续分析。
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "request_id",
                "batch_size",
                "status_code",
                "ok",
                "e2e_ms",
                "ttfb_ms",
                "server_latency_ms",
                "embedding_count",
                "embedding_dims",
                "error_type",
                "error_detail",
            ],
        )
        writer.writeheader()
        for item in samples:
            writer.writerow(
                {
                    "request_id": item.request_id,
                    "batch_size": item.batch_size,
                    "status_code": item.status_code,
                    "ok": item.ok,
                    "e2e_ms": f"{item.e2e_ms:.6f}",
                    "ttfb_ms": "" if item.ttfb_ms is None else f"{item.ttfb_ms:.6f}",
                    "server_latency_ms": "" if item.server_latency_ms is None else f"{item.server_latency_ms:.6f}",
                    "embedding_count": item.embedding_count,
                    "embedding_dims": json.dumps(list(item.embedding_dims), ensure_ascii=False),
                    "error_type": item.error_type,
                    "error_detail": item.error_detail,
                }
            )


def scenario_result_to_dict(result: ScenarioResult) -> Dict[str, Any]:
    # dataclass 转成普通 dict，方便写入 JSON。
    return {
        "name": result.name,
        "concurrency": result.concurrency,
        "batch_size": result.batch_size,
        "input_tokens": result.input_tokens,
        "total_requests": result.total_requests,
        "success_requests": result.success_requests,
        "failed_requests": result.failed_requests,
        "success_rate_pct": result.success_rate_pct,
        "error_rate_pct": result.error_rate_pct,
        "duration_s": result.duration_s,
        "request_qps": result.request_qps,
        "embedding_qps": result.embedding_qps,
        "embedding_items": result.embedding_items,
        "e2e_avg_ms": result.e2e_avg_ms,
        "e2e_p50_ms": result.e2e_p50_ms,
        "e2e_p90_ms": result.e2e_p90_ms,
        "e2e_p95_ms": result.e2e_p95_ms,
        "e2e_p99_ms": result.e2e_p99_ms,
        "e2e_max_ms": result.e2e_max_ms,
        "ttfb_p99_ms": result.ttfb_p99_ms,
        "server_latency_p99_ms": result.server_latency_p99_ms,
        "all_dims_ok": result.all_dims_ok,
        "min_dim": result.min_dim,
        "max_dim": result.max_dim,
        "resource_cpu_max_pct": result.resource_cpu_max_pct,
        "resource_memory_max_pct": result.resource_memory_max_pct,
        "errors": result.errors,
    }


def write_summary_json(path: Path, results: Sequence[ScenarioResult], overall_pass: bool) -> None:
    # 保存场景级别的汇总 JSON，适合归档或给自动化流程读取。
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "overall_pass": overall_pass,
        "percentile_method": "nearest-rank",
        "qps_method": "request_qps = successful requests / measured wall-clock seconds; embedding_qps = successful embedding items / measured wall-clock seconds",
        "results": [scenario_result_to_dict(result) for result in results],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    # 命令行参数入口：可以通过 python bge-m3_new.py --mode latency 这类方式控制测试内容。
    parser = argparse.ArgumentParser(description="bge-m3 direct /v1/embeddings SLA and pressure test")
    parser.add_argument("--url", default=DEFAULT_URL, help="Embeddings API endpoint")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name")
    parser.add_argument("--api-key", default=None, help="Optional Bearer token. Direct mode usually leaves this empty.")
    parser.add_argument("--expected-dim", type=int, default=EXPECTED_DIM, help="Expected embedding dimension")
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout seconds")
    parser.add_argument("--server-latency-header", default=None, help="Header that contains server latency in ms/s")
    parser.add_argument(
        "--mode",
        choices=("full", "dimension", "latency", "throughput", "max-batch"),
        default="full",
        help="Which test to run",
    )
    parser.add_argument("--warmup", type=int, default=3, help="Warmup requests before measured scenarios")
    parser.add_argument("--latency-total", type=int, default=1000, help="Measured requests for latency P99")
    parser.add_argument("--latency-concurrency", type=int, default=DEFAULT_CONCURRENCY, help="Concurrency for latency P99 test")
    parser.add_argument("--throughput-total", type=int, default=1000, help="Measured requests for throughput test")
    parser.add_argument("--throughput-concurrency", type=int, default=DEFAULT_CONCURRENCY, help="Concurrency for throughput test")
    parser.add_argument("--throughput-batch-size", type=int, default=1, help="Batch size for throughput test")
    parser.add_argument("--max-batch-size", type=int, default=64, help="Batch size used for max batch validation")
    parser.add_argument("--input-tokens", type=int, default=128, help="Input token count approximation for SLA tests")
    parser.add_argument(
        "--tokenizer-name",
        default=None,
        help="Optional Hugging Face tokenizer name/path for exact input token count, for example BAAI/bge-m3",
    )
    parser.add_argument("--progress-every", type=int, default=100, help="Print progress every N completed requests")
    parser.add_argument("--output-dir", default="model/results", help="Directory for CSV/JSON output")
    parser.add_argument("--no-files", action="store_true", help="Do not write CSV/JSON output files")
    parser.add_argument("--fail-on-threshold", action="store_true", help="Exit with code 1 if any SLA check fails")
    return parser.parse_args()


def main() -> int:
    # 主流程：解析参数、创建测试器、按 mode 执行对应测试、输出报告和文件。
    args = parse_args()
    tester = BgeM3EmbeddingTester(
        url=args.url,
        model=args.model,
        expected_dim=args.expected_dim,
        timeout_s=args.timeout,
        api_key=args.api_key,
        server_latency_header=args.server_latency_header,
        tokenizer_name=args.tokenizer_name,
    )

    print("=" * 96)
    print("bge-m3 direct embeddings SLA test")
    print("=" * 96)
    print(f"URL                 : {args.url}")
    print(f"Model               : {args.model}")
    print(f"Expected dimension  : {args.expected_dim}")
    print("Gateway auth        : disabled")
    print("Payload             : {'model': model, 'input': text_or_text_list}")
    print("Percentile          : nearest-rank")
    print(f"Tokenizer           : {args.tokenizer_name or 'approximate word-based input'}")

    all_samples: List[Sample] = []
    scenario_results: List[ScenarioResult] = []
    dimension_sample: Optional[Sample] = None
    latency_result: Optional[ScenarioResult] = None
    throughput_result: Optional[ScenarioResult] = None
    max_batch_sample: Optional[Sample] = None

    # dimension：只检查返回向量数量和维度是否正确。
    if args.mode in ("full", "dimension"):
        dimension_sample = tester.check_dimension(batch_size=1, input_tokens=args.input_tokens)
        all_samples.append(dimension_sample)

    # latency：固定 batch_size=1，重点看并发下的 P99、最大响应时间、成功率。
    if args.mode in ("full", "latency"):
        tester.warmup(args.warmup, batch_size=1, input_tokens=args.input_tokens)
        latency_result, samples = tester.run_scenario(
            name="latency_128_tokens",
            total_requests=args.latency_total,
            concurrency=args.latency_concurrency,
            batch_size=1,
            input_tokens=args.input_tokens,
            progress_every=args.progress_every,
        )
        scenario_results.append(latency_result)
        all_samples.extend(samples)

    # throughput：重点看单位时间内成功完成多少请求/向量。
    if args.mode in ("full", "throughput"):
        tester.warmup(args.warmup, batch_size=args.throughput_batch_size, input_tokens=args.input_tokens)
        throughput_result, samples = tester.run_scenario(
            name=f"throughput_batch{args.throughput_batch_size}_concurrency{args.throughput_concurrency}",
            total_requests=args.throughput_total,
            concurrency=args.throughput_concurrency,
            batch_size=args.throughput_batch_size,
            input_tokens=args.input_tokens,
            progress_every=args.progress_every,
        )
        scenario_results.append(throughput_result)
        all_samples.extend(samples)

    # max-batch：单次请求放较大的 batch，验证接口是否能正常返回且不报 OOM。
    if args.mode in ("full", "max-batch"):
        print(f"\nMax batch check: batch_size={args.max_batch_size}")
        max_batch_sample = tester.send_embedding_request(
            request_id=1,
            batch_size=args.max_batch_size,
            input_tokens=args.input_tokens,
        )
        all_samples.append(max_batch_sample)
        print(
            f"  status={max_batch_sample.status_code}, ok={max_batch_sample.ok}, "
            f"embedding_count={max_batch_sample.embedding_count}, e2e={max_batch_sample.e2e_ms:.2f} ms"
        )
        if not max_batch_sample.ok:
            print(f"  error={max_batch_sample.error_type}: {max_batch_sample.error_detail}")

    overall_pass = print_threshold_report(
        dimension_sample=dimension_sample,
        latency_result=latency_result,
        throughput_result=throughput_result,
        max_batch_sample=max_batch_sample,
        expected_dim=args.expected_dim,
    )

    if not args.no_files:
        # 默认把明细 CSV 和汇总 JSON 写到 output_dir，文件名带时间戳避免覆盖旧结果。
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path(args.output_dir)
        csv_path = output_dir / f"bge_m3_samples_{timestamp}.csv"
        json_path = output_dir / f"bge_m3_summary_{timestamp}.json"
        write_samples_csv(csv_path, all_samples)
        write_summary_json(json_path, scenario_results, overall_pass)
        print(f"\nFiles written:")
        print(f"  samples_csv : {csv_path}")
        print(f"  summary_json: {json_path}")

    if args.fail_on_threshold and not overall_pass:
        # 开启该开关后，只要 SLA 不通过，脚本退出码就是 1，适合 CI/自动化判断失败。
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
