"""Download pinned public weights; no microphone access or user-data upload."""
import hashlib
import json
from pathlib import Path

from huggingface_hub import hf_hub_download
from voxlock.core import REVISION

root = Path(__file__).resolve().parent.parent
dest = root / "models" / "voxlock"
manifest = {"repository": "arbazsiddiqui/voxlock", "revision": REVISION, "files": {}}
for name in ["onnx/v2/enroll.onnx", "onnx/v2/chunk_int8.onnx", "onnx/v2/chunk.onnx", "README.md"]:
    path = Path(hf_hub_download("arbazsiddiqui/voxlock", name, revision=REVISION, local_dir=dest))
    manifest["files"][name] = {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    print(name, flush=True)
(dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
