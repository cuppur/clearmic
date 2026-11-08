"""Exercise the real Tk monitor controls with muted headphone output.

No audio is saved. Verifies live capture/render, A/B switches, unchanged Windows
defaults, no calls to the recording writer, and a readable diagnostics archive.
"""
import json
import argparse
from pathlib import Path
import sys
import time
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from muter.app import App
from muter.audio import DATA, write_json
from muter.diagnostics import export_bundle, system_context


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.update()
        time.sleep(.01)


parser = argparse.ArgumentParser()
parser.add_argument("--headset-microphone", action="store_true")
args = parser.parse_args()
before = system_context()["windows_defaults"]
recordings_before = {str(p): p.stat().st_mtime_ns for p in DATA.rglob("*.wav")}
app = App()
app.withdraw()
live = None
try:
    with patch("muter.app.sf.write", side_effect=AssertionError("Monitor attempted to save audio")) as writer:
        deadline = time.monotonic() + 15
        while not app.model_ready and time.monotonic() < deadline:
            pump(app, .05)
        assert app.model_ready
        if args.headset_microphone:
            app.input_var.set(next(label for label in app.inputs if "hands-free" in label.lower()))
        app.test_mode_var.set(True)
        app.set_monitor()
        app.paused = True  # Keep render callbacks active but physically silent.
        live = app.live
        assert live is not None, app.status_var.get()
        pump(app, 3)
        assert app.live is live, app.status_var.get()
        app.raw_btn.invoke()
        pump(app, 2)
        assert live.monitor_original.is_set()
        app.clean_btn.invoke()
        pump(app, 2)
        assert not live.monitor_original.is_set()
        assert app.live is live, app.status_var.get()
        assert not app.conference_route
        app.test_btn.invoke()
        deadline = time.monotonic() + 5
        while app.busy and time.monotonic() < deadline:
            pump(app, .05)
        assert not app.busy
        assert not app.test_mode_var.get()
        writer.assert_not_called()
    after = system_context()["windows_defaults"]
    assert before == after, "Monitor changed Windows defaults"
    assert recordings_before == {str(p): p.stat().st_mtime_ns for p in DATA.rglob("*.wav")}
    bundle = export_bundle(DATA)
    with zipfile.ZipFile(bundle) as archive:
        assert not any(name.endswith((".wav", ".ogg", ".flac")) for name in archive.namelist())
        assert not json.loads(archive.read("system.json"))["audio_content_included"]
    result = {"ok": live.error is None, "audio_saved": False, "render_muted": True,
              "input_callbacks": live.input_callbacks, "output_callbacks": live.output_callbacks,
              "input_device": live.input_device, "output_device": live.output_device,
              "error": live.error, "defaults_unchanged": before == after,
              "diagnostics_bundle": str(bundle), "ab_controls_exercised": True}
    write_json("artifacts/validation/realtime-monitor" + ("-hfp" if args.headset_microphone else "") + ".json", result)
    print(json.dumps(result, ensure_ascii=True))
    assert result["ok"] and live.input_callbacks > 0 and live.output_callbacks > 0
finally:
    if live:
        live.stop()
        live.thread.join(timeout=5)
    app.close()
    try:
        pump(app, .2)
    except Exception:
        pass
