import requests
import base64
import json
import os
import pytest

# ===================== 配置（完全不变）=====================
API_URL = "http://36.111.82.53:10017/v1/audio/trans"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO_FILE = os.path.join(PROJECT_ROOT, "test_asr", "asr_test_audio", "61.wav")
MP4_FILE = os.path.join(PROJECT_ROOT, "test_asr", "asr_test_audio", "")
MP3_FILE = os.path.join(PROJECT_ROOT, "test_asr", "asr_test_audio", "6.mp3")

def send_request(payload):
    try:
        resp = requests.post(API_URL, json=payload)
        if resp.status_code == 200:
            print("✅ Success:")
            result = resp.json()
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return result
        else:
            print(f"❌ Error {resp.status_code}:")
            print(resp.text)
            return None
    except Exception as e:
        print(f"Request failed: {e}")
        return None


# ===================== pytest 测试用例（仅格式转换）=====================
def test_case1_base64_upload():
    """WAV音频文件纯Base64编码上传测试"""
    print(f"Reading {AUDIO_FILE}...")
    if not os.path.exists(AUDIO_FILE):
        print(f"❌ File not found: {AUDIO_FILE}")
        pytest.skip("音频文件不存在，跳过测试")

    with open(AUDIO_FILE, "rb") as f:
        audio_data = f.read()

    b64_data = base64.b64encode(audio_data).decode("utf-8")
    ext = os.path.splitext(AUDIO_FILE)[1][1:].lower()
    if not ext:
        ext = "wav"

    print(f"\n--- Test Case 1: Minimal params (implicit default model='funasr-iic', input_type='stream') ---")
    payload = {
        "model": "funasr-iic",
        "input_type": "stream",
        "input": b64_data,
        "hotwords": ""
    }
    result = send_request(payload)
    assert result is not None, "请求失败，未获得有效响应"


def test_case2_data_uri_upload():
    """WAV音频文件以 Data URI 格式上传测试"""
    print(f"Reading {AUDIO_FILE}...")
    if not os.path.exists(AUDIO_FILE):
        print(f"❌ File not found: {AUDIO_FILE}")
        pytest.skip("音频文件不存在，跳过测试")

    with open(AUDIO_FILE, "rb") as f:
        audio_data = f.read()

    b64_data = base64.b64encode(audio_data).decode("utf-8")
    ext = os.path.splitext(AUDIO_FILE)[1][1:].lower()
    if not ext:
        ext = "wav"

    print(f"\n--- Test Case 2: Data URI (data:audio/{ext};base64,...) ---")
    data_uri = f"data:audio/{ext};base64,{b64_data}"
    payload = {
        "model": "funasr-iic",
        "input_type": "stream",
        "input": data_uri,
        "hotwords": ""
    }
    result = send_request(payload)
    assert result is not None, "请求失败，未获得有效响应"


# ===================== 【新增】MP4 视频测试用例（追加在最后）=====================
def test_case3_mp4_base64_upload():
    """MP4视频文件纯Base64编码上传"""
    print(f"Reading {MP4_FILE}...")
    if not os.path.exists(MP4_FILE):
        print(f"❌ File not found: {MP4_FILE}")
        pytest.skip("视频文件不存在，跳过测试")

    with open(MP4_FILE, "rb") as f:
        file_data = f.read()

    b64_data = base64.b64encode(file_data).decode("utf-8")

    print(f"\n--- Test Case 3: MP4视频 纯Base64上传 ---")
    payload = {
        "model": "funasr-iic",
        "input_type": "stream",
        "input": b64_data,
        "hotwords": ""
    }
    result = send_request(payload)
    assert result is not None, "请求失败，未获得有效响应"


def test_case4_mp4_data_uri_upload():
    """MP4视频文件以 Data URI 格式上传测试"""
    print(f"Reading {MP4_FILE}...")
    if not os.path.exists(MP4_FILE):
        print(f"❌ File not found: {MP4_FILE}")
        pytest.skip("视频文件不存在，跳过测试")

    with open(MP4_FILE, "rb") as f:
        file_data = f.read()

    b64_data = base64.b64encode(file_data).decode("utf-8")
    ext = os.path.splitext(MP4_FILE)[1][1:].lower()
    if not ext:
        ext = "mp4"

    print(f"\n--- Test Case 4: MP4视频 Data URI上传 ---")
    data_uri = f"data:video/{ext};base64,{b64_data}"
    payload = {
        "model": "funasr-iic",
        "input_type": "stream",
        "input": data_uri,
    }
    result = send_request(payload)
    assert result is not None, "请求失败，未获得有效响应"
# ===================== 【新增】MP4 视频测试用例（追加在最后）=====================

def test_case5_mp3_base64_upload():
    """MP3视频文件纯Base64编码上传"""
    print(f"Reading {MP3_FILE}...")
    if not os.path.exists(MP3_FILE):
        print(f"❌ File not found: {MP3_FILE}")
        pytest.skip("视频文件不存在，跳过测试")

    with open(MP3_FILE, "rb") as f:
        file_data = f.read()

    b64_data = base64.b64encode(file_data).decode("utf-8")

    print(f"\n--- Test Case 5: MP3视频 纯Base64上传 ---")
    payload = {
        "model": "funasr-iic",
        "input_type": "stream",
        "input": b64_data,
        "hotwords": ""
    }
    result = send_request(payload)
    assert result is not None, "请求失败，未获得有效响应"


def test_case6_mp3_data_uri_upload():
    """MP4视频文件以 Data URI 格式上传测试"""
    print(f"Reading {MP3_FILE}...")
    if not os.path.exists(MP3_FILE):
        print(f"❌ File not found: {MP3_FILE}")
        pytest.skip("视频文件不存在，跳过测试")

    with open(MP3_FILE, "rb") as f:
        file_data = f.read()

    b64_data = base64.b64encode(file_data).decode("utf-8")
    ext = os.path.splitext(MP3_FILE)[1][1:].lower()
    if not ext:
        ext = "mp3"

    print(f"\n--- Test Case 6: MP3视频 Data URI上传 ---")
    data_uri = f"data:video/{ext};base64,{b64_data}"
    payload = {
        "model": "funasr-iic",
        "input_type": "stream",
        "input": data_uri,
    }
    result = send_request(payload)
    assert result is not None, "请求失败，未获得有效响应"
