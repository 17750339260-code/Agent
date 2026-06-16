# -*- coding: utf-8 -*-
# test_tts_api_npu_serial.py
# 企业级 TTS 7×24 小时稳定性压测系统（NPU 串行压测版）
#
# 特性：
# 1. 实时播放 + 保存 WAV
# 2. 长文本随机生成（稳定 500~800+ 字）
# 3. chunk 级 WatchDog（彻底解决 stream 卡死）
# 4. 单轮硬超时保护（防线程永久挂死）
# 5. response.close() 防句柄泄漏
# 6. 全异常兜底 + 永不退出
# 7. 自动清理旧音频 + 裁剪日志
# 8. 连续失败自动告警
# 9. P95 / P99 / 平均 RT / 成功率统计
# 10. Ctrl+C 优雅退出
# 11. 全局单例 PyAudio（避免资源泄漏）

import os
import glob
import time
import queue
import random
import struct
import signal
import psutil
import pyaudio
import urllib3
import requests
import traceback
from collections import deque
from threading import Thread
from datetime import datetime
from requests.adapters import HTTPAdapter

# =========================================================
# 基础配置
# =========================================================

# 关闭 HTTPS 证书校验告警（verify=False 时避免刷屏）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# TTS 流式合成接口地址
URL = "http://36.111.82.53:10014/v1/audio/speech"

# 音频输出目录与 CSV 日志路径
OUTPUT_DIR = "tts_output"
LOG_DIR = "./logs"                # 你要存放日志的目录
LOG_FILE = os.path.join(LOG_DIR, "tts_stability_log.csv")

# HTTP 连接超时 / 读超时（秒）
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 10

# # 单轮最大生命周期（硬 WatchDog，当前未启用）
# MAX_CASE_TIME = 60

# 流式 chunk 间隔超时：超过该秒数未收到新数据则判定卡死
CHUNK_TIMEOUT = 20

# 磁盘与日志保留策略
KEEP_WAV_FILES = 30  # 最多保留 WAV 文件数量
KEEP_LOG_LINES = 1000  # CSV 日志最多保留数据行数（不含表头）
MAX_STATS_HISTORY = 1000  # RT/TTFT 滑动窗口最大样本数

# 轮次间隔：失败后退避 / 正常随机休眠
FAIL_SLEEP = 5
NORMAL_SLEEP_MIN = 2
NORMAL_SLEEP_MAX = 4

# 连续失败达到阈值时触发控制台告警
ALERT_CONTINUOUS_FAIL = 10

# requests 底层网络自动重试次数
NET_RETRY_TIMES = 1

# 主循环运行标志，Ctrl+C 时置 False
RUNNING = True

# =========================================================
# 全局单例 PyAudio
# =========================================================

# 进程内只初始化一次，避免反复 open/terminate 导致声卡句柄泄漏
GLOBAL_PYAUDIO = pyaudio.PyAudio()


# =========================================================
# 优雅退出
# =========================================================

def graceful_exit(signum=None, frame=None):
    """捕获 SIGINT/SIGTERM，释放 PyAudio 后强制退出进程。"""
    global RUNNING

    print("\n\n🛑 检测到退出信号，正在优雅释放资源...\n")
    RUNNING = False

    try:
        GLOBAL_PYAUDIO.terminate()
    except:
        pass

    print("✅ PyAudio 已释放")
    print("✅ 程序安全退出")

    os._exit(0)


signal.signal(signal.SIGINT, graceful_exit)
signal.signal(signal.SIGTERM, graceful_exit)


# =========================================================
# Session 创建
# =========================================================

def create_session():
    """创建带连接池与重试的 requests Session，每轮压测独立使用。"""
    session = requests.Session()

    # ★ 关键修复：彻底关闭系统代理（防止被 127.0.0.1 劫持）
    session.trust_env = False
    session.proxies = {}

    adapter = HTTPAdapter(
        max_retries=NET_RETRY_TIMES
    )

    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


# =========================================================
# 告警系统
# =========================================================

def send_alert(msg):
    """控制台打印高亮告警信息（可扩展为钉钉/邮件等）。"""
    print("\n" + "=" * 80)
    print(f"🚨 告警触发：{msg}")
    print("=" * 80 + "\n")


# =========================================================
# 百分位统计
# =========================================================

def percentile(data, p):
    """计算响应时间等指标的 P 分位值（如 P95、P99）。"""
    if not data:
        return 0

    data = sorted(data)

    k = int(len(data) * p / 100)
    k = min(k, len(data) - 1)

    return round(data[k], 3)


# =========================================================
# 文本生成器（短/中/长 混合文本生成器）
# =========================================================

class TextGenerator:
    """智学助手 TTS 专用：短/中/长混合文本生成器（7×24 稳定性测试）。"""

    def __init__(self):
        # 1. 短文本（50~200 字：问答、短句、知识点）
        # 注意：相邻字符串字面量会自动拼接为一条长文本
        self.short_texts = [
            # "各位来宾、女士们、先生们，大家好！今天我很荣幸向大家介绍南方电网智学平台。让我们共同探索2025年10月的最新成果和发展方向。谢谢。"
            # "南网智学平台涵盖了全网课程体系，包括1200门党建类、850门管理类、5600门专业技术类、9400门技能类以及300门辅助类课程。平台支持在线学习和专题学习，累计支撑6500万人次在线学习，4080万人次专题学习。南网智学为干部、员工提供全职业生涯一站式学习培训服务，支撑全网每年约17万期次培训项目实施。今年，我们正依托“大瓦特”底座，利用人工智能技术开发学习智能体，建设智能检索、个性化推送、仿真陪练、智慧评价等功能，持续探索培训资源的智慧化管理。"
            # "南网智学目前的建设现状涵盖了广泛的应用和服务对象。应用方面包括集群、用户系统、应用生态等；服务对象则涉及管理层、人资部门、业务部门及培训管理员等。内容生态丰富，包含文档、视频、音频课件以及各类案例和评价结果。运营服务全面，支持学员学习、培训管理、资源管理和培训评估等多个环节，确保培训活动的有效实施和持续优化。"
            #
            "今天是个好天气......................................................................................................................................今天星期五..............................................................................................................................................................................................我想吃肯德基..............................吃吧"
        ]

        # 2. 中文本（300~600 字：段落、课文片段）
        self.medium_texts = [
            "各位来宾、女士们、先生们，大家好！今天我很荣幸向大家介绍南方电网智学平台。让我们共同探索2025年10月的最新成果和发展方向。谢谢。南网智学平台涵盖了全网课程体系，包括1200门党建类、850门管理类、5600门专业技术类、9400门技能类以及300门辅助类课程。平台支持在线学习和专题学习，累计支撑6500万人次在线学习，4080万人次专题学习。南网智学为干部、员工提供全职业生涯一站式学习培训服务，支撑全网每年约17万期次培训项目实施。今年，我们正依托“大瓦特”底座，利用人工智能技术开发学习智能体，建设智能检索、个性化推送、仿真陪练、智慧评价等功能，持续探索培训资源的智慧化管理。南网智学目前的建设现状涵盖了广泛的应用和服务对象。应用方面包括集群、用户系统、应用生态等；服务对象则涉及管理层、人资部门、业务部门及培训管理员等。内容生态丰富，包含文档、视频、音频课件以及各类案例和评价结果。运营服务全面，支持学员学习、培训管理、资源管理和培训评估等多个环节，确保培训活动的有效实施和持续优化。"
        ]

        # 3. 长文本（800~1200 字：完整课文、长材料，边界测试；当前分支已注释）
        self.long_texts = [
            """曲曲折折的荷塘上面，弥望的是田田的叶子。叶子出水很高，像亭亭的舞女的裙。层层的叶子中间，零星地点缀着些白花，有袅娜地开着的，有羞涩地打着朵儿的；正如一粒粒的明珠，又如碧天里的星星，又如刚出浴的美人。微风过处，送来缕缕清香，仿佛远处高楼上渺茫的歌声似的。月光如流水一般，静静地泻在这一片叶子和花上。薄薄的青雾浮起在荷塘里。叶子和花仿佛在牛乳中洗过一样；又像笼着轻纱的梦。虽然是满月，天上却有一层淡淡的云，所以不能朗照；但我以为这恰是到了好处。月光是隔了树照过来的，高处丛生的灌木，落下参差的斑驳的黑影，峭楞楞如鬼一般；弯弯的杨柳的稀疏的倩影，却又像是画在荷叶上。""",

            """秋天，总是以一种安静而温柔的姿态，缓缓降临人间。告别了夏日的燥热与喧嚣，风开始变得轻柔，云慢慢变得疏淡。走在郊外的小路上，道路两旁的树木，渐渐染上深浅不一的色彩，翠绿慢慢泛黄，浅红慢慢晕开，层层叠叠的枝叶，在阳光的照耀下，温柔又静谧。秋日的天空，总是格外辽阔高远。澄澈的蓝天干净纯粹，点缀着几缕轻薄的白云，悠远又宁静。生活从来都不会一直一帆风顺，前路漫漫，总会遇见风雨，遇见坎坷，遇见迷茫与彷徨。心怀温柔，方能遇见美好。用善意对待身边的人和事，用包容接纳生活的不完美。"""
        ]

    def get_random_text(self):
        """按 30% 短 / 40% 中 比例随机抽取测试文本（长文本分支暂未启用）。"""
        rand = random.random()
        if rand < 1:  # 30% 短文本
            return random.choice(self.short_texts)
        # elif rand < 0.4:  # 40% 中文本
        #     return random.choice(self.medium_texts)
        # else:  # 30% 长文本（必测，覆盖边界）
        #     return random.choice(self.long_texts)


# =========================================================
# 自动清理系统
# =========================================================

def cleanup_tts_output():
    """按修改时间删除超出保留数量的旧 WAV 文件。"""
    files = glob.glob(
        os.path.join(OUTPUT_DIR, "*.wav")
    )

    if len(files) <= KEEP_WAV_FILES:
        return

    files.sort(
        key=lambda x: os.path.getmtime(x)
    )

    for f in files[:-KEEP_WAV_FILES]:
        try:
            os.remove(f)
        except:
            pass


def cleanup_log():
    """裁剪 CSV 日志：保留表头 + 最近 KEEP_LOG_LINES 行。"""
    if not os.path.exists(LOG_FILE):
        return

    with open(LOG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    if len(lines) <= KEEP_LOG_LINES + 1:
        return

    header = lines[0]
    body = lines[-KEEP_LOG_LINES:]

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(header)
        f.writelines(body)


# =========================================================
# 单轮执行（核心修复版）
# =========================================================

def execute_request(result_queue, session, payload, iteration):
    """
    单轮 TTS 请求：流式拉取音频、落盘 WAV、实时播放，并将指标写入队列。
    返回元组：(成功, RT, 路径, TTFT, 内存MB, CPU%, 文件KB)
    """
    response = None
    stream = None

    try:
        headers = {
            "accept": "application/json",
            "Content-Type": "application/json"
        }

        start_time = time.time()
        ttft_time = None  # 首包时间（Time To First Token/Chunk）

        response = session.post(
            URL,
            json=payload,
            headers=headers,
            stream=True,
            verify=False,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
        )

        if response.status_code != 200:
            raise Exception(
                f"HTTP状态码异常: {response.status_code}"
            )

        os.makedirs(
            OUTPUT_DIR,
            exist_ok=True
        )

        save_path = os.path.join(
            OUTPUT_DIR,
            f"stable_{iteration}_{payload.get('model')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
        )

        header_buffer = b""
        header_read = False
        sample_rate = 24000  # 默认采样率，解析 WAV 头后覆盖

        total_bytes = 0
        last_chunk_time = time.time()

        with open(save_path, "wb") as f:

            for chunk in response.iter_content(
                    chunk_size=1024
            ):

                # chunk 级超时保护（核心）：防止流式连接假死
                if time.time() - last_chunk_time > CHUNK_TIMEOUT:
                    raise Exception(
                        f"流式返回超时：{CHUNK_TIMEOUT}秒未收到新音频数据"
                    )

                if not chunk:
                    continue

                if ttft_time is None:
                    ttft_time = time.time()

                last_chunk_time = time.time()

                f.write(chunk)
                total_bytes += len(chunk)

                # =================================================
                # 实时播放逻辑
                # =================================================

                if not header_read:
                    header_buffer += chunk

                    # WAV 标准头 44 字节，24~28 偏移为采样率字段
                    if len(header_buffer) >= 44:
                        try:
                            sample_rate = struct.unpack(
                                "<I",
                                header_buffer[24:28]
                            )[0]
                        except:
                            sample_rate = 24000

                        try:
                            stream = GLOBAL_PYAUDIO.open(
                                format=pyaudio.paInt16,
                                channels=1,
                                rate=sample_rate,
                                output=True
                            )
                        except Exception as e:
                            print(
                                f"⚠️ 播放器打开失败: {e}"
                            )
                            stream = None

                        header_read = True

                        # 跳过 WAV 头，播放首段 PCM 数据
                        if stream and len(header_buffer) > 44:
                            stream.write(
                                header_buffer[44:]
                            )

                else:
                    if stream:
                        stream.write(chunk)

        if stream:
            stream.stop_stream()
            stream.close()

        if response:
            response.close()

        if total_bytes < 1024:
            raise Exception("空音频文件")

        total_time = round(
            time.time() - start_time,
            3
        )

        ttft = round(
            ttft_time - start_time,
            3
        ) if ttft_time else 0

        mem = round(
            psutil.Process(
                os.getpid()
            ).memory_info().rss / 1024 / 1024,
            2
        )

        cpu = psutil.cpu_percent(
            interval=0.1
        )

        fsize = round(
            os.path.getsize(save_path) / 1024,
            2
        )

        result_queue.put((
            True,
            total_time,
            save_path,
            ttft,
            mem,
            cpu,
            fsize
        ))

    except Exception as e:
        try:
            if stream:
                stream.stop_stream()
                stream.close()
        except:
            pass

        try:
            if response:
                response.close()
        except:
            pass

        print(
            f"⚠️ 接口异常：{str(e)[:200]}"
        )

        result_queue.put((
            False,
            0,
            "",
            0,
            0,
            0,
            0
        ))


def run_test_case(session, payload, iteration):
    """在独立守护线程中执行单轮请求，主线程 join 等待结果。"""
    result_queue = queue.Queue()

    worker = Thread(
        target=execute_request,
        args=(
            result_queue,
            session,
            payload,
            iteration
        ),
        daemon=True
    )

    worker.start()

    # 不再设置超时，直接等待线程执行完成
    worker.join()

    if not result_queue.empty():
        return result_queue.get()

    return False, 0, "", 0, 0, 0, 0


def main():
    """主压测循环：串行请求、统计指标、写日志、阶段性汇总。"""
    text_gen = TextGenerator()

    # 两种 TTS 模式随机切换：instruct2 / zero_shot
    modes = [
        {
            "model": "instruct2",
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True,
                "split": False,
                "text_frontend": True
            }
        },
        {
            "model": "zero_shot",
            "tts_params": {
                "prompt_text": "",
                "prompt_audio": "",
                "zero_shot_spk_id": "yingyeyuan_male",
                "speed": 1.0,
                "stream": True,
                "split": False,
                "text_frontend": True
            }
        }
    ]

    stats = {
        "success": 0,
        "fail": 0,
        "continuous_fail": 0,
        "rt_list": deque(maxlen=MAX_STATS_HISTORY),
        "ttft_list": deque(maxlen=MAX_STATS_HISTORY)
    }

    # 用于计算 QPS 和每 10 轮阶段性统计
    start_time_global = time.time()
    phase_start_time = time.time()
    phase_request_count = 0
    phase_success_count = 0
    phase_fail_count = 0
    phase_rt_list = []  # 阶段性响应时间列表
    phase_ttft_list = []  # 阶段性 TTFT 列表

    if not os.path.exists(LOG_FILE):
        with open(
                LOG_FILE,
                "w",
                encoding="utf-8"
        ) as f:
            f.write(
                "轮次,成功,模型,TTFT,RT,字数,文件大小KB,内存MB,CPU%,时间\n"
            )

    print("=" * 95)
    print("🏢 企业级 TTS 7×24 小时稳定性压测系统（最终版）启动")
    print("=" * 95)

    # 打印表格头部（含成功率列 + 音频大小 KB）
    print("串行次数\t\t模型\t\tTTFT\t\tRT\t\tP95(s)\t\tP99(s)\t\t长度\t\t音频KB\t\tQPS\t\t成功率\t\t平均响应")
    print("-" * 95)

    i = 0

    while RUNNING:
        i += 1
        phase_request_count += 1
        session = create_session()

        try:
            text = text_gen.get_random_text()
            mode = random.choice(modes)

            payload = {
                "model": mode["model"],
                "input": text,
                "tts_params": mode["tts_params"]
            }

            success, rt, path, ttft, mem, cpu, fsize = run_test_case(
                session,
                payload,
                i
            )

            session.close()

            if success:
                stats["success"] += 1
                phase_success_count += 1
                stats["continuous_fail"] = 0

                stats["rt_list"].append(rt)
                stats["ttft_list"].append(ttft)
                phase_rt_list.append(rt)
                phase_ttft_list.append(ttft)

                # 计算全局统计
                avg_rt = round(
                    sum(stats["rt_list"]) / len(stats["rt_list"]),
                    2
                )

                p95 = percentile(
                    stats["rt_list"],
                    95
                )

                p99 = percentile(
                    stats["rt_list"],
                    99
                )

                # 计算成功率
                success_rate = round(
                    stats["success"] / i * 100,
                    2
                )

                # 计算全局 QPS（成功次数 / 总运行时长）
                global_elapsed_time = time.time() - start_time_global
                if global_elapsed_time > 0:
                    global_qps = round(stats["success"] / global_elapsed_time, 2)
                else:
                    global_qps = 0

                # 控制台输出单轮成功明细
                print(
                    f"✅ 第{i}轮 成功 | "
                    f"模型:{mode['model']:10s} | "
                    f"TTFT:{ttft:6.3f}s | "
                    f"RT:{rt:7.3f}s | "
                    f"P95:{p95:7.3f}s | "
                    f"P99:{p99:7.3f}s | "
                    f"长度:{len(text):4d}字 | "
                    f"音频KB:{fsize:8.2f} | "
                    f"QPS:{global_qps:5.2f} | "
                    f"成功率:{success_rate:6.2f}% | "
                    f"平均响应:{avg_rt:6.2f}s"
                )

            else:
                stats["fail"] += 1
                phase_fail_count += 1
                stats["continuous_fail"] += 1

                # 计算当前成功率
                success_rate = round(
                    stats["success"] / i * 100,
                    2
                )

                print(
                    f"❌ 第{i:4d}轮 失败 | "
                    f"连续失败:{stats['continuous_fail']:2d}次 | "
                    f"成功率:{success_rate:6.2f}%"
                )

                if (
                        stats["continuous_fail"]
                        >= ALERT_CONTINUOUS_FAIL
                ):
                    send_alert(
                        f"连续失败达到 {ALERT_CONTINUOUS_FAIL} 次，请立即检查TTS服务！"
                    )

                time.sleep(
                    FAIL_SLEEP
                )

            # 追加写入 CSV 日志
            with open(
                    LOG_FILE,
                    "a",
                    encoding="utf-8"
            ) as f:
                f.write(
                    f"{i},{success},{mode['model']},{ttft},{rt},"
                    f"{len(text)},{fsize},{mem},{cpu},"
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                )

            # 每 10 个请求输出一次阶段性统计表格
            if phase_request_count >= 10:
                phase_end_time = time.time()
                phase_duration = phase_end_time - phase_start_time

                if phase_duration > 0:
                    # 计算 QPS（每秒成功请求数）
                    qps = round(phase_success_count / phase_duration, 2)
                else:
                    qps = 0

                # 计算阶段成功率
                if phase_request_count > 0:
                    phase_success_rate = round(phase_success_count / phase_request_count * 100, 2)
                else:
                    phase_success_rate = 0

                # 计算平均响应时间
                if phase_rt_list:
                    avg_response = round(sum(phase_rt_list) / len(phase_rt_list), 2)
                else:
                    avg_response = 0

                # 计算平均 TTFA（首包延迟）
                if phase_ttft_list:
                    avg_ttfa = round(sum(phase_ttft_list) / len(phase_ttft_list), 2)
                else:
                    avg_ttfa = 0

                # 计算 P95 响应
                if phase_rt_list:
                    phase_p95 = percentile(phase_rt_list, 95)
                else:
                    phase_p95 = 0

                # 错误分布（当前仅统计失败次数）
                error_dist = f"HTTP错误:{phase_fail_count}"

                print("\n" + "=" * 50)
                print("阶段统计（最近10个请求）:")
                print("并发数\t总数\t成功\t成功率\tQPS\t平均响应\t平均TTFA\tP95响应\t错误分布")
                print("-" * 50)
                print(
                    f"1\t{phase_request_count:4d}\t{phase_success_count:4d}\t{phase_success_rate:5.1f}%\t{qps:5.2f}\t{avg_response:7.2f}\t{avg_ttfa:7.2f}\t{phase_p95:7.2f}\t{error_dist:>8s}")
                print("=" * 50 + "\n")

                # 重置阶段性统计计数器
                phase_start_time = time.time()
                phase_request_count = 0
                phase_success_count = 0
                phase_fail_count = 0
                phase_rt_list = []
                phase_ttft_list = []

            cleanup_tts_output()
            cleanup_log()

            time.sleep(
                random.uniform(
                    NORMAL_SLEEP_MIN,
                    NORMAL_SLEEP_MAX
                )
            )

        except Exception as e:
            print(
                f"⚠️ 主循环异常保护：{e}"
            )

            traceback.print_exc()

            try:
                session.close()
            except:
                pass

            time.sleep(
                FAIL_SLEEP
            )


if __name__ == "__main__":
    main()
