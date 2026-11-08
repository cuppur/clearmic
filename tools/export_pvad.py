"""Build-only exporter for official NomoPVAD + ERes2NetV2 weights.

Runtime needs ONNX Runtime and kaldi-native-fbank, not PyTorch or ModelScope.
Official source and weight URLs and file hashes are recorded beside the exports.
"""
from pathlib import Path
import ast
import hashlib
import json
import math
import sys
import types
import urllib.request

import numpy as np
import onnxruntime as ort
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchaudio.compliance.kaldi as kaldi

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "models" / "pvad"
SOURCE = DEST / "source"
DEST.mkdir(parents=True, exist_ok=True)


def fetch(url, name):
    path = DEST / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        print("Downloading", url, flush=True)
        temp = path.with_suffix(path.suffix + ".download")
        with urllib.request.urlopen(url, timeout=120) as response, temp.open("wb") as out:
            while block := response.read(1024 * 1024):
                out.write(block)
        temp.replace(path)
    return path


URLS = {}
for filename in ["core/__init__.py", "core/model.py", "core/eres2netv2_backbone.py", "core/film.py", "core/frame_adapter.py", "nomo_pvad.py", "LICENSE", "THIRD_PARTY_NOTICES.md", "weights/WEIGHTS_LICENSE.md"]:
    url = "https://raw.githubusercontent.com/tuya/nomo-pvad/master/" + filename
    name = "source/" + filename
    URLS[name] = url
    fetch(url, name)
for filename in ["ERes2NetV2.py", "fusion.py", "pooling_layers.py"]:
    url = "https://raw.githubusercontent.com/modelscope/modelscope/master/modelscope/models/audio/sv/" + filename
    name = "source/modelscope/" + filename
    URLS[name] = url
    fetch(url, name)
URLS["nomo_pvad.pt"] = "https://raw.githubusercontent.com/tuya/nomo-pvad/master/weights/nomo_pvad.pt"
URLS["pretrained_eres2netv2.ckpt"] = "https://modelscope.cn/models/iic/speech_eres2netv2_sv_zh-cn_16k-common/resolve/master/pretrained_eres2netv2.ckpt"
for name in ["nomo_pvad.pt", "pretrained_eres2netv2.ckpt"]:
    fetch(URLS[name], name)


def classes(path, names, namespace):
    # Execute only the reviewed network class definitions, avoiding the unrelated
    # ModelScope service registration and pipeline framework dependencies.
    tree = ast.parse(path.read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in names]
    if len(selected) != len(names):
        raise RuntimeError(f"Unexpected upstream source: {path}")
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)


torch.set_num_threads(2)
env = {"torch": torch, "nn": nn, "F": F, "math": math}
classes(SOURCE / "modelscope" / "fusion.py", {"AFF"}, env)
classes(SOURCE / "modelscope" / "pooling_layers.py", {"TSTP"}, env)
env["pooling_layers"] = types.SimpleNamespace(TSTP=env["TSTP"])
classes(SOURCE / "modelscope" / "ERes2NetV2.py", {"ReLU", "BasicBlockERes2NetV2", "BasicBlockERes2NetV2AFF", "ERes2NetV2"}, env)
for name in ["modelscope", "modelscope.models", "modelscope.models.audio", "modelscope.models.audio.sv", "modelscope.models.audio.sv.ERes2NetV2"]:
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
sys.modules["modelscope.models.audio.sv.ERes2NetV2"].ERes2NetV2 = env["ERes2NetV2"]
sys.path.insert(0, str(SOURCE))
from core.model import build_model

checkpoint = torch.load(DEST / "nomo_pvad.pt", map_location="cpu", weights_only=True)
cfg = checkpoint.get("model_cfg", {})
cfg["eres2netv2_model_dir"] = str(DEST)
detector = build_model(cfg).eval()
loaded = detector.load_state_dict(checkpoint.get("model_state_dict", checkpoint), strict=False)
if loaded.missing_keys or loaded.unexpected_keys:
    raise RuntimeError(f"NomoPVAD state mismatch: {loaded}")
encoder = env["ERes2NetV2"](feat_dim=80, embed_dim=192, baseWidth=26, scale=2, expansion=2).eval()
encoder.load_state_dict(torch.load(DEST / "pretrained_eres2netv2.ckpt", map_location="cpu", weights_only=True), strict=True)


class Enrollment(nn.Module):
    def __init__(self, net):
        super().__init__()
        self.net = net

    def forward(self, fbank):
        return F.normalize(self.net(fbank).float(), dim=-1)


class Detector(nn.Module):
    def __init__(self, net):
        super().__init__()
        self.net = net

    def forward(self, speaker, fbank):
        lens = torch.ones(1, dtype=torch.int64, device=fbank.device) * fbank.shape[1]
        result = self.net(speaker, fbank, lens)
        return F.softmax(result["chunk_logits"], dim=-1)[:, :, 1]


enroll_wrapper = Enrollment(encoder).eval()
detect_wrapper = Detector(detector).eval()
with torch.no_grad():
    features = torch.randn(1, 238, 80)
    speaker = F.normalize(torch.randn(1, 192), dim=-1)
    torch.onnx.export(enroll_wrapper, (features,), str(DEST / "enroll.onnx"),
        input_names=["fbank"], output_names=["speaker"],
        dynamic_axes={"fbank": {1: "frames"}}, opset_version=17, dynamo=False)
    torch.onnx.export(detect_wrapper, (speaker, features), str(DEST / "detector.onnx"),
        input_names=["speaker", "fbank"], output_names=["target_probabilities"],
        dynamic_axes={"fbank": {1: "frames"}, "target_probabilities": {1: "chunks"}},
        opset_version=17, dynamo=False)

options = ort.SessionOptions()
options.intra_op_num_threads = 2
enroll_ort = ort.InferenceSession(str(DEST / "enroll.onnx"), options, providers=["CPUExecutionProvider"])
detect_ort = ort.InferenceSession(str(DEST / "detector.onnx"), options, providers=["CPUExecutionProvider"])
checks = []
for frames in [14, 30, 78, 158, 238]:
    with torch.no_grad():
        x = torch.randn(1, frames, 80)
        expected = detect_wrapper(speaker, x).numpy()
    actual = detect_ort.run(None, {"speaker": speaker.numpy(), "fbank": x.numpy()})[0]
    delta = float(np.max(np.abs(expected - actual)))
    if delta > 2e-4:
        raise RuntimeError(f"ONNX detector mismatch frames={frames}: {delta}")
    checks.append({"frames": frames, "max_error": delta})
with torch.no_grad():
    expected = enroll_wrapper(features).numpy()
actual = enroll_ort.run(None, {"fbank": features.numpy()})[0]
if not np.allclose(expected, actual, atol=2e-4):
    raise RuntimeError("ONNX enrollment mismatch")

manifest = {"model": "tuya/nomo-pvad release-1.1", "runtime": "ONNX Runtime, kaldi-native-fbank", "onnx_parity": checks, "sources": {}}
sys.path.insert(0, str(ROOT))
from muter.pvad import fbank as runtime_fbank
import soundfile as sf
import soxr
feature_checks = []
for fixture in [ROOT / "artifacts/validation/target.ogg", ROOT / "artifacts/validation/chinese/SSB0016-clean.wav"]:
    if not fixture.is_file():
        continue
    audio, rate = sf.read(fixture, dtype="float32")
    audio = soxr.resample(audio, rate, 16000).astype(np.float32)
    def torch_features(x):
        feat = kaldi.fbank(torch.from_numpy(x * 32768.0)[None], num_mel_bins=80,
                           frame_length=25, frame_shift=10, sample_frequency=16000, dither=0.0)
        return feat - feat.mean(dim=0, keepdim=True)
    reference, incoming = audio[:48000], audio[48000:48000 + 38400]
    ref_t, in_t = torch_features(reference), torch_features(incoming)
    ref_n, in_n = runtime_fbank(reference), runtime_fbank(incoming)
    with torch.no_grad():
        speaker_t = enroll_wrapper(ref_t[None])
        prob_t = detect_wrapper(speaker_t, in_t[None]).numpy()
    speaker_n = enroll_ort.run(None, {"fbank": ref_n[None]})[0]
    prob_n = detect_ort.run(None, {"speaker": speaker_n, "fbank": in_n[None]})[0]
    speaker_error = float(np.max(np.abs(speaker_n - speaker_t.numpy())))
    probability_error = float(np.max(np.abs(prob_n - prob_t)))
    if speaker_error > .001 or probability_error > .001:
        raise RuntimeError(f"Runtime feature parity failed {fixture}: {speaker_error}/{probability_error}")
    feature_checks.append({"fixture": str(fixture.relative_to(ROOT)),
        "fbank_max_error": float(np.max(np.abs(ref_n - ref_t.numpy()))),
        "fbank_mean_error": float(np.mean(np.abs(ref_n - ref_t.numpy()))),
        "speaker_max_error": speaker_error, "probability_max_error": probability_error})
manifest["runtime_feature_parity"] = feature_checks
for name, url in URLS.items():
    path = DEST / name
    manifest["sources"][name] = {"url": url, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
for name in ["enroll.onnx", "detector.onnx"]:
    path = DEST / name
    manifest[name] = {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
(DEST / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
print(json.dumps({"exported": str(DEST), "parity": checks}, indent=2), flush=True)
