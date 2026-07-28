import argparse
import os
import queue
import struct
import threading
import time

import requests

try:
    import pyaudio
except (ImportError, OSError) as exc:
    # 播放是可选功能；请求和 WAV 文件保存不应因本机没有 PortAudio 而失败。
    pyaudio = None
    PYAUDIO_IMPORT_ERROR = exc
else:
    PYAUDIO_IMPORT_ERROR = None


DEFAULT_SAMPLE_RATE = 24000
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH = 2
DEFAULT_PRE_BUFFER_SECONDS = 0.5
DEFAULT_CHUNK_SIZE = 8192
MAX_WAV_HEADER_BYTES = 1024 * 1024
UINT32_MAX = (1 << 32) - 1
INT32_MAX = (1 << 31) - 1
# 生产 TTS 服务的流式 WAV 会先写入约 2 GB 的占位长度，连接关闭才表示 data 结束。
PRODUCTION_STREAM_DATA_SIZE = 0x7D000000
PRODUCTION_STREAM_RIFF_SIZE = PRODUCTION_STREAM_DATA_SIZE + 36
STREAM_DATA_SIZE_SENTINELS = {INT32_MAX, UINT32_MAX}
STREAM_RIFF_SIZE_SENTINELS = {0, INT32_MAX, UINT32_MAX}
DEFAULT_PLAYBACK_QUEUE_CHUNKS = 256
DEFAULT_PLAYBACK_JOIN_TIMEOUT = 10.0


def str2bool(value):
    if isinstance(value, bool):
        return value
    if value.lower() in ("yes", "true", "t", "y", "1"):
        return True
    if value.lower() in ("no", "false", "f", "n", "0"):
        return False
    raise argparse.ArgumentTypeError("Boolean value expected.")


def _raw_read(raw, size):
    """兼容 urllib3 HTTPResponse 和普通二进制流。"""
    try:
        return raw.read(size, decode_content=True)
    except TypeError:
        return raw.read(size)


def _read_exact(raw, size):
    """从响应流读取指定字节数；流提前结束时返回已读取的数据。"""
    parts = []
    remaining = size
    while remaining > 0:
        part = _raw_read(raw, remaining)
        if not part:
            break
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


def _read_wav_header(raw, first_byte, output_file):
    """
    精确读取 RIFF/WAV 头，停在 data 块的首个 PCM 字节之前。

    WAV 的 data 块不保证从第 44 字节开始，因此不能固定跳过 44 字节。
    """
    header = bytearray(first_byte)

    riff_tail = _read_exact(raw, 11)
    header.extend(riff_tail)
    output_file.write(riff_tail)
    if len(header) < 12:
        raise ValueError("响应体过短，无法读取 WAV 文件头")
    if bytes(header[0:4]) not in (b"RIFF", b"RF64") or bytes(header[8:12]) != b"WAVE":
        preview = bytes(header[:12])
        raise ValueError(f"响应不是 RIFF/WAV 音频，文件头={preview!r}")

    wav_info = {
        "container": bytes(header[0:4]),
        "declared_riff_size": struct.unpack("<I", header[4:8])[0],
        "audio_format": None,
        "channels": DEFAULT_CHANNELS,
        "sample_rate": DEFAULT_SAMPLE_RATE,
        "sample_width": DEFAULT_SAMPLE_WIDTH,
        "block_align": DEFAULT_CHANNELS * DEFAULT_SAMPLE_WIDTH,
        "byte_rate": DEFAULT_SAMPLE_RATE * DEFAULT_CHANNELS * DEFAULT_SAMPLE_WIDTH,
        "data_offset": None,
        "data_size_offset": None,
        "declared_data_size": None,
        "ds64_data_size": None,
    }

    while len(header) < MAX_WAV_HEADER_BYTES:
        chunk_header = _read_exact(raw, 8)
        header.extend(chunk_header)
        output_file.write(chunk_header)
        if len(chunk_header) < 8:
            raise ValueError("WAV 文件头不完整，未找到 data 块")

        chunk_id = chunk_header[:4]
        chunk_size = struct.unpack("<I", chunk_header[4:8])[0]
        chunk_data_offset = len(header)

        if chunk_id == b"data":
            wav_info["data_offset"] = chunk_data_offset
            wav_info["data_size_offset"] = chunk_data_offset - 4
            wav_info["declared_data_size"] = (
                wav_info["ds64_data_size"]
                if (
                    wav_info["container"] == b"RF64"
                    and chunk_size == UINT32_MAX
                    and wav_info["ds64_data_size"] is not None
                )
                else chunk_size
            )
            if wav_info["audio_format"] is None:
                raise ValueError("WAV 文件缺少 fmt 块")
            return wav_info

        padded_size = chunk_size + (chunk_size & 1)
        if len(header) + padded_size > MAX_WAV_HEADER_BYTES:
            raise ValueError(f"WAV 文件头超过 {MAX_WAV_HEADER_BYTES} 字节，疑似格式异常")

        chunk_data = _read_exact(raw, padded_size)
        header.extend(chunk_data)
        output_file.write(chunk_data)
        if len(chunk_data) < padded_size:
            raise ValueError(f"WAV {chunk_id!r} 块不完整")

        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise ValueError("WAV fmt 块长度小于 16 字节")
            (
                audio_format,
                channels,
                sample_rate,
                byte_rate,
                block_align,
                bits_per_sample,
            ) = struct.unpack("<HHIIHH", chunk_data[:16])
            if channels <= 0 or sample_rate <= 0 or bits_per_sample <= 0:
                raise ValueError("WAV fmt 块中的声道、采样率或位深无效")
            sample_width = (bits_per_sample + 7) // 8
            effective_audio_format = audio_format
            if audio_format == 0xFFFE and chunk_size >= 40:
                # WAVE_FORMAT_EXTENSIBLE 的 SubFormat GUID 前 2 字节是实际格式码。
                effective_audio_format = struct.unpack("<H", chunk_data[24:26])[0]
            # PCM 的 block_align 是每个完整采样帧的字节数，必须同时用于
            # 播放分帧和时长计算。服务端偶尔会填错 byte_rate，因此这里
            # 只接受与格式一致的 block_align，并重新推导 byte_rate。
            if block_align != channels * sample_width:
                raise ValueError(
                    "WAV fmt 块的 block_align 与声道/位深不一致: "
                    f"{block_align} != {channels}*{sample_width}"
                )
            calculated_block_align = block_align
            calculated_byte_rate = sample_rate * calculated_block_align
            wav_info.update(
                {
                    "audio_format": effective_audio_format,
                    "channels": channels,
                    "sample_rate": sample_rate,
                    "sample_width": sample_width,
                    # 指标计算采用格式字段推导值，避免错误的 byte_rate 污染音频时长和 RTF。
                    "block_align": calculated_block_align,
                    "byte_rate": calculated_byte_rate,
                }
            )
        elif chunk_id == b"ds64" and chunk_size >= 28:
            # RF64 用 ds64 中的 64-bit dataSize 替代 data 块的 0xffffffff。
            _, data_size, _, _ = struct.unpack("<QQQI", chunk_data[:28])
            wav_info["ds64_data_size"] = data_size

    raise ValueError(f"WAV 文件头超过 {MAX_WAV_HEADER_BYTES} 字节，未找到 data 块")


def _is_streaming_wav_size_placeholder(wav_info):
    """识别流式 WAV 的未知长度占位值。"""
    if (
        wav_info["container"] == b"RIFF"
        and wav_info["declared_riff_size"] == PRODUCTION_STREAM_RIFF_SIZE
        and wav_info["declared_data_size"] == PRODUCTION_STREAM_DATA_SIZE
    ):
        return True
    declared_data_size = wav_info["declared_data_size"]
    if declared_data_size in STREAM_DATA_SIZE_SENTINELS:
        return True
    # data=0 既可能表示合法空 data 块，也可能是服务端流式占位值；
    # 只有 RIFF 同时使用明确的流式占位值时才按未知长度处理。
    return (
        declared_data_size == 0
        and wav_info["declared_riff_size"] in STREAM_RIFF_SIZE_SENTINELS
    )


def _is_streaming_riff_size_placeholder(wav_info):
    return (
        wav_info["declared_riff_size"] in STREAM_RIFF_SIZE_SENTINELS
        or (
            wav_info["container"] == b"RIFF"
            and wav_info["declared_riff_size"] == PRODUCTION_STREAM_RIFF_SIZE
            and wav_info["declared_data_size"] == PRODUCTION_STREAM_DATA_SIZE
        )
    )


def _get_audio_limit(wav_info):
    """返回可信的 data 长度；流式占位头返回 None，表示读取到 EOF。"""
    if _is_streaming_wav_size_placeholder(wav_info):
        return None
    return wav_info["declared_data_size"]


def _playback_worker(audio_queue, wav_info, start_time, playback_metrics):
    """独立线程播放 PCM；播放失败时继续排空队列，避免阻塞网络接收。"""
    try:
        if pyaudio is None:
            detail = f" ({PYAUDIO_IMPORT_ERROR})" if PYAUDIO_IMPORT_ERROR else ""
            raise RuntimeError(
                "未安装或无法加载 PyAudio/PortAudio"
                f"{detail}；可执行 `python -m pip install PyAudio` 后重试"
            )
        if wav_info["audio_format"] != 1:
            raise ValueError(f"暂不支持播放 WAV format={wav_info['audio_format']}")

        player = pyaudio.PyAudio()
        sample_format = player.get_format_from_width(wav_info["sample_width"])
        stream = player.open(
            format=sample_format,
            channels=wav_info["channels"],
            rate=wav_info["sample_rate"],
            output=True,
        )
    except Exception as exc:
        playback_metrics["error"] = str(exc)
        while audio_queue.get() is not None:
            pass
        return

    try:
        while True:
            chunk = audio_queue.get()
            if chunk is None:
                break
            write_started_at = time.perf_counter() - start_time
            stream.write(chunk)
            if playback_metrics["start"] is None:
                # 记录成功提交的调用开始时间，不把失败的首次 write 误报为播放开始。
                # 这仍然是本地设备提交时间，不等同于可听见时间或服务端 TTFA。
                playback_metrics["start"] = write_started_at
        playback_metrics["complete"] = time.perf_counter() - start_time
    except Exception as exc:
        playback_metrics["error"] = str(exc)
        while audio_queue.get() is not None:
            pass
    finally:
        stream.stop_stream()
        stream.close()
        player.terminate()


def _fix_streaming_wav_sizes(path, wav_info, audio_bytes):
    """将流式 WAV 常见的 0/0xffffffff 占位长度修正为实际长度。"""
    if (
        wav_info["container"] != b"RIFF"
        or audio_bytes > UINT32_MAX
        or not (
            _is_streaming_riff_size_placeholder(wav_info)
            or _is_streaming_wav_size_placeholder(wav_info)
        )
    ):
        return

    file_size = os.path.getsize(path)
    if file_size < 8 or file_size - 8 > UINT32_MAX:
        return

    declared_size = wav_info["declared_data_size"]
    should_fix_data_size = (
        _is_streaming_wav_size_placeholder(wav_info)
        or declared_size > audio_bytes
    )

    with open(path, "r+b") as wav_file:
        wav_file.seek(4)
        wav_file.write(struct.pack("<I", file_size - 8))
        if should_fix_data_size:
            wav_file.seek(wav_info["data_size_offset"])
            wav_file.write(struct.pack("<I", audio_bytes))


def _format_seconds(value):
    return f"{value:.3f}s" if value is not None else "N/A"


def _format_ratio(value):
    return f"{value:.3f}" if value is not None else "N/A"


def _print_metrics(metrics, wav_info, args):
    audio_duration = metrics["audio_duration"]
    audio_last_byte_time = metrics["audio_last_byte_time"]
    response_eof_time = metrics["response_eof_time"]
    audio_rtf = (
        audio_last_byte_time / audio_duration
        if audio_duration > 0 and audio_last_byte_time is not None
        else None
    )
    e2e_rtf = (
        response_eof_time / audio_duration
        if audio_duration > 0 and response_eof_time is not None
        else None
    )
    synthesis_speed = (
        audio_duration / response_eof_time
        if response_eof_time is not None and response_eof_time > 0
        else None
    )
    text_rate = (
        len(args.text) / response_eof_time
        if response_eof_time is not None and response_eof_time > 0
        else None
    )
    mode_name = "流式" if args.stream else "非流式"

    print("\n" + "=" * 64)
    print(f"TTS {mode_name}端到端指标（均从发起 HTTP 请求开始计时）")
    print("  延迟:")
    print(f"    HTTP 响应头时间             : {_format_seconds(metrics['response_header_time'])}")
    print(f"    TTFB（首个响应体字节）      : {_format_seconds(metrics['ttfb'])}")
    print(
        f"    首个 PCM 字节               : "
        f"{_format_seconds(metrics['first_audio_byte_time'])}"
    )
    print(f"    TTFA（首个完整 PCM 帧）     : {_format_seconds(metrics['ttfa'])}")
    print(f"    最后一个 PCM 字节           : {_format_seconds(audio_last_byte_time)}")
    print(f"    HTTP 响应 EOF               : {_format_seconds(response_eof_time)}")
    if not args.stream:
        print("    TTFA 说明                   : 非流式模式下表示客户端首次可用音频时间")
    print("  音频与实时性:")
    print(
        f"    格式                       : {wav_info['sample_rate']}Hz, "
        f"{wav_info['channels']}ch, {wav_info['sample_width'] * 8}bit"
    )
    print(f"    PCM 音频字节                : {metrics['audio_bytes']}")
    print(f"    音频时长                   : {_format_seconds(audio_duration)}")
    print(f"    音频 RTF = 末 PCM / 时长    : {_format_ratio(audio_rtf)}")
    print(f"    端到端 RTF = HTTP EOF / 时长: {_format_ratio(e2e_rtf)}")
    print(
        "    说明                       : 两种 RTF 均包含请求、网络和客户端读取，"
        "不代表服务端纯推理耗时；跨模式比较建议使用端到端 RTF"
    )
    print(
        f"    端到端合成速度 = 1 / RTF    : {synthesis_speed:.2f}x 实时"
        if synthesis_speed is not None
        else "    端到端合成速度 = 1 / RTF    : N/A"
    )
    print("  本地侧:")
    if args.playback:
        print(f"    播放预缓冲                 : {args.pre_buffer_seconds:.3f}s")
        print(f"    PCM 首次提交音频设备        : {_format_seconds(metrics['playback_start'])}")
        print(f"    本地播放完成               : {_format_seconds(metrics['playback_complete'])}")
        if metrics["playback_error"]:
            print(f"    播放状态                   : 不可用（{metrics['playback_error']}）")
    else:
        print("    播放                       : 已禁用")
    print(f"    文本长度                   : {len(args.text)} 字符")
    print(
        f"    端到端文本完成率           : {text_rate:.1f} Unicode字符/s"
        if text_rate is not None
        else "    端到端文本完成率           : N/A"
    )
    print(f"    输出文件                   : {args.output}")
    print("=" * 64)


def _build_parser():
    parser = argparse.ArgumentParser(
        description="Test /v1/audio/speech instruct2 endpoint with accurate streaming metrics"
    )
    parser.add_argument(
        "--text",
        type=str,
        default=(
            "认知的套利者，在这个世界大有搞头的逻辑里，最高级的财富，是你的选择权。"
            "它是智力资本在时间复利中的悄然绽放，它是认知高地对低洼地带的温柔俯瞰。"
            "当别人在存量博弈里拼刺刀，你已在 this is the begin 正确非共识 this is the end "
            "的无人区，种下了属于未来的森林。自由的代价，从来不是不被强迫，而是你看得见，"
            "万千条通往星辰的隐秘路径。"
        ),
        help="Text to synthesize",
    )
    parser.add_argument(
        "--instruct_text",
        type=str,
        default="You are a helpful assistant. 很自然地说<|endofprompt|>",
    )
    parser.add_argument("--prompt_audio", type=str, default="kehu_female_c")
    parser.add_argument("--zero_shot_spk_id", type=str, default="kehu_female_b")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--stream", type=str2bool, default=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--split", type=str2bool, default=True)
    parser.add_argument("--background_audio", type=str, default="")
    parser.add_argument("--background_volume", type=float, default=0.0)
    parser.add_argument("--background_loop", type=str2bool, default=True)
    parser.add_argument("--text_frontend", type=str2bool, default=True)
    parser.add_argument("--res_content", type=str2bool, default=True)
    parser.add_argument("--url", type=str, default="http://36.111.82.53:10014/v1/audio/speech")
    parser.add_argument("--output", type=str, default=os.path.join("tts_output", "received_test.wav"))
    parser.add_argument("--playback", type=str2bool, default=True)
    parser.add_argument("--pre-buffer-seconds", type=float, default=DEFAULT_PRE_BUFFER_SECONDS)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument(
        "--playback-queue-chunks",
        type=int,
        default=DEFAULT_PLAYBACK_QUEUE_CHUNKS,
    )
    parser.add_argument(
        "--playback-join-timeout",
        type=float,
        default=DEFAULT_PLAYBACK_JOIN_TIMEOUT,
    )
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--read-timeout", type=float, default=300.0)
    return parser


def main():
    args = _build_parser().parse_args()
    if args.pre_buffer_seconds < 0:
        raise ValueError("--pre-buffer-seconds must be >= 0")
    if args.chunk_size <= 0:
        raise ValueError("--chunk-size must be > 0")
    if args.playback_queue_chunks <= 0:
        raise ValueError("--playback-queue-chunks must be > 0")
    if args.playback_join_timeout <= 0:
        raise ValueError("--playback-join-timeout must be > 0")
    if args.playback and pyaudio is None:
        detail = f"：{PYAUDIO_IMPORT_ERROR}" if PYAUDIO_IMPORT_ERROR else ""
        print(
            "播放已启用，但 PyAudio/PortAudio 不可用"
            f"{detail}。请执行 `python -m pip install PyAudio`，"
            "或使用 `--playback false` 仅保存 WAV 文件。"
        )
        return 2

    payload = {
        "tts_params": {
            "text": args.text,
            "instruct_text": args.instruct_text,
            "zero_shot_spk_id": args.zero_shot_spk_id,
            "prompt_audio": args.prompt_audio,
            "speed": args.speed,
            "stream": args.stream,
            "background_audio": args.background_audio,
            "background_volume": args.background_volume,
            "background_loop": args.background_loop,
            "text_frontend": args.text_frontend,
            "seed": args.seed,
            "split": args.split,
            "res_content": args.res_content,
        }
    }
    headers = {
        "Accept": "application/octet-stream, audio/wav",
        "Content-Type": "application/json",
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache",
    }

    output_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(output_dir, exist_ok=True)

    print(f"Sending request to {args.url}...")
    print(f"text: {args.text[:40]}{'...' if len(args.text) > 40 else ''}")
    print(f"instruct_text: {args.instruct_text}")
    print(f"zero_shot_spk_id: {args.zero_shot_spk_id}")

    audio_queue = queue.Queue(maxsize=args.playback_queue_chunks)
    playback_metrics = {"start": None, "complete": None, "error": None}
    play_thread = None
    playback_enabled = args.playback
    discard_playback_on_exit = False
    response = None
    wav_info = None
    start_time = None
    metrics = {
        "response_header_time": None,
        "ttfb": None,
        "first_audio_byte_time": None,
        "ttfa": None,
        "audio_last_byte_time": None,
        "response_eof_time": None,
        "audio_bytes": 0,
        "audio_duration": 0.0,
        "playback_start": None,
        "playback_complete": None,
        "playback_error": None,
    }

    def ensure_playback_thread():
        nonlocal play_thread, playback_enabled
        if not playback_enabled or play_thread is not None:
            return
        try:
            play_thread = threading.Thread(
                target=_playback_worker,
                args=(audio_queue, wav_info, start_time, playback_metrics),
                daemon=True,
            )
            play_thread.start()
        except Exception as exc:
            playback_enabled = False
            playback_metrics["error"] = f"播放线程启动失败: {exc}"

    def enqueue_playback(data):
        nonlocal playback_enabled
        if not playback_enabled or not data:
            return
        if playback_metrics["error"]:
            playback_enabled = False
            return
        ensure_playback_thread()
        if not playback_enabled:
            return
        try:
            audio_queue.put_nowait(data)
        except queue.Full:
            playback_enabled = False
            playback_metrics["error"] = (
                "播放队列已满，已停止继续入队，避免本地播放反压并污染网络计时"
            )

    def discard_queued_playback():
        while True:
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                return

    try:
        # HTTP 客户端始终采用 stream=True，payload 中的 stream 决定服务端生成模式。
        # 计时紧贴 requests.post，排除参数构造和日志输出耗时。
        start_time = time.perf_counter()
        response = requests.post(
            args.url,
            json=payload,
            headers=headers,
            stream=True,
            timeout=(args.connect_timeout, args.read_timeout),
        )
        metrics["response_header_time"] = time.perf_counter() - start_time

        if response.status_code != 200:
            error_body = _raw_read(response.raw, 4097)
            suffix = "...<truncated>" if len(error_body) > 4096 else ""
            preview = error_body[:4096].decode(response.encoding or "utf-8", errors="replace")
            print(f"Error: HTTP {response.status_code}: {preview}{suffix}")
            return 1

        content_type = response.headers.get("Content-Type", "").lower()
        if "json" in content_type:
            body = _raw_read(response.raw, 4097)
            suffix = "...<truncated>" if len(body) > 4096 else ""
            preview = body[:4096].decode(response.encoding or "utf-8", errors="replace")
            raise ValueError(
                "服务端返回 JSON 而不是 WAV，无法计算实际音频时长、TTFA 和 RTF；"
                f"请确认 --res_content true。响应={preview}{suffix}"
            )

        # 先读取首字节再打开输出文件，避免文件创建耗时污染 TTFB。
        first_byte = _raw_read(response.raw, 1)
        if not first_byte:
            metrics["response_eof_time"] = time.perf_counter() - start_time
            raise ValueError("HTTP 200 响应体为空")
        metrics["ttfb"] = time.perf_counter() - start_time

        with open(args.output, "wb") as output_file:
            output_file.write(first_byte)

            wav_info = _read_wav_header(response.raw, first_byte, output_file)
            if wav_info["audio_format"] != 1:
                raise ValueError(
                    f"当前指标只支持未压缩 PCM WAV，实际 format={wav_info['audio_format']}"
                )

            bytes_per_second = wav_info["byte_rate"]
            pre_buffer_bytes = max(1, round(bytes_per_second * args.pre_buffer_seconds))
            pre_buffer = bytearray()
            playback_released = not args.playback
            # PyAudio 必须按完整音频帧提交数据。之前首个 PCM 字节单独入队，
            # 再叠加 8192 字节分片，会使 16-bit 音频的首个播放块变成奇数长度，
            # 从而造成整个播放流按 1 字节错位，听起来就是持续杂音。
            # HTTP 分片边界不一定落在完整 PCM 帧边界上。该缓冲同时用于
            # 计算真正“可播放”的 TTFA，以及向 PyAudio 提交完整帧。
            pcm_pending = bytearray()
            block_align = max(1, wav_info["block_align"])

            def aligned_pcm(data):
                """缓存跨 HTTP 分片的半帧，只返回完整 PCM 帧。"""
                if not data:
                    return b""
                pcm_pending.extend(data)
                aligned_size = len(pcm_pending) - (len(pcm_pending) % block_align)
                if aligned_size <= 0:
                    return b""
                result = bytes(pcm_pending[:aligned_size])
                del pcm_pending[:aligned_size]
                return result

            declared_size = wav_info["declared_data_size"]
            audio_limit = _get_audio_limit(wav_info)
            remaining_audio = audio_limit

            while remaining_audio is None or remaining_audio > 0:
                # TTFA 尚未得到时，仅请求凑齐一个 PCM 帧所需的字节，避免
                # 默认 8192 字节读取粒度把“首帧时间”延迟成“首大块时间”。
                if metrics["ttfa"] is None:
                    read_size = max(1, block_align - len(pcm_pending))
                else:
                    read_size = args.chunk_size
                if remaining_audio is not None:
                    read_size = min(read_size, remaining_audio)

                chunk = _raw_read(response.raw, read_size)
                read_completed_at = time.perf_counter() - start_time
                if not chunk:
                    metrics["response_eof_time"] = read_completed_at
                    break
                # response.raw 应遵守 read(size) 的上限；防御性截断可避免
                # 异常流实现把 data 块之后的内容误计入 PCM。
                if remaining_audio is not None and len(chunk) > remaining_audio:
                    chunk = chunk[:remaining_audio]
                if not chunk:
                    break
                if remaining_audio is not None:
                    remaining_audio -= len(chunk)

                output_file.write(chunk)
                metrics["audio_bytes"] += len(chunk)
                if metrics["first_audio_byte_time"] is None:
                    metrics["first_audio_byte_time"] = read_completed_at
                metrics["audio_last_byte_time"] = read_completed_at
                aligned_audio = aligned_pcm(chunk)
                if aligned_audio and metrics["ttfa"] is None:
                    # 首个完整 PCM 帧到达才算 TTFA；仅收到一个 16-bit
                    # 样本字节不能被音频设备播放。
                    metrics["ttfa"] = read_completed_at

                if args.playback and aligned_audio:
                    if not playback_released:
                        pre_buffer.extend(aligned_audio)
                        if len(pre_buffer) >= pre_buffer_bytes:
                            enqueue_playback(bytes(pre_buffer))
                            pre_buffer.clear()
                            playback_released = True
                    else:
                        enqueue_playback(aligned_audio)

            if remaining_audio is not None and remaining_audio != 0:
                raise ValueError(
                    "WAV data 块被截断: "
                    f"声明 {audio_limit} 字节，实际只收到 {metrics['audio_bytes']} 字节"
                )

            # 已知 data 长度时继续读取到 HTTP EOF，并原样保存 data 后的
            # RIFF 填充或 LIST/JUNK 等合法尾部块，但不把它们计入 PCM 指标。
            if metrics["response_eof_time"] is None:
                while True:
                    trailing = _raw_read(response.raw, args.chunk_size)
                    read_completed_at = time.perf_counter() - start_time
                    if not trailing:
                        metrics["response_eof_time"] = read_completed_at
                        break
                    output_file.write(trailing)

            if pcm_pending:
                raise ValueError(
                    "WAV data 长度不是完整 PCM 帧的整数倍: "
                    f"剩余 {len(pcm_pending)} 字节，block_align={block_align}"
                )
            if audio_limit is None and metrics["audio_bytes"] & 1:
                # RIFF chunk 数据按偶数字节对齐；填充字节不属于 PCM，
                # 未知长度流无法从响应中区分最后的填充字节，因此本地补齐。
                output_file.write(b"\x00")
            if args.playback and pre_buffer:
                enqueue_playback(bytes(pre_buffer))

        if metrics["audio_bytes"] <= 0:
            raise ValueError("WAV data 块中没有音频数据")

        metrics["audio_duration"] = metrics["audio_bytes"] / wav_info["byte_rate"]
        _fix_streaming_wav_sizes(args.output, wav_info, metrics["audio_bytes"])

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        discard_playback_on_exit = True
        return 130
    except Exception as exc:
        print(f"Request failed: {type(exc).__name__}: {exc}")
        discard_playback_on_exit = True
        return 1
    finally:
        if response is not None:
            response.close()
        if play_thread is not None:
            if discard_playback_on_exit:
                discard_queued_playback()
            try:
                audio_queue.put(None, timeout=0.5)
            except queue.Full:
                discard_queued_playback()
                audio_queue.put_nowait(None)
            play_thread.join(timeout=args.playback_join_timeout)
            if play_thread.is_alive():
                playback_metrics["error"] = (
                    playback_metrics["error"]
                    or f"播放线程在 {args.playback_join_timeout:.1f}s 内未退出"
                )
        metrics["playback_start"] = playback_metrics["start"]
        metrics["playback_complete"] = playback_metrics["complete"]
        metrics["playback_error"] = playback_metrics["error"]

    if wav_info is not None and metrics["response_eof_time"] is not None:
        print("Success! Audio response received.")
        print(
            f"Worker Info: PID={response.headers.get('X-Process-ID', 'N/A')}, "
            f"Worker={response.headers.get('X-Worker-ID', 'N/A')}"
        )
        print(
            "Detected audio format: "
            f"{wav_info['sample_rate']}Hz, {wav_info['channels']}ch, "
            f"{wav_info['sample_width'] * 8}bit"
        )
        if _is_streaming_wav_size_placeholder(wav_info):
            print(
                "Streaming WAV placeholder normalized: "
                f"RIFF={wav_info['declared_riff_size']}, data={wav_info['declared_data_size']}"
            )
        _print_metrics(metrics, wav_info, args)
        print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
