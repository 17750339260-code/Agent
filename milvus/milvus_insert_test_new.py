#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Milvus 1000 万数据写入压力测试（独立版）

本脚本不依赖 lightrag 项目代码，仅依赖: pymilvus、numpy、python-dotenv。
schema 字段和截断逻辑沿用 lightrag chunks 命名空间，向量维度、数据大小和
HNSW 参数则针对 1000 万数据的写入效率进行了调整。

实验目标：
    以完成 1000 万条数据写入为主，同时观察吞吐、延迟和 timeout 情况。

实验设计：
    1. 保留生产 chunks collection 的字段、索引类型和截断逻辑
    2. 随机向量代替真实 embedding（避免模型限流/超时干扰实验）
    3. 默认针对 1000 万数据进行高吞吐 upsert
    4. 每写入 100000 条输出累计进度、耗时和 ETA
    5. 检测 "message send timeout"，记录首次出现时的累计写入量
    6. 每 30 秒或每 50000 条输出窗口吞吐，可按需将批次、窗口、汇总导出 CSV

用法：
    # 直接运行：正式写入 1000 万条，不保存 CSV
    python milvus_insert_test_new.py

    # 临时覆盖默认写入量，进行小规模试跑
    python milvus_insert_test_new.py --target-count 10000 --warmup-count 0

    # 只验证连接和 schema
    python milvus_insert_test_new.py --dry-run
"""

import os
import time
import hashlib
import argparse
import ast
import csv
import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, TextIO

import numpy as np
from dotenv import load_dotenv
from pymilvus import MilvusClient, FieldSchema, CollectionSchema, DataType


# ============================================================================
# 配置常量（针对 1000 万数据快速写入）
# ============================================================================

SCRIPT_VERSION = "10m-fast-v3"
DEFAULT_WORKSPACE = "stress_test_10m_fast_20260826"  # 使用新 collection，避免旧 schema 冲突
MILVUS_URI = "http://192.168.0.225:30119"  # Milvus 2.6.21 测试集群
MILVUS_DB_NAME = "default"
MILVUS_CLIENT_TIMEOUT = 300              # 大规模写入客户端超时，单位秒
EMBEDDING_DIM = 256                      # 以灌入 1000 万条为主，降低向量存储和索引开销
DEFAULT_TARGET_COUNT = 10_000_000        # 正式统计阶段默认写入 1000 万条
DEFAULT_BATCH_SIZE = 500                 # 单批约数 MB，兼顾吞吐和 gRPC 消息大小
DEFAULT_WARMUP_COUNT = 0                 # 默认不预热，确保实际目标为 1000 万条
DEFAULT_STOP_ON_TIMEOUT = False          # 长时间灌数默认在 timeout 后继续
DEFAULT_MAX_CONSECUTIVE_FAILURES = 10    # 防止持续故障时无限重试同一批数据
CONTENT_TARGET_BYTES = 4_096             # 降低网络和存储开销，保留动态文本字段压力
REPORT_INTERVAL = 100_000                # 1000 万规模每 10 万条输出一次，共约 100 次
WINDOW_SECONDS = 30                      # 长时间压测每 30 秒输出窗口统计
WINDOW_RECORDS = 50_000                  # 或每成功写入 5 万条触发窗口统计
REPORT_SEGMENT_SIZE = 1_000_000          # 1000 万规模按每 100 万条汇总一段
DEFAULT_OUTPUT = None                    # 默认不保存 CSV，避免长期压测产生大文件

# 以写入吞吐为主，降低 HNSW 构建成本；不改变索引类型。
HNSW_M = 8
HNSW_EF_CONSTRUCTION = 64

GRAPH_FIELD_SEP = "<SEP>"                # lightrag/constants.py 快照
CHUNKS_FILE_LIMIT_BYTES = 1024           # chunks 命名空间 file_path/file_name 字节上限
META_FIELD_LIMIT_BYTES = 500             # 动态元数据字段 UTF-8 字节上限
MAX_DYNAMIC_FIELD_BYTES = 50_000         # 动态字段字节兜底上限

VARCHAR_FIELD_LIMITS = {
    "id": 64,
    "group_id": 512,
    "group_name": 512,
    "group_type": 512,
    "full_doc_id": 512,
    "file_name": 1024,
    "file_path": 65_535,
}

# 与生产 MilvusVectorDBStorage 初始化时的 meta_fields 一致
META_FIELDS = {
    "full_doc_id", "content", "group_id", "group_name", "group_type",
    "doc_status", "file_path", "file_url", "file_name", "chunk_order_index",
    "created_at",
}


# ============================================================================
# 数据生成（字段与生产 recover_chunks 一致）
# ============================================================================

_BASE_CONTENT_BYTES = "这是压力测试用的填充内容，用于模拟生产环境的 chunk 文本。".encode("utf-8")


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """按 UTF-8 字节数安全截断，避免从多字节字符中间切断。"""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", "ignore")


def make_content(target_bytes: int = CONTENT_TARGET_BYTES, suffix: str = "") -> str:
    """生成约 target_bytes 字节的 content，并在尾部保留唯一后缀。"""
    suffix_bytes = suffix.encode("utf-8")
    payload_bytes = max(target_bytes - len(suffix_bytes), 0)
    repeats = (payload_bytes + len(_BASE_CONTENT_BYTES) - 1) // len(_BASE_CONTENT_BYTES)
    prefix = (_BASE_CONTENT_BYTES * repeats)[:payload_bytes].decode("utf-8", "ignore")
    return prefix + suffix


def make_batch_records(start_idx: int, batch_size: int, run_id: str = "default") -> dict:
    """生成一批测试记录，字段与生产 recover_chunks 完全一致"""
    dict_data = {}
    run_tag = hashlib.sha1(run_id.encode("utf-8")).hexdigest()[:10]
    for i in range(batch_size):
        idx = start_idx + i
        # run_id 隔离不同压测，避免复用 collection 时旧主键被 upsert 覆盖。
        content = make_content(suffix=f"__run_{run_tag}_idx_{idx}__")
        chunk_id = "ch-" + hashlib.md5(content.encode()).hexdigest()
        # 模拟一个文档拆成最多 100 个 chunk，而非要求 full_doc_id 唯一。
        doc_id = f"stress_{run_tag}_{idx // 100:08d}"

        dict_data[chunk_id] = {
            "content": content,
            "full_doc_id": doc_id,
            "file_path": f"/stress_test/{doc_id}.txt",
            "group_id": "stress_test_group",
            "group_name": "压力测试分组",
            "group_type": "test",
            "doc_status": True,
            "file_url": f"http://stress.test/{doc_id}",
            "file_name": f"{doc_id}.txt",
            "chunk_order_index": idx % 100,
        }
    return dict_data


# ============================================================================
# Milvus chunks collection（保留 schema / 索引类型 / upsert 截断逻辑）
# ============================================================================

def create_chunks_schema(dim: int) -> CollectionSchema:
    """chunks 命名空间的 schema 快照（milvus_impl.py:270-320）"""
    fields = [
        FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=64, is_primary=True),
        FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=dim),
        FieldSchema(name="created_at", dtype=DataType.INT64),
        FieldSchema(name="group_id", dtype=DataType.VARCHAR, max_length=512, nullable=True),
        FieldSchema(name="group_name", dtype=DataType.VARCHAR, max_length=512, nullable=True),
        FieldSchema(name="group_type", dtype=DataType.VARCHAR, max_length=512, nullable=True),
        FieldSchema(name="doc_status", dtype=DataType.BOOL, nullable=True),
        FieldSchema(name="full_doc_id", dtype=DataType.VARCHAR, max_length=512, nullable=True, is_index=True),
        FieldSchema(name="file_name", dtype=DataType.VARCHAR, max_length=1024, nullable=True),
        FieldSchema(name="file_path", dtype=DataType.VARCHAR, max_length=65535, nullable=True),
    ]
    return CollectionSchema(
        fields=fields,
        description="Standalone stress test chunks vector storage (schema snapshot of LightRAG chunks)",
        enable_dynamic_field=True,
    )


def create_chunks_indexes(client: MilvusClient, collection_name: str):
    """保留 chunks 索引类型，HNSW 构建参数使用 1000 万写入优化值。"""
    index_params = client.prepare_index_params()

    index_params.add_index(
        field_name="vector",
        index_type="HNSW",
        metric_type="COSINE",
        params={"M": HNSW_M, "efConstruction": HNSW_EF_CONSTRUCTION},
    )
    index_params.add_index(field_name="full_doc_id", index_type="INVERTED")
    index_params.add_index(field_name="group_id", index_type="INVERTED")
    index_params.add_index(field_name="group_type", index_type="INVERTED")
    index_params.add_index(field_name="doc_status", index_type="BITMAP")

    client.create_index(collection_name=collection_name, index_params=index_params)


def _normalize_index_params(raw_params) -> dict:
    """兼容 PyMilvus 不同版本返回的字典或字典字符串。"""
    if isinstance(raw_params, Mapping):
        return dict(raw_params)
    if isinstance(raw_params, str):
        try:
            parsed = ast.literal_eval(raw_params)
        except (SyntaxError, ValueError):
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _index_param_as_int(params: dict, name: str) -> Optional[int]:
    """将索引数值参数规范为整数；无法解析时交由配置校验报告。"""
    try:
        return int(params[name])
    except (KeyError, TypeError, ValueError):
        return None


def _validate_existing_indexes(client: MilvusClient, collection_name: str) -> None:
    """确保复用 collection 时的索引配置仍与本次实验一致。"""
    expected = {
        "vector": ("HNSW", "COSINE"),
        "full_doc_id": ("INVERTED", None),
        "group_id": ("INVERTED", None),
        "group_type": ("INVERTED", None),
        "doc_status": ("BITMAP", None),
    }
    descriptions = [
        client.describe_index(collection_name=collection_name, index_name=index_name)
        for index_name in client.list_indexes(collection_name=collection_name)
    ]
    indexes_by_field = {item.get("field_name"): item for item in descriptions}

    errors = []
    for field_name, (index_type, metric_type) in expected.items():
        actual = indexes_by_field.get(field_name)
        if not actual:
            errors.append(f"{field_name}: 缺少索引")
            continue
        actual_type = str(actual.get("index_type", "")).upper()
        actual_metric = str(actual.get("metric_type", "")).upper()
        if actual_type != index_type:
            errors.append(f"{field_name}: index_type={actual_type or 'unknown'}，期望 {index_type}")
        if metric_type and actual_metric != metric_type:
            errors.append(f"{field_name}: metric_type={actual_metric or 'unknown'}，期望 {metric_type}")
        if field_name == "vector":
            params = _normalize_index_params(actual.get("params"))
            if _index_param_as_int(params, "M") != HNSW_M:
                errors.append(f"vector: M={params.get('M', 'unknown')}，期望 {HNSW_M}")
            if _index_param_as_int(params, "efConstruction") != HNSW_EF_CONSTRUCTION:
                errors.append(
                    "vector: efConstruction="
                    f"{params.get('efConstruction', 'unknown')}，期望 {HNSW_EF_CONSTRUCTION}"
                )

    if errors:
        raise ValueError("现有 collection 索引配置不匹配: " + "; ".join(errors))


def ensure_collection_ready(client: MilvusClient, collection_name: str, dim: int) -> bool:
    """检查/创建 collection + 索引，并确保已 load（返回是否新建）"""
    if client.has_collection(collection_name):
        description = client.describe_collection(collection_name=collection_name)
        fields = {item.get("name"): item for item in description.get("fields", [])}
        required_fields = {field.name for field in create_chunks_schema(dim).fields}
        missing_fields = sorted(required_fields - fields.keys())
        if missing_fields:
            raise ValueError(
                f"现有 collection schema 缺少字段: {', '.join(missing_fields)}"
            )

        vector_params = fields["vector"].get("params", {})
        existing_dim = int(vector_params.get("dim", 0))
        if existing_dim != dim:
            raise ValueError(
                f"现有 collection 向量维度为 {existing_dim}，命令行 --dim 为 {dim}；"
                "请使用匹配的维度或更换 workspace"
            )
        _validate_existing_indexes(client, collection_name)
        client.load_collection(collection_name)
        print(f"  ✅ 使用现有 collection: {collection_name}（schema/index 已校验并 load）")
        return False

    client.create_collection(
        collection_name=collection_name, schema=create_chunks_schema(dim)
    )
    create_chunks_indexes(client, collection_name)
    client.load_collection(collection_name)
    print(f"  ✅ 新建 collection: {collection_name}（schema + HNSW/INVERTED/BITMAP 索引已创建并 load）")
    return True


def upsert_chunks(client: MilvusClient, collection_name: str,
                  data: dict, dim: int,
                  batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    """upsert 逻辑快照（milvus_impl.py:1050-1173，chunks 命名空间）。

    与生产差异：
    - embedding 用随机向量直接生成（原脚本用 FakeEmbedding 异步调用）
    - 无 milvus_retry 重试装饰器 / 连接自愈（压测观察原始 timeout 行为）
    """
    # 过滤掉内容为空的数据
    data = {k: v for k, v in data.items() if v.get("content") and v["content"].strip()}
    if not data:
        return 0

    current_time = int(time.time())

    # 只保留 meta_fields 内的字段 + id + created_at
    list_data = [
        {
            "id": k,
            "created_at": current_time,
            **{k1: v1 for k1, v1 in v.items() if k1 in META_FIELDS},
        }
        for k, v in data.items()
    ]

    # 随机向量代替真实 embedding
    embeddings = np.random.rand(len(list_data), dim).astype(np.float32)

    for i, d in enumerate(list_data):
        # file_path/file_name 按 1024 字节截断；超长时逐步移除最后一个 <SEP> 及之后内容
        file_path = d.get("file_path") or ""
        while len(file_path.encode("utf-8")) > CHUNKS_FILE_LIMIT_BYTES:
            last_sep_index = file_path.rfind(GRAPH_FIELD_SEP)
            if last_sep_index == -1:
                file_path = _truncate_utf8(file_path, CHUNKS_FILE_LIMIT_BYTES)
                break
            file_path = file_path[:last_sep_index]
        d["file_path"] = file_path

        if isinstance(d.get("file_name"), str):
            d["file_name"] = _truncate_utf8(d["file_name"], CHUNKS_FILE_LIMIT_BYTES)
        else:
            d["file_name"] = file_path

        # 动态元数据按 UTF-8 字节截断；Milvus VARCHAR 的 max_length 也是字节数。
        for key in ("entity_name", "group_name", "group_type"):
            val = d.get(key)
            if isinstance(val, str):
                d[key] = _truncate_utf8(val, META_FIELD_LIMIT_BYTES)

        for key, max_bytes in VARCHAR_FIELD_LIMITS.items():
            val = d.get(key)
            if isinstance(val, str):
                d[key] = _truncate_utf8(val, max_bytes)

        # 全局兜底：所有字符串字段按 50000 字节截断（动态字段限制）
        for k, v in d.items():
            if isinstance(v, str):
                v_bytes = v.encode("utf-8")
                if len(v_bytes) > MAX_DYNAMIC_FIELD_BYTES:
                    d[k] = _truncate_utf8(v, MAX_DYNAMIC_FIELD_BYTES)

        d["vector"] = embeddings[i].tolist()

    # 独立保留分批保护，调用方未来传入大于外层 batch 的数据时仍不会超过 gRPC 限制。
    total_count = len(list_data)
    confirmed_count = 0
    for i in range(0, total_count, batch_size):
        batch_data = list_data[i: i + batch_size]
        result = client.upsert(collection_name=collection_name, data=batch_data)
        upsert_count = result.get("upsert_count") if isinstance(result, dict) else None
        if upsert_count != len(batch_data):
            raise RuntimeError(
                f"Milvus upsert_count 异常: expected={len(batch_data)}, actual={upsert_count}"
            )
        confirmed_count += upsert_count
    return confirmed_count


# ============================================================================
# 报告数据结构
# ============================================================================

@dataclass
class BatchRecord:
    """单批次记录"""
    phase: str
    batch_number: int
    count_before: int
    count_after: int
    cumulative_count: int
    batch_count: int
    elapsed: float
    success: bool
    timestamp: str
    error_type: Optional[str] = None
    error: Optional[str] = None


@dataclass
class StressReport:
    """压力测试报告"""
    target_count: int
    warmup_count: int = 0
    run_id: str = ""
    initial_row_count: int = 0
    warmup_batches: list = field(default_factory=list)
    batches: list = field(default_factory=list)
    windows: list = field(default_factory=list)
    elapsed: float = 0.0
    stop_reason: Optional[str] = None

    @property
    def actual_count(self) -> int:
        return self.batches[-1].count_after if self.batches else 0

    @property
    def actual_warmup_count(self) -> int:
        return self.warmup_batches[-1].count_after if self.warmup_batches else 0

    @property
    def total_actual_count(self) -> int:
        return self.actual_warmup_count + self.actual_count

    @property
    def attempted_count(self) -> int:
        return sum(batch.batch_count for batch in self.batches)

    @property
    def all_batches(self) -> list:
        return self.warmup_batches + self.batches

    @property
    def first_timeout(self) -> Optional[BatchRecord]:
        """首次出现 timeout 的批次"""
        for b in self.all_batches:
            if not b.success and _is_timeout(b.error, b.error_type):
                return b
        return None

    @property
    def success_batches(self) -> list:
        return [b for b in self.batches if b.success]

    @property
    def failed_batches(self) -> list:
        return [b for b in self.batches if not b.success]

    @property
    def error_types(self) -> Counter:
        return Counter(b.error_type or "UnknownError" for b in self.failed_batches)


@dataclass
class WindowRecord:
    """新增：正式统计阶段的时间/条数窗口记录。"""
    window_number: int
    count_before: int
    count_after: int
    cumulative_count: int
    batch_count: int
    success_count: int
    failed_batch_count: int
    elapsed: float
    avg_batch_elapsed: Optional[float]
    min_batch_elapsed: Optional[float]
    max_batch_elapsed: Optional[float]
    throughput: float
    timestamp: str


# ============================================================================
# 新增：CSV 逐批导出
# ============================================================================

CSV_FIELDNAMES = [
    "record_type", "timestamp", "phase", "batch_number", "window_number",
    "count_before", "count_after", "cumulative_count", "batch_count",
    "batch_elapsed_seconds", "success", "error_type", "error",
    "window_batch_count", "window_success_count", "window_failed_batch_count",
    "window_elapsed_seconds", "window_avg_batch_seconds",
    "window_min_batch_seconds", "window_max_batch_seconds",
    "throughput_records_per_second", "summary_name", "summary_value",
]


class CsvResultWriter:
    """可选地导出 CSV；output_path 为 None 时不产生任何文件或行数据。"""

    def __init__(self, output_path: Optional[Path]):
        self.output_path = output_path
        self.file: Optional[TextIO] = None
        self.writer = None

    @property
    def enabled(self) -> bool:
        return self.output_path is not None

    def __enter__(self):
        if not self.enabled:
            return self
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.output_path.open("w", encoding="utf-8-sig", newline="")
        self.writer = csv.DictWriter(self.file, fieldnames=CSV_FIELDNAMES)
        self.writer.writeheader()
        self.file.flush()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.file:
            self.file.close()

    def _write_row(self, row: dict, flush: bool = False):
        if not self.enabled:
            return
        if not self.writer or not self.file:
            raise RuntimeError("CSV writer 尚未打开")
        self.writer.writerow(row)
        if flush:
            self.file.flush()

    def write_batch(self, record: BatchRecord):
        if not self.enabled:
            return
        self._write_row({
            "record_type": "batch",
            "timestamp": record.timestamp,
            "phase": record.phase,
            "batch_number": record.batch_number,
            "count_before": record.count_before,
            "count_after": record.count_after,
            "cumulative_count": record.cumulative_count,
            "batch_count": record.batch_count,
            "batch_elapsed_seconds": f"{record.elapsed:.6f}",
            "success": record.success,
            "error_type": record.error_type or "",
            "error": record.error or "",
        }, flush=not record.success)

    def write_window(self, window: WindowRecord):
        if not self.enabled:
            return
        self._write_row({
            "record_type": "window",
            "timestamp": window.timestamp,
            "phase": "benchmark",
            "window_number": window.window_number,
            "count_before": window.count_before,
            "count_after": window.count_after,
            "cumulative_count": window.cumulative_count,
            "window_batch_count": window.batch_count,
            "window_success_count": window.success_count,
            "window_failed_batch_count": window.failed_batch_count,
            "window_elapsed_seconds": f"{window.elapsed:.6f}",
            "window_avg_batch_seconds": _format_optional_float(window.avg_batch_elapsed),
            "window_min_batch_seconds": _format_optional_float(window.min_batch_elapsed),
            "window_max_batch_seconds": _format_optional_float(window.max_batch_elapsed),
            "throughput_records_per_second": f"{window.throughput:.6f}",
        }, flush=True)

    def write_summary(self, name: str, value):
        if not self.enabled:
            return
        self._write_row({
            "record_type": "summary",
            "timestamp": _now_text(),
            "phase": "benchmark",
            "summary_name": name,
            "summary_value": value,
        })


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _format_optional_float(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.6f}"


def _is_timeout(error: Optional[str], error_type: Optional[str] = None) -> bool:
    """兼容常见 gRPC / PyMilvus timeout 表达。"""
    text = f"{error_type or ''} {error or ''}".lower().replace("_", " ")
    return any(token in text for token in ("timeout", "timed out", "deadline exceeded"))


def _percentile(times: list[float], percentile: float) -> Optional[float]:
    if not times:
        return None
    return float(np.percentile(np.asarray(times, dtype=float), percentile))


def _format_duration(seconds: float) -> str:
    """将秒数格式化为适合长时间压测展示的 HH:MM:SS。"""
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _get_row_count(client: MilvusClient, collection_name: str) -> int:
    stats = client.get_collection_stats(collection_name=collection_name)
    try:
        return int(stats["row_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"无法解析 collection row_count: {stats!r}") from exc


# ============================================================================
# 压力测试主流程
# ============================================================================

def _run_warmup(client, collection_name: str, dim: int, warmup_count: int,
                batch_size: int, stop_on_timeout: bool,
                max_consecutive_failures: int, run_id: str,
                report: StressReport, csv_writer: CsvResultWriter) -> bool:
    """新增：执行不纳入正式性能统计的预热写入，返回是否可继续。"""
    if warmup_count <= 0:
        return True

    print(f"\n🔥 开始预热 | 目标 {warmup_count:,} 条 | 不计入正式统计")
    print("=" * 70)
    inserted = 0
    batch_number = 0
    next_progress = REPORT_INTERVAL
    consecutive_failures = 0

    while inserted < warmup_count:
        current_batch_size = min(batch_size, warmup_count - inserted)
        count_before = inserted
        dict_data = make_batch_records(inserted, current_batch_size, run_id=run_id)
        t0 = time.perf_counter()
        success = True
        error_msg = None
        error_type = None
        try:
            confirmed_count = upsert_chunks(
                client, collection_name, dict_data, dim,
                batch_size=current_batch_size,
            )
            if confirmed_count != current_batch_size:
                raise RuntimeError(
                    f"本批确认数量异常: expected={current_batch_size}, actual={confirmed_count}"
                )
            inserted += confirmed_count
        except Exception as exc:
            success = False
            error_msg = str(exc)
            error_type = type(exc).__name__
        elapsed = time.perf_counter() - t0
        batch_number += 1

        record = BatchRecord(
            phase="warmup",
            batch_number=batch_number,
            count_before=count_before,
            count_after=inserted,
            cumulative_count=report.initial_row_count + inserted,
            batch_count=current_batch_size,
            elapsed=elapsed,
            success=success,
            timestamp=_now_text(),
            error_type=error_type,
            error=error_msg,
        )
        report.warmup_batches.append(record)
        csv_writer.write_batch(record)
        consecutive_failures = 0 if success else consecutive_failures + 1

        if not success:
            print(
                f"  ❌ [预热 {inserted:,}/{warmup_count:,}] "
                f"单批({current_batch_size}条) {elapsed:.2f}s | "
                f"错误: {(error_msg or '')[:150]}",
                flush=True,
            )
        elif inserted >= next_progress or inserted >= warmup_count:
            print(
                f"  🔥 [预热 {inserted:,}/{warmup_count:,}] "
                f"单批({current_batch_size}条) {elapsed:.2f}s",
                flush=True,
            )
            while next_progress <= inserted:
                next_progress += REPORT_INTERVAL

        if not success and _is_timeout(error_msg, error_type):
            if stop_on_timeout:
                report.stop_reason = "warmup_timeout"
                print(f"\n  ⛔ 预热期 timeout @ 累计 {inserted:,} 条（单批 {elapsed:.2f}s）")
                print(f"     错误: {(error_msg or '')[:200]}")
                return False
            print(f"  ⚠️  预热期 Timeout @ {inserted:,}，继续测试...")

        if consecutive_failures >= max_consecutive_failures:
            report.stop_reason = "warmup_consecutive_failures"
            print(f"\n  ⛔ 预热连续失败 {consecutive_failures} 批，终止测试以避免无限循环")
            return False

    print(f"  ✅ 预热完成: {inserted:,} 条（不纳入正式统计）")
    return True


def _build_window(window_number: int, window_start_count: int, inserted: int,
                  cumulative_base_count: int, window_start: float,
                  records: list[BatchRecord]) -> WindowRecord:
    """新增：从当前窗口内的批次生成窗口统计。"""
    window_elapsed = time.perf_counter() - window_start
    success_records = [b for b in records if b.success]
    batch_times = [b.elapsed for b in success_records]
    success_count = sum(b.batch_count for b in success_records)
    return WindowRecord(
        window_number=window_number,
        count_before=window_start_count,
        count_after=inserted,
        cumulative_count=cumulative_base_count + inserted,
        batch_count=len(records),
        success_count=success_count,
        failed_batch_count=len(records) - len(success_records),
        elapsed=window_elapsed,
        avg_batch_elapsed=(sum(batch_times) / len(batch_times) if batch_times else None),
        min_batch_elapsed=min(batch_times) if batch_times else None,
        max_batch_elapsed=max(batch_times) if batch_times else None,
        throughput=success_count / window_elapsed if window_elapsed > 0 else 0.0,
        timestamp=_now_text(),
    )


def _print_window(window: WindowRecord):
    """新增：输出一个时间窗口的统计结果。"""
    avg_text = f"{window.avg_batch_elapsed:.2f}s" if window.avg_batch_elapsed is not None else "N/A"
    min_text = f"{window.min_batch_elapsed:.2f}s" if window.min_batch_elapsed is not None else "N/A"
    max_text = f"{window.max_batch_elapsed:.2f}s" if window.max_batch_elapsed is not None else "N/A"
    print(
        f"   窗口 #{window.window_number} | {window.elapsed:.1f}s | "
        f"{window.batch_count} 批 | 成功 {window.success_count:,} 条 | "
        f"失败 {window.failed_batch_count} 批 | "
        f"avg {avg_text} | min {min_text} | max {max_text} | "
        f"瞬时吞吐 {window.throughput:.0f} 条/秒",
        flush=True,
    )


def run_stress_test(client, collection_name, dim, target_count: int, batch_size: int,
                    stop_on_timeout: bool, warmup_count: int,
                    max_consecutive_failures: int, run_id: str, initial_row_count: int,
                    csv_writer: CsvResultWriter) -> StressReport:
    """持续 upsert 撑大表，记录每批耗时、窗口吞吐和预热数据。"""
    report = StressReport(
        target_count=target_count,
        warmup_count=warmup_count,
        run_id=run_id,
        initial_row_count=initial_row_count,
    )
    can_continue = _run_warmup(
        client=client,
        collection_name=collection_name,
        dim=dim,
        warmup_count=warmup_count,
        batch_size=batch_size,
        stop_on_timeout=stop_on_timeout,
        max_consecutive_failures=max_consecutive_failures,
        run_id=run_id,
        report=report,
        csv_writer=csv_writer,
    )
    if not can_continue:
        return report

    print(f"\n🚀 开始持续 upsert | 目标 {target_count:,} 条 | batch={batch_size}")
    print("=" * 70)
    overall_start = time.perf_counter()
    inserted = 0
    batch_number = 0
    next_progress = REPORT_INTERVAL
    recent_batch_times: list[float] = []

    window_number = 1
    window_start = time.perf_counter()
    window_start_count = 0
    window_records: list[BatchRecord] = []
    window_success_count = 0
    consecutive_failures = 0

    while inserted < target_count:
        current_batch_size = min(batch_size, target_count - inserted)
        count_before = inserted
        dict_data = make_batch_records(
            report.actual_warmup_count + inserted,
            current_batch_size,
            run_id=run_id,
        )
        t0 = time.perf_counter()
        success = True
        error_msg = None
        error_type = None
        try:
            confirmed_count = upsert_chunks(
                client, collection_name, dict_data, dim,
                batch_size=current_batch_size,
            )
            if confirmed_count != current_batch_size:
                raise RuntimeError(
                    f"本批确认数量异常: expected={current_batch_size}, actual={confirmed_count}"
                )
            inserted += confirmed_count
        except Exception as exc:
            success = False
            error_msg = str(exc)
            error_type = type(exc).__name__
        elapsed = time.perf_counter() - t0
        batch_number += 1

        record = BatchRecord(
            phase="benchmark",
            batch_number=batch_number,
            count_before=count_before,
            count_after=inserted,
            cumulative_count=report.initial_row_count + report.actual_warmup_count + inserted,
            batch_count=current_batch_size,
            elapsed=elapsed,
            success=success,
            timestamp=_now_text(),
            error_type=error_type,
            error=error_msg,
        )
        report.batches.append(record)
        csv_writer.write_batch(record)
        window_records.append(record)
        if success:
            recent_batch_times.append(elapsed)
            window_success_count += current_batch_size
            consecutive_failures = 0
        else:
            consecutive_failures += 1

        if not success:
            total_elapsed = time.perf_counter() - overall_start
            speed = inserted / total_elapsed if total_elapsed > 0 else 0
            print(
                f"  ❌ [{inserted:,}/{target_count:,}] "
                f"单批({current_batch_size}条) {elapsed:.2f}s | 累计速度 {speed:.0f} 条/秒 | "
                f"错误: {(error_msg or '')[:150]}",
                flush=True,
            )
        elif inserted >= next_progress and recent_batch_times:
            total_elapsed = time.perf_counter() - overall_start
            speed = inserted / total_elapsed if total_elapsed > 0 else 0
            progress = min(inserted / target_count * 100, 100.0) if target_count > 0 else 100.0
            remaining_count = max(target_count - inserted, 0)
            eta_seconds = remaining_count / speed if speed > 0 else 0
            times = recent_batch_times
            print(
                f"  ✅ [{inserted:,}/{target_count:,} | {progress:.2f}%] "
                f"近 {len(times)} 个成功批次 | "
                f"min {min(times):.2f}s | avg {sum(times) / len(times):.2f}s | max {max(times):.2f}s | "
                f"累计速度 {speed:.0f} 条/秒 | "
                f"已用 {_format_duration(total_elapsed)} | ETA {_format_duration(eta_seconds)}",
                flush=True,
            )
            recent_batch_times = []
            while next_progress <= inserted:
                next_progress += REPORT_INTERVAL

        window_elapsed = time.perf_counter() - window_start
        if window_elapsed >= WINDOW_SECONDS or window_success_count >= WINDOW_RECORDS:
            window = _build_window(
                window_number, window_start_count, inserted,
                report.initial_row_count + report.actual_warmup_count,
                window_start, window_records,
            )
            report.windows.append(window)
            csv_writer.write_window(window)
            _print_window(window)
            window_number += 1
            window_start = time.perf_counter()
            window_start_count = inserted
            window_records = []
            window_success_count = 0

        if not success and _is_timeout(error_msg, error_type):
            if stop_on_timeout:
                report.stop_reason = "benchmark_timeout"
                print(f"\n  ⛔ 首次 timeout @ 累计 {inserted:,} 条（单批 {elapsed:.2f}s）")
                print(f"     错误: {(error_msg or '')[:200]}")
                break
            print(f"  ⚠️  Timeout @ {inserted:,}，继续测试...")

        if consecutive_failures >= max_consecutive_failures:
            report.stop_reason = "benchmark_consecutive_failures"
            print(f"\n  ⛔ 连续失败 {consecutive_failures} 批，终止测试以避免无限循环")
            break

    if window_records:
        window = _build_window(
            window_number, window_start_count, inserted,
            report.initial_row_count + report.actual_warmup_count,
            window_start, window_records,
        )
        report.windows.append(window)
        csv_writer.write_window(window)
        _print_window(window)

    report.elapsed = time.perf_counter() - overall_start
    print(f"\n  ⏱️  正式测试总耗时: {report.elapsed:.1f}s")
    return report


def print_report(report: StressReport, batch_size: int,
                 csv_writer: CsvResultWriter):
    """输出最终报告，并将相同汇总追加到 CSV 末尾。"""
    print("\n" + "=" * 70)
    print("  📊 压力测试汇总")
    print("=" * 70)
    print(f"  预热目标量: {report.warmup_count:,}")
    print(f"  预热已确认量: {report.actual_warmup_count:,}（不纳入以下性能统计）")
    print(f"  启动时 collection 行数: {report.initial_row_count:,}")
    print(f"  目标写入量: {report.target_count:,}")
    print(f"  已确认写入量: {report.actual_count:,}")
    print(f"  尝试写入量: {report.attempted_count:,}（包含失败批次及重试）")
    print(f"  含预热已确认量: {report.total_actual_count:,}")
    print(f"  总批次数: {len(report.batches)}")
    print(f"  成功批次: {len(report.success_batches)}")
    print(f"  失败批次: {len(report.failed_batches)}")
    if report.stop_reason:
        print(f"  终止原因: {report.stop_reason}")
    print(f"  正式测试耗时: {report.elapsed:.2f}s")
    overall_throughput = report.actual_count / report.elapsed if report.elapsed > 0 else 0.0
    print(f"  已确认吞吐量: {overall_throughput:.2f} 条/秒")
    if report.target_count == 0 and not report.batches:
        print("  提示: 正式写入目标为 0，未执行正式批次，吞吐量按 0 统计")
    print("  指标口径: 客户端收到 upsert_count 确认；不代表 flush/持久化或索引构建完成")

    success_times = [b.elapsed for b in report.success_batches]
    if success_times:
        print(f"\n  📈 客户端单批耗时（仅成功批次，含向量生成/序列化/请求，单位: 秒）:")
        print(f"     min: {min(success_times):.2f}")
        print(f"     avg: {sum(success_times) / len(success_times):.2f}")
        print(f"     max: {max(success_times):.2f}")
        print(f"     P50: {_percentile(success_times, 50):.2f}")
        print(f"     P95: {_percentile(success_times, 95):.2f}")
        print(f"     P99: {_percentile(success_times, 99):.2f}")

    if report.success_batches:
        segment_size = REPORT_SEGMENT_SIZE
        print(f"\n  📈 按累计写入量分段（每 {segment_size:,} 条一段）:")
        print(f"     {'区间':<22} | {'批次数':<8} | {'avg(s)':<8} | {'max(s)':<8}")
        print(f"     " + "-" * 60)
        for seg_start in range(0, report.actual_count, segment_size):
            seg_end = min(seg_start + segment_size, report.actual_count)
            seg_batches = [
                b for b in report.success_batches
                if seg_start < b.count_after <= seg_end
            ]
            if seg_batches:
                times = [b.elapsed for b in seg_batches]
                avg_t = sum(times) / len(times)
                max_t = max(times)
                print(f"     {seg_start + 1:>9,}-{seg_end:>9,} | {len(seg_batches):>6} | {avg_t:>6.2f} | {max_t:>6.2f}")

    if report.error_types:
        print("\n  ⚠️  失败错误类型:")
        for error_type, count in report.error_types.most_common():
            print(f"     {error_type}: {count} 次")

    first_to = report.first_timeout
    print(f"\n  ❌ Timeout 统计:")
    if first_to:
        phase_name = "预热期" if first_to.phase == "warmup" else "正式测试"
        print(
            f"     首次 timeout: {phase_name}已确认 {first_to.count_after:,} 条后"
            f"（总累计 {first_to.cumulative_count:,} 条，单批 {first_to.elapsed:.2f}s）"
        )
        print("     注意: timeout 的服务端提交结果不确定，已确认量不等于数据库最终行数")
        print(f"     错误信息: {first_to.error[:200] if first_to.error else 'N/A'}")
    else:
        print(f"     未出现 timeout")

    print("\n" + "=" * 70)

    # 新增：最终汇总追加到同一个 CSV 文件末尾。
    summary = {
        "warmup_target_count": report.warmup_count,
        "warmup_actual_count": report.actual_warmup_count,
        "run_id": report.run_id,
        "initial_row_count": report.initial_row_count,
        "target_count": report.target_count,
        "actual_count": report.actual_count,
        "attempted_count": report.attempted_count,
        "total_actual_count": report.total_actual_count,
        "batch_size": batch_size,
        "total_batches": len(report.batches),
        "success_batches": len(report.success_batches),
        "failed_batches": len(report.failed_batches),
        "stop_reason": report.stop_reason or "completed",
        "elapsed_seconds": f"{report.elapsed:.6f}",
        "overall_throughput_records_per_second": f"{overall_throughput:.6f}",
        "throughput_scope": "client_acknowledged_upserts_not_flush_or_index_completion",
        "min_batch_seconds": _format_optional_float(min(success_times) if success_times else None),
        "avg_batch_seconds": _format_optional_float(
            sum(success_times) / len(success_times) if success_times else None
        ),
        "max_batch_seconds": _format_optional_float(max(success_times) if success_times else None),
        "p50_batch_seconds": _format_optional_float(_percentile(success_times, 50)),
        "p95_batch_seconds": _format_optional_float(_percentile(success_times, 95)),
        "p99_batch_seconds": _format_optional_float(_percentile(success_times, 99)),
        "first_timeout_at": first_to.cumulative_count if first_to else "",
        "first_timeout_phase": first_to.phase if first_to else "",
        "first_timeout_error": first_to.error if first_to else "",
    }
    for error_type, count in report.error_types.most_common():
        summary[f"error_type_{error_type}"] = count
    for name, value in summary.items():
        csv_writer.write_summary(name, value)


# ============================================================================
# 入口
# ============================================================================

def main():
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Milvus 1000 万数据写入压力测试（独立版，不依赖 lightrag）")
    parser.add_argument("--target-count", "-n", type=int, default=DEFAULT_TARGET_COUNT,
                        help=f"正式统计写入量（默认 {DEFAULT_TARGET_COUNT:,}）")
    parser.add_argument("--batch-size", "-b", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"每批 upsert 条数（默认 {DEFAULT_BATCH_SIZE}，针对 1000 万写入）")
    parser.add_argument("--stop-on-timeout", action="store_true", default=DEFAULT_STOP_ON_TIMEOUT,
                        help="首次 timeout 后停止（默认关闭）")
    parser.add_argument("--no-stop-on-timeout", dest="stop_on_timeout", action="store_false",
                        help="首次 timeout 后继续，观察后续行为")
    parser.add_argument("--env", "-e", type=str, default=None, help=".env 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只验证连接和 schema，不写入")
    parser.add_argument("--workspace", type=str, default=DEFAULT_WORKSPACE,
                        help=f"workspace 名称（默认 {DEFAULT_WORKSPACE}，collection 名为 <workspace>_chunks）")
    parser.add_argument("--dim", type=int, default=EMBEDDING_DIM,
                        help=f"向量维度（默认 {EMBEDDING_DIM}）")
    # 新增：预热和 CSV 导出参数。
    parser.add_argument("--warmup-count", type=int, default=DEFAULT_WARMUP_COUNT,
                        help=f"正式统计前的预热写入量（默认 {DEFAULT_WARMUP_COUNT:,}，不纳入性能统计）")
    parser.add_argument("--run-id", type=str, default=None,
                        help="本次数据主键命名空间；默认随机生成，传入相同值可重复 upsert 同一批数据")
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_FAILURES,
        help=f"连续失败多少批后终止（默认 {DEFAULT_MAX_CONSECUTIVE_FAILURES}）",
    )
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT,
                        help="CSV 结果文件路径（默认不保存；传入路径时才导出）")
    args = parser.parse_args()

    if args.target_count < 0:
        parser.error("--target-count 不能小于 0")
    if args.batch_size <= 0:
        parser.error("--batch-size 必须大于 0")
    if args.warmup_count < 0:
        parser.error("--warmup-count 不能小于 0")
    if args.dim <= 0:
        parser.error("--dim 必须大于 0")
    if args.max_consecutive_failures <= 0:
        parser.error("--max-consecutive-failures 必须大于 0")

    if args.env:
        env_path = Path(args.env)
    else:
        env_path = script_dir / ".env"
        if not env_path.exists():
            env_path = script_dir.parent / ".env"

    if env_path.exists():
        load_dotenv(dotenv_path=str(env_path), override=True)
        print(f"📝 已加载配置: {env_path}")
    else:
        print(f"⚠️  .env 不存在: {env_path}（将使用环境变量/默认值）")

    collection_name = f"{args.workspace}_chunks"
    run_id = args.run_id or uuid.uuid4().hex[:12]
    milvus_uri = os.getenv("MILVUS_URI", MILVUS_URI)
    milvus_db_name = os.getenv("MILVUS_DB_NAME", MILVUS_DB_NAME)
    client_timeout = int(os.getenv("MILVUS_TIMEOUT", str(MILVUS_CLIENT_TIMEOUT)))
    estimated_batches = (
        (args.target_count + args.batch_size - 1) // args.batch_size
        if args.target_count > 0 else 0
    )

    print(f"\n{'=' * 70}")
    print(f"  🚀 Milvus 1000 万数据写入压力测试（独立版）")
    print(f"{'=' * 70}")
    print(f"  Script version: {SCRIPT_VERSION}")
    print(f"  Workspace:      {args.workspace}")
    print(f"  Collection:     {collection_name}")
    print(f"  Run ID:         {run_id}")
    print(f"  预热写入:        {args.warmup_count:,} 条")
    print(f"  本次目标:        {args.target_count:,} 条 upsert")
    print(f"  Batch size:     {args.batch_size}")
    print(f"  预计总批次:      {estimated_batches:,}")
    print(f"  Embedding:      随机 {args.dim} 维向量")
    print(f"  Content 大小:   ~{CONTENT_TARGET_BYTES} bytes/条")
    print(f"  进度输出:        每 {REPORT_INTERVAL:,} 条")
    print(f"  窗口输出:        每 {WINDOW_SECONDS}s 或 {WINDOW_RECORDS:,} 条")
    print(f"  HNSW 参数:       M={HNSW_M}, efConstruction={HNSW_EF_CONSTRUCTION}")
    print(f"  Milvus URI:     {milvus_uri}")
    print(f"  Milvus DB:      {milvus_db_name}")
    print(f"  Client timeout: {client_timeout}s")
    print(f"  Stop on timeout: {args.stop_on_timeout}")
    print(f"  Max consecutive failures: {args.max_consecutive_failures}")
    print(f"  CSV 输出:        {args.output or '不保存'}")

    print("\n🔌 连接 Milvus...")
    client = MilvusClient(
        uri=milvus_uri,
        user=os.getenv("MILVUS_USER"),
        password=os.getenv("MILVUS_PASSWORD"),
        token=os.getenv("MILVUS_TOKEN"),
        db_name=milvus_db_name,
        timeout=client_timeout,
    )
    print("  ✅ Milvus 连接成功")

    if args.dry_run:
        print("\n🔍 [DRY RUN] 只读验证连接、schema 和索引，不创建 collection、不写入")
        if client.has_collection(collection_name):
            ensure_collection_ready(client, collection_name, args.dim)
            print(f"  📊 {collection_name} 现有记录数: {_get_row_count(client, collection_name):,}")
        else:
            print(f"  📁 {collection_name} 不存在（dry-run 不会创建）")
        return

    ensure_collection_ready(client, collection_name, args.dim)
    initial_row_count = _get_row_count(client, collection_name)
    print(f"  📊 启动时 collection 行数: {initial_row_count:,}")
    if initial_row_count > 0:
        print("  ⚠️  collection 非空；吞吐统计仅代表本次操作，不能直接当作最终物理行数")
    if args.run_id:
        print("  ⚠️  已显式指定 run-id；若该值曾使用过，本次 upsert 会更新已有主键")

    output_path = Path(args.output).expanduser().resolve() if args.output else None
    with CsvResultWriter(output_path) as csv_writer:
        report = run_stress_test(
            client=client,
            collection_name=collection_name,
            dim=args.dim,
            target_count=args.target_count,
            batch_size=args.batch_size,
            stop_on_timeout=args.stop_on_timeout,
            warmup_count=args.warmup_count,
            max_consecutive_failures=args.max_consecutive_failures,
            run_id=run_id,
            initial_row_count=initial_row_count,
            csv_writer=csv_writer,
        )
        print_report(report, args.batch_size, csv_writer)

    if output_path:
        print(f"  💾 CSV 结果已保存: {output_path}")


if __name__ == "__main__":
    main()
