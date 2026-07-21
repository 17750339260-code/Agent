# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
AI Gateway Qwen2.5-Omni-7B multimodal pressure test script.

Key metrics:
- Stream TTFT is measured at the first non-empty generated content token.
- Non-stream "TTFT" is reported as full response latency, because the API does
  not expose first-token timing without streaming.
- Output token count prefers API usage fields, then an optional tokenizer,
  then optional tiktoken BPE, then a rough local estimate.
- Scenario throughput is total successful output tokens / scenario wall time.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import random
import re
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from tqdm import tqdm


# DEFAULT_MODEL = "Qwen2.5-Omni-7B"
# DEFAULT_COMPONENT_CODE = "04350560"
# DEFAULT_URL = "https://192.168.0.213:18300/ai-inference-gateway/predict"

DEFAULT_MODEL = "Qwen2.5-Omni-7B"
DEFAULT_COMPONENT_CODE = "04100831"
DEFAULT_URL = "https://10.10.65.213:18300/ai-inference-gateway/predict"

DEFAULT_IMAGE_DIR = str(Path(__file__).resolve().parent / "images")
MAX_CONTEXT_TOKENS = 32_000


SLA_DEFINITIONS = [
    {
        "id": "context_le_32k",
        "name": "输入上下文 token 长度 <= 32K",
        "target": "<= 32000 tokens",
    },
    {
        "id": "concurrency_success",
        "name": "目标模型并发 >= 60，响应成功率 >= 90%",
        "target": "concurrency >= 60, success >= 90%",
    },
    {
        "id": "single_image_1024_stream_ttft",
        "name": "单并发，输入 1024 tokens + 图片，流式 TTFT <= 7000ms",
        "target": "<= 7000 ms",
    },
    {
        "id": "single_image_1024_nonstream_ttft_p99",
        "name": "单并发，输入 1024 tokens + 图片，非流式 TTFT(P99) <= 10000ms",
        "target": "<= 10000 ms",
    },
    {
        "id": "concurrent_60_stream_p99_15s",
        "name": "60 并发，流式响应延迟 P99 <= 15000ms",
        "target": "<= 15000 ms",
    },
    {
        "id": "concurrent_60_nonstream_p99_15s",
        "name": "60 并发，非流式响应延迟 P99 <= 15000ms",
        "target": "<= 15000 ms",
    },
    {
        "id": "concurrent_60_stream_tokps",
        "name": "60 并发，流式模型吞吐量 >= 4.40 tok/s",
        "target": ">= 4.40 tok/s",
    },
    {
        "id": "concurrent_60_nonstream_tokps",
        "name": "60 并发，非流式模型吞吐量 >= 4.40 tok/s",
        "target": ">= 4.40 tok/s",
    },
    {
        "id": "single_response_15s",
        "name": "单并发响应时间 <= 15s",
        "target": "<= 15000 ms",
    },
    {
        "id": "server_cpu",
        "name": "服务器 CPU 资源使用率 <= 65%",
        "target": "<= 65%",
    },
    {
        "id": "server_memory",
        "name": "服务器内存使用率 <= 70%",
        "target": "<= 70%",
    },
    {
        "id": "single_text_128_stream_ttft",
        "name": "单请求，输入 128 tokens，TTFT <= 3000ms",
        "target": "<= 3000 ms",
    },
    {
        "id": "concurrent_60_stream_p99_7s",
        "name": "60 并发，流式响应延迟 P99 <= 7000ms",
        "target": "<= 7000 ms",
    },
    {
        "id": "concurrent_60_nonstream_p99_7s",
        "name": "60 并发，非流式响应延迟 P99 <= 7000ms",
        "target": "<= 7000 ms",
    },
]


HTTP_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
HTTP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def utc_http_date() -> str:
    """Locale-independent RFC 7231 date for HMAC signing."""
    now = datetime.now(timezone.utc)
    return (
        f"{HTTP_WEEKDAYS[now.weekday()]}, {now.day:02d} "
        f"{HTTP_MONTHS[now.month - 1]} {now.year:04d} "
        f"{now.hour:02d}:{now.minute:02d}:{now.second:02d} GMT"
    )


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def avg(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def pass_fail(value: Optional[float], operator: str, target: float) -> str:
    # 没有成功样本时返回 N/A，避免把 0ms 误判为达标。
    if value is None:
        return "N/A"
    if operator == "<=":
        return "PASS" if value <= target else "FAIL"
    if operator == ">=":
        return "PASS" if value >= target else "FAIL"
    raise ValueError(f"unsupported operator: {operator}")


def safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def nested_get(data: Dict[str, Any], paths: Sequence[Tuple[str, ...]]) -> Any:
    for path in paths:
        current: Any = data
        found = True
        for key in path:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                found = False
                break
        if found:
            return current
    return None


def extract_usage(data: Dict[str, Any]) -> Dict[str, Optional[int]]:
    usage = nested_get(
        data,
        (
            ("usage",),
            ("token_usage",),
            ("response_metadata", "token_usage"),
            ("response_metadata", "usage"),
        ),
    )
    if not isinstance(usage, dict):
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    def first_int(keys: Sequence[str]) -> Optional[int]:
        for key in keys:
            value = usage.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return None

    return {
        "prompt_tokens": first_int(("prompt_tokens", "input_tokens", "prompt_token_count")),
        "completion_tokens": first_int(("completion_tokens", "output_tokens", "generated_tokens", "completion_token_count")),
        "total_tokens": first_int(("total_tokens", "total_token_count")),
    }


def extract_delta_content(data: Dict[str, Any]) -> str:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            delta = choice.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    return content
            message = choice.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
            text = choice.get("text")
            if isinstance(text, str):
                return text
    for key in ("content", "text", "output_text"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return ""


def parse_json_event(data_text: str) -> Optional[Dict[str, Any]]:
    data_text = data_text.strip()
    if not data_text or data_text == "[DONE]":
        return None
    try:
        value = json.loads(data_text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class TokenCounter:
    def __init__(self, tokenizer_name: Optional[str] = None, tiktoken_encoding: Optional[str] = "cl100k_base"):
        self.tokenizer_name = tokenizer_name
        self.tokenizer = self._load_tokenizer(tokenizer_name)
        self.tiktoken_encoding_name = tiktoken_encoding if self.tokenizer is None else None
        self.tiktoken_encoding = self._load_tiktoken(self.tiktoken_encoding_name)
        if self.tiktoken_encoding is None:
            self.tiktoken_encoding_name = None

    @staticmethod
    def _load_tokenizer(tokenizer_name: Optional[str]) -> Any:
        if not tokenizer_name:
            return None
        try:
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise RuntimeError("--tokenizer-name requires transformers. Install transformers or omit the option.") from exc
        return AutoTokenizer.from_pretrained(tokenizer_name, trust_remote_code=True)

    @staticmethod
    def _load_tiktoken(encoding_name: Optional[str]) -> Any:
        if not encoding_name:
            return None
        try:
            import tiktoken
        except ImportError:
            return None
        return tiktoken.get_encoding(encoding_name)

    def count_text(self, text: str) -> Tuple[int, str]:
        if not text:
            return 0, "empty"
        if self.tokenizer is not None:
            return len(self.tokenizer.encode(text, add_special_tokens=False)), "tokenizer"
        if self.tiktoken_encoding is not None:
            return len(self.tiktoken_encoding.encode(text)), f"tiktoken:{self.tiktoken_encoding_name}"

        cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        words = len(re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u4e00-\u9fff]", text))
        return max(1, cjk_chars + words), "estimated"

    def make_text(self, token_count: int, prefix: str) -> Tuple[str, int, str]:
        words = [
            "vision", "safety", "inspection", "transformer", "substation", "equipment",
            "analysis", "risk", "cable", "switch", "meter", "grounding", "status",
            "latency", "throughput", "request", "response", "quality", "stable",
            "image", "context", "model", "token", "benchmark", "operation", "power",
        ]
        if token_count <= 0:
            raise ValueError("token_count must be positive")

        if self.tokenizer is None and self.tiktoken_encoding is None:
            body = " ".join(words[i % len(words)] for i in range(token_count))
            text = f"{prefix}\n{body}"
            actual, source = self.count_text(text)
            return text, actual, source

        text = prefix + "\n"
        while True:
            text += " ".join(words) + " "
            if self.tokenizer is not None:
                ids = self.tokenizer.encode(text, add_special_tokens=False)
            else:
                ids = self.tiktoken_encoding.encode(text)
            if len(ids) >= token_count:
                if self.tokenizer is not None:
                    trimmed = self.tokenizer.decode(ids[:token_count], skip_special_tokens=True)
                else:
                    trimmed = self.tiktoken_encoding.decode(ids[:token_count])
                actual, source = self.count_text(trimmed)
                return trimmed, actual, source


class ResourceMonitor:
    """Collects local CPU/memory. Run this script on the server for server resource SLA."""

    def __init__(self, interval_s: float = 1.0):
        self.interval_s = interval_s
        self.samples: List[Tuple[float, float]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        try:
            import psutil  # type: ignore

            self.psutil = psutil
        except ImportError:
            self.psutil = None

    def start(self) -> None:
        if self.psutil is None:
            return
        # Prime psutil's CPU baseline and discard the initial meaningless value.
        self.psutil.cpu_percent(interval=None)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="resource-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval_s + 0.5)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            cpu = float(self.psutil.cpu_percent(interval=None))
            mem = float(self.psutil.virtual_memory().percent)
            self.samples.append((cpu, mem))

    def snapshot(self) -> Dict[str, Optional[float]]:
        if not self.samples:
            return {
                "resource_samples": 0,
                "max_cpu_pct": None,
                "avg_cpu_pct": None,
                "max_memory_pct": None,
                "avg_memory_pct": None,
            }
        cpus = [s[0] for s in self.samples]
        mems = [s[1] for s in self.samples]
        return {
            "resource_samples": len(self.samples),
            "max_cpu_pct": max(cpus),
            "avg_cpu_pct": avg(cpus),
            "max_memory_pct": max(mems),
            "avg_memory_pct": avg(mems),
        }


@dataclass
class PromptSpec:
    name: str
    input_tokens: int
    max_tokens: int
    needs_image: bool
    content: str
    actual_input_tokens: int
    token_source: str


@dataclass
class Sample:
    request_id: int
    scenario: str
    prompt_name: str
    stream: bool
    has_image: bool
    image_name: str
    success: bool
    status_code: int
    error: str
    response_time_ms: float
    adjusted_response_time_ms: float
    ttft_ms: float
    ttft_source: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    token_count_source: str
    output_tokens_per_second: float
    request_size_bytes: int
    response_chars: int


@dataclass
class ScenarioResult:
    test: str
    concurrent: int
    stream: bool
    input_tokens_target: int
    has_image: bool
    total_requests: int
    success_count: int
    failed_count: int
    success_rate: float
    total_duration_s: float
    request_qps: float
    output_token_throughput: float
    avg_output_tokens: float
    max_input_tokens: int
    avg_response_ms: float
    p50_response_ms: float
    p90_response_ms: float
    p95_response_ms: float
    p99_response_ms: float
    max_response_ms: float
    avg_ttft_ms: float
    p50_ttft_ms: float
    p95_ttft_ms: float
    p99_ttft_ms: float
    max_cpu_pct: Optional[float]
    max_memory_pct: Optional[float]
    token_count_sources: str


class MultiModalTester:
    def __init__(
        self,
        url: str,
        app_key: str,
        secret_key: str,
        component_code: str = DEFAULT_COMPONENT_CODE,
        model: str = DEFAULT_MODEL,
        image_dir: str = "./images",
        single_image: Optional[str] = None,
        output_dir: str = ".",
        timeout_s: int = 600,
        connect_timeout_s: float = 10.0,
        upload_timeout_s: Optional[float] = None,
        read_timeout_s: Optional[float] = None,
        verify_ssl: bool = False,
        tokenizer_name: Optional[str] = None,
        network_latency_ms: float = 0.0,
        network_latency_factor: float = 0.5,
        tiktoken_encoding: Optional[str] = "cl100k_base",
        max_image_bytes: int = 2 * 1024 * 1024,
        max_image_side: int = 1600,
        image_quality: int = 85,
    ):
        if not app_key:
            raise ValueError("app_key is required. Use --app-key or AI_GATEWAY_APP_KEY.")
        if not secret_key:
            raise ValueError("secret_key is required. Use --secret-key or AI_GATEWAY_SECRET_KEY.")

        self.url = url
        self.app_key = app_key
        self.secret_key = secret_key
        self.component_code = component_code
        self.model = model
        self.image_dir = image_dir
        self.single_image = single_image
        self.output_dir = Path(output_dir)
        self.timeout_s = timeout_s
        self.connect_timeout_s = max(0.1, float(connect_timeout_s))
        self.upload_timeout_s = max(0.1, float(upload_timeout_s if upload_timeout_s is not None else max(60.0, self.connect_timeout_s)))
        self.read_timeout_s = max(0.1, float(read_timeout_s if read_timeout_s is not None else timeout_s))
        # requests/urllib3 do not expose a separate write timeout. A slow HTTPS
        # upload can surface as "The write operation timed out", so use the
        # larger upload timeout in the connect slot.
        self.request_timeout = (self.upload_timeout_s, self.read_timeout_s)
        self.verify_ssl = verify_ssl
        self.network_latency_ms = max(0.0, float(network_latency_ms))
        self.network_latency_factor = min(1.0, max(0.0, float(network_latency_factor)))
        self.token_counter = TokenCounter(tokenizer_name, tiktoken_encoding)
        self.max_image_bytes = max(0, int(max_image_bytes))
        self.max_image_side = max(64, int(max_image_side))
        self.image_quality = min(95, max(10, int(image_quality)))

        self.lock = threading.Lock()
        self._session_local = threading.local()
        self._session_lock = threading.Lock()
        self._sessions: List[requests.Session] = []
        self.samples: List[Sample] = []
        self.images = self._load_test_images()

        print("初始化完成")
        print(f"  URL: {self.url}")
        print(f"  模型: {self.model}")
        print(f"  图片数: {len(self.images)}")
        if tokenizer_name:
            token_counter_desc = f"API usage 优先，缺失时 Hugging Face tokenizer:{tokenizer_name}"
        elif self.token_counter.tiktoken_encoding_name:
            token_counter_desc = f"API usage 优先，缺失时 tiktoken:{self.token_counter.tiktoken_encoding_name}"
        else:
            token_counter_desc = "API usage 优先，缺失时本地粗估"
        print(f"  token 计数: {token_counter_desc}")
        print(
            f"  网络延迟扣减: {self.network_latency_ms:.0f}ms x {self.network_latency_factor:.2f} "
            f"= {self.network_adjustment_ms:.0f}ms"
        )
        print(
            f"  请求超时: connect={self.connect_timeout_s:.1f}s, "
            f"upload/write={self.upload_timeout_s:.1f}s, read={self.read_timeout_s:.1f}s"
        )
        if self.max_image_bytes > 0:
            print(
                f"  图片预处理: >{self.max_image_bytes} bytes 时压缩, "
                f"max_side={self.max_image_side}, quality={self.image_quality}"
            )

    @property
    def network_adjustment_ms(self) -> float:
        return self.network_latency_ms * self.network_latency_factor

    def _get_session(self) -> requests.Session:
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = requests.Session()
            self._session_local.session = session
            with self._session_lock:
                self._sessions.append(session)
        return session

    def _close_sessions(self) -> None:
        with self._session_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        self._session_local = threading.local()
        for session in sessions:
            session.close()

    def _load_test_images(self) -> List[Dict[str, Any]]:
        paths: List[Path] = []
        if self.single_image:
            paths.append(Path(self.single_image))
        else:
            image_dir = Path(self.image_dir)
            if not image_dir.exists():
                image_dir.mkdir(parents=True, exist_ok=True)
                self._create_test_image(image_dir / "test_equipment.png")
            supported = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif"}
            paths.extend(p for p in image_dir.iterdir() if p.suffix.lower() in supported)

        images = []
        for path in paths:
            if not path.exists():
                print(f"警告: 图片不存在: {path}")
                continue
            mime_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
            data = path.read_bytes()
            original_size = len(data)
            data, mime_type, compressed = self._maybe_compress_image(data, mime_type, path)
            image_b64 = base64.b64encode(data).decode("utf-8")
            images.append(
                {
                    "filename": path.name,
                    "url": f"data:{mime_type};base64,{image_b64}",
                    "mime_type": mime_type,
                    "size_bytes": len(data),
                    "original_size_bytes": original_size,
                    "compressed": compressed,
                }
            )
            if compressed:
                print(f"  加载图片: {path.name} ({original_size} -> {len(data)} bytes)")
            else:
                print(f"  加载图片: {path.name} ({len(data)} bytes)")
        if not images:
            print("警告: 未找到测试图片，图片场景会被跳过")
        return images

    def _maybe_compress_image(self, data: bytes, mime_type: str, path: Path) -> Tuple[bytes, str, bool]:
        if self.max_image_bytes <= 0 or len(data) <= self.max_image_bytes:
            return data, mime_type, False

        try:
            from PIL import Image, ImageOps
        except ImportError:
            print(
                f"警告: {path.name} 大小 {len(data)} bytes，未安装 Pillow，无法压缩。"
                "请安装 Pillow 或使用 --single-image 选择较小图片。"
            )
            return data, mime_type, False

        try:
            with Image.open(io.BytesIO(data)) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                image.thumbnail((self.max_image_side, self.max_image_side))

                quality = self.image_quality
                current = image
                best = data
                for _ in range(8):
                    buf = io.BytesIO()
                    current.save(buf, format="JPEG", quality=quality, optimize=True)
                    candidate = buf.getvalue()
                    best = candidate
                    if len(candidate) <= self.max_image_bytes:
                        return candidate, "image/jpeg", True
                    if quality > 45:
                        quality = max(45, quality - 10)
                    else:
                        ratio = max(0.5, (self.max_image_bytes / len(candidate)) ** 0.5 * 0.92)
                        next_size = (
                            max(64, int(current.width * ratio)),
                            max(64, int(current.height * ratio)),
                        )
                        if next_size == current.size:
                            break
                        current = current.resize(next_size)
                return best, "image/jpeg", True
        except Exception as exc:
            print(f"警告: {path.name} 压缩失败，继续使用原图: {exc}")
            return data, mime_type, False

    @staticmethod
    def _create_test_image(path: Path) -> None:
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            print(f"图片目录为空: {path.parent}。安装 Pillow 可自动生成测试图，或手动放入图片。")
            return

        img = Image.new("RGB", (800, 600), color=(235, 238, 240))
        draw = ImageDraw.Draw(img)
        draw.rectangle([50, 50, 750, 550], outline=(30, 65, 120), width=4)
        draw.rectangle([190, 330, 610, 500], outline=(20, 120, 160), width=3)
        draw.text((260, 240), "Power Equipment Test Image", fill=(0, 0, 0))
        draw.text((315, 415), "Transformer Area", fill=(0, 70, 120))
        img.save(path)
        print(f"已生成测试图片: {path}")

    def generate_auth_headers(self) -> Dict[str, str]:
        curl_date = utc_http_date()
        date_str = f"x-date: {curl_date}"
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            date_str.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature_b64 = base64.b64encode(signature).decode("utf-8")
        authorization = (
            f'hmac username="{self.app_key}", algorithm="hmac-sha256", '
            f'headers="x-date", signature="{signature_b64}"'
        )
        return {
            "x-date": curl_date,
            "Authorization": authorization,
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        }

    def make_prompt(self, name: str, input_tokens: int, max_tokens: int, needs_image: bool) -> PromptSpec:
        if needs_image:
            prefix = (
                "请结合图片完成专业分析，重点说明场景、设备、风险、异常点和改进建议。"
                "以下是用于压测的上下文文本，请在回答中保持结构清晰。"
            )
        else:
            prefix = "请回答以下问题，并保持结论明确。以下是用于压测的上下文文本。"
        content, actual, source = self.token_counter.make_text(input_tokens, prefix)
        return PromptSpec(name, input_tokens, max_tokens, needs_image, content, actual, source)

    @staticmethod
    def _build_messages(prompt: PromptSpec, image: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        system = {"role": "system", "content": "你是一个专业的多模态 AI 助手，能够分析图片和文本内容。"}
        if prompt.needs_image:
            if image is None:
                raise ValueError("image scenario requires at least one image")
            user_content: Any = [
                {"type": "text", "text": prompt.content},
                {"type": "image_url", "image_url": {"url": image["url"]}},
            ]
        else:
            user_content = prompt.content
        return [system, {"role": "user", "content": user_content}]

    def _build_payload(self, prompt: PromptSpec, image: Optional[Dict[str, Any]], stream: bool) -> Dict[str, Any]:
        return {
            "componentCode": self.component_code,
            "model": self.model,
            "messages": self._build_messages(prompt, image),
            "stream": stream,
            "temperature": 0.2,
            "max_tokens": prompt.max_tokens,
            "top_p": 0.9,
        }

    def _count_output_tokens(self, text: str, usage: Dict[str, Optional[int]]) -> Tuple[int, str]:
        completion_tokens = usage.get("completion_tokens")
        if completion_tokens is not None:
            return completion_tokens, "usage"
        count, source = self.token_counter.count_text(text)
        return count, source

    def _record_sample(self, sample: Sample) -> None:
        with self.lock:
            self.samples.append(sample)

    def send_request(self, request_id: int, scenario: str, prompt: PromptSpec, stream: bool) -> None:
        # 单请求执行入口：负责发请求、解析 SSE、计 TTFT、计 token，并写入样本。
        start_time = time.perf_counter()
        first_token_time: Optional[float] = None
        response_text = ""
        usage = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
        status_code = 0
        image: Optional[Dict[str, Any]] = None

        try:
            image = random.choice(self.images) if prompt.needs_image and self.images else None
            payload = self._build_payload(prompt, image, stream)
            request_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            headers = self.generate_auth_headers()
            session = self._get_session()

            with session.post(
                self.url,
                headers=headers,
                json=payload,
                verify=self.verify_ssl,
                stream=stream,
                timeout=self.request_timeout,
            ) as response:
                status_code = response.status_code
                if status_code != 200:
                    error_text = response.text[:500]
                    raise RuntimeError(f"HTTP {status_code}: {error_text}")

                if stream:
                    for raw_line in response.iter_lines(decode_unicode=False):
                        if not raw_line:
                            continue
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if line.startswith("data:"):
                            data_text = line[5:].strip()
                        else:
                            data_text = line

                        event = parse_json_event(data_text)
                        if event is None:
                            continue

                        event_usage = extract_usage(event)
                        usage = {k: event_usage.get(k) if event_usage.get(k) is not None else usage.get(k) for k in usage}

                        content = extract_delta_content(event)
                        if content:
                            if first_token_time is None:
                                first_token_time = time.perf_counter()
                            response_text += content
                else:
                    response_data = response.json()
                    response_text = extract_delta_content(response_data)
                    usage = extract_usage(response_data)

            end_time = time.perf_counter()
            response_time_ms = (end_time - start_time) * 1000
            if stream and first_token_time is not None:
                raw_ttft_ms = (first_token_time - start_time) * 1000
                ttft_source = "first_non_empty_stream_content"
            else:
                raw_ttft_ms = response_time_ms
                ttft_source = "non_stream_full_response_latency" if not stream else "fallback_full_response_latency"

            output_tokens, token_source = self._count_output_tokens(response_text, usage)
            prompt_tokens = usage.get("prompt_tokens")
            total_usage_tokens = usage.get("total_tokens")
            input_tokens = prompt_tokens if prompt_tokens is not None else prompt.actual_input_tokens
            total_tokens = total_usage_tokens if total_usage_tokens is not None else (input_tokens + output_tokens)
            adjusted_response_ms = max(0.0, response_time_ms - self.network_adjustment_ms)
            adjusted_ttft_ms = max(0.0, raw_ttft_ms - self.network_adjustment_ms)
            output_tps = output_tokens / (response_time_ms / 1000) if response_time_ms > 0 else 0.0

            sample = Sample(
                request_id=request_id,
                scenario=scenario,
                prompt_name=prompt.name,
                stream=stream,
                has_image=image is not None,
                image_name=image["filename"] if image else "none",
                success=True,
                status_code=status_code,
                error="",
                response_time_ms=response_time_ms,
                adjusted_response_time_ms=adjusted_response_ms,
                ttft_ms=adjusted_ttft_ms,
                ttft_source=ttft_source,
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                total_tokens=int(total_tokens),
                token_count_source=token_source,
                output_tokens_per_second=output_tps,
                request_size_bytes=request_size,
                response_chars=len(response_text),
            )
            self._record_sample(sample)

        except Exception as exc:
            end_time = time.perf_counter()
            response_time_ms = (end_time - start_time) * 1000
            adjusted_elapsed_ms = max(0.0, response_time_ms - self.network_adjustment_ms)
            self._record_sample(
                Sample(
                    request_id=request_id,
                    scenario=scenario,
                    prompt_name=prompt.name,
                    stream=stream,
                    has_image=image is not None,
                    image_name=image["filename"] if image else "none",
                    success=False,
                    status_code=status_code,
                    error=str(exc)[:500],
                    response_time_ms=response_time_ms,
                    adjusted_response_time_ms=adjusted_elapsed_ms,
                    ttft_ms=adjusted_elapsed_ms,
                    ttft_source="error_elapsed_time",
                    input_tokens=prompt.actual_input_tokens,
                    output_tokens=0,
                    total_tokens=prompt.actual_input_tokens,
                    token_count_source="none",
                    output_tokens_per_second=0.0,
                    request_size_bytes=0,
                    response_chars=0,
                )
            )

    def run_scenario(
        self,
        name: str,
        concurrent_level: int,
        total_requests: int,
        stream: bool,
        input_tokens: int,
        max_tokens: int,
        needs_image: bool,
    ) -> ScenarioResult:
        if needs_image and not self.images:
            raise RuntimeError(f"scenario {name} needs image but no image is available")

        prompt = self.make_prompt(name, input_tokens, max_tokens, needs_image)
        print("\n" + "=" * 88)
        print(
            f"场景: {name} | 并发={concurrent_level} | 请求数={total_requests} | "
            f"{'流式' if stream else '非流式'} | 输入={prompt.actual_input_tokens} tokens | "
            f"图片={'是' if needs_image else '否'}"
        )
        print("=" * 88)
        if prompt.actual_input_tokens > MAX_CONTEXT_TOKENS:
            print(f"警告: 当前输入 token 估算 {prompt.actual_input_tokens} > {MAX_CONTEXT_TOKENS}")

        scenario_start = time.perf_counter()
        before_count = len(self.samples)
        monitor = ResourceMonitor()
        monitor.start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrent_level) as executor:
            futures = [
                executor.submit(self.send_request, i + 1, name, prompt, stream)
                for i in range(total_requests)
            ]
            for future in tqdm(concurrent.futures.as_completed(futures), total=total_requests, unit="req"):
                future.result()
        monitor.stop()
        self._close_sessions()
        total_duration_s = time.perf_counter() - scenario_start

        scenario_samples = self.samples[before_count:]
        result = self._summarize_scenario(
            name=name,
            concurrent_level=concurrent_level,
            stream=stream,
            input_tokens_target=input_tokens,
            has_image=needs_image,
            total_duration_s=total_duration_s,
            samples=scenario_samples,
            resource=monitor.snapshot(),
        )
        self._print_scenario_result(result, scenario_samples)
        return result

    def _summarize_scenario(
        self,
        name: str,
        concurrent_level: int,
        stream: bool,
        input_tokens_target: int,
        has_image: bool,
        total_duration_s: float,
        samples: Sequence[Sample],
        resource: Dict[str, Optional[float]],
    ) -> ScenarioResult:
        # 汇总只基于成功样本计算延迟分位数；全失败时延迟值保留为 0，
        # SLA 判定阶段会用 success_count 转成 N/A，避免假阳性。
        success_samples = [s for s in samples if s.success]
        failed_count = len(samples) - len(success_samples)
        success_count = len(success_samples)
        success_rate = success_count / len(samples) * 100 if samples else 0.0
        response_times = [s.adjusted_response_time_ms for s in success_samples]
        ttft_times = [s.ttft_ms for s in success_samples]
        output_tokens = [s.output_tokens for s in success_samples]
        total_output_tokens = sum(output_tokens)
        token_sources = sorted(set(s.token_count_source for s in success_samples))

        return ScenarioResult(
            test=name,
            concurrent=concurrent_level,
            stream=stream,
            input_tokens_target=input_tokens_target,
            has_image=has_image,
            total_requests=len(samples),
            success_count=success_count,
            failed_count=failed_count,
            success_rate=success_rate,
            total_duration_s=total_duration_s,
            request_qps=len(samples) / total_duration_s if total_duration_s > 0 else 0.0,
            output_token_throughput=total_output_tokens / total_duration_s if total_duration_s > 0 else 0.0,
            avg_output_tokens=avg(output_tokens),
            max_input_tokens=max((s.input_tokens for s in success_samples), default=input_tokens_target),
            avg_response_ms=avg(response_times),
            p50_response_ms=percentile(response_times, 50),
            p90_response_ms=percentile(response_times, 90),
            p95_response_ms=percentile(response_times, 95),
            p99_response_ms=percentile(response_times, 99),
            max_response_ms=max(response_times, default=0.0),
            avg_ttft_ms=avg(ttft_times),
            p50_ttft_ms=percentile(ttft_times, 50),
            p95_ttft_ms=percentile(ttft_times, 95),
            p99_ttft_ms=percentile(ttft_times, 99),
            max_cpu_pct=safe_float(resource.get("max_cpu_pct")),
            max_memory_pct=safe_float(resource.get("max_memory_pct")),
            token_count_sources=",".join(token_sources) if token_sources else "none",
        )

    @staticmethod
    def _print_scenario_result(result: ScenarioResult, samples: Sequence[Sample]) -> None:
        print("\n场景结果")
        print(f"  总请求数: {result.total_requests}")
        print(f"  成功/失败: {result.success_count}/{result.failed_count}")
        print(f"  成功率: {result.success_rate:.2f}%")
        print(f"  总耗时: {result.total_duration_s:.2f}s")
        print(f"  请求 QPS: {result.request_qps:.2f} req/s")
        print(f"  输出 token 吞吐量: {result.output_token_throughput:.2f} tok/s")
        print(f"  平均输出 token/请求: {result.avg_output_tokens:.1f}")
        print(f"  最大输入 token: {result.max_input_tokens}")
        print(f"  token 计数来源: {result.token_count_sources}")
        print("  响应延迟(ms, 已按配置扣减网络延迟): "
              f"avg={result.avg_response_ms:.0f}, p50={result.p50_response_ms:.0f}, "
              f"p90={result.p90_response_ms:.0f}, p95={result.p95_response_ms:.0f}, "
              f"p99={result.p99_response_ms:.0f}, max={result.max_response_ms:.0f}")
        print("  TTFT(ms, 已按配置扣减网络延迟): "
              f"avg={result.avg_ttft_ms:.0f}, p50={result.p50_ttft_ms:.0f}, "
              f"p95={result.p95_ttft_ms:.0f}, p99={result.p99_ttft_ms:.0f}")
        print(
            "  资源使用率(本机；脚本跑在服务器上时即服务器): "
            f"CPU max={format_optional(result.max_cpu_pct, '%')}, "
            f"MEM max={format_optional(result.max_memory_pct, '%')}"
        )

        failed = [s for s in samples if not s.success]
        if failed:
            counts = defaultdict(int)
            for sample in failed:
                key = sample.error.splitlines()[0][:120]
                counts[key] += 1
            print("  失败原因:")
            for error, count in counts.items():
                print(f"    {count} x {error}")

    def run_full_test_suite(self, total_60: int = 60) -> List[ScenarioResult]:
        results: List[ScenarioResult] = []

        results.append(
            self.run_scenario(
                name="single_text_128_stream",
                concurrent_level=1,
                total_requests=1,
                stream=True,
                input_tokens=128,
                max_tokens=256,
                needs_image=False,
            )
        )

        if self.images:
            results.append(
                self.run_scenario(
                    name="single_image_1024_stream",
                    concurrent_level=1,
                    total_requests=1,
                    stream=True,
                    input_tokens=1024,
                    max_tokens=512,
                    needs_image=True,
                )
            )
            results.append(
                self.run_scenario(
                    name="single_image_1024_nonstream",
                    concurrent_level=1,
                    total_requests=1,
                    stream=False,
                    input_tokens=1024,
                    max_tokens=512,
                    needs_image=True,
                )
            )
        else:
            print("\n未找到图片，跳过 1024 tokens + 图片的单并发场景。")

        if self.images:
            results.append(
                self.run_scenario(
                    name="concurrent_60_stream",
                    concurrent_level=60,
                    total_requests=max(60, total_60),
                    stream=True,
                    input_tokens=1024,
                    max_tokens=512,
                    needs_image=True,
                )
            )
            results.append(
                self.run_scenario(
                    name="concurrent_60_nonstream",
                    concurrent_level=60,
                    total_requests=max(60, total_60),
                    stream=False,
                    input_tokens=1024,
                    max_tokens=512,
                    needs_image=True,
                )
            )
        else:
            print("\n未找到图片，跳过 60 并发多模态场景，避免用纯文本结果替代多模态 SLA。")

        self.print_summary(results)
        self.save_results(results)
        return results

    def save_results(self, results: Sequence[ScenarioResult]) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        summary_file = self.output_dir / f"multimodal_summary_{timestamp}.csv"
        summary_rows = [asdict(r) for r in results]
        if summary_rows:
            fieldnames = sorted(set().union(*(row.keys() for row in summary_rows)))
            with summary_file.open("w", newline="", encoding="utf-8-sig") as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(summary_rows)
            print(f"\n汇总 CSV 已保存: {summary_file}")

        detail_file = self.output_dir / f"multimodal_detail_{timestamp}.json"
        detail = {
            "summary": summary_rows,
            "samples": [asdict(s) for s in self.samples],
            "notes": {
                "non_stream_ttft": "非流式接口不暴露首 token，脚本用完整响应耗时作为 TTFT 等价指标。",
                "network_latency_adjustment": (
                    f"响应延迟和 TTFT 扣减 network_latency_ms * network_latency_factor = "
                    f"{self.network_latency_ms:.0f}ms * {self.network_latency_factor:.2f} = "
                    f"{self.network_adjustment_ms:.0f}ms。"
                ),
                "resource_metrics": "资源指标来自脚本运行机器；如需服务器资源，请在服务器上运行或接入独立监控。",
            },
        }
        with detail_file.open("w", encoding="utf-8") as file:
            json.dump(detail, file, ensure_ascii=False, indent=2)
        print(f"明细 JSON 已保存: {detail_file}")

    def print_summary(self, results: Sequence[ScenarioResult]) -> None:
        # SLA 总览：延迟类指标必须有成功样本才参与判定。
        by_name = {r.test: r for r in results}
        all_results = list(results)
        print("\n" + "=" * 88)
        print("指标达标总览")
        print("=" * 88)
        print(
            "说明: 非流式 TTFT 使用完整响应耗时；响应延迟按 "
            "--network-latency-ms * --network-latency-factor 扣减，默认不扣。"
        )

        rows = []
        max_context = max((r.max_input_tokens for r in all_results), default=0)
        rows.append(("context_le_32k", max_context, "<=", MAX_CONTEXT_TOKENS, f"{max_context} tokens"))

        c60 = [r for r in all_results if r.concurrent >= 60]
        worst_success_60 = min((r.success_rate for r in c60), default=None)
        rows.append(("concurrency_success", worst_success_60, ">=", 90, format_optional(worst_success_60, "%")))

        r = by_name.get("single_image_1024_stream")
        value = metric_if_success(r, "p99_ttft_ms")
        rows.append(("single_image_1024_stream_ttft", value, "<=", 7000, format_optional(value, "ms")))

        r = by_name.get("single_image_1024_nonstream")
        value = metric_if_success(r, "p99_ttft_ms")
        rows.append(("single_image_1024_nonstream_ttft_p99", value, "<=", 10000, format_optional(value, "ms")))

        r = by_name.get("concurrent_60_stream")
        value = metric_if_success(r, "p99_response_ms")
        rows.append(("concurrent_60_stream_p99_15s", value, "<=", 15000, format_optional(value, "ms")))
        rows.append(("concurrent_60_stream_tokps", r.output_token_throughput if r else None, ">=", 4.40, format_optional(r.output_token_throughput if r else None, "tok/s")))
        rows.append(("concurrent_60_stream_p99_7s", value, "<=", 7000, format_optional(value, "ms")))

        r = by_name.get("concurrent_60_nonstream")
        value = metric_if_success(r, "p99_response_ms")
        rows.append(("concurrent_60_nonstream_p99_15s", value, "<=", 15000, format_optional(value, "ms")))
        rows.append(("concurrent_60_nonstream_tokps", r.output_token_throughput if r else None, ">=", 4.40, format_optional(r.output_token_throughput if r else None, "tok/s")))
        rows.append(("concurrent_60_nonstream_p99_7s", value, "<=", 7000, format_optional(value, "ms")))

        single_response_max = max((r.max_response_ms for r in all_results if r.concurrent == 1 and r.success_count > 0), default=None)
        rows.append(("single_response_15s", single_response_max, "<=", 15000, format_optional(single_response_max, "ms")))

        max_cpu = max((r.max_cpu_pct for r in all_results if r.max_cpu_pct is not None), default=None)
        rows.append(("server_cpu", max_cpu, "<=", 65, format_optional(max_cpu, "%")))

        max_mem = max((r.max_memory_pct for r in all_results if r.max_memory_pct is not None), default=None)
        rows.append(("server_memory", max_mem, "<=", 70, format_optional(max_mem, "%")))

        r = by_name.get("single_text_128_stream")
        value = metric_if_success(r, "p99_ttft_ms")
        rows.append(("single_text_128_stream_ttft", value, "<=", 3000, format_optional(value, "ms")))

        definitions = {item["id"]: item for item in SLA_DEFINITIONS}
        for sla_id, actual_value, operator, target, actual_text in rows:
            definition = definitions[sla_id]
            status = pass_fail(actual_value, operator, target)
            print(f"  [{status}] {definition['name']}")
            print(f"         目标: {definition['target']} | 实际: {actual_text}")


def format_optional(value: Optional[float], unit: str) -> str:
    if value is None:
        return "N/A"
    if unit == "%":
        return f"{value:.2f}%"
    if unit == "ms":
        return f"{value:.0f}ms"
    if unit == "tok/s":
        return f"{value:.2f} tok/s"
    return f"{value:.2f}{unit}"


def metric_if_success(result: Optional[ScenarioResult], attr: str) -> Optional[float]:
    if result is None or result.success_count <= 0:
        return None
    return safe_float(getattr(result, attr))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Qwen2.5-Omni-7B 多模态模型压力测试")
    parser.add_argument("--url", default=os.getenv("AI_GATEWAY_URL", DEFAULT_URL), help="网关接口地址；也可用 AI_GATEWAY_URL")
    parser.add_argument("--app-key", default=os.getenv("AI_GATEWAY_APP_KEY", ""), help="应用 app_key；也可用 AI_GATEWAY_APP_KEY")
    parser.add_argument("--secret-key", default=os.getenv("AI_GATEWAY_SECRET_KEY", ""), help="HMAC 密钥；也可用 AI_GATEWAY_SECRET_KEY，默认不硬编码")
    parser.add_argument("--component-code", default=os.getenv("AI_GATEWAY_COMPONENT_CODE", DEFAULT_COMPONENT_CODE), help="网关 componentCode")
    parser.add_argument("--model", default=os.getenv("AI_GATEWAY_MODEL", DEFAULT_MODEL), help="模型名称")

    parser.add_argument("--mode", choices=("full", "single", "concurrent"), default="full", help="full=完整 SLA 套件；single=单场景；concurrent=指定并发场景")
    parser.add_argument("--concurrent", type=int, default=1, help="并发数，mode=concurrent 时使用")
    parser.add_argument("--total", type=int, default=60, help="请求总数；full 模式下 60 并发场景至少发 60 个请求")
    parser.add_argument("--stream", dest="stream", action="store_true", default=True, help="使用流式请求，默认开启")
    parser.add_argument("--no-stream", dest="stream", action="store_false", help="使用非流式请求")
    parser.add_argument("--input-tokens", type=int, default=128, help="single/concurrent 模式的输入 token 目标长度")
    parser.add_argument("--max-tokens", type=int, default=512, help="模型最大输出 token 数")
    parser.add_argument("--with-image", action="store_true", help="single/concurrent 模式携带图片")

    parser.add_argument("--image-dir", default=DEFAULT_IMAGE_DIR, help="测试图片目录，默认 agent/images")
    parser.add_argument("--single-image", default=None, help="只使用这一张图片，优先级高于 --image-dir")
    parser.add_argument("--output-dir", default=".", help="CSV/JSON 结果输出目录")
    parser.add_argument("--timeout", type=int, default=600, help="单请求超时秒数")
    parser.add_argument("--connect-timeout", type=float, default=10.0, help="连接建立超时秒数")
    parser.add_argument("--upload-timeout", type=float, default=60.0, help="上传/写入请求体超时秒数；requests 会把它放在 connect timeout 位置")
    parser.add_argument("--read-timeout", type=float, default=None, help="读取响应超时秒数；默认沿用 --timeout")
    parser.add_argument("--verify-ssl", action="store_true", help="启用 SSL 证书校验；内网自签证书通常不启用")
    parser.add_argument("--tokenizer-name", default=None, help="Hugging Face tokenizer 名称或本地路径；用于准确输入/输出 token 计数")
    parser.add_argument("--tiktoken-encoding", default="cl100k_base", help="无 Hugging Face tokenizer 时使用的 tiktoken 编码；设为空关闭")
    parser.add_argument("--network-latency-ms", type=float, default=0.0, help="网络耗时估计，默认 0")
    parser.add_argument("--network-latency-factor", type=float, default=0.5, help="网络耗时扣减比例，默认只扣减 50%%，取值会限制在 0 到 1")
    parser.add_argument("--max-image-bytes", type=int, default=2 * 1024 * 1024, help="图片超过该字节数时压缩；设为 0 禁用")
    parser.add_argument("--max-image-side", type=int, default=1600, help="图片压缩前的最长边限制")
    parser.add_argument("--image-quality", type=int, default=85, help="图片压缩 JPEG 质量，范围会限制在 10 到 95")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.verify_ssl:
        requests.packages.urllib3.disable_warnings()

    tester = MultiModalTester(
        url=args.url,
        # app_key=args.app_key,
        # secret_key=args.secret_key,
        app_key="1001300037",
        secret_key="360ce63f5625412ba78d0aed3458b53a",
        component_code=args.component_code,
        model=args.model,
        image_dir=args.image_dir,
        single_image=args.single_image,
        output_dir=args.output_dir,
        timeout_s=args.timeout,
        connect_timeout_s=args.connect_timeout,
        upload_timeout_s=args.upload_timeout,
        read_timeout_s=args.read_timeout,
        verify_ssl=args.verify_ssl,
        tokenizer_name=args.tokenizer_name,
        network_latency_ms=args.network_latency_ms,
        network_latency_factor=args.network_latency_factor,
        tiktoken_encoding=args.tiktoken_encoding or None,
        max_image_bytes=args.max_image_bytes,
        max_image_side=args.max_image_side,
        image_quality=args.image_quality,
    )

    if args.mode == "full":
        tester.run_full_test_suite(total_60=args.total)
        return

    result = tester.run_scenario(
        name=f"{args.mode}_{args.input_tokens}_{'image' if args.with_image else 'text'}_{'stream' if args.stream else 'nonstream'}",
        concurrent_level=args.concurrent,
        total_requests=args.total,
        stream=args.stream,
        input_tokens=args.input_tokens,
        max_tokens=args.max_tokens,
        needs_image=args.with_image,
    )
    tester.print_summary([result])
    tester.save_results([result])


if __name__ == "__main__":
    main()
