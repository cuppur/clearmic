"""Build a self-contained Windows folder; never includes user recordings."""
from importlib import metadata
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from muter import __version__
dist_root = root / "dist" / ("v" + __version__)
subprocess.run([
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--onedir",
    "--name", "Muter", "--collect-all", "voxlock", "--copy-metadata", "onnxruntime",
    "--icon", str(root / "muter/assets/muter.ico"),
    "--add-data", f"{root / 'muter/assets/muter.png'}:muter/assets",
    "--add-data", f"{root / 'muter/assets/muter.ico'}:muter/assets",
    "--copy-metadata", "sounddevice", "--copy-metadata", "sherpa-onnx",
    "--copy-metadata", "soxr", "--copy-metadata", "pycaw",
    "--exclude-module", "torch", "--exclude-module", "huggingface_hub",
    "--exclude-module", "pytest", "--exclude-module", "sympy", "run_muter.py",
    "--exclude-module", "torchaudio", "--exclude-module", "onnx", "--exclude-module", "modelscope",
    "--collect-all", "sherpa_onnx", "--collect-all", "kaldi_native_fbank", "--distpath", str(dist_root),
], cwd=root, check=True)
dest = dist_root / "Muter"
model_dest = dest / "models" / "voxlock"
for name in ["onnx/v2/enroll.onnx", "onnx/v2/chunk_int8.onnx", "onnx/v2/chunk.onnx", "manifest.json", "README.md"]:
    src = root / "models" / "voxlock" / name
    target = model_dest / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
for component, filenames in {
    "gtcrn": ["gtcrn_simple.onnx", "manifest.json", "LICENSE-MIT.txt", "UPSTREAM_CHECKSUM.txt"],
    "pvad": ["enroll.onnx", "detector.onnx", "manifest.json", "LICENSE-APACHE-2.0.txt", "THIRD_PARTY_NOTICES.md", "WEIGHTS_LICENSE.md"],
}.items():
    for name in filenames:
        target = dest / "models" / component / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / "models" / component / name, target)
for name in ["README.md", "THIRD_PARTY_NOTICES.md"]:
    shutil.copy2(root / name, dest / name)
licenses = dest / "licenses"
licenses.mkdir(exist_ok=True)
inventory = []
for dist in metadata.distributions():
    package = dist.metadata["Name"]
    inventory.append(f"{package}=={dist.version}")
    for entry in dist.files or []:
        if any(word in entry.name.lower() for word in ("license", "copying", "notice", "copyright")):
            source = Path(dist.locate_file(entry))
            if source.is_file() and source.suffix.lower() not in (".py", ".pyc", ".dll", ".pyd"):
                target = licenses / package / str(entry).replace("..", "_").replace(":", "_")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.exists():
    shutil.copy2(python_license, licenses / "PYTHON_LICENSE.txt")
(dest / "build-environment.txt").write_text("\n".join(sorted(inventory)), encoding="utf-8")
print(f"Ready: {dest / 'Muter.exe'}", flush=True)
