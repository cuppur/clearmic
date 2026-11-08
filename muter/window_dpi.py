"""Windows DPI and work-area calculations for the desktop UI."""

import ctypes
from ctypes import wintypes

DEFAULT_SIZE = (780, 620)
MINIMUM_SIZE = (680, 540)
BASE_DPI = 96


def enable_per_monitor_dpi():
    """Call before Tk or the notification area creates a window."""
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            user32.SetProcessDPIAware()


def window_display(hwnd=None):
    import win32api
    import win32con

    user32 = ctypes.windll.user32
    if hwnd:
        monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        dpi = user32.GetDpiForWindow(wintypes.HWND(hwnd)) if hasattr(user32, "GetDpiForWindow") else 0
    else:
        monitor = win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY)
        dpi = 0
    if not dpi and hasattr(user32, "GetDpiForSystem"):
        dpi = user32.GetDpiForSystem()
    return dpi or BASE_DPI, win32api.GetMonitorInfo(monitor)["Work"]


def frame_size(dpi):
    import win32con

    frame = wintypes.RECT()
    user32 = ctypes.windll.user32
    if hasattr(user32, "AdjustWindowRectExForDpi"):
        user32.AdjustWindowRectExForDpi(ctypes.byref(frame), win32con.WS_OVERLAPPEDWINDOW, False, 0, dpi)
    else:
        user32.AdjustWindowRectEx(ctypes.byref(frame), win32con.WS_OVERLAPPEDWINDOW, False, 0)
    return frame.right - frame.left, frame.bottom - frame.top


def fit_window(dpi, work, logical_size=DEFAULT_SIZE):
    """Return UI scale, minimum outer size, and a proportionally fitted outer size."""
    left, top, right, bottom = work
    scale = dpi / BASE_DPI
    margin = round(10 * scale)
    width, height = right - left - 2 * margin, bottom - top - 2 * margin
    ui_scale = min(scale, width / MINIMUM_SIZE[0], height / MINIMUM_SIZE[1])
    minimum = tuple(round(value * ui_scale) for value in MINIMUM_SIZE)
    window_scale = min(scale, width / logical_size[0], height / logical_size[1])
    size = tuple(max(low, round(value * window_scale)) for low, value in zip(minimum, logical_size))
    return ui_scale, minimum, size


def top_level_handle(window):
    import win32con
    import win32gui

    return win32gui.GetAncestor(window.winfo_id(), win32con.GA_ROOT)
