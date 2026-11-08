"""Windows notification area integration on the Tk message loop."""
from contextlib import contextmanager
import logging
from pathlib import Path
import time

import pywintypes
import win32api
import win32con
import win32event
import win32gui
import winerror


WINDOW_CLASS = "Muter.NotificationArea"
TRAY_EVENT = win32con.WM_APP + 1
SHOW_WINDOW = win32con.WM_APP + 2


@contextmanager
def desktop_instance():
    """Open the existing window when a shortcut is launched again."""
    mutex = win32event.CreateMutex(None, False, r"Local\Muter.Desktop")
    already_running = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    try:
        if already_running:
            # The first launch may still be loading before its tray window exists.
            for _ in range(30):
                try:
                    hwnd = win32gui.FindWindow(WINDOW_CLASS, None)
                    if hwnd:
                        win32gui.PostMessage(hwnd, SHOW_WINDOW, 0, 0)
                        break
                except pywintypes.error:
                    pass
                time.sleep(.1)
        yield not already_running
    finally:
        mutex.Close()


class TrayIcon:
    def __init__(self, app):
        self.app = app
        self.hwnd = self.icon = self.atom = 0
        self.registered = False
        self.menu_open = False
        self.title = "Muter"
        self.instance = win32api.GetModuleHandle(None)
        self.taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
        try:
            window_class = win32gui.WNDCLASS()
            window_class.hInstance = self.instance
            window_class.lpszClassName = WINDOW_CLASS
            window_class.lpfnWndProc = self._on_message
            self.atom = win32gui.RegisterClass(window_class)
            self.hwnd = win32gui.CreateWindow(WINDOW_CLASS, "Muter tray", win32con.WS_POPUP,
                                              0, 0, 0, 0, 0, 0, self.instance, None)
            icon_path = Path(__file__).parent / "assets" / "muter.ico"
            self.icon = win32gui.LoadImage(0, str(icon_path), win32con.IMAGE_ICON,
                                          win32api.GetSystemMetrics(win32con.SM_CXSMICON),
                                          win32api.GetSystemMetrics(win32con.SM_CYSMICON),
                                          win32con.LR_LOADFROMFILE)
            self._register_icon()
        except Exception:
            self.close()
            raise

    def _register_icon(self):
        flags = win32gui.NIF_ICON | win32gui.NIF_TIP | win32gui.NIF_MESSAGE
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (self.hwnd, 0, flags, TRAY_EVENT, self.icon, self.title))
        self.registered = True
        logging.info("tray_registered")

    def set_status(self, status):
        title = f"Muter · {status}"
        if self.title == title:
            return
        self.title = title
        if self.registered:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, (self.hwnd, 0, win32gui.NIF_TIP, 0, 0, title))
            except pywintypes.error:
                logging.debug("Tray tooltip update unavailable", exc_info=True)

    def _on_message(self, hwnd, message, wparam, lparam):
        if message == SHOW_WINDOW or (message == TRAY_EVENT and lparam == win32con.WM_LBUTTONUP):
            self.app.events.put(("tray_show", None))
        elif message == TRAY_EVENT and lparam == win32con.WM_RBUTTONUP:
            self.app.events.put(("tray_menu", None))
        elif message == self.taskbar_created:
            self.registered = False
            try:
                self._register_icon()
            except pywintypes.error:
                logging.exception("Tray registration after Explorer restart failed")
                self.app.events.put(("tray_show", None))
        else:
            return win32gui.DefWindowProc(hwnd, message, wparam, lparam)
        return 0

    def _build_menu(self, menu):
        app = self.app
        commands = {}

        def add(parent, title, command, enabled=True, checked=False):
            item_id = len(commands) + 1
            flags = win32con.MF_STRING
            if not enabled:
                flags |= win32con.MF_GRAYED
            if checked:
                flags |= win32con.MF_CHECKED
            win32gui.AppendMenu(parent, flags, item_id, title)
            commands[item_id] = command if enabled else None

        def submenu(title):
            child = win32gui.CreatePopupMenu()
            win32gui.AppendMenu(menu, win32con.MF_POPUP, child, title)
            return child

        add(menu, "打开主界面", app.show_window)
        win32gui.SetMenuDefaultItem(menu, 0, True)
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        meeting_title = "停止会议降噪" if app.conference_route else app.quick_btn.cget("text")
        add(menu, meeting_title, app.quick_btn.invoke, app.quick_btn.cget("state") != "disabled")
        add(menu, app.mute_btn.cget("text"), app.mute_btn.invoke, app.mute_btn.cget("state") != "disabled")
        modes = submenu("处理模式")
        for mode in app.quality_combo.cget("values"):
            add(modes, mode, lambda mode=mode: app.choose_tray_mode(mode),
                not app.busy, app.quality_var.get() == mode)
        monitor = submenu("耳机实时试听")
        add(monitor, app.test_btn.cget("text"), app.toggle_tray_monitor, app.test_btn.cget("state") != "disabled")
        listening = app.monitoring and app.live is not None
        original = listening and app.live.monitor_original.is_set()
        add(monitor, "原声", app.raw_btn.invoke, listening, original)
        add(monitor, "降噪后", app.clean_btn.invoke, listening, listening and not original)
        advanced = submenu("高级")
        add(advanced, "设备与高级设置", app.show_settings)
        add(advanced, "刷新音频设备", app.refresh_btn.invoke, not app.busy)
        add(advanced, "导出诊断", app.export_diagnostics)
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        add(menu, "退出 Muter", app.close)
        return commands

    def show_menu(self):
        if self.menu_open or self.app.closed:
            return
        self.menu_open = True
        menu = 0
        try:
            menu = win32gui.CreatePopupMenu()
            commands = self._build_menu(menu)
            x, y = win32gui.GetCursorPos()
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except pywintypes.error:
                pass
            flags = win32con.TPM_RETURNCMD | win32con.TPM_RIGHTBUTTON | win32con.TPM_NONOTIFY
            command = commands.get(win32gui.TrackPopupMenu(menu, flags, x, y, 0, self.hwnd, None))
            if command:
                self.app.after_idle(command)
        finally:
            if menu:
                win32gui.DestroyMenu(menu)
            self.menu_open = False
            win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)

    def close(self):
        if self.registered:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
            except pywintypes.error:
                logging.debug("Tray icon already unavailable", exc_info=True)
            self.registered = False
        if self.hwnd:
            win32gui.DestroyWindow(self.hwnd)
            self.hwnd = 0
        if self.icon:
            win32gui.DestroyIcon(self.icon)
            self.icon = 0
        if self.atom:
            win32gui.UnregisterClass(WINDOW_CLASS, self.instance)
            self.atom = 0
