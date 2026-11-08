"""Reproducible synthetic two-speaker evaluation; never opens a microphone.

The reference and target evaluation utterance use non-overlapping portions of
the same public English LibriSpeech recording. No user's enrollment is read.
Run: .venv/Scripts/python tools/evaluate_scenarios.py --precision int8
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np
import soundfile as sf
import soxr

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SR = 16000
FIXTURES = {
    "target.ogg": "https://librosa.org/data/audio/5703-47212-0000.ogg",
    "interferer.ogg": "https://librosa.org/data/audio/3436-172162-0000.ogg",
}
EPS = 1e-12


def rms(x):
    x = np.asarray(x, np.float64)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def level_db(x):
    return float(20 * np.log10(max(rms(x), 1e-6)))


def active_mask(reference, rate=SR):
    """20 ms energy activity for scoring, not a speech/identity detector."""
    x = np.asarray(reference, np.float64)
    if not x.size or rms(x) < 1e-9:
        return np.zeros(len(x), dtype=bool)
    block = max(1, round(rate * .02))
    padded = np.pad(x, (0, (-len(x)) % block))
    energies = np.mean(padded.reshape(-1, block) ** 2, axis=1)
    # Relative threshold preserves a deliberately low-volume target in scoring.
    threshold = max(float(energies.max()) * 1e-4, 1e-16)
    return np.repeat(energies > threshold, block)[:len(x)]


def target_metrics(estimate, reference):
    """Report scale-sensitive preservation alongside scale-invariant SI-SDR.

    A silent estimate never gets a finite attractive SI-SDR value. It is marked
    absent, with -120 dB gain and scale-sensitive SNR near 0 dB.
    """
    e, r = (np.asarray(x, np.float64) for x in (estimate, reference))
    if e.shape != r.shape or e.ndim != 1 or not e.size:
        raise ValueError("Metric signals must be non-empty matching 1D arrays")
    if not np.isfinite(e).all() or not np.isfinite(r).all():
        raise ValueError("Metric signals must be finite")
    r, e = r - r.mean(), e - e.mean()
    reference_energy = float(np.dot(r, r))
    if reference_energy <= EPS:
        raise ValueError("Target metrics are not defined for a silent reference")
    gain = float(np.dot(e, r) / reference_energy)
    projected = gain * r
    residual_energy = float(np.sum((e - projected) ** 2))
    output_present = rms(e) >= max(1e-8, rms(r) * 1e-4)
    si_sdr = float(10 * np.log10((np.sum(projected ** 2) + EPS) / (residual_energy + EPS))) if output_present else None
    gain_db = float(20 * np.log10(max(abs(gain), 1e-6)))
    return {
        "si_sdr_db": si_sdr,
        "target_gain_db": gain_db,
        "target_gain_sign": 1 if gain >= 0 else -1,
        "scale_sensitive_snr_db": float(10 * np.log10(reference_energy / (np.sum((e - r) ** 2) + EPS))),
        "output_to_target_rms_db": float(20 * np.log10(max(rms(e), 1e-8) / max(rms(r), 1e-8))),
        "output_present": bool(output_present),
        "target_attenuated_over_9db": bool(gain_db < -9),
    }


def leakage_metrics(output, input_mix):
    """Only valid for a region where the desired target is known to be absent."""
    y, x = np.asarray(output), np.asarray(input_mix)
    if y.shape != x.shape or not len(x) or not np.isfinite(y).all() or not np.isfinite(x).all():
        raise ValueError("Leakage signals must be finite non-empty matching arrays")
    return {"input_rms_dbfs": level_db(x), "output_rms_dbfs": level_db(y),
            "attenuation_db": float(level_db(x) - level_db(y))}


def score_scene(output, mixture, target, interferer, noise):
    y, x, clean, other, noise = (np.asarray(a, np.float32) for a in (output, mixture, target, interferer, noise))
    if any(a.shape != x.shape for a in (y, clean, other, noise)) or not np.isfinite(y).all():
        raise ValueError("Invalid model output shape or numeric values")
    target_active, other_active = active_mask(clean), active_mask(other)
    noise_active = active_mask(noise)
    report = {"target_active_seconds": float(target_active.sum() / SR),
              "peak_output": float(np.max(np.abs(y))), "clip_sample_fraction": float(np.mean(np.abs(y) >= .979))}
    if target_active.any():
        report["target"] = target_metrics(y[target_active], clean[target_active])
        report["input_target"] = target_metrics(x[target_active], clean[target_active])
    overlap = target_active & other_active
    if overlap.any():
        report["overlap"] = target_metrics(y[overlap], clean[overlap])
        report["input_overlap"] = target_metrics(x[overlap], clean[overlap])
    target_alone = target_active & ~other_active
    if target_alone.any():
        report["target_without_other"] = target_metrics(y[target_alone], clean[target_alone])
    absent = ~target_active & (other_active | noise_active)
    if absent.any():
        report["target_absent_leakage"] = leakage_metrics(y[absent], x[absent])
        report["target_absent_scored_seconds"] = float(absent.sum() / SR)
    return report


def normalize(x, target_rms=.07):
    return np.asarray(x, np.float32) * (target_rms / max(rms(x), 1e-8))


class ReferencePrimedStream:
    """Experimental study wrapper: prime recurrent state, never expose primer.

    The underlying stream holds back an algorithmic tail. Skip exactly this
    known count from future output so audio from enrollment cannot leak into a
    real call and scoring remains sample-aligned. This wrapper is evaluation
    only until target-present and target-absent cases have been checked.
    """
    def __init__(self, stream, reference):
        self.stream = stream
        started = time.perf_counter()
        # The caller supplies the already-selected 3s model reference.
        stream.push(reference)
        self.skip = int(stream.n_in - stream.n_out)
        if self.skip < 0:
            raise ValueError("Invalid priming sample counters")
        self.held_primer_samples = self.skip
        self.prime_seconds = time.perf_counter() - started

    def _strip_primer(self, output):
        cut = min(self.skip, len(output))
        self.skip -= cut
        return output[cut:]

    def push(self, chunk, **kwargs):
        return self._strip_primer(self.stream.push(chunk, **kwargs))

    def flush(self):
        return self._strip_primer(self.stream.flush())


def build_scenarios(target, other, reference=None):
    if reference is None:
        if len(target) < 12 * SR or len(other) < 10 * SR:
            raise ValueError("Public fixtures need at least 12 s target and 10 s interferer")
        voice = normalize(target[:4 * SR])
        target_utterance = target[4 * SR:12 * SR]
        other_utterance = other[2 * SR:10 * SR]
    else:
        if len(target) < 8 * SR or len(other) < 8 * SR or len(reference) < 3 * SR:
            raise ValueError("Separate fixtures need 8 s evaluation speech and 3 s reference")
        voice = normalize(reference)
        target_utterance, other_utterance = target[:8 * SR], other[:8 * SR]
    clean = np.zeros(12 * SR, np.float32)
    clean[:6 * SR] = target_utterance[:6 * SR]
    clean[10 * SR:] = target_utterance[6 * SR:8 * SR]
    clean = normalize(clean)
    interference = np.zeros_like(clean)
    interference[2 * SR:10 * SR] = other_utterance
    interference = normalize(interference)
    rng = np.random.default_rng(713)
    # Deterministic colored stationary noise plus three broad transient bursts.
    raw = rng.normal(0, 1, len(clean) + 31)
    fan = np.convolve(raw, np.ones(32) / 32, mode="valid")[:len(clean)]
    background = normalize(fan, .018)
    for seconds in (1.5, 5.1, 9.2):
        begin, length = round(seconds * SR), round(.08 * SR)
        burst = rng.normal(0, .13, length) * np.exp(-np.arange(length) / (SR * .015))
        background[begin:begin + length] += burst.astype(np.float32)
    zero = np.zeros_like(clean)
    definitions = [
        ("target_only", "Only target; 6-10 s silence", clean, zero, zero),
        ("other_only", "Only other speaker; enrolled target never speaks", zero, interference, zero),
        ("overlap_equal", "Target 0-6/10-12 s, other 2-10 s, equal overall RMS", clean, interference, zero),
        ("other_louder_6db", "Other speaker raised 6 dB", clean, interference * 2, zero),
        ("quiet_target_12db", "Target lowered 12 dB; other unchanged", clean * .25, interference, zero),
        ("noise_and_talkers", "Equal talkers plus colored fan and transient noises", clean, interference, background),
    ]
    scenarios = []
    for name, description, speech, interferer, noise in definitions:
        mixture = speech + interferer + noise
        scale = min(1.0, .9 / max(float(np.max(np.abs(mixture))), 1e-8))
        scenarios.append({"name": name, "description": description, "mixture": mixture * scale,
                          "target": speech * scale, "other": interferer * scale, "noise": noise * scale,
                          "peak_normalization_scale": scale})
    return voice, scenarios


def fidelity_scenarios(scenarios):
    """No competing talker: isolate noise removal from speaker rejection."""
    clean = scenarios[0]["target"].copy()
    light_noise = scenarios[-1]["noise"].copy() * .25
    zero = np.zeros_like(clean)
    return [
        {"name": "clean_speech", "description": "Target is the only talker; no added noise",
         "mixture": clean.copy(), "target": clean, "other": zero, "noise": zero,
         "peak_normalization_scale": 1.0},
        {"name": "light_noise_speech", "description": "Target is the only talker; light colored fan/transient noise",
         "mixture": clean + light_noise, "target": clean, "other": zero, "noise": light_noise,
         "peak_normalization_scale": 1.0},
        {"name": "quiet_speech_light_noise", "description": "Only target, lowered 12 dB against the same light noise",
         "mixture": clean * .25 + light_noise, "target": clean * .25, "other": zero, "noise": light_noise,
         "peak_normalization_scale": 1.0},
    ]


def read_audio(path):
    x, rate = sf.read(path, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    return soxr.resample(x, rate, SR).astype(np.float32) if rate != SR else x


def load_mandarin(fixture_dir, target_speaker):
    sources = json.loads((fixture_dir / "SOURCES.json").read_text(encoding="utf-8"))
    ids = list(sources["speakers"])
    other_speaker = next(s for s in ids if s != target_speaker)
    def clips(speaker):
        files = sources["speakers"][speaker]["source_files"]
        # Exact original-utterance boundary, not an approximate time split in a composite.
        return files, [read_audio(fixture_dir / item["file"]) for item in files]
    target_files, target_clips = clips(target_speaker)
    other_files, other_clips = clips(other_speaker)
    reference = np.concatenate(target_clips[:3])
    target = np.concatenate(target_clips[3:])
    other = np.concatenate(other_clips[3:])
    provenance = {"dataset": sources["dataset"], "revision": sources["revision"],
                  "target_speaker": target_speaker, "other_speaker": other_speaker,
                  "reference_files": [item["file"] for item in target_files[:3]],
                  "target_files": [item["file"] for item in target_files[3:]],
                  "other_files": [item["file"] for item in other_files[3:]],
                  "source_manifest": str(fixture_dir / "SOURCES.json")}
    return reference, target, other, provenance


def reference_switch_check(output_dir=None, fixtures_dir=None, precision="int8"):
    """Same A+B input with held-out A/B enrollment; verify conditioning."""
    from muter.audio import Model
    from voxlock.core import fit_sample
    fixture_dir = Path(fixtures_dir or ROOT / "artifacts" / "validation")
    output_dir = Path(output_dir or ROOT / "artifacts" / "evaluation" / "reference-switch-heldout")
    output_dir.mkdir(parents=True, exist_ok=True)
    target, other = [read_audio(fixture_dir / name) for name in ("target.ogg", "interferer.ogg")]
    if len(other) < 16 * SR:
        raise ValueError("Interferer needs >=16s to keep reference [12,16)s disjoint from mix [2,10)s")
    voice_a, scenes = build_scenarios(target, other)
    a, b = fit_sample(voice_a), fit_sample(normalize(other[12 * SR:16 * SR]))
    scene = scenes[2]
    model = Model(precision=precision, threads=2)
    ya, _ = model.process(scene["mixture"], a)
    yb, _ = model.process(scene["mixture"], b)
    ea = model.vl.enroll.run(None, {"enroll": a[None]})
    eb = model.vl.enroll.run(None, {"enroll": b[None]})
    result = {"precision": precision, "reference_a": "A source[0,4)s, disjoint from A mix[4,12)s",
              "reference_b": "B source[12,16)s, disjoint from B mix[2,10)s",
              "output_max_abs_difference": float(np.max(np.abs(ya - yb))),
              "output_rms_difference": rms(ya - yb), "outputs_exactly_equal": bool(np.array_equal(ya, yb)),
              "enroll_outputs": [{"name": meta.name, "shape": list(x.shape),
                  "max_abs_difference": float(np.max(np.abs(x - y))),
                  "cosine": float(np.dot(x.ravel(), y.ravel()) / (np.linalg.norm(x) * np.linalg.norm(y) + EPS))}
                  for meta, x, y in zip(model.vl.enroll.get_outputs(), ea, eb)],
              "metrics_a_with_a_reference": score_scene(ya, scene["mixture"], scene["target"], scene["other"], scene["noise"]),
              "metrics_a_with_b_reference": score_scene(yb, scene["mixture"], scene["target"], scene["other"], scene["noise"]),
              "metrics_b_with_b_reference": score_scene(yb, scene["mixture"], scene["other"], scene["target"], scene["noise"])}
    for name, audio in (("mixture.wav", scene["mixture"]), ("reference-a.wav", a), ("reference-b.wav", b),
                        ("output-a-reference.wav", ya), ("output-b-reference.wav", yb)):
        sf.write(output_dir / name, audio, SR, subtype="PCM_16")
    (output_dir / "results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if not k.startswith("metrics")}, indent=2), flush=True)
    return result


def probability_summary(records):
    """Chunk-labelled detector diagnostics, not population accuracy estimates."""
    result = {}
    for period in ("all", "initial_1s", "stable_after_1s"):
        rows = [r for r in records if period == "all" or
                (period == "initial_1s" and r["end_seconds"] <= 1.0) or
                (period == "stable_after_1s" and r["start_seconds"] >= 1.0)]
        populations = {
            "target_present": [r for r in rows if r["target_present"]],
            "other_only": [r for r in rows if r["other_present"] and not r["target_present"]],
            "overlap": [r for r in rows if r["target_present"] and r["other_present"]],
        }
        section = {"chunks": len(rows), "populations": {}, "thresholds": {}}
        for name, selected in populations.items():
            p = np.array([r["probability"] for r in selected], np.float64)
            section["populations"][name] = {"chunks": len(p), "distribution":
                {"min": float(p.min()), "p10": float(np.percentile(p, 10)), "median": float(np.median(p)),
                 "p90": float(np.percentile(p, 90)), "max": float(p.max())} if len(p) else None}
        for threshold in (.5, .65):
            targets, others = populations["target_present"], populations["other_only"]
            rejected = [r for r in targets if r["probability"] < threshold]
            leaked = [r for r in others if r["probability"] >= threshold]
            section["thresholds"][str(threshold)] = {
                "target_rejected_chunks": len(rejected), "target_chunks": len(targets),
                "target_rejected_fraction": len(rejected) / len(targets) if targets else None,
                "target_rejected_active_seconds": sum(r["target_active_fraction"] * .16 for r in rejected),
                "other_accepted_chunks": len(leaked), "other_chunks": len(others),
                "other_accepted_fraction": len(leaked) / len(others) if others else None,
                "other_accepted_active_seconds": sum(r["other_active_fraction"] * .16 for r in leaked),
            }
        result[period] = section
    times = [r["processing_ms"] for r in records]
    result["processing_ms"] = {"mean": float(np.mean(times)), "p95": float(np.percentile(times, 95)),
                               "max": float(np.max(times)), "chunk_ms": 160} if times else None
    return result


def gate_variant_summary(records):
    """Offline centered one-frame dilation: +160ms lookahead and hangover."""
    variants = {}
    for threshold in (.5, .65):
        passed = np.array([r["probability"] >= threshold for r in records], dtype=bool)
        expanded = passed.copy()
        if len(passed) > 1:
            expanded[:-1] |= passed[1:]
            expanded[1:] |= passed[:-1]
        derived = [dict(row, probability=float(value)) for row, value in zip(records, expanded)]
        base = probability_summary(records)
        dilation = probability_summary(derived)
        variants[str(threshold)] = {"probability_threshold": threshold, "lookahead_ms": 160, "hangover_ms": 160,
            "base": {period: base[period]["thresholds"][str(threshold)] for period in ("all", "initial_1s", "stable_after_1s")},
            "one_frame_dilation": {period: dilation[period]["thresholds"][str(threshold)] for period in ("all", "initial_1s", "stable_after_1s")}}
    return variants


def evaluate_pvad(fixture_set="english", output_dir=None, fixtures_dir=None, target_speaker=None, threads=4):
    from muter.pvad import Guard
    from voxlock.core import fit_sample
    fixture_dir = Path(fixtures_dir or ROOT / "artifacts" / "validation" / ("chinese" if fixture_set == "mandarin" else ""))
    output_dir = Path(output_dir or ROOT / "artifacts" / "evaluation" / f"pvad-{fixture_set}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if fixture_set == "mandarin" and target_speaker is None:
        sources = json.loads((fixture_dir / "SOURCES.json").read_text(encoding="utf-8"))
        cases = [evaluate_pvad(fixture_set, output_dir / speaker, fixture_dir, speaker, threads) for speaker in sources["speakers"]]
        report = {"fixture_set": fixture_set, "cases": cases}
        (output_dir / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    if fixture_set == "mandarin":
        reference, target, other, provenance = load_mandarin(fixture_dir, target_speaker)
        voice, scenes = build_scenarios(target, other, reference)
    else:
        target, other = [read_audio(fixture_dir / name) for name in ("target.ogg", "interferer.ogg")]
        voice, scenes = build_scenarios(target, other)
        provenance = FIXTURES
    voice = fit_sample(voice)
    started = time.perf_counter()
    Guard.load_models(threads=threads)
    load_seconds = time.perf_counter() - started
    report = {"fixture_set": fixture_set, "fixtures": provenance, "model_load_seconds": load_seconds,
              "reference_samples": len(voice), "threads": threads, "scenarios": [],
              "limitations": ["Fixed public voices in synthetic mono mixtures, not real-room accuracy",
                              "Target/other chunk labels use known-source 20ms energy activity >=25% of a 160ms chunk",
                              "Rejected/accepted active seconds are energy-labelled diagnostics, not transcript word error",
                              "The 160ms chunk crossing the 1s boundary is excluded from initial/stable split only",
                              "Probability thresholds are evaluated, not assumed to be calibrated"]}
    keep = {"target_only", "other_only", "overlap_equal", "quiet_target_12db"}
    for scene in scenes:
        if scene["name"] not in keep:
            continue
        guard = Guard(voice, threads=threads)
        records = []
        target_activity, other_activity = active_mask(scene["target"]), active_mask(scene["other"])
        for start in range(0, len(scene["mixture"]), 2560):
            end = start + 2560
            chunk = scene["mixture"][start:end]
            if len(chunk) < 2560:
                chunk = np.pad(chunk, (0, 2560 - len(chunk)))
            p = guard.push(chunk)
            ta = float(np.mean(target_activity[start:end]))
            oa = float(np.mean(other_activity[start:end]))
            records.append({"start_seconds": start / SR, "end_seconds": min(end, len(scene["mixture"])) / SR,
                            "probability": p, "processing_ms": guard.last_processing_ms,
                            "target_active_fraction": ta, "other_active_fraction": oa,
                            "target_present": ta >= .25, "other_present": oa >= .25})
        scored = {"name": scene["name"], "enrollment_ms": guard.enrollment_ms,
                  "summary": probability_summary(records), "gate_variants": gate_variant_summary(records), "frames": records}
        report["scenarios"].append(scored)
        print(json.dumps({"fixture": target_speaker or fixture_set, "name": scored["name"],
                          "summary": scored["summary"]}, ensure_ascii=False), flush=True)
        # Preserve completed scenes even if a later diagnostic is interrupted.
        (output_dir / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def evaluate(precision="int8", chunk_ms=40, output_dir=None, fixtures_dir=None,
             prime_reference=False, other_only_seconds=0, fixture_set="english", target_speaker=None,
             reference_selection="loudest", suite="speakers", backend="voxlock"):
    from muter.audio import Model
    from voxlock.core import fit_sample
    default_fixture = ROOT / "artifacts" / "validation"
    if fixture_set == "mandarin":
        default_fixture /= "chinese"
    fixture_dir = Path(fixtures_dir or default_fixture)
    suffix = "-primed" if prime_reference else ""
    if reference_selection == "quality":
        suffix += "-quality-reference"
    if suite == "fidelity":
        suffix += "-fidelity"
    prefix = "mandarin-" if fixture_set == "mandarin" else ""
    if backend != "voxlock":
        prefix += backend + "-"
    output_dir = Path(output_dir or ROOT / "artifacts" / "evaluation" / f"{prefix}{precision}-{chunk_ms}ms{suffix}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if backend == "both":
        if prime_reference:
            raise ValueError("Reference priming is only an experiment for VoxLock")
        comparison = {"fixture_set": fixture_set, "suite": suite, "reference_selection": reference_selection,
                      "note": "GTCRN is environmental denoising; it has no speaker-identity rejection objective",
                      "backends": {}}
        for name in ("voxlock", "gtcrn"):
            comparison["backends"][name] = evaluate(
                precision if name == "voxlock" else "fp32", chunk_ms, output_dir / name, fixture_dir,
                prime_reference, other_only_seconds, fixture_set, target_speaker,
                reference_selection, suite, name)
        (output_dir / "results.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
        return comparison
    if fixture_set == "mandarin" and target_speaker is None:
        cases = []
        sources = json.loads((fixture_dir / "SOURCES.json").read_text(encoding="utf-8"))
        for speaker in sources["speakers"]:
            case = evaluate(precision, chunk_ms, output_dir / speaker, fixture_dir,
                            prime_reference, other_only_seconds, fixture_set, speaker,
                            reference_selection, suite, backend)
            cases.append(case)
        combined = {"fixture_set": fixture_set, "precision": precision, "chunk_ms": chunk_ms,
                    "prime_reference": prime_reference, "reference_selection": reference_selection,
                    "suite": suite, "backend": backend, "cases": cases}
        (output_dir / "results.json").write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
        return combined
    if fixture_set == "mandarin":
        reference, target, other, provenance = load_mandarin(fixture_dir, target_speaker)
        voice, scenarios = build_scenarios(target, other, reference=reference)
        inputs = [target, other]
        reference_description = "First three original target utterances for reference; last seven for evaluation; no shared source utterance"
    else:
        inputs = [read_audio(fixture_dir / name) for name in ("target.ogg", "interferer.ogg")]
        voice, scenarios = build_scenarios(*inputs)
        provenance = FIXTURES
        reference_description = "Target source [0,4)s; scored target source [4,12)s (no shared samples)"
    reference_report = None
    if reference_selection == "quality":
        from muter.voice import select_reference
        voice, reference_report = select_reference(voice, SR)
    elif reference_selection == "loudest":
        voice = fit_sample(voice)
    else:
        raise ValueError("Unsupported reference selection")
    if suite == "fidelity":
        scenarios = fidelity_scenarios(scenarios)
    if other_only_seconds:
        n = round(other_only_seconds * SR)
        interference = normalize(np.resize(inputs[1][2 * SR:10 * SR], n))
        zero = np.zeros(n, np.float32)
        scenarios.append({"name": f"other_only_{other_only_seconds:g}s",
                          "description": "Continuous repetition of other speech, target never speaks",
                          "mixture": interference, "target": zero, "other": interference,
                          "noise": zero, "peak_normalization_scale": 1.0})

    if backend == "gtcrn":
        if prime_reference:
            raise ValueError("Reference priming is only an experiment for VoxLock")
        from muter.denoise import GTCRNModel
        base_model = GTCRNModel
    elif backend == "voxlock":
        base_model = Model
    else:
        raise ValueError("Unsupported backend")

    class EvaluationModel(base_model):
        def stream(self, sample):
            stream = super().stream(sample)
            self.primer_stats = {"enabled": False, "processing_seconds": 0.0, "held_primer_samples": 0}
            if prime_reference:
                stream = ReferencePrimedStream(stream, sample)
                self.primer_stats = {"enabled": True, "processing_seconds": stream.prime_seconds,
                                     "held_primer_samples": stream.held_primer_samples}
            return stream

    model = EvaluationModel(precision=precision, threads=2) if backend == "voxlock" else EvaluationModel()
    model.load()
    model.process(scenarios[0]["mixture"][:SR], voice, chunk_ms=chunk_ms)
    summary = {
        "backend": backend, "precision": precision if backend == "voxlock" else "fp32",
        "chunk_ms": chunk_ms, "sample_rate": SR,
        "prime_reference": prime_reference,
        "reference_selection": reference_selection, "reference_report": reference_report,
        "suite": suite,
        "fixture_set": fixture_set, "fixtures": provenance,
        "reference": reference_description,
        "limitations": [f"Two public {'Mandarin read-speech' if fixture_set == 'mandarin' else 'English'} speakers in a synthetic mono mix",
                        "Not evidence of real-room meeting quality or end-to-end latency",
                        "No microphone access and no user voice/recording loaded",
                        "9 dB gain warning is a diagnostic convention, not a universal quality pass/fail",
                        "Energy activity masks are for scoring only, not speech/identity detectors"],
        "scenarios": [],
    }
    if backend == "gtcrn":
        summary["limitations"].append("GTCRN does not use the voice reference or verify speaker identity; this is environmental denoising")
    sf.write(output_dir / "reference-public.wav", voice, SR, subtype="PCM_16")
    for scenario in scenarios:
        name = scenario["name"]
        y, stats = model.process(scenario["mixture"], voice, chunk_ms=chunk_ms)
        stats["priming"] = model.primer_stats
        scene_report = {"name": name, "description": scenario["description"], "processing": stats,
                        "peak_normalization_scale": scenario["peak_normalization_scale"]}
        scene_report.update(score_scene(y, scenario["mixture"], scenario["target"], scenario["other"], scenario["noise"]))
        if name.startswith("other_only_"):
            windows = []
            for start in range(0, len(y), 10 * SR):
                end = min(start + 10 * SR, len(y))
                active = active_mask(scenario["other"][start:end])
                if active.any():
                    window = {"start_seconds": start / SR, "end_seconds": end / SR}
                    window.update(leakage_metrics(y[start:end][active], scenario["mixture"][start:end][active]))
                    windows.append(window)
            scene_report["leakage_by_10s_window"] = windows
        dest = output_dir / name
        dest.mkdir(exist_ok=True)
        for filename, audio in (("input.wav", scenario["mixture"]), ("output.wav", y), ("target-clean.wav", scenario["target"])):
            sf.write(dest / filename, audio, SR, subtype="PCM_16")
        summary["scenarios"].append(scene_report)
        print(json.dumps(scene_report, ensure_ascii=False), flush=True)
    (output_dir / "results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--precision", choices=("int8", "fp32"), default="int8")
    parser.add_argument("--chunk-ms", type=int, choices=(20, 40), default=40)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--fixtures-dir", type=Path)
    parser.add_argument("--fixture-set", choices=("english", "mandarin"), default="english")
    parser.add_argument("--reference-selection", choices=("loudest", "quality"), default="loudest")
    parser.add_argument("--suite", choices=("speakers", "fidelity"), default="speakers")
    parser.add_argument("--backend", choices=("voxlock", "gtcrn", "both"), default="voxlock")
    parser.add_argument("--pvad-check", action="store_true", help="Evaluate detector probabilities instead of audio enhancement")
    parser.add_argument("--pvad-threads", type=int, default=4)
    parser.add_argument("--reference-switch-check", action="store_true")
    parser.add_argument("--prime-reference", action="store_true",
                        help="Experimental: prime recurrent state with clean reference; discard primer/tail")
    parser.add_argument("--other-only-seconds", type=float, default=0,
                        help="Add a target-absent repetition test, e.g. 60")
    args = parser.parse_args()
    if args.other_only_seconds < 0:
        parser.error("--other-only-seconds must be non-negative")
    if args.pvad_check:
        evaluate_pvad(args.fixture_set, args.output_dir, args.fixtures_dir, threads=args.pvad_threads)
        return
    if args.reference_switch_check:
        reference_switch_check(args.output_dir, args.fixtures_dir, args.precision)
        return
    evaluate(args.precision, args.chunk_ms, args.output_dir, args.fixtures_dir,
             args.prime_reference, args.other_only_seconds, args.fixture_set,
             reference_selection=args.reference_selection, suite=args.suite, backend=args.backend)


if __name__ == "__main__":
    main()
