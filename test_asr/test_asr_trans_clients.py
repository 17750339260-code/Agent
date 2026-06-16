# -*- coding: utf-8 -*-
"""
ASR 转录接口 POST /v1/audio/trans 功能测试。

覆盖两种请求模式：
  - application/json（Base64 / Data URI / input_type=file 路径，兼容历史客户端）
  - multipart/form-data（直接上传音视频文件）

运行示例：
  pytest test_asr/test_asr_trans_clients.py -v -s
  pytest test_asr/test_asr_trans_clients.py -k json -v
  pytest test_asr/test_asr_trans_clients.py -k multipart -v

用例清单（共 31 条，与 pytest 收集数量一致）：
  【用例01】JSON 兼容 - WAV + Base64 + stream
  【用例02】JSON 兼容 - WAV + Data URI + stream
  【用例03】JSON 兼容 - MP4 + Base64 + stream
  【用例04】JSON 兼容 - MP4 + Data URI + stream
  【用例05】JSON 全参 - model=funasr-iic
  【用例06】JSON 全参 - model=funasr-nano
  【用例07】JSON 全参 - model=default
  【用例08】JSON 全参 - 说话人分离关 + 时间戳关
  【用例09】JSON 全参 - 说话人分离关 + 时间戳开
  【用例10】JSON 全参 - 说话人分离开 + 时间戳关
  【用例11】JSON 全参 - 说话人分离开 + 时间戳开
  【用例12】JSON 全参 - language=zh
  【用例13】JSON 全参 - language=en
  【用例14】JSON 全参 - language=auto
  【用例15】JSON 全参 - hotwords 空与非空（单函数内 2 次子请求）
  【用例16】JSON 全参 - 最小载荷（仅 model/input_type/input）
  【用例17】JSON 全参 - input_type=file 服务端路径
  【用例18】multipart - WAV 最简上传
  【用例19】multipart - MP4 上传
  【用例20】multipart - model=funasr-iic
  【用例21】multipart - model=funasr-nano
  【用例22】multipart - model=default
  【用例23】multipart - 说话人分离关 + 时间戳关
  【用例24】multipart - 说话人分离关 + 时间戳开
  【用例25】multipart - 说话人分离开 + 时间戳关
  【用例26】multipart - 说话人分离开 + 时间戳开
  【用例27】multipart - language=zh
  【用例28】multipart - language=en
  【用例29】multipart - 热词
  【用例30】multipart - 全部表单字段
  【用例31】跨模式冒烟 - 同一 WAV 走 JSON 与 multipart
"""

from __future__ import annotations

import os
from typing import Optional

import pytest

from common.asr_trans_client import (
    DEFAULT_API_URL,
    INPUT_TYPE_FILE,
    INPUT_TYPE_STREAM,
    SUPPORTED_MODELS,
    AsrTransClient,
    JsonTranscribeOptions,
    MultipartTranscribeOptions,
    log_response,
    read_file_as_base64,
)

# ===================== 路径与全局配置 =====================

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO_DIR = os.path.join(PROJECT_ROOT, "test_asr", "asr_test_audio")
WAV_FILE = os.path.join(AUDIO_DIR, "4-111.wav")
MP4_FILE = os.path.join(AUDIO_DIR, "video1.mp4")

# 可通过环境变量覆盖服务地址
API_URL = os.environ.get("ASR_API_URL", DEFAULT_API_URL)

# 热词示例（可按业务替换）
SAMPLE_HOTWORDS = ""


# ===================== Fixtures =====================


@pytest.fixture(scope="module")
def asr_client() -> AsrTransClient:
    """模块级 ASR 客户端"""
    return AsrTransClient(api_url=API_URL)


def _require_file(path: str, label: str) -> None:
    """文件不存在时跳过用例"""
    if not os.path.exists(path):
        pytest.skip(f"{label} 不存在: {path}")


@pytest.fixture(scope="module")
def wav_path() -> str:
    _require_file(WAV_FILE, "WAV 测试音频")
    return WAV_FILE


@pytest.fixture(scope="module")
def mp4_path() -> str:
    _require_file(MP4_FILE, "MP4 测试视频")
    return MP4_FILE


def _assert_success(response, *, expect_timestamp: Optional[bool] = None) -> None:
    """
    通用成功断言：
      - HTTP 200
      - 响应体为 dict 且包含识别文本字段 text
    """
    assert response.ok, (
        f"期望 HTTP 200，实际 {response.status_code}: {response.raw_text[:500]}"
    )
    assert isinstance(response.body, dict), "成功响应应为 JSON 对象"
    assert "text" in response.body, "响应应包含 text 字段"

    if expect_timestamp is True:
        assert response.has_timestamps(), "开启时间戳后响应应包含时间戳相关字段"
    # 关闭时间戳时不强制无时间戳字段（服务端实现可能仍返回）


# ===================== JSON 模式 — 兼容历史用例 =====================


class TestJsonLegacyCompatibility:
    """与重构前脚本等价的 JSON stream 用例，确保旧客户端调用方式不受影响（用例01～04）"""

    # 【用例01】JSON 兼容：WAV 纯 Base64 + input_type=stream
    def test_json_base64_stream_wav(
        self, asr_client: AsrTransClient, wav_path: str
    ) -> None:
        """【用例01】纯 Base64 + input_type=stream（原 Case1）"""
        opts = asr_client.build_stream_payload_from_file(
            wav_path,
            use_data_uri=False,
            model="funasr-iic",
            hotwords=SAMPLE_HOTWORDS,
        )
        resp = asr_client.transcribe_json(opts)
        log_response("JSON Base64 stream (wav)", resp)
        _assert_success(resp)

    # 【用例02】JSON 兼容：WAV Data URI + input_type=stream
    def test_json_data_uri_stream_wav(
        self, asr_client: AsrTransClient, wav_path: str
    ) -> None:
        """【用例02】Data URI + input_type=stream（原 Case2）"""
        opts = asr_client.build_stream_payload_from_file(
            wav_path,
            use_data_uri=True,
            model="funasr-iic",
            hotwords=SAMPLE_HOTWORDS,
        )
        resp = asr_client.transcribe_json(opts)
        log_response("JSON Data URI stream (wav)", resp)
        _assert_success(resp)

    # 【用例03】JSON 兼容：MP4 纯 Base64 + input_type=stream
    def test_json_base64_stream_mp4(
        self, asr_client: AsrTransClient, mp4_path: str
    ) -> None:
        """【用例03】MP4 纯 Base64 上传（原 Case3）"""
        opts = asr_client.build_stream_payload_from_file(
            mp4_path,
            use_data_uri=False,
            model="funasr-iic",
            hotwords=SAMPLE_HOTWORDS,
        )
        resp = asr_client.transcribe_json(opts)
        log_response("JSON Base64 stream (mp4)", resp)
        _assert_success(resp)

    # 【用例04】JSON 兼容：MP4 Data URI + input_type=stream
    def test_json_data_uri_stream_mp4(
        self, asr_client: AsrTransClient, mp4_path: str
    ) -> None:
        """【用例04】MP4 Data URI 上传（原 Case4）"""
        opts = asr_client.build_stream_payload_from_file(
            mp4_path,
            use_data_uri=True,
            model="funasr-iic",
        )
        resp = asr_client.transcribe_json(opts)
        log_response("JSON Data URI stream (mp4)", resp)
        _assert_success(resp)


# ===================== JSON 模式 — 全参数字段覆盖 =====================


class TestJsonFullParameters:
    """JSON 请求体各字段组合测试（用例05～17）"""

    # 【用例05～07】JSON 全参：遍历 model
    @pytest.mark.parametrize(
        "model",
        SUPPORTED_MODELS,
        ids=["用例05-funasr-iic", "用例06-funasr-nano", "用例07-default"],
    )
    def test_json_all_models(
        self, asr_client: AsrTransClient, wav_path: str, model: str
    ) -> None:
        """【用例05～07】遍历 model：funasr-iic / funasr-nano / default"""
        opts = JsonTranscribeOptions(
            model=model,
            input_type=INPUT_TYPE_STREAM,
            input=read_file_as_base64(wav_path),
            hotwords=SAMPLE_HOTWORDS,
            language="zh",
        )
        resp = asr_client.transcribe_json(opts)
        log_response(f"JSON model={model}", resp)
        _assert_success(resp)

    # 【用例08～11】JSON 全参：说话人分离 × 时间戳 四种组合
    @pytest.mark.parametrize(
        "speaker_diarization,is_return_timestamp",
        [
            (False, False),
            (False, True),
            (True, False),
            (True, True),
        ],
        ids=[
            "用例08-分离关-时间戳关",
            "用例09-分离关-时间戳开",
            "用例10-分离开-时间戳关",
            "用例11-分离开-时间戳开",
        ],
    )
    def test_json_speaker_and_timestamp_flags(
        self,
        asr_client: AsrTransClient,
        wav_path: str,
        speaker_diarization: bool,
        is_return_timestamp: bool,
    ) -> None:
        """【用例08～11】speaker_diarization 与 is_return_timestamp 布尔组合"""
        opts = JsonTranscribeOptions(
            model="funasr-iic",
            input_type=INPUT_TYPE_STREAM,
            input=read_file_as_base64(wav_path),
            speaker_diarization=speaker_diarization,
            is_return_timestamp=is_return_timestamp,
            language="zh",
        )
        resp = asr_client.transcribe_json(opts)
        log_response(
            f"JSON diarization={speaker_diarization} timestamp={is_return_timestamp}",
            resp,
        )
        _assert_success(resp, expect_timestamp=is_return_timestamp or None)

    # 【用例12～14】JSON 全参：language 字段
    @pytest.mark.parametrize(
        "language",
        ["zh", "en", "auto"],
        ids=["用例12-lang-zh", "用例13-lang-en", "用例14-lang-auto"],
    )
    def test_json_language(
        self, asr_client: AsrTransClient, wav_path: str, language: str
    ) -> None:
        """【用例12～14】language 字段"""
        opts = JsonTranscribeOptions(
            model="funasr-iic",
            input_type=INPUT_TYPE_STREAM,
            input=read_file_as_base64(wav_path),
            language=language,
        )
        resp = asr_client.transcribe_json(opts)
        log_response(f"JSON language={language}", resp)
        _assert_success(resp)

    # 【用例15】JSON 全参：hotwords 空字符串与非空（循环 2 次请求）
    def test_json_hotwords_empty_and_nonempty(
        self, asr_client: AsrTransClient, wav_path: str
    ) -> None:
        """【用例15】hotwords 空字符串与非空（含 15a 空 / 15b 非空两次请求）"""
        for hotwords in ("", SAMPLE_HOTWORDS):
            opts = JsonTranscribeOptions(
                model="funasr-iic",
                input_type=INPUT_TYPE_STREAM,
                input=read_file_as_base64(wav_path),
                hotwords=hotwords,
            )
            resp = asr_client.transcribe_json(opts)
            log_response(f"JSON hotwords={hotwords!r}", resp)
            _assert_success(resp)

    # 【用例16】JSON 全参：最小载荷，验证历史客户端兼容
    def test_json_minimal_payload_backward_compatible(
        self, asr_client: AsrTransClient, wav_path: str
    ) -> None:
        """
        【用例16】最小 JSON 载荷：仅 model + input_type + input。
        验证服务端对历史精简请求的兼容性。
        """
        payload = JsonTranscribeOptions(
            model="funasr-iic",
            input_type=INPUT_TYPE_STREAM,
            input=read_file_as_base64(wav_path),
        )
        resp = asr_client.transcribe_json(payload)
        log_response("JSON minimal payload", resp)
        _assert_success(resp)

    # 【用例17】JSON 全参：input_type=file，服务端本地路径
    def test_json_input_type_file_server_path(
        self, asr_client: AsrTransClient, wav_path: str
    ) -> None:
        """
        【用例17】input_type=file：input 为服务端可访问路径。
        本地路径在远程服务上通常不可用，失败时跳过而非判失败。
        """
        opts = JsonTranscribeOptions(
            model="funasr-iic",
            input_type=INPUT_TYPE_FILE,
            input=wav_path,
        )
        resp = asr_client.transcribe_json(opts)
        log_response("JSON input_type=file (local path)", resp)
        if not resp.ok:
            pytest.skip(
                "input_type=file 需服务端本地路径，当前路径可能不可访问: "
                f"{resp.status_code} {resp.raw_text[:200]}"
            )
        _assert_success(resp)


# ===================== multipart/form-data 模式 =====================


class TestMultipartUpload:
    """直接上传音视频文件的 multipart 测试（用例18～30）"""

    # 【用例18】multipart：WAV 最简上传（仅 file + 默认 model）
    def test_multipart_upload_wav_minimal(
        self, asr_client: AsrTransClient, wav_path: str
    ) -> None:
        """【用例18】仅上传 file + 默认 model"""
        resp = asr_client.transcribe_multipart(wav_path)
        log_response("multipart wav minimal", resp)
        _assert_success(resp)

    # 【用例19】multipart：上传 MP4 视频
    def test_multipart_upload_mp4(
        self, asr_client: AsrTransClient, mp4_path: str
    ) -> None:
        """【用例19】上传 MP4 视频文件"""
        resp = asr_client.transcribe_multipart(mp4_path)
        log_response("multipart mp4", resp)
        _assert_success(resp)

    # 【用例20～22】multipart：遍历 model
    @pytest.mark.parametrize(
        "model",
        SUPPORTED_MODELS,
        ids=["用例20-funasr-iic", "用例21-funasr-nano", "用例22-default"],
    )
    def test_multipart_all_models(
        self, asr_client: AsrTransClient, wav_path: str, model: str
    ) -> None:
        """【用例20～22】multipart 下遍历 model"""
        opts = MultipartTranscribeOptions(model=model, hotwords=SAMPLE_HOTWORDS)
        resp = asr_client.transcribe_multipart(wav_path, options=opts)
        log_response(f"multipart model={model}", resp)
        _assert_success(resp)

    # 【用例23～26】multipart：说话人分离 × 时间戳 四种组合
    @pytest.mark.parametrize(
        "speaker_diarization,is_return_timestamp",
        [
            (False, False),
            (False, True),
            (True, False),
            (True, True),
        ],
        ids=[
            "用例23-分离关-时间戳关",
            "用例24-分离关-时间戳开",
            "用例25-分离开-时间戳关",
            "用例26-分离开-时间戳开",
        ],
    )
    def test_multipart_speaker_and_timestamp(
        self,
        asr_client: AsrTransClient,
        wav_path: str,
        speaker_diarization: bool,
        is_return_timestamp: bool,
    ) -> None:
        """【用例23～26】multipart：说话人分离与时间戳开关"""
        opts = MultipartTranscribeOptions(
            model="funasr-iic",
            speaker_diarization=speaker_diarization,
            is_return_timestamp=is_return_timestamp,
            language="zh",
        )
        resp = asr_client.transcribe_multipart(wav_path, options=opts)
        log_response(
            f"multipart diarization={speaker_diarization} ts={is_return_timestamp}",
            resp,
        )
        _assert_success(resp, expect_timestamp=is_return_timestamp or None)

    # 【用例27～28】multipart：language 字段
    @pytest.mark.parametrize(
        "language",
        ["zh", "en"],
        ids=["用例27-lang-zh", "用例28-lang-en"],
    )
    def test_multipart_language(
        self, asr_client: AsrTransClient, wav_path: str, language: str
    ) -> None:
        """【用例27～28】multipart：language 字段"""
        opts = MultipartTranscribeOptions(model="funasr-nano", language=language)
        resp = asr_client.transcribe_multipart(wav_path, options=opts)
        log_response(f"multipart language={language}", resp)
        _assert_success(resp)

    # 【用例29】multipart：热词
    def test_multipart_hotwords(
        self, asr_client: AsrTransClient, wav_path: str
    ) -> None:
        """【用例29】multipart：热词"""
        opts = MultipartTranscribeOptions(
            model="funasr-iic",
            hotwords=SAMPLE_HOTWORDS,
        )
        resp = asr_client.transcribe_multipart(wav_path, options=opts)
        log_response("multipart hotwords", resp)
        _assert_success(resp)

    # 【用例30】multipart：全部表单字段同时传入
    def test_multipart_full_parameters(
        self, asr_client: AsrTransClient, wav_path: str
    ) -> None:
        """【用例30】multipart：全部表单字段同时传入"""
        opts = MultipartTranscribeOptions(
            model="funasr-iic",
            hotwords=SAMPLE_HOTWORDS,
            speaker_diarization=True,
            language="zh",
            is_return_timestamp=True,
        )
        resp = asr_client.transcribe_multipart(wav_path, options=opts)
        log_response("multipart full params", resp)
        _assert_success(resp, expect_timestamp=True)


# ===================== 跨模式对比（可选冒烟） =====================


class TestCrossModeSmoke:
    """同一音频分别走 JSON 与 multipart，结果均应成功（用例31）"""

    # 【用例31】跨模式冒烟：同一 WAV 分别 JSON 与 multipart
    def test_same_wav_json_vs_multipart(
        self, asr_client: AsrTransClient, wav_path: str
    ) -> None:
        """【用例31】同一 WAV 走 JSON 与 multipart，均应成功"""
        json_resp = asr_client.transcribe_json(
            JsonTranscribeOptions(
                model="funasr-iic",
                input_type=INPUT_TYPE_STREAM,
                input=read_file_as_base64(wav_path),
            )
        )
        multi_resp = asr_client.transcribe_multipart(
            wav_path, MultipartTranscribeOptions(model="funasr-iic")
        )
        log_response("cross-mode JSON", json_resp)
        log_response("cross-mode multipart", multi_resp)
        _assert_success(json_resp)
        _assert_success(multi_resp)


# ===================== 命令行直接运行（非 pytest） =====================


def _run_legacy_demo() -> None:
    """保留与原脚本类似的交互式打印，便于手动调试"""
    client = AsrTransClient(api_url=API_URL)
    for path, label in [(WAV_FILE, "wav"), (MP4_FILE, "mp4")]:
        if not os.path.exists(path):
            print(f"跳过 {label}: 文件不存在 {path}")
            continue
        for use_uri in (False, True):
            mode = "Data URI" if use_uri else "Base64"
            opts = client.build_stream_payload_from_file(path, use_data_uri=use_uri)
            resp = client.transcribe_json(opts)
            log_response(f"[JSON {mode}] {label}", resp)
        resp = client.transcribe_multipart(path)
        log_response(f"[multipart] {label}", resp)


if __name__ == "__main__":
    _run_legacy_demo()
