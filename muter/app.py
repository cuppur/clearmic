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
from tkinter import filedialog, messagebox, ttk
import webbrowser

import numpy as np
import sounddevice as sd
import soundfile as sf

from .audio import DATA, PROFILE, ROOT, SR, LiveAudio, Model, dbfs, devices, load_voice, record, stream_config, write_json, windows_audio_thread
from . import __version__
from .realtime import LiveSettings

BG, CARD, TEXT, DIM = "#EFF4F8", "#FFFFFF", "#14263B", "#506781"
HERO, ACCENT, EDGE, FIELD = "#14283F", "#087560", "#D7E2ED", "#E8F0F7"
AMBIENT, ISOLATION, STRONG, FULL_PRECISION = "环境降噪", "人声隔离", "加强人声隔离", "人声隔离 · FP32"
MODE_DESCRIPTIONS = {
    AMBIENT: "压低风扇、键盘等环境声，无需声音样本。",
    ISOLATION: "用本人声音样本区分说话人，适合日常会议。",
    STRONG: "加强对旁人和电视人声的抑制，增加处理延迟。",
    FULL_PRECISION: "以全精度运行人声隔离，可用于听感对比。",
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Muter · 会议语音净化 v{__version__}")
        self.geometry("900x645")
        self.minsize(760, 620)
        self.configure(bg=BG)
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
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.quality_var = tk.StringVar(value=AMBIENT)
        self.mode_hint = tk.StringVar(value=MODE_DESCRIPTIONS[AMBIENT])
        self.voice_summary = tk.StringVar()
        self.mic_hint = tk.StringVar(value="等待识别麦克风")
        self.latency_var = tk.StringVar(value="均衡 · 80 ms 缓冲")
        self.status_var = tk.StringVar(value="正在载入本地人声模型…")
        self.voice_var = tk.StringVar(value="环境降噪和实时试听可直接使用，无需录入。尝试人声隔离时，再单独录入本人声音。")
        self.route_var = tk.StringVar()
        self.stats_var = tk.StringVar(value="推理 P95：—    输出缓冲：—    欠载：—")
        self.test_var = tk.StringVar(value="戴上耳机，边说边听并比较降噪效果。")
        self._style()
        self._build()
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

    def _style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=TEXT,
                        arrowcolor=ACCENT, bordercolor=EDGE, padding=8,
                        font=("Microsoft YaHei UI", 10))
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)],
                  selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)])
        style.configure("Meter.Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD,
                        bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)
        self.option_add("*TCombobox*Listbox.font", ("Microsoft YaHei UI", 10))

    def label(self, parent, text="", size=10, color=TEXT, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Microsoft YaHei UI", size, "bold" if bold else "normal"),
                        anchor="w", justify="left", **kw)

    def button(self, parent, text, command, primary=False, hero=False):
        background = "#8EF3DB" if hero else ACCENT if primary else FIELD
        foreground = HERO if hero else "#FFFFFF" if primary else TEXT
        return tk.Button(parent, text=text, command=command, bg=background, fg=foreground,
                         activebackground="#B5FAE9" if hero else "#005647" if primary else "#DAE6F2",
                         activeforeground=foreground, disabledforeground="#687C91",
                         relief="flat", bd=0, padx=15, pady=8, cursor="hand2", takefocus=True,
                         font=("Microsoft YaHei UI", 10, "bold"))

    def section(self, parent, title):
        card = tk.Frame(parent, bg=CARD, padx=19, pady=13, highlightthickness=1, highlightbackground=EDGE)
        self.label(card, title, 12, bold=True).pack(anchor="w", pady=(0, 8))
        return card

    def _build(self):
        outer = tk.Frame(self, bg=BG, padx=23, pady=16)
        outer.pack(fill="both", expand=True)
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x", pady=(0, 13))
        icon_path = Path(__file__).parent / "assets" / "muter.png"
        if icon_path.is_file():
            self.app_icon = tk.PhotoImage(file=icon_path)
            self.iconphoto(True, self.app_icon)
            self.header_icon = self.app_icon.subsample(2)
            self.label(head, image=self.header_icon).pack(side="left", padx=(0, 10))
        brand = tk.Frame(head, bg=BG)
        brand.pack(side="left")
        self.label(brand, "Muter", 23, bold=True).pack(anchor="w")
        self.label(brand, "会议语音净化", 9, DIM).pack(anchor="w")
        self.settings_btn = self.button(head, "设置", self.toggle_settings)
        self.settings_btn.pack(side="right")
        self.button(head, "导出诊断", self.export_diagnostics).pack(side="right", padx=8)

        footer = tk.Frame(outer, bg=BG)
        footer.pack(side="bottom", fill="x", pady=(10, 0))
        self.label(footer, "本机处理  ·  实时音频不保存  ·  关闭窗口后在托盘运行", 9, DIM).pack(side="left")
        self.label(footer, f"WINDOWS   /   v{__version__}", 9, DIM).pack(side="right")

        scroll = tk.Frame(outer, bg=BG)
        scroll.pack(fill="both", expand=True)
        canvas = tk.Canvas(scroll, bg=BG, highlightthickness=0, bd=0)
        self.body_canvas = canvas
        bar = ttk.Scrollbar(scroll, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=BG)
        body_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(body_id, width=e.width))
        self.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-int(e.delta / 120), "units")
                  if canvas.bbox("all") and canvas.bbox("all")[3] > canvas.winfo_height() else None)

        hero = tk.Frame(body, bg=HERO, padx=22, pady=16)
        hero.pack(fill="x", pady=(0, 10))
        actions = tk.Frame(hero, bg=HERO)
        actions.pack(side="right", padx=(12, 0))
        self.quick_btn = self.button(actions, "开启会议降噪", self.toggle_conference, hero=True)
        self.quick_btn.grid(row=0, column=0, sticky="ew")
        self.mute_btn = self.button(actions, "暂停传声", self.toggle_mute)
        self.mute_btn.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.state_badge = self.label(hero, "●  正在准备", 9, "#8EF3DB", True)
        self.state_badge.pack(anchor="w")
        self.label(hero, "会议降噪", 21, "#FFFFFF", True).pack(anchor="w", pady=(2, 3))
        status = self.label(hero, textvariable=self.status_var, color="#D1E0F0", wraplength=490)
        status.pack(anchor="w", fill="x")
        status.bind("<Configure>", lambda e: status.configure(wraplength=max(200, e.width - 4)))

        mode = self.section(body, "处理模式")
        mode.pack(fill="x", pady=(0, 10))
        mode_row = tk.Frame(mode, bg=CARD)
        mode_row.pack(fill="x")
        self.quality_combo = ttk.Combobox(mode_row, textvariable=self.quality_var, state="readonly",
                                        width=23, values=tuple(MODE_DESCRIPTIONS))
        self.quality_combo.pack(side="left")
        self.quality_combo.bind("<<ComboboxSelected>>", self.change_model)
        self.label(mode_row, textvariable=self.mode_hint, color=DIM, wraplength=370).pack(
            side="left", padx=(12, 0), fill="x", expand=True)
        sample_row = tk.Frame(mode, bg=CARD)
        sample_row.pack(fill="x", pady=(8, 0))
        self.voice_btn = self.button(sample_row, "录入本人声音", self.enroll)
        self.voice_btn.pack(side="right")
        self.label(sample_row, textvariable=self.voice_summary, size=9, color=DIM).pack(side="left")

        lower = tk.Frame(body, bg=BG)
        lower.pack(fill="x", pady=(0, 10))
        lower.columnconfigure((0, 1), weight=1, uniform="cards")
        monitor = self.section(lower, "耳机实时试听")
        monitor.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.label(monitor, textvariable=self.test_var, color=DIM, wraplength=280).pack(anchor="w")
        monitor_row = tk.Frame(monitor, bg=CARD)
        monitor_row.pack(fill="x", pady=(8, 0))
        self.test_btn = self.button(monitor_row, "开启实时试听", self.toggle_monitor, True)
        self.test_btn.pack(side="left")
        self.ab_row = tk.Frame(monitor_row, bg=CARD)
        self.raw_btn = self.button(self.ab_row, "原声", lambda: self.set_monitor_audio(True))
        self.raw_btn.pack(side="left")
        self.clean_btn = self.button(self.ab_row, "降噪后", lambda: self.set_monitor_audio(False))
        self.clean_btn.pack(side="left", padx=5)

        meters = self.section(lower, "音频电平")
        meters.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        for name, attr in (("麦克风", "in_meter"), ("处理输出", "out_meter")):
            row = tk.Frame(meters, bg=CARD)
            row.pack(fill="x", pady=(2, 7))
            self.label(row, name, 9, DIM, width=8).pack(side="left")
            meter = ttk.Progressbar(row, style="Meter.Horizontal.TProgressbar", maximum=60)
            meter.pack(side="left", fill="x", expand=True, padx=(4, 0))
            setattr(self, attr, meter)
        self.label(meters, textvariable=self.mic_hint, size=9, color=DIM, wraplength=280).pack(anchor="w")

        self.settings_card = self.section(body, "设备与高级设置")
        config = tk.Frame(self.settings_card, bg=CARD)
        config.pack(fill="x")
        config.columnconfigure((0, 1), weight=1, uniform="devices")
        self.label(config, "输入麦克风", color=DIM).grid(row=0, column=0, sticky="w")
        self.label(config, "手动处理输出", color=DIM).grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.input_combo = ttk.Combobox(config, textvariable=self.input_var, state="readonly", width=20)
        self.input_combo.grid(row=1, column=0, sticky="ew", pady=(3, 4))
        self.output_combo = ttk.Combobox(config, textvariable=self.output_var, state="readonly", width=20)
        self.output_combo.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(3, 4))
        self.output_combo.bind("<<ComboboxSelected>>", lambda _: self.update_route())
        self.label(self.settings_card, textvariable=self.route_var, size=9, color=DIM, wraplength=650).pack(anchor="w")
        row = tk.Frame(self.settings_card, bg=CARD)
        row.pack(fill="x", pady=(7, 0))
        self.refresh_btn = self.button(row, "刷新设备", self.refresh_devices)
        self.refresh_btn.pack(side="left")
        self.cable_btn = self.button(row, "虚拟麦克风", self.cable_help)
        self.cable_btn.pack(side="left", padx=5)
        self.latency_combo = ttk.Combobox(row, textvariable=self.latency_var, state="readonly", width=23,
            values=("低延迟 · 60 ms 缓冲", "均衡 · 80 ms 缓冲", "稳健 · 160 ms 缓冲"))
        self.latency_combo.pack(side="right")
        self.ptt_check = tk.Checkbutton(self.settings_card, text="按住右 Ctrl 才传声", variable=self.ptt_var,
            bg=CARD, fg=TEXT, selectcolor=FIELD, activebackground=CARD, activeforeground=TEXT,
            font=("Microsoft YaHei UI", 10), anchor="w")
        self.ptt_check.pack(anchor="w", pady=(6, 0))
        row = tk.Frame(self.settings_card, bg=CARD)
        row.pack(fill="x", pady=(6, 0))
        self.start_btn = self.button(row, "手动开始", self.start_live)
        self.start_btn.pack(side="left")
        self.stop_btn = self.button(row, "停止", self.stop)
        self.stop_btn.pack(side="left", padx=5)
        self.hold_btn = self.button(row, "按住传声", lambda: None)
        self.hold_btn.pack(side="left")
        self.hold_btn.bind("<ButtonPress-1>", lambda _: setattr(self, "hold_pressed", True))
        self.hold_btn.bind("<ButtonRelease-1>", lambda _: setattr(self, "hold_pressed", False))
        self.hold_btn.bind("<Leave>", lambda _: setattr(self, "hold_pressed", False))
        row = tk.Frame(self.settings_card, bg=CARD)
        row.pack(fill="x", pady=(6, 0))
        self.import_btn = self.button(row, "导入声音 WAV", self.import_voice)
        self.import_btn.pack(side="left")
        self.delete_btn = self.button(row, "清除声音样本", self.delete_voice)
        self.delete_btn.pack(side="left", padx=5)
        self.voice_info_btn = self.button(row, "选段详情", self.voice_info)
        self.voice_info_btn.pack(side="left")
        self.label(self.settings_card, textvariable=self.voice_var, size=9, color=DIM, wraplength=650).pack(anchor="w", pady=(7, 0))
        self.label(self.settings_card, textvariable=self.stats_var, size=9, color=DIM, wraplength=650).pack(anchor="w")
        self.input_var.trace_add("write", self.update_mic_hint)
        self.sync_controls()

    def update_mic_hint(self, *_):
        name = self.input_var.get().split("  ·  ", 1)[-1]
        self.mic_hint.set(name or "等待识别麦克风")

    def toggle_settings(self):
        if self.settings_card.winfo_manager():
            self.settings_card.pack_forget()
            self.settings_btn.configure(text="设置")
            self.body_canvas.yview_moveto(0)
        else:
            self.settings_card.pack(fill="x")
            self.settings_btn.configure(text="收起设置")
            self.after_idle(lambda: self.body_canvas.yview_moveto(1))

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
        self.voice_var.set(f"已选取 {r['selected_start_seconds']:.1f}–{r['selected_start_seconds'] + 3:.1f} 秒作为参考，"
                           f"有效声音 {r['active_audio_seconds']:.1f} 秒。" + (warning or "请确认录音中只有你讲话。"))

    def voice_info(self):
        r = self.voice_report
        if not r:
            return
        messagebox.showinfo("参考语音质量", f"选段：第 {r['selected_start_seconds']:.2f} 秒开始的连续 3 秒\n"
            f"有效声音：{r['active_audio_seconds']:.2f} 秒\n音量：{r['selected_rms_dbfs']:.1f} dBFS\n"
            f"爆音比例：{r['selected_clipping_fraction']:.2%}\n\n" + "\n".join(r["warnings"] or ["未发现明显音量或爆音问题。"])
            + "\n\n这些检查只能判断信号质量，不能确认身份或判断是否混入别人。请在安静处独自录入。")

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
        self.voice_summary.set("已准备本人声音样本" if has_voice else
                               "人声隔离需要一次本人声音录入" if self.model.requires_voice else
                               "可选 · 录入本人声音以启用人声隔离")
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
        for b in (self.voice_btn, self.import_btn, self.refresh_btn):
            b.configure(state="normal" if idle else "disabled")
        self.delete_btn.configure(state="normal" if idle and has_voice else "disabled")
        self.voice_info_btn.configure(state="normal" if idle and has_voice else "disabled")
        self.start_btn.configure(state="normal" if idle and can_process and self.model_ready else "disabled")
        monitor_available = self.monitoring or (idle and can_process and self.model_ready)
        self.test_btn.configure(state="normal" if monitor_available else "disabled",
                                text="停止实时试听" if self.monitoring else "开启实时试听")
        if self.monitoring:
            self.ab_row.pack(side="left", padx=(5, 0))
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
        self.mute_btn.configure(state="normal" if self.live and self.busy else "disabled")
        if self.live and self.busy:
            self.mute_btn.grid()
        else:
            self.mute_btn.grid_remove()

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
                    messagebox.showerror("Muter", value)
                elif kind == "voice_ready":
                    self.voice, self.voice_report = value
                    self.show_voice_report()
                elif kind == "record_tick":
                    label, sec, total, level = value
                    prompt = "请正常讲话"
                    if label == "对比测试":
                        prompt = "你单独讲话" if sec < 4 else "你先保持安静，让旁人讲话" if sec < 8 else "尝试你和旁人同时讲话"
                    self.status_var.set(f"{label}：{sec:.1f} / {total} 秒  ·  {prompt}")
                    self.in_meter["value"] = max(0, 60 + level)
                elif kind == "test_ready":
                    self.last_test, stats = value
                    self.test_var.set(f"测试已保存：{stats['audio_seconds']:.1f} 秒录音，{stats.get('backend', 'voxlock')} / {stats['precision']}，处理用时 {stats['processing_seconds']:.2f} 秒。")
                    self.stats_var.set(f"测试推理 P95：{stats['chunk_p95_ms']:.1f} ms / 40 ms 分块    RTF：{stats['rtf']:.2f}（越低越好；非端到端延迟）")
                    self.status_var.set("对比录音已就绪。回放会使用 Windows 默认播放设备。")
                elif kind == "job_done":
                    self.busy, self.job = False, None
                    self.in_meter["value"] = 0
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
                        self.in_meter["value"] = self.out_meter["value"] = 0
                elif kind == "meters":
                    self.in_meter["value"] = max(0, 60 + value["input"])
                    self.out_meter["value"] = max(0, 60 + value["output"])
                    self.stats_var.set(f"推理 P95：{value['p95']:.1f} / {value['chunk_ms']} ms    缓冲：{value['buffer_ms']:.0f} ms    "
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
                    self.in_meter["value"] = self.out_meter["value"] = 0
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
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    from .tray import TrayIcon, desktop_instance
    with desktop_instance() as primary:
        if primary:
            app = App()
            try:
                app.tray = TrayIcon(app)
                app.sync_controls()
            except Exception:
                logging.exception("Could not start Muter tray icon")
            app.mainloop()


if __name__ == "__main__":
    main()
