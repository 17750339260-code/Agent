import requests
import base64
import json
import os

# Configuration
API_URL = "http://36.111.82.53:10017/v1/audio/trans"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 测试音频文件（不同格式）
TEST_FILES = [
    os.path.join(PROJECT_ROOT, "test_asr", "asr_test_audio", "儿童故事 - 吉吉和磨磨.mp3"),
    os.path.join(PROJECT_ROOT, "test_asr", "asr_test_audio", "天空之城 - 老虎拔牙.mp3")
]


def send_request(payload):
    try:
        resp = requests.post(API_URL, json=payload, timeout=300)
        if resp.status_code == 200:
            print("✅ Success:")
            print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
        else:
            print(f"❌ Error {resp.status_code}:")
            print(resp.text[:200])
    except Exception as e:
        print(f"❌ Request failed: {e}")


def files(audio_path: str):
    if not os.path.exists(audio_path):
        print(f"❌ 文件不存在: {audio_path}")
        return

    ext = os.path.splitext(audio_path)[1][1:].lower()
    mime_map = {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "mp4": "audio/mp4",
        "ogg": "audio/ogg",
        "flac": "audio/flac",
        "m4a": "audio/mp4",
    }
    mime = mime_map.get(ext, f"audio/{ext}")

    with open(audio_path, "rb") as f:
        audio_data = f.read()
    b64_data = base64.b64encode(audio_data).decode("utf-8")
    size_kb = len(audio_data) // 1024

    print(f"\n{'='*70}")
    print(f"文件: {os.path.basename(audio_path)} ({ext}, {size_kb}KB)")

    # Case 1: 纯 base64（服务端通过 magic bytes 自动检测格式）
    print(f"\n--- Case 1: 纯 base64（自动检测格式） ---")
    payload = {
        "model": "funasr-nano",
        "input_type": "stream",
        "input": b64_data,
        "hotwords": "测试",
    }
    send_request(payload)

    # Case 2: Data URI（显式指定 MIME 类型）
    print(f"\n--- Case 2: Data URI (data:{mime};base64,...) ---")
    data_uri = f"data:{mime};base64,{b64_data}"
    payload = {
        "model": "funasr-nano",
        "input_type": "stream",
        "input": data_uri,
        "hotwords": "测试",
    }
    send_request(payload)


def main():
    print(f"API: {API_URL}")
    print(f"模型: funasr-nano")
    for audio_path in TEST_FILES:
        files(audio_path)


if __name__ == "__main__":
    main()
