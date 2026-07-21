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

# Qwen3-VL-32B-Instruct AI 课程开发
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
# 默认按列表顺序逐题调用模型，共 50 个真实课程设计需求。
# 如需替换问题，直接修改此列表即可；也可以多次使用 --user-prompt 临时传入问题。
USER_PROMPTS = [
    "帮我生成一份《电力系统继电保护》课程的大纲，面向本科三年级学生，包含8个主要章节，重点涵盖基本原理、线路保护、主设备保护和新技术应用。",
    "为零基础大学生设计《Python 数据分析入门》课程大纲，共10章，覆盖 Python 基础、NumPy、Pandas、数据清洗、可视化和综合项目。",
    "生成一份面向有 Python 基础学员的《机器学习实战》课程大纲，包含12章，兼顾算法原理、特征工程、模型评估和部署。",
    "设计《Java Web 企业级开发》课程大纲，面向高职软件专业二年级学生，包含 Servlet、Spring Boot、数据库、安全和项目实训。",
    "为工科大一学生生成《C 语言程序设计》课程大纲，共9章，重点安排指针、结构体、文件操作和课程设计。",
    "设计《数据库原理与 MySQL 应用》课程大纲，面向计算机本科生，涵盖关系模型、SQL、索引、事务、优化与数据库设计。",
    "生成《计算机网络基础》课程大纲，共8章，按 TCP/IP 分层组织，并加入抓包分析和网络故障排查实践。",
    "设计《操作系统原理》课程大纲，面向计算机专业本科二年级学生，重点涵盖进程、内存、文件系统、并发与实验。",
    "为企业新员工设计《网络安全意识与实践》课程大纲，共6章，覆盖密码安全、钓鱼攻击、终端防护、数据安全和应急响应。",
    "生成《云计算基础与应用》课程大纲，面向有 Linux 基础的学习者，包含虚拟化、云服务模型、云原生、安全和迁移案例。",
    "设计《Docker 与 Kubernetes 实战》课程大纲，共10章，要求从容器基础递进到集群部署、可观测性和综合项目。",
    "为没有版本控制经验的开发者生成《Git 团队协作》课程大纲，包含分支、合并、冲突处理、代码评审和 CI 工作流。",
    "设计《现代前端开发基础》课程大纲，面向零基础学员，涵盖 HTML、CSS、JavaScript、Vue、工程化和响应式项目。",
    "生成《UI/UX 产品界面设计》课程大纲，面向设计初学者，包含用户研究、信息架构、交互原型、视觉规范和可用性测试。",
    "为互联网产品助理设计《产品经理入门》课程大纲，共8章，重点包括需求分析、原型设计、数据指标、项目协作和产品复盘。",
    "生成《项目管理实务》课程大纲，面向企业基层管理者，结合传统项目管理与敏捷方法，并设计一个贯穿式案例。",
    "设计《数字营销基础》课程大纲，共8章，涵盖市场洞察、内容营销、搜索营销、社交媒体、投放和效果评估。",
    "为短视频运营新人设计《短视频策划与运营》课程大纲，包含账号定位、脚本、拍摄、剪辑、发布、数据分析和合规。",
    "生成《新媒体写作》课程大纲，面向高校学生，共7章，重点训练选题、标题、结构、叙事、编辑和多平台改写。",
    "设计《商业数据分析》课程大纲，面向业务管理人员，包含指标体系、Excel、SQL、可视化、实验分析和决策案例。",
    "为非会计专业大学生生成《财务会计基础》课程大纲，共8章，涵盖会计循环、报表、资产负债、收入成本和案例练习。",
    "设计《个人投资理财入门》课程大纲，面向职场新人，覆盖风险收益、现金管理、基金、债券、股票、保险和资产配置。",
    "生成《微观经济学导论》课程大纲，面向本科一年级学生，包含供需、消费者、企业、市场结构、外部性和案例讨论。",
    "设计《宏观经济学基础》课程大纲，共9章，涵盖国民收入、通胀、失业、经济增长、财政政策和货币政策。",
    "为理工科新生生成《高等数学基础》课程大纲，包含函数极限、微分、积分、微分方程和数学建模应用。",
    "设计《线性代数及其应用》课程大纲，共8章，要求将矩阵、线性方程组、向量空间、特征值与数据应用结合。",
    "生成《概率论与数理统计》课程大纲，面向工科本科生，包含概率模型、随机变量、参数估计、假设检验和实验。",
    "设计《大学物理：力学与电磁学》课程大纲，共10章，兼顾概念、公式推导、演示实验和工程案例。",
    "为电气工程专业学生生成《电路分析基础》课程大纲，覆盖直流、交流、暂态、三相电路、仿真和实验。",
    "设计《电力电子技术》课程大纲，面向本科三年级学生，包含功率器件、整流、逆变、变换器控制和仿真实训。",
    "生成《新能源发电技术》课程大纲，共8章，重点涵盖光伏、风电、储能、并网控制、经济性和安全。",
    "设计《智能电网基础》课程大纲，面向电气类本科生，包含通信、调度、量测、需求响应、微电网和网络安全。",
    "为自动化专业学生生成《PLC 控制技术》课程大纲，要求包含硬件、指令、顺序控制、通信、故障诊断和实训项目。",
    "设计《工业机器人编程与应用》课程大纲，共9章，涵盖安全、坐标系、示教编程、轨迹、视觉和工作站集成。",
    "生成《机械 CAD 制图》课程大纲，面向高职一年级学生，包含制图标准、二维绘图、零件图、装配图和综合实训。",
    "设计《工程材料基础》课程大纲，面向机械类本科生，涵盖材料结构、性能、热处理、选材、失效和实验。",
    "为化学专业本科生生成《有机化学基础》课程大纲，共12章，突出结构与反应机理，并安排实验安全与合成实践。",
    "设计《环境监测技术》课程大纲，面向环境工程学生，覆盖采样、质量控制、水气土监测、仪器分析和项目报告。",
    "生成《分子生物学基础》课程大纲，共9章，涵盖 DNA、RNA、蛋白质表达、调控、实验技术和前沿应用。",
    "设计《生物信息学入门》课程大纲，面向有生物学基础但编程较弱的学生，包含数据库、序列分析、组学和实践。",
    "为研究生新生生成《学术论文写作与规范》课程大纲，共7章，重点覆盖选题、文献综述、论证、引用、投稿和学术诚信。",
    "设计《大学英语口语》课程大纲，面向非英语专业一年级学生，包含发音、日常交流、课堂讨论、演讲和情景任务。",
    "生成《商务英语沟通》课程大纲，共8章，覆盖邮件、会议、谈判、演示、跨文化沟通和商务情景模拟。",
    "为中小学教师设计《人工智能素养与教学应用》课程大纲，包含基本概念、提示词、备课、评价、伦理和课堂实践。",
    "设计《教学设计方法》课程大纲，面向新入职高校教师，涵盖学习目标、内容组织、教学活动、评价和课程迭代。",
    "为行政办公人员生成《Excel 高效办公》课程大纲，共8章，包含数据规范、函数、透视表、图表、自动化和综合案例。",
    "设计《职场演讲与汇报》课程大纲，面向入职1至3年的员工，覆盖结构化表达、PPT、数据呈现、现场表达和答问。",
    "生成《基层管理者领导力》课程大纲，共7章，重点包括角色转变、目标管理、授权、反馈、冲突处理和团队激励。",
    "设计《公众急救基础》课程大纲，面向无医学背景的成年人，包含现场评估、心肺复苏、AED、创伤和常见急症。",
    "为老年学习者生成《智能手机基础应用》课程大纲，共6章，涵盖基本操作、通信、移动支付、就医出行和反诈安全。",
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

    return "".join(text_parts) if text_parts else "\n".join(raw_lines), response_bytes, stream_events, first_byte_ms


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
    return Path(__file__).resolve().parent / "reports" / f"32b_answers_{timestamp}.jsonl"


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

    print(f"准备按顺序处理 {len(questions)} 个课程设计问题")
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
    parser = argparse.ArgumentParser(description="按顺序向 32B 模型提交 50 个课程设计问题")
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
