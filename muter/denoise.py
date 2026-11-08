"""Optional local GTCRN environment-noise suppression backend.

GTCRN enhances speech in noise. It does not condition on a target identity and
must never be presented as a replacement for target-speaker extraction when
several people are talking. Each stream owns an independent native state.
"""
from __future__ import annotations

from pathlib import Path
import sys
import threading
import time

import numpy as np

SR = 16000
ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
MODEL_PATH = ROOT / "models" / "gtcrn" / "gtcrn_simple.onnx"


def _audio(x):
    try:
        x = np.asarray(x, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("输入音频格式无效") from exc
    if x.ndim != 1 or not np.isfinite(x).all():
        raise ValueError("输入音频必须是数值有效的 16 kHz 单声道")
    return np.ascontiguousarray(x)


class GTCRNStream:
    """16 kHz float32 PCM in/out with native recurrent and STFT state."""
    def __init__(self, engine):
        self.engine = engine
        self.n_in = 0
        self.n_out = 0
        self.closed = False

    def push(self, x):
        if self.closed:
            raise RuntimeError("该降噪流已结束，请重新创建处理流。")
        x = _audio(x)
        if not len(x):
            return np.empty(0, np.float32)
        self.n_in += len(x)
        y = np.asarray(self.engine(x, SR).samples, dtype=np.float32)
        if y.ndim != 1 or not np.isfinite(y).all() or self.n_out + len(y) > self.n_in:
            self.closed = True
            raise RuntimeError("环境降噪输出异常，已停止处理。")
        self.n_out += len(y)
        return np.clip(y, -.98, .98)

    def flush(self):
        if self.closed:
            return np.empty(0, np.float32)
        self.closed = True
        if not self.n_in:
            return np.empty(0, np.float32)
        tail = np.asarray(self.engine.flush().samples, dtype=np.float32)
        if tail.ndim != 1 or not np.isfinite(tail).all() or self.n_out + len(tail) != self.n_in:
            raise RuntimeError("环境降噪输出长度或数值异常，未生成完整结果。")
        self.n_out += len(tail)
        return np.clip(tail, -.98, .98)


class GTCRNModel:
    """Model-compatible optional backend using sherpa-onnx's online denoiser.

    ``voice`` is accepted solely for API compatibility and is ignored. Local
    weights are required; loading never downloads models or accesses a mic.
    """
    backend = "gtcrn"
    precision = "fp32"
    requires_voice = False

    def __init__(self, model_path=None, threads=1):
        if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
            raise ValueError("推理线程数必须是正整数。")
        self.model_path = Path(model_path) if model_path is not None else MODEL_PATH
        self.threads = threads
        self.lock = threading.Lock()
        self._loaded = False
        self._sherpa = None

    def _configuration(self):
        s = self._sherpa
        return s.OnlineSpeechDenoiserConfig(
            model=s.OfflineSpeechDenoiserModelConfig(
                gtcrn=s.OfflineSpeechDenoiserGtcrnModelConfig(model=str(self.model_path)),
                debug=False, num_threads=self.threads, provider="cpu",
            )
        )

    def load(self):
        with self.lock:
            if self._loaded:
                return self
            if not self.model_path.is_file():
                raise FileNotFoundError("缺少 GTCRN 本地模型，请使用包含 models/gtcrn 的完整版本。")
            try:
                import sherpa_onnx
                import onnxruntime as ort
            except ImportError as exc:
                raise RuntimeError("环境降噪需要 sherpa-onnx 2.0.1 和 ONNX Runtime，请重新安装完整运行环境。") from exc

            # Native sherpa-onnx exits the process for missing GTCRN metadata.
            # Inspect it in Python first so an incompatible upstream export
            # becomes a recoverable application error, never a process abort.
            try:
                options = ort.SessionOptions()
                options.intra_op_num_threads = 1
                check = ort.InferenceSession(str(self.model_path), options, providers=["CPUExecutionProvider"])
                meta = check.get_modelmeta().custom_metadata_map
                expected = {"model_type": "gtcrn", "sample_rate": "16000", "n_fft": "512",
                            "hop_length": "256", "window_length": "512", "window_type": "hann_sqrt",
                            "version": "1", "conv_cache_shape": "2,1,16,16,33",
                            "tra_cache_shape": "2,3,1,1,16", "inter_cache_shape": "2,1,33,16"}
                if any(meta.get(key) != value for key, value in expected.items()):
                    raise ValueError("模型元数据不匹配")
                del check
            except Exception as exc:
                raise ValueError("GTCRN 模型格式不兼容，请使用官方 sherpa-onnx 的 gtcrn_simple.onnx。") from exc
            self._sherpa = sherpa_onnx
            config = self._configuration()
            if not config.validate():
                raise ValueError("GTCRN 模型配置无效，请重新安装完整版本。")
            # Prove native model construction before reporting ready. Reuse no
            # state from this probe in the user's eventual stream.
            probe = sherpa_onnx.OnlineSpeechDenoiser(config)
            if probe.sample_rate != SR:
                raise ValueError("GTCRN 模型采样率不兼容。")
            del probe
            self._loaded = True
        return self

    def stream(self, voice=None):
        self.load()
        return GTCRNStream(self._sherpa.OnlineSpeechDenoiser(self._configuration()))

    def process(self, x, voice=None, cancel=None, progress=None, chunk_ms=40):
        if chunk_ms not in (20, 40):
            raise ValueError("分块必须为 20 或 40 ms")
        x = _audio(x)
        if not len(x):
            raise ValueError("输入音频为空")
        if cancel and cancel.is_set():
            raise InterruptedError("已取消")
        stream = self.stream(voice)
        chunk_samples = SR * chunk_ms // 1000
        out, times = [], []
        for start in range(0, len(x), chunk_samples):
            if cancel and cancel.is_set():
                raise InterruptedError("已取消")
            t = time.perf_counter()
            y = stream.push(x[start:start + chunk_samples])
            times.append(time.perf_counter() - t)
            out.append(y)
            if progress and start % SR < chunk_samples:
                progress(min(1.0, start / len(x)))
        if cancel and cancel.is_set():
            raise InterruptedError("已取消")
        out.append(stream.flush())
        y = np.concatenate(out)
        if len(y) != len(x) or not np.isfinite(y).all():
            raise RuntimeError("环境降噪输出长度或数值异常，未生成结果。")
        if progress:
            progress(1.0)
        return np.clip(y, -.98, .98), {
            "backend": self.backend,
            "precision": self.precision,
            "chunk_ms": chunk_ms,
            "audio_seconds": len(x) / SR,
            "processing_seconds": sum(times),
            "rtf": sum(times) / max(len(x) / SR, 1e-6),
            "chunk_p95_ms": float(np.percentile(times, 95) * 1000) if times else 0.0,
        }
