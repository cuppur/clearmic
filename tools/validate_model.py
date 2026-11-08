"""Real model smoke/performance check with two public LibriSpeech excerpts.

Run from the repository root after downloading the two fixtures listed below.
This does not access the microphone or the user's reference recording.
"""
import json
from pathlib import Path
import sys

import numpy as np
import soundfile as sf
import soxr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from muter.audio import Model, SR, dbfs, write_json

folder = Path("artifacts/validation")
fixtures = {
    "target.ogg": "https://librosa.org/data/audio/5703-47212-0000.ogg",
    "interferer.ogg": "https://librosa.org/data/audio/3436-172162-0000.ogg",
}


def read(name):
    x, rate = sf.read(folder / name, dtype="float32")
    return soxr.resample(x, rate, SR).astype(np.float32)


def sisdr(estimate, reference):
    e = estimate.astype(np.float64)
    r = reference.astype(np.float64)
    e, r = e - e.mean(), r - r.mean()
    target = np.dot(e, r) / (np.dot(r, r) + 1e-12) * r
    return float(10 * np.log10((np.dot(target, target) + 1e-12) / (np.sum((e - target) ** 2) + 1e-12)))


target, other = read("target.ogg"), read("interferer.ogg")
voice = target[:4 * SR]
clean = np.zeros(12 * SR, np.float32)
clean[:6 * SR] = target[4 * SR:10 * SR]
clean[10 * SR:] = target[10 * SR:12 * SR]
interference = np.zeros_like(clean)
interference[2 * SR:10 * SR] = other[2 * SR:10 * SR]
interference *= np.sqrt(np.mean(clean[:6 * SR] ** 2) / max(np.mean(interference[2 * SR:10 * SR] ** 2), 1e-9))
rng = np.random.default_rng(42)
noise = rng.normal(0, .002, len(clean)).astype(np.float32)
mix = clean + interference + noise
scale = min(1.0, .8 / max(np.max(np.abs(mix)), 1e-6))
mix, clean, interference, voice = (a * scale for a in (mix, clean, interference, voice))
model = Model()
model.load()
model.process(mix[:SR], voice)  # exclude first-call setup from timed fixture
out, stats = model.process(mix, voice)
assert len(out) == len(mix) and np.isfinite(out).all()
overlap = slice(2 * SR, 6 * SR)
background = slice(6 * SR, 10 * SR)
stats.update({
    "fixtures": fixtures,
    "scene": "0-2s target; 2-6s overlap; 6-10s other only; 10-12s target",
    "input_overlap_si_sdr_db": sisdr(mix[overlap], clean[overlap]),
    "output_overlap_si_sdr_db": sisdr(out[overlap], clean[overlap]),
    "other_only_attenuation_db": dbfs(mix[background]) - dbfs(out[background]),
    "note": "Two English public excerpts, synthetic mix. Not real-room/Chinese validation or end-to-end latency.",
})
for name, x in [("mixture.wav", mix), ("target-clean.wav", clean), ("isolated.wav", out)]:
    sf.write(folder / name, x, SR)
write_json(folder / "model-results.json", stats)
print(json.dumps(stats, indent=2))
