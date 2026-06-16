# enterprise_tts_stability_parallel_with_asr_cn.py
# 企业级 TTS 高并发压测 + ASR 自动检测“乱读”系统（中文注释版）
#
# 功能：
# 1. 多线程并发请求 TTS，流式保存音频
# 2. TTS 成功后自动 ASR 转写并与原文比对
# 3. 相似度低于阈值标记乱读，写入 CSV
# 4. 滑动窗口统计：P95(TTS_RT/TTFT/RTF)、窗口成功率、窗口乱读率（仅 ASR 成功样本）
# 5. TTS 耗时与 ASR 耗时分列，RTF 仅按 TTS 耗时计算
# 6. ASR 异步提交，不阻塞 TTS 线程，保证实际打到 TTS 服务的并发

import os
import re
import csv
import io
import glob
import time
import queue
import random
import signal
import math
import base64
import psutil
import argparse
import urllib3
import requests
import traceback
import wave
import difflib
from threading import Lock, Thread, BoundedSemaphore
from collections import deque
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait
from requests.adapters import HTTPAdapter
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# =========================================================
# 一、全局配置
# =========================================================
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# TTS 服务地址
TTS_URL = "http://117.68.66.99:10014/v1/audio/speech"
OUTPUT_DIR = "tts_output"              # 合成音频保存目录
LOG_FILE = "tts_stability_asr_log.csv" # CSV 明细日志路径

# CSV 表头（与 format_csv_row 写入字段顺序一致）
LOG_HEADER = (
    "任务ID,成功,模型,TTFT,TTS_RT,ASR耗时,总RT,音频秒数,TTS_RTF,字数,文件KB,内存MB,CPU%,"
    "乱读,ASR状态,相似度,ASR文本,时间,失败原因\n"
)

# ASR 服务与乱读判定
ASR_URL = "http://36.111.82.53:10017/v1/audio/trans"
ASR_MODEL = "funasr-iic"
ASR_CONCURRENCY = 2                  # ASR 线程池大小（与 ASR_SEMAPHORE 一致）
ASR_TIMEOUT = (5, 30)                # ASR 请求超时：(连接秒, 读取秒)
ASR_MAX_ATTEMPTS = 2                 # ASR 最多尝试次数（含首次）
ASR_SEMAPHORE_TIMEOUT = 10           # 等待 ASR 并发配额的最长时间（秒）
ASR_TEXT_SIMILARITY_THRESHOLD = 0.75 # 原文与转写相似度低于此值视为乱读

# TTS 并发与任务规模
CONCURRENCY = 20                     # TTS 线程池并发数
MAX_QUEUE_TASKS = 8                  # 允许排队的额外任务数（信号量 = CONCURRENCY + MAX_QUEUE_TASKS）
TOTAL_TASKS = 10000000000000000      # 理论最大任务数（实际靠 Ctrl+C 停止）

# 各类超时（秒）
API_QUEUE_TIMEOUT = 60               # 等待进入 TTS 执行队列
TASK_SUBMIT_TIMEOUT = 2100           # 单次 TTS 任务最长执行时间
MODEL_ACQUIRE_TIMEOUT = 1200         # 等待模型实例池令牌
CONNECT_TIMEOUT = 5                  # HTTP 连接超时
READ_TIMEOUT = 2100                  # HTTP 整体读取超时
CHUNK_TIMEOUT = 120                  # 流式接收相邻 chunk 最大间隔

# MODEL_POOL_SIZE = 12
MODEL_POOL_SIZE = 8
# 模型实例池大小（限制同时打 TTS 的深度）
KEEP_WAV_FILES = 30                  # 磁盘上最多保留的 wav 数量
KEEP_LOG_LINES = 2000                # 日志文件最多保留的数据行数
METRICS_WINDOW_SECONDS = 3600        # 滑动窗口统计时长（P95、成功率、乱读率）

# 音频参数（用于 WAV 头无效时按文件大小估算时长）
DEFAULT_AUDIO_SAMPLE_RATE = 24000
DEFAULT_AUDIO_CHANNELS = 1
DEFAULT_AUDIO_SAMPLE_WIDTH = 2       # 字节/采样（16bit = 2）
MIN_VALID_SAMPLE_RATE = 8000
MAX_VALID_SAMPLE_RATE = 192000
MAX_REASONABLE_AUDIO_SECONDS = 7200  # 单段音频最长合理时长

NORMAL_SLEEP_MIN = 0                 # 提交任务间隔下限（秒）
NORMAL_SLEEP_MAX = 0.03              # 提交任务间隔上限（秒）
ALERT_CONTINUOUS_FAIL = 10           # 连续失败达到此次数触发告警
NET_RETRY_TIMES = 2                  # TTS 网络类异常重试次数
LOG_FLUSH_INTERVAL = 1               # 日志落盘线程轮询间隔（秒）
LOG_QUEUE_MAXSIZE = 10000            # 内存日志队列容量

# execute_request / run_task 失败时的默认 17 元组：
# success, tts_rt, ttft, audio_duration, tts_rtf, mem, cpu, fsize,
# fail_reason, model, text_len, is_garbled, trans_text, asr_status, text_sim, asr_rt, save_path
FAIL_RESULT = (
    False, 0, 0, 0, 0, 0, 0, 0,
    "", "", 0, False, "", "none", 0.0, 0, "",
)

# =========================================================
# 二、全局变量
# =========================================================
RUNNING = True                       # 主循环运行标志，收到 SIGINT/SIGTERM 后置 False
_EXIT_REQUESTED = False              # 是否已收到过一次退出信号（二次 Ctrl+C 强制退出）
STAT_LOCK = Lock()                   # 保护 stats 字典与窗口指标
TASK_SEMAPHORE = BoundedSemaphore(CONCURRENCY + MAX_QUEUE_TASKS)  # 限制在途 TTS 任务总量
MODEL_SEMAPHORE = BoundedSemaphore(MODEL_POOL_SIZE)               # 限制同时占用模型实例数
ASR_SEMAPHORE = BoundedSemaphore(ASR_CONCURRENCY)                 # 限制同时调用 ASR 的数量
LOG_QUEUE = queue.Queue(maxsize=LOG_QUEUE_MAXSIZE)  # 异步写 CSV 的缓冲队列
LOG_FILE_LOCK = Lock()               # 写日志文件、轮转日志时互斥
GLOBAL_SESSION = None                # 全局 requests.Session（main 中初始化）
ASR_EXECUTOR = None                  # ASR 专用线程池（main 中初始化）

PENDING_ASR_LOCK = Lock()            # 保护 PENDING_ASR_FILES
PENDING_ASR_FILES = set()            # 正在 ASR 中的 wav 路径，清理线程会跳过

# 相似度计算前去除标点与空白，仅保留中英文与数字
_TEXT_CLEAN_RE = re.compile(r'[^\u4e00-\u9fa5a-zA-Z0-9]')

# =========================================================
# 三、退出与 HTTP 会话
# =========================================================
def graceful_exit(signum=None, frame=None):
    """
    捕获 Ctrl+C / kill：首次仅置 RUNNING=False，由 main 收尾（等 TTS/ASR、刷日志）。
    若 ASR 长时间未结束，可再次 Ctrl+C 强制退出。
    """
    global RUNNING, _EXIT_REQUESTED
    if _EXIT_REQUESTED:
        print("\n⚠️ 强制退出（ASR/日志可能未写完）\n")
        os._exit(1)
    _EXIT_REQUESTED = True
    print("\n🛑 正在停止压测，请稍候（等待在途 TTS/ASR 与日志落盘）...\n")
    RUNNING = False

signal.signal(signal.SIGINT, graceful_exit)
signal.signal(signal.SIGTERM, graceful_exit)

def create_global_session():
    """创建共享 HTTP 会话：禁用代理、扩大连接池，供 TTS/ASR 复用。"""
    session = requests.Session()
    session.trust_env = False
    session.proxies = {}
    adapter = HTTPAdapter(max_retries=0, pool_connections=200, pool_maxsize=200)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

# =========================================================
# 四、工具函数
# =========================================================
def send_alert(msg):
    """控制台打印高亮告警（如连续失败过多）。"""
    print("\n" + "=" * 80)
    print(f"🚨 告警触发：{msg}")
    print("=" * 80 + "\n")

def percentile(data, p):
    """计算百分位数（线性插值），用于 P95 等指标。"""
    if not data:
        return 0
    sorted_data = sorted(data)
    if len(sorted_data) == 1:
        return round(sorted_data[0], 3)
    rank = (len(sorted_data) - 1) * p / 100
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return round(sorted_data[int(rank)], 3)
    weight = rank - lower
    value = sorted_data[lower] * (1 - weight) + sorted_data[upper] * weight
    return round(value, 3)

def estimate_audio_duration_by_size(file_path):
    """WAV 头解析失败时，按默认采样参数从文件大小估算时长（秒）。"""
    audio_bytes = max(os.path.getsize(file_path) - 44, 0)  # 减去 44 字节标准头
    bytes_per_second = DEFAULT_AUDIO_SAMPLE_RATE * DEFAULT_AUDIO_CHANNELS * DEFAULT_AUDIO_SAMPLE_WIDTH
    return round(audio_bytes / bytes_per_second, 3) if bytes_per_second > 0 else 0

def get_wav_info(file_path):
    """
    校验 WAV 是否有效并返回时长。
    返回: (是否有效, 时长秒数)
    """
    try:
        with wave.open(file_path, 'rb') as f:
            frames = f.getnframes()
            frame_rate = f.getframerate()
            channels = f.getnchannels()
            sample_width = f.getsampwidth()
            duration = frames / frame_rate if frame_rate else 0
            header_valid = (
                frames > 100
                and MIN_VALID_SAMPLE_RATE <= frame_rate <= MAX_VALID_SAMPLE_RATE
                and 1 <= channels <= 8
                and 1 <= sample_width <= 4
                and 0 < duration <= MAX_REASONABLE_AUDIO_SECONDS
            )
            if header_valid:
                return True, round(duration, 3)
            fallback_duration = estimate_audio_duration_by_size(file_path)
            return fallback_duration > 0, fallback_duration
    except Exception:
        fallback_duration = estimate_audio_duration_by_size(file_path)
        return fallback_duration > 0, fallback_duration

def prune_time_window(stats, now_ts):
    """从各 deque 中移除超出滑动窗口的旧样本。"""
    cutoff = now_ts - METRICS_WINDOW_SECONDS
    for key in ("rt_samples", "ttft_samples", "rtf_samples", "window_events", "garbled_window_events"):
        while stats[key] and stats[key][0][0] < cutoff:
            stats[key].popleft()

def sample_values(samples):
    """从 (时间戳, 值) 队列中提取数值列表，供百分位计算。"""
    return [value for _, value in samples]

def format_csv_row(fields):
    """将字段列表格式化为单行 CSV（自动处理逗号、引号转义）。"""
    buf = io.StringIO()
    csv.writer(buf, lineterminator='\n').writerow(fields)
    return buf.getvalue()

def window_success_rate(window_events):
    """滑动窗口内 TTS 请求成功率（%）。"""
    if not window_events:
        return 0.0
    ok = sum(1 for _, success in window_events if success)
    return round(ok / len(window_events) * 100, 2)

def window_garbled_rate(garbled_window_events):
    """窗口乱读率 = 窗口内乱读次数 / 窗口内 ASR 成功比对次数"""
    if not garbled_window_events:
        return 0.0
    garbled = sum(1 for _, is_garbled in garbled_window_events if is_garbled)
    return round(garbled / len(garbled_window_events) * 100, 2)

def cumulative_garbled_rate(stats):
    """累计乱读率 = 乱读次数 / ASR 成功比对次数（不含 skip/fail）"""
    checked = stats["asr_ok_count"]
    if checked <= 0:
        return 0.0
    return round(stats["garbled_count"] / checked * 100, 2)

# =========================================================
# 五、文本相似度
# =========================================================
def text_similarity(original, transcribed):
    """基于 SequenceMatcher 计算清洗后原文与 ASR 转写的相似度 [0, 1]。"""
    if not original or not transcribed:
        return 0.0

    def clean(t):
        return _TEXT_CLEAN_RE.sub('', t)

    org_clean = clean(original)
    trans_clean = clean(transcribed)
    if not org_clean or not trans_clean:
        return 0.0
    return difflib.SequenceMatcher(None, org_clean, trans_clean).ratio()

# =========================================================
# 六、ASR 模块
# =========================================================
def _parse_asr_text(result):
    """从 ASR JSON 响应中提取转写文本（兼容多种字段名）。"""
    text = result.get("text", "") or result.get("transcript", "") or ""
    if isinstance(text, dict):
        text = text.get("text", "")
    return str(text).strip() if text else ""

def call_asr(wav_path):
    """
    将 wav 以 base64 流式方式 POST 到 ASR 服务。
    成功返回转写字符串，失败返回 None。
    """
    if not os.path.exists(wav_path) or GLOBAL_SESSION is None:
        return None
    with open(wav_path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "model": ASR_MODEL,
        "input_type": "stream",
        "input": b64_data,
        "hotwords": "",
    }

    for attempt in range(ASR_MAX_ATTEMPTS):
        try:
            resp = GLOBAL_SESSION.post(ASR_URL, json=payload, timeout=ASR_TIMEOUT)
            if resp.status_code == 200:
                return _parse_asr_text(resp.json()) or None
            time.sleep(0.5)
        except Exception as e:
            if attempt == ASR_MAX_ATTEMPTS - 1:
                print(f"⚠️ ASR 调用失败（共尝试 {ASR_MAX_ATTEMPTS} 次）: {e}")
            time.sleep(0.5)
    return None

def asr_check_and_mark(save_path, original_text):
    """
    返回: (是否乱读, 转写文本片段, asr_status, 相似度)
    asr_status: ok | skip | fail
    """
    acquired = ASR_SEMAPHORE.acquire(timeout=ASR_SEMAPHORE_TIMEOUT)
    if not acquired:
        return False, "[ASR_SKIP]", "skip", 0.0
    try:
        transcribed = call_asr(save_path)
        if transcribed is None:
            return False, "[ASR_FAIL]", "fail", 0.0
        sim = round(text_similarity(original_text, transcribed), 4)
        is_garbled = sim < ASR_TEXT_SIMILARITY_THRESHOLD
        short_trans = (transcribed[:200] + '...') if len(transcribed) > 200 else transcribed
        return is_garbled, short_trans, "ok", sim
    finally:
        ASR_SEMAPHORE.release()

def process_asr_after_tts(save_path, payload, iteration, stats, tts_snapshot):
    """TTS 成功后异步执行 ASR，完成后更新乱读统计并写日志。"""
    with PENDING_ASR_LOCK:
        PENDING_ASR_FILES.add(save_path)
    try:
        asr_start = time.time()
        is_garbled, trans_text, asr_status, text_sim = asr_check_and_mark(
            save_path, payload['input'],
        )
        asr_rt = round(time.time() - asr_start, 3)
    finally:
        with PENDING_ASR_LOCK:
            PENDING_ASR_FILES.discard(save_path)

    tts_rt = tts_snapshot["tts_rt"]
    total_rt = round(tts_rt + asr_rt, 3)
    event_ts = time.time()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    fail_reason = "ASR检测乱读" if is_garbled else ""

    with STAT_LOCK:
        # 仅 asr_status=ok 的样本计入乱读率分子分母
        if asr_status == "ok":
            stats["asr_ok_count"] += 1
            stats["garbled_window_events"].append((event_ts, is_garbled))
            if is_garbled:
                stats["garbled_count"] += 1
        elif asr_status == "skip":
            stats["asr_skip_count"] += 1
        elif asr_status == "fail":
            stats["asr_fail_count"] += 1

        prune_time_window(stats, event_ts)
        win_garbled = window_garbled_rate(stats["garbled_window_events"])
        cum_garbled = cumulative_garbled_rate(stats)

        garbled_flag = 1 if is_garbled else 0
        log_line = format_csv_row([
            iteration, 1, tts_snapshot["model"], tts_snapshot["ttft"], tts_rt, asr_rt, total_rt,
            tts_snapshot["audio_duration"], tts_snapshot["tts_rtf"], tts_snapshot["text_len"],
            tts_snapshot["fsize"], tts_snapshot["mem"], tts_snapshot["cpu"],
            garbled_flag, asr_status, text_sim, trans_text, now, fail_reason,
        ])
        try:
            LOG_QUEUE.put_nowait(log_line)
        except queue.Full:
            pass

    status = "🔴乱读" if is_garbled else "✅"
    asr_tag = "" if asr_status == "ok" else f" | ASR:{asr_status}"
    print(
        f"{status} 任务{iteration}{asr_tag} [ASR完成] | "
        f"TTS_RT:{tts_rt}s | ASR:{asr_rt}s | 总RT:{total_rt}s | "
        f"窗口乱读率:{win_garbled}% | 累计乱读率:{cum_garbled}%"
        + (f" | 相似度:{text_sim}" if asr_status == "ok" else "")
    )

# =========================================================
# 七、文本生成器
# =========================================================
class TextGenerator:
    """按概率随机抽取短/中/长测试文本，模拟真实输入分布。"""

    def __init__(self):
        self.short_texts = [
           "你好，欢迎参加本次竞岗面试。今天面试总共大约30分钟。前5分钟，请你先做一个简单的自我介绍，重点说清楚两件事：第一，你竞聘这个岗位相比其他人的核心优势是什么；第二，你过去取得的最有说服力的主要业绩或成果。之后我会问你一些问题，有些可能会比较直接，请你放松一些，按真实想法回答就好。那我们现在开始，请你先自我介绍。"
        ]
        self.medium_texts = [
            "中牙-我喜欢秋天，喜欢它的从容内敛，喜欢它的温柔沉静。它没有春日的匆忙绽放，没有夏日的热烈张扬，只用温润的色调告诉我们，万物有序，岁月安然。",
        ]
        self.long_texts = [
            "长长-荷塘的四面，远远近近，高高低低都是树，而杨柳最多。月光如流水一般，静静地泻在这一片叶子和花上。",
        ]

    def get_random_text(self):
        # 20% 短文本，30% 中文本，50% 长文本
        rand = random.random()
        if rand <1:
            return random.choice(self.short_texts)
        # if rand < 0.5:
        #     return random.choice(self.medium_texts)
        # return random.choice(self.long_texts)

# =========================================================
# 八、文件清理与日志
# =========================================================
def cleanup_tts_output():
    """删除最旧的 wav，保留 KEEP_WAV_FILES 个；跳过仍在 ASR 中的文件。"""
    files = glob.glob(os.path.join(OUTPUT_DIR, "*.wav"))
    if len(files) <= KEEP_WAV_FILES:
        return
    files.sort(key=os.path.getmtime)
    with PENDING_ASR_LOCK:
        pending = set(PENDING_ASR_FILES)
    for f in files[:-KEEP_WAV_FILES]:
        if f in pending:
            continue
        try:
            os.remove(f)
        except OSError:
            pass

def cleanup_log():
    """日志行数超限时保留表头 + 最近 KEEP_LOG_LINES 行。"""
    if not os.path.exists(LOG_FILE):
        return
    try:
        with LOG_FILE_LOCK:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) <= KEEP_LOG_LINES + 1:
                return
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(lines[0])
                f.writelines(lines[-KEEP_LOG_LINES:])
    except PermissionError:
        pass

def ensure_log_file():
    """确保 CSV 存在且表头为最新版；表头不匹配则备份旧文件后重建。"""
    try:
        with LOG_FILE_LOCK:
            if not os.path.exists(LOG_FILE):
                with open(LOG_FILE, "w", encoding="utf-8") as f:
                    f.write(LOG_HEADER)
                return
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                header = f.readline()
            if header != LOG_HEADER:
                backup_file = f"{LOG_FILE}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
                os.rename(LOG_FILE, backup_file)
                with open(LOG_FILE, "w", encoding="utf-8") as f:
                    f.write(LOG_HEADER)
                print(f"ℹ️ 检测到旧版日志表头，已备份为：{backup_file}")
    except PermissionError:
        print(f"⚠️ 日志文件被占用，暂时跳过表头检查：{LOG_FILE}")

def _drain_log_queue_to_file():
    """将 LOG_QUEUE 中当前所有条目一次性写入 CSV（线程安全）。"""
    lines = []
    while True:
        try:
            lines.append(LOG_QUEUE.get_nowait())
        except queue.Empty:
            break
    if not lines:
        return
    with LOG_FILE_LOCK:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.writelines(lines)

def flush_log_queue():
    """退出前刷盘：先给 log_worker 一轮落盘时间，再排空队列。"""
    time.sleep(LOG_FLUSH_INTERVAL * 2)
    _drain_log_queue_to_file()

def log_worker():
    """后台线程：批量从 LOG_QUEUE 取出并追加写入 CSV。"""
    while RUNNING:
        try:
            lines = []
            while not LOG_QUEUE.empty() and len(lines) < 100:
                lines.append(LOG_QUEUE.get())
            if lines:
                with LOG_FILE_LOCK:
                    with open(LOG_FILE, "a", encoding="utf-8") as f:
                        f.writelines(lines)
            time.sleep(LOG_FLUSH_INTERVAL)
        except Exception:
            time.sleep(0.1)
    # RUNNING=False 后收尾一轮，减少主线程退出前队列积压
    _drain_log_queue_to_file()

def cleanup_worker():
    """后台线程：周期性清理磁盘 wav 与日志文件。"""
    while RUNNING:
        cleanup_tts_output()
        cleanup_log()
        time.sleep(20)

# =========================================================
# 九、TTS 请求 + ASR 检测
# =========================================================
def _fail_result(payload, fail_reason):
    """构造 execute_request 失败时的 17 元组返回值。"""
    r = list(FAIL_RESULT)
    r[8] = fail_reason
    r[9] = payload['model']
    r[10] = len(payload['input'])
    return tuple(r)

# 仅对网络层瞬时异常重试，业务错误（HTTP 4xx/5xx、空音频等）不重试
@retry(
    stop=stop_after_attempt(NET_RETRY_TIMES),
    wait=wait_exponential(multiplier=1, min=0.2, max=2),
    retry=retry_if_exception_type((
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
        urllib3.exceptions.ProtocolError,
    )),
)
def execute_request(payload, iteration):
    """
    返回 17 元组（仅 TTS，ASR 由 run_task 异步提交）:
    success, tts_rt, ttft, audio_duration, tts_rtf, mem, cpu, fsize,
    fail_reason, model, text_len, is_garbled, trans_text, asr_status, text_sim, asr_rt, save_path
    """
    response = None
    fail_reason = ""
    try:
        start_time = time.time()
        ttft_time = None  # 首包时间（Time To First Token）
        headers = {"accept": "application/json", "Content-Type": "application/json"}

        # 流式 POST，边收边写 wav
        response = GLOBAL_SESSION.post(
            TTS_URL, json=payload, headers=headers, stream=True, verify=False,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        if response.status_code != 200:
            fail_reason = f"HTTP错误 {response.status_code}"
            raise RuntimeError(fail_reason)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        save_path = os.path.join(
            OUTPUT_DIR,
            f"task_{iteration}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav",
        )
        total_bytes = 0
        last_chunk_time = time.time()

        with open(save_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024):
                if time.time() - last_chunk_time > CHUNK_TIMEOUT:
                    fail_reason = f"流超时：{CHUNK_TIMEOUT}秒无数据"
                    raise RuntimeError(fail_reason)
                if not chunk:
                    continue
                if ttft_time is None:
                    ttft_time = time.time()
                last_chunk_time = time.time()
                f.write(chunk)
                total_bytes += len(chunk)

        if response:
            response.close()
            response = None

        if total_bytes < 1024:
            fail_reason = "空音频文件"
            raise RuntimeError(fail_reason)

        wav_valid, audio_duration = get_wav_info(save_path)
        if not wav_valid:
            fail_reason = "音频损坏/静音"
            raise RuntimeError(fail_reason)

        tts_rt = round(time.time() - start_time, 3)   # TTS 端到端耗时
        ttft = round(ttft_time - start_time, 3) if ttft_time else 0
        tts_rtf = round(tts_rt / audio_duration, 3) if audio_duration > 0 else 0  # 实时率 = 耗时/音频长
        mem = round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 2)
        cpu = psutil.cpu_percent(0.1)
        fsize = round(os.path.getsize(save_path) / 1024, 2)

        # ASR 字段占位：后续由 process_asr_after_tts 异步填充并写完整日志行
        return (
            True, tts_rt, ttft, audio_duration, tts_rtf, mem, cpu, fsize,
            "", payload['model'], len(payload['input']),
            False, "", "pending", 0.0, 0, save_path,
        )

    except Exception as e:
        tb_str = traceback.format_exc()[:500]
        fail_reason = fail_reason if fail_reason else f"{str(e)[:200]} | {tb_str}"
        try:
            if response:
                response.close()
        except Exception:
            pass
        return _fail_result(payload, fail_reason)

# =========================================================
# 十、任务调度与统计
# =========================================================
def _queue_fail(payload, reason):
    """排队/超时等未真正发起 TTS 请求时的失败结果。"""
    r = list(FAIL_RESULT)
    r[8] = reason
    r[9] = payload['model']
    r[10] = len(payload['input'])
    return tuple(r)

def run_task(payload, iteration, stats):
    """
    单任务入口：限流 → 调 TTS → 更新窗口统计 → 成功则异步提交 ASR。
    失败立即写 CSV；成功先打 TTS 日志，ASR 完成后再写含乱读字段的完整行。
    """
    if not RUNNING:
        return

    # 第一层：控制总在途任务数
    if not TASK_SEMAPHORE.acquire(timeout=API_QUEUE_TIMEOUT):
        res = _queue_fail(payload, f"API排队超时>{API_QUEUE_TIMEOUT}秒")
    else:
        try:
            # 第二层：控制同时打 TTS 的深度（模型实例池）
            if not MODEL_SEMAPHORE.acquire(timeout=MODEL_ACQUIRE_TIMEOUT):
                res = _queue_fail(payload, f"模型实例池耗尽>{MODEL_ACQUIRE_TIMEOUT}秒")
            else:
                try:
                    # 子线程执行 execute_request，主线程 join 超时防止卡死
                    result_queue = queue.Queue()
                    worker = Thread(
                        target=lambda: result_queue.put(execute_request(payload, iteration)),
                        daemon=True,
                    )
                    worker.start()
                    worker.join(TASK_SUBMIT_TIMEOUT)
                    if worker.is_alive():
                        res = _queue_fail(payload, f"队列提交超时>{TASK_SUBMIT_TIMEOUT}秒")
                    elif not result_queue.empty():
                        res = result_queue.get()
                    else:
                        res = _queue_fail(payload, "队列异常")
                finally:
                    MODEL_SEMAPHORE.release()
        finally:
            TASK_SEMAPHORE.release()

    with STAT_LOCK:
        (
            success, tts_rt, ttft, audio_duration, tts_rtf, mem, cpu, fsize,
            fail_reason, model, text_len, is_garbled, trans_text,
            asr_status, text_sim, asr_rt, save_path,
        ) = res
        event_ts = time.time()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        stats["window_events"].append((event_ts, success))

        if success:
            stats["success"] += 1
            stats["continuous_fail"] = 0
            stats["rt_samples"].append((event_ts, tts_rt))
            stats["ttft_samples"].append((event_ts, ttft))
            stats["rtf_samples"].append((event_ts, tts_rtf))
        else:
            stats["fail"] += 1
            stats["continuous_fail"] += 1

        prune_time_window(stats, event_ts)

        # TTS 失败：立即落盘，无 ASR 字段
        if not success:
            log_line = format_csv_row([
                iteration, 0, model, ttft, tts_rt, asr_rt, 0,
                audio_duration, tts_rtf, text_len, fsize, mem, cpu,
                0, asr_status, text_sim, trans_text, now, fail_reason,
            ])
            try:
                LOG_QUEUE.put_nowait(log_line)
            except queue.Full:
                pass

        if success:
            rt_window = sample_values(stats["rt_samples"])
            ttft_window = sample_values(stats["ttft_samples"])
            rtf_window = sample_values(stats["rtf_samples"])
            avg_rt = round(sum(rt_window) / len(rt_window), 2) if rt_window else 0
            p95_rt = percentile(rt_window, 95)
            p95_ttft = percentile(ttft_window, 95)
            p95_rtf = percentile(rtf_window, 95)
            win_succ = window_success_rate(stats["window_events"])

            print(
                f"✅ 任务{iteration} [TTS完成] | 并发:{CONCURRENCY} | "
                f"TTS_RT:{tts_rt}s | ASR:待检测 | "
                f"AVG_TTS_RT:{avg_rt}s | P95_TTS_RT:{p95_rt}s | P95_TTFT:{p95_ttft}s | "
                f"音频:{audio_duration}s | TTS_RTF:{tts_rtf} | P95_RTF:{p95_rtf} | "
                f"窗口成功率:{win_succ}%"
            )
        else:
            cf = stats["continuous_fail"]
            print(f"❌ 任务{iteration} | 连续失败:{cf} | 原因:{fail_reason[:100]}")
            if cf >= ALERT_CONTINUOUS_FAIL:
                send_alert(f"连续失败{ALERT_CONTINUOUS_FAIL}次！")

    # TTS 成功：提交 ASR 线程池，不阻塞当前 TTS worker
    if success and save_path and ASR_EXECUTOR is not None:
        tts_snapshot = {
            "tts_rt": tts_rt,
            "ttft": ttft,
            "audio_duration": audio_duration,
            "tts_rtf": tts_rtf,
            "mem": mem,
            "cpu": cpu,
            "fsize": fsize,
            "model": model,
            "text_len": text_len,
        }
        ASR_EXECUTOR.submit(
            process_asr_after_tts, save_path, payload, iteration, stats, tts_snapshot,
        )

# =========================================================
# 十一、主函数
# =========================================================
def main():
    """初始化资源、启动后台线程、循环提交 TTS 任务并汇总统计。"""
    parser = argparse.ArgumentParser(description="企业级 TTS 高并发压测 + ASR 乱读检测系统")
    parser.add_argument("--split", type=str, default="True", choices=["True", "False"],
                        help="是否分割文本 (True/False)，默认为 True")
    args = parser.parse_args()

    global GLOBAL_SESSION, ASR_EXECUTOR
    GLOBAL_SESSION = create_global_session()
    # ASR 与 TTS 使用独立线程池，避免 ASR 拖慢 TTS 并发
    ASR_EXECUTOR = ThreadPoolExecutor(
        max_workers=ASR_CONCURRENCY, thread_name_prefix="asr",
    )
    text_gen = TextGenerator()

    Thread(target=log_worker, daemon=True).start()
    Thread(target=cleanup_worker, daemon=True).start()

    # 可随机切换的 TTS 模型配置（instruct2 / zero_shot）
    TTS_MODELS = [
        {
            "model": "instruct2",
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0, "stream": True, "split": False, "text_frontend": True,
            },
        },
        {
            "model": "zero_shot",
            "tts_params": {
                "prompt_text": "", "prompt_audio": "",
                "zero_shot_spk_id": "yingyeyuan_male",
                "speed": 1.0, "stream": True, "split": False, "text_frontend": True,
            },
        },
    ]

    # 运行时统计：累计计数 + 带时间戳的滑动窗口 deque
    stats = {
        "success": 0,
        "fail": 0,
        "continuous_fail": 0,           # 当前连续 TTS 失败次数
        "garbled_count": 0,             # 累计乱读次数（仅 asr_status=ok）
        "asr_ok_count": 0,              # ASR 成功比对次数
        "asr_skip_count": 0,            # ASR 因并发满跳过
        "asr_fail_count": 0,            # ASR 调用失败
        "rt_samples": deque(),          # (ts, TTS_RT) 用于 P95
        "ttft_samples": deque(),        # (ts, TTFT)
        "rtf_samples": deque(),         # (ts, TTS_RTF)
        "window_events": deque(),       # (ts, TTS是否成功) 窗口成功率
        "garbled_window_events": deque(),  # (ts, 是否乱读) 窗口乱读率
    }

    ensure_log_file()

    print("=" * 90)
    print(f"🏢 TTS 压测 + ASR 乱读检测 | 并发:{CONCURRENCY} | 统计窗口:{METRICS_WINDOW_SECONDS}s")
    print(f"   乱读阈值:{ASR_TEXT_SIMILARITY_THRESHOLD} | ASR并发:{ASR_CONCURRENCY}")
    print("=" * 90)

    task_id = 0
    futures = []

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        while RUNNING and task_id < TOTAL_TASKS:
            task_id += 1
            text = text_gen.get_random_text()
            mode = random.choice(TTS_MODELS)
            tts_params = mode["tts_params"].copy()
            if args.split == "True":
                tts_params["split"] = True   # 长文本是否按句切分合成
            else:
                tts_params["split"] = False
            payload = {"model": mode["model"], "input": text, "tts_params": tts_params}
            futures.append(executor.submit(run_task, payload, task_id, stats))
            futures = [f for f in futures if not f.done()]  # 仅保留未完成的 future，防列表膨胀
            time.sleep(random.uniform(NORMAL_SLEEP_MIN, NORMAL_SLEEP_MAX))

        wait(futures)  # 等待所有在途 TTS 任务结束

    if ASR_EXECUTOR is not None:
        ASR_EXECUTOR.shutdown(wait=True)  # 等待队列中 ASR 任务全部完成

    flush_log_queue()  # ASR 完成后可能仍有日志在队列，确保落盘

    total = stats["success"] + stats["fail"]
    cum_garbled = cumulative_garbled_rate(stats)
    print("\n" + "=" * 60)
    print("🏁 压测完成")
    print(f"总请求: {total}  成功: {stats['success']}  失败: {stats['fail']}")
    print(f"成功率: {round(stats['success'] / total * 100, 2) if total else 0}%")
    print(
        f"ASR比对成功: {stats['asr_ok_count']}  跳过: {stats['asr_skip_count']}  "
        f"失败: {stats['asr_fail_count']}"
    )
    print(f"乱读次数: {stats['garbled_count']}  累计乱读率(仅ASR成功): {cum_garbled}%")
    print(f"窗口乱读率(近{METRICS_WINDOW_SECONDS}s): {window_garbled_rate(stats['garbled_window_events'])}%")
    print(f"详细日志: {LOG_FILE}")
    print("=" * 60)

if __name__ == "__main__":
    main()
