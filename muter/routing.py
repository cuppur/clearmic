"""Transactional default-capture routing using MIT-licensed pycaw.

Only changes capture endpoints. Render defaults and app volumes are untouched.
An out-of-process watcher restores our capture changes after a crash.
"""
import ctypes
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

from .audio import DATA, ROOT

STATE = DATA / "routing.json"


class WindowsAudio:
    def repair_virtual_defaults(self):
        """Repair only cable/loopback defaults; preserve all valid physical choices."""
        from pycaw.pycaw import AudioUtilities
        from pycaw.constants import ERole
        from .device_policy import choose, family, names, virtual
        all_devices = AudioUtilities.GetAllDevices()
        active = [d for d in all_devices if d.state.value == 1]
        by_id = {d.id: d for d in active}
        enum = AudioUtilities.GetDeviceEnumerator()
        inputs = [(d.id, {"name": d.FriendlyName or ""}) for d in active if d.id.startswith("{0.0.1.")]
        outputs = [(d.id, {"name": d.FriendlyName or ""}) for d in active if d.id.startswith("{0.0.0.")]
        mic = choose(inputs, enum.GetDefaultAudioEndpoint(1,0).GetId(), capture=True)
        speaker = choose(outputs, enum.GetDefaultAudioEndpoint(0,0).GetId(), input_family=family(names(inputs,mic)))
        live_route = False
        if STATE.exists():
            import psutil
            live_route = psutil.pid_exists(json.loads(STATE.read_text(encoding="utf-8")).get("owner",0))
        changes = []
        for flow, target in ((0,speaker),(1,mic)):
            if flow == 1 and live_route: continue
            for role in range(3):
                old = enum.GetDefaultAudioEndpoint(flow,role).GetId()
                if old not in by_id or virtual(by_id[old].FriendlyName or ""):
                    AudioUtilities.SetDefaultDevice(target,[ERole(role)])
                    changes.append({"flow":flow,"role":role,"before":old,"after":target})
        return changes

    def defaults(self):
        from pycaw.pycaw import AudioUtilities
        e = AudioUtilities.GetDeviceEnumerator()
        return {str(role): e.GetDefaultAudioEndpoint(1, role).GetId() for role in range(3)}

    def set_capture(self, device_id, role):
        from pycaw.pycaw import AudioUtilities
        from pycaw.constants import ERole
        AudioUtilities.SetDefaultDevice(device_id, [ERole(int(role))])

    def cable_capture(self):
        from pycaw.pycaw import AudioUtilities
        for d in AudioUtilities.GetAllDevices():
            if d.id.startswith("{0.0.1.") and "cable output" in (d.FriendlyName or "").lower():
                if d.state.value == 1:
                    return d.id
        raise RuntimeError("没有可用的虚拟麦克风。程序会自动修复组件；如安装器提示重启，请重启后再开启。")


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


class RouteSession:
    def __init__(self, api=None, path=STATE):
        self.api = api or WindowsAudio()
        self.path = Path(path)

    def begin(self, watcher=True):
        if self.path.exists():
            import psutil
            owner = json.loads(self.path.read_text(encoding="utf-8")).get("owner")
            if owner != os.getpid() and owner and psutil.pid_exists(owner):
                raise RuntimeError("另一个 ClearMic 实例正在管理会议麦克风，请先停止该实例。")
            self.restore()
        target = self.api.cable_capture()
        if hasattr(self.api, "repair_virtual_defaults"):
            self.api.repair_virtual_defaults()
        before = self.api.defaults()
        data = {"owner": os.getpid(), "target": target, "before": before}
        atomic_json(self.path, data)
        logging.info("route_begin target=%s before=%s", target, before)
        try:
            for role in before:
                self.api.set_capture(target, role)
            if any(v != target for v in self.api.defaults().values()):
                raise RuntimeError("Windows未接受麦克风路由设置。")
            logging.info("route_verified current=%s", self.api.defaults())
            if watcher:
                args = [sys.executable]
                if not getattr(sys, "frozen", False):
                    args += [str(ROOT / "run_muter.py")]
                args += ["--route-watch", str(os.getpid()), str(self.path)]
                subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
        except Exception:
            self.restore()
            raise

    def restore(self):
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        current = self.api.defaults()
        for role, original in data["before"].items():
            # Respect changes the user/another app made while we were running.
            if current.get(role) == data["target"]:
                self.api.set_capture(original, role)
        self.path.unlink(missing_ok=True)
        logging.info("route_restored before=%s current=%s", data["before"], self.api.defaults())


def watch_route(pid, path):
    import comtypes
    comtypes.CoInitialize()
    try:
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if handle:
            try:
                ctypes.windll.kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        path = Path(path)
        if path.exists() and json.loads(path.read_text(encoding="utf-8")).get("owner") == pid:
            RouteSession(path=path).restore()
    finally:
        comtypes.CoUninitialize()


def install_driver():
    """Invoke the unmodified signed driver bundled by the Windows installer."""
    exe = ROOT / "components" / "vbcable" / "VBCABLE_Setup_x64.exe"
    if not exe.exists():
        raise RuntimeError("缺少组件安装包，请运行完整的 ClearMic 安装程序进行修复。")
    WindowsAudio().repair_virtual_defaults()
    system_snapshot(DATA / "driver-audio-before.json")
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), "-i -h", str(exe.parent), 0)
    if result <= 32:
        raise RuntimeError("Windows未授予驱动安装权限，无法创建虚拟麦克风。")


def finish_driver_install():
    from pycaw.pycaw import AudioUtilities
    from pycaw.constants import ERole
    from .device_policy import virtual
    path = DATA / "driver-audio-before.json"
    if path.exists():
        active = {d.id:d for d in AudioUtilities.GetAllDevices() if d.state.value == 1}
        enum = AudioUtilities.GetDeviceEnumerator()
        for row in json.loads(path.read_text(encoding="utf-8")):
            current = enum.GetDefaultAudioEndpoint(row["flow"], row["role"]).GetId()
            if current in active and virtual(active[current].FriendlyName or "") and row["id"] in active:
                AudioUtilities.SetDefaultDevice(row["id"], [ERole(row["role"])])
        path.unlink(missing_ok=True)
    WindowsAudio().repair_virtual_defaults()


def system_snapshot(path, restore=False):
    from pycaw.pycaw import AudioUtilities
    from pycaw.constants import ERole
    if restore:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
        for row in values:
            AudioUtilities.SetDefaultDevice(row["id"], [ERole(row["role"])])
    else:
        enum = AudioUtilities.GetDeviceEnumerator()
        values = [{"flow": f, "role": r, "id": enum.GetDefaultAudioEndpoint(f,r).GetId()}
                  for f in (0,1) for r in range(3)]
        atomic_json(Path(path), values)
