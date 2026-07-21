from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from email.utils import formatdate
from typing import Any

import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning

#多模态解析大模型 - Qwen2.5-Omni-7B AI课程开发
APP_KEY = os.getenv("APP_KEY", "1001300037")
SECRET_KEY = os.getenv("SECRET_KEY", "360ce63f5625412ba78d0aed3458b53a")
URL = os.getenv("GATEWAY_URL", "https://10.10.65.213:18300/ai-inference-gateway/predict")
COMPONENT_CODE = os.getenv("COMPONENT_CODE", "04100832")
MODEL = os.getenv("MODEL", "Qwen3-8B")

SYSTEM_PROMPT = """
角色：用户：学生或提问者，提出需要解答的题目（可能是数学、物理、化学、编程、逻辑推理等）。
AI 智能体：一位耐心的答疑老师，负责分析题目、讲解思路、展示过程，并给出最终答案和知识点总结。
背景：学生在学习过程中经常会遇到难题，需要老师一步步指导，不仅要知道答案，还要理解解题思路和相关知识点。AI需要扮演老师角色，帮助学生理解并掌握解题方法。
目标：为用户提供清晰的 逐步解题过程 + 最终答案 + 知识点总结，让用户不仅知道解法，还能学会举一反三。
要求：先识别题型（说明这是数学题/物理题/化学题/编程题/逻辑题等）。
给出解题思路（像老师讲课一样，循序渐进）。
展示完整解题过程（推导、公式计算、代码实现等）。
明确写出最终答案。
总结相关知识点，帮助用户扩展理解。
回答风格要亲切友好，避免只报答案。
案例：示例一（编程）：
用户：写一个 Java 方法，计算 n 的阶乘
AI：
题型：编程题
思路：用循环或递归计算阶乘
过程：给出循环实现代码
答案：factorial(5)=120
知识点总结：阶乘常用于排列组合，大数时需用 BigInteger。
示例二（逻辑题）：
用户：有 5 个人排队，A 不站在第一位，B 不站在最后一位，问有多少种排列？
AI：
题型：逻辑与排列组合题
思路：先算总排列，再减去不合法情况
过程：总数=5!=120，不合法情况分别计算，最后得结果
答案：96 种
知识点总结：排列组合常用减法原理处理限制条件。
"""
USER_PROMPT = "有 5 个人排队，A 不站在第一位，B 不站在最后一位，问有多少种排列？"


def make_headers(app_key: str, secret_key: str) -> dict[str, str]:
    x_date = formatdate(timeval=time.time(), localtime=False, usegmt=True)
    sign_text = f"x-date: {x_date}"
    signature = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), sign_text.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    return {
        "x-date": x_date,
        "authorization": (
            f'hmac username="{app_key}", '
            f'algorithm="hmac-sha256", '
            f'headers="x-date", '
            f'signature="{signature}"'
        ),
        "Content-Type": "application/json",
    }


def make_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "componentCode": args.component_code,
        "model": args.model,
        "messages": [
            {"role": "system", "content": args.system_prompt},
            {"role": "user", "content": args.user_prompt},
        ],
        "stream": args.stream,
    }


def pick_text(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "".join(pick_text(item) for item in data)
    if not isinstance(data, dict):
        return ""

    choices = data.get("choices")
    if isinstance(choices, list):
        text_parts: list[str] = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            message = choice.get("message") or {}
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                text_parts.append(delta["content"])
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                text_parts.append(message["content"])
            if isinstance(choice.get("text"), str):
                text_parts.append(choice["text"])
        if text_parts:
            return "".join(text_parts)

    for key in ("content", "text", "answer", "result", "output", "response", "data"):
        value = data.get(key)
        text = pick_text(value)
        if text:
            return text
    return ""


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def response_encoding(response: requests.Response) -> str:
    content_type = response.headers.get("Content-Type", "").lower()
    if "charset=" in content_type:
        return response.encoding or "utf-8"
    if "application/json" in content_type or "text/event-stream" in content_type:
        return "utf-8"
    return response.encoding or "utf-8"


def read_response(response: requests.Response, stream: bool, start: float) -> tuple[Any, int, int, float | None]:
    first_byte_ms = None
    response_bytes = 0
    stream_events = 0
    encoding = response_encoding(response)

    if not stream:
        body = b""
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            if first_byte_ms is None:
                first_byte_ms = (time.perf_counter() - start) * 1000
            response_bytes += len(chunk)
            body += chunk

        text = body.decode(encoding, errors="replace")
        return parse_json(text), response_bytes, stream_events, first_byte_ms

    raw_lines: list[str] = []
    text_parts: list[str] = []
    for raw_line in response.iter_lines(decode_unicode=False):
        if not raw_line:
            continue
        if first_byte_ms is None:
            first_byte_ms = (time.perf_counter() - start) * 1000

        response_bytes += len(raw_line)
        line = raw_line.decode(encoding, errors="replace").strip()
        raw_lines.append(line)

        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue

        stream_events += 1
        data = parse_json(line)
        text = pick_text(data)
        text_parts.append(text if text else line)

    return "".join(text_parts) if text_parts else "\n".join(raw_lines), response_bytes, stream_events, first_byte_ms


def fmt_ms(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f} ms"


def request_once(args: argparse.Namespace) -> bool:
    if args.insecure:
        urllib3.disable_warnings(InsecureRequestWarning)

    payload = make_payload(args)
    if args.print_payload:
        print("请求参数:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    start = time.perf_counter()
    response = requests.post(
        args.url,
        headers=make_headers(args.app_key, args.secret_key),
        json=payload,
        verify=not args.insecure,
        stream=True,
        timeout=args.timeout,
    )

    try:
        header_ms = (time.perf_counter() - start) * 1000
        data, response_bytes, stream_events, first_byte_ms = read_response(response, args.stream, start)
        total_ms = (time.perf_counter() - start) * 1000

        print("\n响应指标:")
        print(f"  状态码: {response.status_code}")
        print(f"  响应头耗时: {fmt_ms(header_ms)}")
        print(f"  首包耗时: {fmt_ms(first_byte_ms)}")
        print(f"  总耗时: {fmt_ms(total_ms)}")
        print(f"  响应字节数: {response_bytes}")
        print(f"  流式事件数: {stream_events}")

        print("\n响应内容:")
        if isinstance(data, (dict, list)):
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(data)

        return response.ok
    finally:
        response.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="模型网关请求脚本")
    parser.add_argument("--app-key", default=APP_KEY)
    parser.add_argument("--secret-key", default=SECRET_KEY)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--component-code", default=COMPONENT_CODE)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--system-prompt", default=SYSTEM_PROMPT)
    parser.add_argument("--user-prompt", default=USER_PROMPT)
    parser.add_argument("--stream", dest="stream", action="store_true", default=True)
    parser.add_argument("--no-stream", dest="stream", action="store_false")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--insecure", action="store_true", default=True)
    parser.add_argument("--verify-ssl", dest="insecure", action="store_false")
    parser.add_argument("--print-payload", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        sys.exit(0 if request_once(parse_args()) else 1)
    except requests.RequestException as exc:
        print(f"请求失败: {exc}", file=sys.stderr)
        sys.exit(1)
