"""
🔥测试asr-添加热词
"""

import argparse
import audioop
import base64
import csv
import io
import json
import re
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


API_URL = "http://36.111.82.53:10017/v1/audio/trans"
MODEL = "funasr-iic"
INPUT_TYPE = "stream"
LANGUAGE = "zh"
REQUEST_TIMEOUT_SECONDS = 180
RETRY_TIMES = 2
FUNASR_EXPECTED_SAMPLE_RATE = 24000

BASE_DIR = Path(__file__).resolve().parent
AUDIO_DIR = BASE_DIR / "asr_test_audio"
DEFAULT_OUTPUT_FILE = BASE_DIR / "asr_generate_hotwords_text_results.csv"

DEFAULT_HOTWORDS = [
    "瓷横担",
    "螺栓",
    "安全绳",
    "缓冲绳",
    "接地体",
    "接地棒",
    "地极",
    "接地极",
    "熔断器",
    "电压极",
    "带电体",
    "验电笔",
    "操作票",
    "绝缘挡板",
    "绝缘遮蔽",
    "绝缘毯",
    "绝缘杆",
    "脚扣",
    "引线",
    "互感器",
    "1T1",
    "12T1",
    "标志牌",
    "闸",
    "刀闸",
    "钢丝绳",
    "熔管",
    "熔丝",
    "档位",
    "开关",
    "吊物绳",
    "防跑绳",
    "钳形电流表",
    "耐张绝缘子",
    "绝缘子",
    "绝缘梯",
    "核相仪",
    "护目镜",
    "电杆",
    "杆基",
    "杆身",
    "紧线器",
    "配电箱",
    "电",
    "A相",
    "B相",
    "C相",
    "相线",
    "火线",
    "零线",
    "三相",
    "兆欧",
    "欧",
    "千伏",
    "电流",
    "电压",
    "负荷",
    "低压",
    "高压",
    "回路",
    "开路",
    "短路",
    "阻值",
    "电阻",
    "绝缘",
    "同相",
    "异相",
    "不同相",
    "交流",
    "直流",
    "相序",
    "相位",
    "跌落",
    "登杆",
    "验明",
    "合闸",
    "分闸",
    "摇测",
    "冲击试验",
    "四维站",
    "冷备用",
    "调度",
    "构件",
    "表计",
    "读数",
    "触头",
    "埋深",
    "验电",
    "竹梯",
    "合上",
    "手锤"
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量识别 asr_test_audio 中 1-100 顺序的 wav 音频")
    parser.add_argument("--url", default=API_URL, help="ASR 接口地址")
    parser.add_argument("--audio-dir", default=str(AUDIO_DIR), help="wav 音频目录")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_FILE), help="识别结果 CSV 保存路径")
    parser.add_argument("--model", default=MODEL, help="ASR 模型名")
    parser.add_argument("--start", type=int, default=1, help="开始序号，默认 1")
    parser.add_argument("--end", type=int, default=100, help="结束序号，默认 100")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个音频，调试时使用")
    parser.add_argument("--hotwords", default="", help="额外热词，支持空格、逗号、分号或换行分隔")
    parser.add_argument("--hotwords-file", default="", help="额外热词文件路径，每行或分隔符一个热词")
    parser.add_argument("--no-default-hotwords", action="store_true", help="不使用脚本内置热词，只使用命令行/文件热词")
    parser.add_argument("--language", default=LANGUAGE, help="识别语言，默认 zh")
    parser.add_argument("--data-uri", action="store_true", help="用 data:audio/wav;base64,... 格式发送音频")
    parser.add_argument(
        "--target-sample-rate",
        type=int,
        default=FUNASR_EXPECTED_SAMPLE_RATE,
        help=f"FunASR 期望输入采样率，超过该值的 wav 会在发送前降采样，默认 {FUNASR_EXPECTED_SAMPLE_RATE}",
    )
    return parser.parse_args()


def extract_audio_index(audio_path: Path) -> Optional[int]:
    """从文件名中提取序号，支持 1.wav、001.wav、audio_1.wav 等命名。"""
    match = re.search(r"\d+", audio_path.stem)
    if match is None:
        return None
    return int(match.group())


def split_hotwords(raw_hotwords: str) -> List[str]:
    """将命令行或文件里的热词文本拆成词列表，并过滤空值。"""
    if not raw_hotwords:
        return []
    return [word.strip() for word in re.split(r"[\s,，;；]+", raw_hotwords) if word.strip()]


def dedupe_preserve_order(words: List[str]) -> List[str]:
    seen = set()
    unique_words = []
    for word in words:
        normalized = word.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_words.append(word)
    return unique_words


def load_hotwords_from_file(hotwords_file: str) -> List[str]:
    if not hotwords_file:
        return []

    file_path = Path(hotwords_file)
    if not file_path.exists():
        raise FileNotFoundError(f"热词文件不存在: {file_path}")
    return split_hotwords(file_path.read_text(encoding="utf-8-sig"))


def build_hotwords(args: argparse.Namespace) -> Tuple[str, List[str]]:
    words: List[str] = []
    if not args.no_default_hotwords:
        words.extend(DEFAULT_HOTWORDS)
    words.extend(split_hotwords(args.hotwords))
    words.extend(load_hotwords_from_file(args.hotwords_file))

    unique_words = dedupe_preserve_order(words)
    return " ".join(unique_words), unique_words


def get_ordered_wav_files(audio_dir: Path, start: int, end: int) -> List[Path]:
    if not audio_dir.exists():
        raise FileNotFoundError(f"音频目录不存在: {audio_dir}")

    indexed_files: Dict[int, Path] = {}
    for audio_path in audio_dir.glob("*.wav"):
        audio_index = extract_audio_index(audio_path)
        if audio_index is None or not start <= audio_index <= end:
            continue
        indexed_files[audio_index] = audio_path

    missing_indexes = [str(i) for i in range(start, end + 1) if i not in indexed_files]
    if missing_indexes:
        print(f"[WARN] 缺少以下序号的 wav 文件: {', '.join(missing_indexes)}")

    return [indexed_files[i] for i in range(start, end + 1) if i in indexed_files]


CSV_FIELDNAMES = [
    "index",
    "audio_file",
    "original_sample_rate",
    "sent_sample_rate",
    "resampled",
    "success",
    "hotword_count",
    "text",
    "elapsed_seconds",
    "error",
    "response_json",
]


def resample_wav_if_needed(audio_path: Path, target_sample_rate: int) -> Tuple[bytes, Dict[str, Any]]:
    """返回适合 ASR 发送的 wav bytes；采样率高于目标值时在内存中降采样。"""
    with wave.open(str(audio_path), "rb") as wav_reader:
        params = wav_reader.getparams()
        original_sample_rate = wav_reader.getframerate()
        audio_frames = wav_reader.readframes(params.nframes)

    audio_info: Dict[str, Any] = {
        "original_sample_rate": original_sample_rate,
        "sent_sample_rate": original_sample_rate,
        "resampled": False,
    }

    if original_sample_rate <= target_sample_rate:
        return audio_path.read_bytes(), audio_info

    converted_frames, _ = audioop.ratecv(
        audio_frames,
        params.sampwidth,
        params.nchannels,
        original_sample_rate,
        target_sample_rate,
        None,
    )

    output_buffer = io.BytesIO()
    with wave.open(output_buffer, "wb") as wav_writer:
        wav_writer.setnchannels(params.nchannels)
        wav_writer.setsampwidth(params.sampwidth)
        wav_writer.setframerate(target_sample_rate)
        wav_writer.writeframes(converted_frames)

    audio_info["sent_sample_rate"] = target_sample_rate
    audio_info["resampled"] = True
    return output_buffer.getvalue(), audio_info


def encode_audio_bytes(audio_bytes: bytes, audio_format: str, use_data_uri: bool = False) -> str:
    b64_audio = base64.b64encode(audio_bytes).decode("utf-8")
    if use_data_uri:
        return f"data:audio/{audio_format};base64,{b64_audio}"
    return b64_audio


def build_payload(
    audio_path: Path,
    model: str,
    hotwords: str,
    language: str,
    use_data_uri: bool,
    target_sample_rate: int,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    audio_bytes, audio_info = resample_wav_if_needed(audio_path, target_sample_rate)
    audio_format = audio_path.suffix.lstrip(".").lower() or "wav"
    return {
        "model": model,
        "input_type": INPUT_TYPE,
        "input": encode_audio_bytes(audio_bytes, audio_format, use_data_uri),
        "hotwords": hotwords,
        "language": language,
    }, audio_info


def extract_text(response_body: Any) -> str:
    if isinstance(response_body, dict):
        for key in ("text", "result", "transcription"):
            value = response_body.get(key)
            if isinstance(value, str):
                return value

        data = response_body.get("data")
        if isinstance(data, dict):
            return extract_text(data)
        if isinstance(data, list):
            return " ".join(extract_text(item) for item in data).strip()

    if isinstance(response_body, list):
        return " ".join(extract_text(item) for item in response_body).strip()

    return ""


def request_asr(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    last_error = ""
    for attempt in range(1, RETRY_TIMES + 2):
        start_time = time.time()
        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            elapsed = round(time.time() - start_time, 3)
            if response.status_code == 200:
                body = response.json()
                return {
                    "success": True,
                    "text": extract_text(body),
                    "elapsed_seconds": elapsed,
                    "response_json": json.dumps(body, ensure_ascii=False),
                    "error": "",
                }

            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
        except Exception as exc:
            last_error = str(exc)

        if attempt <= RETRY_TIMES:
            time.sleep(0.5)

    return {
        "success": False,
        "text": "",
        "elapsed_seconds": "",
        "response_json": "",
        "error": last_error,
    }


def init_output_file(output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()


def append_result(output_file: Path, row: Dict[str, Any]) -> None:
    with output_file.open("a", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDNAMES)
        writer.writerow(row)


def run_batch(args: argparse.Namespace) -> None:
    audio_dir = Path(args.audio_dir)
    output_file = Path(args.output)
    audio_files = get_ordered_wav_files(audio_dir, args.start, args.end)
    hotwords_text, hotwords_list = build_hotwords(args)
    if args.limit is not None:
        audio_files = audio_files[: args.limit]

    if not audio_files:
        print(f"[ERROR] 未找到可识别的 wav 音频: {audio_dir}")
        return

    init_output_file(output_file)
    print(f"共找到 {len(audio_files)} 个 wav 音频，开始顺序调用 ASR 接口...")
    print(f"已启用 {len(hotwords_list)} 个热词")
    print(f"结果保存到: {output_file}")

    success_count = 0
    for current, audio_path in enumerate(audio_files, start=1):
        audio_index = extract_audio_index(audio_path)
        print(f"[{current}/{len(audio_files)}] 正在识别: {audio_path.name}")

        payload, audio_info = build_payload(
            audio_path,
            args.model,
            hotwords_text,
            args.language,
            args.data_uri,
            args.target_sample_rate,
        )
        if audio_info["resampled"]:
            print(
                f"  采样率转换: {audio_info['original_sample_rate']} Hz -> "
                f"{audio_info['sent_sample_rate']} Hz"
            )

        result = request_asr(
            args.url,
            payload,
        )
        if result["success"]:
            success_count += 1
            print(f"[OK] {audio_path.name}: {result['text'][:80]}")
        else:
            print(f"[ERROR] {audio_path.name}: {result['error']}")

        append_result(
            output_file,
            {
                "index": audio_index,
                "audio_file": audio_path.name,
                "original_sample_rate": audio_info["original_sample_rate"],
                "sent_sample_rate": audio_info["sent_sample_rate"],
                "resampled": audio_info["resampled"],
                "success": result["success"],
                "hotword_count": len(hotwords_list),
                "text": result["text"],
                "elapsed_seconds": result["elapsed_seconds"],
                "error": result["error"],
                "response_json": result["response_json"],
            },
        )

    print(f"\n完成：成功 {success_count}/{len(audio_files)}，结果已保存到 {output_file}")


if __name__ == "__main__":
    run_batch(parse_args())
