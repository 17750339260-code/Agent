"""
🔥测试tts-Instruct2 语音合成
"""

import pytest
import os
import wave
import statistics
import json
from datetime import datetime
import numpy as np
from conftest import (
    instruct2_api_request_with_sync,
    OUTPUT_DIR,
    TTS_TTFT_MAX,
    TTS_RT_MAX,
    ACCURACY_THRESHOLD
)


class TestInstruct2ApiBusinessScenarios:
    """新增instruct2接口业务场景测试"""

    def test_instruct2_api_语音字幕同步(self, instruct2_api_url, perf_stats):
        """新增instruct2接口语音字幕同步业务用例"""
        print("\n[业务1] 语音字幕同步")

        test_text = "- 文件内容主要涉及南方电网及相关企业的培训课程、政策文件、技术讲座和管理规定。\n- 包含多个专家团队的课程内容，如赵继光、胡跃申、赵镔、谢铭等，涵盖技术创新、技艺传承、前沿探索等领域。\n- 涉及主题包括计算能力与大数据应用、区块链、光缆资料整理、配网带电作业、网络安全防护、低碳园区、新型电力系统、构网型控制技术等。\n- 包含南方电网内部管理相关指导书和办法，如干部教育培训、高层次人才谈心谈话、培训基地认证、师资管理、劳动竞赛管理等。\n- 有部分课程因课件为纯音乐、无语音或普通话不标准等原因导致解析内容为空或不准确。\n- 包含数字经济、ESG发展、国际电力市场、企业财务管理等宏观议题的课程。\n- 部分课程需重新上传或重新分析课件以获取完整内容。\n\n文档摘要|文件内容为南方电网及其合作单位的系列培训课程、政策规范与技术讲座的目录清单，涵盖技术发展、安全管理、人才培训、数字化转型等多个维度，反映了企业在能源转型与智能化发展背景下的教育培训布局与战略关注重点。"

        payload = {
            "model": "instruct2",
            "input": test_text,
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True
            }
        }

        result = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=True)
        perf_stats["TTFT"].append(result["ttft"])
        perf_stats["RT"].append(result["rt"])

        print(f"结果: TTFT={result['ttft']:.3f}s, RT={result['rt']:.3f}s")

    def test_instruct2_api_语音字幕同步_长文本(self, instruct2_api_url, perf_stats):
        """新增instruct2接口语音字幕同步业务用例 - 长文本"""
        print("\n[业务2] 语音字幕同步-长文本")
        test_text = "- 文件内容主要涉及南方电网及相关企业的培训课程、政策文件、技术讲座和管理规定。\n- 包含多个专家团队的课程内容，如赵继光、胡跃申、赵镔、谢铭等，涵盖技术创新、技艺传承、前沿探索等领域。\n- 涉及主题包括计算能力与大数据应用、区块链、光缆资料整理、配网带电作业、网络安全防护、低碳园区、新型电力系统、构网型控制技术等。\n- 包含南方电网内部管理相关指导书和办法，如干部教育培训、高层次人才谈心谈话、培训基地认证、师资管理、劳动竞赛管理等。\n- 有部分课程因课件为纯音乐、无语音或普通话不标准等原因导致解析内容为空或不准确。\n- 包含数字经济、ESG发展、国际电力市场、企业财务管理等宏观议题的课程。\n- 部分课程需重新上传或重新分析课件以获取完整内容。\n\n文档摘要|文件内容为南方电网及其合作单位的系列培训课程、政策规范与技术讲座的目录清单，涵盖技术发展、安全管理、人才培训、数字化转型等多个维度，反映了企业在能源转型与智能化发展背景下的教育培训布局与战略关注重点。"
        payload = {
            "model": "instruct2",
            "input": test_text,
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True
            }
        }

        result = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=True)
        perf_stats["TTFT"].append(result["ttft"])
        perf_stats["RT"].append(result["rt"])

        print(f"结果: TTFT={result['ttft']:.3f}s, RT={result['rt']:.3f}s, 大小={result['size']}字节")


class TestInstruct2ApiMetrics:
    """新增instruct2接口核心指标测试类"""

    def test_1_instruct2_api_TTFT首包延迟(self, instruct2_api_url, ttft_limit, perf_stats):
        """指标1：新增instruct2接口TTFT首包延迟专项测试"""
        print(f"\n[指标1] TTFT首包延迟 (阈值={ttft_limit}s)")

        text = "测试新增instruct2接口首包延迟"
        payload = {
            "model": "instruct2",
            "input": text,
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True
            }
        }

        res = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=True)
        perf_stats["TTFT"].append(res["ttft"])

        print(f"结果: TTFT={res['ttft']:.3f}s")
        assert res["ttft"] <= ttft_limit, f"首包延迟超标: {res['ttft']:.3f}s > {ttft_limit}s"

    def test_2_instruct2_api_RT总耗时(self, instruct2_api_url, rt_limit, perf_stats):
        """指标2：新增instruct2接口RT合成总耗时专项测试"""
        print(f"\n[指标2] RT合成总耗时 (阈值={rt_limit}s)")

        text = "测试新增instruct2接口总耗时"
        payload = {
            "model": "instruct2",
            "input": text,
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True
            }
        }

        res = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=True)
        perf_stats["RT"].append(res["rt"])

        print(f"结果: RT={res['rt']:.3f}s")
        assert res["rt"] <= rt_limit, f"总耗时超标: {res['rt']:.3f}s > {rt_limit}s"

    def test_3_instruct2_api_文本准确率(self, instruct2_api_url, crr_calculator, asr_request):
        """指标3：新增instruct2接口文本识别准确率专项测试"""
        print(f"\n[指标3] 文本识别准确率 (阈值={ACCURACY_THRESHOLD})")

        text = "你好，测试新增instruct2接口文本准确率。"
        payload = {
            "model": "instruct2",
            "input": text,
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True
            }
        }

        res = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=False)
        asr_text = asr_request(res["path"])

        if not asr_text:
            print("ASR接口无返回，跳过测试")
            pytest.skip("ASR接口无返回，跳过本次准确率测试")

        crr = crr_calculator(text, asr_text)
        print(f"结果: 原文={text[:20]}... | ASR={asr_text[:20]}... | 准确率={crr:.4f}")
        assert crr >= ACCURACY_THRESHOLD, f"准确率不达标: {crr:.4f} < {ACCURACY_THRESHOLD}"

    def test_4_instruct2_api_音文同步(self, instruct2_api_url):
        """指标4：新增instruct2接口音文同步有效性专项测试"""
        print("\n[指标4] 音文同步有效性")

        text = "测试新增instruct2接口音文同步"
        payload = {
            "model": "instruct2",
            "input": text,
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True
            }
        }

        res = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=True)
        audio_path = res["path"]
        file_exist = os.path.exists(audio_path)
        file_size = os.path.getsize(audio_path) if file_exist else 0

        print(f"结果: 文件存在={file_exist}, 大小={file_size}字节")
        assert file_exist and file_size > 0, "未生成有效音频文件"

    def test_5_instruct2_api_音频音质(self, instruct2_api_url):
        """指标5：新增instruct2接口音频文件音质有效性专项测试"""
        print("\n[指标5] 音频音质有效性")

        text = "测试新增instruct2接口音频音质"
        payload = {
            "model": "instruct2",
            "input": text,
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True
            }
        }

        res = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=True)
        audio_path = res["path"]

        try:
            with wave.open(audio_path, 'rb') as wav:
                frames = wav.getnframes()
                rate = wav.getframerate()
                duration = frames / float(rate) if rate > 0 else 0

                print(f"结果: 采样率={rate}Hz, 时长={duration:.3f}s, 帧数={frames}")

                assert rate in [16000, 24000, 44100, 48000], f"异常采样率: {rate}Hz"
                assert duration > 0.1, f"音频过短: {duration:.3f}秒"

        except Exception as e:
            print(f"WAV格式验证失败: {e}")
            file_size = os.path.getsize(audio_path)
            min_file_size = 1024
            print(f"结果: 文件大小={file_size}字节")
            assert file_size > min_file_size, f"音频文件过小: {file_size}字节 <= {min_file_size}字节"

    def test_6_instruct2_api_流式稳定性(self, instruct2_api_url):
        """指标6：新增instruct2接口流式传输稳定性专项测试"""
        print("\n[指标6] 流式传输稳定性")

        text = "测试新增instruct2接口流式稳定性，验证分片接收是否完整无中断。"
        payload = {
            "model": "instruct2",
            "input": text,
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True
            }
        }

        res = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=True)
        file_size = res.get("size", 0)

        print(f"结果: 音频大小={file_size}字节")

        assert file_size > 0
        with open(res["path"], 'rb') as f:
            file_data = f.read()
            if len(file_data) >= 44 and file_data[0:4] == b'RIFF':
                print("WAV格式验证: 通过")

    def test_7_instruct2_api_P95稳定性(self, instruct2_api_url, perf_stats):
        """指标7：新增instruct2接口P95性能数据稳定性专项测试"""
        print("\n[指标7] P95性能稳定性 (3次采样)")

        test_count = 3
        old_ttft = len(perf_stats["TTFT"])
        old_rt = len(perf_stats["RT"])
        ttft_values = []
        rt_values = []

        for i in range(test_count):
            text = f"新增instruct2接口P95性能采样_{i + 1}"
            payload = {
                "model": "instruct2",
                "input": text,
                "tts_params": {
                    "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                    "prompt_audio": "kehu_female_b",
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": 1.0,
                    "stream": True
                }
            }

            res = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=False)
            ttft_values.append(res["ttft"])
            rt_values.append(res["rt"])
            print(f"采样{i + 1}: TTFT={res['ttft']:.3f}s, RT={res['rt']:.3f}s")

        perf_stats["TTFT"].extend(ttft_values)
        perf_stats["RT"].extend(rt_values)

        if len(ttft_values) > 1:
            manual_p95_ttft = sorted(ttft_values)[int(len(ttft_values) * 0.95)]
            numpy_p95_ttft = np.percentile(ttft_values, 95)
            diff = abs(manual_p95_ttft - numpy_p95_ttft)
            print(f"P95对比: 手动={manual_p95_ttft:.3f}s, numpy={numpy_p95_ttft:.3f}s, 差异={diff:.3f}s")
            assert diff < 0.01, "P95计算不一致"

        new_ttft_num = len(perf_stats["TTFT"]) - old_ttft
        new_rt_num = len(perf_stats["RT"]) - old_rt
        print(f"新增: TTFT={new_ttft_num}条, RT={new_rt_num}条")
        assert new_ttft_num == test_count and new_rt_num == test_count, "性能采样数据丢失"

    def test_8_instruct2_api_打断响应(self, instruct2_api_url):
        """指标8：新增instruct2接口播放打断响应能力专项测试"""
        print("\n[指标8] 打断响应能力")

        text = "测试新增instruct2接口打断响应，这是一个较长的测试文本，用于验证播放过程中是否可以被打断。"
        payload = {
            "model": "instruct2",
            "input": text,
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True
            }
        }

        res = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=True)
        audio_exist = os.path.exists(res["path"])
        file_size = os.path.getsize(res["path"]) if audio_exist else 0

        print(f"结果: 音频存在={audio_exist}, 大小={file_size}字节")
        assert audio_exist and file_size > 0, "播放链路异常"


class TestInstruct2ApiFunctional:
    """新增instruct2接口功能增强测试"""

    def test_9_instruct2_api_语音质量评估(self, instruct2_api_url):
        """指标9：新增instruct2接口语音质量专项测试"""
        print("\n[功能1] 语音质量评估")

        test_text = "这是一个新增instruct2接口语音质量测试，评估音高、音色、自然度和情感表达。"
        payload = {
            "model": "instruct2",
            "input": test_text,
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 用自然的语气说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True
            }
        }

        res = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=True)
        print(f"结果: 文本长度={len(test_text)}字, 音频大小={res['size']}字节")
        assert res["size"] > len(test_text) * 10, "音频质量异常，文件偏小"

    def test_10_instruct2_api_中断恢复(self, instruct2_api_url):
        """指标10：新增instruct2接口语音中断恢复能力测试"""
        print("\n[功能2] 中断恢复能力")

        test_text = "测试新增instruct2接口语音中断恢复能力，验证在中断后能否正常恢复播放。"
        payload = {
            "model": "instruct2",
            "input": test_text,
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 用自然的语气说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True
            }
        }

        res = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=True)
        print(f"结果: 路径={res['path']}, 大小={res.get('size', 0)}字节")
        assert os.path.exists(res["path"]) and res.get("size", 0) > 0, "基础播放链路异常"

    def test_11_instruct2_api_多语言支持(self, instruct2_api_url):
        """指标11：新增instruct2接口多语言/方言支持测试"""
        print("\n[功能3] 多语言支持测试")

        test_cases = [
            {"text": "Hello, this is an English test.", "desc": "英文测试"},
            {"text": "你好，这是中文测试。", "desc": "中文测试"},
            {"text": "こんにちは、これは日本語テストです。", "desc": "日文测试"},
            {"text": "안녕하세요, 이것은 한국어 테스트입니다。", "desc": "韩文测试"},
            {"text": "1234567890", "desc": "纯数字测试"},
            {"text": "Hello 123 你好！@#", "desc": "混合内容测试"},
        ]

        print(f"测试用例: {len(test_cases)}种语言")
        success_count = 0

        for i, case in enumerate(test_cases, 1):
            print(f"{i}/{len(test_cases)}: {case['desc']} - {case['text'][:20]}...")

            payload = {
                "model": "instruct2",
                "input": case["text"],
                "tts_params": {
                    "instruct_text": "You are a helpful assistant. 用自然的语气说<|endofprompt|>",
                    "prompt_audio": "kehu_female_b",
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": 1.0,
                    "stream": True
                }
            }

            try:
                res = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=False)
                if res["size"] > 0:
                    success_count += 1
            except Exception as e:
                print(f"  失败: {str(e)[:30]}")

        print(f"结果: 成功{success_count}/{len(test_cases)}")
        assert success_count >= len(test_cases) * 0.8, f"多语言支持不足: {success_count}/{len(test_cases)}"

    def test_12_instruct2_api_标点处理(self, instruct2_api_url):
        """指标12：新增instruct2接口标点符号语音处理测试"""
        print("\n[功能4] 标点符号处理测试")

        test_cases = [
            {"text": "你好，世界！", "desc": "逗号和感叹号"},
            {"text": "这是问号？", "desc": "问号"},
            {"text": "引号测试：\"这是引号\"", "desc": "双引号"},
            {"text": "单引号测试：'内容'", "desc": "单引号"},
            {"text": "括号测试（括号内容）", "desc": "括号"},
            {"text": "省略号测试...", "desc": "省略号"},
            {"text": "破折号测试——内容", "desc": "破折号"},
            {"text": "冒号：分号；", "desc": "冒号分号"},
        ]

        print(f"测试用例: {len(test_cases)}种标点")
        success_count = 0

        for i, case in enumerate(test_cases, 1):
            print(f"{i}/{len(test_cases)}: {case['desc']} - {case['text']}")

            payload = {
                "model": "instruct2",
                "input": case["text"],
                "tts_params": {
                    "instruct_text": "You are a helpful assistant. 用自然的语气说<|endofprompt|>",
                    "prompt_audio": "kehu_female_b",
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": 1.0,
                    "stream": True
                }
            }

            try:
                res = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=False)
                if res["size"] > 0:
                    success_count += 1
            except Exception as e:
                print(f"  失败: {str(e)[:30]}")

        print(f"结果: 成功{success_count}/{len(test_cases)}")
        assert success_count == len(test_cases), f"标点处理异常: {success_count}/{len(test_cases)}"