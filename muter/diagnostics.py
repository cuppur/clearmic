"""Bounded local support logs. Never collect waveforms or voice profiles."""
from datetime import datetime, timezone
from importlib import metadata
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import platform
import sys
import uuid
import zipfile

from . import __version__


def configure(data):
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    destination = str((data / "app.log").resolve())
    for handler in root.handlers:
        if getattr(handler, "baseFilename", None) == destination:
            return
    session = uuid.uuid4().hex[:12]
    handler = RotatingFileHandler(destination, maxBytes=2 * 1024 * 1024, backupCount=3,
                                  encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter(
        f"%(asctime)s %(levelname)s [session={session}] [%(threadName)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)
    logging.info("app_start version=%s os=%s python=%s frozen=%s audio_content_logged=false",
                 __version__, platform.platform(), platform.python_version(), getattr(sys, "frozen", False))


def system_context():
    result = {"app_version": __version__, "created_utc": datetime.now(timezone.utc).isoformat(),
              "os": platform.platform(), "python": platform.python_version(),
              "audio_content_included": False, "packages": {}}
    for name in ("sounddevice", "onnxruntime", "sherpa-onnx", "soxr", "pycaw"):
        try:
            result["packages"][name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    try:
        import sounddevice as sd
        result["host_apis"] = [dict(api) for api in sd.query_hostapis()]
        result["devices"] = [dict(device, index=i) for i, device in enumerate(sd.query_devices())]
    except Exception as exc:
        result["device_query_error"] = str(exc)
    if sys.platform == "win32":
        try:
            from pycaw.pycaw import AudioUtilities
            enumerator = AudioUtilities.GetDeviceEnumerator()
            result["windows_defaults"] = {
                str(flow): {str(role): enumerator.GetDefaultAudioEndpoint(flow, role).GetId()
                            for role in range(3)} for flow in (0, 1)}
        except Exception as exc:
            result["windows_defaults_error"] = str(exc)
    return result


def export_bundle(data, context=None):
    """Allowlist only our bounded text logs plus a generated system report."""
    data = Path(data)
    folder = data / "diagnostics"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / ("ClearMic-diagnostics-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".zip")
    logging.info("diagnostics_export audio_content_included=false")
    for handler in logging.getLogger().handlers:
        handler.flush()
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("system.json", json.dumps(context if context is not None else system_context(),
                                                ensure_ascii=False, indent=2))
        bundle.writestr("README.txt", "Local device names, versions, routing IDs and error logs only.\n"
                        "No microphone audio, voice profiles or test recordings are included.\n")
        for name in ("app.log", "app.log.1", "app.log.2", "app.log.3"):
            path = data / name
            if path.is_file():
                bundle.write(path, name)
    return destination
