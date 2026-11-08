from __future__ import annotations

import json
import logging
import os
import ctypes
from contextlib import closing, contextmanager
from pathlib import Path
import queue
import sys
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd
import soundfile as sf
import soxr
from .realtime import AudioBuffer, ClockController, LiveSettings, PermissionTimeline

SR = 16000
ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "MuterPrototype"
PROFILE = DATA / "voice.wav"
MODEL = ROOT / "models" / "voxlock" / "onnx" / "v2"


@contextmanager
def windows_audio_thread():
    """WASAPI callback streams need COM on their owning Python thread.

    PortAudio initialization on the GUI thread does not initialize a worker.
    Balance successful COM initialization on the same thread after stream close.
    """
    initialized = False
    if sys.platform == "win32":
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0)
        initialized = hr >= 0
    try:
        yield
    finally:
        if initialized:
            ctypes.windll.ole32.CoUninitialize()


def dbfs(x):
    return float(20 * np.log10(max(float(np.sqrt(np.mean(np.square(x)))), 1e-6))) if len(x) else -120.0


def mono(x):
    x = np.asarray(x, dtype=np.float32)
    return x.mean(axis=1) if x.ndim == 2 else x


def load_voice(path=PROFILE, with_report=False):
    x, rate = sf.read(str(path), dtype="float32", always_2d=True)
    x = mono(x)
    if rate != SR:
        x = soxr.resample(x, rate, SR).astype(np.float32)
    from .voice import select_reference
    selected, report = select_reference(x, SR)
    return (selected, report) if with_report else selected


def devices(refresh=False):
    """Prefer WASAPI; hide duplicate endpoints exposed by other host APIs."""
    if refresh:
        sd._terminate()
        sd._initialize()
    all_devices = sd.query_devices()
    apis = sd.query_hostapis()
    wasapi = next((i for i, a in enumerate(apis) if a["name"] == "Windows WASAPI"), None)
    chosen = [(i, dict(d)) for i, d in enumerate(all_devices) if wasapi is None or d["hostapi"] == wasapi]
    if wasapi is not None:
        defaults = (apis[wasapi]["default_input_device"], apis[wasapi]["default_output_device"])
    else:
        defaults = tuple(sd.default.device)
    inputs = [(i, d) for i, d in chosen if d["max_input_channels"] > 0]
    outputs = [(i, d) for i, d in chosen if d["max_output_channels"] > 0]
    current_out = next((d["name"] for i, d in outputs if i == defaults[1]), "")
    current_in = next((d["name"] for i, d in inputs if i == defaults[0]), "")
    if "hands-free" in current_out.lower() and "hands-free" not in current_in.lower():
        stem = current_out.lower().split("hands-free")[0].split("(")[-1].strip()
        stereo = next((i for i, d in outputs if stem in d["name"].lower() and "stereo" in d["name"].lower()), None)
        if stereo is not None:
            defaults = (defaults[0], stereo)
    from .device_policy import choose, family, names
    physical_in = choose(inputs, defaults[0], capture=True) if inputs else -1
    physical_out = choose(outputs, defaults[1], input_family=family(names(inputs, physical_in))) if outputs else -1
    return inputs, outputs, (physical_in, physical_out)


def stream_config(index, kind):
    d = sd.query_devices(index)
    rate = int(d["default_samplerate"])
    channels = min(2, d[f"max_{kind}_channels"])
    if channels < 1:
        raise ValueError("所选音频设备不可用。请刷新设备列表。")
    return rate, channels


class Model:
    requires_voice = True
    backend = "voxlock"
    def __init__(self, precision="int8", threads=2):
        if precision not in ("int8", "fp32"):
            raise ValueError("不支持的模型精度")
        self.precision, self.threads = precision, threads
        self.vl = None
        self.lock = threading.Lock()

    def load(self):
        with self.lock:
            if self.vl is None:
                chunk_file = "chunk_int8.onnx" if self.precision == "int8" else "chunk.onnx"
                if not all((MODEL / n).is_file() for n in ("enroll.onnx", chunk_file)):
                    raise FileNotFoundError("缺少本地模型。请运行 tools/prepare_model.py，或使用包含 models 文件夹的完整版本。")
                from voxlock import VoxLock
                self.vl = VoxLock(str(MODEL), precision=self.precision, threads=self.threads)
        return self.vl

    def stream(self, voice):
        return self.load().stream(voice)

    def process(self, x, voice, cancel=None, progress=None, chunk_ms=40):
        if chunk_ms not in (20, 40):
            raise ValueError("分块必须为 20 或 40 ms")
        x = np.asarray(x, np.float32)
        if x.ndim != 1 or not len(x) or not np.isfinite(x).all():
            raise ValueError("输入音频为空或数值异常")
        chunk_samples = SR * chunk_ms // 1000
        stream = self.stream(voice)
        out, times = [], []
        for start in range(0, len(x), chunk_samples):
            if cancel and cancel.is_set():
                raise InterruptedError("已取消")
            chunk = x[start:start + chunk_samples]
            t = time.perf_counter()
            y = stream.push(chunk)
            times.append(time.perf_counter() - t)
            if not np.isfinite(y).all():
                raise RuntimeError("模型输出异常，已停止处理。")
            out.append(y)
            if progress and start % SR < 640:
                progress(min(1.0, start / max(len(x), 1)))
        out.append(stream.flush())
        y = np.concatenate(out)[:len(x)]
        if len(y) != len(x) or not np.isfinite(y).all():
            raise RuntimeError("模型输出长度或数值异常，未生成结果。")
        return np.clip(y, -.98, .98), {
            "audio_seconds": len(x) / SR,
            "processing_seconds": sum(times),
            "rtf": sum(times) / max(len(x) / SR, 1e-6),
            "chunk_p95_ms": float(np.percentile(times, 95) * 1000) if times else 0,
            "precision": self.precision, "chunk_ms": chunk_ms,
            "backend": self.backend,
        }


@windows_audio_thread()
def record(index, seconds, cancel, tick=None, native=False):
    rate, channels = stream_config(index, "input")
    blocks = []
    block = round(rate * .04)
    total = round(seconds * rate)
    got = 0
    with sd.InputStream(device=index, samplerate=rate, channels=channels, dtype="float32", blocksize=block) as stream:
        while got < total:
            if cancel.is_set():
                raise InterruptedError("已取消录音")
            audio, overflow = stream.read(min(block, total - got))
            if overflow:
                raise RuntimeError("录音设备发生丢帧，请关闭占用设备的程序后重试。")
            blocks.append(mono(audio))
            got += len(audio)
            if tick:
                tick(got / rate, dbfs(audio))
    x = np.concatenate(blocks)
    if native:
        return x, rate
    return soxr.resample(x, rate, SR).astype(np.float32) if rate != SR else x


class LiveAudio:
    """Conference output fails closed; original A/B audio is headphone-only."""
    def __init__(self, model, voice, input_device, output_device, events, settings=None, transmit_allowed=None,
                 monitor_only=False):
        self.settings = settings or LiveSettings()
        self.model, self.voice = model, voice
        self.input_device, self.output_device = input_device, output_device
        self.events = events
        self.transmit_allowed = transmit_allowed
        self.cancel = threading.Event()
        self.muted = threading.Event()
        self.error = None
        self.input_queue = queue.Queue(maxsize=48)
        self.thread = None
        self.peak_in = self.peak_out = -120.0
        self.times = deque(maxlen=100)
        self.starved_frames = 0
        self.underruns = 0
        self.ratio_adjustment = 0.0
        self.last_output_callback = None
        self.capture_recovering = threading.Event()
        self.monitor_only = monitor_only
        self.monitor_original = threading.Event()
        self.input_callbacks = self.output_callbacks = 0
        self.last_input_callback = None

    def set_monitor_original(self, enabled):
        if not self.monitor_only:
            raise ValueError("原声比较只允许用于本地耳机试听。")
        if enabled:
            self.monitor_original.set()
        else:
            self.monitor_original.clear()

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="muter-audio")
        self.thread.start()

    def stop(self):
        self.muted.set()
        self.cancel.set()

    def fail(self, reason):
        if self.error is None:
            self.error = str(reason)
        self.stop()

    def can_transmit(self):
        if self.cancel.is_set() or self.muted.is_set():
            return False
        try:
            return self.transmit_allowed is None or bool(self.transmit_allowed())
        except Exception:
            return False

    def _input(self, data, frames, timing, status):
        self.input_callbacks += 1
        self.last_input_callback = time.monotonic()
        if status:
            self.fail(f"麦克风流异常：{status}。已停止并静音。")
        if self.cancel.is_set():
            return
        try:
            self.input_queue.put_nowait((time.monotonic(), mono(data).copy(), self.can_transmit()))
        except queue.Full:
            self.fail("处理跟不上录音速度，已停止并静音。")

    def _output(self, data, frames, timing, status):
        self.output_callbacks += 1
        data.fill(0)
        self.last_output_callback = time.monotonic()
        if self.cancel.is_set():
            return
        if status:
            self.fail(f"输出音频流异常：{status}。已停止并静音。")
            return
        if self.capture_recovering.is_set():
            return
        if not self.ready:
            if self.buffer.size < self.prefill:
                return
            self.ready = True
        missing = self.buffer.pop_into(data)
        if missing:
            self.underruns += 1
            self.starved_frames += missing
            if self.starved_frames > self.output_rate * .5:
                self.fail("模型输出持续中断，已停止并静音。")
                data.fill(0)
                return
        else:
            self.starved_frames = 0
        if not self.can_transmit():
            data.fill(0)

    def _capture(self, rate, channels):
        """Treat missing callbacks as a transport gap, never as absent speech.

        Silence packets are ordinary packets, regardless of their level. Keep
        the render endpoint open and silent while retrying the same microphone.
        Never switch to a different person's microphone or forward raw input.
        """
        gap_started = None
        attempts = 0
        restarts = deque()
        while not self.cancel.is_set():
            try:
                with sd.InputStream(device=self.input_device, samplerate=rate,
                                    channels=channels, dtype="float32", blocksize=round(rate * .01),
                                    callback=self._input, latency="low") as source:
                    opened = time.monotonic()
                    while not self.cancel.is_set():
                        try:
                            captured = self.input_queue.get(timeout=.25)
                        except queue.Empty:
                            if self.cancel.is_set():
                                return
                            now = time.monotonic()
                            if gap_started is None:
                                gap_started = now
                                self.capture_recovering.set()
                                self.ready = False
                                self.buffer.clear()
                                self.peak_in = self.peak_out = -120.0
                                self.events.put(("capture_recovering", "麦克风数据暂时中断，正在自动恢复；会议输出暂时静音。"))
                                logging.warning("capture_gap device=%s source_active=%s callbacks=%s age_seconds=%.3f",
                                                self.input_device, source.active, self.input_callbacks, now - opened)
                            if source.active and now - opened < 1.5:
                                continue
                            break
                        # Zero-valued packets count as healthy capture. No VAD
                        # or volume threshold participates in device health.
                        recovered = gap_started is not None
                        if recovered:
                            logging.info("capture_resumed device=%s gap_seconds=%.3f restarts=%s",
                                         self.input_device, time.monotonic() - gap_started, attempts)
                        gap_started = None
                        attempts = 0
                        opened = time.monotonic()
                        yield captured, recovered, source.latency
            except sd.PortAudioError:
                logging.exception("capture_stream_failed device=%s attempt=%s", self.input_device, attempts)
                if self.cancel.is_set():
                    return
                if gap_started is None:
                    gap_started = time.monotonic()
                    self.capture_recovering.set()
                    self.ready = False
                    self.buffer.clear()
            if self.cancel.is_set():
                return
            now = time.monotonic()
            while restarts and now - restarts[0] > 30:
                restarts.popleft()
            if attempts >= 3 or len(restarts) >= 3 or now - gap_started >= 8:
                raise RuntimeError("麦克风数据持续中断，自动恢复未成功；会议输出保持静音。与说话停顿无关。")
            attempts += 1
            restarts.append(now)
            logging.warning("capture_reopen device=%s attempt=%s", self.input_device, attempts)
            self.events.put(("capture_recovering", f"正在自动重连原麦克风（{attempts}/3）；会议输出暂时静音。"))
            # The old stream is closed before reopening. Discard its queued
            # packets so a reconnect cannot replay audio from before the gap.
            while True:
                try:
                    self.input_queue.get_nowait()
                except queue.Empty:
                    break
            if self.cancel.wait(.2):
                return

    @windows_audio_thread()
    def _run(self):
        try:
            self.events.put(("status", "正在准备本地模型…"))
            stream = self.model.stream(self.voice)
            # Warm runtime kernels before acquiring a microphone. Discard this state.
            stream.push(np.zeros(SR // 5, np.float32))
            stream = self.model.stream(self.voice)
            if self.cancel.is_set():
                return
            in_rate, in_channels = stream_config(self.input_device, "input")
            self.output_rate, out_channels = stream_config(self.output_device, "output")
            logging.info("audio_start input=%s output=%s input_rate=%s output_rate=%s channels=%s/%s "
                         "model=%s backend=%s precision=%s settings=%s monitor_only=%s",
                         dict(sd.query_devices(self.input_device)), dict(sd.query_devices(self.output_device)),
                         in_rate, self.output_rate, in_channels, out_channels,
                         type(self.model).__name__, getattr(self.model, "backend", "custom"),
                         getattr(self.model, "precision", "default"),
                         self.settings, self.monitor_only)
            if self.monitor_only:
                from .device_policy import headphone
                if not headphone(sd.query_devices(self.output_device)["name"]):
                    raise RuntimeError("实时试听只允许输出到实体耳机。")
            down = soxr.ResampleStream(in_rate, SR, 1, dtype="float32", quality="LQ") if in_rate != SR else None
            # Variable-rate output resampling gradually tracks independent device clocks.
            up = soxr.ResampleStream(SR * 1.002, self.output_rate, 1, dtype="float32", quality="LQ", vr=True)
            up.set_io_ratio(SR, self.output_rate)
            clock = ClockController()
            block_ms = getattr(self.model, "processing_block_ms", self.settings.chunk_ms)
            if not isinstance(block_ms, (int, float)):
                block_ms = self.settings.chunk_ms
            effective_buffer_ms = max(self.settings.buffer_ms, block_ms)
            self.buffer = AudioBuffer(round(self.output_rate * max(.3, (effective_buffer_ms + block_ms + 160) / 1000)))
            self.prefill = round(self.output_rate * effective_buffer_ms / 1000)
            self.ready = False
            chunk_samples = SR * self.settings.chunk_ms // 1000
            pending = np.zeros(0, np.float32)
            permissions = PermissionTimeline()
            if self.cancel.is_set():
                return
            self.last_output_callback = time.monotonic()
            with sd.OutputStream(device=self.output_device, samplerate=self.output_rate,
                                 channels=out_channels, dtype="float32", blocksize=round(self.output_rate * .01),
                                 callback=self._output, latency="low") as output:
                if self.cancel.is_set():
                    return
                with closing(self._capture(in_rate, in_channels)) as capture:
                    started = False
                    original = False
                    last_event = time.monotonic()
                    last_log = last_event
                    for captured, recovered, input_latency in capture:
                        if self.cancel.is_set():
                            break
                        captured_at, x = captured[:2]
                        allowed = captured[2] if len(captured) > 2 else True
                        requested_original = self.monitor_only and self.monitor_original.is_set()
                        mode_changed = requested_original != original
                        if recovered or mode_changed:
                            # Clear model and resampler history as well as render
                            # buffering. Resume only with newly processed audio.
                            stream = self.model.stream(self.voice)
                            down = soxr.ResampleStream(in_rate, SR, 1, dtype="float32", quality="LQ") if in_rate != SR else None
                            up = soxr.ResampleStream(SR * 1.002, self.output_rate, 1, dtype="float32", quality="LQ", vr=True)
                            up.set_io_ratio(SR, self.output_rate)
                            clock = ClockController()
                            pending = np.zeros(0, np.float32)
                            permissions = PermissionTimeline()
                            self.buffer.clear()
                            self.starved_frames = 0
                            self.ready = False
                            self.ratio_adjustment = 0.0
                            self.capture_recovering.clear()
                            if recovered and started:
                                self.events.put(("capture_recovered", "麦克风数据已恢复，降噪继续运行。"))
                        original = requested_original
                        if mode_changed:
                            logging.info("monitor_mode original=%s", original)
                            self.events.put(("monitor_mode", "实时试听：原声（不保存录音）。" if original else "实时试听：降噪后（不保存录音）。"))
                        if not started:
                            started = True
                            self.events.put(("live_started", {"input_rate": in_rate, "output_rate": self.output_rate,
                                                             "input_latency": input_latency, "output_latency": output.latency}))
                        self.peak_in = dbfs(x)
                        if time.monotonic() - captured_at > self.settings.max_input_age_ms / 1000:
                            raise RuntimeError("音频处理延迟过高，已停止并静音。请使用均衡模式或更大的缓冲。")
                        if not np.isfinite(x).all():
                            raise RuntimeError("麦克风音频异常，已停止并静音。")
                        if self.last_output_callback and time.monotonic() - self.last_output_callback > .25:
                            raise RuntimeError("输出设备没有持续接收音频。蓝牙耳机请改选 Stereo 立体声通道，或切换输出设备。")
                        if not allowed:
                            # Resamplers retain history: zero before conversion so a
                            # later key press cannot authorize an earlier forbidden frame.
                            x = np.zeros_like(x)
                        x = down.resample_chunk(x) if down else x
                        permissions.append(len(x), allowed)
                        pending = np.concatenate([pending, x])
                        while len(pending) >= chunk_samples and not self.cancel.is_set():
                            t = time.perf_counter()
                            y = pending[:chunk_samples].copy() if original else stream.push(pending[:chunk_samples])
                            pending = pending[chunk_samples:]
                            self.times.append((time.perf_counter() - t) * 1000)
                            if not np.isfinite(y).all():
                                raise RuntimeError("模型输出异常，已停止并静音。")
                            y = permissions.apply(y)
                            self.peak_out = dbfs(y)
                            if self.ready:
                                self.ratio_adjustment = clock.update(time.monotonic(), self.buffer.size / self.output_rate)
                                up.set_io_ratio(SR * (1 + self.ratio_adjustment), self.output_rate,
                                                slew_len=round(self.output_rate * .2))
                            y = up.resample_chunk(y)
                            self.buffer.push(np.clip(y, -.98, .98).astype(np.float32))
                        if not output.active:
                            raise RuntimeError("输出设备已断开，已停止并静音。")
                        if time.monotonic() - last_event >= .15:
                            self.events.put(("meters", {"input": self.peak_in, "output": self.peak_out,
                                "p95": float(np.percentile(self.times, 95)) if self.times else 0,
                                "buffer_ms": self.buffer.size / self.output_rate * 1000, "underruns": self.underruns,
                                "clock_ppm": self.ratio_adjustment * 1e6, "chunk_ms": block_ms,
                                "input_age_ms": (time.monotonic() - captured_at) * 1000}))
                            last_event = time.monotonic()
                        if time.monotonic() - last_log >= 10:
                            logging.info("audio_health input_callbacks=%s output_callbacks=%s queue=%s "
                                         "buffer_ms=%.1f underruns=%s p95_ms=%.2f clock_ppm=%.1f",
                                         self.input_callbacks, self.output_callbacks, self.input_queue.qsize(),
                                         self.buffer.size / self.output_rate * 1000, self.underruns,
                                         float(np.percentile(self.times, 95)) if self.times else 0,
                                         self.ratio_adjustment * 1e6)
                            last_log = time.monotonic()
        except Exception as exc:
            logging.exception("audio_failed input_device=%s output_device=%s", self.input_device, self.output_device)
            self.fail(str(exc))
        finally:
            logging.info("audio_stop error=%s input_callbacks=%s output_callbacks=%s underruns=%s",
                         self.error, self.input_callbacks, self.output_callbacks, self.underruns)
            self.events.put(("live_stopped", self.error))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
