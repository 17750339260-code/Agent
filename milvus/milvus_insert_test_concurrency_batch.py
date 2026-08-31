#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Milvus 多客户端并发写入压力测试（独立版）

本脚本不依赖 lightrag 项目代码，仅强制依赖 pymilvus、numpy；
python-dotenv 为可选依赖，缺失时使用标准库读取简单 .env。
schema 字段和截断逻辑沿用 lightrag chunks 命名空间，向量维度、数据大小和
HNSW 参数则针对 1000 万数据的写入效率进行了调整。

实验目标：
    使用多个独立 MilvusClient 线程并发写入，观察全局/worker 吞吐、延迟和 timeout。

实验设计：
    1. 保留生产 chunks collection 的字段、索引类型和截断逻辑
    2. 随机向量代替真实 embedding（避免模型限流/超时干扰实验）
    3. 每个 collection 最多写入 200 万条，达到上限后自动创建编号分片
    4. target-count 和 warmup-count 均匀分段，worker 间主键不重叠
    5. 每个线程持有独立客户端，网络 I/O 等待不受 Python GIL 限制
    6. 检测 timeout，并原子记录首次出现时所有 worker 的累计确认量
    7. 导出 batch/window/worker_summary/summary 四类 CSV 记录

用法：
    # 直接运行：正式写入 1000 万条，不保存 CSV
    python milvus_insert_test_concurrency_batch.py --concurrency 8

    # 临时覆盖默认写入量，进行小规模试跑
    python milvus_insert_test_concurrency_batch.py --target-count 10000 --concurrency 4 --warmup-count 1000

    # 每批成功后立即 flush，并保持 collection 已加载以供实时查询
    python milvus_insert_test_concurrency_batch.py --target-count 10000 --realtime-visible

    # 只验证连接和 schema
    python milvus_insert_test_concurrency_batch.py --dry-run
"""

from __future__ import annotations

import os
import time
import hashlib
import argparse
import ast
import csv
import uuid
import threading
import re
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Optional, TextIO

import numpy as np
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
from pymilvus import MilvusClient, FieldSchema, CollectionSchema, DataType


# ============================================================================
# 配置常量（针对 1000 万数据快速写入）
# ============================================================================

SCRIPT_VERSION = "10m-concurrent-v8-legacy-pymilvus"
DEFAULT_COLLECTION_NAME = "stress_test_chunks"      # 基础 collection，后续分片追加 _1、_2...
MILVUS_URI = "http://192.168.0.237:30001"  # Milvus 2.6.21 测试集群
MILVUS_DB_NAME = "default"
MILVUS_CLIENT_TIMEOUT = 300              # 普通 RPC 客户端超时，单位秒
# None 表示索引创建/等待不设人为总时限；服务端失败或 RPC 异常仍会退出。
INDEX_BUILD_TIMEOUT: Optional[float] = None
EMBEDDING_DIM = 1024                     # 与固定目标 stress_test_chunks 的向量维度一致
DEFAULT_TARGET_COUNT = 2_000_000        # 正式统计阶段默认写入 1000 万条
DEFAULT_BATCH_SIZE = 100                 # 单批约数 MB，兼顾吞吐和 gRPC 消息大小
COLLECTION_MAX_ROWS = 2_000_000          # 单个 collection 的最大逻辑行数
DEFAULT_CONCURRENCY = 8                  # 每个线程持有独立 MilvusClient
DEFAULT_WARMUP_COUNT = 0                 # 默认不预热，确保实际目标为 1000 万条
DEFAULT_STOP_ON_TIMEOUT = False          # 长时间灌数默认在 timeout 后继续
DEFAULT_MAX_CONSECUTIVE_FAILURES = 10    # 防止持续故障时无限重试同一批数据
CONTENT_TARGET_BYTES = 4_096             # 降低网络和存储开销，保留动态文本字段压力
REPORT_INTERVAL = 100_000                # 1000 万规模每 10 万条输出一次，共约 100 次
WINDOW_SECONDS = 30                      # 长时间压测每 30 秒输出窗口统计
WINDOW_RECORDS = 50_000                  # 或每成功写入 5 万条触发窗口统计
DEFAULT_OUTPUT = None                    # 默认不保存 CSV，避免长期压测产生大文件

# 与固定目标 stress_test_chunks 的现有 HNSW 索引配置一致。
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 256

# 正式环境只为实际参与过滤/删除的字段创建标量索引。
SCALAR_INDEX_SPECS = (
    ("full_doc_id", "INVERTED"),
    ("group_id", "INVERTED"),
    ("group_type", "INVERTED"),
    ("doc_status", "BITMAP"),
)

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


def load_environment(path: Path) -> None:
    """加载 .env；旧服务器未安装 python-dotenv 时使用标准库兜底。"""
    if load_dotenv is not None:
        load_dotenv(dotenv_path=str(path), override=True)
        return

    with path.open("r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if value and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key:
                os.environ[key] = value


def _client_connection(client: MilvusClient):
    get_connection = getattr(client, "_get_connection", None)
    if not callable(get_connection):
        raise RuntimeError("当前 PyMilvus 版本无法访问索引元数据接口")
    return get_connection()


def _list_indexes_compat(client: MilvusClient, collection_name: str,
                         timeout: Optional[float] = None) -> list[str]:
    """兼容缺少 MilvusClient.list_indexes 的旧版 PyMilvus。"""
    list_indexes = getattr(client, "list_indexes", None)
    if callable(list_indexes):
        return list(
            list_indexes(collection_name=collection_name, timeout=timeout)
        )

    raw_indexes = _client_connection(client).list_indexes(
        collection_name=collection_name,
        timeout=timeout,
    )
    names = []
    for item in raw_indexes:
        if isinstance(item, str):
            name = item
        elif isinstance(item, Mapping):
            name = item.get("index_name") or item.get("field_name") or ""
        else:
            name = (
                getattr(item, "index_name", "")
                or getattr(item, "field_name", "")
            )
        if name:
            names.append(str(name))
    return names


def _describe_index_compat(client: MilvusClient, collection_name: str,
                           index_name: str,
                           timeout: Optional[float] = None) -> dict:
    """兼容缺少 MilvusClient.describe_index 的旧版 PyMilvus。"""
    describe_index = getattr(client, "describe_index", None)
    if not callable(describe_index):
        describe_index = _client_connection(client).describe_index
    description = describe_index(
        collection_name=collection_name,
        index_name=index_name,
        timeout=timeout,
    )
    if isinstance(description, Mapping):
        return dict(description)
    try:
        return dict(description)
    except (TypeError, ValueError):
        return {
            "index_name": str(getattr(description, "index_name", index_name)),
            "field_name": str(getattr(description, "field_name", index_name)),
            "index_type": str(getattr(description, "index_type", "")),
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


def create_chunks_indexes(client: MilvusClient, collection_name: str,
                          timeout: Optional[float] = None,
                          sync: bool = True):
    """补齐 chunks 缺失索引，保留已有索引以支持中断后恢复。"""
    existing_fields = set()
    for index_name in _list_indexes_compat(client, collection_name, timeout):
        description = _describe_index_compat(
            client, collection_name, index_name, timeout
        )
        existing_fields.add(str(description.get("field_name") or index_name))

    index_params = client.prepare_index_params()
    missing_fields = []

    if "vector" not in existing_fields:
        index_params.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": HNSW_M, "efConstruction": HNSW_EF_CONSTRUCTION},
        )
        missing_fields.append("vector")
    for field_name, index_type in SCALAR_INDEX_SPECS:
        if field_name not in existing_fields:
            index_params.add_index(field_name=field_name, index_type=index_type)
            missing_fields.append(field_name)

    if not missing_fields:
        return

    print(f"  ⏳ 补建缺失索引: {', '.join(missing_fields)}", flush=True)

    client.create_index(
        collection_name=collection_name,
        index_params=index_params,
        timeout=timeout,
        sync=sync,
    )


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


def _validate_existing_indexes(client: MilvusClient, collection_name: str,
                               timeout: Optional[float] = None) -> None:
    """确保复用 collection 时的索引配置仍与本次实验一致。"""
    expected = {"vector": ("HNSW", "COSINE")}
    expected.update(
        {
            field_name: (index_type, None)
            for field_name, index_type in SCALAR_INDEX_SPECS
        }
    )
    descriptions = [
        _describe_index_compat(client, collection_name, index_name, timeout)
        for index_name in _list_indexes_compat(client, collection_name, timeout)
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
            # PyMilvus 3.x 将 HNSW 参数放在描述顶层，部分旧版本放在 params 中。
            params = _normalize_index_params(actual)
            params.update(_normalize_index_params(actual.get("params")))
            if _index_param_as_int(params, "M") != HNSW_M:
                errors.append(f"vector: M={params.get('M', 'unknown')}，期望 {HNSW_M}")
            if _index_param_as_int(params, "efConstruction") != HNSW_EF_CONSTRUCTION:
                errors.append(
                    "vector: efConstruction="
                    f"{params.get('efConstruction', 'unknown')}，期望 {HNSW_EF_CONSTRUCTION}"
                )

    if errors:
        raise ValueError("现有 collection 索引配置不匹配: " + "; ".join(errors))


def _field_param_as_int(field: Mapping, name: str) -> Optional[int]:
    params = _normalize_index_params(field.get("params"))
    value = params.get(name, field.get(name))
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dtype_as_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        enum_value = getattr(value, "value", None)
        try:
            return int(enum_value)
        except (TypeError, ValueError):
            return None


def _validate_existing_schema(description: Mapping, dim: int) -> None:
    """完整校验会影响写入语义和载荷的 schema 属性。"""
    expected = {
        "id": (DataType.VARCHAR, False, True, 64),
        "vector": (DataType.FLOAT_VECTOR, False, False, dim),
        "created_at": (DataType.INT64, False, False, None),
        "group_id": (DataType.VARCHAR, True, False, 512),
        "group_name": (DataType.VARCHAR, True, False, 512),
        "group_type": (DataType.VARCHAR, True, False, 512),
        "doc_status": (DataType.BOOL, True, False, None),
        "full_doc_id": (DataType.VARCHAR, True, False, 512),
        "file_name": (DataType.VARCHAR, True, False, 1024),
        "file_path": (DataType.VARCHAR, True, False, 65_535),
    }
    fields = {item.get("name"): item for item in description.get("fields", [])}
    errors = []
    for name, (dtype, nullable, primary, size) in expected.items():
        actual = fields.get(name)
        if actual is None:
            errors.append(f"{name}: 缺少字段")
            continue
        actual_dtype = _dtype_as_int(actual.get("type", actual.get("dtype")))
        if actual_dtype != _dtype_as_int(dtype):
            errors.append(f"{name}: dtype={actual.get('type', actual.get('dtype'))!r}，期望 {dtype.name}")
        if bool(actual.get("is_primary", False)) != primary:
            errors.append(f"{name}: is_primary={actual.get('is_primary', False)}，期望 {primary}")
        if bool(actual.get("nullable", False)) != nullable:
            errors.append(f"{name}: nullable={actual.get('nullable', False)}，期望 {nullable}")
        if size is not None:
            param_name = "dim" if name == "vector" else "max_length"
            actual_size = _field_param_as_int(actual, param_name)
            if actual_size != size:
                errors.append(f"{name}: {param_name}={actual_size}，期望 {size}")

    dynamic_value = description.get(
        "enable_dynamic_field", description.get("enableDynamicField")
    )
    if dynamic_value is not None and not bool(dynamic_value):
        errors.append("enable_dynamic_field=False，期望 True")
    if errors:
        raise ValueError("现有 collection schema 配置不匹配: " + "; ".join(errors))


def ensure_collection_ready(client: MilvusClient, collection_name: str, dim: int,
                            load_collection: bool = False,
                            timeout: Optional[float] = None,
                            create_indexes: bool = False) -> bool:
    """检查/创建 collection schema；索引在全部数据 flush 后统一创建。"""
    print(f"  ⏳ 检查 collection 是否存在: {collection_name}", flush=True)
    if client.has_collection(collection_name, timeout=timeout):
        print("  ⏳ 校验现有 collection schema...", flush=True)
        description = client.describe_collection(
            collection_name=collection_name,
            timeout=timeout,
        )
        _validate_existing_schema(description, dim)
        if create_indexes:
            print("  ⏳ 校验现有 collection 索引...", flush=True)
            _validate_existing_indexes(client, collection_name, timeout=timeout)
        print(f"  ✅ 使用现有 collection: {collection_name}（仅校验 schema，索引稍后统一处理）")
        return False

    print("  ⏳ collection 不存在，正在创建 schema...", flush=True)
    client.create_collection(
        collection_name=collection_name,
        schema=create_chunks_schema(dim),
        timeout=timeout,
    )
    if create_indexes:
        print("  ⏳ 正在创建 HNSW/INVERTED/BITMAP 索引...", flush=True)
        create_chunks_indexes(
            client,
            collection_name,
            timeout=INDEX_BUILD_TIMEOUT,
        )
    print(f"  ✅ 新建 collection: {collection_name}（仅创建 schema，索引稍后统一处理）")
    return True


def prepare_chunk_rows(data: dict, dim: int,
                       rng: Optional[np.random.Generator] = None) -> list[dict]:
    """将原始 chunk 数据转换为可直接提交给 Milvus 的行。"""
    data = {k: v for k, v in data.items() if v.get("content") and v["content"].strip()}
    if not data:
        return []

    current_time = int(time.time())
    list_data = [
        {
            "id": key,
            "created_at": current_time,
            **{meta_key: value for meta_key, value in item.items() if meta_key in META_FIELDS},
        }
        for     key, item in data.items()
    ]
    generator = rng or np.random.default_rng()
    embeddings = generator.random((len(list_data), dim), dtype=np.float32)

    for i, row in enumerate(list_data):
        file_path = row.get("file_path") or ""
        while len(file_path.encode("utf-8")) > CHUNKS_FILE_LIMIT_BYTES:
            last_sep_index = file_path.rfind(GRAPH_FIELD_SEP)
            if last_sep_index == -1:
                file_path = _truncate_utf8(file_path, CHUNKS_FILE_LIMIT_BYTES)
                break
            file_path = file_path[:last_sep_index]
        row["file_path"] = file_path

        if isinstance(row.get("file_name"), str):
            row["file_name"] = _truncate_utf8(row["file_name"], CHUNKS_FILE_LIMIT_BYTES)
        else:
            row["file_name"] = file_path

        for key in ("entity_name", "group_name", "group_type"):
            value = row.get(key)
            if isinstance(value, str):
                row[key] = _truncate_utf8(value, META_FIELD_LIMIT_BYTES)

        for key, max_bytes in VARCHAR_FIELD_LIMITS.items():
            value = row.get(key)
            if isinstance(value, str):
                row[key] = _truncate_utf8(value, max_bytes)

        for key, value in row.items():
            if isinstance(value, str) and len(value.encode("utf-8")) > MAX_DYNAMIC_FIELD_BYTES:
                row[key] = _truncate_utf8(value, MAX_DYNAMIC_FIELD_BYTES)

        row["vector"] = embeddings[i].tolist()
    return list_data


def upsert_prepared_rows(client: MilvusClient, collection_name: str,
                         rows: list[dict], batch_size: int,
                         timeout: Optional[float] = None,
                         flush_after_batch: bool = False) -> int:
    """提交已准备的数据，并严格校验每个请求返回的确认数和可选 flush。"""
    confirmed_count = 0
    for i in range(0, len(rows), batch_size):
        batch_data = rows[i: i + batch_size]
        result = client.upsert(
            collection_name=collection_name,
            data=batch_data,
            timeout=timeout,
        )
        upsert_count = result.get("upsert_count") if isinstance(result, Mapping) else None
        if upsert_count != len(batch_data):
            raise RuntimeError(
                f"Milvus upsert_count 异常: expected={len(batch_data)}, actual={upsert_count}"
            )
        if flush_after_batch:
            client.flush(collection_name=collection_name, timeout=timeout)
        confirmed_count += upsert_count
    return confirmed_count


def upsert_chunks(client: MilvusClient, collection_name: str,
                  data: dict, dim: int,
                  batch_size: int = DEFAULT_BATCH_SIZE,
                  timeout: Optional[float] = None,
                  flush_after_batch: bool = False) -> int:
    """upsert 逻辑快照（milvus_impl.py:1050-1173，chunks 命名空间）。

    与生产差异：
    - embedding 用随机向量直接生成（原脚本用 FakeEmbedding 异步调用）
    - 无 milvus_retry 重试装饰器 / 连接自愈（压测观察原始 timeout 行为）
    """
    rows = prepare_chunk_rows(data, dim)
    if not rows:
        return 0
    return upsert_prepared_rows(
        client,
        collection_name,
        rows,
        batch_size,
        timeout=timeout,
        flush_after_batch=flush_after_batch,
    )


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
    prepare_elapsed: float
    rpc_elapsed: float
    completed_at: float
    success: bool
    timestamp: str
    worker_id: int = 0
    error_type: Optional[str] = None
    error_code: Optional[str] = None
    error: Optional[str] = None
    timeout: bool = False


@dataclass
class WorkerErrorRecord:
    """不属于 Milvus 批请求的客户端/worker 基础设施异常。"""
    worker_id: int
    phase: str
    error_type: str
    error: str
    completed_at: float
    timestamp: str


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
    workers: list = field(default_factory=list)
    worker_errors: list = field(default_factory=list)
    concurrency: int = 1
    realtime_visible: bool = False
    elapsed: float = 0.0
    stop_reason: Optional[str] = None
    final_row_count: Optional[int] = None
    final_row_delta: Optional[int] = None
    final_verification_elapsed: float = 0.0
    final_verification_error: Optional[str] = None

    @property
    def actual_count(self) -> int:
        return sum(batch.batch_count for batch in self.batches if batch.success)

    @property
    def actual_warmup_count(self) -> int:
        return sum(batch.batch_count for batch in self.warmup_batches if batch.success)

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
        timeouts = [batch for batch in self.all_batches if not batch.success and batch.timeout]
        return min(timeouts, key=lambda batch: batch.completed_at) if timeouts else None

    @property
    def acknowledged_at_first_timeout(self) -> Optional[int]:
        first = self.first_timeout
        if first is None:
            return None
        return sum(
            batch.batch_count
            for batch in self.all_batches
            if batch.success and batch.completed_at <= first.completed_at
        )

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
class WorkerReport:
    """单个写入线程的正式阶段结果。"""
    worker_id: int
    target_count: int
    start_idx: int
    confirmed_count: int = 0
    attempted_count: int = 0
    success_batches: int = 0
    failed_batches: int = 0
    elapsed: float = 0.0
    stop_reason: Optional[str] = None

    @property
    def throughput(self) -> float:
        return self.confirmed_count / self.elapsed if self.elapsed > 0 else 0.0


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
    avg_rpc_elapsed: Optional[float]
    throughput: float
    completed_at: float
    timestamp: str
    cumulative_attempted_count: int = 0
    cumulative_warmup_confirmed: int = 0
    cumulative_success_batches: int = 0
    cumulative_failed_batches: int = 0
    cumulative_timeout_count: int = 0
    cumulative_error_types: tuple[tuple[str, int], ...] = ()
    cumulative_min_batch_elapsed: Optional[float] = None
    cumulative_avg_batch_elapsed: Optional[float] = None
    cumulative_max_batch_elapsed: Optional[float] = None
    cumulative_p50_batch_elapsed: Optional[float] = None
    cumulative_p95_batch_elapsed: Optional[float] = None
    cumulative_p99_batch_elapsed: Optional[float] = None


# ============================================================================
# CSV 延迟导出（正式计时结束后统一写入）
# ============================================================================

CSV_FIELDNAMES = [
    "record_type", "worker_id", "timestamp", "phase", "batch_number", "window_number",
    "count_before", "count_after", "cumulative_count", "batch_count",
    "batch_elapsed_seconds", "prepare_elapsed_seconds", "rpc_elapsed_seconds",
    "success", "error_type", "error_code", "error",
    "window_batch_count", "window_success_count", "window_failed_batch_count",
    "window_elapsed_seconds", "window_avg_batch_seconds",
    "window_min_batch_seconds", "window_max_batch_seconds", "window_avg_rpc_seconds",
    "window_cumulative_attempted_count", "window_cumulative_success_batches",
    "window_cumulative_failed_batches", "window_cumulative_timeout_count",
    "throughput_records_per_second", "summary_name", "summary_value",
]


class CsvResultWriter:
    """可选地导出 CSV；runtime 记录在正式计时结束后统一写入。"""

    def __init__(self, output_path: Optional[Path]):
        self.output_path = output_path
        self.file: Optional[TextIO] = None
        self.writer = None
        self.lock = threading.Lock()
        self.defer_runtime_records = True
        self.runtime_records_written = False

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
        with self.lock:
            self.writer.writerow(row)
            if flush:
                self.file.flush()

    def write_batch(self, record: BatchRecord):
        if not self.enabled or self.defer_runtime_records:
            return
        self._write_row({
            "record_type": "batch",
            "worker_id": record.worker_id,
            "timestamp": record.timestamp,
            "phase": record.phase,
            "batch_number": record.batch_number,
            "count_before": record.count_before,
            "count_after": record.count_after,
            "cumulative_count": record.cumulative_count,
            "batch_count": record.batch_count,
            "batch_elapsed_seconds": f"{record.elapsed:.6f}",
            "prepare_elapsed_seconds": f"{record.prepare_elapsed:.6f}",
            "rpc_elapsed_seconds": f"{record.rpc_elapsed:.6f}",
            "success": record.success,
            "error_type": record.error_type or "",
            "error_code": record.error_code or "",
            "error": record.error or "",
        }, flush=not record.success)

    def write_worker_error(self, record: WorkerErrorRecord):
        if not self.enabled or self.defer_runtime_records:
            return
        self._write_row({
            "record_type": "worker_error",
            "worker_id": record.worker_id,
            "timestamp": record.timestamp,
            "phase": record.phase,
            "success": False,
            "error_type": record.error_type,
            "error": record.error,
        }, flush=True)

    def write_window(self, window: WindowRecord):
        if not self.enabled or self.defer_runtime_records:
            return
        self._write_row({
            "record_type": "window",
            "worker_id": getattr(window, "worker_id", ""),
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
            "window_avg_rpc_seconds": _format_optional_float(window.avg_rpc_elapsed),
            "window_cumulative_attempted_count": window.cumulative_attempted_count,
            "window_cumulative_success_batches": window.cumulative_success_batches,
            "window_cumulative_failed_batches": window.cumulative_failed_batches,
            "window_cumulative_timeout_count": window.cumulative_timeout_count,
            "throughput_records_per_second": f"{window.throughput:.6f}",
        }, flush=True)

    def write_runtime_records(self, report: StressReport):
        """正式计时结束后按完成时间统一导出，避免文件 I/O 进入 worker 热路径。"""
        if not self.enabled or self.runtime_records_written:
            return
        events = []
        events.extend(
            (record.completed_at, 0, "batch", record) for record in report.all_batches
        )
        events.extend(
            (record.completed_at, 1, "worker_error", record)
            for record in report.worker_errors
        )
        events.extend(
            (window.completed_at, 2, "window", window) for window in report.windows
        )
        self.defer_runtime_records = False
        try:
            for _, _, event_type, record in sorted(events, key=lambda item: (item[0], item[1])):
                if event_type == "batch":
                    self.write_batch(record)
                elif event_type == "worker_error":
                    self.write_worker_error(record)
                else:
                    self.write_window(record)
        finally:
            self.defer_runtime_records = True
        self.runtime_records_written = True
        if self.file:
            self.file.flush()

    def write_worker_summary(self, worker_id: int, name: str, value):
        if not self.enabled:
            return
        self._write_row({
            "record_type": "worker_summary", "worker_id": worker_id,
            "timestamp": _now_text(), "phase": "benchmark",
            "summary_name": name, "summary_value": value,
        })

    def write_summary(self, name: str, value):
        if not self.enabled:
            return
        self._write_row({
            "record_type": "summary",
            "worker_id": "",
            "timestamp": _now_text(),
            "phase": "benchmark",
            "summary_name": name,
            "summary_value": value,
        })


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _format_optional_float(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.6f}"


def _exception_code(exc: Exception) -> Optional[str]:
    for name in ("code", "_code"):
        value = getattr(exc, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if value is not None:
            return str(value)
    return None


def _is_timeout(error: Optional[str], error_type: Optional[str] = None,
                error_code: Optional[str] = None) -> bool:
    """优先识别异常状态码，并兼容常见 gRPC / PyMilvus timeout 文本。"""
    code_text = (error_code or "").upper()
    if "DEADLINE_EXCEEDED" in code_text or code_text in {"TIMEOUT", "4"}:
        return True
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


def _get_row_count(client: MilvusClient, collection_name: str,
                   timeout: Optional[float] = None) -> int:
    stats = client.get_collection_stats(
        collection_name=collection_name,
        timeout=timeout,
    )
    try:
        return int(stats["row_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"无法解析 collection row_count: {stats!r}") from exc


def _list_partition_collections(client: MilvusClient, base_name: str,
                                timeout: Optional[float] = None) -> list[str]:
    """查找 base collection 及其按 ``_<序号>`` 命名的分片。"""
    try:
        names = client.list_collections(timeout=timeout)
    except TypeError:
        try:
            names = client.list_collections()
        except Exception:
            names = []
    except Exception:
        names = []
    if not isinstance(names, (list, tuple, set)):
        names = []
    # 某些旧版 PyMilvus 不提供可用的 list_collections，退回到连续探测。
    if not names:
        def has_collection(name: str) -> bool:
            try:
                return bool(client.has_collection(name, timeout=timeout))
            except TypeError:
                return bool(client.has_collection(name))

        names = []
        if has_collection(base_name):
            names.append(base_name)
        suffix_index = 1
        while has_collection(f"{base_name}_{suffix_index}"):
            names.append(f"{base_name}_{suffix_index}")
            suffix_index += 1
    pattern = re.compile(rf"^{re.escape(base_name)}_(\d+)$")
    suffixes = []
    for name in names:
        match = pattern.match(str(name))
        if match:
            suffixes.append((int(match.group(1)), str(name)))
    result = [base_name]
    result.extend(name for _, name in sorted(suffixes))
    return result


class CollectionRouter:
    """将一次压测的数据流拆分到多个容量受限的 collection。"""

    def __init__(self, client_factory: Callable[[], MilvusClient], base_name: str,
                 dim: int, existing: list[tuple[str, int]],
                 load_collection: bool = False,
                 capacity: int = COLLECTION_MAX_ROWS,
                 timeout: Optional[float] = None):
        self.client_factory = client_factory
        self.base_name = base_name
        self.dim = dim
        self.capacity = capacity
        self.load_collection = load_collection
        self.timeout = timeout
        self.lock = threading.RLock()
        # timeout 后整批重试时，已成功的前半段可能再次 ACK；用逻辑区间去重
        # load 触发计数，避免因重试提前把分片标记为已写满。
        self.completed_segments: set[tuple[int, int, int]] = set()
        self.collections = [
            {"name": name, "initial": count, "written": 0,
             "loaded": bool(load_collection)}
            for name, count in existing
        ]
        if not self.collections:
            raise ValueError("至少需要一个 collection 分片")
        for index, item in enumerate(self.collections):
            expected_name = self._name_for_index(index)
            if item["name"] != expected_name:
                raise ValueError(
                    f"collection 分片命名不连续: index={index}, "
                    f"actual={item['name']}, expected={expected_name}"
                )
        self.initial_total_count = sum(item["initial"] for item in self.collections)
        self.existing_partition_count = len(self.collections)

    @property
    def collection_names(self) -> list[str]:
        with self.lock:
            return [item["name"] for item in self.collections]

    def _name_for_index(self, index: int) -> str:
        return self.base_name if index == 0 else f"{self.base_name}_{index}"

    def _ensure_partition(self, client: MilvusClient, index: int) -> dict:
        with self.lock:
            while len(self.collections) <= index:
                name = self._name_for_index(len(self.collections))
                ensure_collection_ready(
                    client, name, self.dim, load_collection=False,
                    timeout=self.timeout,
                )
                self.collections.append({
                    "name": name, "initial": 0, "written": 0, "loaded": False,
                })
            return self.collections[index]

    def _locate_position(self, global_offset: int) -> tuple[int, int, int]:
        """返回 (分片序号, 分片内偏移, 该分片剩余容量)。"""
        remaining = global_offset
        # 先使用已有分片的空余位置，即使基础 collection 历史上已超过容量。
        for index, item in enumerate(self.collections[:self.existing_partition_count]):
            initial = min(max(item["initial"], 0), self.capacity)
            available = self.capacity - initial
            if remaining < available:
                offset = initial + remaining
                return index, offset, self.capacity - offset
            remaining -= available

        index = self.existing_partition_count + remaining // self.capacity
        offset = remaining % self.capacity
        return index, offset, self.capacity - offset

    def _mark_written(self, index: int, offset: int, count: int) -> bool:
        """记录已确认写入；批量导入阶段不触发 flush/load。"""
        with self.lock:
            item = self.collections[index]
            segment_key = (index, offset, count)
            if segment_key in self.completed_segments:
                return False
            self.completed_segments.add(segment_key)
            item["written"] += count
            required = max(self.capacity - item["initial"], 0)
            return False

    def upsert_rows(self, client: MilvusClient, rows: list[dict], global_start: int,
                    batch_size: int, timeout: Optional[float] = None,
                    flush_after_batch: bool = False) -> int:
        """按 collection 容量拆分 rows；global_start 是本次运行的全局序号。"""
        if not rows:
            return 0
        acknowledged = 0
        while acknowledged < len(rows):
            index, offset_in_partition, remaining_capacity = self._locate_position(
                global_start + acknowledged
            )
            segment_size = min(remaining_capacity, len(rows) - acknowledged)
            item = self._ensure_partition(client, index)
            segment = rows[acknowledged: acknowledged + segment_size]
            upsert_prepared_rows(
                client, item["name"], segment, batch_size,
                timeout=timeout if timeout is not None else self.timeout,
                flush_after_batch=flush_after_batch,
            )
            self._mark_written(index, offset_in_partition, segment_size)
            acknowledged += segment_size
        return acknowledged

    def finalize(self, client: MilvusClient) -> None:
        """全部写入完成后统一 flush 所有已触及分片。"""
        with self.lock:
            items = list(self.collections)
        for item in items:
            client.flush(collection_name=item["name"], timeout=self.timeout)

    def build_indexes(self, client: MilvusClient) -> None:
        """flush 后为所有分片创建或校验索引，并等待索引任务完成。"""
        for name in self.collection_names:
            indexes = _list_indexes_compat(client, name, self.timeout)
            if indexes:
                print(f"  ⏳ {name} 检查并补齐缺失索引...", flush=True)
            else:
                print(f"  ⏳ {name} 开始创建 HNSW/INVERTED/BITMAP 索引...", flush=True)
            # 索引可能需要数小时；不要复用普通 RPC 的 300 秒超时。
            create_chunks_indexes(
                client,
                name,
                timeout=INDEX_BUILD_TIMEOUT,
                sync=False,
            )
            self._wait_for_indexes(client, name)
            _validate_existing_indexes(client, name, timeout=self.timeout)
            print(f"  ✅ {name} 的 5 个索引均已就绪", flush=True)

    def _wait_for_indexes(self, client: MilvusClient, collection_name: str) -> None:
        deadline = (
            None
            if INDEX_BUILD_TIMEOUT is None
            else time.monotonic() + INDEX_BUILD_TIMEOUT
        )
        while True:
            descriptions = [
                _describe_index_compat(
                    client, collection_name, index_name, INDEX_BUILD_TIMEOUT
                )
                for index_name in _list_indexes_compat(
                    client, collection_name, INDEX_BUILD_TIMEOUT
                )
            ]
            states = [
                str(
                    item.get(
                        "state",
                        item.get("index_state", item.get("status", "")),
                    )
                ).lower()
                for item in descriptions
            ]
            finished_states = {"2", "3", "finished", "built", "complete", "completed"}
            if descriptions and all(
                not state or state in finished_states
                or any(token in state for token in ("finished", "built", "complete"))
                for state in states
            ):
                print(f"  ✅ {collection_name} 索引已完成", flush=True)
                return
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"等待 {collection_name} 索引完成超时")
            time.sleep(2)


# ============================================================================
# 压力测试主流程
# ============================================================================

def _build_window(window_number: int, window_start_count: int, inserted: int,
                  cumulative_base_count: int, window_start: float, window_end: float,
                  records: list[BatchRecord],
                  cumulative_records: Optional[list[BatchRecord]] = None,
                  cumulative_warmup_confirmed: int = 0) -> WindowRecord:
    """按批次完成时间构造窗口，避免输出和客户端关闭时间污染。"""
    window_elapsed = max(window_end - window_start, 0.0)
    success_records = [b for b in records if b.success]
    batch_times = [b.elapsed for b in success_records]
    rpc_times = [b.rpc_elapsed for b in success_records]
    success_count = sum(b.batch_count for b in success_records)
    cumulative_records = cumulative_records if cumulative_records is not None else records
    cumulative_success = [record for record in cumulative_records if record.success]
    cumulative_failed = [record for record in cumulative_records if not record.success]
    cumulative_times = [record.elapsed for record in cumulative_success]
    cumulative_errors = Counter(
        record.error_type or "UnknownError" for record in cumulative_failed
    )
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
        avg_rpc_elapsed=(sum(rpc_times) / len(rpc_times) if rpc_times else None),
        throughput=success_count / window_elapsed if window_elapsed > 0 else 0.0,
        completed_at=window_end,
        timestamp=_now_text(),
        cumulative_attempted_count=sum(record.batch_count for record in cumulative_records),
        cumulative_warmup_confirmed=cumulative_warmup_confirmed,
        cumulative_success_batches=len(cumulative_success),
        cumulative_failed_batches=len(cumulative_failed),
        cumulative_timeout_count=sum(record.timeout for record in cumulative_failed),
        cumulative_error_types=tuple(cumulative_errors.most_common()),
        cumulative_min_batch_elapsed=min(cumulative_times) if cumulative_times else None,
        cumulative_avg_batch_elapsed=(
            sum(cumulative_times) / len(cumulative_times) if cumulative_times else None
        ),
        cumulative_max_batch_elapsed=max(cumulative_times) if cumulative_times else None,
        cumulative_p50_batch_elapsed=_percentile(cumulative_times, 50),
        cumulative_p95_batch_elapsed=_percentile(cumulative_times, 95),
        cumulative_p99_batch_elapsed=_percentile(cumulative_times, 99),
    )


def _print_window(window: WindowRecord, report: StressReport,
                  overall_start: float) -> None:
    """输出当前窗口及截至当前时刻的正式测试累计指标。"""
    avg_text = f"{window.avg_batch_elapsed:.2f}s" if window.avg_batch_elapsed is not None else "N/A"
    min_text = f"{window.min_batch_elapsed:.2f}s" if window.min_batch_elapsed is not None else "N/A"
    max_text = f"{window.max_batch_elapsed:.2f}s" if window.max_batch_elapsed is not None else "N/A"
    rpc_text = f"{window.avg_rpc_elapsed:.2f}s" if window.avg_rpc_elapsed is not None else "N/A"

    total_elapsed = max(window.completed_at - overall_start, 0.0)
    actual_count = window.count_after
    progress = (
        min(actual_count / report.target_count * 100, 100.0)
        if report.target_count > 0 else 100.0
    )
    overall_throughput = actual_count / total_elapsed if total_elapsed > 0 else 0.0
    remaining_count = max(report.target_count - actual_count, 0)
    eta_text = (
        _format_duration(remaining_count / overall_throughput)
        if overall_throughput > 0 else "N/A"
    )

    if window.cumulative_avg_batch_elapsed is not None:
        cumulative_latency = (
            f"min {window.cumulative_min_batch_elapsed:.2f}s | "
            f"avg {window.cumulative_avg_batch_elapsed:.2f}s | "
            f"max {window.cumulative_max_batch_elapsed:.2f}s | "
            f"P50 {window.cumulative_p50_batch_elapsed:.2f}s | "
            f"P95 {window.cumulative_p95_batch_elapsed:.2f}s | "
            f"P99 {window.cumulative_p99_batch_elapsed:.2f}s"
        )
    else:
        cumulative_latency = "min N/A | avg N/A | max N/A | P50 N/A | P95 N/A | P99 N/A"

    error_text = ", ".join(
        f"{error_type}={count}"
        for error_type, count in window.cumulative_error_types
    ) or "无"
    print(
        f"   窗口 #{window.window_number} | {window.elapsed:.1f}s | "
        f"{window.batch_count} 批 | 成功 {window.success_count:,} 条 | "
        f"失败 {window.failed_batch_count} 批 | "
        f"端到端 avg {avg_text} | RPC avg {rpc_text} | min {min_text} | max {max_text} | "
        f"瞬时吞吐 {window.throughput:.0f} 条/秒\n"
        f"     累计进度 | 正式确认 {actual_count:,}/{report.target_count:,} "
        f"({progress:.2f}%) | 尝试 {window.cumulative_attempted_count:,} 条 | "
        f"含预热确认 {window.cumulative_warmup_confirmed + actual_count:,} 条 | "
        f"批次 成功 {window.cumulative_success_batches} / "
        f"失败 {window.cumulative_failed_batches}\n"
        f"     全程性能 | 已用 {_format_duration(total_elapsed)} | "
        f"累计吞吐 {overall_throughput:.2f} 条/秒 | ETA {eta_text} | "
        f"单批延迟 {cumulative_latency}\n"
        f"     异常统计 | Timeout {window.cumulative_timeout_count} 次 | "
        f"错误类型 {error_text}\n"
        f"{'=' * 70}",
        flush=True,
    )


def _partition_ranges(total_count: int, concurrency: int) -> list[tuple[int, int]]:
    """将总量尽可能平均地分成不重叠的 [start, count] 数据段。"""
    quotient, remainder = divmod(total_count, concurrency)
    result = []
    start = 0
    for worker_id in range(concurrency):
        count = quotient + (1 if worker_id < remainder else 0)
        result.append((start, count))
        start += count
    return result


class _ConcurrentState:
    """串行化共享指标更新，确保累计量和首次 timeout 快照准确。"""

    def __init__(self, report: StressReport, csv_writer: CsvResultWriter):
        self.report = report
        self.csv_writer = csv_writer
        self.lock = threading.Lock()
        self.global_stop = threading.Event()
        self.warmup_confirmed = 0
        self.benchmark_confirmed = 0
        self.benchmark_start: Optional[float] = None
        self.next_progress = REPORT_INTERVAL
        self.window_number = 1
        self.window_start = 0.0
        self.window_start_count = 0
        self.window_records: list[BatchRecord] = []
        self.window_success_count = 0
        self.benchmark_last_completion: Optional[float] = None

    def mark_benchmark_start(self) -> None:
        with self.lock:
            self.benchmark_start = time.perf_counter()
            self.window_start = self.benchmark_start
        print(
            f"\n🚀 所有 worker 预热完成，开始并发 upsert | "
            f"目标 {self.report.target_count:,} 条 | concurrency={self.report.concurrency}"
        )
        print("=" * 70)

    def add_record(self, record: BatchRecord) -> None:
        progress_message = None
        window = None
        with self.lock:
            records = self.report.warmup_batches if record.phase == "warmup" else self.report.batches
            records.append(record)
            if record.success:
                if record.phase == "warmup":
                    self.warmup_confirmed += record.batch_count
                else:
                    self.benchmark_confirmed += record.batch_count
            # 这里只表示本次运行收到 ACK 的操作量，不能与 collection 可见行数混用。
            record.cumulative_count = self.warmup_confirmed + self.benchmark_confirmed

            if record.phase == "benchmark":
                if self.benchmark_last_completion is None:
                    self.benchmark_last_completion = record.completed_at
                else:
                    self.benchmark_last_completion = max(
                        self.benchmark_last_completion, record.completed_at
                    )
                self.window_records.append(record)
                if record.success:
                    self.window_success_count += record.batch_count
                progress_message = self._progress_message(record)
                window = self._close_window(self.benchmark_last_completion)

            benchmark_confirmed = self.benchmark_confirmed

        # 文件和终端 I/O 不占用共享指标锁，也不改变窗口的时间边界。
        self.csv_writer.write_batch(record)
        if progress_message:
            print(progress_message, flush=True)
        if window is not None:
            self.csv_writer.write_window(window)
            _print_window(window, self.report, self.benchmark_start or window.completed_at)

        if not record.success:
            label = "Timeout" if record.timeout else "失败"
            print(
                f"  ❌ [worker {record.worker_id}] {label} | "
                f"批次 {record.batch_number} ({record.batch_count} 条) {record.elapsed:.2f}s | "
                f"全局确认 {benchmark_confirmed:,} | 错误: {(record.error or '')[:150]}",
                flush=True,
            )

    def add_worker_error(self, record: WorkerErrorRecord) -> None:
        with self.lock:
            self.report.worker_errors.append(record)
            if self.benchmark_start is not None and record.phase == "benchmark":
                if self.benchmark_last_completion is None:
                    self.benchmark_last_completion = record.completed_at
                else:
                    self.benchmark_last_completion = max(
                        self.benchmark_last_completion, record.completed_at
                    )
        self.csv_writer.write_worker_error(record)

    def _progress_message(self, record: BatchRecord) -> Optional[str]:
        confirmed = self.benchmark_confirmed
        if not record.success or confirmed < self.next_progress:
            return None
        elapsed = max(record.completed_at - (self.benchmark_start or record.completed_at), 0.0)
        throughput = confirmed / elapsed if elapsed > 0 else 0.0
        progress = confirmed / self.report.target_count * 100 if self.report.target_count else 100.0
        remaining = max(self.report.target_count - confirmed, 0)
        eta = remaining / throughput if throughput > 0 else 0.0
        message = (
            f"  ✅ [全局 {confirmed:,}/{self.report.target_count:,} | {progress:.2f}%] "
            f"累计速度 {throughput:.0f} 条/秒 | 已用 {_format_duration(elapsed)} | "
            f"ETA {_format_duration(eta)}"
        )
        while self.next_progress <= confirmed:
            self.next_progress += REPORT_INTERVAL
        return message

    def _close_window(self, window_end: float, force: bool = False) -> Optional[WindowRecord]:
        if not self.window_records or not self.benchmark_start:
            return None
        elapsed = max(window_end - self.window_start, 0.0)
        if not force and elapsed < WINDOW_SECONDS and self.window_success_count < WINDOW_RECORDS:
            return None
        window = _build_window(
            self.window_number, self.window_start_count, self.benchmark_confirmed,
            self.warmup_confirmed, self.window_start, window_end, self.window_records,
            cumulative_records=self.report.batches,
            cumulative_warmup_confirmed=self.warmup_confirmed,
        )
        self.report.windows.append(window)
        self.window_number += 1
        self.window_start = window_end
        self.window_start_count = self.benchmark_confirmed
        self.window_records = []
        self.window_success_count = 0
        return window

    def finish_windows(self) -> None:
        with self.lock:
            if not self.window_records:
                return
            window_end = max(record.completed_at for record in self.window_records)
            window = self._close_window(window_end, force=True)
        if window is not None:
            self.csv_writer.write_window(window)
            _print_window(window, self.report, self.benchmark_start or window.completed_at)

    def request_global_stop(self, reason: str) -> None:
        with self.lock:
            if not self.report.stop_reason:
                self.report.stop_reason = reason
            self.global_stop.set()


def _run_worker(
    worker_id: int, client_factory: Callable[[], MilvusClient], state: _ConcurrentState,
    barrier: threading.Barrier, router: CollectionRouter, dim: int, batch_size: int,
    warmup_start: int, warmup_target: int, benchmark_start_idx: int,
    benchmark_target: int, run_id: str, stop_on_timeout: bool,
    global_stop_on_timeout: bool, max_consecutive_failures: int,
    rpc_timeout: Optional[float], realtime_visible: bool,
) -> WorkerReport:
    worker = WorkerReport(worker_id, benchmark_target, benchmark_start_idx)
    client = None
    last_benchmark_completion: Optional[float] = None
    seed_material = f"{run_id}:{worker_id}".encode("utf-8")
    worker_seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "big")
    rng = np.random.default_rng(worker_seed)

    def run_phase(phase: str, start_idx: int, target: int) -> bool:
        nonlocal last_benchmark_completion
        confirmed = 0
        batch_number = 0
        consecutive_failures = 0
        while confirmed < target and not state.global_stop.is_set():
            current_size = min(batch_size, target - confirmed)
            count_before = confirmed
            started = time.perf_counter()
            prepare_elapsed = 0.0
            rpc_elapsed = 0.0
            rpc_started = None
            error = error_type = error_code = None
            try:
                data = make_batch_records(start_idx + confirmed, current_size, run_id=run_id)
                rows = prepare_chunk_rows(data, dim, rng=rng)
                prepare_elapsed = time.perf_counter() - started
                rpc_started = time.perf_counter()
                acknowledged = router.upsert_rows(
                    client,
                    rows,
                    start_idx + confirmed,
                    current_size,
                    timeout=rpc_timeout,
                    flush_after_batch=realtime_visible,
                )
                if acknowledged != current_size:
                    raise RuntimeError(
                        f"本批确认数量异常: expected={current_size}, actual={acknowledged}"
                    )
                confirmed += acknowledged
                success = True
                consecutive_failures = 0
            except Exception as exc:
                success = False
                error = str(exc)
                error_type = type(exc).__name__
                error_code = _exception_code(exc)
                consecutive_failures += 1
            completed_at = time.perf_counter()
            if rpc_started is None:
                prepare_elapsed = completed_at - started
            else:
                rpc_elapsed = completed_at - rpc_started
            timeout = not success and _is_timeout(error, error_type, error_code)
            batch_number += 1
            record = BatchRecord(
                phase=phase, batch_number=batch_number, count_before=count_before,
                count_after=confirmed, cumulative_count=0, batch_count=current_size,
                elapsed=completed_at - started, prepare_elapsed=prepare_elapsed,
                rpc_elapsed=rpc_elapsed, completed_at=completed_at, success=success,
                timestamp=_now_text(), worker_id=worker_id,
                error_type=error_type, error_code=error_code, error=error, timeout=timeout,
            )
            state.add_record(record)

            if phase == "benchmark":
                last_benchmark_completion = completed_at
                worker.attempted_count += current_size
                if success:
                    worker.confirmed_count += current_size
                    worker.success_batches += 1
                else:
                    worker.failed_batches += 1

            if timeout:
                if global_stop_on_timeout:
                    worker.stop_reason = f"{phase}_timeout_global"
                    state.request_global_stop(worker.stop_reason)
                    return False
                if stop_on_timeout:
                    worker.stop_reason = f"{phase}_timeout"
                    return False
            if consecutive_failures >= max_consecutive_failures:
                worker.stop_reason = f"{phase}_consecutive_failures"
                return False
        completed = confirmed == target
        if not completed and state.global_stop.is_set() and not worker.stop_reason:
            worker.stop_reason = "global_stop"
        return completed

    try:
        client = client_factory()
        if not run_phase("warmup", warmup_start, warmup_target):
            barrier.abort()
            state.request_global_stop(worker.stop_reason or "warmup_aborted")
            return worker
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            worker.stop_reason = worker.stop_reason or "warmup_aborted"
            return worker

        run_phase("benchmark", benchmark_start_idx, benchmark_target)
        if worker.confirmed_count == worker.target_count and not worker.stop_reason:
            worker.stop_reason = "completed"
    except Exception as exc:
        barrier.abort()
        worker.stop_reason = "client_or_worker_error"
        state.request_global_stop(worker.stop_reason)
        completed_at = time.perf_counter()
        if state.benchmark_start is not None:
            last_benchmark_completion = completed_at
        state.add_worker_error(WorkerErrorRecord(
            worker_id=worker_id,
            phase="warmup" if state.benchmark_start is None else "benchmark",
            error_type=type(exc).__name__, error=str(exc),
            completed_at=completed_at, timestamp=_now_text(),
        ))
    finally:
        if state.benchmark_start is not None and last_benchmark_completion is not None:
            worker.elapsed = max(last_benchmark_completion - state.benchmark_start, 0.0)
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    state.add_worker_error(WorkerErrorRecord(
                        worker_id=worker_id, phase="close",
                        error_type=type(exc).__name__, error=str(exc),
                        completed_at=time.perf_counter(), timestamp=_now_text(),
                    ))
                    print(f"  ⚠️  [worker {worker_id}] 关闭客户端失败: {exc}", flush=True)
    return worker


def run_stress_test(
    client_factory: Callable[[], MilvusClient], router: CollectionRouter, dim: int,
    target_count: int, batch_size: int, concurrency: int, stop_on_timeout: bool,
    global_stop_on_timeout: bool, warmup_count: int,
    max_consecutive_failures: int, run_id: str, initial_row_count: int,
    csv_writer: CsvResultWriter, rpc_timeout: Optional[float] = None,
    realtime_visible: bool = False,
) -> StressReport:
    """用独立客户端线程并发写入，并在主进程内聚合全部确认指标。"""
    report = StressReport(
        target_count=target_count, warmup_count=warmup_count, run_id=run_id,
        initial_row_count=initial_row_count, concurrency=concurrency,
        realtime_visible=realtime_visible,
    )
    state = _ConcurrentState(report, csv_writer)
    warmup_ranges = _partition_ranges(warmup_count, concurrency)
    benchmark_ranges = _partition_ranges(target_count, concurrency)
    barrier = threading.Barrier(concurrency, action=state.mark_benchmark_start)

    print(
        f"\n🔥 启动 {concurrency} 个独立客户端 worker | "
        f"预热总量 {warmup_count:,} | 正式总量 {target_count:,} | batch={batch_size}"
    )
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="milvus-writer") as executor:
        futures = []
        for worker_id in range(concurrency):
            warmup_start, worker_warmup = warmup_ranges[worker_id]
            target_start, worker_target = benchmark_ranges[worker_id]
            futures.append(executor.submit(
                _run_worker, worker_id, client_factory, state, barrier,
                router, dim, batch_size, warmup_start, worker_warmup,
                warmup_count + target_start, worker_target, run_id,
                stop_on_timeout, global_stop_on_timeout, max_consecutive_failures,
                rpc_timeout, realtime_visible,
            ))
        report.workers = [future.result() for future in futures]

    if state.benchmark_start is not None and state.benchmark_last_completion is not None:
        # 以最后一个正式阶段事件的完成时刻为终点，排除客户端关闭和汇总输出。
        report.elapsed = max(state.benchmark_last_completion - state.benchmark_start, 0.0)
    state.finish_windows()
    if not report.stop_reason:
        stopped = [w for w in report.workers if w.stop_reason not in (None, "completed")]
        report.stop_reason = stopped[0].stop_reason if stopped else None
    print(f"\n  ⏱️  正式测试墙钟总耗时: {report.elapsed:.1f}s")
    return report


def verify_final_collection_state(client_factory: Callable[[], MilvusClient],
                                  router: CollectionRouter, report: StressReport,
                                  timeout: Optional[float] = None) -> None:
    """写入结束后统一 flush、建索引并等待完成，再记录总可见行数。"""
    started = time.perf_counter()
    client = None
    try:
        client = client_factory()
        router.finalize(client)
        router.build_indexes(client)
        report.final_row_count = sum(
            _get_row_count(client, name, timeout=timeout)
            for name in router.collection_names
        )
        report.final_row_delta = report.final_row_count - report.initial_row_count
    except Exception as exc:
        report.final_verification_error = f"{type(exc).__name__}: {exc}"
    finally:
        report.final_verification_elapsed = time.perf_counter() - started
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def print_report(report: StressReport, batch_size: int,
                 csv_writer: CsvResultWriter):
    """输出最终报告，并将相同汇总追加到 CSV 末尾。"""
    csv_writer.write_runtime_records(report)
    success_times = [batch.elapsed for batch in report.success_batches]
    attempt_times = [batch.elapsed for batch in report.batches]
    success_rpc_times = [batch.rpc_elapsed for batch in report.success_batches]
    prepare_times = [batch.prepare_elapsed for batch in report.batches]
    overall_throughput = report.actual_count / report.elapsed if report.elapsed > 0 else 0.0
    all_failed = [batch for batch in report.all_batches if not batch.success]
    all_error_types = Counter(batch.error_type or "UnknownError" for batch in all_failed)
    first_to = report.first_timeout

    print("\n" + "=" * 70)
    print("  📊 并发写入压力测试汇总")
    print("=" * 70)
    print(f"  并发 worker 数: {report.concurrency}")
    print(f"  预热目标量: {report.warmup_count:,}")
    print(f"  预热已确认量: {report.actual_warmup_count:,}（不纳入以下性能统计）")
    print(f"  启动时 collection 总行数: {report.initial_row_count:,}")
    print(f"  目标写入量: {report.target_count:,}")
    print(f"  已确认写入量: {report.actual_count:,}")
    print(f"  尝试写入量: {report.attempted_count:,}（包含失败批次及重试）")
    print(f"  含预热已确认量: {report.total_actual_count:,}")
    print(f"  总批次数: {len(report.batches)}")
    print(f"  成功批次: {len(report.success_batches)}")
    print(f"  失败批次: {len(report.failed_batches)}")
    print(f"  Worker 基础设施异常: {len(report.worker_errors)}")
    if report.stop_reason:
        print(f"  终止原因: {report.stop_reason}")
    print(f"  正式测试墙钟耗时: {report.elapsed:.2f}s")
    print(f"  已确认全局吞吐量: {overall_throughput:.2f} 条/秒")
    if report.target_count == 0 and not report.batches:
        print("  提示: 正式写入目标为 0，未执行正式批次，吞吐量按 0 统计")
    print("  吞吐口径: 从全部 worker 同步启动到最后一次正式阶段事件完成，包含客户端数据准备")
    if report.realtime_visible:
        print("  成功口径: 客户端收到 upsert_count，且该批之后的 flush 已成功返回")
    else:
        print("  ACK 口径: 客户端收到 upsert_count；与 collection 可见逻辑行数分开统计")

    if report.final_row_count is not None:
        print(f"  Flush 后所有 collection 可见总行数: {report.final_row_count:,}")
        print(f"  Flush 后可见行数变化: {report.final_row_delta:+,}")
        print(f"  最终校验耗时（不纳入吞吐）: {report.final_verification_elapsed:.2f}s")
        if report.final_row_delta != report.total_actual_count:
            print("  注意: 可见行数变化与 ACK 数不同，可能存在重复主键或 timeout 未确认写入")
    elif report.final_verification_error:
        print(f"  最终可见行数校验失败: {report.final_verification_error}")

    if success_times:
        print(f"\n  📈 成功批次端到端耗时（数据准备 + RPC，单位: 秒）:")
        print(f"     min: {min(success_times):.2f}")
        print(f"     avg: {sum(success_times) / len(success_times):.2f}")
        print(f"     max: {max(success_times):.2f}")
        print(f"     P50: {_percentile(success_times, 50):.2f}")
        print(f"     P95: {_percentile(success_times, 95):.2f}")
        print(f"     P99: {_percentile(success_times, 99):.2f}")

    if attempt_times:
        print("\n  📉 全部尝试耗时（包含失败/timeout，单位: 秒）:")
        print(f"     avg: {sum(attempt_times) / len(attempt_times):.2f}")
        print(f"     P95: {_percentile(attempt_times, 95):.2f}")
        print(f"     P99: {_percentile(attempt_times, 99):.2f}")
        print(f"     max: {max(attempt_times):.2f}")
        print(
            f"     avg prepare: {sum(prepare_times) / len(prepare_times):.2f} | "
            f"avg success RPC: "
            f"{sum(success_rpc_times) / len(success_rpc_times):.2f}"
            if success_rpc_times else
            f"     avg prepare: {sum(prepare_times) / len(prepare_times):.2f} | avg success RPC: N/A"
        )

    print("\n  👥 Worker 汇总:")
    print(f"     {'ID':>4} | {'目标':>10} | {'确认':>10} | {'成功批':>7} | {'失败批':>7} | {'吞吐(条/s)':>12} | 状态")
    print("     " + "-" * 86)
    for worker in sorted(report.workers, key=lambda item: item.worker_id):
        print(
            f"     {worker.worker_id:>4} | {worker.target_count:>10,} | "
            f"{worker.confirmed_count:>10,} | {worker.success_batches:>7} | "
            f"{worker.failed_batches:>7} | {worker.throughput:>12.2f} | "
            f"{worker.stop_reason or 'unknown'}"
        )
        worker_summary = {
            "target_count": worker.target_count,
            "confirmed_count": worker.confirmed_count,
            "attempted_count": worker.attempted_count,
            "success_batches": worker.success_batches,
            "failed_batches": worker.failed_batches,
            "elapsed_seconds": f"{worker.elapsed:.6f}",
            "throughput_records_per_second": f"{worker.throughput:.6f}",
            "stop_reason": worker.stop_reason or "unknown",
        }
        for name, value in worker_summary.items():
            csv_writer.write_worker_summary(worker.worker_id, name, value)

    if all_error_types:
        print("\n  ⚠️  合并后的错误类型（含预热）:")
        for error_type, count in all_error_types.most_common():
            print(f"     {error_type}: {count} 次")
    if report.worker_errors:
        print("\n  ⚠️  Worker 基础设施异常（不计为批次）:")
        for item in report.worker_errors:
            print(f"     worker {item.worker_id} | {item.phase} | {item.error_type}: {item.error[:150]}")

    print(f"\n  ❌ Timeout 统计:")
    if first_to:
        phase_name = "预热期" if first_to.phase == "warmup" else "正式测试"
        run_confirmed = report.acknowledged_at_first_timeout or 0
        print(
            f"     首次 timeout: worker {first_to.worker_id} | {phase_name} | "
            f"按完成时间统计的本次累计 ACK {run_confirmed:,} 条"
            f"（单批端到端 {first_to.elapsed:.2f}s，RPC {first_to.rpc_elapsed:.2f}s）"
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
        "concurrency": report.concurrency,
        "realtime_visible": report.realtime_visible,
        "initial_row_count": report.initial_row_count,
        "target_count": report.target_count,
        "actual_count": report.actual_count,
        "attempted_count": report.attempted_count,
        "total_actual_count": report.total_actual_count,
        "batch_size": batch_size,
        "total_batches": len(report.batches),
        "success_batches": len(report.success_batches),
        "failed_batches": len(report.failed_batches),
        "worker_errors": len(report.worker_errors),
        "stop_reason": report.stop_reason or "completed",
        "elapsed_seconds": f"{report.elapsed:.6f}",
        "overall_throughput_records_per_second": f"{overall_throughput:.6f}",
        "throughput_scope": "end_to_end_client_workload_until_last_benchmark_event",
        "min_batch_seconds": _format_optional_float(min(success_times) if success_times else None),
        "avg_batch_seconds": _format_optional_float(
            sum(success_times) / len(success_times) if success_times else None
        ),
        "max_batch_seconds": _format_optional_float(max(success_times) if success_times else None),
        "p50_batch_seconds": _format_optional_float(_percentile(success_times, 50)),
        "p95_batch_seconds": _format_optional_float(_percentile(success_times, 95)),
        "p99_batch_seconds": _format_optional_float(_percentile(success_times, 99)),
        "all_attempt_p95_seconds": _format_optional_float(_percentile(attempt_times, 95)),
        "all_attempt_p99_seconds": _format_optional_float(_percentile(attempt_times, 99)),
        "avg_prepare_seconds": _format_optional_float(
            sum(prepare_times) / len(prepare_times) if prepare_times else None
        ),
        "avg_success_rpc_seconds": _format_optional_float(
            sum(success_rpc_times) / len(success_rpc_times) if success_rpc_times else None
        ),
        "final_row_count": report.final_row_count if report.final_row_count is not None else "",
        "final_row_delta": report.final_row_delta if report.final_row_delta is not None else "",
        "final_verification_elapsed_seconds": f"{report.final_verification_elapsed:.6f}",
        "final_verification_error": report.final_verification_error or "",
        "first_timeout_at_acknowledged_count": run_confirmed if first_to else "",
        "first_timeout_run_confirmed_count": run_confirmed if first_to else "",
        "first_timeout_worker_id": first_to.worker_id if first_to else "",
        "first_timeout_phase": first_to.phase if first_to else "",
        "first_timeout_error": first_to.error if first_to else "",
    }
    for error_type, count in all_error_types.most_common():
        summary[f"error_type_{error_type}"] = count
    for name, value in summary.items():
        csv_writer.write_summary(name, value)


# ============================================================================
# 入口
# ============================================================================

def main():
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Milvus 多客户端并发写入压力测试（独立版）")
    parser.add_argument("--target-count", "-n", type=int, default=DEFAULT_TARGET_COUNT,
                        help=f"正式统计写入量（默认 {DEFAULT_TARGET_COUNT:,}）")
    parser.add_argument("--batch-size", "-b", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"每批 upsert 条数（默认 {DEFAULT_BATCH_SIZE}，针对 1000 万写入）")
    parser.add_argument("--concurrency", "-c", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"并发写入线程数，每线程独立客户端（默认 {DEFAULT_CONCURRENCY}）")
    parser.add_argument("--stop-on-timeout", action="store_true", default=DEFAULT_STOP_ON_TIMEOUT,
                        help="timeout 后仅停止发生超时的 worker（默认关闭）")
    parser.add_argument("--no-stop-on-timeout", dest="stop_on_timeout", action="store_false",
                        help="首次 timeout 后继续，观察后续行为")
    parser.add_argument(
        "--global-stop-on-timeout", action="store_true",
        help="任一 worker timeout 后通知全部 worker 停止（在途请求会先结束）",
    )
    parser.add_argument("--env", "-e", type=str, default=None, help=".env 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只验证连接和 schema，不写入")
    parser.add_argument(
        "--load-collection",
        action="store_true",
        help="兼容旧参数；批量导入模式不会在写入阶段 load，查询请由查询脚本负责",
    )
    parser.add_argument(
        "--realtime-visible",
        action="store_true",
        help=(
            "实时可见模式：写入前 load collection，每批 upsert 后 flush；"
            "仅在 upsert 和 flush 都成功后将该批计为成功"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=f"Milvus 单次 RPC 超时秒数（默认读取 MILVUS_TIMEOUT，缺省 {MILVUS_CLIENT_TIMEOUT}）",
    )
    parser.add_argument(
        "--collection-name", type=str, default=DEFAULT_COLLECTION_NAME,
        help=(
            f"基础 collection 名称（默认 {DEFAULT_COLLECTION_NAME}）；"
            f"每 {COLLECTION_MAX_ROWS:,} 条自动创建 _1、_2 等分片"
        ),
    )
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
                        help="CSV 结果路径（默认不保存；正式计时结束后统一导出）")
    parser.add_argument("--skip-final-verification", action="store_true",
                        help="跳过计时结束后的统一 flush、建索引和可见逻辑行数校验")
    args = parser.parse_args()

    if args.target_count < 0:
        parser.error("--target-count 不能小于 0")
    if args.batch_size <= 0:
        parser.error("--batch-size 必须大于 0")
    if args.concurrency <= 0:
        parser.error("--concurrency 必须大于 0")
    if args.warmup_count < 0:
        parser.error("--warmup-count 不能小于 0")
    if args.dim <= 0:
        parser.error("--dim 必须大于 0")
    if args.max_consecutive_failures <= 0:
        parser.error("--max-consecutive-failures 必须大于 0")
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout 必须大于 0")

    if args.env:
        env_path = Path(args.env)
    else:
        env_path = script_dir / ".env"
        if not env_path.exists():
            env_path = script_dir.parent / ".env"

    if env_path.exists():
        load_environment(env_path)
        print(f"📝 已加载配置: {env_path}")
    else:
        print(f"⚠️  .env 不存在: {env_path}（将使用环境变量/默认值）")

    run_id = args.run_id or uuid.uuid4().hex[:12]
    collection_name = args.collection_name
    collection_mode = (
        "固定默认 collection"
        if collection_name == DEFAULT_COLLECTION_NAME
        else "命令行覆盖 collection"
    )
    milvus_uri = os.getenv("MILVUS_URI", MILVUS_URI)
    milvus_db_name = os.getenv("MILVUS_DB_NAME", MILVUS_DB_NAME)
    try:
        client_timeout = (
            args.timeout
            if args.timeout is not None
            else int(os.getenv("MILVUS_TIMEOUT", str(MILVUS_CLIENT_TIMEOUT)))
        )
    except ValueError:
        parser.error("MILVUS_TIMEOUT 必须是整数秒")
    if client_timeout <= 0:
        parser.error("MILVUS_TIMEOUT 必须大于 0")
    estimated_benchmark_batches = sum(
        (worker_count + args.batch_size - 1) // args.batch_size
        for _, worker_count in _partition_ranges(args.target_count, args.concurrency)
    )
    estimated_warmup_batches = sum(
        (worker_count + args.batch_size - 1) // args.batch_size
        for _, worker_count in _partition_ranges(args.warmup_count, args.concurrency)
    )

    print(f"\n{'=' * 70}")
    print(f"  🚀 Milvus 多客户端并发写入压力测试（独立版）")
    print(f"{'=' * 70}")
    print(f"  Script version: {SCRIPT_VERSION}")
    print(f"  Collection:     {collection_name}")
    print(f"  Collection 分片容量: {COLLECTION_MAX_ROWS:,} 条/collection")
    print(f"  Collection 模式: {collection_mode}")
    print(f"  Run ID:         {run_id}")
    print(f"  预热写入:        {args.warmup_count:,} 条")
    print(f"  本次目标:        {args.target_count:,} 条 upsert")
    print(f"  Batch size:     {args.batch_size}")
    print(f"  Concurrency:    {args.concurrency} 个独立客户端线程")
    print(
        f"  预计批次:        正式 {estimated_benchmark_batches:,} + "
        f"预热 {estimated_warmup_batches:,}（不含失败重试）"
    )
    print(f"  Embedding:      随机 {args.dim} 维向量")
    print(f"  Content 大小:   ~{CONTENT_TARGET_BYTES} bytes/条")
    print(f"  进度输出:        每 {REPORT_INTERVAL:,} 条")
    print(f"  窗口输出:        每 {WINDOW_SECONDS}s 或 {WINDOW_RECORDS:,} 条")
    print(f"  HNSW 参数:       M={HNSW_M}, efConstruction={HNSW_EF_CONSTRUCTION}")
    print(f"  Milvus URI:     {milvus_uri}")
    print(f"  Milvus DB:      {milvus_db_name}")
    print(f"  RPC timeout:    {client_timeout}s（显式传给初始化/写入/flush）")
    print("  Index timeout:  None（建索引及等待完成不设时间限制）")
    # 插入脚本只负责导入、flush 和建索引；load 由独立查询脚本负责。
    effective_load_collection = False
    print(
        "  Collection load: "
        + (
            "disabled（由查询脚本按需 load）"
        )
    )
    print(
        "  实时可见模式:     "
        + (
            "enabled（每批 upsert 后 flush；吞吐包含 flush 耗时）"
            if args.realtime_visible
            else "disabled（仅在计时结束后 flush）"
        )
    )
    if args.realtime_visible:
        print(
            f"  ⚠️  实时可见模式将执行约 {estimated_benchmark_batches + estimated_warmup_batches:,} 次 "
            "flush，结果不代表纯 upsert 吞吐"
        )
    print(f"  Stop on timeout: {args.stop_on_timeout}")
    print(f"  Global stop on timeout: {args.global_stop_on_timeout}")
    print(f"  Max consecutive failures: {args.max_consecutive_failures}")
    print(
        f"  CSV 输出:        {args.output or '不保存'}"
        f"{'（正式计时结束后统一写入）' if args.output else ''}"
    )
    print(
        "  Final verification: "
        + (
            "disabled"
            if args.skip_final_verification
            else "enabled（计时结束后统一 flush、建索引并等待完成；可能额外耗时较长）"
        )
    )

    print("\n🔌 连接 Milvus...")
    client_kwargs = {key: value for key, value in {
        "uri": milvus_uri,
        "user": os.getenv("MILVUS_USER"),
        "password": os.getenv("MILVUS_PASSWORD"),
        "token": os.getenv("MILVUS_TOKEN"),
        "db_name": milvus_db_name,
        "timeout": client_timeout,
    }.items() if value is not None}
    client_factory = lambda: MilvusClient(**client_kwargs)
    client = client_factory()
    print("  ✅ Milvus 连接成功")

    if args.dry_run:
        print("\n🔍 [DRY RUN] 只读验证连接、schema 和索引，不创建 collection、不写入")
        if client.has_collection(collection_name, timeout=client_timeout):
            ensure_collection_ready(
                client,
                collection_name,
                args.dim,
                load_collection=False,
                timeout=client_timeout,
            )
            print(
                f"  📊 {collection_name} 现有记录数: "
                f"{_get_row_count(client, collection_name, timeout=client_timeout):,}"
            )
        else:
            print(f"  📁 {collection_name} 不存在（dry-run 不会创建）")
        close = getattr(client, "close", None)
        if callable(close):
            close()
        return

    collection_created = ensure_collection_ready(
        client,
        collection_name,
        args.dim,
        load_collection=effective_load_collection,
        timeout=client_timeout,
    )
    # 读取已有分片，后续从最后一个未满分片继续写入；新分片按容量自动创建。
    existing_collections = []
    for existing_name in _list_partition_collections(
        client, collection_name, timeout=client_timeout
    ):
        if not client.has_collection(existing_name, timeout=client_timeout):
            continue
        if existing_name != collection_name:
            ensure_collection_ready(
                client, existing_name, args.dim,
                load_collection=effective_load_collection,
                timeout=client_timeout,
            )
        existing_collections.append((
            existing_name,
            _get_row_count(client, existing_name, timeout=client_timeout),
        ))
    if not existing_collections:
        raise RuntimeError(f"无法找到基础 collection: {collection_name}")
    router = CollectionRouter(
        client_factory=client_factory,
        base_name=collection_name,
        dim=args.dim,
        existing=existing_collections,
        load_collection=effective_load_collection,
        capacity=COLLECTION_MAX_ROWS,
        timeout=client_timeout,
    )
    initial_row_count = router.initial_total_count
    print(
        f"  📊 启动时 collection 总行数: {initial_row_count:,}"
        f"（已有 {len(existing_collections)} 个分片）"
    )
    if initial_row_count > 0:
        print("  ⚠️  collection 非空；吞吐统计仅代表本次操作，不能直接当作可见行数增量")
    if args.run_id and not collection_created and initial_row_count > 0:
        print("\n" + "!" * 70)
        print("  ⚠️  高风险提示：显式 run-id 对应的 collection 已有数据")
        print("  本次生成的主键可能与旧数据重叠，upsert 会覆盖旧行；ACK 数不等于新增行数")
        print("!" * 70)

    # 管理客户端只负责 schema/行数检查；关闭后再启动恰好 N 个写入客户端。
    close = getattr(client, "close", None)
    if callable(close):
        close()

    output_path = Path(args.output).expanduser().resolve() if args.output else None
    with CsvResultWriter(output_path) as csv_writer:
        report = run_stress_test(
            client_factory=client_factory,
            router=router,
            dim=args.dim,
            target_count=args.target_count,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
            stop_on_timeout=args.stop_on_timeout,
            global_stop_on_timeout=args.global_stop_on_timeout,
            warmup_count=args.warmup_count,
            max_consecutive_failures=args.max_consecutive_failures,
            run_id=run_id,
            initial_row_count=initial_row_count,
            csv_writer=csv_writer,
            rpc_timeout=client_timeout,
            realtime_visible=args.realtime_visible,
        )
        if not args.skip_final_verification:
            print("\n🔎 正式计时已结束，执行统一 flush、建索引及最终校验...")
            verify_final_collection_state(
                client_factory,
                router,
                report,
                timeout=client_timeout,
            )
        print_report(report, args.batch_size, csv_writer)

    if output_path:
        print(f"  💾 CSV 结果已保存: {output_path}")


if __name__ == "__main__":
    main()
