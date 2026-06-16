import editdistance
import numpy as np
import librosa

# 核心：字错误率 CER（功能测试核心指标）
def get_cer(reference_text, predict_text):
    if len(reference_text) == 0:
        return 0.0
    edit_len = editdistance.eval(list(reference_text), list(predict_text))
    cer = edit_len / len(reference_text)
    return round(cer, 4)

# 核心：词错误率 WER
def get_wer(reference_text, predict_text):
    ref_list = reference_text.split()
    pre_list = predict_text.split()
    if len(ref_list) == 0:
        return 0.0
    edit_len = editdistance.eval(ref_list, pre_list)
    wer = edit_len / len(ref_list)
    return round(wer, 4)

# 音频噪音、音质检测
def check_audio_noise(audio_data):
    y = np.frombuffer(audio_data, dtype=np.int16)
    db = np.mean(np.abs(y))
    assert db > 10, "音频异常：无声音/静音"
    assert db < 30000, "音频异常：爆音/杂音过大"

# 性能指标计算：P50 P95 P99
def calc_percentile(time_list):
    p50 = np.percentile(time_list, 50)
    p95 = np.percentile(time_list, 95)
    p99 = np.percentile(time_list, 99)
    return round(p50, 3), round(p95, 3), round(p99, 3)