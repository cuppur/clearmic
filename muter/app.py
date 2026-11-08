from __future__ import annotations

import ctypes
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
import webbrowser

import numpy as np
import sounddevice as sd
import soundfile as sf

from .audio import DATA, PROFILE, ROOT, SR, LiveAudio, Model, dbfs, devices, load_voice, record, stream_config, write_json, windows_audio_thread
from . import APP_NAME
from .realtime import LiveSettings
from .desktop_ui import DesktopUI
from .window_dpi import enable_per_monitor_dpi

AMBIENT, ISOLATION, STRONG, FULL_PRECISION = "环境降噪", "人声隔离", "加强人声隔离", "人声隔离 · FP32"
MODE_DESCRIPTIONS = {
    AMBIENT: "压低风扇、键盘等环境声，无需声音样本。",
    ISOLATION: "用本人声音样本区分说话人，适合日常会议。",
    STRONG: "加强对旁人和电视人声的抑制，增加处理延迟。",
    FULL_PRECISION: "以全精度运行人声隔离，可用于听感对比。",
}


class App(DesktopUI, tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        DATA.mkdir(parents=True, exist_ok=True)
        from .diagnostics import configure
        configure(DATA)
        self.events = queue.Queue()
        from .denoise import GTCRNModel
        from .routing import RouteSession
        self.model = GTCRNModel()
        self.models = {"gtcrn": self.model}
        self.route = RouteSession()
        self.conference_route = False
        self.model_generation = 0
        self.model_threads = []
        self.poll_timer = None
        self.worker = None
        self.model_ready = False
        self.cancel = threading.Event()
        self.live = None
        self.busy = False
        self.job = None
        self.last_test = None
        self.voice = None
        self.voice_report = None
        self.closed = False
        self.tray = None
        self.paused = False
        self.hold_pressed = False
        self.ptt_var = tk.BooleanVar(value=False)
        self.test_mode_var = tk.BooleanVar(value=False)
        self.monitoring = False
        self.monitor_previous_output = None
        self.inputs = {}
        self.outputs = {}
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.quality_var = tk.StringVar(value=AMBIENT)
        self.mode_hint = tk.StringVar(value=MODE_DESCRIPTIONS[AMBIENT])
        self.voice_summary = tk.StringVar()
        self.mic_hint = tk.StringVar(value="等待识别麦克风")
        self.input_level = tk.StringVar(value="— dBFS")
        self.output_level = tk.StringVar(value="— dBFS")
        self.latency_var = tk.StringVar(value="均衡 · 80 ms 缓冲")
        self.status_var = tk.StringVar(value="正在载入本地人声模型…")
        self.voice_var = tk.StringVar(value="环境降噪与试听可直接使用。人声隔离需录入样本。")
        self.route_var = tk.StringVar()
        self.stats_var = tk.StringVar(value="推理 P95：—    输出缓冲：—    欠载：—")
        self.test_var = tk.StringVar(value="戴上耳机，边说边听并比较降噪效果。")
        self.initialize_desktop(DATA / "appearance.json", tuple(MODE_DESCRIPTIONS))
        self.input_var.trace_add("write", self.update_mic_hint)
        try:
            repaired = self.route.api.repair_virtual_defaults()
            if repaired: logging.info("Automatically repaired virtual defaults: %s", repaired)
        except Exception:
            logging.exception("Default-device recovery failed")
        self.refresh_devices()
        try:
            if PROFILE.exists():
                self.voice, self.voice_report = load_voice(with_report=True)
                self.show_voice_report()
        except Exception:
            self.voice_var.set("原有声音样本不可用，请重新录入。")
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.poll_timer = self.after(100, self.poll)
        if self.voice is None:
            self.start_model_load()
        else:
            self.quality_var.set(ISOLATION)
            self.change_model()

        self.deiconify()

    def hide_to_tray(self):
        if self.closed:
            return
        if self.tray and self.tray.registered:
            self.withdraw()
            logging.info("window_hidden_to_tray")
        else:
            self.close()

    def show_window(self):
        if not self.closed:
            self.deiconify()
            self.lift()

    def show_settings(self):
        if self.closed:
            return
        self.show_window()
        if not self.settings_card.winfo_manager():
            self.toggle_settings()

    def choose_tray_mode(self, mode):
        if self.closed or self.busy or mode not in MODE_DESCRIPTIONS:
            return
        if self.quality_var.get() != mode:
            self.quality_var.set(mode)
            self.change_model()
        if self.model.requires_voice and self.voice is None:
            self.show_window()

    def toggle_tray_monitor(self):
        self.test_btn.invoke()
        if self.live is None and not self.busy:
            self.show_window()

    def toggle_conference(self):
        if self.busy or self.conference_route:
            self.stop()
        else:
            self.quick_start()

    def refresh_devices(self):
        if self.busy:
            return
        try:
            previous_in, previous_out = self.input_var.get(), self.output_var.get()
            from .device_policy import virtual
            ins, outs, defaults = devices(refresh=True)
            ins = [(i,d) for i,d in ins if not virtual(d['name'])]
            self.inputs = {f"{i}  ·  {d['name']}": i for i, d in ins}
            self.outputs = {f"{i}  ·  {d['name']}": i for i, d in outs}
            self.input_combo["values"] = list(self.inputs)
            self.output_combo["values"] = list(self.outputs)
            self.input_var.set(previous_in if previous_in in self.inputs else next((k for k, v in self.inputs.items() if v == defaults[0]), next(iter(self.inputs), "")))
            preferred = next((k for k in self.outputs if "cable input" in k.lower()), None)
            selected_out = previous_out if previous_out in self.outputs else preferred or next((k for k, v in self.outputs.items() if v == defaults[1]), next(iter(self.outputs), ""))
            if not previous_out and "hands-free" in selected_out.lower():
                stem = selected_out.lower().split("hands-free")[0].split("(")[-1].strip()
                selected_out = next((k for k in self.outputs if stem in k.lower() and "stereo" in k.lower()), selected_out)
            self.output_var.set(selected_out)
            self.update_route()
        except Exception as exc:
            self.status_var.set(f"读取设备失败：{exc}")

    def restore_test(self):
        try:
            folders = sorted((DATA / "tests").glob("*"), reverse=True)
            for folder in folders:
                if all((folder / name).is_file() for name in ("original.wav", "processed.wav", "metrics.json")):
                    stats = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
                    self.last_test = folder
                    self.test_var.set(f"已恢复上次对比录音（{stats['audio_seconds']:.0f} 秒）。可直接回放，也可以重新测试。")
                    self.sync_controls()
                    break
        except Exception:
            logging.exception("Could not restore previous test")

    def update_route(self):
        if "cable input" in self.output_var.get().lower():
            self.route_var.set("虚拟会议输出已就绪；一键开启会自动配置系统麦克风。")
        else:
            self.route_var.set("当前为本地试听。蓝牙耳机优先选 Stereo；接入会议需选虚拟音频设备。")

    def show_voice_report(self):
        r = self.voice_report
        if not r:
            return
        warning = "；".join(r["warnings"][:1])
        self.voice_var.set(f"参考选段 {r['selected_start_seconds']:.1f}–{r['selected_start_seconds'] + 3:.1f} 秒"
                           f" · 有效声音 {r['active_audio_seconds']:.1f} 秒" + (f" · {warning}" if warning else ""))

    def voice_info(self):
        r = self.voice_report
        if not r:
            return
        messagebox.showinfo("参考语音质量", f"选段：第 {r['selected_start_seconds']:.2f} 秒开始的连续 3 秒\n"
            f"有效声音：{r['active_audio_seconds']:.2f} 秒\n音量：{r['selected_rms_dbfs']:.1f} dBFS\n"
            f"爆音比例：{r['selected_clipping_fraction']:.2%}\n\n" + "\n".join(r["warnings"] or ["未发现明显音量或爆音问题。"])
            + "\n\n这些检查只评估信号质量，无法判断说话人身份。请确认录音中只有你讲话。")

    def selected_settings(self):
        return {"低延迟 · 60 ms 缓冲": LiveSettings(60, 20),
                "均衡 · 80 ms 缓冲": LiveSettings(80, 40),
                "稳健 · 160 ms 缓冲": LiveSettings(160, 40)}[self.latency_var.get()]

    def change_model(self, _=None):
        mode = self.quality_var.get()
        if mode == AMBIENT:
            from .denoise import GTCRNModel
            if "gtcrn" not in self.models:
                self.models["gtcrn"] = GTCRNModel()
            self.model = self.models["gtcrn"]
        else:
            precision = "fp32" if mode == FULL_PRECISION else "int8"
            if precision not in self.models:
                self.models[precision] = Model(precision, threads=min(4, os.cpu_count() or 1))
            self.model = self.models[precision]
        if mode == STRONG:
            from .pipeline import GuardedModel
            self.model = GuardedModel(self.model, smooth=True)
        self.model_ready = False
        self.status_var.set("正在准备所选处理模式…")
        self.sync_controls()
        self.start_model_load()

    def start_model_load(self):
        self.model_generation += 1
        thread = threading.Thread(target=self._load_model, args=(self.model, self.model_generation), daemon=True)
        self.model_threads = [t for t in self.model_threads if t.is_alive()]
        self.model_threads.append(thread)
        thread.start()

    def sync_controls(self):
        idle = not self.busy
        has_voice = self.voice is not None
        can_process = has_voice or not self.model.requires_voice
        can_start = idle and can_process and self.model_ready
        can_stop = (self.busy and not self.monitoring) or self.conference_route
        action = "停止处理" if self.live or self.conference_route else "取消操作"
        self.quick_btn.configure(state="normal" if can_start or can_stop else "disabled",
                                 text=action if can_stop else "开启会议降噪")
        self.mode_hint.set(MODE_DESCRIPTIONS[self.quality_var.get()])
        self.mode_tag.configure(text="单人会议推荐" if self.quality_var.get() == ISOLATION else
                                "无需声音样本" if not self.model.requires_voice else "使用本人声纹")
        self.voice_summary.set("已准备本人声音样本" if has_voice else
                               "需要本人声音样本" if self.model.requires_voice else
                               "可选 · 本人声音样本")
        if self.busy:
            badge = "已暂停传声" if self.live and self.paused else "耳机试听" if self.monitoring else "处理中"
        elif self.conference_route:
            badge = "已静音"
        elif self.model_ready:
            badge = "准备就绪" if can_process else "等待声音样本"
        else:
            badge = "正在准备"
        self.state_badge.configure(text=f"●  {badge}")
        if self.tray:
            self.tray.set_status(badge)
        for b in (self.voice_btn, self.manage_btn, self.refresh_btn):
            b.configure(state="normal" if idle else "disabled")
        self.start_btn.configure(state="normal" if idle and can_process and self.model_ready else "disabled")
        monitor_available = self.monitoring or (idle and can_process and self.model_ready)
        self.test_btn.configure(state="normal" if monitor_available else "disabled",
                                text="停止实时试听" if self.monitoring else "开启实时试听")
        if self.monitoring:
            self.ab_row.pack(side="right")
        else:
            self.ab_row.pack_forget()
        for b in (self.raw_btn, self.clean_btn):
            b.configure(state="normal" if self.monitoring and self.live else "disabled")
        self.input_combo.configure(state="readonly" if idle else "disabled")
        self.output_combo.configure(state="readonly" if idle else "disabled")
        self.quality_combo.configure(state="readonly" if idle else "disabled")
        self.latency_combo.configure(state="readonly" if idle else "disabled")
        self.ptt_check.configure(state="normal" if idle else "disabled")
        self.hold_btn.configure(state="normal" if self.live and self.busy and self.ptt_var.get() and not self.monitoring else "disabled")
        self.stop_btn.configure(state="normal" if self.busy else "disabled")
        self.mute_btn.configure(state="normal" if self.live and self.busy else "disabled",
                                text="恢复传声" if self.paused else "暂停传声")
        if self.live and self.busy:
            self.mute_btn.grid(padx=(0, self.px(10)))
        else:
            self.mute_btn.grid_remove()
        self.paint_buttons()

    def _load_model(self, model, generation):
        try:
            started = time.perf_counter()
            model.load()
            self.events.put(("model_ready", (model, generation, time.perf_counter() - started)))
        except Exception as exc:
            logging.exception("Model load failed")
            self.events.put(("model_error", (model, generation, f"模型载入失败：{exc}")))

    def launch_job(self, name, fn):
        if self.busy:
            return
        self.busy = True
        self.job = name
        self.cancel = threading.Event()
        self.sync_controls()
        def run():
            try:
                fn()
            except InterruptedError:
                self.events.put(("status", "已取消。"))
            except Exception as exc:
                logging.exception("%s failed", name)
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("job_done", None))
        self.worker = threading.Thread(target=run, daemon=True, name=f"muter-{name}")
        self.worker.start()

    def selected_input(self):
        self.refresh_devices()
        if self.input_var.get() not in self.inputs:
            raise ValueError("请先选择一个麦克风。")
        return self.inputs[self.input_var.get()]

    def enroll(self):
        try:
            index = self.selected_input()
        except Exception as exc:
            messagebox.showerror("选择麦克风", str(exc))
            return
        def work():
            self.events.put(("status", "开始录入：请连续说话 8 秒，周围人保持安静。"))
            x = record(index, 8, self.cancel, lambda sec, level: self.events.put(("record_tick", ("声音录入", sec, 8, level))))
            temp = DATA / "voice.new.wav"
            sf.write(temp, x, SR, subtype="PCM_16")
            voice, report = load_voice(temp, with_report=True)
            if self.cancel.is_set():
                temp.unlink(missing_ok=True)
                raise InterruptedError()
            temp.replace(PROFILE)
            self.events.put(("voice_ready", (voice, report)))
            self.events.put(("status", "声音录入完成。可开启耳机实时试听。"))
        self.launch_job("enroll", work)

    def import_voice(self):
        path = filedialog.askopenfilename(title="导入只有你说话的干净录音（至少 3 秒）", filetypes=[("WAV 音频", "*.wav")])
        if not path:
            return
        try:
            voice, report = load_voice(path, with_report=True)
            sf.write(PROFILE, voice, SR, subtype="PCM_16")
            self.voice, self.voice_report = voice, report
            self.show_voice_report()
            self.status_var.set("声音样本已准备好。")
            self.sync_controls()
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))

    def delete_voice(self):
        PROFILE.unlink(missing_ok=True)
        self.voice = None
        self.voice_report = None
        self.voice_var.set("声音样本已清除。人声隔离和本人检测需重录；普通环境降噪仍可使用。")
        self.status_var.set("已清除本地声音样本。此前主动保存的测试录音仍在测试文件夹。")
        self.sync_controls()

    def test_record(self):
        try:
            index = self.selected_input()
        except Exception as exc:
            messagebox.showerror("选择麦克风", str(exc))
            return
        voice = self.voice.copy() if self.voice is not None else None
        def work():
            self.events.put(("status", "正在录制测试：自己说话 → 旁人说话 → 同时说话。"))
            native, native_rate = record(index, 12, self.cancel, lambda sec, level: self.events.put(("record_tick", ("对比测试", sec, 12, level))), native=True)
            import soxr
            x = soxr.resample(native, native_rate, SR).astype(np.float32) if native_rate != SR else native
            self.process_test_audio(native, native_rate, voice)
        self.launch_job("test", work)

    def process_test_audio(self, native, native_rate, voice):
        import soxr
        x = soxr.resample(native, native_rate, SR).astype(np.float32) if native_rate != SR else native
        if dbfs(x) < -60:
            raise ValueError("几乎没有声音，请检查麦克风选择或音频文件。")
        self.events.put(("status", "正在本机处理音频…"))
        y, stats = self.model.process(x, voice, self.cancel,
            lambda p: self.events.put(("status", f"正在处理测试录音… {p:.0%}")))
        if self.cancel.is_set():
            raise InterruptedError()
        folder = DATA / "tests" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        folder.mkdir(parents=True)
        sf.write(folder / "original.wav", native, native_rate, subtype="PCM_16")
        sf.write(folder / "model-input.wav", x, SR, subtype="PCM_16")
        sf.write(folder / "processed.wav", y, SR, subtype="PCM_16")
        stats["original_sample_rate"] = int(native_rate)
        write_json(folder / "metrics.json", stats)
        if self.voice_report:
            write_json(folder / "reference-quality.json", self.voice_report)
        self.events.put(("test_ready", (folder, stats)))

    def process_test_file(self, path):
        voice = self.voice.copy() if self.voice is not None else None
        def work():
            info = sf.info(path)
            if info.duration > 120:
                raise ValueError("原型目前支持最长 2 分钟的测试文件，请先截取需要比较的片段。")
            audio, rate = sf.read(path, dtype="float32", always_2d=True)
            self.process_test_audio(audio.mean(axis=1), rate, voice)
        self.launch_job("file-test", work)

    def import_test(self):
        path = filedialog.askopenfilename(title="导入需要处理的测试录音（最长 2 分钟）", filetypes=[("WAV 音频", "*.wav")])
        if path:
            self.process_test_file(path)

    def reprocess_test(self):
        self.process_test_file(self.last_test / "original.wav")

    def play(self, filename):
        path = self.last_test / filename
        @windows_audio_thread()
        def work():
            x, rate = sf.read(path, dtype="float32", always_2d=True)
            from .device_policy import choose
            _, outputs, defaults = devices(refresh=True)
            default = choose(outputs, defaults[1])
            self.events.put(("status", "正在通过实体播放设备播放原声…" if filename == "original.wav" else "正在通过实体播放设备播放处理后的声音…"))
            output_rate, channels = stream_config(default, "output")
            import soxr
            audio = x.mean(axis=1)
            if rate != output_rate:
                audio = soxr.resample(audio, rate, output_rate).astype(np.float32)
            audio = np.repeat(audio[:, None], channels, axis=1)
            with sd.OutputStream(device=default, samplerate=output_rate, channels=channels, dtype="float32") as stream:
                block = round(output_rate * .04)
                for i in range(0, len(audio), block):
                    if self.cancel.is_set():
                        raise InterruptedError()
                    stream.write(audio[i:i + block])
            self.events.put(("status", "回放完成。可以切换原声和处理后录音继续比较。"))
        self.launch_job("playback", work)

    def toggle_monitor(self):
        self.test_mode_var.set(not self.monitoring)
        self.set_monitor()

    def export_diagnostics(self):
        from .diagnostics import export_bundle
        try:
            path = export_bundle(DATA)
            self.status_var.set(f"诊断包已导出：{path.name}（不含录音）")
            os.startfile(str(path.parent))
        except Exception:
            logging.exception("diagnostics_export_failed")
            self.status_var.set("诊断导出未成功，错误已写入本地日志。")

    def report_callback_exception(self, exc, value, traceback):
        logging.error("gui_callback_failed", exc_info=(exc, value, traceback))
        if hasattr(self, "status_var"):
            self.status_var.set("操作遇到异常，原因已记录到日志。")

    def set_monitor(self):
        if not self.test_mode_var.get():
            if self.monitoring:
                self.stop()
            return
        if self.busy:
            self.test_mode_var.set(self.monitoring)
            return
        try:
            # A prior failed meeting session may have retained the silent
            # virtual route. Restore that session before local-only listening.
            if self.conference_route:
                self.route.restore()
                self.conference_route = False
            self.refresh_devices()
            from .device_policy import choose_monitor
            input_name = self.input_var.get()
            candidates = [(i, {"name": label}) for label, i in self.outputs.items()]
            output = choose_monitor(candidates, input_name)
            self.monitor_previous_output = self.output_var.get()
            self.output_var.set(next(label for label, i in self.outputs.items() if i == output))
            self.start_live(monitor=True)
        except Exception as exc:
            self.monitoring = False
            self.test_mode_var.set(False)
            self.status_var.set(str(exc))
            self.sync_controls()

    def set_monitor_audio(self, original):
        if self.monitoring and self.live:
            self.live.set_monitor_original(original)
            self.status_var.set("正在切换到原声试听…" if original else "正在切换到降噪后试听…")

    def start_live(self, monitor=False):
        try:
            index = self.selected_input()
            out = self.outputs[self.output_var.get()]
            ptt = self.ptt_var.get() and not monitor
            def transmit_allowed():
                if self.paused:
                    return False
                if not ptt:
                    return True
                # Checked by the audio callbacks, independently of Tk dialogs.
                keys = ctypes.windll.user32.GetAsyncKeyState
                return bool(keys(0xA3) & 0x8000) or (self.hold_pressed and bool(keys(0x01) & 0x8000))
            self.live = LiveAudio(self.model, self.voice.copy() if self.voice is not None else None, index, out,
                                  self.events, self.selected_settings(), transmit_allowed=transmit_allowed,
                                  monitor_only=monitor)
            self.monitoring = monitor
            self.test_mode_var.set(monitor)
            self.paused = self.hold_pressed = False
            if ptt:
                self.live.muted.set()
            self.busy, self.job = True, "live"
            self.status_var.set("正在准备实时处理…试听请佩戴耳机。")
            self.sync_controls()
            self.live.start()
        except Exception as exc:
            self.live = None
            self.monitoring = False
            self.test_mode_var.set(False)
            self.busy = False
            self.sync_controls()
            messagebox.showerror("无法开始", str(exc))

    def quick_start(self, attempt=0):
        if self.busy:
            return
        try:
            self.route.api.cable_capture()
        except RuntimeError:
            from .routing import install_driver
            try:
                install_driver()
                self.status_var.set("正在安装虚拟音频组件，Windows可能要求管理员授权或重启…")
                self.after(2000, lambda: self.wait_driver(25))
            except Exception as exc:
                messagebox.showerror("组件未就绪", str(exc))
            return
        self.refresh_devices()
        physical = self.input_var.get()
        if any(s in physical.lower() for s in ("cable", "steam", "virtual")):
            physical = next((k for k in self.inputs if not any(s in k.lower() for s in ("cable", "steam", "virtual", "stereo mix", "混音"))), "")
        output = next((k for k in self.outputs if "cable input" in k.lower()), None)
        if not physical or not output:
            if attempt >= 3:
                self.status_var.set("设备尚未激活，已保留原有音频配置。程序会在下次开启时重新检测。")
                return
            self.status_var.set("正在自动重新扫描音频设备…")
            self.after(500, lambda: self.quick_start(attempt + 1))
            return
        self.input_var.set(physical)
        self.output_var.set(output)
        self.conference_route = True
        self.start_live()
        if not self.live:
            self.conference_route = False
            self.sync_controls()

    def wait_driver(self, remaining, resume=True):
        if self.closed:
            return
        try:
            self.route.api.cable_capture()
        except RuntimeError:
            if remaining:
                self.after(2000, lambda: self.wait_driver(remaining - 1, resume))
            else:
                self.status_var.set("组件尚未激活；如Windows要求重启，重启后即可一键开启，无需手动选线。")
            return
        # Refresh PortAudio only while no streams are open after driver installation.
        if not self.busy:
            from .routing import finish_driver_install
            finish_driver_install()
            sd._terminate()
            sd._initialize()
            if resume:
                self.quick_start()
            else:
                self.refresh_devices()
                self.status_var.set("组件已就绪，原有系统设备已恢复。可以一键开启或独立测试。")

    def toggle_mute(self):
        if not self.live:
            return
        self.paused = not self.paused
        if not self.paused:
            self.mute_btn.configure(text="暂停传声")
            self.status_var.set("实时处理运行中。")
        else:
            self.live.muted.set()
            self.mute_btn.configure(text="恢复传声")
            self.status_var.set("已暂停传声：输出静音，模型继续保持运行。")
        self.sync_controls()

    def stop(self):
        logging.info("stop_requested monitor=%s conference_route=%s", self.monitoring, self.conference_route)
        self.cancel.set()
        if self.live:
            self.live.stop()
        if self.conference_route:
            try:
                self.route.restore()
                self.conference_route = False
            except Exception as exc:
                logging.exception("Route restore failed")
                self.status_var.set(f"设备恢复失败：{exc}，退出后恢复守护会再次处理。")
        self.status_var.set("正在停止并释放设备…")
        self.stop_btn.configure(state="disabled")

    def cable_help(self):
        from .routing import install_driver
        try:
            self.route.api.cable_capture()
            messagebox.showinfo("组件已就绪", "虚拟麦克风已就绪。点击一键开启后自动配置；停止时恢复原来的录音设备。\n\nVB-CABLE由VB-Audio提供，是Donationware，欢迎支持：https://vb-audio.com/Cable/\n飞书应使用系统默认麦克风；应用内固定了其他设备时，Windows默认路由可能不会生效。")
        except RuntimeError:
            try:
                install_driver()
                self.status_var.set("正在自动修复组件，必要时Windows会要求管理员授权或重启。")
                self.after(2000, lambda: self.wait_driver(25, False))
            except Exception as exc:
                messagebox.showerror("组件修复失败", str(exc))

    def open_tests(self):
        p = self.last_test or DATA / "tests"
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(str(p))

    def poll(self):
        if self.poll_timer is not None:
            try:
                self.after_cancel(self.poll_timer)
            except tk.TclError:
                pass
            self.poll_timer = None
        if self.live and self.busy:
            held = self.hold_pressed
            if self.ptt_var.get() and not self.monitoring:
                try:
                    held = held or bool(ctypes.windll.user32.GetAsyncKeyState(0xA3) & 0x8000)
                except Exception:
                    pass
            allowed = not self.paused and (self.monitoring or not self.ptt_var.get() or held)
            if allowed:
                self.live.muted.clear()
            else:
                self.live.muted.set()
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "tray_show":
                    self.show_window()
                elif kind == "tray_menu":
                    self.after_idle(self.tray.show_menu)
                elif kind == "model_ready":
                    if value[0] is not self.model or value[1] != self.model_generation:
                        continue
                    self.model_ready = True
                    if not self.busy:
                        if not self.model.requires_voice:
                            self.status_var.set("环境降噪已就绪，无需录入声音。此模式不区分你与旁人的身份。")
                        else:
                            self.status_var.set("请先录入本人声音。" if self.voice is None else "人声隔离已就绪，可以开始会议降噪或实时试听。")
                    self.sync_controls()
                elif kind == "model_error":
                    if value[0] is not self.model or value[1] != self.model_generation:
                        continue
                    self.status_var.set(value[2])
                    messagebox.showerror("模型未就绪", value[2])
                elif kind == "status":
                    self.status_var.set(value)
                elif kind == "error":
                    self.status_var.set(f"操作未完成：{value}")
                    messagebox.showerror(APP_NAME, value)
                elif kind == "voice_ready":
                    self.voice, self.voice_report = value
                    self.show_voice_report()
                elif kind == "record_tick":
                    label, sec, total, level = value
                    prompt = "请正常讲话"
                    if label == "对比测试":
                        prompt = "你单独讲话" if sec < 4 else "你先保持安静，让旁人讲话" if sec < 8 else "尝试你和旁人同时讲话"
                    self.status_var.set(f"{label}：{sec:.1f} / {total} 秒  ·  {prompt}")
                    self.show_levels(input_db=level)
                elif kind == "test_ready":
                    self.last_test, stats = value
                    self.test_var.set(f"测试已保存：{stats['audio_seconds']:.1f} 秒录音，{stats.get('backend', 'voxlock')} / {stats['precision']}，处理用时 {stats['processing_seconds']:.2f} 秒。")
                    self.stats_var.set(f"测试推理 P95：{stats['chunk_p95_ms']:.1f} ms / 40 ms 分块    RTF：{stats['rtf']:.2f}（越低越好；非端到端延迟）")
                    self.status_var.set("对比录音已就绪。回放会使用 Windows 默认播放设备。")
                elif kind == "job_done":
                    self.busy, self.job = False, None
                    self.show_levels()
                    self.sync_controls()
                elif kind == "live_started":
                    if self.conference_route:
                        try:
                            self.route.begin()
                        except Exception as exc:
                            self.live.stop()
                            self.status_var.set(f"会议路由失败，已静音：{exc}")
                            continue
                    logging.info("Live streams opened: %s", value)
                    self.status_var.set("实时处理运行中 · 仅输出处理结果。" + ("会议麦克风请选择 CABLE Output。" if "cable input" in self.output_var.get().lower() else "当前为本地耳机试听，尚未接入会议。"))
                    if self.monitoring:
                        self.status_var.set("耳机试听已开启，音频在本机实时处理。")
                        self.test_var.set("正在试听降噪后的声音。")
                    elif self.ptt_var.get():
                        self.status_var.set("按住传声保护已启用：按住右 Ctrl 或按钮时传声，松开即静音。")
                    elif self.conference_route:
                        self.status_var.set("会议降噪已开启：系统默认与通信麦克风已切换到降噪音频。飞书请使用“系统默认”。")
                elif kind == "monitor_mode":
                    logging.info("%s: %s", kind, value)
                    self.status_var.set(value)
                    self.test_var.set("正在试听原声。" if "原声" in value
                                      else "正在试听降噪后的声音。")
                elif kind in ("capture_recovering", "capture_recovered"):
                    logging.info("%s: %s", kind, value)
                    self.status_var.set(value)
                    if kind == "capture_recovering":
                        self.show_levels()
                elif kind == "meters":
                    self.show_levels(value["input"], value["output"])
                    self.stats_var.set(f"推理 P95：{value['p95']:.1f} / {value['chunk_ms']} ms    输出缓冲：{value['buffer_ms']:.0f} ms    "
                        f"欠载：{value['underruns']}    时钟校正：{value['clock_ppm']:+.0f} ppm（非端到端延迟）")
                elif kind == "live_stopped":
                    if self.conference_route and not value:
                        try:
                            self.route.restore()
                            self.conference_route = False
                        except Exception:
                            logging.exception("Route restore failed")
                    self.live = None
                    if self.monitoring:
                        self.monitoring = False
                        self.test_mode_var.set(False)
                        self.test_var.set("实时试听已停止，未保存录音。")
                        if self.monitor_previous_output in self.outputs:
                            self.output_var.set(self.monitor_previous_output)
                            self.update_route()
                        self.monitor_previous_output = None
                    self.busy, self.job = False, None
                    self.show_levels()
                    self.mute_btn.configure(text="暂停传声")
                    self.status_var.set(f"已停止并静音：{value}" if value else "实时处理已停止，设备已释放。")
                    if value:
                        logging.error("Live stopped: %s", value)
                    self.sync_controls()
        except queue.Empty:
            pass
        if not self.closed:
            self.poll_timer = self.after(25, self.poll)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop_desktop_timers()
        logging.info("app_exit_requested")
        if self.poll_timer is not None:
            self.after_cancel(self.poll_timer)
            self.poll_timer = None
        self.cancel.set()
        if self.live:
            self.live.stop()
        self.status_var.set("正在停止处理并释放音频设备…")
        self._finish_close()

    def _finish_close(self):
        workers = self.model_threads + [getattr(self, "worker", None), self.live.thread if self.live else None]
        if any(t and t.is_alive() for t in workers):
            self.after(50, self._finish_close)
        else:
            if self.conference_route:
                try:
                    self.route.restore()
                except Exception:
                    logging.exception("Close route restore failed")
            try:
                if self.tray:
                    self.tray.close()
            finally:
                self.destroy()


def main():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    enable_per_monitor_dpi()
    from .tray import TrayIcon, desktop_instance
    with desktop_instance() as primary:
        if primary:
            app = App()
            try:
                app.tray = TrayIcon(app)
                app.sync_controls()
            except Exception:
                logging.exception("Could not start ClearMic tray icon")
            app.mainloop()


if __name__ == "__main__":
    main()
