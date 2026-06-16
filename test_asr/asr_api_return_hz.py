"""
批量测试 ASR：扫描目录中的 WAV/MP4 文件并按需降采样。
"""

import argparse
import audioop
import base64
import csv
import io
import json
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests


API_URL = "http://36.111.82.53:10017/v1/audio/trans"
MODEL = "funasr-iic"
INPUT_TYPE = "stream"
LANGUAGE = "zh"
REQUEST_TIMEOUT_SECONDS = 3600
RETRY_TIMES = 2
FUNASR_EXPECTED_SAMPLE_RATE = 24000
SUPPORTED_AUDIO_SUFFIXES = {".wav", ".mp4"}

BASE_DIR = Path(__file__).resolve().parent
AUDIO_DIR = BASE_DIR / "asr_test_audio"
DEFAULT_OUTPUT_FILE = BASE_DIR / "asr_text_results.csv"


CSV_FIELDNAMES = [
    "file_order",
    "audio_file",
    "original_format",
    "sent_format",
    "original_sample_rate",
    "sent_sample_rate",
    "resampled",
    "success",
    "text",
    "elapsed_seconds",
    "error",
    "response_json",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量识别指定目录中的 WAV/MP4 音频文件")
    parser.add_argument("--url", default=API_URL, help="ASR 接口地址")
    parser.add_argument("--audio-dir", default=str(AUDIO_DIR), help="音频目录，支持 .wav 和 .mp4")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_FILE), help="识别结果 CSV 保存路径")
    parser.add_argument("--model", default=MODEL, help="ASR 模型名")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个音频，调试时使用")
    parser.add_argument("--language", default=LANGUAGE, help="识别语言，默认 zh")
    parser.add_argument("--data-uri", action="store_true", help="用 data:audio/<format>;base64,... 格式发送音频")
    parser.add_argument(
        "--target-sample-rate",
        type=int,
        default=FUNASR_EXPECTED_SAMPLE_RATE,
        help=f"目标采样率；高于该值的音频会在发送前内存降采样，默认 {FUNASR_EXPECTED_SAMPLE_RATE}",
    )
    return parser.parse_args()


def get_audio_files(audio_dir: Path) -> List[Path]:
    if not audio_dir.exists():
        raise FileNotFoundError(f"音频目录不存在: {audio_dir}")
    if not audio_dir.is_dir():
        raise NotADirectoryError(f"音频路径不是目录: {audio_dir}")

    return sorted(
        (
            audio_path
            for audio_path in audio_dir.rglob("*")
            if audio_path.is_file() and audio_path.suffix.lower() in SUPPORTED_AUDIO_SUFFIXES
        ),
        key=lambda path: str(path.relative_to(audio_dir)).lower(),
    )


def encode_audio_bytes(audio_bytes: bytes, audio_format: str, use_data_uri: bool = False) -> str:
    b64_audio = base64.b64encode(audio_bytes).decode("utf-8")
    if use_data_uri:
        return f"data:audio/{audio_format};base64,{b64_audio}"
    return b64_audio


def build_audio_info(
    audio_path: Path,
    original_sample_rate: int,
    sent_sample_rate: int,
    sent_format: str,
) -> Dict[str, Any]:
    original_format = audio_path.suffix.lstrip(".").lower()
    return {
        "original_format": original_format,
        "sent_format": sent_format,
        "original_sample_rate": original_sample_rate,
        "sent_sample_rate": sent_sample_rate,
        "resampled": original_sample_rate > sent_sample_rate,
    }


def read_wav_for_asr(audio_path: Path, target_sample_rate: int) -> Tuple[bytes, Dict[str, Any]]:
    """读取 WAV；采样率高于目标值时在内存中降采样后重新封装为 WAV。"""
    with wave.open(str(audio_path), "rb") as wav_reader:
        params = wav_reader.getparams()
        original_sample_rate = wav_reader.getframerate()
        audio_frames = wav_reader.readframes(params.nframes)

    if original_sample_rate <= target_sample_rate:
        audio_info = build_audio_info(audio_path, original_sample_rate, original_sample_rate, "wav")
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

    audio_info = build_audio_info(audio_path, original_sample_rate, target_sample_rate, "wav")
    return output_buffer.getvalue(), audio_info


def ensure_command_exists(command_name: str) -> None:
    command = str(command_name)
    try:
        result = subprocess.run([command, "-version"], capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"处理 MP4 需要安装并配置 {command} 到 PATH") from exc

    if result.returncode != 0:
        raise RuntimeError(f"处理 MP4 需要安装并配置可用的 {command}")


def get_media_sample_rate(audio_path: Path) -> int:
    ensure_command_exists("ffprobe")
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        error_text = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"读取 MP4 采样率失败: {error_text}")

    sample_rate_text = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not sample_rate_text.isdigit():
        raise RuntimeError(f"MP4 中未找到可用音频流: {audio_path}")
    return int(sample_rate_text)


def read_mp4_for_asr(audio_path: Path, target_sample_rate: int) -> Tuple[bytes, Dict[str, Any]]:
    """通过 ffmpeg 从 MP4 读取音频；必要时降采样，并以内存 WAV 发送。"""
    ensure_command_exists("ffmpeg")
    original_sample_rate = get_media_sample_rate(audio_path)
    sent_sample_rate = min(original_sample_rate, target_sample_rate)

    command = ["ffmpeg", "-v", "error", "-i", str(audio_path), "-vn"]
    if original_sample_rate > target_sample_rate:
        command.extend(["-ar", str(target_sample_rate)])
    command.extend(["-f", "wav", "pipe:1"])

    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        error_text = result.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(f"读取 MP4 音频失败: {error_text}")

    audio_info = build_audio_info(audio_path, original_sample_rate, sent_sample_rate, "wav")
    return result.stdout, audio_info


def read_audio_for_asr(audio_path: Path, target_sample_rate: int) -> Tuple[bytes, Dict[str, Any]]:
    suffix = audio_path.suffix.lower()
    if suffix == ".wav":
        return read_wav_for_asr(audio_path, target_sample_rate)
    if suffix == ".mp4":
        return read_mp4_for_asr(audio_path, target_sample_rate)
    raise ValueError(f"不支持的音频格式: {audio_path.suffix}")


def build_payload(
    audio_path: Path,
    model: str,
    language: str,
    use_data_uri: bool,
    target_sample_rate: int,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    audio_bytes, audio_info = read_audio_for_asr(audio_path, target_sample_rate)
    return {
        "model": model,
        "input_type": INPUT_TYPE,
        "input": encode_audio_bytes(audio_bytes, audio_info["sent_format"], use_data_uri),
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
    audio_files = get_audio_files(audio_dir)

    if args.limit is not None:
        audio_files = audio_files[: args.limit]

    if not audio_files:
        print(f"[ERROR] 未找到可识别的 WAV/MP4 音频: {audio_dir}")
        return

    init_output_file(output_file)
    print(f"共找到 {len(audio_files)} 个 WAV/MP4 音频，开始顺序调用 ASR 接口...")
    print(f"结果保存到: {output_file}")

    success_count = 0
    for current, audio_path in enumerate(audio_files, start=1):
        audio_display_path = str(audio_path.relative_to(audio_dir))
        print(f"[{current}/{len(audio_files)}] 正在识别: {audio_display_path}")

        try:
            payload, audio_info = build_payload(
                audio_path,
                args.model,
                args.language,
                args.data_uri,
                args.target_sample_rate,
            )
        except Exception as exc:
            print(f"[ERROR] {audio_display_path}: {exc}")
            append_result(
                output_file,
                {
                    "file_order": current,
                    "audio_file": audio_display_path,
                    "original_format": audio_path.suffix.lstrip(".").lower(),
                    "sent_format": "",
                    "original_sample_rate": "",
                    "sent_sample_rate": "",
                    "resampled": False,
                    "success": False,
                    "text": "",
                    "elapsed_seconds": "",
                    "error": str(exc),
                    "response_json": "",
                },
            )
            continue

        if audio_info["resampled"]:
            print(
                f"  采样率转换: {audio_info['original_sample_rate']} Hz -> "
                f"{audio_info['sent_sample_rate']} Hz"
            )

        result = request_asr(args.url, payload)
        if result["success"]:
            success_count += 1
            print(f"[OK] {audio_display_path}: {result['text'][:80]}")
        else:
            print(f"[ERROR] {audio_display_path}: {result['error']}")

        append_result(
            output_file,
            {
                "file_order": current,
                "audio_file": audio_path.name,
                "original_format": audio_info["original_format"],
                "sent_format": audio_info["sent_format"],
                "original_sample_rate": audio_info["original_sample_rate"],
                "sent_sample_rate": audio_info["sent_sample_rate"],
                "resampled": audio_info["resampled"],
                "success": result["success"],
                "text": result["text"],
                "elapsed_seconds": result["elapsed_seconds"],
                "error": result["error"],
                "response_json": result["response_json"],
            },
        )

    print(f"\n完成：成功 {success_count}/{len(audio_files)}，结果已保存到 {output_file}")


if __name__ == "__main__":
    run_batch(parse_args())
