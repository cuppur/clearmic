"""Explicit local device diagnostic, output muted and microphone never saved."""
import argparse
import queue
from pathlib import Path
import sys
import time

import soundfile as sf
import soxr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from muter.audio import Model, LiveAudio, devices, write_json
from muter.realtime import LiveSettings

p = argparse.ArgumentParser()
p.add_argument("--seconds", type=int, default=60)
p.add_argument("--precision", choices=["int8", "fp32"], default="int8")
p.add_argument("--backend", choices=["voxlock", "gtcrn"], default="voxlock")
p.add_argument("--output-device", type=int)
p.add_argument("--input-device", type=int)
p.add_argument("--guard", action="store_true")
p.add_argument("--buffer-ms", type=int, default=80)
p.add_argument("--chunk-ms", type=int, default=40)
p.add_argument("--report", default="artifacts/validation/runtime-soak.json")
args = p.parse_args()
x, rate = sf.read("artifacts/validation/target.ogg", dtype="float32")
voice = soxr.resample(x[:4 * rate], rate, 16000).astype("float32")
events = queue.Queue()
input_id, output_id = devices()[2]
if args.input_device is not None:
    input_id = args.input_device
if args.output_device is not None:
    output_id = args.output_device
if args.backend == "gtcrn":
    from muter.denoise import GTCRNModel
    model = GTCRNModel()
else:
    model = Model(args.precision)
if args.guard:
    from muter.pipeline import GuardedModel
    model = GuardedModel(model)
live = LiveAudio(model, voice, input_id, output_id, events,
                 LiveSettings(args.buffer_ms, args.chunk_ms))
live.muted.set()
started = time.monotonic()
live.start()
stats, opened, recovery = [], None, []
try:
    while time.monotonic() - started < args.seconds:
        try:
            kind, value = events.get(timeout=.25)
            if kind == "live_started":
                opened = value
            if kind == "meters":
                stats.append(value)
            if kind in ("capture_recovering", "capture_recovered"):
                recovery.append({"event": kind, "message": value, "seconds": time.monotonic() - started})
            if kind == "live_stopped":
                break
        except queue.Empty:
            pass
finally:
    live.stop()
    live.thread.join(timeout=5)
result = {"elapsed_seconds": time.monotonic() - started, "precision": args.precision,
          "output_muted": True, "audio_saved": False, "devices": [input_id, output_id],
          "opened": opened, "error": live.error, "thread_stopped": not live.thread.is_alive(),
          "settings": vars(args), "metrics": stats, "recovery": recovery}
write_json(args.report, result)
print({k: v for k, v in result.items() if k != "metrics"}, flush=True)
if stats:
    print("buffer range (ms)", min(x["buffer_ms"] for x in stats), max(x["buffer_ms"] for x in stats),
          "last p95", stats[-1]["p95"], "underruns", stats[-1]["underruns"], flush=True)
assert opened and stats and live.error is None and not live.thread.is_alive()
