"""
批量调用 TTS 接口，根据固定提示文本生成 wav 音频。
说明：
    PROMPT_TEXTS 有多少条，就只生成多少个音频。
    默认使用 instruct2 稳定生成短文本。
    --model both 表示每条提示词随机选择 instruct2 或 zero_shot，不是每条生成两个音频。
"""

import argparse
import csv
import re
import random
import struct
import threading
import time
import traceback
import wave
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# =========================================================
# 1. 只需要改这里：补充你的 100 段提示词
# =========================================================
PROMPT_TEXTS = [
    "未完成检验无线高压发射器绝缘杆",
    "未完成戴绝缘手套",
    "未完成穿绝缘靴",
    "未完成与带电体保持距离",
    "未完成两测量人保持距离",
    "未完成同相检测",
    "未完成异相检测",
    "未完成将发射器接触在测量位置",
    "未完成测量Aa核相",
    "未完成记录结果",
    "未完成判断结果正确",
    "未完成拆除“止步，高压危险!”标志牌",
    "未完成拆除“从此进出!”标志牌",
    "未完成拆除“在此工作!”标志牌",
    "未完成人员撤离",
    "未完成清理现场",
    "未完成摇表检查",
    "未完成静态调零",
    "未完成动态调零",
    "未完成检查绝缘靴",
    "未完成检查手锤、临时地极",
    "未完成悬挂“止步，高压危险!”标志牌",
    "未完成悬挂“从此进出!”标志牌",
    "未完成装设临时地极",
    "未完成断开接地装置与设备的连接",
    "未完成电流接地棒、电压接地棒与被测接地体应成直线排列",
    "未完成临时地极距离要求",
    "未完成电压极与电流引线距离",
    "未完成打入接地棒",
    "未完成清除铁锈",
    "未完成E端接在被测接地装置的接地引线上",
    "未完成P端接电压接地棒测试线",
    "未完成C端接电流接地棒测试线",
    "未完成测量接地网接地电阻",
    "未完成断开低压侧分路开关",
    "未完成断开低压侧总开关",
    "未完成拉开低压侧总刀闸",
    "未完成左（右）脚扣无重叠交叉",
    "未完成杆上作业不失去安全带保护",
    "未完成安全带挂在主杆或牢固构件上、高挂低用",
    "未完成挂安全绳时安全带要受力不许脱落",
    "未完成站位正确",
    "未完成吊物绳绑扎在牢固构件",
    "未完成用吊绳绑扎防止掉落",
    "未完成无跌落物",
    "未完成物件传递、绑扎可靠",
    "未完成吊物过程无碰撞",
    "未完成装上跌落式熔断器",
    "未完成拆除安全绳",
    "未完成悬挂“禁止合闸，线路有人工作”标志牌",
    "未完成拉开跌落式熔断器",
    "未完成用绝缘操作棒取下熔管",
    "未完成检查熔管无弯曲",
    "未完成检查触头间接触良",
    "未完成检查各部件的组装良好",
    "未完成更换熔丝",
    "未完成装上熔管",
    "未完成合上跌落式熔断器",
    "未完成安全带冲击试验",
    "未完成安全绳冲击试验",
    "未完成脚扣冲击试验",
    "未完成上杆过程不失去安全带保护",
    "注意正确逐相驳接",
    "注意工具及扎线无碰触墙体",
    "注意人体无碰触墙体",
    "注意人体无同时接触两根裸露的线头(导线)",
    "注意竹梯摆放角度符合要求",
    "注意做好绝缘遮蔽措施",
    "注意超过2米有正确使用安全带",
    "注意正确佩戴护目镜",
    "注意驳接完一相必须立即恢复绝缘",
    "注意绝缘胶布使用规范",
    "注意绝缘胶布包缠不小于两层",
    "注意绝缘层破口长度合适",
    "注意绑扎长度符合要求",
    "注意绑扎应顺着导线绞向进行",
    "注意零相接头连接紧密牢固",
    "注意火相接头连接紧密牢固",
    "注意动作规范正确",
    "注意无重复多余动作发生",
    "注意在工作地点适当位置悬挂“在此工作!”标志牌",
    "注意正确利用电笔逐相进行感应验电",
    "注意根据验电笔的感应电显示结果及根据低压线路的相色分清相线并记录",
    "注意根据数字式电笔的感应电显示结果及根据低压线路的相色分清零线并记录",
    "注意正确拆除开关下端出线，先拆出相线，后拆除零线",
    "注意对拆出相线、零线做绝缘遮蔽",
    "注意正确拆除开关上端（电源侧）出线，先拆出相线，后拆除零线",
    "注意及时对拆出相线、零线做绝缘遮蔽",
    "注意正确拆除开关",
    "注意检查开关安装垂直，安装牢固",
    "注意检查开关在分闸位置",
    "注意安装开关上端（电源侧）接线牢固",
    "注意未接出线，试分合开关，空载运行正常",
    "注意",
    "试分合开关，检查开关在分闸位置",
    "注意装设开关下端接线",
    "注意先接零线后接火线",
    "注意工具及人体无碰触墙体、金属箱体",
    "注意工作中不得同时接触两根裸露的线头(导线)",
    "注意无发生乱放置工具材料现象"
]
# =========================================================
# 2. 接口配置
# =========================================================
URL = "http://117.68.66.99:10014/v1/audio/speech"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "tts_output"
LOG_FILE = BASE_DIR / "tts_batch_generate_log.csv"

DEFAULT_MODEL = "instruct2"  # 默认只用短文本更稳定的 instruct2；需要随机模型时再传 --model both
DEFAULT_SPEAKER = "random"  # random / 具体 spk_id

END_OF_PROMPT = "<|endofprompt|>"
DEFAULT_INSTRUCT_TEXT = f"You are a helpful assistant. 很自然地说{END_OF_PROMPT}"

CONCURRENCY = 1
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 180
NET_RETRY_TIMES = 5
RETRY_SLEEP_SECONDS = 0.5
MIN_AUDIO_DURATION = 0.3
MIN_RMS_AMPLITUDE = 20
DEFAULT_TTS_STREAM = True
END_PAUSE_MARKS = "。。。。"
OUTPUT_TAIL_PADDING_SECONDS = 1.0


SPEAKER_PROFILES = [
    {"spk_id": "企业中年女", "created_at": "predefined", "has_audio": True},
    {"spk_id": "企业中年男", "created_at": "predefined", "has_audio": True},
    {"spk_id": "居民中年女", "created_at": "predefined", "has_audio": True},
    {"spk_id": "居民中年男", "created_at": "predefined", "has_audio": True},
    {"spk_id": "ref_female_kh", "created_at": "predefined", "has_audio": True},
    {"spk_id": "ref_male_kf", "created_at": "predefined", "has_audio": True},
    {"spk_id": "yingyeyuan_male", "created_at": "predefined", "has_audio": True},
    {"spk_id": "yingyeyuan_female", "created_at": "predefined", "has_audio": True},
    {"spk_id": "kehu_male_a", "created_at": "predefined", "has_audio": True},
    {"spk_id": "kehu_male_b", "created_at": "predefined", "has_audio": True},
    {"spk_id": "kehu_male_c", "created_at": "predefined", "has_audio": True},
    {"spk_id": "kehu_female_a", "created_at": "predefined", "has_audio": True},
    {"spk_id": "kehu_female_b", "created_at": "predefined", "has_audio": True},
    {"spk_id": "kehu_female_c", "created_at": "predefined", "has_audio": True},
]
SPEAKER_IDS = [item["spk_id"] for item in SPEAKER_PROFILES if item.get("has_audio")]
ZERO_SHOT_UNSUPPORTED_SPEAKERS = {"ref_female_kh", "ref_male_kf"}
MODEL_SPEAKER_IDS = {
    "instruct2": SPEAKER_IDS,
    "zero_shot": [speaker_id for speaker_id in SPEAKER_IDS if speaker_id not in ZERO_SHOT_UNSUPPORTED_SPEAKERS],
}


MODEL_TEMPLATES = {
    "instruct2": {
        "model": "instruct2",
        "tts_params": {
            "instruct_text": DEFAULT_INSTRUCT_TEXT,
            "prompt_audio": "",
            "zero_shot_spk_id": "",
            "speed": 1.0,
            "stream": DEFAULT_TTS_STREAM,
            "split": True,
            "text_frontend": True,
        },
    },
    "zero_shot": {
        "model": "zero_shot",
        "tts_params": {
            "prompt_text": "",
            "prompt_audio": "",
            "zero_shot_spk_id": "",
            "speed": 1.0,
            "stream": DEFAULT_TTS_STREAM,
            "split": True,
            "text_frontend": True,
        },
    },
}


@dataclass(frozen=True)
class TTSJob:
    task_id: int
    prompt_index: int
    text: str
    model: str
    speaker_id: str


thread_local = threading.local()
log_lock = threading.Lock()


def parse_bool(value: str) -> bool:
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "y"}:
        return True
    if value in {"0", "false", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError(f"无法识别布尔值：{value}")


def create_session(pool_size: int) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.proxies = {}
    adapter = HTTPAdapter(max_retries=0, pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_thread_session(pool_size: int) -> requests.Session:
    session = getattr(thread_local, "session", None)
    if session is None:
        session = create_session(pool_size)
        thread_local.session = session
    return session


def sanitize_filename(value: str, max_length: int = 40) -> str:
    value = re.sub(r'[\\/:*?"<>|\s]+', "_", value.strip())
    value = value.strip("._")
    return (value or "empty")[:max_length]


def check_wav_valid(file_path: Path) -> bool:
    try:
        with wave.open(str(file_path), "rb") as audio_file:
            return audio_file.getnframes() > 100
    except (wave.Error, OSError):
        return False


def find_wav_offsets(data: bytes) -> List[int]:
    offsets = []
    start = 0
    while True:
        offset = data.find(b"RIFF", start)
        if offset < 0:
            return offsets
        if offset + 12 <= len(data) and data[offset + 8 : offset + 12] == b"WAVE":
            offsets.append(offset)
            start = offset + 12
        else:
            start = offset + 4


def parse_wav_segment(segment: bytes) -> Dict[str, bytes]:
    chunks: Dict[str, bytes] = {}
    offset = 12
    while offset + 8 <= len(segment):
        chunk_id = segment[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", segment, offset + 4)[0]
        chunk_start = offset + 8
        chunk_end = min(chunk_start + chunk_size, len(segment))
        if chunk_end < chunk_start:
            break
        if chunk_id == b"fmt ":
            chunks["fmt"] = segment[chunk_start:chunk_end]
        elif chunk_id == b"data":
            # 流式 WAV 的 data 长度经常不准；当前片段到下一个 RIFF 前都是真实音频数据。
            chunks["data"] = segment[chunk_start:]
            break
        offset = chunk_start + chunk_size + (chunk_size % 2)
    return chunks


def wav_block_align(fmt_chunk: bytes) -> int:
    if len(fmt_chunk) >= 14:
        return max(1, struct.unpack_from("<H", fmt_chunk, 12)[0])
    return 1


def trim_to_full_frames(audio_data: bytes, fmt_chunk: bytes) -> bytes:
    block_align = wav_block_align(fmt_chunk)
    remainder = len(audio_data) % block_align
    return audio_data[:-remainder] if remainder else audio_data


def make_silence(fmt_chunk: bytes, seconds: float) -> bytes:
    if seconds <= 0 or len(fmt_chunk) < 16:
        return b""

    channels = max(1, struct.unpack_from("<H", fmt_chunk, 2)[0])
    sample_rate = max(1, struct.unpack_from("<I", fmt_chunk, 4)[0])
    block_align = wav_block_align(fmt_chunk)
    bits_per_sample = max(8, struct.unpack_from("<H", fmt_chunk, 14)[0])
    sample_width = max(1, bits_per_sample // 8)
    frame_count = int(sample_rate * seconds)
    if frame_count <= 0:
        return b""

    if bits_per_sample == 8:
        frame = b"\x80" * channels
    else:
        frame = b"\x00" * block_align
    return (frame * frame_count)[: frame_count * block_align]


def make_wav_chunk(chunk_id: bytes, payload: bytes) -> bytes:
    padding = b"\x00" if len(payload) % 2 else b""
    return chunk_id + struct.pack("<I", len(payload)) + payload + padding


def repair_wav_header(file_path: Path) -> None:
    data = file_path.read_bytes()
    wav_offsets = find_wav_offsets(data)
    if not wav_offsets:
        return

    fmt_chunk = b""
    data_chunks = []
    for index, offset in enumerate(wav_offsets):
        next_offset = wav_offsets[index + 1] if index + 1 < len(wav_offsets) else len(data)
        chunks = parse_wav_segment(data[offset:next_offset])
        if not chunks.get("fmt") or not chunks.get("data"):
            continue
        if not fmt_chunk:
            fmt_chunk = chunks["fmt"]
        elif chunks["fmt"] != fmt_chunk:
            raise RuntimeError("接口返回了多个音频格式不同的 WAV 片段，无法安全合并")
        data_chunks.append(trim_to_full_frames(chunks["data"], fmt_chunk))

    if not fmt_chunk or not data_chunks:
        return

    merged_data = b"".join(data_chunks) + make_silence(fmt_chunk, OUTPUT_TAIL_PADDING_SECONDS)
    fmt_part = make_wav_chunk(b"fmt ", fmt_chunk)
    data_part = make_wav_chunk(b"data", merged_data)
    riff_size = 4 + len(fmt_part) + len(data_part)
    file_path.write_bytes(b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + fmt_part + data_part)


def analyze_wav(file_path: Path) -> Dict[str, float]:
    with wave.open(str(file_path), "rb") as audio_file:
        channels = audio_file.getnchannels()
        sample_width = audio_file.getsampwidth()
        frame_rate = audio_file.getframerate()
        frame_count = audio_file.getnframes()
        frames = audio_file.readframes(frame_count)

    header_duration = frame_count / frame_rate if frame_rate else 0.0
    data_bytes = max(file_path.stat().st_size - 44, 0)
    bytes_per_second = frame_rate * channels * sample_width
    size_duration = data_bytes / bytes_per_second if bytes_per_second else 0.0
    # 部分流式 WAV 的 header 帧数不可信，短文本优先按实际文件大小估算时长。
    duration = size_duration if size_duration > 0 else header_duration
    rms = 0.0
    if frames and sample_width == 2:
        sample_count = len(frames) // 2
        if sample_count:
            samples = struct.unpack("<" + "h" * sample_count, frames)
            sum_squares = sum(sample * sample for sample in samples)
            rms = (sum_squares / sample_count) ** 0.5

    return {
        "duration": round(duration, 3),
        "channels": channels,
        "sample_width": sample_width,
        "frame_rate": frame_rate,
        "rms": round(rms, 2),
    }


def format_response_preview(data: bytes, max_length: int = 300) -> str:
    if not data:
        return "<empty>"
    return data[:max_length].decode("utf-8", errors="replace").replace("\r", "\\r").replace("\n", "\\n")


def normalize_tts_text(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    # 短文本末尾容易被 TTS 吞掉最后一个字，给真实文本补约 2 秒停顿保护尾字。
    if text[-1] in "，,、：:":
        return f"{text}。{END_PAUSE_MARKS}"
    if text[-1] in "。！？.!?；;":
        return f"{text}{END_PAUSE_MARKS}"
    return f"{text}。{END_PAUSE_MARKS}"


def iter_valid_prompts(prompts: Iterable[str]) -> List[str]:
    valid_prompts = [text.strip() for text in prompts if isinstance(text, str) and text.strip()]
    if not valid_prompts:
        raise ValueError("PROMPT_TEXTS 为空，请先在代码顶部补充你的 100 段提示词。")
    return valid_prompts


def resolve_model_names(model_arg: str) -> List[str]:
    if model_arg == "both":
        return ["instruct2", "zero_shot"]
    return [model_arg]


def resolve_speaker_ids(speaker_arg: str) -> Optional[List[str]]:
    if speaker_arg in {"random", "round_robin", "all"}:
        return None
    if speaker_arg not in SPEAKER_IDS:
        raise ValueError(f"音色不存在：{speaker_arg}，可选音色：{', '.join(SPEAKER_IDS)}")
    return [speaker_arg]


def build_jobs(prompts: List[str], model_names: List[str], speaker_arg: str) -> List[TTSJob]:
    return [choose_job(prompt_index, text, model_names, speaker_arg) for prompt_index, text in enumerate(prompts, start=1)]


def choose_job(prompt_index: int, text: str, model_names: List[str], speaker_arg: str) -> TTSJob:
    if speaker_arg in {"random", "round_robin", "all"}:
        candidate_models = model_names
    else:
        if speaker_arg not in SPEAKER_IDS:
            raise ValueError(f"音色不存在：{speaker_arg}，可选音色：{', '.join(SPEAKER_IDS)}")
        candidate_models = [model_name for model_name in model_names if speaker_arg in MODEL_SPEAKER_IDS[model_name]]
        if not candidate_models:
            unsupported = ", ".join(sorted(ZERO_SHOT_UNSUPPORTED_SPEAKERS))
            raise ValueError(f"当前模型不支持音色 {speaker_arg}。已知 zero_shot 不稳定音色：{unsupported}")

    model_name = random.choice(candidate_models)
    supported_speakers = MODEL_SPEAKER_IDS[model_name]

    if speaker_arg in {"random", "round_robin", "all"}:
        speaker_id = random.choice(supported_speakers)
    else:
        speaker_id = speaker_arg

    return TTSJob(
        task_id=prompt_index,
        prompt_index=prompt_index,
        text=text,
        model=model_name,
        speaker_id=speaker_id,
    )


def build_payload(job: TTSJob, split: bool, stream: bool, speed: float) -> Dict:
    template = deepcopy(MODEL_TEMPLATES[job.model])
    tts_params = template["tts_params"]
    tts_text = normalize_tts_text(job.text)
    tts_params["prompt_audio"] = job.speaker_id
    tts_params["zero_shot_spk_id"] = job.speaker_id
    tts_params["split"] = split
    tts_params["stream"] = stream
    tts_params["speed"] = speed

    return {
        "model": template["model"],
        "input": tts_text,
        "tts_params": tts_params,
    }


def make_output_path(job: TTSJob) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    speaker_name = sanitize_filename(job.speaker_id)
    text_name = sanitize_filename(job.text[:20])
    file_name = f"{job.prompt_index:03d}_{job.task_id:04d}_{job.model}_{speaker_name}_{text_name}_{timestamp}.wav"
    return OUTPUT_DIR / file_name


def write_log(row: Dict) -> None:
    fieldnames = [
        "task_id",
        "prompt_index",
        "success",
        "model",
        "speaker_id",
        "text_len",
        "tts_text",
        "ttft",
        "elapsed",
        "duration",
        "rtf",
        "rms",
        "file_kb",
        "file_path",
        "created_at",
        "error",
    ]
    with log_lock:
        file_exists = LOG_FILE.exists()
        with LOG_FILE.open("a", encoding="utf-8-sig", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)


def request_tts(
    job: TTSJob,
    url: str,
    split: bool,
    stream: bool,
    speed: float,
    pool_size: int,
) -> Dict:
    start_time = time.time()
    ttft = 0.0
    output_path = make_output_path(job)
    tts_text = normalize_tts_text(job.text)
    payload = build_payload(job, split=split, stream=stream, speed=speed)
    headers = {"accept": "application/json", "Content-Type": "application/json"}
    response = None

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        session = get_thread_session(pool_size)
        response = session.post(
            url,
            json=payload,
            headers=headers,
            stream=True,
            verify=False,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )

        if response.status_code != 200:
            body = response.text[:300] if response.text else ""
            raise RuntimeError(f"HTTP {response.status_code}: {body}")

        total_bytes = 0
        first_bytes = bytearray()
        first_chunk_time = None
        last_chunk_time = time.time()

        with output_path.open("wb") as audio_file:
            for chunk in response.iter_content(chunk_size=1024):
                if not chunk:
                    continue
                if first_chunk_time is None:
                    first_chunk_time = time.time()
                last_chunk_time = time.time()
                if len(first_bytes) < 4096:
                    first_bytes.extend(chunk[: 4096 - len(first_bytes)])
                audio_file.write(chunk)
                total_bytes += len(chunk)

        if total_bytes < 1024:
            content_type = response.headers.get("Content-Type", "")
            preview = format_response_preview(bytes(first_bytes))
            raise RuntimeError(
                f"返回音频过小，疑似服务端空响应或错误响应 | "
                f"HTTP={response.status_code} Content-Type={content_type} Bytes={total_bytes} Preview={preview}"
            )
        repair_wav_header(output_path)
        if not check_wav_valid(output_path):
            preview = format_response_preview(bytes(first_bytes))
            raise RuntimeError(f"WAV 文件校验失败，疑似损坏或静音 | Bytes={total_bytes} Preview={preview}")

        elapsed = round(time.time() - start_time, 3)
        ttft = round(first_chunk_time - start_time, 3) if first_chunk_time else 0.0
        audio_info = analyze_wav(output_path)
        duration = audio_info["duration"]
        rms = audio_info["rms"]
        rtf = round(elapsed / duration, 3) if duration > 0 else 999.0
        if duration < MIN_AUDIO_DURATION:
            raise RuntimeError(f"音频时长过短，疑似空音频 | duration={duration}s")
        if rms < MIN_RMS_AMPLITUDE:
            raise RuntimeError(f"音频振幅过低，疑似静音 | rms={rms}")

        return {
            "task_id": job.task_id,
            "prompt_index": job.prompt_index,
            "success": True,
            "model": job.model,
            "speaker_id": job.speaker_id,
            "text_len": len(job.text),
            "tts_text": tts_text,
            "ttft": ttft,
            "elapsed": elapsed,
            "duration": duration,
            "rtf": rtf,
            "rms": rms,
            "file_kb": round(output_path.stat().st_size / 1024, 2),
            "file_path": str(output_path),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": "",
        }
    except Exception as exc:
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        return {
            "task_id": job.task_id,
            "prompt_index": job.prompt_index,
            "success": False,
            "model": job.model,
            "speaker_id": job.speaker_id,
            "text_len": len(job.text),
            "tts_text": tts_text,
            "ttft": ttft,
            "elapsed": round(time.time() - start_time, 3),
            "duration": 0,
            "rtf": 0,
            "rms": 0,
            "file_kb": 0,
            "file_path": "",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": f"{str(exc)[:300]} | {traceback.format_exc(limit=2).strip()[:500]}",
        }
    finally:
        if response is not None:
            response.close()


def request_tts_with_retry(
    job: TTSJob,
    url: str,
    split: bool,
    stream: bool,
    speed: float,
    pool_size: int,
) -> Dict:
    last_result = {}
    for attempt in range(1, NET_RETRY_TIMES + 2):
        last_result = request_tts(job, url, split, stream, speed, pool_size)
        if last_result["success"]:
            return last_result
        if attempt <= NET_RETRY_TIMES:
            time.sleep(RETRY_SLEEP_SECONDS * attempt)
    return last_result


def generate_prompt_with_retry(
    prompt_index: int,
    text: str,
    model_names: List[str],
    speaker_arg: str,
    url: str,
    split: bool,
    stream: bool,
    speed: float,
    pool_size: int,
) -> Dict:
    last_result = {}
    for attempt in range(1, NET_RETRY_TIMES + 2):
        job = choose_job(prompt_index, text, model_names, speaker_arg)
        last_result = request_tts(job, url, split, stream, speed, pool_size)
        if last_result["success"]:
            return last_result
        if attempt <= NET_RETRY_TIMES:
            time.sleep(RETRY_SLEEP_SECONDS * attempt)
    return last_result


def print_plan(jobs: List[TTSJob], model_names: List[str], speaker_arg: str, concurrency: int) -> None:
    print("=" * 90)
    print("TTS 批量合成任务")
    if len(model_names) == 1:
        print(f"模型: {model_names[0]}")
    else:
        print(f"模型: {', '.join(model_names)}（每条提示词随机选 1 个模型）")
    print(f"音色策略: {speaker_arg}")
    print(f"任务数: {len(jobs)}（1 条提示词只生成 1 个音频）")
    print(f"并发: {concurrency}")
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"日志文件: {LOG_FILE}")
    print("前 10 个任务预览:")
    for job in jobs[:10]:
        print(f"  {job.prompt_index:03d}. model={job.model}, speaker={job.speaker_id}, text={job.text}")
    print("=" * 90)


def main() -> None:
    parser = argparse.ArgumentParser(description="固定提示词批量生成 TTS 音频")
    parser.add_argument("--url", default=URL, help="TTS 接口地址")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        choices=["instruct2", "zero_shot", "both"],
        help="使用哪个模型；默认 instruct2；both 表示每条提示词随机选 instruct2 或 zero_shot",
    )
    parser.add_argument("--speaker", default=DEFAULT_SPEAKER, help="random 或具体 spk_id")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY, help="并发请求数")
    parser.add_argument("--limit", type=int, default=0, help="只生成前 N 条提示词，0 表示不限制")
    parser.add_argument("--split", type=parse_bool, default=True, help="tts_params.split")
    parser.add_argument("--stream", type=parse_bool, default=DEFAULT_TTS_STREAM, help="tts_params.stream")
    parser.add_argument("--speed", type=float, default=1.0, help="tts_params.speed")
    parser.add_argument("--seed", type=int, default=None, help="随机种子；不填则每次随机组合都不同")
    parser.add_argument("--dry-run", action="store_true", help="只打印任务计划，不发请求")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    prompts = iter_valid_prompts(PROMPT_TEXTS)
    if args.limit > 0:
        prompts = prompts[: args.limit]

    model_names = resolve_model_names(args.model)
    jobs = build_jobs(prompts, model_names, args.speaker)
    concurrency = max(1, args.concurrency)

    print_plan(jobs, model_names, args.speaker, concurrency)
    if args.dry_run:
        return

    success_count = 0
    fail_count = 0
    started_at = time.time()

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                generate_prompt_with_retry,
                index,
                text,
                model_names,
                args.speaker,
                args.url,
                args.split,
                args.stream,
                args.speed,
                concurrency,
            )
            for index, text in enumerate(prompts, start=1)
        ]

        for future in as_completed(futures):
            result = future.result()
            write_log(result)
            if result["success"]:
                success_count += 1
                print(
                    f"[OK] task={result['task_id']} prompt={result['prompt_index']} "
                    f"model={result['model']} speaker={result['speaker_id']} "
                    f"rt={result['elapsed']}s duration={result['duration']}s "
                    f"rtf={result['rtf']} rms={result['rms']} file={result['file_path']}"
                )
            else:
                fail_count += 1
                print(
                    f"[FAIL] task={result['task_id']} prompt={result['prompt_index']} "
                    f"model={result['model']} speaker={result['speaker_id']} "
                    f"error={result['error'][:160]}"
                )

    elapsed = round(time.time() - started_at, 3)
    total = success_count + fail_count
    success_rate = round(success_count / total * 100, 2) if total else 0
    print("=" * 90)
    print(f"完成：总数={total} 成功={success_count} 失败={fail_count} 成功率={success_rate}% 总耗时={elapsed}s")
    print(f"音频目录：{OUTPUT_DIR}")
    print(f"日志文件：{LOG_FILE}")


if __name__ == "__main__":
    main()
