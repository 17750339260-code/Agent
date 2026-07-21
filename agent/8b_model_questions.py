from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime
from email.utils import formatdate
from pathlib import Path
from typing import Any

import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning

# 多模态解析大模型 - Qwen3-8B AI 学习助手
APP_KEY = os.getenv("APP_KEY", "1001300037")
SECRET_KEY = os.getenv("SECRET_KEY", "360ce63f5625412ba78d0aed3458b53a")
URL = os.getenv("GATEWAY_URL", "https://10.10.65.213:18300/ai-inference-gateway/predict")
COMPONENT_CODE = os.getenv("COMPONENT_CODE", "04100832")
MODEL = os.getenv("MODEL", "Qwen3-8B")

SYSTEM_PROMPT = """
角色：你是一位耐心、严谨的答疑老师。用户可能提出数学、物理、化学、编程、
逻辑推理、语言或生活常识等问题。
回答要求：
1. 先判断问题类型；
2. 清晰说明解题或分析思路；
3. 展示必要的推导、计算、代码或操作步骤；
4. 明确给出最终答案；
5. 简要总结相关知识点或注意事项。

回答应准确、友好、层次清晰。简单问题可以简洁回答，不要为了套用格式而重复内容；
信息不足时应明确指出缺少的信息，不要编造事实。
""".strip()

# 默认按列表顺序逐题调用模型，共 50 个问题。
# 如需替换问题，直接修改此列表即可；也可以多次使用 --user-prompt 临时传入问题。
USER_PROMPTS = [
    "有 5 个人排队，A 不站在第一位，B 不站在最后一位，问有多少种排列？",
    "解方程 x²-5x+6=0，并说明用了什么方法。",
    "一件商品原价 480 元，先打八折，再使用 30 元优惠券，最终需要支付多少钱？",
    "已知 2x+y=11，x-y=1，求 x 和 y。",
    "同时掷两枚普通六面骰子，点数之和为 8 的概率是多少？",
    "某班 5 名学生的成绩为 78、85、92、88、77，平均分和中位数分别是多少？",
    "一个长方形周长为 36 厘米，长比宽多 4 厘米，求它的面积。",
    "本金 10000 元，年利率 3%，按年复利计算，3 年后本息共多少元？",
    "等差数列 3、7、11、15……的第 20 项是多少？前 20 项的和是多少？",
    "甲乙两地相距 360 千米，一辆汽车以 90 千米/小时的速度行驶，中途休息 30 分钟，总共需要多长时间？",
    "一个 2 千克的物体受到 10 牛的水平合力，它的加速度是多少？",
    "一个 12V 电源连接 4Ω 电阻，电流和电阻消耗的功率分别是多少？",
    "忽略空气阻力，以 20m/s 的初速度竖直上抛物体，取 g=10m/s²，最高能上升多高？",
    "某物体质量为 540 克，体积为 200 立方厘米，它的密度是多少？",
    "把 2 千克水从 20℃ 加热到 50℃，水的比热容取 4.2×10³J/(kg·℃)，需要多少热量？",
    "看到闪电 6 秒后听到雷声，声速按 340m/s 计算，雷电大约距离多远？",
    "一台机器在 20 秒内完成 6000 焦的功，它的平均功率是多少？",
    "请配平化学方程式：Fe + O₂ → Fe₃O₄，并说明配平思路。",
    "将 5.85 克 NaCl 溶于水配成 500mL 溶液，NaCl 摩尔质量取 58.5g/mol，物质的量浓度是多少？",
    "常温下某溶液的氢离子浓度为 1×10⁻³mol/L，它的 pH 是多少？",
    "为什么同一周期元素从左到右原子半径通常逐渐减小？",
    "在反应 Zn + CuSO₄ → ZnSO₄ + Cu 中，谁被氧化，谁被还原？",
    "写一个 Java 方法计算非负整数 n 的阶乘，并处理 n 过大可能溢出的问题。",
    "如何用 Python 在保持原顺序的情况下去除列表中的重复元素？请给出代码。",
    "请解释二分查找的原理、适用条件和时间复杂度，并给出 Python 实现。",
    "SQL 中如何查询员工表里工资第二高的员工？请兼顾工资并列情况。",
    "HTTP 状态码 404 和 500 分别表示什么？排查方向有什么不同？",
    "Git 中刚提交了一次 commit，但还没有 push，如何撤销提交并保留代码修改？",
    "为什么哈希表的平均查询复杂度是 O(1)，最坏情况下却可能是 O(n)？",
    "什么是数据库死锁？请举一个简单例子并说明常见的预防方法。",
    "请用 Python 编写函数，判断一个忽略空格和大小写的字符串是否为回文。",
    "JavaScript 中防抖和节流有什么区别？分别适合哪些场景？",
    "设计一个 REST API 的分页接口时，页码分页和游标分页各有什么优缺点？",
    "Docker 启动容器时 -p 8080:80 表示什么？为什么浏览器可能仍然访问不到？",
    "房间外有三个开关，房间内有三盏灯，只能进房间一次，怎样判断每个开关对应哪盏灯？",
    "甲说‘乙在说谎’，乙说‘甲和丙都在说谎’，丙说‘乙说的是真话’。如果只有一人说真话，谁说真话？",
    "一个人要把狼、羊和白菜运过河，小船每次只能带其中一个，怎样保证它们都安全过河？",
    "至少需要多少人，才能保证其中至少有 3 个人出生在同一个月份？请说明理由。",
    "找出数列 1、1、2、3、5、8 的规律，并写出后面 5 项。",
    "如果今天是星期三，100 天后是星期几？请写出计算过程。",
    "英语中 ‘used to do’、‘be used to doing’ 和 ‘be used to do’ 有什么区别？",
    "请帮我写一封简洁礼貌的邮件，向老师申请将作业截止时间延后两天。",
    "把‘持续学习比短期突击更容易形成稳定的能力’翻译成自然的英文，并解释关键词选择。",
    "中国近代洋务运动的主要目标、代表人物和历史局限是什么？",
    "东亚季风是怎样形成的？它为什么会造成夏季多雨、冬季干燥的气候特点？",
    "DNA 和 RNA 在结构、功能和分布上有哪些主要区别？",
    "通货膨胀为什么会降低货币购买力？对储蓄者和借款人通常有什么不同影响？",
    "一台 1500W 的电热水壶每天使用 20 分钟，连续使用 30 天大约耗电多少度？",
    "我每天能学习英语 40 分钟，请制定一个包含听、说、读、写的 4 周入门计划。",
    "家里的手机能连上 Wi-Fi 但无法上网，应按什么顺序排查问题？",
]

# 保留原变量名，方便其他代码仍以单问题方式导入该脚本。
USER_PROMPT = USER_PROMPTS[0]


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


def make_payload(args: argparse.Namespace, user_prompt: str) -> dict[str, Any]:
    return {
        "componentCode": args.component_code,
        "model": args.model,
        "messages": [
            {"role": "system", "content": args.system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": args.stream,
    }


def pick_text(data: Any) -> str:
    """从普通 JSON 或流式事件中递归提取模型回答。"""
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
        text = pick_text(data.get(key))
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


def read_response(
    response: requests.Response, stream: bool, start: float
) -> tuple[Any, int, int, float | None]:
    first_byte_ms = None
    response_bytes = 0
    stream_events = 0
    encoding = response_encoding(response)

    if not stream:
        chunks: list[bytes] = []
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            if first_byte_ms is None:
                first_byte_ms = (time.perf_counter() - start) * 1000
            response_bytes += len(chunk)
            chunks.append(chunk)

        text = b"".join(chunks).decode(encoding, errors="replace")
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

        if line.startswith("event:"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue

        stream_events += 1
        data = parse_json(line)
        text = pick_text(data)
        if text:
            text_parts.append(text)

    if text_parts:
        return "".join(text_parts), response_bytes, stream_events, first_byte_ms
    return "\n".join(raw_lines), response_bytes, stream_events, first_byte_ms


def round_ms(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def request_once(
    session: requests.Session,
    args: argparse.Namespace,
    question_index: int,
    question: str,
    attempt: int,
) -> dict[str, Any]:
    payload = make_payload(args, question)
    if args.print_payload:
        print("请求参数:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    start = time.perf_counter()
    response = session.post(
        args.url,
        headers=make_headers(args.app_key, args.secret_key),
        json=payload,
        verify=not args.insecure,
        stream=True,
        timeout=args.timeout,
    )

    try:
        header_ms = (time.perf_counter() - start) * 1000
        data, response_bytes, stream_events, first_byte_ms = read_response(
            response, args.stream, start
        )
        total_ms = (time.perf_counter() - start) * 1000
        answer = pick_text(data).strip()
        success = response.ok and bool(answer)

        error = ""
        if not response.ok:
            error = f"HTTP {response.status_code}"
        elif not answer:
            error = "模型响应成功，但没有提取到回答内容"

        return {
            "index": question_index,
            "question": question,
            "answer": answer,
            "success": success,
            "status_code": response.status_code,
            "attempts": attempt,
            "error": error,
            "metrics": {
                "header_ms": round_ms(header_ms),
                "first_byte_ms": round_ms(first_byte_ms),
                "total_ms": round_ms(total_ms),
                "response_bytes": response_bytes,
                "stream_events": stream_events,
            },
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    finally:
        response.close()


def request_with_retry(
    session: requests.Session,
    args: argparse.Namespace,
    question_index: int,
    question: str,
) -> dict[str, Any]:
    last_result: dict[str, Any] | None = None

    for attempt in range(1, args.retries + 2):
        try:
            result = request_once(session, args, question_index, question, attempt)
            last_result = result
            if result["success"]:
                return result

            status_code = result["status_code"]
            retryable = status_code == 429 or status_code >= 500 or not result["answer"]
            if not retryable:
                return result
        except requests.RequestException as exc:
            last_result = {
                "index": question_index,
                "question": question,
                "answer": "",
                "success": False,
                "status_code": None,
                "attempts": attempt,
                "error": str(exc),
                "metrics": {},
                "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }

        if attempt <= args.retries:
            wait_seconds = args.retry_wait * (2 ** (attempt - 1))
            print(f"  第 {attempt} 次请求失败，{wait_seconds:.1f} 秒后重试……")
            time.sleep(wait_seconds)

    assert last_result is not None
    return last_result


def select_questions(args: argparse.Namespace) -> list[str]:
    questions = args.user_prompts if args.user_prompts else USER_PROMPTS
    questions = [question.strip() for question in questions if question.strip()]
    if args.limit is not None:
        questions = questions[: args.limit]
    if not questions:
        raise ValueError("问题列表不能为空")
    return questions


def default_output_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(__file__).resolve().parent / "reports" / f"8b_answers_{timestamp}.jsonl"


def append_jsonl(output_path: Path, result: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(result, ensure_ascii=False) + "\n")
        file.flush()


def run_batch(args: argparse.Namespace) -> bool:
    if args.insecure:
        urllib3.disable_warnings(InsecureRequestWarning)

    questions = select_questions(args)
    output_path = Path(args.output).expanduser().resolve() if args.output else default_output_path()
    success_count = 0
    processed_count = 0
    batch_start = time.perf_counter()

    print(f"准备按顺序处理 {len(questions)} 个问题")
    print(f"结果文件: {output_path}")

    with requests.Session() as session:
        for index, question in enumerate(questions, start=1):
            print(f"\n[{index}/{len(questions)}] 用户: {question}")
            result = request_with_retry(session, args, index, question)
            append_jsonl(output_path, result)
            processed_count += 1

            if result["success"]:
                success_count += 1
                print(f"模型: {result['answer']}")
                metrics = result["metrics"]
                print(
                    "指标: "
                    f"状态码={result['status_code']}，"
                    f"首包={metrics.get('first_byte_ms')} ms，"
                    f"总耗时={metrics.get('total_ms')} ms，"
                    f"尝试次数={result['attempts']}"
                )
            else:
                print(f"请求失败: {result['error']}", file=sys.stderr)
                if args.stop_on_error:
                    print("已根据 --stop-on-error 停止后续请求。", file=sys.stderr)
                    break

            if args.interval > 0 and index < len(questions):
                time.sleep(args.interval)

    total_seconds = time.perf_counter() - batch_start
    print("\n批处理完成:")
    print(f"  本次计划: {len(questions)} 题")
    print(f"  实际处理: {processed_count} 题")
    print(f"  成功: {success_count} 题")
    print(f"  失败: {processed_count - success_count} 题")
    print(f"  总耗时: {total_seconds:.2f} 秒")
    print(f"  结果文件: {output_path}")
    return processed_count == len(questions) and success_count == len(questions)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按顺序向模型网关提交 50 个问题")
    parser.add_argument("--app-key", default=APP_KEY)
    parser.add_argument("--secret-key", default=SECRET_KEY)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--component-code", default=COMPONENT_CODE)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--system-prompt", default=SYSTEM_PROMPT)
    parser.add_argument(
        "--user-prompt",
        dest="user_prompts",
        action="append",
        help="临时指定问题；可重复传入多次。传入后将替代脚本内置的 50 个问题",
    )
    parser.add_argument("--limit", type=int, help="只处理前 N 个问题，便于测试")
    parser.add_argument("--output", help="JSONL 结果文件路径；默认写入 agent/reports")
    parser.add_argument("--retries", type=int, default=2, help="单题失败后的重试次数")
    parser.add_argument("--retry-wait", type=float, default=2.0, help="首次重试等待秒数")
    parser.add_argument("--interval", type=float, default=0.2, help="相邻问题之间的间隔秒数")
    parser.add_argument("--stop-on-error", action="store_true", help="某题最终失败后停止批处理")
    parser.add_argument("--stream", dest="stream", action="store_true", default=True)
    parser.add_argument("--no-stream", dest="stream", action="store_false")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--insecure", action="store_true", default=True)
    parser.add_argument("--verify-ssl", dest="insecure", action="store_false")
    parser.add_argument("--print-payload", action="store_true")
    args = parser.parse_args()

    if args.limit is not None and args.limit <= 0:
        parser.error("--limit 必须大于 0")
    if args.retries < 0:
        parser.error("--retries 不能小于 0")
    if args.retry_wait < 0 or args.interval < 0:
        parser.error("--retry-wait 和 --interval 不能小于 0")
    return args


if __name__ == "__main__":
    try:
        sys.exit(0 if run_batch(parse_args()) else 1)
    except (OSError, ValueError) as exc:
        print(f"执行失败: {exc}", file=sys.stderr)
        sys.exit(1)
