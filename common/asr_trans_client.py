# -*- coding: utf-8 -*-
"""
ASR 音频转录接口客户端封装。

接口：POST /v1/audio/trans
支持两种请求方式（向后兼容 JSON，新增 multipart 直传文件）：
  1. application/json  — FunASRRequestJSON（Base64 / Data URI / 服务端路径）
  2. multipart/form-data — 直接上传音视频文件
"""

from __future__ import annotations
import base64
import json
import mimetypes
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, BinaryIO, Dict, Optional
import requests

# 默认服务地址（可通过环境变量 ASR_API_URL 覆盖）
DEFAULT_API_URL = os.environ.get(
    "ASR_API_URL", "http://36.111.82.53:10017/v1/audio/trans"
)

# 支持的模型列表（接口文档约定）
SUPPORTED_MODELS = ("funasr-iic", "funasr-nano", "default")

# input_type 枚举值
INPUT_TYPE_STREAM = "stream"  # input 为 Base64 或 Data URI
INPUT_TYPE_FILE = "file"      # input 为服务端可访问的文件路径


class RequestMode(str, Enum):
    """请求体格式"""
    JSON = "json"
    MULTIPART = "multipart"


@dataclass
class AsrResponse:
    """统一封装 HTTP 响应，便于测试断言"""
    status_code: int
    body: Any
    raw_text: str
    elapsed_sec: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status_code == 200

    @property
    def text(self) -> str:
        if isinstance(self.body, dict):
            return self.body.get("text", "") or ""
        return ""

    def has_timestamps(self) -> bool:
        """响应中是否包含时间戳相关字段（字段名因实现而异，做宽松判断）"""
        if not isinstance(self.body, dict):
            return False
        keys = ("segments", "timestamp", "timestamps", "words", "sentence_info")
        return any(k in self.body for k in keys)


@dataclass
class JsonTranscribeOptions:
    """JSON 模式可选参数（与历史客户端字段保持一致，并覆盖文档全部字段）"""
    model: str = "funasr-iic"
    input_type: str = INPUT_TYPE_STREAM
    input: str = ""
    hotwords: str = ""
    speaker_diarization: bool = False
    language: str = "zh"
    is_return_timestamp: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        payload = {
            "model": self.model,
            "input_type": self.input_type,
            "input": self.input,
            "hotwords": self.hotwords,
            "speaker_diarization": self.speaker_diarization,
            "language": self.language,
            "is_return_timestamp": self.is_return_timestamp,
        }
        payload.update(self.extra)
        return payload


@dataclass
class MultipartTranscribeOptions:
    """multipart/form-data 模式可选参数"""
    model: str = "funasr-iic"
    hotwords: str = ""
    speaker_diarization: bool = False
    language: str = "zh"
    is_return_timestamp: bool = False
    extra: Dict[str, str] = field(default_factory=dict)

    def to_form_data(self) -> Dict[str, str]:
        data = {
            "model": self.model,
            "hotwords": self.hotwords,
            "speaker_diarization": _bool_to_form(self.speaker_diarization),
            "language": self.language,
            "is_return_timestamp": _bool_to_form(self.is_return_timestamp),
        }
        data.update(self.extra)
        return data


def _bool_to_form(value: bool) -> str:
    """multipart 表单中布尔值通常以字符串传递"""
    return "true" if value else "false"


def read_file_as_base64(file_path: str) -> str:
    """读取本地文件并转为 Base64 字符串"""
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_data_uri(file_path: str, b64_data: Optional[str] = None) -> str:
    """
    构造 Data URI：data:audio/{ext};base64,...
    用于 JSON 模式下 input_type=stream 的另一种 input 格式
    """
    ext = os.path.splitext(file_path)[1].lstrip(".").lower() or "wav"
    if b64_data is None:
        b64_data = read_file_as_base64(file_path)
    # 视频扩展名在 Data URI 中仍常用 audio/* 前缀（与历史脚本一致）
    return f"data:audio/{ext};base64,{b64_data}"


def guess_mime_type(file_path: str) -> str:
    """根据扩展名推断 MIME，供 multipart 上传使用"""
    mime, _ = mimetypes.guess_type(file_path)
    if mime:
        return mime
    ext = os.path.splitext(file_path)[1].lower()
    fallback = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".mp4": "video/mp4",
        ".m4a": "audio/mp4",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
    }
    return fallback.get(ext, "application/octet-stream")


class AsrTransClient:
    """POST /v1/audio/trans 客户端"""

    def __init__(self, api_url: str = DEFAULT_API_URL, timeout: int = 120):
        self.api_url = api_url
        self.timeout = timeout

    def transcribe_json(
        self,
        options: JsonTranscribeOptions,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> AsrResponse:
        """
        JSON 模式转录。
        Content-Type: application/json
        """
        req_headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
        }
        if headers:
            req_headers.update(headers)

        import time
        start = time.time()
        resp = requests.post(
            self.api_url,
            json=options.to_payload(),
            headers=req_headers,
            timeout=self.timeout,
        )
        return _build_response(resp, time.time() - start)

    def transcribe_multipart(
        self,
        file_path: str,
        options: Optional[MultipartTranscribeOptions] = None,
        *,
        file_obj: Optional[BinaryIO] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> AsrResponse:
        """
        multipart/form-data 模式：直接上传音视频文件。
        注意：不要手动设置 Content-Type，由 requests 自动附带 boundary。
        """
        options = options or MultipartTranscribeOptions()
        req_headers = {"accept": "application/json"}
        if headers:
            req_headers.update(headers)

        filename = os.path.basename(file_path)
        mime = guess_mime_type(file_path)

        import time
        start = time.time()

        if file_obj is not None:
            files = {"file": (filename, file_obj, mime)}
            resp = requests.post(
                self.api_url,
                headers=req_headers,
                files=files,
                data=options.to_form_data(),
                timeout=self.timeout,
            )
        else:
            with open(file_path, "rb") as f:
                files = {"file": (filename, f, mime)}
                resp = requests.post(
                    self.api_url,
                    headers=req_headers,
                    files=files,
                    data=options.to_form_data(),
                    timeout=self.timeout,
                )

        return _build_response(resp, time.time() - start)

    @staticmethod
    def build_stream_payload_from_file(
        file_path: str,
        *,
        use_data_uri: bool = False,
        model: str = "funasr-iic",
        hotwords: str = "",
        speaker_diarization: bool = False,
        language: str = "zh",
        is_return_timestamp: bool = False,
    ) -> JsonTranscribeOptions:
        """从本地文件构造 JSON stream 模式请求参数（兼容旧客户端写法）"""
        b64 = read_file_as_base64(file_path)
        input_value = build_data_uri(file_path, b64) if use_data_uri else b64
        return JsonTranscribeOptions(
            model=model,
            input_type=INPUT_TYPE_STREAM,
            input=input_value,
            hotwords=hotwords,
            speaker_diarization=speaker_diarization,
            language=language,
            is_return_timestamp=is_return_timestamp,
        )


def _build_response(resp: requests.Response, elapsed: float) -> AsrResponse:
    raw = resp.text
    try:
        body = resp.json()
    except json.JSONDecodeError:
        body = raw
    return AsrResponse(
        status_code=resp.status_code,
        body=body,
        raw_text=raw,
        elapsed_sec=elapsed,
    )


def log_response(label: str, response: AsrResponse) -> None:
    """打印响应（便于本地调试，pytest -s 时可见）"""
    print(f"\n--- {label} ---")
    if response.ok:
        print("✅ Success:")
        if isinstance(response.body, dict):
            print(json.dumps(response.body, indent=2, ensure_ascii=False))
        else:
            print(response.body)
    else:
        print(f"❌ Error {response.status_code}:")
        print(response.raw_text)
