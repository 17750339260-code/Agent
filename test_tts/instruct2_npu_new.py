import argparse
import os

import requests
import struct
import time
import threading
import queue

# 播放缓冲：先攒够 PRE_BUFFER_BYTES 再开始播放，避免断断续续
# 24kHz 16bit mono: 48000 bytes/s，攒 0.5s 的数据
PRE_BUFFER_BYTES = 48000


def playback_worker(audio_queue, sample_rate, playback_started, playback_done):
    """独立线程播放音频，从队列消费数据"""
    import pyaudio
    p = pyaudio.PyAudio()
    stream = p.open(format=pyaudio.paInt16, channels=1, rate=sample_rate, output=True)
    try:
        while True:
            chunk = audio_queue.get()
            if chunk is None:  # 结束信号
                break
            if not playback_started.is_set():
                playback_started.set()
            stream.write(chunk)
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()
        playback_done.set()


def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

def main():
    parser = argparse.ArgumentParser(description="Test /api/tts/instruct2 endpoint with streaming playback")
    parser.add_argument("--text", type=str,
        default="认知的套利者，在这个世界大有搞头的逻辑里，最高级的财富，是你的选择权。它是智力资本在时间复利中的悄然绽放，它是认知高地对低洼地带的温柔俯瞰。当别人在存量博弈里拼刺刀，你已在 this is the begin 正确非共识 this is the end 的无人区，种下了属于未来的森林。自由的代价，从来不是不被强迫，而是你看得见，万千条通往星辰的隐秘路径。",
        help="Text to synthesize")
    parser.add_argument("--instruct_text", type=str, default="You are a helpful assistant. 很自然地说<|endofprompt|>")
    parser.add_argument("--prompt_audio", type=str, default="kehu_female_c")
    parser.add_argument("--zero_shot_spk_id", type=str, default="kehu_female_b")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--stream", type=str2bool, default=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--split", type=str2bool, default=True)
    parser.add_argument("--background_audio", type=str, default="")
    parser.add_argument("--background_volume", type=float, default=0.0)
    parser.add_argument("--background_loop", type=str2bool, default=True)
    parser.add_argument("--text_frontend", type=str2bool, default=True)
    parser.add_argument("--res_content", type=str2bool, default=True)
    parser.add_argument("--url", type=str, default="http://127.0.0.1:10014/api/tts/instruct2")

    args = parser.parse_args()

    print(f"Sending request to {args.url}...")
    print(f"text: {args.text[:40]}...")
    print(f"instruct_text: {args.instruct_text}")
    print(f"zero_shot_spk_id: {args.zero_shot_spk_id}")

    payload = {
        "tts_params": {
            "text": args.text,
            "instruct_text": args.instruct_text,
            "zero_shot_spk_id": args.zero_shot_spk_id,
            "prompt_audio": args.prompt_audio,
            "speed": args.speed,
            "stream": args.stream,
            "background_audio": args.background_audio,
            "background_volume": args.background_volume,
            "background_loop": args.background_loop,
            "text_frontend": args.text_frontend,
            "seed": args.seed,
            "split": args.split,
            "res_content": args.res_content,
        }
    }

    headers = {"accept": "application/json", "Content-Type": "application/json"}

    start_time = time.time()
    try:
        response = requests.post(args.url, json=payload, headers=headers, stream=args.stream)

        if response.status_code != 200:
            print(f"Error: {response.status_code}")
            print(response.text)
            return

        print(f"Success! Streaming audio...")
        print(f"Worker Info: PID={response.headers.get('X-Process-ID', 'N/A')}, "
              f"Worker={response.headers.get('X-Worker-ID', 'N/A')}")
        TTS_DIR = "./tts_output"
        save_path = os.path.join(TTS_DIR, "received_test.wav")
        f_save = open(save_path, "wb")

        # 播放队列 + 独立线程，解耦网络接收和音频播放
        audio_q = queue.Queue(maxsize=500)
        playback_started = threading.Event()
        playback_done = threading.Event()
        play_thread = None

        sample_rate = 24000
        header_read = False
        header_buffer = b""
        pre_buffer = b""
        pre_buffering = True

        first_chunk_time = None
        ttfa_time = None

        try:
            # 用 8KB chunk 减少网络往返次数
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue

                if first_chunk_time is None:
                    first_chunk_time = time.time() - start_time
                    print(f"\n[Performance] 首个数据包到达: {first_chunk_time:.3f}s")

                # 保存到文件
                f_save.write(chunk)

                if not header_read:
                    header_buffer += chunk
                    if len(header_buffer) >= 44:
                        # 解析 WAV 头获取采样率
                        try:
                            sample_rate = struct.unpack('<I', header_buffer[24:28])[0]
                            print(f"Detected sample rate: {sample_rate} Hz")
                        except Exception as e:
                            print(f"Failed to parse sample rate: {e}, using default 24000")

                        # 启动播放线程
                        play_thread = threading.Thread(
                            target=playback_worker,
                            args=(audio_q, sample_rate, playback_started, playback_done),
                            daemon=True
                        )
                        play_thread.start()

                        audio_data = header_buffer[44:]
                        header_read = True

                        if audio_data:
                            pre_buffer += audio_data
                else:
                    if pre_buffering:
                        pre_buffer += chunk
                    else:
                        audio_q.put(chunk)

                # 攒够预缓冲后一次性送入播放队列
                if pre_buffering and header_read and len(pre_buffer) >= PRE_BUFFER_BYTES:
                    if ttfa_time is None:
                        ttfa_time = time.time() - start_time
                        print(f"[Performance] 首帧播放时间(TTFA): {ttfa_time:.3f}s")
                    audio_q.put(pre_buffer)
                    pre_buffer = b""
                    pre_buffering = False

            # 流结束，把剩余预缓冲送出去
            if pre_buffer:
                if ttfa_time is None:
                    ttfa_time = time.time() - start_time
                    print(f"[Performance] 首帧播放时间(TTFA): {ttfa_time:.3f}s")
                audio_q.put(pre_buffer)

            # 发送结束信号，等待播放完成
            audio_q.put(None)
            if play_thread:
                play_thread.join()

        except KeyboardInterrupt:
            print("\nPlayback interrupted.")
            audio_q.put(None)
        finally:
            f_save.close()
            total_time = time.time() - start_time
            print(f"Audio saved to {save_path}")
            print(f"Total time: {total_time:.2f}s")

        print("Done.")

    except Exception as e:
        print(f"Request failed: {e}")


if __name__ == "__main__":
    main()
