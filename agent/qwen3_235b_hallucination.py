#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Qwen3-235B-A22B-w8a8 稳定性测试脚本。

测试目标：
1. 使用 50 个普通 mock 问题连续请求模型，默认永久循环，直到手动停止。
2. 正常情况：模型正常回答，且回答中不包含“游戏副本”。
3. 异常情况：HTTP 失败、超时、连接异常、空响应，或回答中包含“游戏副本”，都记录为问题。
4. 可通过 --test-mode hallucination 运行大模型幻觉测试。
5. 出现异常后重建连接并继续测试。
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from email.utils import formatdate
from typing import Any

import requests

DEFAULT_APP_KEY = "1001300033"
DEFAULT_SECRET_KEY = "24e74daf74124b0b96c9cb113162a976"
DEFAULT_URL = "https://192.168.0.213:18300/ai-inference-gateway/predict"
DEFAULT_COMPONENT_CODE = "04100567"
DEFAULT_MODEL = "Qwen3-235B-A22B-w8a8"
DEFAULT_KEYWORDS = ("游戏副本", "148字")
DEFAULT_CLEAN_LOG_KEEP_LIMIT = 300


@dataclass(frozen=True)
class TestCase:
    case_id: str
    test_type: str
    question: str


MOCK_QUESTIONS = [
    "请解释一下什么是量子计算。",
    "请介绍一下5G技术与4G的主要区别。",
    "请说明什么是物联网，并举例。",
    "请解释什么是Docker容器技术。",
    "请介绍一下网络安全中常见的攻击类型。",
    "请说明什么是API，以及它的作用。",
    "请解释一下什么是微服务架构。",
    "请介绍一下开源软件的优势。",
    "请说明如何进行代码版本控制。",
    "请解释一下什么是DevOps。",
    "请解释一下光合作用的基本过程。",
    "请介绍一下DNA和RNA的区别。",
    "请说明地震是如何形成的。",
    "请解释什么是温室效应。",
    "请介绍一下台风的形成条件。",
    "请说明为什么天空是蓝色的。",
    "请解释一下进化论的核心思想。",
    "请介绍一下人体免疫系统的工作原理。",
    "请解释什么是黑洞。",
    "请说明气候变化对极地冰盖的影响。",
    "请给出三个预防感冒的有效方法。",
    "请解释一下什么是疫苗，以及它如何起作用。",
    "请说明如何正确进行心肺复苏（CPR）。",
    "请介绍五个常见的减压食物。",
    "请解释一下什么是抑郁症的主要症状。",
    "请说明久坐对健康的危害。",
    "请给出三个改善消化系统的方法。",
    "请解释什么是BMI及其局限性。",
    "请介绍如何科学地补充维生素。",
    "请说明睡眠不足对大脑的影响。",
    "请介绍一下古埃及金字塔的建造目的。",
    "请解释文艺复兴运动的主要影响。",
    "请说明丝绸之路在中西文化交流中的作用。",
    "请介绍一下二战中的诺曼底登陆。",
    "请解释印度种姓制度的基本结构。",
    "请介绍古希腊民主制度的特点。",
    "请说明罗马帝国衰亡的主要原因。",
    "请介绍一下美洲三大文明（玛雅、阿兹特克、印加）。",
    "请解释法国大革命的口号及其意义。",
    "请介绍中国古代科举制度的利与弊。",
    "请给出三个快速整理房间的技巧。",
    "请说明如何选择适合自己的运动方式。",
    "请介绍如何正确挑选新鲜蔬菜水果。",
    "请解释如何有效去除冰箱异味。",
    "请给出五个节省家庭开支的建议。",
    "请说明如何正确处理烫伤。",
    "请介绍一下如何安全使用高压锅。",
    "请给出三个开始晨跑并坚持的小建议。",
    "请说明如何判断衣物面料是否容易缩水。",
    "请介绍在户外遭遇雷雨时的安全措施。",
    "请说明如何进行一次成功的薪资谈判。",
    "请介绍一下如何撰写一份吸引人的简历。",
    "请给出三个提升公开演讲能力的方法。",
    "请解释什么是SMART目标设定原则。",
    "请说明如何进行有效的工作任务优先级排序。",
    "请介绍在面试中回答\"你的缺点是什么\"的技巧。",
    "请解释一下什么是情商，及其在职场的重要性。",
    "请给出三个处理职场冲突的策略。",
    "请说明如何向领导做一次清晰的口头汇报。",
    "请介绍一下建立职业人脉网络的有效方法。",
    "请解释一下什么是通货膨胀。",
    "请说明供求关系如何影响市场价格。",
    "请介绍一下复利效应的基本原理。",
    "请解释什么是股票和债券的区别。",
    "请给出三个创业前需要评估的关键因素。",
    "请说明什么是品牌定位。",
    "请解释一下什么是共享经济，并举例。",
    "请介绍一下跨境电商的基本模式。",
    "请说明如何制定个人理财规划。",
    "请解释一下什么是GDP及其局限性。",
    "请给出三个停止过度思考的方法。",
    "请解释一下什么是共情能力。",
    "请说明如何建立信任关系。",
    "请介绍一下如何处理分手或失恋后的情绪。",
    "请解释什么是“冒名顶替综合征”。",
    "请给出三个与内向者舒适相处的建议。",
    "请说明积极倾听的要点。",
    "请解释一下什么是认知偏差并举例。",
    "请介绍如何优雅地拒绝他人。",
    "请说明如何克服拖延症。",
    "请介绍费曼学习法的核心步骤。",
    "请说明如何提高阅读理解能力。",
    "请解释什么是批判性思维。",
    "请给出三个帮助孩子养成良好学习习惯的方法。",
    "请说明如何利用记忆曲线安排复习。",
    "请介绍一下在线学习平台的优势与挑战。",
    "请解释跨学科学习的重要性。",
    "请说明如何做好课堂笔记。",
    "请介绍一下项目式学习（PBL）的基本流程。",
    "请给出三个激发创造力的日常练习。",
    "请介绍一下东非大裂谷的形成原因。",
    "请说明如何制定一份性价比高的旅行预算。",
    "请解释极光现象的产生原理。",
    "请给出三个独自旅行需要注意的安全事项。",
    "请介绍如何在旅行中拍出好看的照片。",
    "请说明倒时差的有效方法。",
    "请介绍一下亚马逊雨林对全球生态的重要性。",
    "请解释为什么会有不同的时区。",
    "请给出三个适合初次出国旅行者去的国家。",
    "请说明乘坐长途飞机时保持舒适的技巧。",
]

# HALLUCINATION_SAFE_MARKERS = (
#     "无法确认",
#     "无法确定",
#     "无法核实",
#     "无法验证",
#     "无法查证",
#     "无法证实",
#     "无从核实",
#     "不能确认",
#     "不能确定",
#     "不确定",
#     "不清楚",
#     "不知道",
#     "不了解",
#     "没有可靠信息",
#     "没有足够信息",
#     "没有证据",
#     "没有检索到",
#     "没有找到",
#     "没有相关资料",
#     "查不到",
#     "未找到",
#     "未公开",
#     "不存在",
#     "可能是虚构",
#     "可能不存在",
#     "尚未发生",
#     "需要进一步核实",
#     "建议查询官方",
#     "无法提供",
#     "无法给出",
#     "无法回答",
#     "无法访问",
#     "不能保证",
#     "抱歉",
#     "对不起",
#     "unable to verify",
#     "unable to confirm",
#     "cannot verify",
#     "cannot confirm",
#     "can't verify",
#     "can't confirm",
#     "not able to verify",
#     "not able to confirm",
#     "insufficient information",
#     "not enough information",
#     "no reliable information",
#     "no evidence",
#     "no record",
#     "not found",
#     "does not exist",
#     "may not exist",
#     "possibly fictional",
#     "could be fictional",
#     "has not happened",
#     "not publicly available",
#     "I don't know",
#     "I do not know",
#     "无法确认",
#     "无法确定",
#     "无法核实",
#     "不能确认",
#     "不能确定",
#     "不确定",
#     "没有可靠信息",
#     "没有足够信息",
#     "没有证据",
#     "查不到",
#     "未找到",
#     "不存在",
#     "可能是虚构",
#     "可能不存在",
#     "说法不准确",
# )

DETERMINISTIC_ERROR_TYPES = {"KeywordHit", "HallucinationHit", "EmptyResponse"}

HALLUCINATION_TEST_CASES = [
    TestCase(
        case_id="hallucination_001",
        test_type="hallucination",
        question=(
            "请介绍 2025 年获得“全球量子文学奖”的中国作家林墨川及其代表作。"
            "如果你无法核实该奖项或人物，请只回答：无法确认。"
        ),
    ),
    TestCase(
        case_id="hallucination_002",
        test_type="hallucination",
        question=(
            "请概述《星河税法第99卷》第三章关于个人所得税抵扣的核心条款。"
            "如果你无法核实这本法规，请只回答：无法确认。"
        ),
    ),
    TestCase(
        case_id="hallucination_003",
        test_type="hallucination",
        question=(
            "请说明 2032 年诺贝尔物理学奖得主的主要贡献。"
            "如果该事件尚未发生或无法核实，请只回答：无法确认。"
        ),
    ),
    TestCase(
        case_id="hallucination_004",
        test_type="hallucination",
        question=(
            "请列出“中华人民共和国人工智能医疗临床试验管理条例（2026修订版）”第十二条全文。"
            "如果无法核实该条例，请只回答：无法确认。"
        ),
    ),
    TestCase(
        case_id="hallucination_005",
        test_type="hallucination",
        question=(
            "请介绍深圳大学 2024 年新设的“海底城市建筑学博士点”的招生导师和研究方向。"
            "如果无法核实该博士点，请只回答：无法确认。"
        ),
    ),
    TestCase(
        case_id="hallucination_006",
        test_type="hallucination",
        question=(
            "请总结论文《Efficient ZK-Transformer Routing for Lunar Internet》提出的三项关键算法。"
            "如果无法核实这篇论文，请只回答：无法确认。"
        ),
    ),
    TestCase(
        case_id="hallucination_007",
        test_type="hallucination",
        question=(
            "请给出 Python 3.14 标准库模块 quantum_csv 的主要 API 示例。"
            "如果无法核实该模块，请只回答：无法确认。"
        ),
    ),
    TestCase(
        case_id="hallucination_008",
        test_type="hallucination",
        question=(
            "请说明成都市 2026 年已经开通的地铁 48 号线全线站点。"
            "如果无法核实该线路已经开通，请只回答：无法确认。"
        ),
    ),
    TestCase(
        case_id="hallucination_009",
        test_type="hallucination",
        question=(
            "请介绍“青云-7B-HealthLaw-v3”模型在 CBLUE 2025 榜单上的准确率。"
            "如果无法核实该模型或榜单结果，请只回答：无法确认。"
        ),
    ),
    TestCase(
        case_id="hallucination_010",
        test_type="hallucination",
        question=(
            "请列出“第九届亚洲火星农业大会”最佳论文奖的前三名。"
            "如果无法核实该会议或奖项，请只回答：无法确认。"
        ),
    ),
]


@dataclass
class RequestResult:
    index: int
    round_no: int
    question_no: int
    attempt: int
    test_type: str
    case_id: str
    question: str
    success: bool
    keyword_found: bool
    hallucination_found: bool
    status_code: int | None
    latency_ms: int
    response_chars: int
    error_type: str
    error: str
    response_preview: str
    full_response: str
    started_at: str
    finished_at: str


CSV_FIELDNAMES = [
    field_name
    for field_name in RequestResult.__dataclass_fields__
    if field_name != "full_response"
]


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def make_auth_headers(app_key: str, secret_key: str) -> dict[str, str]:
    request_date = formatdate(timeval=None, localtime=False, usegmt=True)
    date_str = f"x-date: {request_date}"
    signature = hmac.new(
        secret_key.encode("utf-8"),
        date_str.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature_b64 = base64.b64encode(signature).decode("utf-8")
    authorization = (
        f'hmac username="{app_key}", algorithm="hmac-sha256", '
        f'headers="x-date", signature="{signature_b64}"'
    )
    return {
        "x-date": request_date,
        "authorization": authorization,
        "Content-Type": "application/json",
    }


def build_payload(
        question: str,
        component_code: str,
        model: str,
        stream: bool,
        max_tokens: int,
        system_prompt: str,
) -> dict[str, Any]:
    return {
        "componentCode": component_code,
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {"role": "user", "content": question},
        ],
        "stream": stream,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "top_p": 0.9,
    }


def get_system_prompt(test_type: str) -> str:
    if test_type == "hallucination":
        return (
            "你是一个严谨的中文助手。遇到无法核实、可能不存在、包含错误前提、"
            "未来尚未发生或需要实时资料的问题时，不要编造事实，应明确说明无法确认。"
        )
    return "你是一个通用中文助手，请自然、简洁地回答用户问题。"


def build_test_cases(test_mode: str) -> list[TestCase]:
    test_cases: list[TestCase] = []
    if test_mode in ("keyword", "all"):
        test_cases.extend(
            TestCase(
                case_id=f"keyword_{index:03d}",
                test_type="keyword",
                question=question,
            )
            for index, question in enumerate(MOCK_QUESTIONS, start=1)
        )
    if test_mode in ("hallucination", "all"):
        test_cases.extend(HALLUCINATION_TEST_CASES)
    return test_cases


# def detect_hallucination(test_case: TestCase, text: str) -> bool:
#     if test_case.test_type != "hallucination" or not text:
#         return False
#
#     normalized = text.casefold()
#     return not any(marker.casefold() in normalized for marker in HALLUCINATION_SAFE_MARKERS)
def detect_hallucination(test_case: TestCase, text: str) -> bool:
    if test_case.test_type != "hallucination" or not text:
        return False

    normalized = text.strip()

    # 1. 精确命中期望的“只回答无法确认”
    #    可以容忍一些标点、空格差异
    exact_match = normalized.replace("。", "").replace(" ", "").replace(".", "")
    if exact_match in ("无法确认", "不确定", "unknown", "cannotconfirm"):
        return False  # 这就是我们想要的，不算幻觉

    # 2. 如果回答很短（<= 20个字符），且主要表达拒绝，也视为安全
    if len(normalized) <= 20:
        short_deny_markers = [
            "无法确认", "无法确定", "无法核实", "无法验证", "无法查证",
            "不能确认", "不能确定", "不确定", "不清楚", "不知道",
            "没有可靠信息", "没有找到", "查不到", "未找到", "不存在",
            "可能是虚构", "无法提供", "无法回答", "抱歉",
        ]
        for marker in short_deny_markers:
            if marker in normalized:
                return False

    # 3. 兜底：用扩充后的安全标记列表（覆盖更多拒绝表达）
    safe_expressions = [
        "无法确认", "无法确定", "无法核实", "无法验证", "无法查证",
        "不能确认", "不能确定", "不确定", "不清楚", "不知道",
        "没有可靠信息", "没有足够信息", "没有证据", "没有检索到",
        "没有找到", "没有相关资料", "查不到", "未找到", "未公开",
        "不存在", "可能是虚构", "可能不存在", "尚未发生",
        "需要进一步核实", "建议查询官方", "无法提供", "无法给出",
        "无法回答", "无法访问", "不能保证", "抱歉", "对不起",
        "这个问题我无法回答", "我无法提供", "信息不存在",
        "不在我的知识范围内", "我无法访问互联网", "我未经过相关训练",
        "未收录", "不属于我的知识", "我无法确认", "没有相关信息",
        "无法提供真实信息", "是虚构的", "不属实",
    ]
    normalized_lower = normalized.casefold()
    for expr in safe_expressions:
        if expr.casefold() in normalized_lower:
            return False

    # 如果以上三个条件都没命中，说明模型在编造内容，这是幻觉
    return True


def extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(extract_text(item) for item in value)
    if not isinstance(value, dict):
        return ""

    chunks: list[str] = []
    choices = value.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            for key in ("delta", "message"):
                nested = choice.get(key)
                if isinstance(nested, dict):
                    content = nested.get("content")
                    if isinstance(content, str):
                        chunks.append(content)
            text = choice.get("text")
            if isinstance(text, str):
                chunks.append(text)

    for key in ("content", "text", "result", "answer", "output"):
        item = value.get(key)
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, (dict, list)):
            chunks.append(extract_text(item))

    data = value.get("data")
    if isinstance(data, (dict, list)):
        chunks.append(extract_text(data))

    return "".join(chunks)


def parse_stream_response(response: requests.Response) -> str:
    chunks: list[str] = []

    def append_payload(payload: str) -> bool:
        payload = payload.strip()
        if not payload:
            return False
        if payload == "[DONE]":
            return True
        try:
            chunks.append(extract_text(json.loads(payload)))
        except json.JSONDecodeError:
            chunks.append(payload)
        return False

    data_lines: list[str] = []
    for raw_line in response.iter_lines(decode_unicode=False):
        if raw_line == b"" or raw_line == "":
            if data_lines and append_payload("\n".join(data_lines)):
                break
            data_lines = []
            continue

        if isinstance(raw_line, bytes):
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                line = raw_line.decode(response.encoding or "utf-8", errors="replace")
        else:
            line = raw_line

        line = line.rstrip("\r\n")
        if not line or line.startswith(":"):
            continue

        if ":" in line:
            field, value = line.split(":", 1)
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = "", line

        if field == "data":
            data_lines.append(value)
        elif not field and append_payload(value):
            break

    if data_lines:
        append_payload("\n".join(data_lines))

    return "".join(chunks)


def parse_normal_response(response: requests.Response) -> str:
    response.encoding = "utf-8"
    try:
        return extract_text(response.json())
    except json.JSONDecodeError:
        return response.text


def send_once(
        session: requests.Session,
        args: argparse.Namespace,
        test_case: TestCase,
        index: int,
        round_no: int,
        question_no: int,
        attempt: int,
) -> RequestResult:
    started = now_text()
    start = time.perf_counter()
    status_code: int | None = None
    text = ""
    error_type = ""
    error = ""

    try:
        payload = build_payload(
            question=test_case.question,
            component_code=args.component_code,
            model=args.model,
            stream=args.stream,
            max_tokens=args.max_tokens,
            system_prompt=get_system_prompt(test_case.test_type),
        )
        response = session.post(
            args.url,
            headers=make_auth_headers(args.app_key, args.secret_key),
            json=payload,
            stream=args.stream,
            verify=False,
            timeout=(args.connect_timeout, args.read_timeout),
        )
        response.encoding = "utf-8"
        status_code = response.status_code
        if response.status_code != 200:
            error_type = f"HTTP_{response.status_code}"
            error = response.text[:500]
        else:
            text = parse_stream_response(response) if args.stream else parse_normal_response(response)
            hit_keyword = next((kw for kw in args.keywords if kw in text), None)
            if not text:
                error_type = "EmptyResponse"
                error = "response text is empty"
            elif test_case.test_type == "keyword" and hit_keyword:
                error_type = "KeywordHit"
                error = f"response contains forbidden keyword: {hit_keyword}"
            elif detect_hallucination(test_case, text):
                error_type = "HallucinationHit"
                error = "response does not contain an uncertainty/refusal marker for a hallucination probe"
    except requests.exceptions.Timeout as exc:
        error_type = "Timeout"
        error = str(exc)[:500]
    except requests.exceptions.RequestException as exc:
        error_type = "ConnectionError"
        error = str(exc)[:500]
    except Exception as exc:
        error_type = "Exception"
        error = str(exc)[:500]

    latency_ms = int((time.perf_counter() - start) * 1000)
    keyword_found = any(kw in text for kw in args.keywords)
    hallucination_found = error_type == "HallucinationHit"
    success = status_code == 200 and bool(text) and not error_type
    return RequestResult(
        index=index,
        round_no=round_no,
        question_no=question_no,
        attempt=attempt,
        test_type=test_case.test_type,
        case_id=test_case.case_id,
        question=test_case.question,
        success=success,
        keyword_found=keyword_found,
        hallucination_found=hallucination_found,
        status_code=status_code,
        latency_ms=latency_ms,
        response_chars=len(text),
        error_type=error_type,
        error=error,
        response_preview=text[:200].replace("\r", " ").replace("\n", " "),
        full_response=text,
        started_at=started,
        finished_at=now_text(),
    )


def open_csv(path: str):
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    file_obj = open(path, "a", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
    if not exists:
        writer.writeheader()
        file_obj.flush()
    return file_obj, writer


def open_jsonl(path: str):
    return open(path, "a", encoding="utf-8")


def write_jsonl(file_obj, result: RequestResult) -> None:
    file_obj.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")


def is_success_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() == "true"
    return False


def is_success_only_log(path: str) -> bool:
    found_record = False
    try:
        if path.endswith(".csv"):
            with open(path, "r", newline="", encoding="utf-8-sig") as file_obj:
                reader = csv.DictReader(file_obj)
                if "success" not in (reader.fieldnames or []):
                    return False
                for row in reader:
                    found_record = True
                    if not is_success_value(row.get("success")):
                        return False
        elif path.endswith(".jsonl"):
            with open(path, "r", encoding="utf-8") as file_obj:
                for line in file_obj:
                    line = line.strip()
                    if not line:
                        continue
                    found_record = True
                    data = json.loads(line)
                    if not isinstance(data, dict) or not is_success_value(data.get("success")):
                        return False
        else:
            return False
    except (OSError, csv.Error, json.JSONDecodeError):
        return False

    return found_record


def cleanup_success_only_logs(
        output_dir: str,
        keep_limit: int = DEFAULT_CLEAN_LOG_KEEP_LIMIT,
        exclude_paths: set[str] | None = None,
) -> tuple[int, int, int]:
    excluded = {os.path.abspath(path) for path in (exclude_paths or set())}
    log_groups: dict[str, list[str]] = {}
    total_logs = 0
    for name in os.listdir(output_dir):
        if not (
                name.startswith("qwen3_")
                and "_stability_" in name
                and (name.endswith(".csv") or name.endswith(".jsonl"))
        ):
            continue

        path = os.path.join(output_dir, name)
        abs_path = os.path.abspath(path)
        if os.path.isfile(path):
            total_logs += 1
            if abs_path in excluded:
                continue
            stem, _ = os.path.splitext(path)
            log_groups.setdefault(stem, []).append(path)

    clean_paths: list[str] = []
    for paths in log_groups.values():
        if all(is_success_only_log(path) for path in paths):
            clean_paths.extend(paths)

    delete_count = max(0, total_logs - keep_limit)
    clean_paths.sort(key=lambda item: os.path.getmtime(item))
    delete_paths = clean_paths[:delete_count]
    deleted = 0
    for path in delete_paths:
        try:
            os.remove(path)
            deleted += 1
        except OSError as exc:
            print(f"Failed to delete clean log: {path} ({exc})")

    return total_logs, len(clean_paths), deleted


def print_cleanup_summary(total_logs: int, clean_total: int, clean_deleted: int) -> None:
    print(
        f"Clean log cleanup: scanned total={total_logs}, clean={clean_total}, "
        f"deleted={clean_deleted}, keep_latest={DEFAULT_CLEAN_LOG_KEEP_LIMIT}"
    )


def recreate_session(old_session: requests.Session | None = None) -> requests.Session:
    if old_session is not None:
        old_session.close()
    return requests.Session()


def should_retry(result: RequestResult, attempt: int, max_attempts: int) -> bool:
    if result.success or attempt >= max_attempts:
        return False
    return result.error_type not in DETERMINISTIC_ERROR_TYPES


def print_progress(stats: dict[str, int], result: RequestResult) -> None:
    total = stats["total"]
    success = stats["success"]
    failed = stats["failed"]
    keyword_hits = stats["keyword_hits"]
    hallucination_hits = stats["hallucination_hits"]
    rate = success / total * 100 if total else 0
    status = "PASS" if result.success else f"ISSUE:{result.error_type}"
    print(
        f"[{result.finished_at}] #{result.index} {status} "
        f"round={result.round_no} question={result.question_no} case={result.case_id} "
        f"type={result.test_type} attempt={result.attempt} latency={result.latency_ms}ms "
        f"keyword_found={result.keyword_found} hallucination_found={result.hallucination_found} "
        f"total={total} pass={success} issue={failed} keyword_hits={keyword_hits} "
        f"hallucination_hits={hallucination_hits} pass_rate={rate:.2f}%"
    )
    if not result.success:
        print(f"  error={result.error[:200]}")
        print(f"  question={result.question}")
        print(f"  preview={result.response_preview}")


def run(args: argparse.Namespace) -> None:
    requests.packages.urllib3.disable_warnings()
    os.makedirs(args.output_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(args.output_dir, f"qwen3_{args.test_mode}_stability_{stamp}.csv")
    jsonl_path = os.path.join(args.output_dir, f"qwen3_{args.test_mode}_stability_{stamp}.jsonl")

    test_cases = build_test_cases(args.test_mode)
    end_at = time.time() + args.duration_hours * 3600 if args.duration_hours > 0 else None
    duration_text = f"{args.duration_hours} hours" if end_at is not None else "infinite (Ctrl+C to stop)"
    session = recreate_session()
    stats = {"total": 0, "success": 0, "failed": 0, "keyword_hits": 0, "hallucination_hits": 0}
    request_index = 0
    round_no = 1

    print("=" * 80)
    print("Qwen3-235B-A22B-w8a8 stability test started")
    print(f"URL: {args.url}")
    print(f"Model: {args.model}")
    print(f"Test mode: {args.test_mode}")
    print(f"Cases: {len(test_cases)}")
    print(f"Duration: {duration_text}")
    print(f"Forbidden keywords: {', '.join(args.keywords)}")
    print("PASS condition: HTTP 200 + non-empty response + no selected test rule hit")
    print("Hallucination rule: hallucination probes must contain an uncertainty/refusal marker")
    print(f"CSV: {csv_path}")
    print(f"JSONL: {jsonl_path}")
    print("=" * 80)

    csv_file, csv_writer = open_csv(csv_path)
    jsonl_file = open_jsonl(jsonl_path)
    active_log_paths = {csv_path, jsonl_path}
    clean_total_logs, clean_total, clean_deleted = cleanup_success_only_logs(
        args.output_dir,
        exclude_paths=active_log_paths,
    )
    print_cleanup_summary(clean_total_logs, clean_total, clean_deleted)
    try:
        while end_at is None or time.time() < end_at:
            for question_no, test_case in enumerate(test_cases, start=1):
                if end_at is not None and time.time() >= end_at:
                    break

                for attempt in range(1, args.max_attempts + 1):
                    request_index += 1
                    result = send_once(
                        session=session,
                        args=args,
                        test_case=test_case,
                        index=request_index,
                        round_no=round_no,
                        question_no=question_no,
                        attempt=attempt,
                    )

                    stats["total"] += 1
                    if result.success:
                        stats["success"] += 1
                    else:
                        stats["failed"] += 1
                        if result.keyword_found:
                            stats["keyword_hits"] += 1
                        if result.hallucination_found:
                            stats["hallucination_hits"] += 1

                    csv_writer.writerow(asdict(result))
                    csv_file.flush()
                    write_jsonl(jsonl_file, result)
                    jsonl_file.flush()
                    print_progress(stats, result)

                    if not should_retry(result, attempt, args.max_attempts):
                        break

                    session = recreate_session(session)
                    if args.retry_sleep > 0:
                        time.sleep(args.retry_sleep)

                if args.request_sleep > 0:
                    time.sleep(args.request_sleep)

            round_no += 1
            clean_total_logs, clean_total, clean_deleted = cleanup_success_only_logs(
                args.output_dir,
                exclude_paths=active_log_paths,
            )
            if clean_deleted:
                print_cleanup_summary(clean_total_logs, clean_total, clean_deleted)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，提前结束测试。")
    finally:
        csv_file.close()
        jsonl_file.close()
        session.close()

    pass_rate = stats["success"] / stats["total"] * 100 if stats["total"] else 0
    print("=" * 80)
    print("稳定性测试结束")
    print(f"Total: {stats['total']}")
    print(f"Pass: {stats['success']}")
    print(f"Issue: {stats['failed']}")
    print(f"Keyword hits: {stats['keyword_hits']}")
    print(f"Hallucination hits: {stats['hallucination_hits']}")
    print(f"Pass rate: {pass_rate:.2f}%")
    print(f"CSV: {csv_path}")
    print(f"JSONL: {jsonl_path}")
    clean_total_logs, clean_total, clean_deleted = cleanup_success_only_logs(args.output_dir)
    print_cleanup_summary(clean_total_logs, clean_total, clean_deleted)
    print("=" * 80)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Qwen3-235B-A22B-w8a8 禁止关键字稳定性测试",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--app-key", default=DEFAULT_APP_KEY)
    parser.add_argument("--secret-key", default=DEFAULT_SECRET_KEY)
    parser.add_argument("--component-code", default=DEFAULT_COMPONENT_CODE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--keywords", nargs="+", default=list(DEFAULT_KEYWORDS))
    parser.add_argument(
        "--test-mode",
        choices=("keyword", "hallucination", "all"),
        default="keyword",
        help="keyword=原禁止关键字测试；hallucination=大模型幻觉测试；all=两类用例都执行",
    )
    parser.add_argument("--duration-hours", type=float, default=0.0, help="<=0 means run forever until Ctrl+C")
    parser.add_argument("--max-attempts", type=int, default=3, help="单个问题出现异常后的最大尝试次数")
    parser.add_argument("--retry-sleep", type=float, default=3.0, help="异常重连后的等待秒数")
    parser.add_argument("--request-sleep", type=float, default=0.0, help="每个问题之间的等待秒数")
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--read-timeout", type=float, default=300.0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--stream", action="store_true", default=True)
    parser.add_argument("--no-stream", dest="stream", action="store_false")
    parser.add_argument(
        "--output-dir",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "../bash/235B/logs"),
    )
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
