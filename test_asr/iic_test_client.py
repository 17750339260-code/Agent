import requests
import base64
import json
import os
import numpy as np
import soundfile as sf

# Configuration
API_URL = "http://36.111.82.53:10017/v1/audio/trans"
# Get the project root directory (assuming script is in app4npu/)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO_FILE = os.path.join(PROJECT_ROOT, "test_asr", "asr_test_audio", "儿童故事 - 吉吉和磨磨.mp3")

def generate_dummy_mp3(filename):
    """
    Generate a dummy wav and convert to mp3 using ffmpeg if available.
    Otherwise just warn.
    """
    if not os.path.exists(filename):
        print(f"File not found: {filename}")
        return

def test_mp3_input():
    # generate_dummy_mp3(AUDIO_FILE)
    
    print(f"Reading {AUDIO_FILE}...")
    if not os.path.exists(AUDIO_FILE):
        print(f"❌ File not found: {AUDIO_FILE}")
        return

    with open(AUDIO_FILE, "rb") as f:
        audio_data = f.read()
    
    b64_data = base64.b64encode(audio_data).decode("utf-8")
    
    # Detect format from extension
    ext = os.path.splitext(AUDIO_FILE)[1][1:].lower() # e.g. "wav" or "mp3"
    if not ext: ext = "mp4"

    # # Case 1: Explicit audio_format
    print(f"\n--- Test Case 1: Minimal params (implicit default model='funasr-iic', input_type='stream') ---")
    payload = {
        "model": "funasr-iic", # Default
        "input_type": "stream", # Default
        "input": b64_data,
        # "is_return_timestamp": True,
        "hotwords": "测试 MP3"
    }
    send_request(payload)

    # Case 2: Data URI with mime type
    print(f"\n--- Test Case 2: Data URI (data:audio/{ext};base64,...) ---")
    data_uri = f"data:audio/{ext};base64,{b64_data}"
    payload = {
        "model": "funasr-iic",
        "input_type": "stream", 
        "input": data_uri,
        # "is_return_timestamp": True,
        "hotwords": "测试 MP3"
    }
    send_request(payload)

    # Case 3: File Path (Server-side path)
    # Ignored because server is remote
    
    # Case 4: Multipart File Upload (Real File Transfer)
    # print(f"\n--- Test Case 4: Multipart File Upload ---")
    # files = {
    #     "file": ("test1.wav", open(AUDIO_FILE, "rb"), "audio/wav")
    # }
    # data = {
    #     "model": "funasr-iic",
    #     "hotwords": "测试文件上传"
    # }
    # # Note: Using a different endpoint for file upload
    # upload_url = API_URL.replace("/trans", "/transcriptions_file") # Hacky URL switch
    
    # try:
    #     print(f"Uploading to {upload_url}...")
    #     resp = requests.post(upload_url, files=files, data=data)
    #     if resp.status_code == 200:
    #         print("✅ Success:")
    #         print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
    #     else:
    #         print(f"❌ Error {resp.status_code}:")
    #         print(resp.text)
    # except Exception as e:
    #     print(f"Upload failed: {e}")
    # finally:
    #     files["file"][1].close()

def send_request(payload):
    try:
        resp = requests.post(API_URL, json=payload)
        if resp.status_code == 200:
            print("✅ Success:")
            print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
        else:
            print(f"❌ Error {resp.status_code}:")
            print(resp.text)
    except Exception as e:
        print(f"Request failed: {e}")

if __name__ == "__main__":
    test_mp3_input()
