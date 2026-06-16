# conftest.py
import pytest
import requests
import urllib3
import pyaudio
import time
import os
import wave
import io
import logging
from collections import defaultdict
from jiwer import cer
import base64
import librosa
import numpy as np
# 禁用SSL警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ====================== 配置常量 ======================

# 接口地址配置
TTS_GATE_URL = "http://36.111.82.53:10014/v1/audio/speech"
ZERO_SHOT_TTS_URL = "http://117.68.66.99:10014/api/tts/zero-shot"
INSTRUCT2_API_URL = "http://36.111.82.53:10014/api/tts/instruct2"
ASR_HTTP_URL = "http://117.68.66.99:10017/v1/audio/trans"

# 性能阈值配置
TTS_TTFT_MAX = 0.35
TTS_RT_MAX = 2.5
ACCURACY_THRESHOLD = 0.85

# 音频参数
SAMPLE_RATE = 24000
CHANNELS = 1
AUDIO_FORMAT = pyaudio.paInt16
MIN_FILE_SIZE = 1024

# 输出目录
OUTPUT_DIR = "audio_output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 并发测试配置
CONCURRENT_WORKERS = 5
CONCURRENT_REQUESTS = 20
STRESS_TEST_COUNT = 10

# 测试模式
ENABLE_PLAYBACK = True
ENABLE_SUBTITLE = True

# ====================== 简化日志配置 ======================
# 初始化根日志器（关键！）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    encoding="utf-8"
)
logger = logging.getLogger(__name__)
# 只禁用第三方库的详细日志
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("requests").setLevel(logging.ERROR)
logging.getLogger("pyaudio").setLevel(logging.ERROR)


# 控制pytest输出格式
def pytest_configure(config):
    """配置pytest输出格式"""
    config.option.verbose = 0
    config.option.no_header = True
    config.option.no_summary = False


# ====================== Fixtures ======================

@pytest.fixture(scope="session")
def perf_stats():
    """全局性能统计"""
    stats = defaultdict(list)
    yield stats

    # 测试结束后生成最终报告
    if stats["HEADER"] or stats["TTFB"] or stats["TTFT"] or stats["RT"]:
        print("\n" + "=" * 60)
        print("📊 TTS自动化验收-P95最终报告")
        if stats["HEADER"]:
            header_p95 = np.percentile(stats["HEADER"], 95)
            print(f"HTTP响应头返回 P95: {header_p95:.3f} s")
        if stats["TTFB"]:
            ttfb_p95 = np.percentile(stats["TTFB"], 95)
            print(f"TTFB首个响应体分片 P95: {ttfb_p95:.3f} s")
        if stats["TTFT"]:
            ttft_p95 = np.percentile(stats["TTFT"], 95)
            print(f"首个音频包到达 P95：{ttft_p95:.3f} s (阈值={TTS_TTFT_MAX}s)")
        if stats["RT"]:
            rt_p95 = np.percentile(stats["RT"], 95)
            print(f"RT合成总耗时 P95：{rt_p95:.3f} s (阈值={TTS_RT_MAX}s)")
        print("=" * 50)

@pytest.fixture(scope="session")
def audio_quality_analyzer():
    """音频质量分析fixture"""
    return analyze_audio_quality

@pytest.fixture(scope="session")
def asr_request():
    """ASR识别请求fixture - 返回识别文本"""

    def _asr(audio_path, model="funasr-nano", input_type="stream", language="zh"):
        try:
            # 读取音频文件并转换为base64
            with open(audio_path, 'rb') as f:
                audio_bytes = f.read()
                audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')

            # 构造符合API文档的请求体
            payload = {
                "model": model,
                "input_type": input_type,
                "input": audio_b64,  # Base64编码的音频数据
                "hotwords": "",
                "speaker_diarization": False,
                "language": language,
                "is_return_timestamp": False
            }

            headers = {
                "accept": "application/json",
                "Content-Type": "application/json"
            }

            res = requests.post(
                ASR_HTTP_URL,
                json=payload,
                headers=headers,
                verify=False,
                timeout=10
            )

            if res.status_code == 200:
                return res.json().get("text", "")
            else:
                return f"ERROR: HTTP {res.status_code}"
        except Exception as e:
            return f"ERROR: {str(e)}"

    return _asr


@pytest.fixture(scope="session")
def asr_request_with_metrics():
    """ASR识别请求fixture - 返回包含性能指标的完整结果"""

    def _asr_with_metrics(audio_path, expected_text=None, model="funasr-nano", input_type="stream", language="zh"):
        import time
        from jiwer import cer

        result = {
            "success": False,
            "response_time": 0.0,
            "recognized_text": "",
            "accuracy": 0.0,
            "error": None
        }

        start_time = time.time()
        try:
            # 读取音频文件并转换为base64
            with open(audio_path, 'rb') as f:
                audio_bytes = f.read()
                audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')

            # 构造符合API文档的请求体
            payload = {
                "model": model,
                "input_type": input_type,
                "input": audio_b64,
                "hotwords": "",
                "speaker_diarization": False,
                "language": language,
                "is_return_timestamp": False
            }

            headers = {
                "accept": "application/json",
                "Content-Type": "application/json"
            }

            res = requests.post(
                ASR_HTTP_URL,
                json=payload,
                headers=headers,
                verify=False,
                timeout=10
            )

            response_time = time.time() - start_time
            result["response_time"] = response_time

            if res.status_code == 200:
                json_resp = res.json()
                recognized_text = json_resp.get("text", "")
                result["success"] = True
                result["recognized_text"] = recognized_text

                # 计算准确率
                if expected_text is not None and expected_text != "":
                    try:
                        error_rate = cer(expected_text, recognized_text)
                        accuracy = 1 - error_rate
                        result["accuracy"] = accuracy
                    except:
                        result["accuracy"] = 0.0
            else:
                result["error"] = f"HTTP {res.status_code}: {res.text}"

        except Exception as e:
            result["response_time"] = time.time() - start_time
            result["error"] = str(e)

        return result

    return _asr_with_metrics
@pytest.fixture(scope="session")
def crr_calculator():
    """文本准确率计算"""

    def _calc(ref, hypo):
        if not hypo:
            return 0.0
        try:
            error_rate = cer(ref, hypo)
            return round(1 - error_rate, 4)
        except Exception as e:
            return 0.0

    return _calc


@pytest.fixture
def tts_url():
    """TTS接口URL"""
    return TTS_GATE_URL


@pytest.fixture
def zero_shot_tts_url():
    """零样本TTS接口URL"""
    return ZERO_SHOT_TTS_URL


@pytest.fixture
def instruct2_api_url():
    """新增instruct2接口URL"""
    return INSTRUCT2_API_URL


@pytest.fixture
def ttft_limit():
    """TTFT阈值"""
    return TTS_TTFT_MAX


@pytest.fixture
def rt_limit():
    """RT阈值"""
    return TTS_RT_MAX


# ====================== 工具函数 ======================

def calculate_audio_duration_fixed(audio_bytes):
    """计算音频时长"""
    try:
        if len(audio_bytes) < 44:
            return 0.0

        if audio_bytes[0:4] != b'RIFF':
            return 0.0

        if len(audio_bytes) < 12 or audio_bytes[8:12] != b'WAVE':
            return 0.0

        # 使用wave模块计算
        try:
            with io.BytesIO(audio_bytes) as f:
                with wave.open(f, 'rb') as wav:
                    frames = wav.getnframes()
                    rate = wav.getframerate()
                    if rate > 0 and frames > 0:
                        return frames / float(rate)
        except:
            pass

        # 手动解析WAV头
        return calculate_duration_manually_fixed(audio_bytes)

    except Exception:
        return 0.0


def calculate_duration_manually_fixed(audio_bytes):
    """手动计算音频时长"""
    try:
        if len(audio_bytes) < 44:
            return 0.0

        # 查找fmt块
        fmt_start = audio_bytes.find(b'fmt ')
        if fmt_start == -1:
            return 0.0

        # 解析音频参数
        sample_rate = int.from_bytes(audio_bytes[fmt_start + 12:fmt_start + 16], 'little')
        channels = int.from_bytes(audio_bytes[fmt_start + 10:fmt_start + 12], 'little')
        bits_per_sample = int.from_bytes(audio_bytes[fmt_start + 22:fmt_start + 24], 'little')

        # 查找data块
        data_start = audio_bytes.find(b'data')
        if data_start == -1:
            return 0.0

        # 数据大小
        data_size = int.from_bytes(audio_bytes[data_start + 4:data_start + 8], 'little')

        # 计算时长
        if sample_rate > 0 and data_size > 0 and channels > 0 and bits_per_sample > 0:
            bytes_per_sample = bits_per_sample // 8
            duration = data_size / (sample_rate * channels * bytes_per_sample)

            if duration > 3600:  # 超过1小时
                return calculate_duration_from_data_size(audio_bytes, data_size, sample_rate)
            else:
                return duration

    except Exception as e:
        pass

    return calculate_duration_from_data_size(audio_bytes, len(audio_bytes), 24000)


def calculate_duration_from_data_size(audio_bytes, data_size, sample_rate=24000):
    """基于数据大小估算时长"""
    try:
        bytes_per_second = sample_rate * 2
        if bytes_per_second > 0:
            duration = data_size / bytes_per_second
            duration = max(0.1, min(duration, 300))
            return duration
    except Exception:
        pass
    return 0.0


def extract_pure_audio(audio_bytes):
    """提取纯音频数据，跳过WAV头"""
    try:
        data_start = audio_bytes.find(b'data')
        if data_start != -1 and data_start + 8 < len(audio_bytes):
            audio_start = data_start + 8
            pure_audio = audio_bytes[audio_start:]
            return pure_audio
    except Exception:
        pass

    if len(audio_bytes) > 44:
        return audio_bytes[44:]
    return audio_bytes


def play_audio_simple(audio_bytes, sample_rate=24000):
    """简单播放音频"""
    p = None
    stream = None

    try:
        pure_audio = extract_pure_audio(audio_bytes)
        if len(pure_audio) == 0:
            return 0.0

        p = pyaudio.PyAudio()
        stream = p.open(
            format=AUDIO_FORMAT,
            channels=CHANNELS,
            rate=sample_rate,
            output=True
        )

        start_time = time.time()
        stream.write(pure_audio)
        actual_duration = time.time() - start_time
        return actual_duration

    except Exception:
        return 0.0
    finally:
        if stream:
            stream.stop_stream()
            stream.close()
        if p:
            p.terminate()


def show_subtitle_with_duration(text, audio_duration):
    """根据音频时长显示字幕 - 精简版"""
    if not text or audio_duration <= 0:
        return

    if audio_duration <= 0 or audio_duration > 300:
        char_delay = 0.12
        estimated_duration = len(text) * char_delay

        time.sleep(0.5)
        for char in text:
            print(char, end="", flush=True)
            time.sleep(char_delay)
        print()
        return

    effective_duration = max(0.5, audio_duration - 0.5)
    char_delay = effective_duration / len(text) if len(text) > 0 else 0.12
    char_delay = max(0.08, min(char_delay, 0.25))

    time.sleep(0.5)
    for char in text:
        print(char, end="", flush=True)
        time.sleep(char_delay)
    print()


def validate_audio_data(audio_bytes):
    """验证音频数据有效性"""
    if len(audio_bytes) < 44:
        return False, "音频数据过短"

    if audio_bytes[0:4] != b'RIFF':
        return False, "无效的RIFF头"

    if len(audio_bytes) < 12 or audio_bytes[8:12] != b'WAVE':
        return False, "不是WAVE格式"

    data_start = audio_bytes.find(b'data')
    if data_start == -1:
        return False, "未找到data块"

    return True, "音频数据有效"


# ====================== 核心TTS函数 ======================

def tts_request_with_sync(url, payload, enable_playback=True):
    """
    执行TTS请求并同步播放
    返回性能指标：
    - response_header_time: HTTP响应头返回时间
    - ttfb: 首个响应体分片到达时间
    - ttft: 兼容旧字段，等同于首个音频包到达时间
    - ttfa: 首段可播放音频数据到达时间（跳过WAV头）
    - rt: 响应体最后一个分片到达时间
    """
    timestamp = int(time.time() * 1000)
    model_name = payload.get("model", "unknown")
    audio_path = os.path.join(OUTPUT_DIR, f"{model_name}_{timestamp}.wav")

    start_time = time.time()
    response_header_time = None
    first_chunk_time = None
    first_playable_audio_time = None
    last_chunk_time = None
    audio_data = b""

    try:
        headers = {"accept": "application/json", "Content-Type": "application/json"}

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            stream=True,
            verify=False,
            timeout=30
        )
        response_header_time = round(time.time() - start_time, 3)

        if response.status_code != 200:
            raise RuntimeError(f"TTS接口失败: {response.status_code}")

        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                current_time = time.time()
                if first_chunk_time is None:
                    first_chunk_time = current_time
                if first_playable_audio_time is None and len(audio_data) + len(chunk) > 44:
                    first_playable_audio_time = current_time
                last_chunk_time = current_time
                audio_data += chunk

        ttfb = round(first_chunk_time - start_time, 3) if first_chunk_time else 0.0
        ttft = ttfb
        ttfa = round(first_playable_audio_time - start_time, 3) if first_playable_audio_time else 0.0
        rt = round(last_chunk_time - start_time, 3) if last_chunk_time else 0.0

        is_valid, msg = validate_audio_data(audio_data)
        if not is_valid:
            raise RuntimeError(f"音频数据无效: {msg}")

        with open(audio_path, "wb") as f:
            f.write(audio_data)

        if enable_playback and ENABLE_PLAYBACK and audio_data:
            try:
                test_text = payload.get("input", "")
                audio_duration = calculate_audio_duration_fixed(audio_data)

                if audio_duration <= 0:
                    audio_duration = len(audio_data) / (24000 * 2)
                    audio_duration = max(0.5, min(audio_duration, 300))

                if ENABLE_SUBTITLE:
                    show_subtitle_with_duration(test_text, audio_duration)

                play_audio_simple(audio_data, SAMPLE_RATE)

            except Exception:
                pass  # 静默处理播放错误

        return {
            "response_header_time": response_header_time,
            "first_body_chunk_time": ttfb,
            "first_audio_chunk_time": ttft,
            "first_playable_audio_time": ttfa,
            "ttfb": ttfb,
            "ttft": ttft,
            "ttfa": ttfa,
            "rt": rt,
            "path": audio_path,
            "size": len(audio_data),
            "success": True
        }

    except Exception as e:
        if 'audio_path' in locals() and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except:
                pass
        raise RuntimeError(f"TTS请求失败: {e}")


def instruct2_api_request_with_sync(url, payload, enable_playback=True):
    """
    执行新增instruct2接口请求并同步播放
    返回性能指标，字段含义与 tts_request_with_sync 一致
    """
    timestamp = int(time.time() * 1000)
    audio_path = os.path.join(OUTPUT_DIR, f"instruct2_api_{timestamp}.wav")

    start_time = time.time()
    response_header_time = None
    first_chunk_time = None
    first_playable_audio_time = None
    last_chunk_time = None
    audio_data = b""

    try:
        headers = {"accept": "application/json", "Content-Type": "application/json"}

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            stream=True,
            verify=False,
            timeout=30
        )
        response_header_time = round(time.time() - start_time, 3)

        if response.status_code != 200:
            raise RuntimeError(f"新增instruct2接口失败: {response.status_code}")

        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                current_time = time.time()
                if first_chunk_time is None:
                    first_chunk_time = current_time
                if first_playable_audio_time is None and len(audio_data) + len(chunk) > 44:
                    first_playable_audio_time = current_time
                last_chunk_time = current_time
                audio_data += chunk

        ttfb = round(first_chunk_time - start_time, 3) if first_chunk_time else 0.0
        ttft = ttfb
        ttfa = round(first_playable_audio_time - start_time, 3) if first_playable_audio_time else 0.0
        rt = round(last_chunk_time - start_time, 3) if last_chunk_time else 0.0

        is_valid, msg = validate_audio_data(audio_data)
        if not is_valid:
            raise RuntimeError(f"音频数据无效: {msg}")

        with open(audio_path, "wb") as f:
            f.write(audio_data)

        if enable_playback and ENABLE_PLAYBACK and audio_data:
            try:
                test_text = payload.get("input", "")
                audio_duration = calculate_audio_duration_fixed(audio_data)

                if audio_duration <= 0:
                    audio_duration = len(audio_data) / (24000 * 2)
                    audio_duration = max(0.5, min(audio_duration, 300))

                if ENABLE_SUBTITLE:
                    show_subtitle_with_duration(test_text, audio_duration)

                play_audio_simple(audio_data, SAMPLE_RATE)

            except Exception:
                pass  # 静默处理播放错误

        return {
            "response_header_time": response_header_time,
            "first_body_chunk_time": ttfb,
            "first_audio_chunk_time": ttft,
            "first_playable_audio_time": ttfa,
            "ttfb": ttfb,
            "ttft": ttft,
            "ttfa": ttfa,
            "rt": rt,
            "path": audio_path,
            "size": len(audio_data),
            "success": True
        }

    except Exception as e:
        if 'audio_path' in locals() and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except:
                pass

        raise RuntimeError(f"新增instruct2接口请求失败: {e}")


# 🔧 兼容性：保留原有函数名
calculate_audio_duration = calculate_audio_duration_fixed
play_audio = play_audio_simple


# ====================== 额外的fixtures ======================

@pytest.fixture
def test_case_counter():
    """测试用例计数器"""
    return {"count": 0}


@pytest.fixture(autouse=True)
def increment_counter(test_case_counter):
    """自动递增测试用例计数器"""
    test_case_counter["count"] += 1
    yield
    test_case_counter["count"] -= 1


@pytest.fixture(scope="session")
def test_config():
    """测试配置"""
    return {
        "tts_url": TTS_GATE_URL,
        "zero_shot_tts_url": ZERO_SHOT_TTS_URL,
        "instruct2_api_url": INSTRUCT2_API_URL,
        "asr_url": ASR_HTTP_URL,
        "concurrent_workers": CONCURRENT_WORKERS,
        "concurrent_requests": CONCURRENT_REQUESTS,
        "stress_test_count": STRESS_TEST_COUNT,
        "output_dir": OUTPUT_DIR
    }


# ====================== pytest钩子 ======================

def pytest_addoption(parser):
    """添加命令行选项"""
    parser.addoption("--enable-playback", action="store_true", default=True,
                     help="启用音频播放")
    parser.addoption("--enable-subtitle", action="store_true", default=True,
                     help="启用字幕显示")


def pytest_configure(config):
    """配置全局设置"""
    global ENABLE_PLAYBACK, ENABLE_SUBTITLE
    ENABLE_PLAYBACK = config.getoption("--enable-playback")
    ENABLE_SUBTITLE = config.getoption("--enable-subtitle")


# 简化pytest的输出格式
def pytest_report_teststatus(report):
    """简化测试状态报告"""
    if report.when == 'call':
        if report.failed:
            return ("FAIL", "R", "FAILED")
        elif report.skipped:
            return ("SKIP", "S", "SKIPPED")
        else:
            return ("PASS", ".", "PASSED")


# 设置pytest的选项
def pytest_sessionstart(session):
    """会话开始时的设置"""
    session.config.option.tbstyle = "no"
    session.config.option.verbose = 0
    session.config.option.quiet = False


# 在测试结束时显示摘要
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """终端摘要"""
    if exitstatus == 0:
        print("\n✅ 所有测试通过！")
    else:
        print(f"\n❌ 测试失败: exit code {exitstatus}")


def analyze_audio_quality(audio_path):
    """
    音频质量客观分析
    返回信噪比、谐噪比、静音检测等指标
    """
    try:
        # 使用librosa加载音频
        y, sr = librosa.load(audio_path, sr=None)

        if len(y) < sr * 0.1:  # 至少0.1秒
            return {"error": "音频过短"}

        results = {
            "sample_rate": sr,
            "duration": len(y) / sr,
            "success": True
        }

        # 1. 计算信号能量
        energy = np.sum(y ** 2) / len(y)
        results["energy"] = float(energy)

        # 2. 估计信噪比（简化版）
        # 将信号分为静音段和语音段（简化：前5%为静音段）
        silent_samples = int(len(y) * 0.05)
        if silent_samples > 0:
            silent_part = y[:silent_samples]
            signal_part = y[silent_samples:]

            if len(silent_part) > 0 and len(signal_part) > 0:
                noise_power = np.mean(silent_part ** 2)
                signal_power = np.mean(signal_part ** 2)

                if noise_power > 0:
                    snr = 10 * np.log10(signal_power / noise_power)
                    results["snr_db"] = float(snr)
                else:
                    results["snr_db"] = float("inf")

        # 3. 检测爆破音（过零率异常）
        zero_crossings = librosa.zero_crossings(y, pad=False)
        zcr = np.mean(zero_crossings)
        results["zero_crossing_rate"] = float(zcr)

        # 4. 基频范围（粗略估计）
        try:
            f0, voiced_flag, voiced_probs = librosa.pyin(
                y,
                fmin=librosa.note_to_hz('C2'),
                fmax=librosa.note_to_hz('C7'),
                sr=sr
            )
            f0_values = f0[~np.isnan(f0)]
            if len(f0_values) > 0:
                results["f0_min"] = float(np.min(f0_values))
                results["f0_max"] = float(np.max(f0_values))
                results["f0_mean"] = float(np.mean(f0_values))
        except:
            pass

        return results

    except Exception as e:
        return {"error": str(e), "success": False}
