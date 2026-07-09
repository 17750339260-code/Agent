import argparse
import base64
import hmac
import os
import queue
import struct
import threading
import time
import wave
from datetime import datetime, timezone
from hashlib import sha256
from http.client import IncompleteRead

import requests
import urllib3
from urllib3.exceptions import ProtocolError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# # 公司测试环境网关地址
# TTS_BINDING = "southgrid"
# TTS_MODEL = "tts-v1"
# TTS_BINDING_HOST = os.getenv(
#     "TTS_BINDING_HOST",
#     "http://192.168.0.213:18300/ai-inference-gateway/predict",
# )
# TTS_BINDING_API_KEY = os.getenv("TTS_BINDING_API_KEY", "24e74daf74124b0b96c9cb113162a976")
# TTS_CUSTCODE = os.getenv("TTS_CUSTCODE", "1001300033")
# TTS_COMPONENTCODE = os.getenv("TTS_COMPONENTCODE", "04100945")

# # 生产机省测网关地址
# TTS_BINDING = "southgrid"
# TTS_MODEL = "TTS-v1"
# TTS_BINDING_HOST = os.getenv(
#     "TTS_BINDING_HOST",
#     "https://10.134.252.232:5030/ai-gateway/predict",
# )
# TTS_BINDING_API_KEY = os.getenv("TTS_BINDING_API_KEY", "c560cdb7d37240fab373d9f8a536a146")
# TTS_CUSTCODE = os.getenv("TTS_CUSTCODE", "1000401100004")
# TTS_COMPONENTCODE = os.getenv("TTS_COMPONENTCODE", "04350558")

# # 生产机网级网关地址
# TTS_BINDING = "southgrid"
# TTS_MODEL = "TTS-v1"
# TTS_BINDING_HOST = os.getenv(
#     "TTS_BINDING_HOST",
#     "https://10.10.65.213:18300/ai-inference-gateway/predict",
# )
# TTS_BINDING_API_KEY = os.getenv("TTS_BINDING_API_KEY", "24e74daf74124b0b96c9cb113162a976")
# TTS_CUSTCODE = os.getenv("TTS_CUSTCODE", "1001300033")
# TTS_COMPONENTCODE = os.getenv("TTS_COMPONENTCODE", "04100945")

TTS_BINDING = "southgrid"
TTS_MODEL = "TTS-v1"
TTS_BINDING_HOST = os.getenv(
    "TTS_BINDING_HOST",
    "https://10.134.252.232:5030/ai-gateway/predict",
)
TTS_BINDING_API_KEY = os.getenv("TTS_BINDING_API_KEY", "b899eef382324e8d8973493fb9c35998")
TTS_CUSTCODE = os.getenv("TTS_CUSTCODE", "1000400672300031")
TTS_COMPONENTCODE = os.getenv("TTS_COMPONENTCODE", "04351372")

DEFAULT_SAMPLE_RATE = 24000
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH = 2
MAX_REASONABLE_AUDIO_SECONDS = 300
MAX_HEADER_SIZE_DURATION_DRIFT = 0.20


def getLocalAuthInfo(customerCode, secretKey):
    """生成南网网关HMAC认证信息。"""
    date_value = datetime.now(timezone.utc).strftime("%a, %d %b %Y %T GMT")
    date_str = f"x-date: {date_value}"
    signature = base64.b64encode(
        hmac.new(secretKey.encode("utf-8"), date_str.encode("utf-8"), sha256).digest()
    ).decode("utf-8")
    authorization = (
        f'hmac username="{customerCode}", algorithm="hmac-sha256", '
        f'headers="x-date", signature="{signature}"'
    )
    return date_value, authorization


def str2bool(value):
    if isinstance(value, bool):
        return value
    normalized = value.lower()
    if normalized in ("yes", "true", "t", "y", "1"):
        return True
    if normalized in ("no", "false", "f", "n", "0"):
        return False
    raise argparse.ArgumentTypeError("Boolean value expected.")


def _format_seconds(value):
    return f"{value:.3f}s" if value and value > 0 else "N/A"


def _format_ratio(value):
    return f"{value:.3f}" if value and value > 0 else "N/A"


def _find_wav_data_chunk(audio_bytes):
    data_start = audio_bytes.find(b"data")
    if data_start == -1 or data_start + 8 > len(audio_bytes):
        return None
    data_size = int.from_bytes(audio_bytes[data_start + 4:data_start + 8], "little", signed=False)
    return data_start, data_start + 8, data_size


def _duration_from_size(byte_count, sample_rate, channels, sample_width):
    bytes_per_second = sample_rate * channels * sample_width
    return byte_count / bytes_per_second if byte_count > 0 and bytes_per_second > 0 else 0.0


def _parse_wav_prefix(audio_bytes):
    """从已收到的WAV前缀中解析播放需要的格式信息和data偏移。"""
    info = {
        "sample_rate": DEFAULT_SAMPLE_RATE,
        "channels": DEFAULT_CHANNELS,
        "sample_width": DEFAULT_SAMPLE_WIDTH,
        "data_offset": None,
    }

    if len(audio_bytes) < 12 or audio_bytes[:4] != b"RIFF" or audio_bytes[8:12] != b"WAVE":
        if len(audio_bytes) >= 44:
            info["data_offset"] = 44
        return info

    fmt_start = audio_bytes.find(b"fmt ")
    if fmt_start != -1 and fmt_start + 24 <= len(audio_bytes):
        try:
            info["channels"] = struct.unpack("<H", audio_bytes[fmt_start + 10:fmt_start + 12])[0]
            info["sample_rate"] = struct.unpack("<I", audio_bytes[fmt_start + 12:fmt_start + 16])[0]
            bits_per_sample = struct.unpack("<H", audio_bytes[fmt_start + 22:fmt_start + 24])[0]
            info["sample_width"] = max(bits_per_sample // 8, 1)
        except struct.error:
            pass

    data_chunk = _find_wav_data_chunk(audio_bytes)
    if data_chunk:
        _, data_offset, _ = data_chunk
        info["data_offset"] = data_offset
    elif len(audio_bytes) >= 44:
        info["data_offset"] = 44

    return info


def _get_audio_duration_info(audio_path):
    """返回音频时长与计算来源，避免流式WAV头占位长度污染RTF。"""
    if not audio_path or not os.path.exists(audio_path):
        return {"duration": 0.0, "source": "missing", "sample_rate": 0, "channels": 0, "sample_width": 0}

    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()

        sample_rate = DEFAULT_SAMPLE_RATE
        channels = DEFAULT_CHANNELS
        sample_width = DEFAULT_SAMPLE_WIDTH
        header_duration = 0.0

        try:
            with wave.open(audio_path, "rb") as wav:
                sample_rate = wav.getframerate() or DEFAULT_SAMPLE_RATE
                channels = wav.getnchannels() or DEFAULT_CHANNELS
                sample_width = wav.getsampwidth() or DEFAULT_SAMPLE_WIDTH
                frames = wav.getnframes()
                header_duration = frames / float(sample_rate) if sample_rate > 0 and frames > 0 else 0.0
        except Exception:
            prefix_info = _parse_wav_prefix(audio_bytes[:128])
            sample_rate = prefix_info["sample_rate"]
            channels = prefix_info["channels"]
            sample_width = prefix_info["sample_width"]

        data_chunk = _find_wav_data_chunk(audio_bytes)
        if data_chunk:
            _, data_offset, declared_size = data_chunk
            actual_data_bytes = max(len(audio_bytes) - data_offset, 0)
            data_bytes = actual_data_bytes
            if 0 < declared_size <= actual_data_bytes:
                data_bytes = declared_size
        else:
            data_bytes = max(len(audio_bytes) - 44, 0)

        size_duration = _duration_from_size(data_bytes, sample_rate, channels, sample_width)
        header_is_reasonable = (
            0 < header_duration <= MAX_REASONABLE_AUDIO_SECONDS
            and (
                size_duration <= 0
                or abs(header_duration - size_duration) / max(size_duration, 0.001)
                <= MAX_HEADER_SIZE_DURATION_DRIFT
            )
        )

        if header_is_reasonable:
            duration = header_duration
            source = "WAV header"
        else:
            duration = size_duration
            source = "file size fallback"

        return {
            "duration": duration,
            "source": source,
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width": sample_width,
            "header_duration": header_duration,
            "size_duration": size_duration,
        }
    except Exception as exc:
        return {
            "duration": 0.0,
            "source": f"error: {exc}",
            "sample_rate": 0,
            "channels": 0,
            "sample_width": 0,
        }


def fix_wav_header(filepath):
    """修复RIFF和data块大小；兼容data块不在固定40偏移的WAV。"""
    try:
        file_size = os.path.getsize(filepath)
        if file_size < 44:
            return

        with open(filepath, "r+b") as f:
            audio_bytes = f.read()
            data_chunk = _find_wav_data_chunk(audio_bytes)
            if data_chunk:
                data_start, data_offset, _ = data_chunk
            else:
                data_start, data_offset = 40, 44

            riff_size = max(file_size - 8, 0)
            data_size = max(file_size - data_offset, 0)
            f.seek(4)
            f.write(struct.pack("<I", riff_size))
            f.seek(data_start + 4)
            f.write(struct.pack("<I", data_size))
        print(f"WAV header fixed: data size = {data_size} bytes")
    except Exception as exc:
        print(f"Warning: failed to fix WAV header: {exc}")


def play_thread_func(audio_queue, sample_rate, channels, sample_width):
    """后台播放线程，从队列中取PCM音频数据并播放。"""
    try:
        import pyaudio

        sample_format = {
            1: pyaudio.paInt8,
            2: pyaudio.paInt16,
            3: pyaudio.paInt24,
            4: pyaudio.paInt32,
        }.get(sample_width, pyaudio.paInt16)

        p = pyaudio.PyAudio()
        stream = p.open(format=sample_format, channels=channels, rate=sample_rate, output=True)
    except Exception as exc:
        print(f"Warning: playback unavailable: {exc}")
        while audio_queue.get() is not None:
            pass
        return

    try:
        while True:
            data = audio_queue.get()
            if data is None:
                break
            stream.write(data)
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()


def build_headers():
    x_date, authorization = getLocalAuthInfo(TTS_CUSTCODE, TTS_BINDING_API_KEY)
    return {
        "x-date": x_date,
        "authorization": authorization,
        "Content-Type": "application/json",
        "Accept": "application/octet-stream",
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache",
    }


def build_payload(args):
    return {
        "componentCode": TTS_COMPONENTCODE,
        "model": TTS_MODEL,
        "function": args.target_func,
        "tts_params": {
            "input_text": args.text,
            "speaker_id": args.zero_shot_spk_id,
            "prompt_audio": args.prompt_audio,
            "instruct_text": args.instruct_text if args.target_func == "instruct2" else "",
            "stream": args.stream,
            "speed": args.speed,
        },
    }


def request_tts(args):
    metrics = {
        "response_header_time": 0.0,
        "ttfb": 0.0,
        "ttft": 0.0,
        "ttfa": 0.0,
        "rt": 0.0,
        "size": 0,
        "path": args.output,
    }

    total_bytes = 0
    first_body_chunk_received = False
    header_read = False
    header_buffer = b""
    wav_info = {
        "sample_rate": DEFAULT_SAMPLE_RATE,
        "channels": DEFAULT_CHANNELS,
        "sample_width": DEFAULT_SAMPLE_WIDTH,
        "data_offset": None,
    }

    audio_queue = queue.Queue()
    player_thread = None
    playback_enabled = args.playback

    start_time = time.time()
    try:
        response = requests.post(
            args.url,
            json=build_payload(args),
            headers=build_headers(),
            stream=True,
            verify=False,
            timeout=(args.connect_timeout, args.read_timeout),
        )
        metrics["response_header_time"] = time.time() - start_time

        with response:
            if response.status_code != 200:
                print(f"Error: {response.status_code}")
                try:
                    print(response.json())
                except Exception:
                    print(response.text)
                return None

            print(f"Success! HTTP响应头返回时间: {metrics['response_header_time']:.3f} 秒")

            with open(args.output, "wb") as f_save:
                try:
                    for chunk in response.iter_content(chunk_size=args.chunk_size):
                        if not chunk:
                            continue

                        now = time.time()
                        if not first_body_chunk_received:
                            metrics["ttfb"] = now - start_time
                            metrics["ttft"] = metrics["ttfb"]
                            print(f"\n[Performance] TTFB/TTFT首个响应体分片到达: {metrics['ttfb']:.3f} 秒\n")
                            first_body_chunk_received = True

                        f_save.write(chunk)
                        total_bytes += len(chunk)

                        if not header_read:
                            header_buffer += chunk
                            wav_info = _parse_wav_prefix(header_buffer)
                            data_offset = wav_info.get("data_offset")
                            if data_offset is None or len(header_buffer) <= data_offset:
                                continue

                            header_read = True
                            print(
                                "Detected audio format: "
                                f"{wav_info['sample_rate']}Hz, "
                                f"{wav_info['channels']}ch, "
                                f"{wav_info['sample_width'] * 8}bit"
                            )
                            audio_part = header_buffer[data_offset:]
                        else:
                            audio_part = chunk

                        if audio_part and metrics["ttfa"] <= 0:
                            metrics["ttfa"] = now - start_time
                            print(f"\n[Performance] TTFA首段可播放音频到达: {metrics['ttfa']:.3f} 秒\n")

                        if playback_enabled and audio_part:
                            if player_thread is None:
                                try:
                                    import pyaudio  # noqa: F401

                                    player_thread = threading.Thread(
                                        target=play_thread_func,
                                        args=(
                                            audio_queue,
                                            wav_info["sample_rate"],
                                            wav_info["channels"],
                                            wav_info["sample_width"],
                                        ),
                                        daemon=True,
                                    )
                                    player_thread.start()
                                except Exception as exc:
                                    playback_enabled = False
                                    print(f"Warning: playback disabled: {exc}")

                            if playback_enabled:
                                audio_queue.put(audio_part)

                except (IncompleteRead, ProtocolError, requests.exceptions.ChunkedEncodingError) as exc:
                    print(f"\nStream ended early: {type(exc).__name__}: {exc}")
                finally:
                    metrics["rt"] = time.time() - start_time

    except Exception as exc:
        print(f"Request failed: {exc}")
        return None
    finally:
        audio_queue.put(None)
        if player_thread is not None:
            try:
                player_thread.join(timeout=10)
            except Exception as exc:
                print(f"Warning: playback thread did not finish cleanly: {exc}")

    fix_wav_header(args.output)
    metrics["size"] = os.path.getsize(args.output) if os.path.exists(args.output) else total_bytes
    return metrics


def print_tts_key_metrics(scene_name, metrics, text):
    duration_info = _get_audio_duration_info(metrics.get("path"))
    audio_duration = duration_info["duration"]
    rt = metrics.get("rt", 0.0)
    rtf = rt / audio_duration if audio_duration > 0 else 0.0
    synth_speed = audio_duration / rt if rt > 0 else 0.0
    text_len = len(text or "")
    chars_per_sec = text_len / rt if rt > 0 else 0.0
    audio_kb = metrics.get("size", 0) / 1024 if metrics.get("size") else 0.0

    print("\n" + "=" * 60)
    print(f"📊 {scene_name} TTS关键指标")
    print("  延迟:")
    print(f"    HTTP响应头        : {_format_seconds(metrics.get('response_header_time'))}")
    print(f"    TTFB/TTFT首音频包 : {_format_seconds(metrics.get('ttfb') or metrics.get('ttft'))}")
    print(f"    TTFA首段可播放音频: {_format_seconds(metrics.get('ttfa'))}")
    print(f"    RT合成总耗时      : {_format_seconds(rt)}")
    print("  实时性:")
    print(f"    音频时长          : {_format_seconds(audio_duration)} ({duration_info['source']})")
    print(f"    RTF=RT/音频时长   : {_format_ratio(rtf)}")
    print(
        f"    合成速度=1/RTF    : {synth_speed:.2f}x实时"
        if synth_speed > 0
        else "    合成速度=1/RTF    : N/A"
    )
    print("  吞吐与产物:")
    print(f"    文本长度          : {text_len}字")
    print(f"    文本吞吐          : {chars_per_sec:.1f}字/s" if chars_per_sec > 0 else "    文本吞吐          : N/A")
    print(f"    文件大小          : {audio_kb:.1f}KB")
    print("  判定参考:")
    print("    TTFT阈值          : 0.35s")
    print("    RTF说明           : RTF<1 表示合成快于实时播放；TTRF不是当前脚本中的独立指标，通常应按RTF理解")
    print("=" * 60)


def parse_args():
    parser = argparse.ArgumentParser(description="Test AI Gateway /predict endpoint for TTS")
    parser.add_argument(
        "--text",
        type=str,
        default=(
            " 语音合成技术，又称文语转换，是指通过计算机算法将输入的文本信息转换为自然流畅语音输出的技术。它让机器得以“开口说话”，是人机交互领域的关键一环。从早期冰冷的机械发音，到今天以假乱真、带有情感的语音，TTS技术已经走过了近百年的演进之路。最早的语音合成可以追溯到18世纪的机械发声装置，但真正意义上的电子合成则始于20世纪中期。当时的系统基于规则，依靠对发音器官的物理建模，例如共振峰合成。这种方法通过调整频率、带宽等参数模拟元音和辅音，生成的语音虽然可懂，但听起来机械感十足，像机器人说话，缺乏自然的韵律。到了20世纪90年代，拼接合成法成为主流。它的原理是先录制大量真人语音片段，建立一个庞大的音库，合成时根据输入文本从音库中选择合适的基元，再拼接成完整的语句。拼接合成的音质有了显著提升，但要做到高度自然，需要极大规模的音库，且拼接点容易产生不连贯的“咔嗒”声，语调调整也不够灵活。随后，参数统计合成方法，特别是隐马尔可夫模型的引入，让语音合成迈向了数据驱动。隐马尔可夫模型对语音的频谱、基频、时长等参数进行建模，合成时根据文本序列预测参数，再由声码器还原成波形。这种方式生成的语音平稳流畅，但音质偏“沉闷”，常被形容为带着一股挥之不去的“电子音”，与真实人声仍有明显差距。真正的变革来自深度学习。2016年，DeepMind推出的WaveNet模型直接对原始音频波形进行自回归建模，生成的高保真语音震惊了学术界。它模拟了每一个采样点的概率分布，使语音中的气声、语调细微变化都得以呈现，但也因为逐点生成导致速度极慢，无法满足实时需求。之后，端到端的序列到序列模型Tacotron横空出世，它直接将字符或音素序列映射到梅尔频谱图，再通过声码器合成语音。"
        ),
        help="Text to synthesize",
    )
    parser.add_argument("--instruct_text", type=str, default="You are a helpful assistant. 很自然地说<|endofprompt|>")
    parser.add_argument("--zero_shot_spk_id", type=str, default="kehu_female_b")
    parser.add_argument("--prompt_audio", type=str, default="kehu_female_b")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--stream", type=str2bool, default=True)
    parser.add_argument("--playback", type=str2bool, default=True)
    parser.add_argument("--target_func", type=str, default="instruct2", choices=["zero_shot", "instruct2", "cross_lingual"])
    parser.add_argument("--url", type=str, default=TTS_BINDING_HOST)
    parser.add_argument("--output", type=str, default="gateway_test_output.wav")
    parser.add_argument("--chunk_size", type=int, default=1024)
    parser.add_argument("--connect_timeout", type=float, default=10)
    parser.add_argument("--read_timeout", type=float, default=300)
    return parser.parse_args()


def main():
    args = parse_args()

    print(f"Sending request to {args.url}...")
    print(f"text: {args.text[:40]}...")
    print(f"target_func (model): {args.target_func}")

    metrics = request_tts(args)
    if not metrics:
        return

    scene_name = f"{args.target_func.capitalize()}语音字幕同步"
    print_tts_key_metrics(scene_name, metrics, args.text)


if __name__ == "__main__":
    main()
