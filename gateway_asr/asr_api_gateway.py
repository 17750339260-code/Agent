#!/usr/bin/env python3
"""
通过 AI Gateway 访问 ASR 服务的测试程序
参考 gateway_api_npu.py 的认证方式，访问 ASR 接口
"""
import argparse
import requests
import urllib3

# 禁用 InsecureRequestWarning 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import os
import base64
import json
import time
from datetime import datetime, timezone
import hmac
from hashlib import sha256

# ASR Gateway 配置 ----公司环境
# ASR_BINDING = "southgrid"
# ASR_MODEL = "funasr-iic"
# ASR_BINDING_HOST = "https://192.168.0.213:18300/ai-inference-gateway/predict"
# ASR_BINDING_API_KEY = "24e74daf74124b0b96c9cb113162a976"
# ASR_CUSTCODE = "1001300033"
# ASR_COMPONENTCODE = "04101002"

# ASR Gateway 配置 --------省测做评测
ASR_BINDING = "southgrid"
ASR_MODEL = "funasr-iic"
ASR_BINDING_HOST = "https://10.134.252.232:5030/ai-gateway/predict"
ASR_BINDING_API_KEY = "b899eef382324e8d8973493fb9c35998"
ASR_CUSTCODE = "1000400672300031"
ASR_COMPONENTCODE = "04351378"

# # ASR Gateway 配置 --------网级做评测
# ASR_BINDING = "southgrid"
# ASR_MODEL = "funasr-iic"
# ASR_BINDING_HOST = "https://10.10.65.213:18300/ai-inference-gateway/predict"
# ASR_BINDING_API_KEY = "24e74daf74124b0b96c9cb113162a976"
# ASR_CUSTCODE = "1001300033"
# ASR_COMPONENTCODE = "04101002"



def getLocalAuthInfo(customerCode, secretKey):
    """生成认证信息"""
    # 使用Python内置datetime生成正确格式的GMT日期，确保英文格式
    date_value = datetime.now(timezone.utc).strftime('%a, %d %b %Y %T GMT')
    date_str = f"x-date: {date_value}"

    # 使用HMAC-SHA256生成签名
    message_bytes = date_str.encode('utf-8')
    secret_bytes = secretKey.encode('utf-8')

    h = hmac.new(secret_bytes, message_bytes, sha256)
    signature_bytes = h.digest()
    signature = base64.b64encode(signature_bytes).decode('utf-8')

    authorization = f'hmac username="{customerCode}", algorithm="hmac-sha256", headers="x-date", signature="{signature}"'
    return date_value, authorization

def asr_via_gateway(audio_file, model="funasr-iic", hotwords=None, is_return_timestamp=False):
    """
    通过 Gateway 测试 ASR 服务

    Args:
        audio_file: 音频文件路径
        model: 模型名称 (funasr-iic, funasr-nano, default)
        hotwords: 热词列表或字符串
        is_return_timestamp: 是否返回时间戳
    """
    print(f"🎵 Testing ASR via Gateway")
    print(f"   Host: {ASR_BINDING_HOST}")
    print(f"   Model: {model}")
    print(f"   Audio: {audio_file}")

    # 读取音频文件
    if not os.path.exists(audio_file):
        print(f"❌ Audio file not found: {audio_file}")
        return None

    with open(audio_file, "rb") as f:
        audio_data = f.read()

    # Base64 编码
    b64_data = base64.b64encode(audio_data).decode("utf-8")

    # 检测音频格式
    ext = os.path.splitext(audio_file)[1][1:].lower()
    if not ext:
        ext = "wav"

    # 使用 Data URI 格式
    data_uri = f"data:audio/{ext};base64,{b64_data}"

    # 获取认证信息
    x_date, authorization = getLocalAuthInfo(ASR_CUSTCODE, ASR_BINDING_API_KEY)

    # 构造请求头
    headers = {
        "x-date": x_date,
        "authorization": authorization,
        "Content-Type": "application/json",
    }

    # 处理热词
    if isinstance(hotwords, list):
        hotwords = json.dumps(hotwords, ensure_ascii=False)

    # 构造 Gateway 统一入口的请求体
    # 注意：Gateway 可能直接透传参数，不使用 asr_params 包装
    payload = {
        "componentCode": ASR_COMPONENTCODE,
        # "model": ASR_MODEL,  # 网关校验通过的模型名称
        "function": model,  # 实际的子功能通过 function 传递
        # 直接传递 ASR 参数（与直接调用 ASR 接口格式一致）
        "model": model,
        "input_type": "stream",
        "input": data_uri,
        "hotwords": hotwords,
        "is_return_timestamp": is_return_timestamp,
        "language": "zh"
    }

    print(f"\n📤 Sending request...")
    start_time = time.time()

    try:
        response = requests.post(
            ASR_BINDING_HOST,
            json=payload,
            headers=headers,
            verify=False,
            timeout=120
        )

        elapsed = time.time() - start_time

        if response.status_code == 200:
            print(f"\n✅ Success! (耗时: {elapsed:.2f}s)")
            result = response.json()

            # 打印结果
            if "text" in result:
                text = result.get("text", "")
                print(f"\n📝 识别结果: {text}")

                if is_return_timestamp and "segments" in result:
                    print(f"\n⏱️  时间戳信息:")
                    for seg in result.get("segments", []):
                        print(f"   [{seg['start']:.2f}s - {seg['end']:.2f}s] {seg['text']}")
            else:
                print(f"\n📄 响应内容:")
                print(json.dumps(result, indent=2, ensure_ascii=False))

            return result
        else:
            print(f"\n❌ Error: {response.status_code}")
            print(f"Response: {response.text}")
            return None

    except requests.exceptions.Timeout:
        print(f"\n⏱️ Request timeout after {time.time() - start_time:.2f}s")
        return None
    except Exception as e:
        print(f"\n❌ Request failed: {e}")
        return None


def run_multiple_files(audio_files, model="funasr-iic"):
    """测试多个音频文件"""
    print(f"\n{'=' * 60}")
    print(f"Testing {len(audio_files)} audio files")
    print(f"{'=' * 60}")

    results = []
    for audio_file in audio_files:
        if not os.path.exists(audio_file):
            print(f"\n⚠️  Skipping {audio_file} (not found)")
            continue

        result = asr_via_gateway(audio_file, model=model)
        results.append({
            "file": audio_file,
            "result": result
        })
        time.sleep(0.5)  # 避免请求过快

    return results


def main():
    parser = argparse.ArgumentParser(description="通过 AI Gateway 测试 ASR 服务")
    parser.add_argument("--audio", type=str, help="音频文件路径 (支持 mp3, wav, mp4 等)")
    parser.add_argument("--model", type=str, default="funasr-iic",
                        choices=["funasr-iic", "funasr-nano", "default"],
                        help="模型名称")
    parser.add_argument("--hotwords", type=str, help="热词，用逗号分隔")
    parser.add_argument("--timestamp", action="store_true", help="返回时间戳")
    parser.add_argument("--multiple", type=str, help="测试多个文件，文件路径用逗号分隔")

    args = parser.parse_args()

    # 处理热词
    hotwords = None
    if args.hotwords:
        hotwords = [hw.strip() for hw in args.hotwords.split(",")]

    # 确定音频文件
    # PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # default_audio = os.path.join(PROJECT_ROOT, "gateway_test_output.wav")
    # 原代码两行替换为：
    default_audio = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway_test_output.wav")

    if args.multiple:
        # 测试多个文件
        audio_files = [f.strip() for f in args.multiple.split(",")]
        run_multiple_files(audio_files, model=args.model)
    elif args.audio:
        # 测试单个文件
        asr_via_gateway(
            audio_file=args.audio,
            model=args.model,
            hotwords=hotwords,
            is_return_timestamp=args.timestamp
        )
    else:
        # 使用默认文件
        print(f"ℹ️  使用默认音频文件: {default_audio}")
        asr_via_gateway(
            audio_file=default_audio,
            model=args.model,
            hotwords=hotwords,
            is_return_timestamp=args.timestamp
        )


if __name__ == "__main__":
    main()
