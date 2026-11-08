"""ClearMic's standard desktop layout and presentation state."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import json
import logging
from pathlib import Path
import re
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

from . import APP_NAME
from .window_dpi import DEFAULT_SIZE, fit_window, frame_size, top_level_handle, window_display

# The title and footer mirror the supplied desktop design.
DISPLAY_VERSION = "0.4.1"
CLOSING_MARKS = "，。！？；：、）》〉】」』〕）％…,.!?;:%)]}"
OPENING_MARKS = "（《〈【「『〔([{"
COLOR_OPTIONS = {
    "background": ("bg", "card", "hero", "field", "edge", "selected", "accent"),
    "activebackground": ("bg", "card", "hero", "field", "edge", "selected"),
    "highlightbackground": ("bg", "card", "hero", "field", "edge", "selected"),
    "selectcolor": ("bg", "card", "hero", "field", "edge", "selected"),
    "foreground": ("text", "dim", "accent"),
    "activeforeground": ("text", "dim", "accent"),
    "disabledforeground": ("text", "dim", "accent"),
    "highlightcolor": ("text", "dim", "accent"),
}


@dataclass(frozen=True)
class Palette:
    bg: str
    card: str
    text: str
    dim: str
    hero: str
    accent: str
    edge: str
    field: str
    selected: str


PALETTES = {
    "light": Palette("#ECF2F3", "#FFFFFF", "#142D37", "#526F7B", "#102A33",
                     "#116A59", "#CFE1E4", "#E5EFF0", "#CFEAE5"),
    "dark": Palette("#0A151B", "#12232D", "#E8F2F4", "#A7BCC7", "#0D2932",
                    "#7BDDC6", "#304854", "#203640", "#1C493E"),
}


def wrap_text(text, font, width):
    """Break Chinese text at characters, retaining punctuation with its word."""
    lines = []
    for paragraph in text.split("\n"):
        paragraph = re.sub(rf" +(?=[{re.escape(CLOSING_MARKS)}])", "", paragraph)
        tokens = []
        for token in re.findall(r"[A-Za-z0-9]+(?:[./_-][A-Za-z0-9]+)*| +|.", paragraph):
            if tokens and (token[0] in CLOSING_MARKS or tokens[-1][-1] in OPENING_MARKS):
                tokens[-1] += token
            else:
                tokens.append(token)
        rows = [[]]
        for token in tokens:
            row = rows[-1]
            if row and not token.isspace() and font.measure("".join(row) + token) > width:
                boundary = next((i + 1 for i in reversed(range(len(row) - 1))
                                 if row[i].rstrip().endswith(("，", "；", "。", "·"))
                                 and font.measure("".join(row[:i + 1])) >= width / 2
                                 and font.measure("".join(row[i + 1:]) + token) <= width), len(row))
                rows.append(row[boundary:])
                del row[boundary:]
                row = rows[-1]
            if row or not token.isspace():
                row.append(token)
        if len(rows) > 1 and len("".join(rows[-1]).strip()) <= 4:
            previous, last = rows[-2:]
            # Prefer a clause boundary over an isolated word at the end.
            split = next((i + 1 for i in reversed(range(len(previous) - 1))
                          if previous[i].rstrip().endswith(("，", "、", "；", "。"))), None)
            if split and font.measure("".join(previous[split:] + last).strip()) <= width:
                last[:0] = previous[split:]
                del previous[split:]
            else:
                while (len(previous) > 1 and len("".join(last).strip()) < 5
                       and font.measure("".join(previous[-1:] + last).strip()) <= width):
                    last.insert(0, previous.pop())
        lines.extend("".join(row).strip() for row in rows)
    return lines


class FlowLabel(tk.Label):
    """Width-aware text with a bounded footprint and access to full details."""

    def __init__(self, parent, *, source, font, lines, **kwargs):
        super().__init__(parent, font=font, width=1, height=lines, padx=0, pady=0, bd=0,
                         anchor="nw", justify="left", **kwargs)
        self.source = source
        self.font = font
        self.lines = lines
        self.pending = None
        self.overflow = False
        self.trace = source.trace_add("write", self.schedule)
        self.bind("<Configure>", self.schedule)
        self.bind("<Button-1>", self.details)
        self.bind("<Return>", self.details)

    def schedule(self, *_):
        if self.pending is None:
            self.pending = self.after_idle(self.reflow)

    def reflow(self):
        self.pending = None
        width = self.winfo_width()
        if width <= 1:
            return
        text = self.source.get()
        lines = wrap_text(text, self.font, width)
        self.overflow = len(lines) > self.lines or any(self.font.measure(line) > width for line in lines)
        visible = "\n".join(lines)
        if self.overflow:
            first_sentence = text.partition("。")[0]
            brief = first_sentence + "…"
            visible = brief if font_fits(self.font, brief, width) else "查看完整信息…"
        self.configure(text=visible, height=1 if self.overflow else len(lines),
                       cursor="hand2" if self.overflow else "", takefocus=self.overflow)

    def details(self, _=None):
        if self.overflow:
            messagebox.showinfo("详细信息", self.source.get(), parent=self.winfo_toplevel())

    def destroy(self):
        self.source.trace_remove("write", self.trace)
        if self.pending is not None:
            self.after_cancel(self.pending)
        super().destroy()


def font_fits(font, text, width):
    return "\n" not in text and font.measure(text) <= width


class LevelMeter(tk.Canvas):
    def __init__(self, parent, palette, height):
        super().__init__(parent, bg=palette.field, height=height, bd=0, highlightthickness=0)
        self.level = 0.0
        self.bar = self.create_rectangle(0, 0, 0, 0, fill=palette.accent, outline="")
        self.bind("<Configure>", lambda _: self.set_level(self.level))

    def set_level(self, level):
        self.level = level
        self.coords(self.bar, 0, 0, self.winfo_width() * level / 60, self.winfo_height())


class DesktopUI:
    """Tk controls. Audio and device state remain owned by App."""

    def initialize_desktop(self, appearance_path, modes):
        self.appearance_path = appearance_path
        self.theme = self.load_theme()
        self.palette = PALETTES[self.theme]
        self.title(f"{APP_NAME} · 会议语音净化 v{DISPLAY_VERSION}")
        self.configure(bg=self.palette.bg)
        self.display = window_display()
        self.ui_scale, minimum, outer_size = fit_window(*self.display)
        self.layout_options = []
        self.pixel_options = []
        self.flow_labels = []
        self.button_specs = []
        self.button_faces = {}
        self.panel_surfaces = []
        self.diagnostics_window = None
        self.fonts = {
            key: tkfont.Font(root=self, family="Microsoft YaHei UI", size=-self.px(size), weight=weight)
            for key, size, weight in (
                ("small", 11, "normal"), ("body", 12, "normal"), ("action", 12, "bold"),
                ("section", 14, "bold"), ("brand", 23, "bold"), ("hero", 24, "bold"),
            )
        }
        ttk.Style(self).theme_use("clam")
        self.build_desktop(modes)
        self.style_desktop()
        dpi, (left, top, right, bottom) = self.display
        width, height = self.client_size(outer_size, dpi)
        self.minsize(*self.client_size(minimum, dpi))
        self.last_outer_size = outer_size
        self.logical_size = DEFAULT_SIZE
        x = left + (right - left - outer_size[0]) // 2
        y = top + (bottom - top - outer_size[1]) // 2
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.display_timer = self.after(160, self.check_display)

    def check_display(self):
        import win32con
        import win32gui

        if self.closed:
            return
        if self.state() in ("normal", "zoomed") and self.winfo_ismapped():
            hwnd = top_level_handle(self)
            display = window_display(hwnd)
            x, y, right, bottom = win32gui.GetWindowRect(hwnd)
            outer_size = (right - x, bottom - y)
            if display != self.display:
                old_dpi, old_work = self.display
                dpi, work = display
                self.display = display
                same_capacity = (old_work[2] - old_work[0], old_work[3] - old_work[1]) == (
                    work[2] - work[0], work[3] - work[1])
                if dpi != old_dpi or not same_capacity:
                    scale, minimum, fitted = fit_window(dpi, work, self.logical_size)
                    if scale != self.ui_scale:
                        self.ui_scale = scale
                        self.scale_desktop()
                    self.minsize(*self.client_size(minimum, dpi))
                    if self.state() == "normal":
                        width, height = self.client_size(fitted, dpi)
                        self.geometry(f"{width}x{height}")
                        left, top, right, bottom = work
                        x = min(max(x, left), right - fitted[0])
                        y = min(max(y, top), bottom - fitted[1])
                        win32gui.SetWindowPos(hwnd, 0, x, y, 0, 0,
                                              win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
                    self.last_outer_size = fitted
            elif self.state() == "normal" and outer_size != self.last_outer_size:
                self.last_outer_size = outer_size
                self.logical_size = tuple(round(length / self.ui_scale) for length in outer_size)
        self.display_timer = self.after(160, self.check_display)

    def stop_desktop_timers(self):
        self.after_cancel(self.display_timer)
        if getattr(self, "meter_animation", None) is not None:
            self.after_cancel(self.meter_animation)
            self.meter_animation = None

    @staticmethod
    def client_size(outer, dpi):
        return tuple(max(1, length - border) for length, border in zip(outer, frame_size(dpi)))

    def px(self, value):
        return round(value * self.ui_scale)

    def scaled_options(self, options):
        return {key: tuple(self.px(v) for v in value) if isinstance(value, tuple) else self.px(value)
                for key, value in options.items()}

    def place_widget(self, widget, manager="pack", *, spacing=None, **options):
        place = getattr(widget, manager)
        if spacing:
            self.layout_options.append((widget, manager, spacing))
            options.update(self.scaled_options(spacing))
        place(**options)
        return widget

    def set_pixels(self, widget, **options):
        self.pixel_options.append((widget, options))
        widget.configure(**self.scaled_options(options))

    def scale_desktop(self):
        # Update existing widgets to keep keyboard focus and audio bindings intact.
        for key, size in (("small", 11), ("body", 12), ("action", 12), ("section", 14), ("brand", 23), ("hero", 24)):
            self.fonts[key].configure(size=-self.px(size))
        for widget, options in self.pixel_options:
            if widget.winfo_exists():
                widget.configure(**self.scaled_options(options))
        for widget, manager, options in self.layout_options:
            if widget.winfo_exists() and widget.winfo_manager() == manager:
                getattr(widget, manager + "_configure")(**self.scaled_options(options))
        if self.diagnostics_window is not None:
            self.diagnostics_window.minsize(self.px(320), self.px(120))
        self.style_desktop()
        self.paint_desktop()
        for label in self.flow_labels:
            if label.winfo_exists():
                label.schedule()

    def load_theme(self):
        try:
            config = json.loads(self.appearance_path.read_text(encoding="utf-8"))
            if isinstance(config, dict) and config.get("theme") == "dark":
                return "dark"
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logging.warning("Could not read appearance settings", exc_info=True)
        return "light"

    def toggle_theme(self):
        from .routing import atomic_json

        previous = self.palette
        self.theme = "dark" if self.theme == "light" else "light"
        self.palette = PALETTES[self.theme]
        colors = {option: {getattr(previous, role).lower(): getattr(self.palette, role) for role in roles}
                  for option, roles in COLOR_OPTIONS.items()}
        widgets = [self]
        while widgets:
            widget = widgets.pop()
            widgets.extend(widget.winfo_children())
            changes = {}
            for option in colors.keys() & set(widget.keys()):
                new_color = colors[option].get(str(widget.cget(option)).lower())
                if new_color:
                    changes[option] = new_color
            if changes:
                widget.configure(**changes)
        self.style_desktop()
        self.paint_desktop()
        if self.diagnostics_window is not None:
            self.theme_btn.configure(text=self.theme_caption())
        try:
            atomic_json(self.appearance_path, {"theme": self.theme})
        except OSError:
            logging.warning("Could not save appearance settings", exc_info=True)

    def theme_caption(self):
        return "切换深色主题" if self.theme == "light" else "切换浅色主题"

    def style_desktop(self):
        p = self.palette
        style = ttk.Style(self)
        style.configure("TCombobox", fieldbackground=p.field, background=p.field, foreground=p.text,
                        arrowcolor=p.accent, bordercolor=p.edge, lightcolor=p.edge, darkcolor=p.edge,
                        padding=self.px(5), arrowsize=self.px(13), font=self.fonts["body"])
        style.map("TCombobox", fieldbackground=[("readonly", p.field)],
                  selectbackground=[("readonly", p.field)], selectforeground=[("readonly", p.text)],
                  background=[("active", p.selected), ("disabled", p.bg)], foreground=[("disabled", p.dim)])
        dropdown = {"background": p.card, "foreground": p.text, "selectBackground": p.selected,
                    "selectForeground": p.text, "font": self.fonts["body"]}
        for option, value in dropdown.items():
            self.option_add(f"*TCombobox*Listbox.{option}", value)
        for combo in (self.input_combo, self.output_combo, self.quality_combo, self.latency_combo):
            listbox = f"{combo}.popdown.f.l"
            if self.tk.call("winfo", "exists", listbox):
                for option, value in dropdown.items():
                    self.tk.call(listbox, "configure", f"-{option.lower()}", value)

    def paint_desktop(self):
        for surface, hero in self.panel_surfaces:
            self.paint_panel(surface, hero)
        self.paint_buttons()
        self.paint_logo()
        for meter in (self.in_meter, self.out_meter):
            meter.itemconfigure(meter.bar, fill=self.palette.accent)

    def label(self, parent, text="", font="body", *, color=None, **options):
        return tk.Label(parent, text=text, font=self.fonts[font], fg=color or self.palette.text,
                        bg=parent.cget("bg"), anchor="w", padx=0, pady=0, bd=0, **options)

    def flow(self, parent, source, lines, *, font="body", color=None):
        label = FlowLabel(parent, source=source, font=self.fonts[font], lines=lines,
                          fg=color or self.palette.dim, bg=parent.cget("bg"))
        self.flow_labels.append(label)
        return label

    def button(self, parent, caption, command, variant="normal", *, width=None):
        surface = parent.cget("bg")
        button = tk.Button(parent, text=caption, command=command, compound="center",
                           bg=surface, activebackground=surface, highlightbackground=surface,
                           highlightcolor=self.palette.accent, relief="flat", bd=0, padx=0, pady=0,
                           cursor="hand2", takefocus=True, font=self.fonts["action"])
        self.button_specs.append((button, variant, caption, width))
        self.paint_button(button, variant, caption, width)
        return button

    def paint_button(self, button, variant, caption, width):
        p = self.palette
        bg, fg = p.field, p.text
        if variant == "hero":
            bg, fg = "#82E3C9", "#103932"
        elif variant == "stop":
            bg, fg = "#FFD8CB", "#6B291A"
        elif variant == "primary":
            bg, fg = "#116A59", "#FFFFFF"
        elif variant == "selected":
            bg, fg = p.selected, p.accent
        button.configure(fg=fg, activeforeground=fg,
                         disabledforeground="#BAD0D8" if variant == "primary" else p.dim)
        font = self.fonts["action"]
        width = max(self.px(width or 0), font.measure(caption) + self.px(16))
        height = font.metrics("linespace") + self.px(10 if variant in ("hero", "stop") else 7)
        key = (width, height, bg)
        if key not in self.button_faces:
            face = tk.PhotoImage(master=self, width=width, height=height)
            radius = min(self.px(8), height // 2)
            for y in range(height):
                dy = max(radius - y, y - (height - radius - 1), 0)
                inset = radius - int((radius ** 2 - dy ** 2) ** .5)
                face.put(bg, to=(inset, y, width - inset, y + 1))
            self.button_faces[key] = face
        button.configure(image=self.button_faces[key])

    def paint_buttons(self):
        for button, variant, caption, width in self.button_specs:
            if button.winfo_exists():
                if button is self.quick_btn and self.quick_btn.cget("text") != "开启会议降噪":
                    variant = "stop"
                for key, tab in self.tab_buttons.items():
                    if button is tab and key == self.selected_tab:
                        variant = "selected"
                self.paint_button(button, variant, caption, width)

    def panel(self, parent, hero=False):
        p = self.palette
        outer = tk.Frame(parent, bg=p.bg)
        surface = tk.Canvas(outer, bg=p.bg, bd=0, highlightthickness=0)
        surface.place(relwidth=1, relheight=1)
        self.panel_surfaces.append((surface, hero))
        surface.bind("<Configure>", lambda _: self.paint_panel(surface, hero))
        body = tk.Frame(outer, bg=p.hero if hero else p.card)
        self.place_widget(body, fill="both", expand=True, spacing={"padx": 12, "pady": 7})
        return outer, body

    def paint_panel(self, surface, hero):
        p = self.palette
        width, height = surface.winfo_width() - 1, surface.winfo_height() - 1
        if width < 2 or height < 2:
            return
        r = min(self.px(13), width // 2, height // 2)
        surface.delete("surface")
        surface.create_polygon(r, 1, width-r, 1, width, 1, width, r, width, height-r,
                               width, height, width-r, height, r, height, 1, height, 1, height-r,
                               1, r, 1, 1, smooth=True, fill=p.hero if hero else p.card,
                               outline="#235050" if hero else p.edge, width=self.px(1), tags="surface")

    def title_row(self, parent, caption):
        row = tk.Frame(parent, bg=parent.cget("bg"))
        marker = tk.Frame(row, bg=self.palette.accent)
        self.set_pixels(marker, width=4, height=15)
        self.place_widget(marker, side="left", spacing={"padx": (0, 6)})
        self.label(row, caption, "section").pack(side="left")
        return row

    def paint_logo(self):
        icon = self.original_icon
        if icon:
            size = self.px(34)
            # The bundled square PNG has a high-resolution source.
            ratio = Fraction(size, icon.width()).limit_denominator(8)
            self.header_icon = icon.zoom(ratio.numerator).subsample(ratio.denominator)
            self.logo_label.configure(image=self.header_icon)

    def rule(self, parent, *, spacing):
        line = tk.Frame(parent, bg=self.palette.edge)
        self.set_pixels(line, height=1)
        self.place_widget(line, fill="x", spacing={"pady": spacing})

    def build_desktop(self, modes):
        p = self.palette
        pack = self.place_widget
        root = tk.Frame(self, bg=p.bg)
        pack(root, fill="both", expand=True, spacing={"padx": 12, "pady": 8})
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        root.rowconfigure(2, weight=3)
        root.rowconfigure(3, weight=2)

        header = tk.Frame(root, bg=p.bg)
        pack(header, "grid", row=0, column=0, sticky="ew", spacing={"pady": (0, 7)})
        icon_path = Path(__file__).parent / "assets" / "muter.png"
        self.original_icon = tk.PhotoImage(file=icon_path) if icon_path.is_file() else None
        self.logo_label = self.label(header)
        if self.original_icon:
            self.iconphoto(True, self.original_icon)
            self.paint_logo()
            pack(self.logo_label, side="left", spacing={"padx": (0, 9)})
        brand = tk.Frame(header, bg=p.bg)
        brand.pack(side="left")
        self.label(brand, APP_NAME, "brand").pack(anchor="w")
        self.label(brand, "会议语音净化", "small", color=p.dim).pack(anchor="w")
        self.settings_btn = self.button(header, "收起设置", self.toggle_settings, width=74)
        self.settings_btn.pack(side="right")
        pack(self.button(header, "诊断与性能", self.show_diagnostics), side="right", spacing={"padx": 7})

        shell, hero_body = self.panel(root, hero=True)
        pack(shell, "grid", row=1, column=0, sticky="nsew", spacing={"pady": (0, 7)})
        hero = tk.Frame(hero_body, bg=p.hero)
        hero.pack(fill="x", expand=True)
        hero.columnconfigure(0, weight=1)
        badge_box = tk.Frame(hero, bg=p.hero)
        badge_box.grid(row=0, column=0, sticky="w")
        self.state_badge = self.label(badge_box, "●  正在准备", "action", color="#80E1C8")
        self.state_badge.pack(side="left")
        self.label(hero, "会议降噪", "hero", color="#FFFFFF").grid(row=1, column=0, sticky="w")
        status = self.flow(hero, self.status_var, 1, color="#C9DFE4")
        pack(status, "grid", row=2, column=0, sticky="ew", spacing={"pady": (3, 0), "padx": (0, 10)})
        hero_actions = tk.Frame(hero, bg=p.hero)
        hero_actions.grid(row=0, column=1, rowspan=3)
        self.quick_btn = self.button(hero_actions, "开启会议降噪", self.toggle_conference, "hero", width=118)
        self.quick_btn.grid(row=0, column=0, sticky="ew")
        self.mute_btn = self.button(hero_actions, "暂停传声", self.toggle_mute, width=118)
        pack(self.mute_btn, "grid", row=1, column=0, sticky="ew", spacing={"pady": (4, 0)})

        core = tk.Frame(root, bg=p.bg)
        pack(core, "grid", row=2, column=0, sticky="nsew", spacing={"pady": (0, 7)})
        core.rowconfigure(0, weight=1)
        core.columnconfigure(0, weight=11, uniform="cards")
        core.columnconfigure(1, weight=9, uniform="cards")
        left, mode = self.panel(core)
        right, monitor = self.panel(core)
        pack(left, "grid", row=0, column=0, sticky="nsew", spacing={"padx": (0, 4)})
        pack(right, "grid", row=0, column=1, sticky="nsew", spacing={"padx": (4, 0)})
        mode_head = self.title_row(mode, "处理模式与声纹样本")
        mode_head.pack(fill="x")
        self.mode_tag = self.label(mode_head, font="small", color=p.dim)
        self.mode_tag.pack(side="right")
        mode_row = tk.Frame(mode, bg=p.card)
        pack(mode_row, fill="x", spacing={"pady": (7, 0)})
        mode_row.columnconfigure(1, weight=1)
        self.quality_combo = ttk.Combobox(mode_row, textvariable=self.quality_var, state="readonly",
                                         width=11, values=modes)
        self.quality_combo.grid(row=0, column=0, sticky="n")
        self.quality_combo.bind("<<ComboboxSelected>>", self.change_model)
        pack(self.flow(mode_row, self.mode_hint, 2, font="small"), "grid", row=0, column=1, sticky="new",
             spacing={"padx": (8, 0)})
        sample = tk.Frame(mode, bg=p.card)
        sample.pack(side="bottom", fill="x")
        self.rule(sample, spacing=(4, 5))
        sample_actions = tk.Frame(sample, bg=p.card)
        sample_actions.pack(side="right")
        sample_details = tk.Frame(sample, bg=p.card)
        pack(sample_details, side="left", fill="x", expand=True, spacing={"padx": (0, 5)})
        self.flow(sample_details, self.voice_summary, 2, font="action", color=p.text).pack(fill="x")
        pack(self.flow(sample_details, self.voice_var, 2, font="small"), fill="x", spacing={"pady": (2, 0)})
        self.sample_menu = tk.Menu(sample, tearoff=False, font=self.fonts["body"], bg=p.card, fg=p.text,
                                   activebackground=p.selected, activeforeground=p.text, disabledforeground=p.dim)
        self.sample_menu.add_command(label="导入声音 WAV", command=self.import_voice)
        self.sample_menu.add_command(label="清除声音样本", command=self.delete_voice)
        self.sample_menu.add_command(label="选段详情", command=self.voice_info)
        self.manage_btn = self.button(sample_actions, "样本管理 ▾", self.show_sample_menu)
        self.manage_btn.pack(side="right")
        self.voice_btn = self.button(sample_actions, "录入本人声音", self.enroll)
        pack(self.voice_btn, side="right", spacing={"padx": (0, 5)})

        monitor_head = tk.Frame(monitor, bg=p.card)
        monitor_head.pack(fill="x")
        self.title_row(monitor_head, "耳机实时试听").pack(side="left")
        self.test_btn = self.button(monitor_head, "开启实时试听", self.toggle_monitor, "primary")
        self.test_btn.pack(side="right")
        monitor_state = tk.Frame(monitor, bg=p.card)
        pack(monitor_state, fill="x", spacing={"pady": (4, 0)})
        self.monitor_message = self.flow(monitor_state, self.test_var, 2, font="small")
        self.monitor_message.pack(side="left", fill="x", expand=True)
        self.ab_row = tk.Frame(monitor_state, bg=p.card)
        self.raw_btn = self.button(self.ab_row, "原声", lambda: self.set_monitor_audio(True))
        self.raw_btn.pack(side="left")
        self.clean_btn = self.button(self.ab_row, "降噪后", lambda: self.set_monitor_audio(False))
        pack(self.clean_btn, side="left", spacing={"padx": 5})
        self.level_panel = tk.Frame(monitor, bg=p.card)
        self.level_panel.pack(side="bottom", fill="x")
        self.rule(self.level_panel, spacing=(4, 5))
        self.label(self.level_panel, "音频电平监控", "action").pack(anchor="w")
        for title, attr, value in (("麦克风", "in_meter", self.input_level), ("处理输出", "out_meter", self.output_level)):
            line = tk.Frame(self.level_panel, bg=p.card)
            pack(line, fill="x", spacing={"pady": (4, 2)})
            self.label(line, title, "small").pack(side="left")
            self.label(line, font="small", color=p.dim, textvariable=value).pack(side="right")
            if attr == "in_meter":
                pack(self.flow(line, self.mic_hint, 1, font="small"), side="left", fill="x", expand=True,
                     spacing={"padx": (5, 4)})
            meter = LevelMeter(self.level_panel, p, self.px(7))
            self.set_pixels(meter, height=7)
            meter.pack(fill="x")
            setattr(self, attr, meter)

        self.settings_card, settings_body = self.panel(root)
        self.settings_card.grid(row=3, column=0, sticky="nsew")
        settings = tk.Frame(settings_body, bg=p.card)
        settings.pack(fill="x", expand=True)
        settings_head = tk.Frame(settings, bg=p.card)
        settings_head.pack(fill="x")
        self.title_row(settings_head, "设备与高级设置").pack(side="left")
        tabs = tk.Frame(settings_head, bg=p.card)
        tabs.pack(side="right")
        self.settings_pages = {}
        self.tab_buttons = {}
        for key, caption in (("devices", "音频设备"), ("ptt", "传声控制 (PTT)"), ("diagnostics", "缓冲与延迟")):
            tab = self.button(tabs, caption, lambda key=key: self.select_settings_tab(key))
            pack(tab, side="left", spacing={"padx": (4, 0)})
            self.tab_buttons[key] = tab
            self.settings_pages[key] = tk.Frame(settings, bg=p.card)
        self.rule(settings, spacing=(5, 5))
        self.build_settings()

        footer = tk.Frame(root, bg=p.bg)
        pack(footer, "grid", row=4, column=0, sticky="ew", spacing={"pady": (7, 0)})
        self.label(footer, "本机处理 · 实时音频不保存 · 关闭窗口后在托盘运行", "small", color=p.dim).pack(side="left")
        self.label(footer, f"WINDOWS / v{DISPLAY_VERSION}", "small", color=p.dim).pack(side="right")
        self.select_settings_tab("devices")
        self.sync_controls()

    def build_settings(self):
        p = self.palette
        pack = self.place_widget
        devices, ptt, latency = (self.settings_pages[key] for key in ("devices", "ptt", "diagnostics"))
        row = tk.Frame(devices, bg=p.card)
        row.pack(fill="x")
        row.columnconfigure((0, 1), weight=1, uniform="devices")
        for col, title, attr, variable in (
            (0, "输入麦克风", "input_combo", self.input_var),
            (1, "手动处理输出", "output_combo", self.output_var),
        ):
            inset = (0, 5) if col == 0 else (5, 0)
            pack(self.label(row, title, "small", color=p.dim), "grid", row=0, column=col, sticky="w",
                 spacing={"padx": inset})
            combo = ttk.Combobox(row, textvariable=variable, state="readonly", width=12)
            pack(combo, "grid", row=1, column=col, sticky="ew", spacing={"padx": inset, "pady": (3, 0)})
            setattr(self, attr, combo)
        self.output_combo.bind("<<ComboboxSelected>>", lambda _: self.update_route())
        actions = tk.Frame(devices, bg=p.card)
        pack(actions, fill="x", spacing={"pady": (6, 0)})
        self.cable_btn = self.button(actions, "虚拟麦克风", self.cable_help)
        self.cable_btn.pack(side="right")
        self.refresh_btn = self.button(actions, "刷新设备", self.refresh_devices)
        pack(self.refresh_btn, side="right", spacing={"padx": (4, 7)})
        self.flow(actions, self.route_var, 2, font="small").pack(side="left", fill="x", expand=True)

        self.ptt_check = tk.Checkbutton(ptt, text="按住右 Ctrl 才传声", variable=self.ptt_var,
            bg=p.card, fg=p.text, selectcolor=p.field, activebackground=p.card, activeforeground=p.text,
            font=self.fonts["body"], anchor="w", padx=0, pady=0, bd=0)
        self.ptt_check.pack(anchor="w")
        self.label(ptt, "松开按键即暂停向会议送音。", "small", color=p.dim).pack(anchor="w")
        controls = tk.Frame(ptt, bg=p.card)
        pack(controls, fill="x", spacing={"pady": (6, 0)})
        self.start_btn = self.button(controls, "手动开始", self.start_live)
        self.start_btn.pack(side="left")
        self.stop_btn = self.button(controls, "停止", self.stop)
        pack(self.stop_btn, side="left", spacing={"padx": 5})
        self.hold_btn = self.button(controls, "按住传声", lambda: None)
        self.hold_btn.pack(side="left")
        self.hold_btn.bind("<ButtonPress-1>", lambda _: setattr(self, "hold_pressed", True))
        self.hold_btn.bind("<ButtonRelease-1>", lambda _: setattr(self, "hold_pressed", False))
        self.hold_btn.bind("<Leave>", lambda _: setattr(self, "hold_pressed", False))

        self.label(latency, "输出缓冲策略", "small", color=p.dim).pack(anchor="w")
        self.latency_combo = ttk.Combobox(latency, textvariable=self.latency_var, state="readonly", width=24,
            values=("低延迟 · 60 ms 缓冲", "均衡 · 80 ms 缓冲", "稳健 · 160 ms 缓冲"))
        pack(self.latency_combo, anchor="w", spacing={"pady": (3, 4)})
        self.label(latency, "缓冲越小，延迟越低。设备不稳定时可选「稳健」。", "small", color=p.dim).pack(anchor="w")

    def select_settings_tab(self, selected):
        self.selected_tab = selected
        for key, page in self.settings_pages.items():
            if key == selected:
                page.pack(fill="x")
            else:
                page.pack_forget()
        self.paint_buttons()

    def toggle_settings(self):
        if self.settings_card.winfo_manager():
            self.settings_card.grid_remove()
            self.settings_btn.configure(text="展开设置")
        else:
            self.settings_card.grid()
            self.settings_btn.configure(text="收起设置")

    def show_sample_menu(self):
        if self.busy:
            return
        menu = self.sample_menu
        menu.entryconfigure(1, state="normal" if self.voice is not None else "disabled")
        menu.entryconfigure(2, state="normal" if self.voice_report else "disabled")
        try:
            menu.tk_popup(self.manage_btn.winfo_rootx(), self.manage_btn.winfo_rooty() + self.manage_btn.winfo_height())
        finally:
            menu.grab_release()

    def show_diagnostics(self):
        if self.closed:
            return
        self.show_window()
        if self.diagnostics_window is not None:
            self.diagnostics_window.deiconify()
            self.diagnostics_window.lift()
            return
        p = self.palette
        dialog = tk.Toplevel(self)
        self.diagnostics_window = dialog
        dialog.title("诊断与性能")
        dialog.transient(self)
        dialog.configure(bg=p.card)
        content = tk.Frame(dialog, bg=p.card)
        self.place_widget(content, fill="both", expand=True, spacing={"padx": 15, "pady": 12})
        self.title_row(content, "实时性能").pack(anchor="w")
        self.place_widget(self.flow(content, self.stats_var, 3), fill="x", spacing={"pady": (8, 7)})
        self.theme_btn = self.button(content, self.theme_caption(), self.toggle_theme, width=105)
        self.theme_btn.pack(side="left")
        self.button(content, "导出诊断", self.export_diagnostics, "primary").pack(side="right")
        dialog.geometry(f"{self.px(395)}x{self.px(125)}")
        dialog.minsize(self.px(320), self.px(120))
        dialog.protocol("WM_DELETE_WINDOW", dialog.withdraw)
        dialog.bind("<Escape>", lambda _: dialog.withdraw())

    def update_mic_hint(self, *_):
        name = self.input_var.get().split("  ·  ", 1)[-1]
        for prefix in ("Microphone (", "麦克风 (", "耳机 ("):
            if name.startswith(prefix) and name.endswith(")"):
                name = name[len(prefix):-1]
                break
        self.mic_hint.set(name or "等待识别")

    def show_levels(self, input_db=None, output_db=None):
        for db, label in ((input_db, self.input_level), (output_db, self.output_level)):
            label.set("— dBFS" if db is None else f"{db:.0f} dBFS")
        if getattr(self, "meter_animation", None) is not None:
            self.after_cancel(self.meter_animation)
        meters = (self.in_meter, self.out_meter)
        targets = tuple(0 if db is None else min(60, max(0, 60 + db)) for db in (input_db, output_db))
        if input_db is None and output_db is None:
            self.meter_animation = None
            for meter in meters:
                meter.set_level(0)
            return
        initial = tuple(meter.level for meter in meters)

        def animate(step):
            for meter, start, target in zip(meters, initial, targets):
                meter.set_level(start + (target - start) * step / 5)
            self.meter_animation = self.after(16, animate, step + 1) if step < 5 and not self.closed else None

        animate(1)
