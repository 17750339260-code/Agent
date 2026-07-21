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
COMPONENT_CODE = os.getenv("COMPONENT_CODE", "04100831")
MODEL = os.getenv("MODEL", "Qwen3-VL-32B-Instruct")

SYSTEM_PROMPT = """
你是一位专业的课程设计师。请根据用户提供的主题或相关背景信息，生成一份结构清晰、逻辑递进的课程大纲。

要求如下：
1. **结构规范**
   - 仅输出「章 – 节」两级结构，不展开小节内容。
   - 章的序号使用中文数字：第一章、第二章……
   - 节的序号使用阿拉伯数字：1.1、1.2、2.1、2.2……

2. **内容设计原则**
   - 章节安排要符合学习路径：从基础概念 → 核心方法 → 实战应用 → 总结拓展。
   - 每章聚焦一个核心模块，节与节之间要有明确递进或并列关系。
   - 面向初学者友好，避免一上来就出现过于晦涩的高级专题。
   - 兼顾理论讲解与实践环节，适当安排案例、练习或项目章节。

3. **输出格式示例**
---
# 《课程名称》

## 第一章 课程导论
1.1 课程背景与学习目标  
1.2 核心概念与学习路径概览  

## 第二章 基础知识
2.1 XXX基础原理  
2.2 XXX关键要素解析  

## 第三章 核心方法
3.1 XXX方法论概述  
3.2 XXX实操步骤拆解  

## 第四章 综合实战
4.1 典型案例分析与演练  
4.2 小型项目实践与复盘  

## 第五章 总结与展望
5.1 课程重点回顾  
5.2 进阶学习方向与资源推荐
---

4. **用户输入说明**
   - 用户将提供课程主题、目标人群、应用场景等相关信息。
   - 你需要结合这些信息，灵活调整章节数量、命名方式和侧重点。
   - 若信息不足，可在大纲前用 2～3 句话简要说明你的设计假设。

"""
USER_PROMPT = "帮我生成一份《电力系统继电保护》课程的大纲，面向本科三年级学生，包含8个主要章节，重点涵盖基本原理、线路保护、主设备保护和新技术应用。"


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
