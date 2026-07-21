# -*- coding: utf-8 -*-
"""Qwen3-8B 全场景阶梯并发性能测试脚本。

默认执行 4K/32K、流式/非流式四种场景，并依次提升并发数。脚本不依赖本地
模型或 tokenizer，使用离线保守估算构造目标长度的输入 Prompt；每个梯度持续
完整发压，不会因为首个失败样本提前终止，以保证统计窗口一致。

主要指标口径：
1. 成功率：HTTP 成功、无业务错误且存在非空模型输出的请求占比。
2. 成功/总 QPS：请求数除以首个请求开始至最后一个请求结束的有效区间。
3. 输出吞吐：成功请求 completion token 总数除以相同有效区间。
4. 成功延迟与全量延迟分别统计；百分位采用 nearest-rank 方法。
5. 饱和判断综合成功率、成功 QPS、输出吞吐和全量 P95 延迟变化。
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import csv
import hashlib
import hmac
import json
import math
import os
import queue
import re
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path
from typing import Any, Optional

import requests
import urllib3
from requests import Response, Session
from requests.exceptions import RequestException
from urllib3.exceptions import InsecureRequestWarning

APP_KEY = os.getenv("APP_KEY", "1001300037")
SECRET_KEY = os.getenv("SECRET_KEY", "360ce63f5625412ba78d0aed3458b53a")
URL = os.getenv(
    "GATEWAY_URL",
    "https://10.10.65.213:18300/ai-inference-gateway/predict",
)
COMPONENT_CODE = os.getenv("COMPONENT_CODE", "04100831")
MODEL = os.getenv("MODEL", "Qwen3-VL-32B-Instruct")

CONCURRENCY_LEVELS = (1, 5, 10, 15, 20, 25, 30, 40)
DEFAULT_DURATION_SECONDS = 180
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30
DEFAULT_READ_TIMEOUT_SECONDS = 300
REPORT_HEADERS = (
    "上下文窗口目标(tokens)",
    "估算输入Prompt(tokens)",
    "响应模式",
    "并发数",
    "请求数",
    "成功数",
    "失败数",
    "成功率(%)",
    "发压窗口(s)",
    "有效统计区间(s)",
    "总请求QPS",
    "成功请求QPS",
    "成功请求平均响应时间(s)",
    "成功请求P95响应时间(s)",
    "成功请求P99响应时间(s)",
    "全量平均响应时间(s)",
    "全量P95响应时间(s)",
    "全量P99响应时间(s)",
    "平均首Token延迟(s)",
    "P95首Token延迟(s)",
    "TTFT覆盖率(%)",
    "生成吞吐量(tokens/s)",
    "Token统计覆盖率(%)",
    "当前梯度是否饱和",
    "饱和判断依据",
)

CONTEXT_TOKEN_TARGETS = {"4k": 4096, "32k": 32768}
CONTEXT_LABELS = {"4k": "4K", "32k": "32K"}
MODE_LABELS = {"stream": "流式输出", "non-stream": "非流式输出"}
FULL_SCENARIO_MATRIX = (
    ("4k", "stream"),
    ("4k", "non-stream"),
    ("32k", "stream"),
    ("32k", "non-stream"),
)

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
PLACEHOLDER_PARAGRAPH = """
《电力系统继电保护》是电气工程专业的核心课程，面向已学习电路、电机学、电力系统分析
和短路计算的本科三年级学生。课程应帮助学生理解继电保护在电力系统中的作用，
建立“故障特征分析—保护原理选择—整定配合—动作结果评价”的完整学习路径。
课程内容要同时体现选择性、速动性、灵敏性和可靠性等基本要求及其相互制约关系。

基础部分应介绍电力系统正常、异常与故障状态，说明保护装置、断路器、电流互感器、
电压互感器以及二次回路之间的配合关系。学生需要掌握三段式电流保护、方向性元件、
阻抗测量、差动原理和零序分量等核心概念，能够根据系统运行方式、短路类型与故障位置
判断电流、电压和阻抗的变化特征，并理解主保护、后备保护及远后备保护的分工。

线路保护是课程的重点。对辐射形线路，应由阶段式电流保护过渡到方向电流保护，
突出动作电流、动作时限和灵敏度校验的整定思路。对高压输电线路，应讲解距离保护的
测量阻抗、动作特性、振荡闭锁和电压回路断线闭锁，再引入线路纵联差动保护、方向纵联
保护与光纤通信通道，形成从单端电气量判断到双端信息协同判断的认知递进。

主设备保护应覆盖电力变压器、同步发电机、母线和电动机等对象。变压器保护需要
说明纵联差动保护、励磁涌流识别、差流速断、复合电压启动过电流保护以及瓦斯保护；
发电机保护需覆盖定子绕组短路、转子接地、失磁、逆功率和过负荷等异常运行工况；
母线保护应强调差动原理、电流互感器饱和影响、复式比率制动以及断路器失灵保护的配合。

自动重合闸与备用电源自动投入可作为保护后处理和系统恢复内容，通过瞬时性故障、
永久性故障及单相重合闸案例，说明保护动作、断路器跳闸、重合闸与后加速之间的
时序关系。实践环节可包括二次回路识图、保护定值计算、动作特性绘制、故障录波分析、
装置试验与典型误动或拒动案例复盘，使学生能将原理、整定和工程应用联系起来。

新技术应用应在完成传统保护原理学习后展开，可包括微机继电保护的采样与数字滤波、
IEC 61850通信体系、智能变电站、电子式互感器、行波保护与故障测距、广域保护、
自适应保护以及人工智能辅助故障识别。课程应客观区分已广泛应用的工程技术与仍在
发展中的研究方向，同时关注新能源大规模并网、电力电子化和交直流混联系统对传统
保护判据带来的挑战。

八个主要章节的设计应遵循从概述与基础原理，到线路保护、主设备保护、自动装置与系统恢复，
再到数字化和智能化新技术的学习路径。每章应聚焦一个核心模块，章内各节保持明确的递进或
并列关系，并适当安排定值计算、特性分析、工程案例或综合实训。最终大纲只使用“章—节”
两级结构，不展开更细的小节内容，但要使章节名称准确反映本科三年级学生的知识基础、
培养目标和电力行业的实际应用需求。
""".strip()


@dataclass(frozen=True)
class RequestResult:
    """单个请求的采样结果。"""

    success: bool
    start_time_s: float
    end_time_s: float
    response_time_s: float
    ttft_s: Optional[float]
    completion_tokens: Optional[int]
    prompt_tokens: Optional[int] = None
    token_count_source: str = ""
    status_code: Optional[int] = None
    error: str = ""


@dataclass(frozen=True)
class ParsedResponse:
    """响应解析结果；error 非空表示业务或内容校验失败。"""

    output_text: str
    completion_tokens: Optional[int]
    token_count_source: str
    ttft_s: Optional[float]
    end_time_s: float
    error: str = ""
    prompt_tokens: Optional[int] = None


@dataclass(frozen=True)
class StepMetrics:
    """一个并发梯度的汇总指标。"""

    concurrency: int
    attempted: int
    succeeded: int
    failed: int
    success_rate: float
    average_response_time_s: Optional[float]
    p95_response_time_s: Optional[float]
    p99_response_time_s: Optional[float]
    all_average_response_time_s: Optional[float]
    all_p95_response_time_s: Optional[float]
    all_p99_response_time_s: Optional[float]
    average_ttft_s: Optional[float]
    p95_ttft_s: Optional[float]
    ttft_coverage: Optional[float]
    total_qps: float
    success_qps: float
    token_throughput: Optional[float]
    token_count_coverage: float
    pressure_duration_s: float
    effective_duration_s: float
    wall_duration_s: float
    errors: dict[str, int]


def make_headers(app_key: str, secret_key: str) -> dict[str, str]:
    """为每个请求生成新的 HMAC 鉴权头。"""

    x_date = formatdate(timeval=time.time(), localtime=False, usegmt=True)
    sign_text = f"x-date: {x_date}"
    signature = base64.b64encode(
        hmac.new(
            secret_key.encode("utf-8"),
            sign_text.encode("utf-8"),
            hashlib.sha256,
        ).digest()
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
        "Accept": "text/event-stream, application/json",
    }


def estimate_text_tokens(text: str) -> int:
    """离线保守估算：非 ASCII 字符按 1 token，ASCII 按约 4 字符/token。"""

    token_count = 0
    for part in re.findall(r"[\x00-\x7f]+|[^\x00-\x7f]", text):
        if part.isascii():
            compact_length = len(part.strip())
            if compact_length:
                token_count += math.ceil(compact_length / 4)
        else:
            token_count += 1
    return token_count


def estimate_prompt_tokens(context_text: str) -> int:
    """估算 system/user 消息和聊天模板的完整输入长度。"""

    return (
            estimate_text_tokens(SYSTEM_PROMPT)
            + estimate_text_tokens(context_text)
            + 16
    )


def build_chinese_context(
        context: str,
        output_token_reserve: int = 0,
) -> tuple[str, int]:
    """保留固定问题，并用背景材料填充至估算的 4K/32K 输入长度。"""

    window_target = CONTEXT_TOKEN_TARGETS[context]
    prompt_target = window_target - output_token_reserve
    if prompt_target <= 0:
        raise ValueError("输出 token 预留必须小于上下文窗口目标")

    cache: dict[int, int] = {}

    def text_for_chars(char_count: int) -> str:
        repeated = PLACEHOLDER_PARAGRAPH * math.ceil(
            max(char_count, 1) / len(PLACEHOLDER_PARAGRAPH)
        )
        filler = repeated[:char_count]
        if not filler:
            return USER_PROMPT
        return f"背景材料：\n{filler}\n\n问题：{USER_PROMPT}"

    def tokens_for_chars(char_count: int) -> int:
        if char_count not in cache:
            cache[char_count] = estimate_prompt_tokens(text_for_chars(char_count))
        return cache[char_count]

    low = 0
    high = max(prompt_target * 2, 1024)
    while tokens_for_chars(high) < prompt_target:
        high *= 2

    while low < high:
        middle = (low + high) // 2
        if tokens_for_chars(middle) < prompt_target:
            low = middle + 1
        else:
            high = middle

    boundary = low
    candidates = range(max(0, boundary - 8), boundary + 9)
    valid = [
        (tokens_for_chars(char_count), char_count)
        for char_count in candidates
        if tokens_for_chars(char_count) <= prompt_target
    ]
    if not valid:
        raise RuntimeError(f"无法构造不超过 {prompt_target} tokens 的输入 Prompt")

    actual_tokens, best_chars = max(valid, key=lambda item: (item[0], item[1]))
    return text_for_chars(best_chars), actual_tokens


def build_payload(
        args: argparse.Namespace,
        context_text: str,
        stream: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "componentCode": args.component_code,
        "model": args.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context_text},
        ],
        "stream": stream,
    }
    if args.max_tokens is not None:
        payload["max_tokens"] = args.max_tokens
    if stream and args.stream_include_usage:
        payload["stream_options"] = {"include_usage": True}
    return payload


def content_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content"):
                    if isinstance(item.get(key), str):
                        parts.append(item[key])
                        break
        return "".join(parts)
    return ""


def extract_choice_text(data: Any, prefer_delta: bool) -> str:
    """提取 choices 文本，并避免同时累计 delta/message 导致重复。"""

    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list):
        return ""

    parts: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        ordered_keys = ("delta", "message") if prefer_delta else ("message", "delta")
        selected = ""
        for key in ordered_keys:
            nested = choice.get(key)
            if not isinstance(nested, dict):
                continue
            nested_parts = []
            for field in ("reasoning_content", "reasoning", "content"):
                text = content_to_text(nested.get(field))
                if text:
                    nested_parts.append(text)
            if nested_parts:
                selected = "".join(nested_parts)
                break
        if not selected:
            selected = content_to_text(choice.get("text"))
        if selected:
            parts.append(selected)
    return "".join(parts)


def extract_non_stream_text(data: Any) -> str:
    choice_text = extract_choice_text(data, prefer_delta=False)
    if choice_text:
        return choice_text
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "".join(extract_non_stream_text(item) for item in data)
    if not isinstance(data, dict):
        return ""
    for key in ("content", "text", "answer", "result", "output", "response", "data"):
        text = extract_non_stream_text(data.get(key))
        if text:
            return text
    return ""


def to_int(value: Any) -> Optional[int]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return None
    return converted if converted >= 0 else None


def find_usage(data: Any) -> Optional[dict[str, Any]]:
    if isinstance(data, dict):
        usage = data.get("usage")
        if isinstance(usage, dict):
            return usage
        for value in data.values():
            nested = find_usage(value)
            if nested is not None:
                return nested
    elif isinstance(data, list):
        for item in data:
            nested = find_usage(item)
            if nested is not None:
                return nested
    return None


def extract_completion_tokens(data: Any) -> Optional[int]:
    usage = find_usage(data) or {}
    for key in (
            "completion_tokens",
            "output_tokens",
            "generated_tokens",
            "completion_token_count",
    ):
        value = to_int(usage.get(key))
        if value is not None:
            return value

    prompt_tokens = None
    total_tokens = None
    for key in ("prompt_tokens", "input_tokens", "prompt_token_count"):
        prompt_tokens = to_int(usage.get(key))
        if prompt_tokens is not None:
            break
    for key in ("total_tokens", "total_token_count"):
        total_tokens = to_int(usage.get(key))
        if total_tokens is not None:
            break
    if prompt_tokens is not None and total_tokens is not None:
        return max(total_tokens - prompt_tokens, 0)
    return None


def extract_prompt_tokens(data: Any) -> Optional[int]:
    """提取网关 usage 中的实际输入 token 数；不存在时返回 None。"""

    usage = find_usage(data) or {}
    for key in ("prompt_tokens", "input_tokens", "prompt_token_count"):
        value = to_int(usage.get(key))
        if value is not None:
            return value
    return None


def find_business_error(data: Any) -> str:
    """识别 HTTP 200 响应体中的常见业务失败结构。"""

    if isinstance(data, list):
        for item in data:
            nested = find_business_error(item)
            if nested:
                return nested
        return ""
    if not isinstance(data, dict):
        return ""

    if data.get("error"):
        return f"BusinessError: {str(data.get('error'))[:200]}"
    success = data.get("success")
    if isinstance(success, bool) and not success:
        return f"BusinessError: success=false, body={str(data)[:200]}"

    for key in ("code", "status_code", "error_code"):
        if key not in data:
            continue
        value = data.get(key)
        numeric = to_int(value)
        if numeric is not None and numeric not in (0, 200):
            return f"BusinessError: {key}={value}"
        if numeric is None:
            text = str(value).strip().lower()
            if text and text not in {"0", "200", "ok", "success", "succeeded"}:
                return f"BusinessError: {key}={value}"

    status = str(data.get("status", "")).strip().lower()
    if status in {"error", "failed", "failure", "fail"}:
        return f"BusinessError: status={data.get('status')}"

    for value in data.values():
        nested = find_business_error(value)
        if nested:
            return nested
    return ""


def parse_stream_response(
        response: Response,
        start_time: float,
) -> ParsedResponse:
    """读取 SSE，优先采用 usage，并将 reasoning token 纳入 TTFT/输出统计。"""

    first_token_time: Optional[float] = None
    output_parts: list[str] = []
    completion_tokens: Optional[int] = None
    prompt_tokens: Optional[int] = None
    business_error = ""

    for raw_line in response.iter_lines(chunk_size=1, decode_unicode=False):
        if not raw_line:
            continue
        line_arrival_time = time.perf_counter()
        line = raw_line.decode("utf-8", errors="replace").strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]" or line.startswith(":"):
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not business_error:
            business_error = find_business_error(event)
        usage_tokens = extract_completion_tokens(event)
        if usage_tokens is not None:
            completion_tokens = usage_tokens
        usage_prompt_tokens = extract_prompt_tokens(event)
        if usage_prompt_tokens is not None:
            prompt_tokens = usage_prompt_tokens

        content = extract_choice_text(event, prefer_delta=True)
        if content:
            if first_token_time is None:
                first_token_time = line_arrival_time
            output_parts.append(content)

    response_end_time = time.perf_counter()
    output_text = "".join(output_parts)
    if business_error:
        return ParsedResponse(
            "", None, "", None, response_end_time, business_error,
            prompt_tokens=prompt_tokens,
        )
    if not output_text.strip():
        return ParsedResponse(
            "",
            None,
            "",
            None,
            response_end_time,
            "EmptyOutput: 流式响应没有模型输出",
            prompt_tokens=prompt_tokens,
        )
    if completion_tokens is not None:
        source = "usage"
    else:
        completion_tokens = estimate_text_tokens(output_text)
        source = "estimated"
    ttft_s = None if first_token_time is None else first_token_time - start_time
    return ParsedResponse(
        output_text,
        completion_tokens,
        source,
        ttft_s,
        response_end_time,
        prompt_tokens=prompt_tokens,
    )


def parse_non_stream_response(
        response: Response,
        response_end_time: Optional[float] = None,
) -> ParsedResponse:
    """解析非流式响应，验证业务状态与非空输出。"""

    response_end_time = response_end_time or time.perf_counter()
    raw_text = response.text
    try:
        data = response.json()
    except ValueError:
        content_type = response.headers.get("Content-Type", "").lower()
        stripped = raw_text.strip()
        if not stripped:
            return ParsedResponse(
                "", None, "", None, response_end_time, "EmptyOutput: 响应体为空"
            )
        if "json" in content_type:
            return ParsedResponse(
                "",
                None,
                "",
                None,
                response_end_time,
                "InvalidJSON: JSON 响应无法解析",
            )
        if "html" in content_type or stripped.lower().startswith(("<!doctype html", "<html")):
            return ParsedResponse(
                "",
                None,
                "",
                None,
                response_end_time,
                "InvalidOutput: 收到 HTML 响应",
            )
        return ParsedResponse(
            stripped,
            estimate_text_tokens(stripped),
            "estimated",
            None,
            response_end_time,
        )

    prompt_tokens = extract_prompt_tokens(data)
    business_error = find_business_error(data)
    if business_error:
        return ParsedResponse(
            "", None, "", None, response_end_time, business_error,
            prompt_tokens=prompt_tokens,
        )

    output_text = extract_non_stream_text(data)
    if not output_text.strip():
        return ParsedResponse(
            "",
            None,
            "",
            None,
            response_end_time,
            "EmptyOutput: 非流式响应没有模型输出",
            prompt_tokens=prompt_tokens,
        )

    completion_tokens = extract_completion_tokens(data)
    if completion_tokens is not None:
        source = "usage"
    else:
        completion_tokens = estimate_text_tokens(output_text)
        source = "estimated"
    return ParsedResponse(
        output_text,
        completion_tokens,
        source,
        None,
        response_end_time,
        prompt_tokens=prompt_tokens,
    )


def request_once(
        session: Session,
        args: argparse.Namespace,
        payload: dict[str, Any],
        stream: bool,
) -> RequestResult:
    """执行一次请求，连接由 worker 专属 Session 复用。"""

    start_time = time.perf_counter()
    response: Optional[Response] = None
    try:
        response = session.post(
            args.url,
            headers=make_headers(args.app_key, args.secret_key),
            json=payload,
            stream=stream,
            verify=args.verify_ssl,
            timeout=(args.connect_timeout, args.read_timeout),
        )
        non_stream_end_time = time.perf_counter() if not stream else None
        response.raise_for_status()

        parsed = (
            parse_stream_response(response, start_time)
            if stream
            else parse_non_stream_response(response, non_stream_end_time)
        )
        end_time = parsed.end_time_s
        if parsed.error:
            return RequestResult(
                success=False,
                start_time_s=start_time,
                end_time_s=end_time,
                response_time_s=end_time - start_time,
                ttft_s=parsed.ttft_s,
                completion_tokens=parsed.completion_tokens,
                prompt_tokens=parsed.prompt_tokens,
                token_count_source=parsed.token_count_source,
                status_code=response.status_code,
                error=parsed.error,
            )

        return RequestResult(
            success=True,
            start_time_s=start_time,
            end_time_s=end_time,
            response_time_s=end_time - start_time,
            ttft_s=parsed.ttft_s,
            completion_tokens=parsed.completion_tokens,
            prompt_tokens=parsed.prompt_tokens,
            token_count_source=parsed.token_count_source,
            status_code=response.status_code,
        )
    except RequestException as exc:
        end_time = time.perf_counter()
        return RequestResult(
            success=False,
            start_time_s=start_time,
            end_time_s=end_time,
            response_time_s=end_time - start_time,
            ttft_s=None,
            completion_tokens=None,
            status_code=response.status_code if response is not None else None,
            error=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:
        end_time = time.perf_counter()
        return RequestResult(
            success=False,
            start_time_s=start_time,
            end_time_s=end_time,
            response_time_s=end_time - start_time,
            ttft_s=None,
            completion_tokens=None,
            status_code=response.status_code if response is not None else None,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if response is not None:
            response.close()


def load_worker(
        args: argparse.Namespace,
        payload: dict[str, Any],
        stream: bool,
        start_barrier: threading.Barrier,
        start_holder: list[float],
        duration_s: float,
        result_queue: queue.Queue[RequestResult],
) -> None:
    """单 worker 闭环持续发压；每个 worker 复用自己的 HTTP 连接池。"""

    with requests.Session() as session:
        try:
            start_barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            now = time.perf_counter()
            result_queue.put(
                RequestResult(
                    False,
                    now,
                    now,
                    0.0,
                    None,
                    None,
                    error="BrokenBarrierError: worker 启动失败",
                )
            )
            return

        deadline = start_holder[0] + duration_s
        while time.perf_counter() < deadline:
            result_queue.put(request_once(session, args, payload, stream))


def percentile(values: list[float], percent: float) -> Optional[float]:
    """nearest-rank 百分位：返回实际观测值，适合 SLA/压测报告。"""

    if not values:
        return None
    if not 0 <= percent <= 100:
        raise ValueError("percentile 必须位于 [0, 100]")
    ordered = sorted(values)
    if percent == 0:
        return ordered[0]
    rank = math.ceil(percent / 100.0 * len(ordered))
    return ordered[min(max(rank - 1, 0), len(ordered) - 1)]


def average(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def drain_results(
        result_queue: queue.Queue[RequestResult],
        results: list[RequestResult],
) -> None:
    while True:
        try:
            results.append(result_queue.get_nowait())
        except queue.Empty:
            return


def current_success_rate(results: list[RequestResult]) -> Optional[float]:
    if not results:
        return None
    return sum(item.success for item in results) / len(results) * 100.0


def print_gateway_prompt_tokens(results: list[RequestResult]) -> None:
    """仅在网关返回 usage.prompt_tokens 时打印实际输入 token 采样。"""

    samples = [item.prompt_tokens for item in results if item.prompt_tokens is not None]
    if not samples:
        return
    counts = Counter(samples)
    values = ", ".join(
        f"{token_value} tokens（{sample_count}次）"
        for token_value, sample_count in sorted(counts.items())
    )
    print(
        f"[网关 usage.prompt_tokens] 实际输入={values}，"
        f"覆盖请求={len(samples)}/{len(results)}",
        flush=True,
    )


def run_pressure_step(
        args: argparse.Namespace,
        mode: str,
        concurrency: int,
        context_text: str,
) -> StepMetrics:
    """完整运行一个并发梯度，不因单次失败提前截断统计窗口。"""

    stream = mode == "stream"
    payload = build_payload(args, context_text, stream)
    result_queue: queue.Queue[RequestResult] = queue.Queue()
    results: list[RequestResult] = []
    start_holder = [0.0]

    def set_exact_start() -> None:
        start_holder[0] = time.perf_counter()

    start_barrier = threading.Barrier(concurrency + 1, action=set_exact_start)
    wall_start = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                load_worker,
                args,
                payload,
                stream,
                start_barrier,
                start_holder,
                args.duration,
                result_queue,
            )
            for _ in range(concurrency)
        ]

        try:
            start_barrier.wait(timeout=30)
        except threading.BrokenBarrierError as exc:
            raise RuntimeError("并发 worker 未能在 30 秒内全部就绪") from exc

        next_progress = min(30.0, args.duration)
        while True:
            elapsed = time.perf_counter() - start_holder[0]
            drain_results(result_queue, results)
            if elapsed >= next_progress or elapsed >= args.duration:
                display_elapsed = min(elapsed, args.duration)
                live_rate = current_success_rate(results)
                rate_text = "N/A" if live_rate is None else f"{live_rate:.2f}%"
                print(
                    f"[并发: {concurrency}] 已完成 {display_elapsed:.0f}/{args.duration:g} 秒，"
                    f"已完成请求 {len(results)}，当前成功率 {rate_text} ...",
                    flush=True,
                )
                next_progress += 30.0
            if elapsed >= args.duration:
                break
            time.sleep(min(1.0, max(0.05, args.duration - elapsed)))

        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                now = time.perf_counter()
                result_queue.put(
                    RequestResult(
                        False,
                        now,
                        now,
                        0.0,
                        None,
                        None,
                        error=f"WorkerError: {type(exc).__name__}: {exc}",
                    )
                )

    drain_results(result_queue, results)
    print_gateway_prompt_tokens(results)
    wall_duration = time.perf_counter() - wall_start
    return calculate_metrics(
        concurrency,
        results,
        args.duration,
        wall_duration,
        stream,
    )


def calculate_metrics(
        concurrency: int,
        results: list[RequestResult],
        pressure_duration_s: float,
        wall_duration_s: float,
        stream: bool,
) -> StepMetrics:
    attempted = len(results)
    succeeded_results = [item for item in results if item.success]
    succeeded = len(succeeded_results)
    failed = attempted - succeeded
    success_rate = succeeded / attempted * 100.0 if attempted else 0.0

    successful_response_times = [item.response_time_s for item in succeeded_results]
    all_response_times = [item.response_time_s for item in results]
    ttfts = [item.ttft_s for item in succeeded_results if item.ttft_s is not None]
    token_samples = [
        item.completion_tokens
        for item in succeeded_results
        if item.completion_tokens is not None
    ]
    total_tokens = sum(token_samples)

    timed_results = [
        item
        for item in results
        if item.start_time_s > 0 and item.end_time_s >= item.start_time_s
    ]
    if timed_results:
        effective_start = min(item.start_time_s for item in timed_results)
        effective_end = max(item.end_time_s for item in timed_results)
        effective_duration = max(effective_end - effective_start, 1e-9)
    else:
        effective_duration = 0.0

    errors = Counter(
        item.error.split(":", 1)[0] if item.error else "UnknownError"
        for item in results
        if not item.success
    )

    return StepMetrics(
        concurrency=concurrency,
        attempted=attempted,
        succeeded=succeeded,
        failed=failed,
        success_rate=success_rate,
        average_response_time_s=average(successful_response_times),
        p95_response_time_s=percentile(successful_response_times, 95),
        p99_response_time_s=percentile(successful_response_times, 99),
        all_average_response_time_s=average(all_response_times),
        all_p95_response_time_s=percentile(all_response_times, 95),
        all_p99_response_time_s=percentile(all_response_times, 99),
        average_ttft_s=average(ttfts),
        p95_ttft_s=percentile(ttfts, 95),
        ttft_coverage=(len(ttfts) / succeeded * 100.0 if stream and succeeded else None),
        total_qps=(attempted / effective_duration if effective_duration > 0 else 0.0),
        success_qps=(succeeded / effective_duration if effective_duration > 0 else 0.0),
        token_throughput=(
            total_tokens / effective_duration
            if effective_duration > 0 and token_samples
            else None
        ),
        token_count_coverage=(len(token_samples) / succeeded * 100.0 if succeeded else 0.0),
        pressure_duration_s=pressure_duration_s,
        effective_duration_s=effective_duration,
        wall_duration_s=wall_duration_s,
        errors=dict(errors),
    )


def relative_growth(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None or previous <= 0:
        return None
    return (current - previous) / previous


def evaluate_saturation(
        previous: Optional[StepMetrics],
        current: StepMetrics,
        args: argparse.Namespace,
) -> tuple[bool, str]:
    """基于当前梯度相对上一梯度的变化判断是否进入容量拐点。"""

    if current.success_rate < args.required_success_rate:
        return True, (
            f"成功率 {current.success_rate:.2f}% 低于要求 "
            f"{args.required_success_rate:.2f}%"
        )
    if previous is None:
        return False, "首个梯度，无上一梯度可比较"

    qps_growth = relative_growth(current.success_qps, previous.success_qps)
    token_growth = relative_growth(current.token_throughput, previous.token_throughput)
    latency_growth = (
        current.all_p95_response_time_s / previous.all_p95_response_time_s
        if current.all_p95_response_time_s is not None
           and previous.all_p95_response_time_s is not None
           and previous.all_p95_response_time_s > 0
        else None
    )

    qps_plateau = qps_growth is not None and qps_growth <= args.saturation_growth_threshold
    token_plateau = (
            token_growth is not None and token_growth <= args.saturation_growth_threshold
    )
    latency_degraded = (
            latency_growth is not None
            and latency_growth >= args.saturation_latency_growth_threshold
    )

    details = []
    if qps_growth is not None:
        details.append(f"成功QPS增长={qps_growth * 100:.2f}%")
    if token_growth is not None:
        details.append(f"Token吞吐增长={token_growth * 100:.2f}%")
    if latency_growth is not None:
        details.append(f"全量P95倍率={latency_growth:.2f}x")

    saturated = qps_plateau and (token_plateau or latency_degraded)
    if saturated:
        return True, "；".join(details) or "吞吐增长停滞"
    return False, "；".join(details) or "有效样本不足，暂不判定饱和"


def initialize_report(report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if report_path.exists() and report_path.stat().st_size > 0:
        with report_path.open("r", newline="", encoding="utf-8-sig") as file:
            existing_header = next(csv.reader(file), [])
        if tuple(existing_header) != REPORT_HEADERS:
            raise RuntimeError(
                f"报告 {report_path} 使用旧版表头，请更换 --report 路径或备份后删除旧文件"
            )
        return
    with report_path.open("w", newline="", encoding="utf-8-sig") as file:
        csv.writer(file).writerow(REPORT_HEADERS)


def format_csv_number(value: Optional[float], digits: int = 4) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}"


def append_step_to_report(
        report_path: Path,
        context: str,
        prompt_tokens: int,
        mode: str,
        metrics: StepMetrics,
        is_saturated: bool,
        saturation_reason: str,
) -> None:
    row = (
        CONTEXT_TOKEN_TARGETS[context],
        prompt_tokens,
        MODE_LABELS[mode],
        metrics.concurrency,
        metrics.attempted,
        metrics.succeeded,
        metrics.failed,
        f"{metrics.success_rate:.2f}",
        f"{metrics.pressure_duration_s:.4f}",
        f"{metrics.effective_duration_s:.4f}",
        f"{metrics.total_qps:.4f}",
        f"{metrics.success_qps:.4f}",
        format_csv_number(metrics.average_response_time_s),
        format_csv_number(metrics.p95_response_time_s),
        format_csv_number(metrics.p99_response_time_s),
        format_csv_number(metrics.all_average_response_time_s),
        format_csv_number(metrics.all_p95_response_time_s),
        format_csv_number(metrics.all_p99_response_time_s),
        format_csv_number(metrics.average_ttft_s),
        format_csv_number(metrics.p95_ttft_s),
        format_csv_number(metrics.ttft_coverage, 2),
        format_csv_number(metrics.token_throughput),
        f"{metrics.token_count_coverage:.2f}",
        str(is_saturated),
        saturation_reason,
    )
    with report_path.open("a", newline="", encoding="utf-8-sig") as file:
        csv.writer(file).writerow(row)
        file.flush()
        os.fsync(file.fileno())


def append_scenario_separator(report_path: Path) -> None:
    with report_path.open("a", newline="", encoding="utf-8-sig") as file:
        csv.writer(file).writerow([])
        file.flush()
        os.fsync(file.fileno())


def format_display(value: Optional[float], suffix: str = "") -> str:
    return "N/A" if value is None else f"{value:.4f}{suffix}"


def print_step_summary(
        metrics: StepMetrics,
        is_saturated: bool,
        saturation_reason: str,
) -> None:
    print(
        f"[梯度完成] 并发={metrics.concurrency}，请求={metrics.attempted}，"
        f"成功/失败={metrics.succeeded}/{metrics.failed}，"
        f"成功率={metrics.success_rate:.2f}%，"
        f"总QPS/成功QPS={metrics.total_qps:.4f}/{metrics.success_qps:.4f}，"
        f"成功P95={format_display(metrics.p95_response_time_s, 's')}，"
        f"全量P95={format_display(metrics.all_p95_response_time_s, 's')}，"
        f"Token吞吐={format_display(metrics.token_throughput, ' tokens/s')}，"
        f"是否饱和={is_saturated}",
        flush=True,
    )
    print(
        f"[统计窗口] 发压={metrics.pressure_duration_s:.4f}s，"
        f"有效区间={metrics.effective_duration_s:.4f}s，"
        f"含线程准备的墙钟耗时={metrics.wall_duration_s:.4f}s",
        flush=True,
    )
    if metrics.average_ttft_s is not None:
        print(
            f"[流式指标] 平均TTFT={metrics.average_ttft_s:.4f}s，"
            f"P95 TTFT={format_display(metrics.p95_ttft_s, 's')}，"
            f"覆盖率={format_display(metrics.ttft_coverage, '%')}",
            flush=True,
        )
    print(f"[饱和判断] {saturation_reason}", flush=True)
    if metrics.errors:
        print(f"[失败分类] {json.dumps(metrics.errors, ensure_ascii=False)}", flush=True)


def select_scenarios(args: argparse.Namespace) -> list[tuple[str, str]]:
    if args.context is None and args.mode is None:
        return list(FULL_SCENARIO_MATRIX)
    return [
        (context, mode)
        for context, mode in FULL_SCENARIO_MATRIX
        if (args.context is None or context == args.context)
           and (args.mode is None or mode == args.mode)
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Qwen3-8B 4K/32K、流式/非流式全矩阵阶梯并发压测脚本"
    )
    parser.add_argument("--context", choices=("4k", "32k"), default=None)
    parser.add_argument("--mode", choices=("stream", "non-stream"), default=None)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--app-key", default=APP_KEY)
    parser.add_argument("--secret-key", default=SECRET_KEY)
    parser.add_argument("--component-code", default=COMPONENT_CODE)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=256,
        help="单请求最大生成 token 数；传入 0 可不发送 max_tokens 字段",
    )
    parser.add_argument(
        "--output-token-reserve",
        type=int,
        default=0,
        help="从4K/32K窗口中为输出预留的token数；默认0表示目标值全部用于输入Prompt",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION_SECONDS,
        help="每个并发梯度的持续发压秒数，默认180",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--read-timeout",
        type=float,
        default=DEFAULT_READ_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(__file__).resolve().parent / "性能测试报告.csv",
        help="CSV 报告路径，默认位于本脚本目录",
    )
    parser.add_argument(
        "--verify-ssl",
        action="store_true",
        help="校验 HTTPS 证书；默认关闭以兼容内网自签名证书",
    )
    parser.add_argument(
        "--no-stream-include-usage",
        dest="stream_include_usage",
        action="store_false",
        help="不在流式请求中发送 stream_options.include_usage",
    )
    parser.set_defaults(stream_include_usage=True)
    parser.add_argument(
        "--required-success-rate",
        type=float,
        default=100.0,
        help="成功率断言阈值，所有场景完成后以此决定退出码",
    )
    parser.add_argument(
        "--stop-on-failed-step",
        action="store_true",
        help="某梯度低于成功率阈值时，在该完整梯度结束后停止后续测试",
    )
    parser.add_argument(
        "--saturation-growth-threshold",
        type=float,
        default=0.05,
        help="相邻梯度QPS/Token吞吐增长不超过该比例时视为平台期，默认0.05",
    )
    parser.add_argument(
        "--saturation-latency-growth-threshold",
        type=float,
        default=1.5,
        help="相邻梯度全量P95增长倍数阈值，默认1.5",
    )
    args = parser.parse_args()

    if not args.app_key or not args.secret_key:
        parser.error("必须通过环境变量或参数提供 APP_KEY 和 SECRET_KEY")
    if args.duration <= 0:
        parser.error("--duration 必须大于0")
    if args.connect_timeout <= 0 or args.read_timeout <= 0:
        parser.error("连接和读取超时必须大于0")
    if args.max_tokens is not None and args.max_tokens < 0:
        parser.error("--max-tokens 不能小于0")
    if args.max_tokens == 0:
        args.max_tokens = None
    if args.output_token_reserve < 0:
        parser.error("--output-token-reserve 不能小于0")
    if not 0 <= args.required_success_rate <= 100:
        parser.error("--required-success-rate 必须位于[0, 100]")
    if args.saturation_growth_threshold < 0:
        parser.error("--saturation-growth-threshold 不能小于0")
    if args.saturation_latency_growth_threshold <= 1:
        parser.error("--saturation-latency-growth-threshold 必须大于1")
    for target in CONTEXT_TOKEN_TARGETS.values():
        if args.output_token_reserve >= target:
            parser.error("--output-token-reserve 必须小于所有待测上下文窗口")
    return args


def main() -> int:
    args = parse_args()
    if not args.verify_ssl:
        urllib3.disable_warnings(InsecureRequestWarning)

    try:
        scenarios = select_scenarios(args)
        initialize_report(args.report)
    except Exception as exc:
        print(f"[FATAL] 初始化失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    required_contexts = list(dict.fromkeys(context for context, _ in scenarios))
    context_cache: dict[str, tuple[str, int]] = {}
    for context in required_contexts:
        try:
            context_text, prompt_tokens = build_chinese_context(
                context,
                args.output_token_reserve,
            )
        except Exception as exc:
            print(
                f"[FATAL] 构造 {CONTEXT_LABELS[context]} 输入失败: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 2
        context_cache[context] = (context_text, prompt_tokens)
        print(
            f"[输入估算] {CONTEXT_LABELS[context]}窗口目标="
            f"{CONTEXT_TOKEN_TARGETS[context]} tokens，估算输入Prompt={prompt_tokens} tokens，"
            f"输出预留={args.output_token_reserve} tokens",
            flush=True,
        )

    print(f"CSV 报告: {args.report.resolve()}")
    print(f"目标接口: {args.url}")
    print("Token 计数方式: 离线保守估算（不依赖本地 tokenizer）")
    print(f"并发梯度: {list(CONCURRENCY_LEVELS)}")
    print(f"每梯度持续发压: {args.duration:g} 秒")

    assertion_failed = False
    stop_requested = False
    for scenario_index, (context, mode) in enumerate(scenarios, start=1):
        context_text, prompt_tokens = context_cache[context]
        print(
            f"\n[{scenario_index}/{len(scenarios)}] 正在测试: "
            f"{CONTEXT_LABELS[context]}上下文 + {MODE_LABELS[mode]} ...",
            flush=True,
        )
        previous_metrics: Optional[StepMetrics] = None

        for concurrency in CONCURRENCY_LEVELS:
            print(
                f"\n[开始梯度] 场景={CONTEXT_LABELS[context]}+{MODE_LABELS[mode]}，"
                f"并发={concurrency}",
                flush=True,
            )
            try:
                metrics = run_pressure_step(
                    args,
                    mode,
                    concurrency,
                    context_text,
                )
            except Exception as exc:
                print(f"[FATAL] 梯度调度失败: {type(exc).__name__}: {exc}", file=sys.stderr)
                return 2

            is_saturated, saturation_reason = evaluate_saturation(
                previous_metrics,
                metrics,
                args,
            )
            append_step_to_report(
                args.report,
                context,
                prompt_tokens,
                mode,
                metrics,
                is_saturated,
                saturation_reason,
            )
            print_step_summary(metrics, is_saturated, saturation_reason)

            if mode == "stream" and metrics.p95_ttft_s is not None:
                if metrics.p95_ttft_s > 3.0:
                    print("[WARNING] P95 首 Token 延迟超过3秒", flush=True)

            if metrics.attempted == 0 or metrics.success_rate < args.required_success_rate:
                assertion_failed = True
                print(
                    f"[ASSERTION FAILED] 并发 {concurrency} 成功率="
                    f"{metrics.success_rate:.2f}%，要求不低于"
                    f"{args.required_success_rate:.2f}%",
                    file=sys.stderr,
                    flush=True,
                )
                if args.stop_on_failed_step:
                    stop_requested = True
                    break

            previous_metrics = metrics

        append_scenario_separator(args.report)
        print(
            f"[{scenario_index}/{len(scenarios)}] 场景结束，结果已追加到 {args.report.resolve()}",
            flush=True,
        )
        if stop_requested:
            break

    if assertion_failed:
        print("\n测试已完成，但存在未达到成功率阈值的梯度。", file=sys.stderr, flush=True)
        return 1
    print("\n全部指定测试场景执行完成，所有成功率断言均通过。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
