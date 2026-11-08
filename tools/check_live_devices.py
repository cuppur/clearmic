"""Explicit diagnostic: 2 x 6s local microphone runs, output always muted.

Does not save microphone audio or use the user's enrolled voice. Requires the
public validation fixture artifacts/validation/target.ogg.
"""
import json
from pathlib import Path
import queue
import sys
import time

import soundfile as sf
import soxr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from muter.audio import LiveAudio, Model, devices, write_json

source, rate = sf.read("artifacts/validation/target.ogg", dtype="float32")
voice = soxr.resample(source[:4 * rate], rate, 16000)
defaults = devices()[2]
model = Model()
model.load()
results = []
for cycle in range(2):
    events = queue.Queue()
    live = LiveAudio(model, voice, *defaults, events)
    live.muted.set()
    live.start()
    started = time.monotonic()
    opened, last = None, None
    while time.monotonic() - started < 6:
        try:
            kind, value = events.get(timeout=.25)
            if kind == "live_started":
                opened = value
            if kind == "meters":
                last = value
            if kind == "live_stopped":
                break
        except queue.Empty:
            pass
    live.stop()
    live.thread.join(timeout=5)
    result = {"cycle": cycle + 1, "opened": opened, "last_metrics": last,
              "error": live.error, "thread_stopped": not live.thread.is_alive()}
    results.append(result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    assert opened is not None and last is not None and live.error is None and not live.thread.is_alive()
write_json("artifacts/validation/live-device-results.json", {
    "output_muted": True, "audio_saved": False, "runs": results,
})
