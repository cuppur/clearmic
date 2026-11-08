# Third-party components

ClearMic uses the following upstream projects. Original copyright,
license, attribution and notices remain applicable. The packaged `licenses/`
directory contains discovered license/notice files from the build environment;
`build-environment.txt` records installed distribution versions.

- VoxLock 1.3.0, Arbaz Siddiqui: https://github.com/arbazsiddiqui/voxlock
  Code: Apache-2.0. Released model card declares Apache-2.0.
  Model source: https://huggingface.co/arbazsiddiqui/voxlock
  Revision: 2413b292bc2c337bfc19a9802aaf3e011e68e9c4.
  The model manifest records the exact bundled ONNX files and SHA256 hashes.
  VoxLock builds on REAL-TSE/WeSep and WeSpeaker; see its model card for provenance.
- ONNX Runtime: https://github.com/microsoft/onnxruntime (MIT).
- NumPy: https://github.com/numpy/numpy (BSD-3-Clause and bundled component notices).
- python-sounddevice / PortAudio: https://github.com/spatialaudio/python-sounddevice
  and https://github.com/PortAudio/portaudio (MIT-style licenses).
- SoundFile / libsndfile: https://github.com/bastibe/python-soundfile
  and https://github.com/libsndfile/libsndfile (BSD and LGPL component terms).
  The soundfile binary wheel's bundled library and notices are retained.
- python-soxr / libsoxr: https://github.com/dofuuz/python-soxr
  and https://sourceforge.net/projects/soxr/ (BSD and LGPL component terms).
- CPython and Tcl/Tk runtime: Python Software Foundation and upstream Tcl/Tk licenses.
- pywin32: https://github.com/mhammond/pywin32 (see bundled license).
- PyInstaller bootloader: https://github.com/pyinstaller/pyinstaller
  (GPL with the upstream distribution exception).

The installer includes the original, unmodified base VB-CABLE package from
VB-Audio Software (https://vb-audio.com/Cable/). VB-CABLE is Donationware, and
all contributions are welcome. Distribution and professional deployment terms
are published at https://vb-audio.com/Services/licensing.htm .

Development validation used two public LibriSpeech excerpts distributed by
librosa: https://librosa.org/data/audio/5703-47212-0000.ogg and
https://librosa.org/data/audio/3436-172162-0000.ogg . These fixtures and derived
validation audio are not included in the portable application.

Speech enhancement components:

- NomoPVAD, Tuya: https://github.com/tuya/nomo-pvad (Apache-2.0).
  ERes2NetV2 model originates from ModelScope/3D-Speaker. Models were exported
  from official weights; exact downloaded sources and hashes are in
  models/pvad/manifest.json, with WEIGHTS_LICENSE.md and THIRD_PARTY_NOTICES.md.
- GTCRN, Xiaobin Rong and contributors: https://github.com/Xiaobin-Rong/gtcrn
  (MIT). Runtime-compatible export from sherpa-onnx official speech enhancement
  model release; exact checksum and license are in models/gtcrn.
- sherpa-onnx: https://github.com/k2-fsa/sherpa-onnx (Apache-2.0).
- kaldi-native-fbank: https://github.com/csukuangfj/kaldi-native-fbank (Apache-2.0).

English and AISHELL-3 Mandarin evaluation assets are not included in the
portable application. Mandarin source licensing and fixed revisions are in
artifacts/validation/chinese/SOURCES.json and the upstream dataset card.
