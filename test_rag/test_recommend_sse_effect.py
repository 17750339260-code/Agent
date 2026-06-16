"""
/query/recommend/sse 接口效果测试脚本（原样调用，不做任何加工）

目的：
  接口内部已实现推荐文档的相关性过滤算法（jieba 词级分词 + IDF 覆盖率 +
  双门槛过滤）。本脚本只负责原样调用接口、原样接收返回，用 20 个 query
  观察接口真实的推荐效果，不对返回结果做任何精简、过滤或改写。

用法：
  - 跑全部 20 个 query：       python tests/test_recommend_sse_effect.py
  - 只跑某一条（从 1 开始）：  python tests/test_recommend_sse_effect.py --index 5
  - 临时自定义问题：          python tests/test_recommend_sse_effect.py --query "xxx"
  - 不保存结果：              python tests/test_recommend_sse_effect.py --no-save
"""
import sys
import os
import json
import time
import argparse
import requests


# ========================= 配置 =========================
HOST = "http://172.16.1.10:81/api/ailearngraphrag/query/recommend/sse"

API_KEY = ""        # 服务端 --key，放进请求头 X-API-Key
BEARER_TOKEN = ""   # JWT token，放进 Authorization: Bearer xxx

# 报告文档 docid（报告正文中 (***xxx***) 标记会被接口抽取为推荐文档）
DOC_ID = "doc-855dc48a829fb7873e6d131e9678b37e"
IDS = [DOC_ID]

# 围绕业务规范性监测报告设计的 20 个 query
QUERIES = [
    "该单位党组织设置方面存在哪些不规范情况？",
    "党委设置不规范的具体情况是怎样的？涉及多少个党组织？",
    "党总支设置不规范的问题表现在哪些方面？",
    "党支部设置是否存在不规范的情况？",
    "该单位在党组织换届选举方面有哪些不规范之处？",
    "党组织是否存在超期未换届的情况？具体有多少个？",
    "党员发展工作方面存在哪些不规范问题？",
    "该单位在三会一课开展方面是否规范？",
    "主题党日活动的开展情况如何？是否存在不规范？",
    "党委中心组（理论学习中心组）学习是否规范开展？",
    "党支部基本培训学习的开展情况如何？",
    "该单位在组织生活方面存在哪些异常或不规范数据？",
    "团青工作（团建）方面存在哪些不规范情况？",
    "团组织设置与党组织是否匹配？有无异常？",
    "该单位的业务量数据中是否存在明显异常？",
    "报告中提及的不规范问题按严重程度排序是怎样的？",
    "该单位党建工作整体规范性情况总体评价如何？",
    "哪些问题最需要优先整改？请给出重点问题清单？",
    "报告涵盖哪些业务规范性检查维度？分别是什么？",
    "综合来看，该单位在组织设置、党员发展、组织生活三方面哪个问题最突出？",
]

GENERATE_RECOMMENDATIONS = True
GENERATE_RECOMMENDATIONS_NUM = 5
ENABLE_THINK = True

# 流式接收（边收边打印）。--no-stream 可关闭。
STREAM = True
STREAM_CHUNK_SIZE = 1
TIMEOUT = 500
# =======================================================


def build_payload(query: str, stream: bool) -> dict:
    payload = {
        "query": query,
        "mode": "local",  # 接口内部强制改为 bypass，此处仅占位
        "ids": IDS,
        "enable_think": ENABLE_THINK,
        "generate_recommendations": GENERATE_RECOMMENDATIONS,
        "generate_recommendations_num": GENERATE_RECOMMENDATIONS_NUM,
        "stream": stream,
    }
    return payload


def build_headers() -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Accept-Encoding": "identity",  # 禁用 gzip，保证流式逐块
        "Cache-Control": "no-cache",
    }
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    if BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {BEARER_TOKEN}"
    return headers


def run_one(query: str, label: str, stream: bool) -> dict:
    url = HOST.rstrip("/") + "/query/recommend/sse"
    payload = build_payload(query, stream)
    headers = build_headers()

    print("=" * 70)
    print(f"POST {url}  {label}")
    print(f"Query: {query}")
    print("-" * 70)

    result = {
        "label": label,
        "query": query,
        "payload": payload,
        "status": "ok",
        "error": None,
        "http_status": None,
        "answer": "",
        "source_documents": None,
        "recommendations": None,
        "elapsed": None,
    }

    answer_parts = []
    state = {"source_documents": None, "recommendations": None}
    start = time.time()

    def handle_line(raw: str):
        if not raw:
            return
        if not raw.startswith("data: "):
            return
        try:
            data = json.loads(raw[6:])
        except json.JSONDecodeError:
            return
        dtype = data.get("type")
        resp = data.get("response")
        if dtype == "batch" and isinstance(resp, str):
            answer_parts.append(resp)
            if stream:
                print(resp, end="", flush=True)
        elif dtype == "extra_data" and isinstance(resp, dict):
            # 原样接收，不做任何加工
            state["source_documents"] = resp.get("source_documents")
            state["recommendations"] = resp.get("recommendations")
        elif dtype == "error":
            print(f"\n[流内错误] {data.get('error')}")

    try:
        with requests.post(url, json=payload, headers=headers,
                           stream=stream, timeout=TIMEOUT) as resp:
            result["http_status"] = resp.status_code
            if resp.status_code != 200:
                print(f"[错误] HTTP {resp.status_code}\n{resp.text}")
                result["status"] = "http_error"
                result["error"] = resp.text
                result["elapsed"] = time.time() - start
                return result
            if stream:
                for raw in resp.iter_lines(chunk_size=STREAM_CHUNK_SIZE,
                                           decode_unicode=True):
                    handle_line(raw)
            else:
                for raw in resp.text.splitlines():
                    handle_line(raw)
    except requests.exceptions.RequestException as e:
        print(f"\n[请求失败] {e}")
        result["status"] = "request_failed"
        result["error"] = str(e)
        result["elapsed"] = time.time() - start
        return result

    result["elapsed"] = time.time() - start
    result["answer"] = "".join(answer_parts)
    result["source_documents"] = state["source_documents"]
    result["recommendations"] = state["recommendations"]

    docs = result["source_documents"] or []
    print("\n" + "-" * 70)
    print(f"推荐文档（共 {len(docs)} 篇）：")
    if docs:
        for d in docs:
            print(f"  - {d.get('file_name')}")
    else:
        print("  (无)")
    print(f"总耗时: {result['elapsed']:.3f}s")
    print("=" * 70)
    return result


def parse_args():
    p = argparse.ArgumentParser(description="测试 /query/recommend/sse 接口效果")
    p.add_argument("--query", default=None, help="临时自定义问题（只跑这一条）")
    p.add_argument("--index", type=int, default=None,
                   help="只跑 QUERIES 第 N 条（从 1 开始）")
    p.add_argument("--no-save", action="store_true", help="不保存结果文档")
    p.add_argument("--no-stream", dest="stream", action="store_false",
                   default=STREAM, help="非流式接收")
    return p.parse_args()


def save_results(results: list, out_path: str):
    """原样保存接口返回结果到 Markdown。"""
    ok = [r for r in results if r["status"] == "ok"]
    lines = []
    lines.append("# /query/recommend/sse 接口效果测试结果")
    lines.append("")
    lines.append(f"- 测试时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 接口地址：`{HOST.rstrip('/')}/query/recommend/sse`")
    lines.append(f"- 文档 ids：`{IDS}`")
    lines.append(f"- 用例总数：{len(results)}，成功：{len(ok)}，"
                 f"失败：{len(results) - len(ok)}")
    lines.append("")

    # 汇总表
    lines.append("## 推荐效果汇总")
    lines.append("")
    lines.append("| # | Query | 状态 | 总耗时 | 推荐数 | 推荐文档 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for i, r in enumerate(results, 1):
        q = r["query"].replace("|", "\\|")
        docs = r.get("source_documents") or []
        names = [d.get("file_name") or "(未命名)" for d in docs]
        docs_str = "、".join(names).replace("|", "\\|") if names else "—"
        elapsed = f"{r['elapsed']:.3f}s" if r.get("elapsed") else "—"
        lines.append(f"| {i} | {q} | {r['status']} | {elapsed} | "
                     f"{len(names)} | {docs_str} |")
    lines.append("")

    # 明细
    lines.append("## 明细")
    lines.append("")
    for i, r in enumerate(results, 1):
        lines.append(f"### {i}. {r['query']}")
        lines.append("")
        lines.append(f"- 状态：`{r['status']}`"
                     + (f"（HTTP {r['http_status']}）" if r.get("http_status") else ""))
        elapsed = f"{r['elapsed']:.3f}s" if r.get("elapsed") else "—"
        lines.append(f"- 总耗时：{elapsed}")
        lines.append("")
        if r["status"] != "ok":
            lines.append("**错误信息**")
            lines.append("")
            lines.append("```")
            lines.append(str(r.get("error")))
            lines.append("```")
            lines.append("")
            continue
        lines.append("**主回答（LLM 响应）**")
        lines.append("")
        lines.append(r["answer"] or "(空)")
        lines.append("")
        docs = r.get("source_documents") or []
        lines.append(f"**推荐文档 source_documents（共 {len(docs)} 篇，接口原样返回）**")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(docs, ensure_ascii=False, indent=2) if docs else "[]")
        lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n[结果已保存] {out_path}")


def main():
    args = parse_args()
    stream = args.stream
    results = []
    if args.query is not None:
        results.append(run_one(args.query, "[自定义]", stream))
    elif args.index is not None:
        if not (1 <= args.index <= len(QUERIES)):
            print(f"--index 超范围，应为 1~{len(QUERIES)}")
            sys.exit(1)
        results.append(run_one(QUERIES[args.index - 1],
                               f"[{args.index}/{len(QUERIES)}]", stream))
    else:
        total = len(QUERIES)
        for i, q in enumerate(QUERIES, 1):
            results.append(run_one(q, f"[{i}/{total}]", stream))
            time.sleep(0.5)

    if not args.no_save and results:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(
            out_dir, f"recommend_sse_效果_{time.strftime('%Y%m%d_%H%M%S')}.md")
        save_results(results, out_path)


if __name__ == "__main__":
    main()
