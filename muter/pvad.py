"""NomoPVAD target-speaker probabilities using local ONNX files only.

The detector is a speaker-conditioned network, not an energy gate. Every stream
holds at most 2.4 s of audio; sessions share immutable ONNX model weights. Runtime
imports neither PyTorch nor ModelScope and never downloads or saves audio.
"""
from __future__ import annotations

from pathlib import Path
import sys
import threading
import time

import numpy as np
import onnxruntime as ort
import kaldi_native_fbank as knf

SR = 16000
CHUNK_SAMPLES = 2560
WINDOW_SAMPLES = 38400
ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "models" / "pvad"


def _audio(audio):
    x = np.asarray(audio, np.float32)
    if x.ndim != 1 or not np.isfinite(x).all():
        raise ValueError("说话人检测需要有效的 16 kHz 单声道音频。")
    return np.ascontiguousarray(x)


def fbank(audio):
    """Match official torchaudio.compliance.kaldi settings and time mean removal."""
    x = _audio(audio)
    if len(x) < 400:
        raise ValueError("音频不足 25 ms，无法计算说话人特征。")
    opts = knf.FbankOptions()
    opts.frame_opts.samp_freq = SR
    opts.frame_opts.frame_length_ms = 25.0
    opts.frame_opts.frame_shift_ms = 10.0
    opts.frame_opts.dither = 0.0
    opts.frame_opts.snip_edges = True
    opts.frame_opts.window_type = "povey"
    opts.mel_opts.num_bins = 80
    opts.mel_opts.low_freq = 20.0
    opts.mel_opts.high_freq = 0.0
    opts.use_energy = False
    opts.use_log_fbank = True
    opts.use_power = True
    computer = knf.OnlineFbank(opts)
    computer.accept_waveform(SR, x * 32768.0)
    # With snip_edges=True only complete frames are eligible. input_finished()
    # does not add reflected tail frames, matching torchaudio's offline fbank.
    computer.input_finished()
    feat = np.stack([computer.get_frame(i) for i in range(computer.num_frames_ready)]).astype(np.float32)
    return np.ascontiguousarray(feat - feat.mean(axis=0, keepdims=True))


class _Models:
    def __init__(self, directory, threads):
        paths = [directory / name for name in ("enroll.onnx", "detector.onnx")]
        if not all(path.is_file() for path in paths):
            raise FileNotFoundError("缺少本地目标说话人检测模型，请使用包含 models/pvad 的完整版本。")
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self.enroll = ort.InferenceSession(str(paths[0]), options, providers=["CPUExecutionProvider"])
        self.detector = ort.InferenceSession(str(paths[1]), options, providers=["CPUExecutionProvider"])


class Guard:
    """Per-stream enrolled target detection; ``push`` accepts exactly 160 ms.

    The returned probability is not a guarantee of identity or absence of other
    voices. The caller must evaluate false rejection, overlap and leakage, and
    account for chunk buffering plus model execution latency.
    """
    _models = {}
    _models_lock = threading.Lock()
    chunk_samples = CHUNK_SAMPLES

    @classmethod
    def load_models(cls, model_dir=None, threads=4):
        if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
            raise ValueError("说话人检测线程数必须是正整数。")
        directory = Path(model_dir) if model_dir is not None else MODEL_DIR
        key = (str(directory.resolve()), threads)
        with cls._models_lock:
            if key not in cls._models:
                cls._models[key] = _Models(directory, threads)
            return cls._models[key]

    def __init__(self, voice16k, model_dir=None, threads=4, window_samples=WINDOW_SAMPLES):
        voice = _audio(voice16k)
        if len(voice) < SR * 3:
            raise ValueError("目标说话人检测需要至少 3 秒参考语音。")
        if float(np.sqrt(np.mean(voice ** 2))) < 1e-6:
            raise ValueError("参考语音几乎无声，无法注册目标说话人。")
        if (not CHUNK_SAMPLES <= window_samples <= WINDOW_SAMPLES
                or window_samples % CHUNK_SAMPLES):
            raise ValueError("检测窗口必须由完整的 160 ms 分块组成。")
        self.window_samples = window_samples
        self.models = self.load_models(model_dir, threads)
        started = time.perf_counter()
        features = fbank(voice[:SR * 30])[None]
        self.speaker = self.models.enroll.run(None, {"fbank": features})[0].astype(np.float32)
        if self.speaker.shape != (1, 192) or not np.isfinite(self.speaker).all():
            raise RuntimeError("说话人注册输出异常。")
        self.enrollment_ms = (time.perf_counter() - started) * 1000
        self.last_processing_ms = 0.0
        self.reset()

    def reset(self):
        self._buf = np.empty(0, np.float32)

    def push(self, chunk):
        chunk = _audio(chunk)
        if len(chunk) != CHUNK_SAMPLES:
            raise ValueError("目标说话人检测每次需要恰好 2560 个采样（160 ms）。")
        started = time.perf_counter()
        self._buf = np.concatenate((self._buf, chunk))[-self.window_samples:]
        features = fbank(self._buf)[None]
        probabilities = self.models.detector.run(None, {"speaker": self.speaker, "fbank": features})[0]
        self.last_processing_ms = (time.perf_counter() - started) * 1000
        if probabilities.ndim != 2 or not probabilities.size or not np.isfinite(probabilities).all():
            raise RuntimeError("目标说话人概率输出异常，不能继续传声。")
        probability = float(probabilities[0, -1])
        if not 0.0 <= probability <= 1.0:
            raise RuntimeError("目标说话人概率越界，不能继续传声。")
        return probability
