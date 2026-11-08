"""Bounded audio storage and a slow device-clock servo (no speaker decisions)."""
from dataclasses import dataclass
import math
import threading

import numpy as np


@dataclass(frozen=True)
class LiveSettings:
    buffer_ms: int = 80
    chunk_ms: int = 40
    max_input_age_ms: int = 200

    def __post_init__(self):
        if self.buffer_ms not in (60, 80, 120, 160):
            raise ValueError("输出缓冲必须是 60、80、120 或 160 ms")
        if self.chunk_ms not in (20, 40):
            raise ValueError("推理分块必须是 20 或 40 ms")


class ClockController:
    """Learn the normal packetized buffer level, then correct slow clock drift.

    Returns fractional input-rate adjustment, capped at +/-1500 ppm. The audio
    resampler slews between ratios; no samples or speech segments are dropped.
    Short inference jitter is averaged rather than chased. Time is supplied by
    the caller for repeatable long-duration simulation.
    """
    def __init__(self):
        self.previous_time = None
        self.elapsed = 0.0
        self.mean = None
        self.baseline = None
        self.adjustment = 0.0

    def update(self, now, level_seconds):
        if not math.isfinite(level_seconds) or level_seconds < 0:
            raise ValueError("Invalid audio buffer level")
        if self.previous_time is None:
            self.previous_time = now
            self.mean = level_seconds
            return 0.0
        dt = max(0.0, min(now - self.previous_time, 1.0))
        self.previous_time = now
        self.elapsed += dt
        weight = 1 - math.exp(-dt / 2.0)
        self.mean += weight * (level_seconds - self.mean)
        if self.elapsed < 4.0:
            return 0.0
        if self.baseline is None:
            self.baseline = self.mean
        error = self.mean - self.baseline
        desired = float(np.clip(error * .035, -.0015, .0015))
        # Max 100 ppm change/second avoids abrupt pitch modulation.
        step = .0001 * dt
        self.adjustment += float(np.clip(desired - self.adjustment, -step, step))
        return self.adjustment


class AudioBuffer:
    """Preallocated circular mono buffer; render directly into PortAudio output."""
    def __init__(self, capacity):
        self.capacity = int(capacity)
        self.data = np.zeros(self.capacity, np.float32)
        self.read_at = self.write_at = self.size = 0
        self.lock = threading.Lock()

    def push(self, x):
        count = len(x)
        if not count:
            return
        with self.lock:
            if self.size + count > self.capacity:
                raise RuntimeError("输出积压过多，已停止并静音。请重新启动处理。")
            first = min(count, self.capacity - self.write_at)
            self.data[self.write_at:self.write_at + first] = x[:first]
            if first < count:
                self.data[:count - first] = x[first:]
            self.write_at = (self.write_at + count) % self.capacity
            self.size += count

    def pop_into(self, output):
        count = len(output)
        output.fill(0)
        with self.lock:
            n = min(count, self.size)
            first = min(n, self.capacity - self.read_at)
            if output.ndim == 2:
                output[:first] = self.data[self.read_at:self.read_at + first, None]
                if first < n:
                    output[first:n] = self.data[:n - first, None]
            else:
                output[:first] = self.data[self.read_at:self.read_at + first]
                if first < n:
                    output[first:n] = self.data[:n - first]
            self.read_at = (self.read_at + n) % self.capacity
            self.size -= n
        return count - n

    def pop(self, count):
        # Convenience for non-callback callers and diagnostics.
        output = np.zeros(count, np.float32)
        return output, self.pop_into(output)

    def clear(self):
        with self.lock:
            self.read_at = self.write_at = self.size = 0


class PermissionTimeline:
    """Track capture-time permission through delayed model waveform output."""
    def __init__(self):
        self.pending = np.zeros(0, np.bool_)

    def append(self, count, allowed):
        if len(self.pending) + count > 32000:
            raise RuntimeError("模型连续两秒未输出完整音频，已停止并静音。")
        self.pending = np.concatenate((self.pending, np.full(count, allowed, np.bool_)))

    def apply(self, audio):
        if len(audio) > len(self.pending):
            raise RuntimeError("处理输出与采集时序不一致，已停止并静音。")
        result = audio * self.pending[:len(audio)]
        self.pending = self.pending[len(audio):]
        return result
