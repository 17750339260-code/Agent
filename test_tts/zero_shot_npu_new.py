import argparse
import requests
import os
import pyaudio
import json
import time

def main():
    parser = argparse.ArgumentParser(description="Test /api/tts/zero-shot endpoint with streaming playback")
    parser.add_argument("--text", type=str, default="觉醒：走出单向度的隧道 我们曾是那只在大地褶皱里跋涉的小虫，在“山脚视角”的深井里，把勤奋磨损成一种生存的惯性。盯着那张被前人画满红线的“呆逼小地图”，在显性的路径里，为了一寸得失而屏息。那是认知的窄框架—— 一堵由单一学科、过时经验筑起的高墙，让我们在浩瀚世界的羊肠小道上，误以为，这就是命运的全貌。2. 升维：八十层楼的视界 直到你决定，向上走。不是为了逃避，而是为了“俯瞰”。当你站上查理·芒格描述的“栅格架”，当你像乔布斯那样，推开八十层楼的高窗—— 城市的走向，不再是错综复杂的迷宫，而是能量与逻辑交织的脉络。知识不再是零散的瓦片，而是相互钩连、彼此照亮的星图。你看见了未来，那团“分布不均”的光，正从降维打击的缝隙里，透出隐性的光芒。3. 穿透：冰山下的静默结构 世界并非你所见的“事件”堆砌。那是海面上喧嚣的浪花，而决定航向的，是冰山下寂静的结构。你开始绕过问题，而不是解决问题，因为你知道，心智模式才是最底层的引擎。正如荣格所言，有些风暴你无需搏斗，你只需要“长高”，直到那些让你彻夜难眠的困境，在更高维度的意识里，慢慢淡化成地平线上一抹无关紧要的微尘。4. 自由：在旷野上绘制地图 于是，你从“热锅上的爬虫”变成了“制图的人”。人生不再是一条被定义好的轨道，而是一片无限延伸的旷野。所谓的“安全感”，不再源于那份昂贵的雇佣契约，而源于你脑中那副随时可以迭代的心智地图。每一次跨学科的融合，都是在荒原上架起一座新的桥梁。你不再害怕错过的浪潮，因为你已掌握了制造风暴的原理。5. 终局：认知的套利者 在这个世界大有搞头的逻辑里，最高级的财富，是你的选择权。它是智力资本在时间复利中的悄然绽放，它是认知高地对低洼地带的温柔俯瞰。当别人在存量博弈里拼刺刀，你已在“正确非共识”的无人区，种下了属于未来的森林。自由的代价，从来不是不被强迫，而是你看得见，万千条通往星辰的隐秘路径。", help="Text to synthesize")
    parser.add_argument("--prompt_text", type=str, default="", help="Prompt text")
    parser.add_argument("--prompt_audio", type=str, default="", help="Path to prompt wav file")
    parser.add_argument("--zero_shot_spk_id", type=str, default="kehu_female_a", help="Zero shot speaker ID")
    parser.add_argument("--speed", type=float, default=1.0, help="Speech speed (default: 1.0)")
    parser.add_argument("--stream", type=bool, default=True, help="Stream output (True/False)")
    parser.add_argument("--seed", type=int, default=0, help="Random seed")
    parser.add_argument("--split", type=bool, default=True, help="Split long text (True/False)")
    parser.add_argument("--background_audio", type=str, default="", help="Background audio path")
    parser.add_argument("--background_volume", type=float, default=0.0, help="Background volume")
    parser.add_argument("--background_loop", type=bool, default=True, help="Background loop")
    parser.add_argument("--text_frontend", type=bool, default=True, help="Text frontend processing")
    parser.add_argument("--res_content", type=bool, default=True, help="Response content directly")
    parser.add_argument("--url", type=str, default="http://36.111.82.53:10015/api/tts/zero-shot", help="Endpoint URL")
    
    args = parser.parse_args()
    
    # Allow port override if URL is default local (helper logic, though default is now remote)
    # Keeping it simple as per user request to use the specific URL
    
    print(f"Sending request to {args.url}...")
    print(f"text: {args.text[:20]}...")
    print(f"zero_shot_spk_id: {args.zero_shot_spk_id}")
    
    # Construct JSON payload
    payload = {
        "tts_params": {
            "text": args.text,
            "zero_shot_spk_id": args.zero_shot_spk_id,
            "prompt_audio": args.prompt_audio,
            "prompt_text": args.prompt_text,
            "speed": args.speed,
            "stream": args.stream,
            "background_audio": args.background_audio,
            "background_volume": args.background_volume,
            "background_loop": args.background_loop,
            "text_frontend": args.text_frontend,
            "seed": args.seed,
            "split": args.split,
            "res_content": args.res_content
        }
    }
    
    # Fix "string" placeholders if they are actual file paths
    # If the user provided a real path in args, use it.
    # If args.prompt_audio is empty, sending "string" might be bad.
    # I'll stick to what args has. Default is "".
    # However, to strictly follow the "user curl" structure which has "string", 
    # I will rely on args default being empty string, which is better than "string".
    
    headers = {
        "accept": "application/json",
        "Content-Type": "application/json"
    }
    
    start_time = time.time()
    try:
        response = requests.post(
            args.url,
            json=payload,
            headers=headers,
            stream=True
        )
        
        if response.status_code == 200:
            print(f"Success! Buffering and playing audio stream...")
            print(f"Worker Info: Process ID={response.headers.get('X-Process-ID', 'N/A')}, Worker ID={response.headers.get('X-Worker-ID', 'N/A')}")
            
            # 准备保存文件
            save_path = "received_test.wav"
            f_save = open(save_path, 'wb')
            
            p = pyaudio.PyAudio()
            stream = None # 稍后初始化
            
            header_read = False
            header_buffer = b""
            sample_rate = 24000 # 默认值 CosyVoice3
            
            first_chunk_received = False
            ttfa_recorded = False

            try:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        if not first_chunk_received:
                            first_token_time = time.time() - start_time
                            print(f"\n[Performance] 首个Token返回时间: {first_token_time:.3f} 秒\n")
                            first_chunk_received = True
                            
                        # 保存到文件
                        f_save.write(chunk)
                        
                        if not header_read:
                            header_buffer += chunk
                            if len(header_buffer) >= 44:
                                # 解析WAV头获取采样率
                                import struct
                                # Offset 24 is Sample Rate (4 bytes)
                                try:
                                    sr_bytes = header_buffer[24:28]
                                    sample_rate = struct.unpack('<I', sr_bytes)[0]
                                    print(f"Detected sample rate: {sample_rate} Hz")
                                except Exception as e:
                                    print(f"Failed to parse sample rate: {e}, using default 24000")
                                
                                # 初始化播放流
                                stream = p.open(format=pyaudio.paInt16,
                                                channels=1,
                                                rate=sample_rate,
                                                output=True)
                                
                                # 跳过前44字节的WAV头
                                audio_chunk = header_buffer[44:]
                                header_read = True
                                # 直接播放
                                if stream:
                                    if not ttfa_recorded and len(audio_chunk) > 0:
                                        ttfa_time = time.time() - start_time
                                        print(f"\n[Performance] 端到端音频首帧播放时间(TTFA): {ttfa_time:.3f} 秒\n")
                                        ttfa_recorded = True
                                    stream.write(audio_chunk)
                        else:
                            # 直接播放
                            if stream:
                                if not ttfa_recorded and len(chunk) > 0:
                                    ttfa_time = time.time() - start_time
                                    print(f"\n[Performance] 端到端音频首帧播放时间(TTFA): {ttfa_time:.3f} 秒\n")
                                    ttfa_recorded = True
                                stream.write(chunk)
                
            except KeyboardInterrupt:
                print("\nPlayback interrupted.")
            finally:
                f_save.close()
                print(f"Audio saved to {save_path}")
                if stream:
                    stream.stop_stream()
                    stream.close()
                p.terminate()
            print("Done.")
        else:
            print(f"Error: {response.status_code}")
            print(response.text)
            
    except Exception as e:
        print(f"Request failed: {e}")

if __name__ == "__main__":
    main()
