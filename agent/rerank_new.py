# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
bge-reranker-v2-m3 直连接口压测与 SLA 验证脚本。

默认接口不走网关：
    http://36.111.82.20:10027/rerank

统计口径说明：
1. P50/P90/P95/P99 使用 nearest-rank 算法，1000 个样本的 P99 是排序后第 990 个样本。
2. 请求 QPS = 成功请求数 / 场景总耗时，失败请求不计入吞吐。
3. rerank 模型不是生成式接口，没有真实 token 输出；本脚本额外统计 query-document pair/s 作为文档对吞吐参考。
4. “不含网络的服务端延迟”只有接口返回服务端耗时 header 时才能精确计算；否则控制台会明确使用端到端耗时 E2E 作为保守参考。
5. CPU/内存通过 psutil 采样当前运行脚本的机器；要验证服务器资源，请在模型服务器侧运行脚本或保证采样环境就是服务器。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import random
import statistics
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from requests.adapters import HTTPAdapter


# ==================== 默认配置与 SLA 阈值 ====================

# 新 API 默认不走网关，直接请求 /rerank。
DEFAULT_URL = "http://36.111.82.20:10027/rerank"
DEFAULT_MODEL = "bge-reranker-v2-m3"
DEFAULT_TOP_N = 3

# 验收目标：如需求变更，只改这里即可。
SLA_SUCCESS_RATE_PCT = 90.0
SLA_P99_LATENCY_MS = 7000.0
SLA_SINGLE_RESPONSE_MS = 7000.0
SLA_THROUGHPUT_QPS = 4.40
SLA_CPU_MAX_PCT = 65.0
SLA_MEMORY_MAX_PCT = 70.0
SLA_MIN_CONCURRENCY_10 = 10
SLA_MIN_CONCURRENCY_20 = 20
SLA_DEFAULT_TOTAL_REQUESTS = 1000

# 常见服务端耗时响应头；如果接口没有返回这些 header，只能统计端到端 E2E 延迟。
COMMON_SERVER_LATENCY_HEADERS = (
    "x-server-latency-ms",
    "x-process-time-ms",
    "x-processing-time-ms",
    "x-request-duration-ms",
    "server-timing",
)


@dataclass(frozen=True)
class RequestSample:
    """单次请求明细，所有汇总指标都从这里计算。"""

    request_id: int
    status_code: int
    ok: bool
    e2e_ms: float
    ttfb_ms: Optional[float]
    server_latency_ms: Optional[float]
    document_count: int
    result_count: int
    error_type: str = ""
    error_detail: str = ""


@dataclass(frozen=True)
class ResourceSnapshot:
    """压测期间采集到的 CPU/内存峰值。"""

    cpu_max_pct: Optional[float]
    memory_max_pct: Optional[float]


@dataclass(frozen=True)
class ScenarioResult:
    """一个压测场景的汇总结果。"""

    name: str
    concurrency: int
    total_requests: int
    success_requests: int
    failed_requests: int
    success_rate_pct: float
    duration_s: float
    request_qps: float
    pair_qps: float
    document_pairs: int
    e2e_avg_ms: Optional[float]
    e2e_p50_ms: Optional[float]
    e2e_p90_ms: Optional[float]
    e2e_p95_ms: Optional[float]
    e2e_p99_ms: Optional[float]
    e2e_max_ms: Optional[float]
    ttfb_p99_ms: Optional[float]
    server_latency_p99_ms: Optional[float]
    cpu_max_pct: Optional[float]
    memory_max_pct: Optional[float]
    errors: Dict[str, int]


class ResourceSampler:
    """后台采样资源；没有安装 psutil 时自动跳过，指标显示为 N/A。"""

    def __init__(self, interval_s: float = 0.5) -> None:
        self.interval_s = interval_s
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cpu_values: List[float] = []
        self._memory_values: List[float] = []
        self._psutil: Any = None

    def start(self) -> None:
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
        while not self._stop_event.wait(self.interval_s):
            self._cpu_values.append(float(self._psutil.cpu_percent(interval=None)))
            self._memory_values.append(float(self._psutil.virtual_memory().percent))


class BGERerankerTester:
    """封装 rerank 请求、并发压测和结果统计。"""

    def __init__(
        self,
        url: str,
        model: str,
        top_n: int,
        timeout_s: float,
        min_documents: int,
        max_documents: int,
        document_field: str,
        server_latency_header: Optional[str],
        enable_gateway_auth: bool,
        app_key: str,
        secret_key: str,
        component_code: str,
        seed: int,
    ) -> None:
        self.url = url
        self.model = model
        self.top_n = top_n
        self.timeout_s = timeout_s
        self.min_documents = min_documents
        self.max_documents = max_documents
        self.document_field = document_field
        self.server_latency_header = server_latency_header
        self.enable_gateway_auth = enable_gateway_auth
        self.app_key = app_key
        self.secret_key = secret_key
        self.component_code = component_code
        self.seed = seed
        # 每个线程独立复用自己的 Session，避免并发下频繁建连影响统计。
        self.local = threading.local()
        self.test_data = self._prepare_test_data()

    def _session(self) -> requests.Session:
        session = getattr(self.local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=256, pool_maxsize=256, max_retries=0)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self.local.session = session
        return session

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if not self.enable_gateway_auth:
            return headers

        # 兼容旧网关参考脚本：默认新接口不会启用这段鉴权。
        curl_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        date_str = f"x-date: {curl_date}"
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            date_str.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature_base64 = base64.b64encode(signature).decode("utf-8")
        headers.update(
            {
                "x-date": curl_date,
                "Authorization": (
                    f'hmac username="{self.app_key}", algorithm="hmac-sha256", '
                    f'headers="x-date", signature="{signature_base64}"'
                ),
            }
        )
        return headers

    @staticmethod
    def _prepare_test_data() -> List[Dict[str, Any]]:
        """准备覆盖多主题的测试数据，避免 1000 请求完全重复。"""

        return [
            {
                "query": "机器学习的应用领域",
                "texts": [
                    "机器学习在图像识别领域有广泛应用",
                    "自然语言处理是机器学习的另一个重要应用",
                    "天气预报主要依赖于气象卫星数据",
                    "推荐系统使用机器学习算法为用户推荐商品",
                    "机器学习在医疗诊断中也有重要应用",
                    "自动驾驶技术依赖机器学习算法",
                    "金融风控系统使用机器学习进行欺诈检测",
                    "语音识别是机器学习的重要应用场景",
                    "机器学习可以帮助工业设备做预测性维护",
                    "搜索排序系统常用机器学习提升相关性",
                ],
            },
            {
                "query": "人工智能的发展历史",
                "texts": [
                    "图灵测试是人工智能领域的早期概念",
                    "达特茅斯会议标志着人工智能学科的诞生",
                    "专家系统在20世纪80年代取得显著进展",
                    "深度学习在21世纪带来人工智能的复兴",
                    "AlphaGo战胜李世石是人工智能的里程碑事件",
                    "大语言模型开启了人工智能新阶段",
                    "符号主义和连接主义长期影响人工智能研究路线",
                    "算力提升推动了神经网络模型规模增长",
                ],
            },
            {
                "query": "中国的传统文化",
                "texts": [
                    "春节是中国最重要的传统节日",
                    "儒家思想对中国文化影响深远",
                    "京剧被誉为中国的国粹",
                    "中国书法是独特的艺术形式",
                    "四大名著是中国文学的瑰宝",
                    "中医是中华民族的传统医学",
                    "二十四节气体现了古人对自然规律的观察",
                    "传统建筑讲究中轴对称和礼制秩序",
                ],
            },
            {
                "query": "环境保护的重要性",
                "texts": [
                    "气候变化威胁人类生存环境",
                    "减少碳排放是应对气候变暖的关键",
                    "垃圾分类有助于资源回收利用",
                    "保护生物多样性维持生态平衡",
                    "可再生能源的发展减少环境污染",
                    "森林保护对地球生态至关重要",
                    "节约用水可以缓解区域水资源压力",
                    "绿色交通有助于降低城市污染",
                ],
            },
            {
                "query": "Python编程语言的特点",
                "texts": [
                    "Python语法简洁易学",
                    "Python拥有丰富的第三方库",
                    "Python是数据科学的首选语言之一",
                    "Python支持面向对象编程",
                    "Python具有跨平台特性",
                    "Python是人工智能开发的主流语言",
                    "Python生态提供了大量 Web 开发框架",
                    "Python脚本适合快速自动化办公任务",
                ],
            },
            {
                "query": "深度学习的核心技术",
                "texts": [
                    "神经网络是深度学习的基础架构",
                    "反向传播算法用于训练神经网络",
                    "卷积神经网络擅长处理图像数据",
                    "循环神经网络适合处理序列数据",
                    "注意力机制提升了模型性能",
                    "Transformer架构推动了自然语言处理发展",
                    "正则化技术可以缓解模型过拟合",
                    "大规模预训练提升了模型泛化能力",
                ],
            },
            {
                "query": "新能源汽车的优势",
                "texts": [
                    "电动汽车减少尾气排放",
                    "新能源汽车降低能源消耗",
                    "电动车运行成本低于燃油车",
                    "新能源汽车技术发展迅速",
                    "电动汽车加速性能优越",
                    "新能源汽车享受政策支持",
                    "充电基础设施完善提高了使用便利性",
                    "动力电池技术进步提升了续航里程",
                ],
            },
            {
                "query": "区块链技术的应用场景",
                "texts": [
                    "加密货币是区块链的典型应用",
                    "智能合约实现自动化交易",
                    "供应链管理利用区块链提升透明度",
                    "数字身份认证受益于区块链技术",
                    "区块链用于版权保护和溯源",
                    "去中心化金融是区块链的重要领域",
                    "区块链账本具有难以篡改的特点",
                    "跨机构数据协作可以借助区块链增强信任",
                ],
            },
        ]

    def build_payload(self, request_id: int) -> Tuple[Dict[str, Any], int]:
        # 用 request_id 加随机种子控制样本选择，保证并发下仍有可复现实验数据。
        request_random = random.Random(self.seed + request_id)
        test_case = request_random.choice(self.test_data)
        texts = test_case["texts"]
        max_count = min(self.max_documents, len(texts))
        min_count = min(self.min_documents, max_count)
        document_count = request_random.randint(min_count, max_count)
        selected_texts = request_random.sample(texts, document_count)

        payload: Dict[str, Any] = {
            "model": self.model,
            "query": test_case["query"],
            # 不同 rerank 服务可能叫 texts 或 documents，通过 --document-field 控制。
            self.document_field: selected_texts,
            "top_n": min(self.top_n, len(selected_texts)),
        }
        if self.enable_gateway_auth:
            payload["componentCode"] = self.component_code
        return payload, document_count

    def send_request(self, request_id: int) -> RequestSample:
        """发送一次 rerank 请求，并记录 E2E、TTFB、服务端耗时等明细。"""

        payload, document_count = self.build_payload(request_id)
        start_ns = time.perf_counter_ns()
        first_byte_ns: Optional[int] = None
        status_code = 0
        response_text = ""
        server_latency_ms: Optional[float] = None

        try:
            # stream=True 用于记录第一个响应字节到达时间 TTFB。
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
                        first_byte_ns = time.perf_counter_ns()
                    chunks.append(chunk)
            end_ns = time.perf_counter_ns()
            response_text = b"".join(chunks).decode(response_decode_encoding(response), errors="replace")
        except requests.Timeout as exc:
            end_ns = time.perf_counter_ns()
            return self._failed_sample(
                request_id, status_code, start_ns, end_ns, first_byte_ns,
                server_latency_ms, document_count, "timeout", str(exc)
            )
        except requests.RequestException as exc:
            end_ns = time.perf_counter_ns()
            return self._failed_sample(
                request_id, status_code, start_ns, end_ns, first_byte_ns,
                server_latency_ms, document_count, "request_exception", str(exc)
            )

        e2e_ms = ns_to_ms(end_ns - start_ns)
        ttfb_ms = ns_to_ms(first_byte_ns - start_ns) if first_byte_ns else None

        if not 200 <= status_code < 300:
            return RequestSample(
                request_id=request_id,
                status_code=status_code,
                ok=False,
                e2e_ms=e2e_ms,
                ttfb_ms=ttfb_ms,
                server_latency_ms=server_latency_ms,
                document_count=document_count,
                result_count=0,
                error_type=f"http_{status_code}",
                error_detail=response_text[:500],
            )

        try:
            response_data = json.loads(response_text) if response_text else {}
        except json.JSONDecodeError as exc:
            return RequestSample(
                request_id=request_id,
                status_code=status_code,
                ok=False,
                e2e_ms=e2e_ms,
                ttfb_ms=ttfb_ms,
                server_latency_ms=server_latency_ms,
                document_count=document_count,
                result_count=0,
                error_type="invalid_json",
                error_detail=str(exc),
            )

        result_count = extract_result_count(response_data)
        if result_count <= 0:
            return RequestSample(
                request_id=request_id,
                status_code=status_code,
                ok=False,
                e2e_ms=e2e_ms,
                ttfb_ms=ttfb_ms,
                server_latency_ms=server_latency_ms,
                document_count=document_count,
                result_count=result_count,
                error_type="empty_or_unrecognized_result",
                error_detail=response_text[:500],
            )

        return RequestSample(
            request_id=request_id,
            status_code=status_code,
            ok=True,
            e2e_ms=e2e_ms,
            ttfb_ms=ttfb_ms,
            server_latency_ms=server_latency_ms,
            document_count=document_count,
            result_count=result_count,
        )

    def _failed_sample(
        self,
        request_id: int,
        status_code: int,
        start_ns: int,
        end_ns: int,
        first_byte_ns: Optional[int],
        server_latency_ms: Optional[float],
        document_count: int,
        error_type: str,
        error_detail: str,
    ) -> RequestSample:
        return RequestSample(
            request_id=request_id,
            status_code=status_code,
            ok=False,
            e2e_ms=ns_to_ms(end_ns - start_ns),
            ttfb_ms=ns_to_ms(first_byte_ns - start_ns) if first_byte_ns else None,
            server_latency_ms=server_latency_ms,
            document_count=document_count,
            result_count=0,
            error_type=error_type,
            error_detail=error_detail[:500],
        )

    def _extract_server_latency_ms(self, headers: requests.structures.CaseInsensitiveDict) -> Optional[float]:
        # 优先使用命令行指定的 header；没指定时尝试常见服务端耗时 header。
        if self.server_latency_header:
            return parse_latency_header_value(headers.get(self.server_latency_header))
        for header_name in COMMON_SERVER_LATENCY_HEADERS:
            parsed = parse_latency_header_value(headers.get(header_name))
            if parsed is not None:
                return parsed
        return None

    def warmup(self, count: int) -> None:
        # 预热不计入统计，减少首次建连和服务冷启动对正式结果的影响。
        if count <= 0:
            return
        print(f"\n预热请求: {count} 次")
        for index in range(count):
            sample = self.send_request(-index - 1)
            print(
                f"  warmup {index + 1}/{count}: status={sample.status_code}, "
                f"ok={sample.ok}, e2e={sample.e2e_ms:.2f} ms"
            )

    def run_scenario(self, name: str, total_requests: int, concurrency: int, progress_every: int) -> ScenarioResult:
        if total_requests <= 0:
            raise ValueError("total_requests must be > 0")
        if concurrency <= 0:
            raise ValueError("concurrency must be > 0")

        print("\n" + "=" * 96)
        print(f"场景: {name}")
        print(f"总请求数={total_requests}, 并发数={concurrency}, top_n={self.top_n}, timeout={self.timeout_s}s")
        print("=" * 96)

        samples: List[RequestSample] = []
        resource_sampler = ResourceSampler()
        start_ns = time.perf_counter_ns()
        resource_sampler.start()

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(self.send_request, request_id + 1) for request_id in range(total_requests)]
            completed = 0
            ok_count = 0
            for future in as_completed(futures):
                sample = future.result()
                samples.append(sample)
                completed += 1
                if sample.ok:
                    ok_count += 1
                if progress_every > 0 and (completed % progress_every == 0 or completed == total_requests):
                    print(f"  进度: {completed}/{total_requests}, 成功={ok_count}, 失败={completed - ok_count}")

        end_ns = time.perf_counter_ns()
        resource_snapshot = resource_sampler.stop()
        result = summarize_scenario(name, samples, ns_to_s(end_ns - start_ns), concurrency, resource_snapshot)
        print_scenario_result(result)
        return result


def summarize_scenario(
    name: str,
    samples: Sequence[RequestSample],
    duration_s: float,
    concurrency: int,
    resource_snapshot: ResourceSnapshot,
) -> ScenarioResult:
    """把单次请求明细汇总为成功率、P99、QPS、资源峰值等验收指标。"""

    total = len(samples)
    success_samples = [item for item in samples if item.ok]
    failed = total - len(success_samples)
    success_rate = len(success_samples) / total * 100.0 if total else 0.0
    e2e_values = [item.e2e_ms for item in success_samples]
    all_e2e_values = [item.e2e_ms for item in samples]
    ttfb_values = [item.ttfb_ms for item in success_samples if item.ttfb_ms is not None]
    server_latency_values = [
        item.server_latency_ms for item in success_samples if item.server_latency_ms is not None
    ]
    document_pairs = sum(item.document_count for item in success_samples)
    errors = Counter(item.error_type or "unknown" for item in samples if not item.ok)

    return ScenarioResult(
        name=name,
        concurrency=concurrency,
        total_requests=total,
        success_requests=len(success_samples),
        failed_requests=failed,
        success_rate_pct=success_rate,
        duration_s=duration_s,
        request_qps=len(success_samples) / duration_s if duration_s > 0 else 0.0,
        pair_qps=document_pairs / duration_s if duration_s > 0 else 0.0,
        document_pairs=document_pairs,
        e2e_avg_ms=statistics.fmean(e2e_values) if e2e_values else None,
        e2e_p50_ms=nearest_rank_percentile(e2e_values, 50),
        e2e_p90_ms=nearest_rank_percentile(e2e_values, 90),
        e2e_p95_ms=nearest_rank_percentile(e2e_values, 95),
        e2e_p99_ms=nearest_rank_percentile(e2e_values, 99),
        e2e_max_ms=max(all_e2e_values) if all_e2e_values else None,
        ttfb_p99_ms=nearest_rank_percentile(ttfb_values, 99),
        server_latency_p99_ms=nearest_rank_percentile(server_latency_values, 99),
        cpu_max_pct=resource_snapshot.cpu_max_pct,
        memory_max_pct=resource_snapshot.memory_max_pct,
        errors=dict(errors),
    )


def nearest_rank_percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    # nearest-rank 百分位算法，避免 numpy 默认插值导致 P99 和验收口径不一致。
    if not values:
        return None
    if percentile < 0 or percentile > 100:
        raise ValueError("percentile must be between 0 and 100")
    sorted_values = sorted(values)
    if percentile == 0:
        return sorted_values[0]
    index = math.ceil(percentile / 100.0 * len(sorted_values)) - 1
    index = min(max(index, 0), len(sorted_values) - 1)
    return sorted_values[index]


def ns_to_ms(value: int) -> float:
    return value / 1_000_000.0


def ns_to_s(value: int) -> float:
    return value / 1_000_000_000.0


def response_decode_encoding(response: requests.Response) -> str:
    content_type = response.headers.get("Content-Type", "")
    for part in content_type.split(";"):
        key_value = part.strip().split("=", 1)
        if len(key_value) == 2 and key_value[0].strip().lower() == "charset":
            charset = key_value[1].strip().strip('"')
            if charset:
                return charset
    return response.encoding or "utf-8"


def parse_latency_header_value(value: Optional[str]) -> Optional[float]:
    """解析服务端耗时 header，支持 12.3、12.3ms、0.012s、server-timing dur=12.3。"""

    if not value:
        return None

    raw = value.strip()
    lower = raw.lower()
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


def extract_result_count(response_data: Any) -> int:
    """兼容常见 rerank 返回结构，提取结果条数用于判断接口是否成功返回排序结果。"""

    if isinstance(response_data, dict):
        for key in ("results", "data", "result", "rerank_results"):
            value = response_data.get(key)
            if isinstance(value, list):
                return len(value)
            if isinstance(value, dict):
                nested = extract_result_count(value)
                if nested > 0:
                    return nested
        if all(key in response_data for key in ("index", "relevance_score")):
            return 1
    if isinstance(response_data, list):
        return len(response_data)
    return 0


def format_ms(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.2f} ms"


def format_pct(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.2f}%"


def pass_fail(value: Optional[float], op: str, threshold: float) -> str:
    if value is None:
        return "N/A"
    if op == "<=":
        return "PASS" if value <= threshold else "FAIL"
    if op == ">=":
        return "PASS" if value >= threshold else "FAIL"
    raise ValueError(f"unsupported op: {op}")


def print_scenario_result(result: ScenarioResult) -> None:
    print(f"\n场景结果: {result.name}")
    print(f"  并发数                 : {result.concurrency}")
    print(f"  总请求数               : {result.total_requests}")
    print(f"  成功/失败              : {result.success_requests}/{result.failed_requests}")
    print(f"  成功率                 : {result.success_rate_pct:.4f}%")
    print(f"  总耗时                 : {result.duration_s:.3f} s")
    print(f"  成功请求 QPS           : {result.request_qps:.2f} req/s")
    print(f"  文档对吞吐             : {result.pair_qps:.2f} query-document pairs/s")
    print(f"  成功文档对数量         : {result.document_pairs}")
    print(f"  E2E 平均               : {format_ms(result.e2e_avg_ms)}")
    print(
        f"  E2E P50/P90/P95/P99    : {format_ms(result.e2e_p50_ms)} / {format_ms(result.e2e_p90_ms)} / "
        f"{format_ms(result.e2e_p95_ms)} / {format_ms(result.e2e_p99_ms)}"
    )
    print(f"  E2E 最大               : {format_ms(result.e2e_max_ms)}")
    print(f"  TTFB P99               : {format_ms(result.ttfb_p99_ms)}")
    print(f"  服务端延迟 P99         : {format_ms(result.server_latency_p99_ms)}")
    print(f"  CPU 峰值               : {format_pct(result.cpu_max_pct)}")
    print(f"  内存峰值               : {format_pct(result.memory_max_pct)}")
    if result.errors:
        print(f"  错误分布               : {result.errors}")


def print_sla_report(results: Sequence[ScenarioResult]) -> bool:
    """控制台输出需求里的全部验收指标，逐项给出 PASS/FAIL/N/A。"""

    by_name = {item.name: item for item in results}
    single = by_name.get("single_concurrency")
    conc10 = by_name.get("concurrency_10_1000_requests")
    conc20 = by_name.get("concurrency_20_1000_requests")
    all_pass = True

    print("\n" + "=" * 110)
    print("SLA 指标验证报告")
    print("=" * 110)
    print("统计口径: P99=nearest-rank; 请求 QPS=成功请求数/场景总耗时; 无服务端耗时 header 时以 E2E 延迟保守参考。")
    print(f"{'指标':<38} {'实测值':<28} {'目标':<26} 结果")
    print("-" * 110)

    def row(metric: str, measured: str, target: str, status: str) -> None:
        nonlocal all_pass
        if status == "FAIL":
            all_pass = False
        print(f"{metric:<38} {measured:<28} {target:<26} {status}")

    if single is not None:
        row(
            "单并发响应时间",
            format_ms(single.e2e_max_ms),
            f"<= {SLA_SINGLE_RESPONSE_MS:.0f} ms",
            pass_fail(single.e2e_max_ms, "<=", SLA_SINGLE_RESPONSE_MS),
        )
        row(
            "单并发成功率",
            f"{single.success_rate_pct:.4f}%",
            f">= {SLA_SUCCESS_RATE_PCT:.0f}%",
            pass_fail(single.success_rate_pct, ">=", SLA_SUCCESS_RATE_PCT),
        )

    if conc10 is not None:
        row(
            "目标模型并发 >= 10",
            f"concurrency={conc10.concurrency}",
            f">= {SLA_MIN_CONCURRENCY_10}",
            pass_fail(float(conc10.concurrency), ">=", float(SLA_MIN_CONCURRENCY_10)),
        )
        row(
            "10并发1000请求成功率",
            f"{conc10.success_requests}/{conc10.total_requests} ({conc10.success_rate_pct:.4f}%)",
            f">= {SLA_SUCCESS_RATE_PCT:.0f}%",
            pass_fail(conc10.success_rate_pct, ">=", SLA_SUCCESS_RATE_PCT),
        )

    if conc20 is not None:
        latency_value = conc20.server_latency_p99_ms if conc20.server_latency_p99_ms is not None else conc20.e2e_p99_ms
        latency_label = "Server" if conc20.server_latency_p99_ms is not None else "E2E"
        row(
            "目标模型并发 >= 20",
            f"concurrency={conc20.concurrency}",
            f">= {SLA_MIN_CONCURRENCY_20}",
            pass_fail(float(conc20.concurrency), ">=", float(SLA_MIN_CONCURRENCY_20)),
        )
        row(
            "20并发成功率",
            f"{conc20.success_requests}/{conc20.total_requests} ({conc20.success_rate_pct:.4f}%)",
            f">= {SLA_SUCCESS_RATE_PCT:.0f}%",
            pass_fail(conc20.success_rate_pct, ">=", SLA_SUCCESS_RATE_PCT),
        )
        row(
            "20并发P99延迟",
            f"{format_ms(latency_value)} {latency_label}",
            f"<= {SLA_P99_LATENCY_MS:.0f} ms",
            pass_fail(latency_value, "<=", SLA_P99_LATENCY_MS),
        )
        row(
            "20并发吞吐 QPS",
            f"{conc20.request_qps:.2f} req/s",
            f">= {SLA_THROUGHPUT_QPS:.2f} req/s",
            pass_fail(conc20.request_qps, ">=", SLA_THROUGHPUT_QPS),
        )
        row(
            "20并发文档对吞吐",
            f"{conc20.pair_qps:.2f} pairs/s",
            "参考项，rerank无输出tok/s",
            "INFO",
        )
        row(
            "服务器CPU资源使用率",
            format_pct(conc20.cpu_max_pct),
            f"<= {SLA_CPU_MAX_PCT:.0f}%",
            pass_fail(conc20.cpu_max_pct, "<=", SLA_CPU_MAX_PCT),
        )
        row(
            "服务器内存资源使用率",
            format_pct(conc20.memory_max_pct),
            f"<= {SLA_MEMORY_MAX_PCT:.0f}%",
            pass_fail(conc20.memory_max_pct, "<=", SLA_MEMORY_MAX_PCT),
        )

    print("-" * 110)
    print(f"整体结论: {'PASS' if all_pass else 'FAIL'}")
    if conc20 is not None and conc20.server_latency_p99_ms is None:
        print("说明: 接口未返回服务端耗时 header，20并发P99使用端到端 E2E 延迟，包含网络耗时。")
    if any(item.cpu_max_pct is None or item.memory_max_pct is None for item in results):
        print("说明: 未采集到 CPU/内存，通常是未安装 psutil；可执行 pip install psutil 后重跑。")
    return all_pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="bge-reranker-v2-m3 直连接口压测与 SLA 验证")
    parser.add_argument("--url", default=DEFAULT_URL, help="rerank API endpoint，新接口默认不走网关")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="模型名称")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help="返回 top_n 条重排序结果")
    parser.add_argument("--timeout", type=float, default=30.0, help="单请求超时时间，单位秒")
    parser.add_argument("--warmup", type=int, default=3, help="正式压测前预热请求数")
    parser.add_argument("--single-total", type=int, default=20, help="单并发场景请求数")
    parser.add_argument("--concurrency10-total", type=int, default=SLA_DEFAULT_TOTAL_REQUESTS, help="10并发场景总请求数")
    parser.add_argument("--concurrency20-total", type=int, default=SLA_DEFAULT_TOTAL_REQUESTS, help="20并发场景总请求数")
    parser.add_argument("--concurrency10", type=int, default=10, help="并发>=10验证场景的并发数")
    parser.add_argument("--concurrency20", type=int, default=20, help="并发>=20和吞吐验证场景的并发数")
    parser.add_argument("--min-documents", type=int, default=5, help="每个请求最少候选文本数")
    parser.add_argument("--max-documents", type=int, default=8, help="每个请求最多候选文本数")
    parser.add_argument(
        "--document-field",
        choices=("texts", "documents"),
        default="texts",
        help="请求体中文档列表字段名；如果接口按 OpenAI/Jina 风格实现可改为 documents",
    )
    parser.add_argument("--server-latency-header", default=None, help="服务端处理耗时 header 名称，单位支持 ms/s")
    parser.add_argument("--progress-every", type=int, default=100, help="每完成 N 个请求打印一次进度，0 表示不打印")
    parser.add_argument("--seed", type=int, default=20260612, help="随机种子，保证测试数据选择可复现")
    parser.add_argument("--fail-on-threshold", action="store_true", help="任一 SLA 失败时以退出码 1 结束")

    # 下面参数仅为兼容旧网关参考脚本，新直连接口默认不会使用。
    parser.add_argument("--enable-gateway-auth", action="store_true", help="兼容旧网关鉴权；新接口默认不要打开")
    parser.add_argument("--app-key", default="1000401100004", help="旧网关 app key")
    parser.add_argument("--secret-key", default="c560cdb7d37240fab373d9f8a536a146", help="旧网关 secret key")
    parser.add_argument("--component-code", default="04350571", help="旧网关 componentCode")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.min_documents <= 0 or args.max_documents <= 0:
        raise ValueError("--min-documents 和 --max-documents 必须大于 0")
    if args.min_documents > args.max_documents:
        raise ValueError("--min-documents 不能大于 --max-documents")

    tester = BGERerankerTester(
        url=args.url,
        model=args.model,
        top_n=args.top_n,
        timeout_s=args.timeout,
        min_documents=args.min_documents,
        max_documents=args.max_documents,
        document_field=args.document_field,
        server_latency_header=args.server_latency_header,
        enable_gateway_auth=args.enable_gateway_auth,
        app_key=args.app_key,
        secret_key=args.secret_key,
        component_code=args.component_code,
        seed=args.seed,
    )

    print("=" * 96)
    print("bge-reranker-v2-m3 直连接口 SLA 压测")
    print("=" * 96)
    print(f"URL                 : {args.url}")
    print(f"Model               : {args.model}")
    print(f"Gateway auth        : {'enabled' if args.enable_gateway_auth else 'disabled'}")
    print(f"Payload             : model/query/{args.document_field}/top_n")
    print(f"Percentile          : nearest-rank")
    print(f"SLA                 : success>=90%, P99<=7000ms, QPS>=4.40, CPU<=65%, memory<=70%")

    results: List[ScenarioResult] = []
    tester.warmup(args.warmup)
    results.append(
        tester.run_scenario(
            name="single_concurrency",
            total_requests=args.single_total,
            concurrency=1,
            progress_every=args.progress_every,
        )
    )
    results.append(
        tester.run_scenario(
            name="concurrency_10_1000_requests",
            total_requests=args.concurrency10_total,
            concurrency=args.concurrency10,
            progress_every=args.progress_every,
        )
    )
    results.append(
        tester.run_scenario(
            name="concurrency_20_1000_requests",
            total_requests=args.concurrency20_total,
            concurrency=args.concurrency20,
            progress_every=args.progress_every,
        )
    )

    overall_pass = print_sla_report(results)
    if args.fail_on_threshold and not overall_pass:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
