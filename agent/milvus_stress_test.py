#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Milvus 压力测试脚本 - 复现生产环境 upsert timeout 问题（独立版）

本脚本不依赖 lightrag 项目代码，仅依赖: pymilvus、numpy、python-dotenv。
schema / HNSW 索引 / 截断逻辑为 lightrag.kg.milvus_impl.py (chunks 命名空间) 的复刻快照。

实验目标：
    验证 "大表 upsert 触发 Milvus 服务端 30 秒内部 RPC timeout" 假设。
    通过持续 upsert 撑大表，观察单批耗时随表大小增长的趋势，找出 timeout 拐点。

实验设计：
    1. 复刻生产 chunks collection 的 schema / 索引 / 截断逻辑（快照，不随生产代码更新）
    2. 随机 1024 维向量代替真实 embedding（避免模型限流/超时干扰实验）
    3. 持续 upsert，每批 50 条（与生产 upsert_batch_size 一致）
    4. 每写入 1000 条记录单批耗时
    5. 检测 "message send timeout"，记录首次出现时的累计写入量

用法：
    # 先小规模试跑，验证连接和 schema 没问题
    python milvus_stress_test.py --target-count 10000

    # 正式跑到 50 万
    python milvus_stress_test.py --target-count 500000

    # 跑到 100 万，首次 timeout 后继续（观察后续是否稳定 timeout）
    python milvus_stress_test.py --target-count 1000000 --no-stop-on-timeout

    # 只验证连接和 schema
    python milvus_stress_test.py --dry-run
"""

import os
import time
import hashlib
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from dotenv import load_dotenv
from pymilvus import MilvusClient, FieldSchema, CollectionSchema, DataType


# ============================================================================
# 配置常量（与生产 milvus_impl.py chunks 命名空间对齐的快照）
# ============================================================================

DEFAULT_WORKSPACE = "stress_test"        # 隔离 workspace，不污染生产
MILVUS_URI = "http://192.168.0.225:30119"  # Milvus 2.6.21 测试集群
MILVUS_DB_NAME = "default"
EMBEDDING_DIM = 1024                     # bge-m3 维度
DEFAULT_BATCH_SIZE = 50                  # 生产 upsert_batch_size
CONTENT_TARGET_BYTES = 45_000            # 接近生产 max_dynamic_field_bytes=50000
REPORT_INTERVAL = 1000                   # 每写入 1000 条输出一次进度

GRAPH_FIELD_SEP = "<SEP>"                # lightrag/constants.py 快照
CHUNKS_FILE_LIMIT_BYTES = 1024           # chunks 命名空间 file_path/file_name 字节上限
META_FIELD_LIMIT_CHARS = 500             # entity_name/group_name/group_type 字符上限
MAX_DYNAMIC_FIELD_BYTES = 50_000         # 动态字段字节兜底上限

# 与生产 MilvusVectorDBStorage 初始化时的 meta_fields 一致
META_FIELDS = {
    "full_doc_id", "content", "group_id", "group_name", "group_type",
    "doc_status", "file_path", "file_url", "file_name", "chunk_order_index",
    "created_at",
}


# ============================================================================
# 数据生成（字段与生产 recover_chunks 一致）
# ============================================================================

_BASE_CONTENT = "这是压力测试用的填充内容，用于模拟生产环境的 chunk 文本。" * 20


def make_content(target_bytes: int = CONTENT_TARGET_BYTES, suffix: str = "") -> str:
    """生成接近 target_bytes 字节的 content，加 suffix 确保每条唯一"""
    content = _BASE_CONTENT
    while len(content.encode("utf-8")) < target_bytes:
        content += _BASE_CONTENT
    return content.encode("utf-8")[:target_bytes].decode("utf-8", "ignore") + suffix


def make_batch_records(start_idx: int, batch_size: int) -> dict:
    """生成一批测试记录，字段与生产 recover_chunks 完全一致"""
    dict_data = {}
    for i in range(batch_size):
        idx = start_idx + i
        # 每条 content 加唯一后缀，确保 md5 主键唯一（避免覆盖更新）
        content = make_content(suffix=f"__idx_{idx}__")
        chunk_id = "ch-" + hashlib.md5(content.encode()).hexdigest()
        doc_id = f"stress_doc_{idx // 100:08d}"

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
# Milvus chunks collection 复刻（schema / 索引 / upsert 截断逻辑）
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
    """索引创建快照（milvus_impl.py:414-542，chunks 分支 + 公共字段）"""
    index_params = client.prepare_index_params()

    index_params.add_index(
        field_name="vector",
        index_type="HNSW",
        metric_type="COSINE",
        params={"M": 16, "efConstruction": 256},
    )
    index_params.add_index(field_name="full_doc_id", index_type="INVERTED")
    index_params.add_index(field_name="group_id", index_type="INVERTED")
    index_params.add_index(field_name="group_type", index_type="INVERTED")
    index_params.add_index(field_name="doc_status", index_type="BITMAP")

    client.create_index(collection_name=collection_name, index_params=index_params)


def ensure_collection_ready(client: MilvusClient, collection_name: str, dim: int) -> bool:
    """检查/创建 collection + 索引，并确保已 load（返回是否新建）"""
    if client.has_collection(collection_name):
        client.load_collection(collection_name)
        print(f"  ✅ 使用现有 collection: {collection_name}（已 load）")
        return False

    client.create_collection(
        collection_name=collection_name, schema=create_chunks_schema(dim)
    )
    create_chunks_indexes(client, collection_name)
    client.load_collection(collection_name)
    print(f"  ✅ 新建 collection: {collection_name}（schema + HNSW/INVERTED/BITMAP 索引已创建并 load）")
    return True


def upsert_chunks(client: MilvusClient, collection_name: str,
                  data: dict, dim: int, batch_size: int = 50) -> None:
    """upsert 逻辑快照（milvus_impl.py:1050-1173，chunks 命名空间）。

    与生产差异：
    - embedding 用随机向量直接生成（原脚本用 FakeEmbedding 异步调用）
    - 无 milvus_retry 重试装饰器 / 连接自愈（压测观察原始 timeout 行为）
    """
    # 过滤掉内容为空的数据
    data = {k: v for k, v in data.items() if v.get("content") and v["content"].strip()}
    if not data:
        return

    client.load_collection(collection_name)
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
        file_path = d.get("file_path", "")
        while len(file_path.encode("utf-8")) > CHUNKS_FILE_LIMIT_BYTES:
            last_sep_index = file_path.rfind(GRAPH_FIELD_SEP)
            if last_sep_index == -1:
                file_path = file_path.encode("utf-8")[:CHUNKS_FILE_LIMIT_BYTES].decode("utf-8", "ignore")
                break
            file_path = file_path[:last_sep_index]
        d["file_path"] = file_path

        if "file_name" in d:
            file_name = d["file_name"]
            if len(file_name.encode("utf-8")) > CHUNKS_FILE_LIMIT_BYTES:
                d["file_name"] = file_name.encode("utf-8")[:CHUNKS_FILE_LIMIT_BYTES].decode("utf-8", "ignore")
        else:
            d["file_name"] = file_path

        # entity_name/group_name/group_type 按 500 字符截断
        for key in ("entity_name", "group_name", "group_type"):
            val = d.get(key)
            if isinstance(val, str) and len(val) > META_FIELD_LIMIT_CHARS:
                d[key] = val[:META_FIELD_LIMIT_CHARS]

        # 全局兜底：所有字符串字段按 50000 字节截断（动态字段限制）
        for k, v in d.items():
            if isinstance(v, str):
                v_bytes = v.encode("utf-8")
                if len(v_bytes) > MAX_DYNAMIC_FIELD_BYTES:
                    d[k] = v_bytes[:MAX_DYNAMIC_FIELD_BYTES].decode("utf-8", "ignore")

        d["vector"] = embeddings[i].tolist()

    # 分批 upsert，避免 gRPC 消息大小限制（64MB）
    total_count = len(list_data)
    for i in range(0, total_count, batch_size):
        batch_data = list_data[i: i + batch_size]
        client.upsert(collection_name=collection_name, data=batch_data)


# ============================================================================
# 报告数据结构
# ============================================================================

@dataclass
class BatchRecord:
    """单批次记录"""
    count_before: int          # 该批写入前的累计量
    count_after: int           # 该批写入后的累计量
    elapsed: float             # 单批耗时（秒）
    success: bool              # 是否成功
    error: Optional[str] = None  # 失败时的错误信息


@dataclass
class StressReport:
    """压力测试报告"""
    target_count: int
    batches: list = field(default_factory=list)  # list[BatchRecord]

    @property
    def actual_count(self) -> int:
        return self.batches[-1].count_after if self.batches else 0

    @property
    def first_timeout(self) -> Optional[BatchRecord]:
        """首次出现 timeout 的批次"""
        for b in self.batches:
            if not b.success and b.error and "timeout" in b.error.lower():
                return b
        return None

    @property
    def success_batches(self) -> list:
        return [b for b in self.batches if b.success]


# ============================================================================
# 压力测试主流程
# ============================================================================

def run_stress_test(client, collection_name, dim, target_count: int, batch_size: int,
                    stop_on_timeout: bool) -> StressReport:
    """持续 upsert 撑大表，记录每批耗时"""
    report = StressReport(target_count=target_count)
    overall_start = time.time()
    inserted = 0

    # 最近一个汇总窗口内的单批耗时，用于每 1000 条汇总输出
    recent_batch_times: list[float] = []

    print(f"\n🚀 开始持续 upsert | 目标 {target_count:,} 条 | batch={batch_size}")
    print("=" * 70)

    while inserted < target_count:
        dict_data = make_batch_records(inserted, batch_size)
        t0 = time.time()
        success = True
        error_msg = None
        try:
            upsert_chunks(client, collection_name, dict_data, dim, batch_size=batch_size)
            inserted += batch_size
        except Exception as e:
            success = False
            error_msg = str(e)
        elapsed = time.time() - t0

        record = BatchRecord(
            count_before=inserted - (batch_size if success else 0),
            count_after=inserted,
            elapsed=elapsed,
            success=success,
            error=error_msg,
        )
        report.batches.append(record)
        recent_batch_times.append(elapsed)

        # 失败立即输出（不等到 1000 条汇总）
        if not success:
            total_elapsed = time.time() - overall_start
            speed = inserted / total_elapsed if total_elapsed > 0 else 0
            print(
                f"  ❌ [{inserted:,}/{target_count:,}] "
                f"单批({batch_size}条) {elapsed:.2f}s | 累计速度 {speed:.0f} 条/秒 | "
                f"错误: {error_msg[:150]}",
                flush=True
            )
        # 每 1000 条汇总输出一次：min/avg/max 单批耗时
        elif inserted % REPORT_INTERVAL == 0 and recent_batch_times:
            total_elapsed = time.time() - overall_start
            speed = inserted / total_elapsed if total_elapsed > 0 else 0
            times = recent_batch_times
            print(
                f"  ✅ [{inserted:,}/{target_count:,}] "
                f"近 {len(times)} 批({batch_size}条/批) | "
                f"min {min(times):.2f}s | avg {sum(times)/len(times):.2f}s | max {max(times):.2f}s | "
                f"累计速度 {speed:.0f} 条/秒",
                flush=True
            )
            recent_batch_times = []

        # 首次 timeout 处理
        if not success and error_msg and "timeout" in error_msg.lower():
            if stop_on_timeout:
                print(f"\n  ⛔ 首次 timeout @ 累计 {inserted:,} 条（单批 {elapsed:.2f}s）")
                print(f"     错误: {error_msg[:200]}")
                break
            else:
                print(f"  ⚠️  Timeout @ {inserted:,}，继续测试...")

    overall_elapsed = time.time() - overall_start
    print(f"\n  ⏱️  总耗时: {overall_elapsed:.1f}s")
    return report


def print_report(report: StressReport, batch_size: int):
    """输出最终报告"""
    print("\n" + "=" * 70)
    print("  📊 压力测试汇总")
    print("=" * 70)
    print(f"  目标写入量: {report.target_count:,}")
    print(f"  实际写入量: {report.actual_count:,}")
    print(f"  总批次数: {len(report.batches)}")
    print(f"  成功批次: {len(report.success_batches)}")
    print(f"  失败批次: {len(report.batches) - len(report.success_batches)}")

    # 单批耗时统计（仅成功批次）
    success_times = [b.elapsed for b in report.success_batches]
    if success_times:
        print(f"\n  📈 单批耗时统计（仅成功批次，单位: 秒）:")
        print(f"     min: {min(success_times):.2f}")
        print(f"     avg: {sum(success_times) / len(success_times):.2f}")
        print(f"     max: {max(success_times):.2f}")

    # 按累计写入量分段统计（每 10 万条一段）
    if report.success_batches:
        segment_size = 100_000
        print(f"\n  📈 按累计写入量分段（每 {segment_size:,} 条一段）:")
        print(f"     {'区间':<22} | {'批次数':<8} | {'avg(s)':<8} | {'max(s)':<8}")
        print(f"     " + "-" * 60)
        for seg_start in range(0, report.actual_count + segment_size, segment_size):
            seg_end = seg_start + segment_size
            seg_batches = [
                b for b in report.success_batches
                if seg_start <= b.count_after <= seg_end
            ]
            if seg_batches:
                times = [b.elapsed for b in seg_batches]
                avg_t = sum(times) / len(times)
                max_t = max(times)
                print(f"     {seg_start:>9,}-{seg_end:>9,} | {len(seg_batches):>6} | {avg_t:>6.2f} | {max_t:>6.2f}")

    # timeout 统计
    first_to = report.first_timeout
    print(f"\n  ❌ Timeout 统计:")
    if first_to:
        print(f"     首次 timeout: 累计 {first_to.count_after:,} 条时（单批 {first_to.elapsed:.2f}s）")
        print(f"     错误信息: {first_to.error[:200] if first_to.error else 'N/A'}")
    else:
        print(f"     未出现 timeout")

    print("\n" + "=" * 70)


# ============================================================================
# 入口
# ============================================================================

def main():
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Milvus 压力测试 - 复现 upsert timeout（独立版，不依赖 lightrag）")
    parser.add_argument("--target-count", "-n", type=int, default=1_000_000,
                        help="目标累计写入量（默认 100 万）")
    parser.add_argument("--batch-size", "-b", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"每批 upsert 条数（默认 {DEFAULT_BATCH_SIZE}，与生产一致）")
    parser.add_argument("--stop-on-timeout", action="store_true", default=True,
                        help="首次 timeout 后停止（默认开启）")
    parser.add_argument("--no-stop-on-timeout", dest="stop_on_timeout", action="store_false",
                        help="首次 timeout 后继续，观察后续行为")
    parser.add_argument("--env", "-e", type=str, default=None, help=".env 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只验证连接和 schema，不写入")
    parser.add_argument("--workspace", type=str, default=DEFAULT_WORKSPACE,
                        help=f"workspace 名称（默认 {DEFAULT_WORKSPACE}，collection 名为 <workspace>_chunks）")
    parser.add_argument("--dim", type=int, default=EMBEDDING_DIM,
                        help=f"向量维度（默认 {EMBEDDING_DIM}）")
    args = parser.parse_args()

    # 加载 .env：--env 指定 > 脚本同目录 .env > 上级目录 .env
    if args.env:
        env_path = Path(args.env)
    else:
        env_path = script_dir / ".env"
        if not env_path.exists():
            env_path = script_dir.parent.parent / ".env"

    if env_path.exists():
        load_dotenv(dotenv_path=str(env_path), override=True)
        print(f"📝 已加载配置: {env_path}")
    else:
        print(f"⚠️  .env 不存在: {env_path}（将使用环境变量/默认值）")

    collection_name = f"{args.workspace}_chunks"

    # 打印实验配置
    print(f"\n{'=' * 70}")
    print(f"  🚀 Milvus 压力测试 - 复现 upsert timeout（独立版）")
    print(f"{'=' * 70}")
    print(f"  Workspace:      {args.workspace}")
    print(f"  Collection:     {collection_name}")
    print(f"  目标累计:        {args.target_count:,} 条")
    print(f"  Batch size:     {args.batch_size}")
    print(f"  Embedding:      随机 {args.dim} 维向量")
    print(f"  Content 大小:   ~{CONTENT_TARGET_BYTES} bytes/条")
    print(f"  Milvus URI:     {MILVUS_URI}")
    print(f"  Milvus DB:      {MILVUS_DB_NAME}")
    print(f"  Stop on timeout: {args.stop_on_timeout}")

    # 连接 Milvus
    print("\n🔌 连接 Milvus...")
    client = MilvusClient(
        uri=MILVUS_URI,
        user=os.getenv("MILVUS_USER"),
        password=os.getenv("MILVUS_PASSWORD"),
        token=os.getenv("MILVUS_TOKEN"),
        db_name=MILVUS_DB_NAME,
        timeout=int(os.getenv("MILVUS_TIMEOUT", "120")),
    )
    print("  ✅ Milvus 连接成功")

    # 检查/创建 collection + 索引
    ensure_collection_ready(client, collection_name, args.dim)

    # dry-run 模式：只验证连接和 schema，不写入
    if args.dry_run:
        print("\n🔍 [DRY RUN] 只验证连接和 schema，不写入")
        if client.has_collection(collection_name):
            stats = client.get_collection_stats(collection_name)
            print(f"  📊 {collection_name} 现有记录数: {stats.get('row_count', 'unknown')}")
        else:
            print(f"  📁 {collection_name} 不存在（首次跑会自动创建）")
        return

    # 跑压力测试
    report = run_stress_test(
        client=client,
        collection_name=collection_name,
        dim=args.dim,
        target_count=args.target_count,
        batch_size=args.batch_size,
        stop_on_timeout=args.stop_on_timeout,
    )

    # 输出报告
    print_report(report, args.batch_size)


if __name__ == "__main__":
    main()
