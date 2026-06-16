"""
新增instruct2接口 - 专项性能与压力测试脚本
目标：测试接口在高并发、持续负载、异常情况下的性能表现与稳定性。
"""
import numpy as np
import pytest
import os
import time
import threading
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Tuple
from conftest import (
    instruct2_api_request_with_sync,
    OUTPUT_DIR,
    TTS_TTFT_MAX,
    TTS_RT_MAX
)
import random


class TestInstruct2ApiPerformance:
    """instruct2接口性能与压力测试类"""

    def test_perf_single_request_baseline(self, instruct2_api_url):
        """性能基线测试：单请求常规文本"""
        print(f"\n[性能基线] 单请求常规文本")
        test_text = "这是一个用于性能基线测试的标准文本。"
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
        start = time.time()
        result = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=False)
        elapsed = time.time() - start

        print(
            f"结果: TTFT={result['ttft']:.3f}s, RT={result['rt']:.3f}s, 总耗时={elapsed:.3f}s, 大小={result.get('size', 0)}字节")
        # 基线断言：单请求性能应在合理范围内
        assert result["ttft"] < 2.0, f"单请求TTFT异常偏高: {result['ttft']:.3f}s"
        assert result["rt"] < 5.0, f"单请求RT异常偏高: {result['rt']:.3f}s"

    def test_perf_concurrent_requests(self, instruct2_api_url):
        """压力测试：低并发（例如5个并发用户）"""
        print(f"\n[压力测试] 低并发请求 (5个用户)")
        concurrent_users = 5
        test_texts = [
            f"并发用户{i}的测试文本，验证系统同时处理多个请求的能力。" for i in range(concurrent_users)
        ]

        all_results = []
        errors = []

        def _single_request(idx: int, text: str) -> Tuple[int, Dict]:
            """单个并发请求任务"""
            try:
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
                result = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=False)
                return idx, result
            except Exception as e:
                return idx, {"error": str(e)}

        start_time = time.time()
        with ThreadPoolExecutor(max_workers=concurrent_users) as executor:
            future_to_idx = {executor.submit(_single_request, idx, text): idx for idx, text in enumerate(test_texts)}
            for future in as_completed(future_to_idx):
                idx, res = future.result()
                if "error" in res:
                    errors.append((idx, res["error"]))
                else:
                    all_results.append(res)

        total_time = time.time() - start_time

        if all_results:
            ttfts = [r["ttft"] for r in all_results]
            rts = [r["rt"] for r in all_results]
            print(f"完成: 成功{len(all_results)}/{concurrent_users}, 总耗时{total_time:.2f}s")
            print(
                f"TTFT - 平均: {statistics.mean(ttfts):.3f}s, 最大: {max(ttfts):.3f}s, P95: {np.percentile(ttfts, 95):.3f}s")
            print(
                f"RT   - 平均: {statistics.mean(rts):.3f}s, 最大: {max(rts):.3f}s, P95: {np.percentile(rts, 95):.3f}s")

            # 断言：并发下P95延迟不应超过单点阈值的2倍（可根据实际情况调整）
            assert statistics.mean(ttfts) < TTS_TTFT_MAX * 2, f"并发平均TTFT {statistics.mean(ttfts):.3f}s 超标"
            assert statistics.mean(rts) < TTS_RT_MAX * 2, f"并发平均RT {statistics.mean(rts):.3f}s 超标"
        if errors:
            print(f"发生错误: {errors}")
            # 允许少量失败，取决于业务容错要求
            assert len(errors) <= concurrent_users * 0.2, f"失败率过高: {len(errors)}/{concurrent_users}"

    def test_perf_mixed_text_length(self, instruct2_api_url):
        """性能测试：混合不同长度的文本请求（短/中/长）"""
        print(f"\n[混合负载] 不同文本长度混合请求")

        text_variants = [
            ("短文本", "你好。"),
            ("中文本", "这是一个中等长度的测试句子，用于模拟常见的查询或指令。"),
            ("长文本", "这是一段较长的文本，旨在测试系统处理长内容时的性能表现。"
                       "长文本可能包含多个句子，甚至段落，这对语音合成的连贯性和缓存机制都是考验。"
                       "我们将观察其响应时间和资源使用情况。" * 2)
        ]

        results = []
        for desc, text in text_variants:
            print(f"  处理: {desc} ({len(text)}字)...")
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
            result = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=False)
            result['desc'] = desc
            result['text_len'] = len(text)
            results.append(result)

        # 输出分析
        print("\n混合长度测试结果汇总:")
        for r in results:
            print(
                f"  {r['desc']}({r['text_len']}字): TTFT={r['ttft']:.3f}s, RT={r['rt']:.3f}s, RT/字={(r['rt'] / r['text_len'] if r['text_len'] > 0 else 0):.4f}s")

        # 断言：长文本的RT虽然更长，但单位字的处理时间不应指数增长
        long_res = [r for r in results if r['desc'] == '长文本'][0]
        short_res = [r for r in results if r['desc'] == '短文本'][0]
        avg_time_per_char_long = long_res['rt'] / long_res['text_len']
        avg_time_per_char_short = short_res['rt'] / short_res['text_len']
        # 允许长文本单位耗时稍高，但不超过短文本的3倍（经验值）
        assert avg_time_per_char_long < avg_time_per_char_short * 3, f"长文本处理效率过低: 长文{avg_time_per_char_long:.4f}s/字 > 短文{avg_time_per_char_short:.4f}s/字的3倍"

    def test_perf_sustained_load(self, instruct2_api_url):
        """稳定性测试：持续负载（例如连续发送20个请求，观察性能衰减）"""
        print(f"\n[稳定性测试] 持续负载 (20个连续请求)")
        request_count = 20
        test_text = "稳定性测试连续请求文本，序列号: {seq}。"

        ttfts, rts = [], []
        error_count = 0

        for i in range(request_count):
            current_text = test_text.format(seq=i + 1)
            payload = {
                "model": "instruct2",
                "input": current_text,
                "tts_params": {
                    "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                    "prompt_audio": "kehu_female_b",
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": 1.0,
                    "stream": True
                }
            }
            try:
                result = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=False)
                ttfts.append(result["ttft"])
                rts.append(result["rt"])
                if (i + 1) % 5 == 0:
                    print(f"  已完成 {i + 1}/{request_count} 个请求")
            except Exception as e:
                print(f"  第{i + 1}个请求失败: {e}")
                error_count += 1
            time.sleep(0.5)  # 轻微间隔，模拟真实场景

        print(f"\n稳定性测试完成: 成功{request_count - error_count}/{request_count}")
        if ttfts:
            # 将请求序列分为前半段和后半段，对比性能
            half = len(ttfts) // 2
            first_half_avg_ttft = statistics.mean(ttfts[:half])
            second_half_avg_ttft = statistics.mean(ttfts[half:])
            first_half_avg_rt = statistics.mean(rts[:half])
            second_half_avg_rt = statistics.mean(rts[half:])

            print(f"TTFT - 前半段平均: {first_half_avg_ttft:.3f}s, 后半段平均: {second_half_avg_ttft:.3f}s")
            print(f"RT   - 前半段平均: {first_half_avg_rt:.3f}s, 后半段平均: {second_half_avg_rt:.3f}s")

            # 断言：后半段性能不应比前半段差太多（例如不超过50%）
            assert second_half_avg_ttft < first_half_avg_ttft * 1.5, f"系统可能疲劳，TTFT性能下降超过50%"
            assert second_half_avg_rt < first_half_avg_rt * 1.5, f"系统可能疲劳，RT性能下降超过50%"

        assert error_count < request_count * 0.1, f"持续负载失败率过高: {error_count}/{request_count}"

    def test_perf_varying_speed(self, instruct2_api_url):
        """性能测试：不同语速参数 (speed) 对性能的影响"""
        print(f"\n[参数测试] 不同语速(speed)下的性能")
        test_text = "测试不同语速下的合成性能，观察响应时间的变化。"
        speed_factors = [0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 2.0]

        results = []
        for speed in speed_factors:
            payload = {
                "model": "instruct2",
                "input": test_text,
                "tts_params": {
                    "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                    "prompt_audio": "kehu_female_b",
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": speed,
                    "stream": True
                }
            }
            result = instruct2_api_request_with_sync(instruct2_api_url, payload, enable_playback=False)
            result['speed'] = speed
            results.append(result)
            print(f"  speed={speed}: TTFT={result['ttft']:.3f}s, RT={result['rt']:.3f}s")
            time.sleep(0.2)  # 请求间短暂间隔

        # 简单分析：RT应大致与语速成反比（语速越快，音频越短，RT可能略短）
        print("\n语速影响分析:")
        for r in results:
            print(f"  speed={r['speed']}: RT={r['rt']:.3f}s")
        # 此处可根据需要添加更详细的断言，例如验证极端语速下的RT仍在可接受范围内
        for r in results:
            assert r['rt'] < 10.0, f"语速 {r['speed']} 时RT异常高: {r['rt']:.3f}s"