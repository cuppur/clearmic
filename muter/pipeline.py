"""Optional speaker-conditioned gate, aligned to delayed enhancer output.

This is an experimental probability gate, not authentication. It can reject the
target's quiet speech and cannot remove an interferer during accepted overlap.
"""
import time
import numpy as np

SR, BLOCK = 16000, 2560
SOFT_WINDOW_BLOCKS = 7
SOFT_LOW, SOFT_HIGH, SOFT_FLOOR = .22, .55, .10


class GatedStream:
    def __init__(self, enhancer, guard, threshold=.65, smooth=False):
        self.enhancer, self.guard, self.threshold = enhancer, guard, threshold
        self.pending = np.zeros(0, np.float32)
        self.mask = np.zeros(0, np.float32)
        self.closed = False
        self.probabilities = []
        self.guard_times = []
        self.smooth = smooth
        self.delayed = None
        self.previous_probability = 0.0
        self.previous_gain = None

    def _aligned(self, output):
        if len(output) > len(self.mask) or not np.isfinite(output).all():
            raise RuntimeError("本人检测与降噪时序不一致，已停止处理。")
        y = output * self.mask[:len(output)]
        self.mask = self.mask[len(output):]
        return y

    def _enhance(self, x, gain):
        self.mask = np.concatenate((self.mask, gain))
        return self._aligned(self.enhancer.push(x))

    def _release_delayed(self, next_probability):
        if self.delayed is None:
            return np.empty(0, np.float32)
        x, probability = self.delayed
        self.delayed = None
        # Protect the start and end of a phrase using adjacent confidence.
        # Only one 160 ms block is held. Uncertain speech is attenuated, not cut.
        confidence = max(probability, .78 * self.previous_probability, .95 * next_probability)
        fraction = float(np.clip((confidence - SOFT_LOW) / (SOFT_HIGH - SOFT_LOW), 0, 1))
        level = SOFT_FLOOR + (1 - SOFT_FLOOR) * fraction ** 2 * (3 - 2 * fraction)
        gain = np.full(len(x), level, np.float32)
        if self.previous_gain is not None:
            ramp = min(len(x), round(SR * .012))
            gain[:ramp] = np.linspace(self.previous_gain, level, ramp, endpoint=False)
        self.previous_probability = probability
        self.previous_gain = level
        return self._enhance(x, gain)

    def _block(self, x):
        padded = np.pad(x, (0, BLOCK - len(x))) if len(x) < BLOCK else x
        started = time.perf_counter()
        probability = self.guard.push(padded)
        self.guard_times.append((time.perf_counter() - started) * 1000)
        if not np.isfinite(probability) or not 0 <= probability <= 1:
            raise RuntimeError("本人检测概率异常，已停止处理。")
        self.probabilities.append(float(probability))
        if self.smooth:
            output = self._release_delayed(probability)
            self.delayed = (x.copy(), probability)
            return output
        allowed = probability >= self.threshold
        return self._enhance(x, np.full(len(x), float(allowed), np.float32))

    def push(self, x):
        if self.closed:
            raise RuntimeError("该处理流已结束。")
        x = np.asarray(x, np.float32)
        if x.ndim != 1 or not np.isfinite(x).all():
            raise ValueError("音频数值异常")
        self.pending = np.concatenate((self.pending, x))
        out = []
        while len(self.pending) >= BLOCK:
            out.append(self._block(self.pending[:BLOCK]))
            self.pending = self.pending[BLOCK:]
        return np.concatenate(out) if out else np.zeros(0, np.float32)

    def flush(self):
        if self.closed:
            return np.zeros(0, np.float32)
        self.closed = True
        out = []
        if len(self.pending):
            out.append(self._block(self.pending))
            self.pending = np.zeros(0, np.float32)
        if self.smooth:
            out.append(self._release_delayed(0.0))
        out.append(self._aligned(self.enhancer.flush()))
        if len(self.mask):
            raise RuntimeError("本人检测处理未输出完整音频。")
        return np.concatenate(out)


class GuardedModel:
    requires_voice = True
    processing_block_ms = 160

    def __init__(self, base, threshold=.65, guard_factory=None, smooth=False):
        self.base = base
        self.threshold = float(threshold)
        self.guard_factory = guard_factory
        self.smooth = smooth
        self.precision = base.precision
        self.backend = base.backend + ("+soft-speaker-guard" if smooth else "+speaker-guard")

    def load(self):
        self.base.load()
        if self.guard_factory is None:
            from .pvad import Guard
            Guard.load_models()
        return self

    def stream(self, voice):
        self.load()
        if self.guard_factory is None:
            from .pvad import Guard
            factory = Guard
        else:
            factory = self.guard_factory
        if self.guard_factory is None and self.smooth:
            detector = factory(voice, window_samples=BLOCK * SOFT_WINDOW_BLOCKS)
        else:
            detector = factory(voice)
        return GatedStream(self.base.stream(voice), detector, self.threshold, self.smooth)

    def process(self, x, voice, cancel=None, progress=None, chunk_ms=40):
        x = np.asarray(x, np.float32)
        if x.ndim != 1 or not len(x) or not np.isfinite(x).all():
            raise ValueError("输入音频为空或异常")
        if chunk_ms not in (20, 40):
            raise ValueError("分块必须为 20 或 40 ms")
        if cancel and cancel.is_set():
            raise InterruptedError("已取消")
        stream = self.stream(voice)
        out, times = [], []
        step = SR * chunk_ms // 1000
        for i in range(0, len(x), step):
            if cancel and cancel.is_set():
                raise InterruptedError("已取消")
            started = time.perf_counter()
            out.append(stream.push(x[i:i + step]))
            times.append(time.perf_counter() - started)
            if progress and i % SR < step:
                progress(i / len(x))
        if cancel and cancel.is_set():
            raise InterruptedError("已取消")
        started = time.perf_counter()
        out.append(stream.flush())
        times.append(time.perf_counter() - started)
        y = np.concatenate(out)
        if len(y) != len(x) or not np.isfinite(y).all():
            raise RuntimeError("本人检测处理输出异常。")
        probabilities = np.asarray(stream.probabilities)
        stats = {
            "backend": self.backend, "precision": self.precision, "chunk_ms": chunk_ms,
            "audio_seconds": len(x) / SR, "processing_seconds": sum(times),
            "rtf": sum(times) / (len(x) / SR), "chunk_p95_ms": float(np.percentile(times, 95) * 1000),
            "guard_frame_ms": 160,
            "guard_lookahead_ms": 160 if self.smooth else 0,
            "guard_p95_ms": float(np.percentile(stream.guard_times, 95)),
            "guard_probability_mean": float(np.mean(probabilities)),
        }
        if self.smooth:
            stats.update(guard_gain_floor=SOFT_FLOOR, guard_confidence_range=[SOFT_LOW, SOFT_HIGH])
        else:
            stats.update(guard_threshold=self.threshold,
                         guard_rejected_fraction=float(np.mean(probabilities < self.threshold)))
        return np.clip(y, -.98, .98), stats
