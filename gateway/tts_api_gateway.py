import argparse
import requests
import urllib3

# 禁用 InsecureRequestWarning 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import os

try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    pyaudio = None
    PYAUDIO_AVAILABLE = False
import struct
from datetime import datetime, timezone
import hmac
from hashlib import sha256
from http.client import IncompleteRead
from urllib3.exceptions import ProtocolError
import base64
import time

TTS_BINDING = "southgrid"
TTS_MODEL = "tts-v1"
TTS_BINDING_HOST = "http://192.168.0.213:18300/ai-inference-gateway/predict"
TTS_BINDING_API_KEY = "24e74daf74124b0b96c9cb113162a976"
TTS_CUSTCODE = "1001300033"
TTS_COMPONENTCODE = "04100945"


##################本地模拟南网接口网关认证 bign###########################
def getLocalAuthInfo(customerCode, secretKey):
    """生成认证信息"""
    # 使用Python内置datetime生成正确格式的GMT日期，确保英文格式
    # 使用 timezone-aware 对象替代废弃的 datetime.utcnow()
    date_value = datetime.now(timezone.utc).strftime('%a, %d %b %Y %T GMT')
    date_str = f"x-date: {date_value}"

    # 使用Python内置hmac库直接生成签名，避免shell命令执行问题
    message_bytes = date_str.encode('utf-8')
    secret_bytes = secretKey.encode('utf-8')

    # 创建HMAC对象并计算签名
    h = hmac.new(secret_bytes, message_bytes, sha256)
    signature_bytes = h.digest()

    # 将签名转换为base64编码
    signature = base64.b64encode(signature_bytes).decode('utf-8')

    # 构建正确格式的authorization头，确保格式完全符合要求
    curl_authorization = f'hmac username="{customerCode}", algorithm="hmac-sha256", headers="x-date", signature="{signature}"'
    return date_value, curl_authorization


##################本地模拟南网接口网关认证 end############################

def fix_wav_header(filepath):
    """修复WAV文件头中的数据大小字段，使其与实际文件大小一致"""
    try:
        file_size = os.path.getsize(filepath)
        if file_size < 44:
            return
        with open(filepath, 'r+b') as f:
            # RIFF chunk size = file_size - 8
            f.seek(4)
            f.write(struct.pack('<I', file_size - 8))
            # data chunk size = file_size - 44
            f.seek(40)
            f.write(struct.pack('<I', file_size - 44))
        print(f"WAV header fixed: data size = {file_size - 44} bytes")
    except Exception as e:
        print(f"Warning: failed to fix WAV header: {e}")


def main():
    parser = argparse.ArgumentParser(description="Test AI Gateway /predict endpoint for TTS with streaming playback")
    parser.add_argument("--text", type=str,
                        default='1. 觉醒：走出单向度的隧道 我们曾是那只在大地褶皱里跋涉的小虫，在\u201c山脚视角\u201d的深井里，把勤奋磨损成一种生存的惯性。盯着那张被前人画满红线的\u201c呆逼小地图\u201d，在显性的路径里，为了一寸得失而屏息。那是认知的窄框架\u2014\u2014 一堵由单一学科、过时经验筑起的高墙，让我们在浩瀚世界的羊肠小道上，误以为，这就是命运的全貌。2. 升维：八十层楼的视界 直到你决定，向上走。不是为了逃避，而是为了\u201c俯瞰\u201d。当你站上查理\u00b7芒格描述的\u201c栅格架\u201d，当你像乔布斯那样，推开八十层楼的高窗\u2014\u2014 城市的走向，不再是错综复杂的迷宫，而是能量与逻辑交织的脉络。知识不再是零散的瓦片，而是相互钩连、彼此照亮的星图。你看见了未来，那团\u201c分布不均\u201d的光，正从降维打击的缝隙里，透出隐性的光芒。3. 穿透：冰山下的静默结构 世界并非你所见的\u201c事件\u201d堆砌。那是海面上喧嚣的浪花，而决定航向的，是冰山下寂静的结构。你开始绕过问题，而不是解决问题，因为你知道，心智模式才是最底层的引擎。正如荣格所言，有些风暴你无需搏斗，你只需要\u201c长高\u201d，直到那些让你彻夜难眠的困境，在更高维度的意识里，慢慢淡化成地平线上一抹无关紧要的微尘。4. 自由：在旷野上绘制地图 于是，你从\u201c热锅上的爬虫\u201d变成了\u201c制图的人\u201d。人生不再是一条被定义好的轨道，而是一片无限延伸的旷野。所谓的\u201c安全感\u201d，不再源于那份昂贵的雇佣契约，而源于你脑中那副随时可以迭代的心智地图。每一次跨学科的融合，都是在荒原上架起一座新的桥梁。你不再害怕错过的浪潮，因为你已掌握了制造风暴的原理。5. 终局：认知的套利者 在这个世界大有搞头的逻辑里，最高级的财富，是你的选择权。它是智力资本在时间复利中的悄然绽放，它是认知高地对低洼地带的温柔俯瞰。当别人在存量博弈里拼刺刀，你已在\u201c正确非共识\u201d的无人区，种下了属于未来的森林。自由的代价，从来不是不被强迫，而是你看得见，万千条通往星辰的隐秘路径。',
                        help="Text to synthesize")
    parser.add_argument("--instruct_text", type=str, default="You are a helpful assistant. 很自然地说<|endofprompt|>",
                        help="Instruction text (if using instruct mode)")
    parser.add_argument("--zero_shot_spk_id", type=str, default="kehu_female_b", help="Zero shot speaker ID")
    parser.add_argument("--prompt_audio", type=str, default="kehu_female_b", help="Path to prompt wav file or id")
    parser.add_argument("--speed", type=float, default=1.0, help="Speech speed (default: 1.0)")
    parser.add_argument("--stream", type=bool, default=True, help="Stream output (True/False)")
    parser.add_argument("--target_func", type=str, default="instruct2",
                        choices=["zero_shot", "instruct2", "cross_lingual"], help="Which function to test")
    parser.add_argument("--playback", action="store_true", default=None,
                        help="Enable real-time speaker playback (requires pyaudio)")
    parser.add_argument("--no-playback", action="store_true",
                        help="Disable playback even if pyaudio is installed")

    args = parser.parse_args()
    if args.no_playback:
        enable_playback = False
    elif args.playback is True:
        enable_playback = PYAUDIO_AVAILABLE
        if not PYAUDIO_AVAILABLE:
            print("Warning: --playback requested but pyaudio is not installed; saving WAV only.")
    else:
        enable_playback = PYAUDIO_AVAILABLE
    if not enable_playback and not args.no_playback and not PYAUDIO_AVAILABLE:
        print("Note: pyaudio not installed; streaming to file only (no speaker playback).")

    print(f"Sending request to {TTS_BINDING_HOST}...")
    print(f"text: {args.text[:20]}...")
    print(f"target_func (model): {args.target_func}")

    # 获取认证信息
    x_date, authorization = getLocalAuthInfo(TTS_CUSTCODE, TTS_BINDING_API_KEY)

    # 构造请求头
    headers = {
        "x-date": x_date,
        "authorization": authorization,
        "Content-Type": "application/json",
        # 流式透传：告诉网关/代理不要缓冲响应
        "Accept": "application/octet-stream",
        "X-Accel-Buffering": "no",  # Nginx 反向代理禁用缓冲
        "Cache-Control": "no-cache",  # 禁用缓存
    }

    # 构造统一入口的请求体
    payload = {
        "componentCode": TTS_COMPONENTCODE,
        "model": TTS_MODEL,  # 必须传网关校验通过的模型名称
        "function": args.target_func,  # 真实的子功能通过 function 传给服务端做 dispatch
        "tts_params": {
            "input_text": args.text,
            "speaker_id": args.zero_shot_spk_id,
            "prompt_audio": args.prompt_audio,
            "instruct_text": args.instruct_text if args.target_func == "instruct2" else "",
            "stream": args.stream,
            "speed": args.speed
        }
    }

    start_time = time.time()
    try:
        # 发送请求
        response = requests.post(
            TTS_BINDING_HOST,
            json=payload,
            headers=headers,
            stream=args.stream,
            verify=False  # 忽略SSL证书验证
        )
        response_header_time = time.time() - start_time

        if response.status_code == 200:
            print(f"Success! HTTP响应头返回时间: {response_header_time:.3f} 秒")

            # 准备保存文件
            save_path = "gateway_test_output.wav"
            f_save = open(save_path, 'wb')

            p = None
            stream = None

            header_read = False
            header_buffer = b""
            sample_rate = 24000  # 默认值 CosyVoice3
            total_bytes = 0

            first_body_chunk_received = False
            ttfa_recorded = False

            try:
                # 使用 iter_content + 捕获 IncompleteRead
                # 服务端WAV头声明了2GB的datasize（流式占位），但实际音频远小于此
                # 网关可能据此设置Content-Length，导致requests在流结束时抛出IncompleteRead
                for chunk in response.iter_content(chunk_size=1024):
                    if chunk:
                        if not first_body_chunk_received:
                            first_body_chunk_time = time.time() - start_time
                            print(f"\n[Performance] TTFB首个响应体分片到达: {first_body_chunk_time:.3f} 秒\n")
                            first_body_chunk_received = True

                        # 保存到文件
                        f_save.write(chunk)
                        total_bytes += len(chunk)

                        if not header_read:
                            header_buffer += chunk
                            if len(header_buffer) >= 44:
                                # 解析WAV头获取采样率
                                try:
                                    sr_bytes = header_buffer[24:28]
                                    sample_rate = struct.unpack('<I', sr_bytes)[0]
                                    print(f"Detected sample rate: {sample_rate} Hz")
                                except Exception as e:
                                    print(f"Failed to parse sample rate: {e}, using default 24000")

                                audio_chunk = header_buffer[44:]
                                header_read = True
                                if audio_chunk and not ttfa_recorded:
                                    ttfa_time = time.time() - start_time
                                    print(f"\n[Performance] TTFA首段可播放音频到达: {ttfa_time:.3f} 秒\n")
                                    ttfa_recorded = True

                                if enable_playback:
                                    p = pyaudio.PyAudio()
                                    stream = p.open(format=pyaudio.paInt16,
                                                    channels=1,
                                                    rate=sample_rate,
                                                    output=True)

                                if stream and audio_chunk:
                                    stream.write(audio_chunk)
                                elif not enable_playback and audio_chunk and not ttfa_recorded:
                                    ttfa_time = time.time() - start_time
                                    print(f"\n[Performance] TTFA首段可播放音频到达: {ttfa_time:.3f} 秒 (仅保存文件)\n")
                                    ttfa_recorded = True
                        else:
                            if stream:
                                if not ttfa_recorded and len(chunk) > 0:
                                    ttfa_time = time.time() - start_time
                                    print(f"\n[Performance] TTFA首段可播放音频到达: {ttfa_time:.3f} 秒\n")
                                    ttfa_recorded = True
                                stream.write(chunk)

            except (IncompleteRead, ProtocolError) as e:
                # 服务端流式WAV头声明了2GB占位大小，实际音频结束后连接关闭
                # 网关据此Content-Length期望更多数据，导致IncompleteRead——这是正常的流结束
                print(f"\nStream ended (expected): {type(e).__name__}")
                print(f"Total audio bytes received: {total_bytes}")
            except requests.exceptions.ChunkedEncodingError as e:
                # requests 有时会把 IncompleteRead 包装成 ChunkedEncodingError
                print(f"\nStream ended (ChunkedEncodingError): {e}")
                print(f"Total audio bytes received: {total_bytes}")
            except KeyboardInterrupt:
                print("\nPlayback interrupted.")
            finally:
                f_save.close()
                # 修复WAV文件头，将占位的2GB大小替换为实际数据大小
                fix_wav_header(save_path)
                print(f"Audio saved to {save_path}")
                if stream:
                    stream.stop_stream()
                    stream.close()
                if p is not None:
                    p.terminate()
            print(f"Done. Total time: {time.time() - start_time:.2f}s")
        else:
            print(f"Error: {response.status_code}")
            try:
                print(response.json())
            except:
                print(response.text)

    except Exception as e:
        print(f"Request failed: {e}")


if __name__ == "__main__":
    main()
