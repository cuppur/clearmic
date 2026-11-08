"""Conservative reference-audio checks and a clean continuous 3 s selector.

This module measures signal quality. It does not identify a speaker, recognize
speech, or detect overlapping people. A stationary noise can pass these checks;
the caller must still ask the user to record only their own voice in a quiet room.
No learned model, network access, microphone access, or file I/O is used here.
"""
from __future__ import annotations

import numpy as np

REFERENCE_SECONDS = 3.0
MAX_ANALYSIS_SECONDS = 30.0


def _db(rms: float) -> float:
    return float(20.0 * np.log10(max(float(rms), 1e-6)))


def _rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))


def select_reference(audio, sample_rate=16000):
    """Return ``(three_second_float32_audio, quality_report)``.

    The input must be normalized mono PCM. The returned segment is contiguous,
    at the input sampling rate, and is not amplified or denoised. Selection uses
    activity coverage with a large penalty for clipping, rather than loudness
    alone. A longer recording is inspected up to its first 30 seconds.

    ``active_audio_seconds`` means energy above an adaptive signal threshold,
    not verified speech. Report flags deliberately preserve that distinction.
    Low-level but otherwise usable recordings produce warnings instead of an
    unnecessarily aggressive rejection.
    """
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, np.integer)) or sample_rate < 1000:
        raise ValueError("采样率必须是至少 1000 Hz 的整数。")
    sample_rate = int(sample_rate)
    try:
        x = np.asarray(audio, dtype=np.float32)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("声音样本无法读取，请使用单声道音频。") from exc
    if x.ndim != 1:
        raise ValueError("声音样本必须是单声道音频，请先转换为单声道。")
    if not len(x):
        raise ValueError("声音样本为空，请重新录制。")
    if not np.isfinite(x).all():
        raise ValueError("声音样本含有无效数值，请重新录制或更换音频文件。")
    required = int(round(sample_rate * REFERENCE_SECONDS))
    if len(x) < required:
        raise ValueError("声音样本需要至少 3 秒，请连续正常讲话后重新录制。")

    duration = len(x) / sample_rate
    x = x[:int(round(sample_rate * MAX_ANALYSIS_SECONDS))]
    # Remove only the DC component for measurement; preserve source samples in
    # the selected output. DC offset alone must not count as usable voice audio.
    dc = float(np.mean(x, dtype=np.float64))
    centered = x.astype(np.float64) - dc
    frame = max(1, int(round(sample_rate * .01)))
    n_frames = int(np.ceil(len(x) / frame))
    padded = np.pad(centered, (0, n_frames * frame - len(x)))
    frame_rms = np.sqrt(np.mean(padded.reshape(n_frames, frame) ** 2, axis=1))
    levels = 20.0 * np.log10(np.maximum(frame_rms, 1e-6))
    clipped = np.pad(np.abs(x) >= .99, (0, n_frames * frame - len(x)))
    reliable = clipped.reshape(n_frames, frame).mean(axis=1) <= .01
    if not np.any(reliable):
        raise ValueError("声音样本明显爆音，请降低麦克风音量后重新录制。")
    # A badly overloaded beginning must not raise the activity threshold so far
    # that a later, quieter clean segment disappears from consideration.
    reliable_levels = levels[reliable]
    high = float(np.percentile(reliable_levels, 95))
    floor = float(np.percentile(reliable_levels, 20))
    if high < -68.0:
        raise ValueError("声音样本几乎无声或过轻，请选对麦克风、靠近一些后重新录制。")

    # The upper cap keeps quiet continuous speech eligible, even where the
    # recording has no clear pauses. Stationary noise is explicitly only warned
    # about below, since an energy heuristic cannot determine what made a sound.
    threshold = max(-68.0, min(floor + 8.0, high - 10.0))
    active = levels >= threshold
    active_prefix = np.concatenate(([0], np.cumsum(active)))
    clip_prefix = np.concatenate(([0], np.cumsum(np.abs(x) >= .99)))
    power_prefix = np.concatenate(([0.0], np.cumsum(centered ** 2)))
    starts = list(range(0, len(x) - required + 1, frame))
    if starts[-1] != len(x) - required:
        starts.append(len(x) - required)

    best = None
    for start in starts:
        end = start + required
        first_frame = start // frame
        last_frame = min(n_frames, int(np.ceil(end / frame)))
        activity = float(active_prefix[last_frame] - active_prefix[first_frame]) / max(last_frame - first_frame, 1)
        clipping = float(clip_prefix[end] - clip_prefix[start]) / required
        rms = float(np.sqrt(max((power_prefix[end] - power_prefix[start]) / required, 0)))
        local_levels = levels[first_frame:last_frame]
        local_contrast = float(np.percentile(local_levels, 90) - np.percentile(local_levels, 20))
        # Prefer a full useful segment; transient/sustained overload cannot win
        # merely because it is louder. A small modulation term breaks ties.
        score = activity * 100.0 - clipping * 700.0 + min(local_contrast, 20.0) * .2
        if best is None or score > best[0]:
            best = (score, start, activity, clipping, rms)

    score, start, activity, clipping, rms = best
    chosen = x[start:start + required].copy()
    active_seconds = min(REFERENCE_SECONDS, activity * REFERENCE_SECONDS)
    if clipping > .03:
        raise ValueError("找不到没有明显爆音的 3 秒声音片段，请降低麦克风音量后重新录制。")
    if active_seconds < 1.2 or _db(rms) < -65.0:
        raise ValueError("可用声音太少，请连续正常讲话至少 3 秒，避免长时间停顿。")

    warnings = []
    selected_rms = _db(rms)
    contrast = float(np.percentile(levels, 90) - floor)
    if selected_rms < -42.0:
        warnings.append("声音较轻，建议靠近麦克风；如果试听能清晰听见自己，也可以先试用。")
    if clipping > .002:
        warnings.append("选中片段存在少量爆音，建议稍微降低麦克风音量后重新录入。")
    if active_seconds < 2.0:
        warnings.append("选中片段停顿较多，连续正常讲话通常能提供更好的参考。")
    if contrast < 5.0:
        warnings.append("声音强度变化较小，可能有持续背景声；请试听确认内容清楚且只有你讲话。")
    if abs(dc) > .03:
        warnings.append("录音存在较明显直流偏移，建议检查麦克风或录音设备。")
    if duration > MAX_ANALYSIS_SECONDS:
        warnings.append("仅检查了录音的前 30 秒。")

    report = {
        "status": "warning" if warnings else "ok",
        "input_duration_seconds": float(duration),
        "analyzed_duration_seconds": len(x) / sample_rate,
        "selected_start_seconds": start / sample_rate,
        "selected_duration_seconds": REFERENCE_SECONDS,
        "sample_rate": sample_rate,
        "selected_rms_dbfs": selected_rms,
        "selected_peak_dbfs": _db(float(np.max(np.abs(chosen)))),
        "selected_clipping_fraction": clipping,
        "input_clipping_fraction": float(np.mean(np.abs(x) >= .99)),
        "active_audio_seconds": active_seconds,
        "active_fraction": activity,
        "activity_threshold_dbfs": float(threshold),
        "estimated_noise_floor_dbfs": floor,
        "level_contrast_db": contrast,
        "warnings": warnings,
        "identity_verified": False,
        "speech_verified": False,
        "multiple_speakers_checked": False,
        "method": "能量活动覆盖、爆音惩罚及连续片段选择",
        "limitations": [
            "能量活动不等于已识别的讲话；持续噪声也可能通过检查。",
            "不能确认录音者身份，也不能检测是否混入其他说话人。",
            "请在安静处仅由目标用户讲话，并试听确认参考录音。",
        ],
    }
    return chosen, report
