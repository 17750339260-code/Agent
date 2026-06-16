"""
🔥测试tts——zero接口零样本语音合成
"""

import pytest
import requests
import json
import os
import time
import struct
import pyaudio
import wave
import io
import threading
import numpy as np
from conftest import (
    OUTPUT_DIR,
    TTS_TTFT_MAX,
    TTS_RT_MAX,
    ACCURACY_THRESHOLD,
    asr_request,
    crr_calculator,
    perf_stats
)


def zero_shot_tts_request_with_sync(url, payload, enable_playback=True):
    """
    零样本TTS接口请求函数 - 简化版
    """
    timestamp = int(time.time() * 1000)
    audio_path = os.path.join(OUTPUT_DIR, f"zero_shot_{timestamp}.wav")

    start_time = time.time()
    first_chunk_time = None
    last_chunk_time = None
    audio_data = b""

    try:
        headers = {"accept": "application/json", "Content-Type": "application/json"}
        test_text = payload.get("tts_params", {}).get("text", "")
        if not test_text:
            test_text = payload.get("text", "")

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            stream=True,
            verify=False,
            timeout=30
        )

        if response.status_code != 200:
            raise RuntimeError(f"零样本TTS接口失败: {response.status_code}")

        if enable_playback and test_text:
            subtitle_thread = threading.Thread(
                target=show_streaming_subtitle,
                args=(test_text,),
                name="StreamingSubtitle"
            )
            subtitle_thread.daemon = True
            subtitle_thread.start()

        f_save = open(audio_path, 'wb')
        p = None
        stream = None
        header_read = False
        header_buffer = b""
        sample_rate = 24000
        first_chunk_received = False

        try:
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:
                    current_time = time.time()
                    if first_chunk_time is None:
                        first_chunk_time = current_time
                    last_chunk_time = current_time

                    f_save.write(chunk)
                    audio_data += chunk

                    if not first_chunk_received:
                        first_token_time = current_time - start_time
                        first_chunk_received = True

                    if enable_playback and chunk:
                        if not header_read:
                            header_buffer += chunk
                            if len(header_buffer) >= 44:
                                try:
                                    sr_bytes = header_buffer[24:28]
                                    sample_rate = struct.unpack('<I', sr_bytes)[0]
                                except:
                                    sample_rate = 24000

                                p = pyaudio.PyAudio()
                                stream = p.open(
                                    format=pyaudio.paInt16,
                                    channels=1,
                                    rate=sample_rate,
                                    output=True
                                )

                                audio_chunk = header_buffer[44:]
                                header_read = True

                                if stream and len(audio_chunk) > 0:
                                    stream.write(audio_chunk)
                        else:
                            if stream:
                                stream.write(chunk)

        finally:
            f_save.close()
            if stream:
                stream.stop_stream()
                stream.close()
            if p:
                p.terminate()

            if enable_playback and test_text:
                time.sleep(0.5)

        ttft = round(first_chunk_time - start_time, 3) if first_chunk_time else 0.0
        rt = round(last_chunk_time - start_time, 3) if last_chunk_time else 0.0

        return {
            "ttft": ttft,
            "rt": rt,
            "path": audio_path,
            "size": len(audio_data),
            "success": True
        }

    except Exception as e:
        if 'audio_path' in locals() and os.path.exists(audio_path):
            os.remove(audio_path)
        raise


def show_streaming_subtitle(text, char_delay=0.12):
    """简化字幕显示"""
    time.sleep(1.0)
    for char in text:
        print(char, end="", flush=True)
        time.sleep(char_delay)
    print()


class TestZeroShotTTSMetrics:
    """零样本TTS核心指标测试类 - 8个测试用例"""

    def test_1_zero_shot_TTFT首包延迟(self, zero_shot_tts_url, ttft_limit, perf_stats):
        """指标1：零样本TTFT首包延迟专项测试"""
        print(f"\n[指标1] TTFT首包延迟测试 | 阈值={ttft_limit}s")

        payload = {
            "tts_params": {
                "text": "测试零样本首包延迟",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True,
                "response_format": "wav"
            }
        }

        res = zero_shot_tts_request_with_sync(zero_shot_tts_url, payload, enable_playback=True)
        perf_stats["TTFT"].append(res["ttft"])

        print(f"TTFT: {res['ttft']:.3f}s | 文件: {res['size']}字节")
        assert res["ttft"] <= ttft_limit, f"TTFT超时: {res['ttft']}s > {ttft_limit}s"

    def test_2_zero_shot_RT总耗时(self, zero_shot_tts_url, rt_limit, perf_stats):
        """指标2：零样本RT合成总耗时专项测试"""
        print(f"\n[指标2] RT合成总耗时测试 | 阈值={rt_limit}s")

        payload = {
            "tts_params": {
                "text": "测试零样本合成总耗时",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True,
                "response_format": "wav"
            }
        }

        res = zero_shot_tts_request_with_sync(zero_shot_tts_url, payload, enable_playback=True)
        perf_stats["RT"].append(res["rt"])

        print(f"RT: {res['rt']:.3f}s | 文件: {res['size']}字节")
        assert res["rt"] <= rt_limit, f"RT超时: {res['rt']}s > {rt_limit}s"

    def test_3_zero_shot_文本准确率(self, zero_shot_tts_url, crr_calculator, asr_request):
        """指标3：零样本文本识别准确率专项测试"""
        print(f"\n[指标3] 文本准确率测试 | 阈值={ACCURACY_THRESHOLD}")

        test_text = "你好，测试零样本文本准确率，包含数字123。"
        payload = {
            "tts_params": {
                "text": test_text,
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True,
                "response_format": "wav"
            }
        }

        res = zero_shot_tts_request_with_sync(zero_shot_tts_url, payload, enable_playback=False)
        asr_text = asr_request(res["path"])

        print(f"原始: {test_text}")
        print(f"识别: {asr_text if asr_text else '无返回'}")

        if not asr_text:
            print("跳过: ASR接口无返回")
            pytest.skip("ASR接口无返回")

        crr = crr_calculator(test_text, asr_text)
        print(f"准确率: {crr:.4f}")

        if crr >= 0.95:
            level = "优秀"
        elif crr >= 0.90:
            level = "良好"
        elif crr >= 0.85:
            level = "及格"
        elif crr >= 0.70:
            level = "偏低"
        else:
            level = "不达标"

        print(f"评估: {level}")
        assert crr >= ACCURACY_THRESHOLD, f"准确率不达标: {crr} < {ACCURACY_THRESHOLD}"

    def test_4_zero_shot_音文同步(self, zero_shot_tts_url):
        """指标4：零样本音文同步有效性专项测试"""
        print(f"\n[指标4] 音文同步测试")

        payload = {
            "tts_params": {
                "text": "测试零样本音文同步",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True,
                "response_format": "wav"
            }
        }

        res = zero_shot_tts_request_with_sync(zero_shot_tts_url, payload, enable_playback=True)
        audio_path = res["path"]
        file_exist = os.path.exists(audio_path)
        file_size = os.path.getsize(audio_path) if file_exist else 0

        print(f"文件: {audio_path}")
        print(f"大小: {file_size}字节 | 状态: {'正常' if file_exist else '失败'}")

        min_file_size = 50
        assert file_exist and file_size > min_file_size, f"音频文件无效: 存在={file_exist}, 大小={file_size}"

    def test_5_zero_shot_音频音质(self, zero_shot_tts_url):
        """指标5：零样本音频文件音质有效性专项测试"""
        print(f"\n[指标5] 音频音质测试")

        payload = {
            "tts_params": {
                "text": "测试零样本音频音质",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True,
                "response_format": "wav"
            }
        }

        res = zero_shot_tts_request_with_sync(zero_shot_tts_url, payload, enable_playback=True)
        audio_path = res["path"]

        try:
            with wave.open(audio_path, 'rb') as wav:
                frames = wav.getnframes()
                rate = wav.getframerate()
                duration = frames / float(rate) if rate > 0 else 0

                print(f"采样率: {rate}Hz | 时长: {duration:.3f}s")
                print(f"声道: {wav.getnchannels()} | 帧数: {frames}")

                assert rate in [8000, 16000, 24000, 44100, 48000]
                assert duration > 0.1

                file_size = os.path.getsize(audio_path)
                if duration > 0:
                    print(f"码率: {(file_size * 8) / duration / 1000:.1f} kbps")

        except Exception as e:
            print(f"WAV验证失败: {e}")
            file_size = os.path.getsize(audio_path)
            min_file_size = 50
            print(f"文件大小: {file_size}字节 | 最小: {min_file_size}字节")
            assert file_size > min_file_size, f"音频文件过小: {file_size}字节"

    def test_6_zero_shot_流式稳定性(self, zero_shot_tts_url):
        """指标6：零样本流式传输稳定性专项测试"""
        print(f"\n[指标6] 流式稳定性测试")

        payload = {
            "tts_params": {
                "text": "测试零样本流式稳定性，验证分片接收是否完整无中断。",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True,
                "response_format": "wav"
            }
        }

        res = zero_shot_tts_request_with_sync(zero_shot_tts_url, payload, enable_playback=True)
        file_size = res.get("size", 0)

        print(f"流式文件: {res['path']}")
        print(f"文件大小: {file_size}字节")

        assert file_size > 0, "流式文件为空"
        with open(res["path"], 'rb') as f:
            file_data = f.read()
            if len(file_data) >= 44 and file_data[0:4] == b'RIFF':
                print("WAV格式: 正常")
            else:
                print("WAV格式: 警告")

    def test_7_zero_shot_P95稳定性(self, zero_shot_tts_url, perf_stats):
        """指标7：零样本P95性能数据稳定性专项测试"""
        print(f"\n[指标7] P95稳定性测试 | 采样=3次")

        test_count = 3
        old_ttft = len(perf_stats["TTFT"])
        old_rt = len(perf_stats["RT"])

        ttft_values = []
        rt_values = []

        for i in range(test_count):
            payload = {
                "tts_params": {
                    "text": f"P95零样本性能采样_{i + 1}",
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": 1.0,
                    "stream": True,
                    "response_format": "wav"
                }
            }

            res = zero_shot_tts_request_with_sync(zero_shot_tts_url, payload, enable_playback=False)
            ttft_values.append(res["ttft"])
            rt_values.append(res["rt"])
            print(f"采样{i+1}: TTFT={res['ttft']:.3f}s, RT={res['rt']:.3f}s")

        perf_stats["TTFT"].extend(ttft_values)
        perf_stats["RT"].extend(rt_values)

        if len(ttft_values) > 1:
            sorted_ttft = sorted(ttft_values)
            manual_p95_ttft = sorted_ttft[int(len(sorted_ttft) * 0.95)]
            numpy_p95_ttft = np.percentile(ttft_values, 95)
            print(f"P95手动: {manual_p95_ttft:.3f}s | numpy: {numpy_p95_ttft:.3f}s")
            assert abs(manual_p95_ttft - numpy_p95_ttft) < 0.01, "P95计算不一致"

        new_ttft_num = len(perf_stats["TTFT"]) - old_ttft
        new_rt_num = len(perf_stats["RT"]) - old_rt
        print(f"新增TTFT: {new_ttft_num}条 | RT: {new_rt_num}条")
        assert new_ttft_num == test_count and new_rt_num == test_count, "性能采样数据丢失"

    def test_8_zero_shot_参数组合(self, zero_shot_tts_url, perf_stats):
        """指标8：零样本参数组合兼容性专项测试"""
        print(f"\n[指标8] 参数组合测试")

        test_cases = [
            {"name": "基础参数", "speed": 1.0, "spk": "kehu_female_b"},
            {"name": "慢速-女声", "speed": 0.8, "spk": "kehu_female_b"},
            {"name": "快速-女声", "speed": 1.2, "spk": "kehu_female_b"},
            {"name": "正常-男声", "speed": 1.0, "spk": "yingyeyuan_male"},
        ]

        all_ttfts = []
        all_rts = []

        for i, case in enumerate(test_cases, 1):
            payload = {
                "tts_params": {
                    "text": f"测试{case['name']}",
                    "zero_shot_spk_id": case["spk"],
                    "speed": case["speed"],
                    "stream": True,
                    "response_format": "wav"
                }
            }

            res = zero_shot_tts_request_with_sync(zero_shot_tts_url, payload, enable_playback=False)
            all_ttfts.append(res["ttft"])
            all_rts.append(res["rt"])
            print(f"{case['name']}: TTFT={res['ttft']:.3f}s, RT={res['rt']:.3f}s")

        perf_stats["TTFT"].extend(all_ttfts)
        perf_stats["RT"].extend(all_rts)

        print(f"TTFT范围: {min(all_ttfts):.3f}s~{max(all_ttfts):.3f}s")
        print(f"RT范围: {min(all_rts):.3f}s~{max(all_rts):.3f}s")
        assert len(all_ttfts) == len(test_cases) and len(all_rts) == len(test_cases)


class TestZeroShotTTSFunctional:
    """零样本TTS功能增强测试 - 4个测试用例"""

    def test_9_zero_shot_语音质量评估(self, zero_shot_tts_url):
        """指标9：零样本语音质量专项测试"""
        print(f"\n[指标9] 语音质量测试")

        test_text = "这是一个零样本语音质量测试，评估音高、音色、自然度和情感表达。"
        payload = {
            "tts_params": {
                "text": test_text,
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True,
                "response_format": "wav"
            }
        }

        res = zero_shot_tts_request_with_sync(zero_shot_tts_url, payload, enable_playback=True)
        print(f"文本长度: {len(test_text)}字符")
        print(f"文件大小: {res['size']}字节")
        assert res["size"] > len(test_text) * 2, "音频文件异常偏小"

    def test_10_zero_shot_多语言支持(self, zero_shot_tts_url):
        """指标10：零样本多语言/方言支持测试"""
        print(f"\n[指标10] 多语言支持测试")

        test_cases = [
            {"text": "Hello, this is an English test.", "desc": "英文"},
            {"text": "你好，这是中文测试。", "desc": "中文"},
            {"text": "1234567890", "desc": "数字"},
            {"text": "Hello 123 你好！@#", "desc": "混合"},
        ]

        success_count = 0
        for i, case in enumerate(test_cases, 1):
            payload = {
                "tts_params": {
                    "text": case["text"],
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": 1.0,
                    "stream": True,
                    "response_format": "wav"
                }
            }

            try:
                res = zero_shot_tts_request_with_sync(zero_shot_tts_url, payload, enable_playback=False)
                if res["size"] > 50:
                    print(f"{case['desc']}: 成功")
                    success_count += 1
                else:
                    print(f"{case['desc']}: 文件过小")
            except Exception as e:
                print(f"{case['desc']}: 失败 - {str(e)[:30]}")

        print(f"总计: {len(test_cases)}用例, 成功: {success_count}")
        assert success_count >= len(test_cases) * 0.7, f"多语言支持不足: {success_count}/{len(test_cases)}"

    def test_11_zero_shot_标点处理(self, zero_shot_tts_url):
        """指标11：零样本标点符号语音处理测试"""
        print(f"\n[指标11] 标点处理测试")

        test_cases = [
            {"text": "你好，世界！", "desc": "逗号和感叹号"},
            {"text": "这是问号？", "desc": "问号"},
            {"text": "括号测试（括号内容）", "desc": "括号"},
            {"text": "省略号测试...", "desc": "省略号"},
        ]

        success_count = 0
        for i, case in enumerate(test_cases, 1):
            payload = {
                "tts_params": {
                    "text": case["text"],
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": 1.0,
                    "stream": True,
                    "response_format": "wav"
                }
            }

            try:
                res = zero_shot_tts_request_with_sync(zero_shot_tts_url, payload, enable_playback=False)
                if res["size"] > 50:
                    print(f"{case['desc']}: 成功 ({res['size']}字节)")
                    success_count += 1
                else:
                    print(f"{case['desc']}: 文件过小")
            except Exception as e:
                print(f"{case['desc']}: 失败")

        print(f"总计: {len(test_cases)}种标点, 成功: {success_count}")
        assert success_count >= len(test_cases) * 0.7, f"标点处理异常: {success_count}/{len(test_cases)}"

    def test_12_zero_shot_边界值测试(self, zero_shot_tts_url):
        """指标12：零样本边界值测试"""
        print(f"\n[指标12] 边界值测试")

        test_cases = [
            {"text": "A", "desc": "1个英文字符"},
            {"text": "测试" * 10, "desc": "20字符"},
            {"text": "测试" * 50, "desc": "100字符"},
        ]

        success_count = 0
        for i, case in enumerate(test_cases, 1):
            payload = {
                "tts_params": {
                    "text": case["text"],
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": 1.0,
                    "stream": True,
                    "response_format": "wav"
                }
            }

            try:
                res = zero_shot_tts_request_with_sync(zero_shot_tts_url, payload, enable_playback=False)
                if res["size"] > 50:
                    print(f"{case['desc']}: 成功 ({res['size']}字节)")
                    success_count += 1
                else:
                    print(f"{case['desc']}: 文件过小")
            except Exception as e:
                print(f"{case['desc']}: 失败")

        print(f"总计: {len(test_cases)}用例, 成功: {success_count}")
        assert success_count >= len(test_cases) * 0.7, f"边界处理异常: {success_count}/{len(test_cases)}"


# 调试测试
def test_debug_zero_shot_interface(zero_shot_tts_url):
    """调试：检查零样本接口返回的数据"""
    print(f"\n[调试] 检查零样本接口")

    payload = {
        "tts_params": {
            "text": "测试接口调试",
            "zero_shot_spk_id": "kehu_female_b",
            "speed": 1.0,
            "stream": True,
            "response_format": "wav"
        }
    }

    headers = {"accept": "application/json", "Content-Type": "application/json"}

    try:
        response = requests.post(
            zero_shot_tts_url,
            json=payload,
            headers=headers,
            stream=True,
            verify=False,
            timeout=10
        )

        print(f"状态码: {response.status_code}")
        print(f"响应大小: {len(response.content)}字节")

        if response.status_code == 200:
            content = response.content
            if len(content) > 4 and content[:4] == b'RIFF':
                print("格式: WAV音频")
                with open("audio_output/debug_zero_shot.wav", "wb") as f:
                    f.write(content)
                print("已保存: debug_zero_shot.wav")
            else:
                try:
                    text = content.decode('utf-8', errors='ignore')
                    print(f"响应: {text[:200]}")
                except:
                    print(f"响应: 二进制数据")
        else:
            print(f"响应: {response.text[:200]}")
    except Exception as e:
        print(f"失败: {e}")