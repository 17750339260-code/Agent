# enterprise_tts_stability_parallel_final_three_layers.py
# 🔥 企业级 TTS 高并发并行压测系统（最终优化版，三层超时）
# 核心：无内存泄漏 + 异步日志 + 线程池限流 + 7×24稳定 + 无音频播放 + 三层超时
import os
import glob
import time
import queue
import random
import signal
import math
import csv
import psutil
import argparse
import urllib3
import requests
import traceback
import wave
from io import StringIO
from threading import Lock, Thread, BoundedSemaphore, local
from collections import deque
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait
from requests.adapters import HTTPAdapter
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# =========================================================
# 🔥 核心配置
# =========================================================
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

URL = "http://36.111.82.53:10015/api/tts/instruct2"
OUTPUT_DIR = "tts_output"
LOG_DIR = "./logs"                # 你要存放日志的目录
LOG_FILE = os.path.join(LOG_DIR, "tts_stability_log.csv")
# 日志表头：新增“音频秒数/RTF”，方便后续用 Excel 或 BI 工具按列分析
LOG_HEADER = "任务ID,成功,模型,TTFT,RT,音频秒数,RTF,字数,文件KB,内存MB,CPU%,时间,失败原因\n"
# 并发配置
# CONCURRENCY = 20                # 增加到 20 路并发测试新架构
CONCURRENCY = 8  # 增加到 20 路并发测试新架构
# MAX_QUEUE_TASKS = 25            # 客户端排队队列
MAX_QUEUE_TASKS = 8  # 客户端排队队列
TOTAL_TASKS = 10000000000000000  # 循环次数
# 超时配置
API_QUEUE_TIMEOUT = 60  # 第1层：API排队超时-入口排队：如果连门都进不去，没必要等太久
TASK_SUBMIT_TIMEOUT = 2100  # 第2层：任务队列提交超时-35分钟-真正干活：3000字生成音频+推理，给足宽裕时间
MODEL_ACQUIRE_TIMEOUT = 1200  # 第3层：模型实例获取超时-抢资源：如果抢不到干活的机器，说明太忙了，赶紧重试
CONNECT_TIMEOUT = 5  # 建连超时；1 秒在跨网段压测时容易误伤正常请求
READ_TIMEOUT = 2100  # 读取超时，必须覆盖整个生成过程
CHUNK_TIMEOUT = 120  # 【关键】流式包间隔超时，不是总时长！如果并发数增加这里也要增加
# 模型池数量-实际发给服务端请求
# MODEL_POOL_SIZE = 5
MODEL_POOL_SIZE = 8
# 自动清理
KEEP_WAV_FILES = 30
KEEP_LOG_LINES = 1000
# 【关键】企业监控/压测常用 Time-based Sliding Window：
# P95/平均RT/窗口成功率只看最近 1 小时，不让很久以前的慢请求长期污染当前指标
METRICS_WINDOW_SECONDS = 3600  # 企业压测常用口径：最近1小时滑动时间窗口
# WAV 时长兜底配置：有些流式接口返回的 WAV 头可能不规范，采样率异常时用文件大小兜底估算
DEFAULT_AUDIO_SAMPLE_RATE = 24000  # 常见 TTS 输出采样率：24kHz
DEFAULT_AUDIO_CHANNELS = 1  # 常见 TTS 输出：单声道
DEFAULT_AUDIO_SAMPLE_WIDTH = 2  # 常见 PCM16：每采样点 2 字节
MIN_VALID_SAMPLE_RATE = 8000
MAX_VALID_SAMPLE_RATE = 192000
MAX_REASONABLE_AUDIO_SECONDS = 7200
# 压测间隔 稳定性测试（或者叫负载测试）
# NORMAL_SLEEP_MIN = 5
# NORMAL_SLEEP_MAX = 20
# 压测间隔 并发测试
NORMAL_SLEEP_MIN = 0
NORMAL_SLEEP_MAX = 0.03
# 告警与重试
ALERT_CONTINUOUS_FAIL = 10
NET_RETRY_TIMES = 2
# 异步日志
LOG_FLUSH_INTERVAL = 1
LOG_QUEUE_MAXSIZE = 10000
# 全局状态
RUNNING = True
STAT_LOCK = Lock()
IN_FLIGHT_SEMAPHORE = BoundedSemaphore(CONCURRENCY + MAX_QUEUE_TASKS)
MODEL_SEMAPHORE = BoundedSemaphore(MODEL_POOL_SIZE)
LOG_QUEUE = queue.Queue(maxsize=LOG_QUEUE_MAXSIZE)
LOG_FILE_LOCK = Lock()
SESSION_LOCAL = local()


class TTSResponseError(Exception):
    """服务端返回非预期内容时使用；这类错误不走网络重试。"""


# =========================================================
# 优雅退出
# =========================================================
def graceful_exit(signum=None, frame=None):
    global RUNNING
    print("\n[STOP] 压测停止，等待当前任务结束...\n")
    RUNNING = False      # 删掉 os._exit(0) 这行就行


signal.signal(signal.SIGINT, graceful_exit)
signal.signal(signal.SIGTERM, graceful_exit)


# =========================================================
# 全局HTTP会话
# =========================================================
def create_global_session():
    session = requests.Session()
    session.trust_env = False
    session.proxies = {}
    adapter = HTTPAdapter(max_retries=0, pool_connections=200, pool_maxsize=200)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_session():
    if not hasattr(SESSION_LOCAL, "session"):
        SESSION_LOCAL.session = create_global_session()
    return SESSION_LOCAL.session


# =========================================================
# 工具函数
# =========================================================
def send_alert(msg):
    print("\n" + "=" * 80)
    print(f"[ALERT] 告警触发：{msg}")
    print("=" * 80 + "\n")


def percentile(data, p):
    """按企业压测常用口径计算百分位：对窗口内样本排序后做线性插值。"""
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
    # WAV 头异常时兜底：按 PCM16/24k/单声道，用文件大小估算音频时长
    # 这样可以避免异常 WAV 头把采样率读成 24Hz，导致音频时长被放大成十几个小时
    audio_bytes = max(os.path.getsize(file_path) - 44, 0)
    bytes_per_second = DEFAULT_AUDIO_SAMPLE_RATE * DEFAULT_AUDIO_CHANNELS * DEFAULT_AUDIO_SAMPLE_WIDTH
    return round(audio_bytes / bytes_per_second, 3) if bytes_per_second > 0 else 0


def get_wav_info(file_path):
    # 读取 WAV 真实音频时长：RTF 必须用“生成耗时 / 音频时长”，不能只看文件大小或文本长度
    # 优先使用标准 WAV 头；如果采样率/声道/位宽明显异常，则回退到文件大小估算
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
    except:
        fallback_duration = estimate_audio_duration_by_size(file_path)
        return fallback_duration > 0, fallback_duration


def prune_time_window(stats, now_ts):
    # 滑动窗口淘汰：每次任务完成时，把 1 小时以前的样本从队列头部清理掉
    # 注意这里按时间清理，不按固定条数清理；并发量升高时统计口径仍然是“最近1小时”
    cutoff = now_ts - METRICS_WINDOW_SECONDS
    for key in ("rt_samples", "ttft_samples", "rtf_samples", "audio_duration_samples", "window_events"):
        while stats[key] and stats[key][0][0] < cutoff:
            stats[key].popleft()


def sample_values(samples):
    # 指标队列保存的是 (时间戳, 指标值)，计算 P95 时只取指标值
    return [value for _, value in samples]


def make_csv_line(row):
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(row)
    return buffer.getvalue()


def count_window_success(events):
    total = len(events)
    if total == 0:
        return 0, 0, 0
    success_count = sum(1 for _, success in events if success)
    return total, success_count, round(success_count / total * 100, 2)


# =========================================================
# 文本生成器
# =========================================================
class TextGenerator:
    def __init__(self):
        self.short_texts = [
           "今天是个好天气......................................................................................................................................今天星期五..............................................................................................................................................................................................我想吃肯德基..............................吃吧"
        ]
        self.medium_texts = [
            "通知用户查询银行卡账单确定退费是否已 技能类培训标准教材 15到账。不退费的应向客户解释说明。 5.交班和日结 (1)每个收费员在当班(当日)收费完毕后,必须对现金和支票进行清点,核对银行进 账回单、电费票据存根、未收电费票据、作废电费票据、POS机交易回单等,形成报表,并 根据报表的汇总数将电费交日结负责人,现金和支票必须当日全额进账,不得存放他处,严 禁挪用电费。"
            "中用户确认栏中签名,并收集客户身份证复印件和银行卡复印件(银行卡必须为用户自身身份证 办理的银行卡,不得使用他人银行卡)。对于其它非居民客户应在《退费审批表》中用户确 认栏中签名并加盖公章,收集营业执照、法人身份证、公司银行账户号码(开户银行许可证 或加盖公司公章的银行账户信息);若非法定代表人签名,由他人代签,还需收集授权委托 书和委托人身份证复印件。 (2)窗口人员接到退费申请审批完成信息后"
        ]
        # self.long_texts = [
        #     "我喜欢秋天，喜欢它的从容内敛，喜欢它的温柔沉静。它没有春日的匆忙绽放，没有夏日的热烈张扬，没有冬日的清冷孤寂，只用温润的色调、舒缓的清风、静谧的氛围，告诉我们，万物有序，岁月安然。前行的路上，难免会遇到挫折，经历低谷，会想要放弃，会心生退缩。但真正的成长，往往就藏在咬牙坚持的每一刻。熬过风雨，才能遇见彩虹；熬过迷茫，才能看清方向；熬过枯燥，才能收获惊喜。生活从来都不会一直一帆风顺，前路漫漫，总会遇见风雨，遇见坎坷，遇见迷茫与彷徨。但无论前路多么曲折，我们的心底，总要留存一束暖阳，一份热爱，一份永不言弃的力量。愿你在这漫长的旅途中，保持初心，砥砺前行。世间所有的美好，都离不开长久的坚持与默默的付出。没有一蹴而就的成功，没有轻而易举的收获，那些看似耀眼的光芒，背后都是无数个日夜的坚守与努力。每一次挑战都是成长的契机，每一份困难都是锻炼的阶梯。只要心中有光，脚下就有路。世间所有的美好，都离不开长久的坚持与默默的付出。没有一蹴而就的成功，没有轻而易举的收获，那些看似耀眼的光芒，背后都是无数个日夜的坚守与努力。每一次挑战都是成长的契机，每一份困难都是锻炼的阶梯。只要心中有光，脚下就有路。愿你在这条漫长的人生路上，保持热爱，奔赴山海，忠于自己，砥砺前行。",
        #     "世间所有的美好，都离不开长久的坚持与默默的付出。没有一蹴而就的成功，没有轻而易举的收获，那些看似耀眼的光芒，背后都是无数个日夜的坚守与努力。每一次挑战都是成长的契机，每一份困难都是锻炼的阶梯。只要心中有光，脚下就有路。喜欢秋天，喜欢它的从容内敛，喜欢它的温柔沉静。它没有春日的匆忙绽放，没有夏日的热烈张扬，没有冬日的清冷孤寂，只用温润的色调、舒缓的清风、静谧的氛围，告诉我们，万物有序，岁月安然。前行的路上，难免会遇到挫折，经历低谷，会想要放弃，会心生退缩。但真正的成长，往往就藏在咬牙坚持的每一刻。熬过风雨，才能遇见彩虹；熬过迷茫，才能看清方向；熬过枯燥，才能收获惊喜。生活的道路从不是一帆风顺，我们会遇到挫折，会经历失败，会在深夜里独自流泪。但正是这些时刻，塑造了我们的坚韧与勇气。每一次跌倒都是重新站起的准备，每一次伤痛都是成长的勋章。不要害怕暂时的困境，因为黑夜过后必定是黎明；不要放弃坚持的希望，因为雨后天空总会绽放彩虹。愿你在这条漫长的人生路上，保持热爱，奔赴山海，忠于自己，砥砺前行。愿你眼中总有光芒，活成自己想要的模样。愿你在这漫长的旅途中，保持初心，砥砺前行，愿你所有坚持，都能换来繁花似锦。",
        # ]
        self.long_texts = [
            "在前两节课里,我们学习了三大资源规划中的培训教材规划和培训基地规划。那么,如何开展培训师资规划呢?本节课将从概念工具和关注点三个方面,了解培训师资规划的有关内容。首先给大家介绍一下概念篇培训师资规划,是指依据企业发展战略和新业务发展需求,结合师资现状和师资需求分析结果,对企业师资队伍的选用预留进行整体规划,并制定可操作、可实施的发展规划。一般来说,师资规划、有师资现状、盘点师资需求分析、师资问题分"
            "款凭证(一张支票打印一张送款凭证),确认电费资金到 账后打印电费发票。 (4)对支票退票,在营销管理信息系统进行收费冲正操作后,进入追收工作流程。 4.退费业务 (1)受理客户退费申请并转相关部门办理。受理客户退费申请资料,收集提供退费所属 用电期间的电费发票及复印件或其他退费的依据等,收集客户身份证明及银行卡信息。对于 居民客户或非居民中的自然人(例如无公章的个体工商户),请客户在《退费审批表》"
            "据相符,避免错收。 (2)在前台收费中,应当面点清,并唱收唱付。收费后,即时打印电费发票,电子发票 自带发票专用章直接打印拿给客户,若客户需要电子版,可将电子发票推送至客户登记的邮 箱账号。若电费发票为纸质发票(部分地市专用增值税发票仍为纸质发票)则打印后须盖上 发票专用章,再交给客户。 (3)收取支票时,核对支票上的各项目是否符合要求,然后在营销管理信息系统上输入 支票的号码和金额,打印银行的送"
        ]

    def get_random_text(self):
        rand = random.random()
        if rand < 0.2:
            return random.choice(self.short_texts)
        elif rand < 0.3:
            return random.choice(self.medium_texts)
        else:
            return random.choice(self.long_texts)


# =========================================================
# 文件清理
# =========================================================
def cleanup_tts_output():
    files = glob.glob(os.path.join(OUTPUT_DIR, "*.wav"))
    if len(files) <= KEEP_WAV_FILES: return
    files.sort(key=os.path.getmtime)
    for f in files[:-KEEP_WAV_FILES]:
        try:
            os.remove(f)
        except:
            pass


def cleanup_log():
    # Windows 下 CSV 可能被日志线程、Excel/WPS 或杀毒软件短暂占用；占用时跳过本轮清理即可
    if not os.path.exists(LOG_FILE): return
    try:
        with LOG_FILE_LOCK:
            with open(LOG_FILE, "r", encoding="utf-8") as f: lines = f.readlines()
            if len(lines) <= KEEP_LOG_LINES + 1: return
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(lines[0])
                f.writelines(lines[-KEEP_LOG_LINES:])
    except PermissionError:
        return


def ensure_log_file():
    # 兼容旧日志：旧表头没有音频秒数/RTF，继续追加会导致 CSV 列错位
    # 发现旧格式时自动备份，然后创建新表头，避免后续分析误读
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
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
                print(f"[INFO] 检测到旧版日志表头，已备份为：{backup_file}")
    except PermissionError:
        print(f"[WARN] 日志文件被占用，暂时跳过表头检查：{LOG_FILE}")


# =========================================================
# 异步日志线程
# =========================================================
def log_worker():
    while RUNNING:
        try:
            flush_log_queue(max_lines=100)
            time.sleep(LOG_FLUSH_INTERVAL)
        except:
            time.sleep(0.1)


def flush_log_queue(max_lines=None):
    lines = []
    while not LOG_QUEUE.empty() and (max_lines is None or len(lines) < max_lines):
        lines.append(LOG_QUEUE.get())
    if lines:
        with LOG_FILE_LOCK:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.writelines(lines)


# =========================================================
# 定时清理线程
# =========================================================
def cleanup_worker():
    while RUNNING:
        cleanup_tts_output()
        cleanup_log()
        time.sleep(20)


# =========================================================
# 核心请求
# =========================================================
@retry(stop=stop_after_attempt(NET_RETRY_TIMES),
       wait=wait_exponential(multiplier=1, min=0.2, max=2),
       retry=retry_if_exception_type(
           (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError,
            urllib3.exceptions.ProtocolError)))
def request_tts_once(payload, iteration):
    response = None
    start_time = time.time()
    ttft_time = None
    headers = {"accept": "application/json", "Content-Type": "application/json"}

    request_payload = {"tts_params": payload["tts_params"]}
    try:
        response = get_session().post(
            URL, json=request_payload, headers=headers, stream=True, verify=False,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
        )

        if response.status_code != 200:
            raise TTSResponseError(f"HTTP错误 {response.status_code}: {response.text[:200]}")

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        save_path = os.path.join(OUTPUT_DIR, f"task_{iteration}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav")
        total_bytes = 0
        last_chunk_time = time.time()

        with open(save_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                now = time.time()
                if now - last_chunk_time > CHUNK_TIMEOUT:
                    raise TTSResponseError(f"流超时：{CHUNK_TIMEOUT}秒无数据")
                if not chunk:
                    continue
                if ttft_time is None:
                    ttft_time = now
                last_chunk_time = now
                f.write(chunk)
                total_bytes += len(chunk)

        if total_bytes < 1024:
            fail_reason = (
                f"空音频文件: bytes={total_bytes}, "
                f"content_type={response.headers.get('Content-Type', 'N/A')}, "
                f"worker={response.headers.get('X-Worker-ID', 'N/A')}, "
                f"pid={response.headers.get('X-Process-ID', 'N/A')}"
            )
            raise TTSResponseError(fail_reason)

        with open(save_path, "rb") as audio_file:
            file_header = audio_file.read(12)
        if not (file_header.startswith(b"RIFF") and file_header[8:12] == b"WAVE"):
            raise TTSResponseError(
                f"非WAV响应: bytes={total_bytes}, content_type={response.headers.get('Content-Type', 'N/A')}"
            )

        wav_valid, audio_duration = get_wav_info(save_path)
        if not wav_valid:
            raise TTSResponseError("音频损坏/静音")

        rt = round(time.time() - start_time, 3)
        ttft = round(ttft_time - start_time, 3) if ttft_time else 0
        # RTF(Real Time Factor)：值越小越好；RTF=1 表示生成 1 秒音频需要 1 秒
        # TTS 压测里 RT 受文本长短影响很大，RTF 更适合横向比较不同文本长度的生成效率
        rtf = round(rt / audio_duration, 3) if audio_duration > 0 else 0
        mem = round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 2)
        cpu = psutil.cpu_percent(0.1)
        fsize = round(os.path.getsize(save_path) / 1024, 2)
        return True, rt, ttft, audio_duration, rtf, mem, cpu, fsize, "", payload['model'], len(payload['input'])
    finally:
        try:
            response.close() if response else None
        except:
            pass


def execute_request(payload, iteration):
    try:
        return request_tts_once(payload, iteration)
    except Exception as e:
        tb_str = traceback.format_exc()[:500]
        fail_reason = f"{str(e)[:200]} | {tb_str}"
        return False, 0, 0, 0, 0, 0, 0, 0, fail_reason, payload['model'], len(payload['input'])


# =========================================================
# 并行任务 + 三层超时
# =========================================================
def record_result(res, iteration, stats):
    with STAT_LOCK:
        success, rt, ttft, audio_duration, rtf, mem, cpu, fsize, fail_reason, model, text_len = res
        event_ts = time.time()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # window_events 保留最近1小时所有请求事件；窗口成功率直接基于它计算
        stats["window_events"].append((event_ts, success))
        if success:
            stats["success"] += 1
            stats["continuous_fail"] = 0
            # 成功请求才进入 RT/TTFT/RTF 百分位统计；失败请求单独计入成功率
            stats["rt_samples"].append((event_ts, rt))
            stats["ttft_samples"].append((event_ts, ttft))
            stats["rtf_samples"].append((event_ts, rtf))
            stats["audio_duration_samples"].append((event_ts, audio_duration))
        else:
            stats["fail"] += 1
            stats["continuous_fail"] += 1
        prune_time_window(stats, event_ts)

        log_line = make_csv_line([
            iteration, success, model, ttft, rt, audio_duration, rtf, text_len, fsize, mem, cpu, now, fail_reason
        ])
        try:
            LOG_QUEUE.put_nowait(log_line)
        except queue.Full:
            pass

        rt_window = sample_values(stats["rt_samples"])
        ttft_window = sample_values(stats["ttft_samples"])
        rtf_window = sample_values(stats["rtf_samples"])
        audio_window = sample_values(stats["audio_duration_samples"])
        avg_rt = round(sum(rt_window) / len(rt_window), 2) if rt_window else 0
        p95_rt = percentile(rt_window, 95)
        p95_ttft = percentile(ttft_window, 95)
        p95_rtf = percentile(rtf_window, 95)
        window_total, _, window_rate = count_window_success(stats["window_events"])
        window_start_ts = stats["window_events"][0][0] if stats["window_events"] else stats["start_ts"]
        elapsed_window = max(1, event_ts - window_start_ts)
        qps = round(window_total / elapsed_window, 3)
        audio_per_second = round(sum(audio_window) / elapsed_window, 3) if audio_window else 0
        total = stats["success"] + stats["fail"]
        total_rate = round(stats["success"] / total * 100, 2) if total > 0 else 0

        # 控制台输出
        if success:
            print(
                f"[OK] 任务{iteration} | 并发:{CONCURRENCY} | RT:{rt}s | AVG_RT:{avg_rt}s | "
                f"P95_RT:{p95_rt}s | P95_TTFT:{p95_ttft}s | 音频:{audio_duration}s | RTF:{rtf} | "
                f"P95_RTF:{p95_rtf} | QPS/TPS:{qps} | 音频秒/s:{audio_per_second} | "
                f"窗口成功率:{window_rate}% | 累计成功率:{total_rate}%"
            )
        else:
            cf = stats["continuous_fail"]
            print(
                f"[FAIL] 任务{iteration} | 连续失败:{cf} | QPS/TPS:{qps} | "
                f"窗口成功率:{window_rate}% | 累计成功率:{total_rate}% | 原因:{fail_reason[:100]}"
            )
            if cf >= ALERT_CONTINUOUS_FAIL:
                send_alert(f"连续失败{ALERT_CONTINUOUS_FAIL}次！")


def run_task(payload, iteration, stats):
    if not RUNNING:
        return

    if not MODEL_SEMAPHORE.acquire(timeout=MODEL_ACQUIRE_TIMEOUT):
        res = (False, 0, 0, 0, 0, 0, 0, 0, f"模型实例池耗尽>{MODEL_ACQUIRE_TIMEOUT}秒", payload['model'],
               len(payload['input']))
    else:
        try:
            res = execute_request(payload, iteration)
        finally:
            MODEL_SEMAPHORE.release()

    record_result(res, iteration, stats)


# =========================================================
# 主函数
# =========================================================
def main():
    global CONCURRENCY, MODEL_POOL_SIZE, MAX_QUEUE_TASKS, TOTAL_TASKS
    global IN_FLIGHT_SEMAPHORE, MODEL_SEMAPHORE
    global URL, NORMAL_SLEEP_MIN, NORMAL_SLEEP_MAX

    parser = argparse.ArgumentParser(description="企业级 TTS 高并发压测系统")
    parser.add_argument("--split", type=str, default="True", choices=["True", "False"],
                        help="是否分割文本 (True/False)，默认为 True")
    parser.add_argument("--url", type=str, default=URL, help="TTS instruct2 接口地址")
    parser.add_argument("--total-tasks", type=int, default=TOTAL_TASKS, help="本次压测任务总数")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY, help="并发请求数")
    parser.add_argument("--model-pool-size", type=int, default=None, help="客户端模型信号量大小，默认等于并发数")
    parser.add_argument("--max-queue-tasks", type=int, default=MAX_QUEUE_TASKS, help="客户端额外排队任务数")
    parser.add_argument("--prompt-audio", type=str, default="kehu_female_b", help="prompt_audio 音色ID")
    parser.add_argument("--zero-shot-spk-id", type=str, default="kehu_female_b", help="zero_shot_spk_id 音色ID")
    parser.add_argument("--sleep-min", type=float, default=NORMAL_SLEEP_MIN, help="提交请求之间的最小随机间隔秒数")
    parser.add_argument("--sleep-max", type=float, default=NORMAL_SLEEP_MAX, help="提交请求之间的最大随机间隔秒数")
    parser.add_argument("--text", type=str, default="", help="固定合成文本；为空时使用随机文本池")
    args = parser.parse_args()
    split_value = args.split == "True"

    URL = args.url
    CONCURRENCY = max(1, args.concurrency)
    MODEL_POOL_SIZE = max(1, args.model_pool_size or CONCURRENCY)
    MAX_QUEUE_TASKS = max(0, args.max_queue_tasks)
    TOTAL_TASKS = max(1, args.total_tasks)
    NORMAL_SLEEP_MIN = max(0, args.sleep_min)
    NORMAL_SLEEP_MAX = max(NORMAL_SLEEP_MIN, args.sleep_max)
    IN_FLIGHT_SEMAPHORE = BoundedSemaphore(CONCURRENCY + MAX_QUEUE_TASKS)
    MODEL_SEMAPHORE = BoundedSemaphore(MODEL_POOL_SIZE)
    text_gen = TextGenerator()

    Thread(target=log_worker, daemon=True).start()
    Thread(target=cleanup_worker, daemon=True).start()

    TTS_MODELS = [
        {
            "model": "instruct2",
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                "prompt_audio": args.prompt_audio,
                "zero_shot_spk_id": args.zero_shot_spk_id,
                "speed": 1.0,
                "stream": True,
                "background_audio": "",
                "background_volume": 0.0,
                "background_loop": True,
                "text_frontend": True,
                "seed": 0,
                "split": split_value,
                "res_content": True,
            },
        }
    ]

    stats = {
        "success": 0,
        "fail": 0,
        "continuous_fail": 0,
        "rt_samples": deque(),
        "ttft_samples": deque(),
        "rtf_samples": deque(),
        "audio_duration_samples": deque(),
        "window_events": deque(),
        "start_ts": time.time(),
    }

    ensure_log_file()

    print("=" * 90)
    print(f"企业级 TTS 高并发压测系统 | 并发：{CONCURRENCY} | 总任务：{TOTAL_TASKS}")
    print("=" * 90)

    task_id = 0
    futures = []

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        while RUNNING and task_id < TOTAL_TASKS:
            task_id += 1
            text = args.text or text_gen.get_random_text()
            mode = random.choice(TTS_MODELS)

            # 🔥 复制 tts_params，避免修改原字典（影响后续任务）
            tts_params = mode["tts_params"].copy()
            tts_params["text"] = text
            # 🔥 随机决定 split 值（True 或 False）
            # tts_params["split"] = random.choice([True, False])

            payload = {
                "model": mode["model"],
                "input": text,
                "tts_params": tts_params
            }

            if not IN_FLIGHT_SEMAPHORE.acquire(timeout=API_QUEUE_TIMEOUT):
                res = (
                    False, 0, 0, 0, 0, 0, 0, 0,
                    f"客户端提交队列超时>{API_QUEUE_TIMEOUT}秒 | in-flight已达{CONCURRENCY + MAX_QUEUE_TASKS}",
                    payload['model'], len(payload['input'])
                )
                record_result(res, task_id, stats)
                continue

            future = executor.submit(run_task, payload, task_id, stats)
            future.add_done_callback(lambda _: IN_FLIGHT_SEMAPHORE.release())
            futures.append(future)
            futures = [f for f in futures if not f.done()]
            time.sleep(random.uniform(NORMAL_SLEEP_MIN, NORMAL_SLEEP_MAX))

        wait(futures)
    flush_log_queue()
    print("\n[DONE] 压测任务全部完成！")


if __name__ == "__main__":
    main()
