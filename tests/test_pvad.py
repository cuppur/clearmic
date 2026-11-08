"""Tests for the real local speaker detector; no microphone access."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np

from muter.pvad import Guard, fbank, MODEL_DIR, CHUNK_SAMPLES


class FeatureTests(unittest.TestCase):
    def test_kaldi_frame_geometry_and_mean(self):
        rng = np.random.default_rng(3)
        for samples, frames in [(2560, 14), (5120, 30), (38400, 238)]:
            features = fbank(rng.normal(0, .1, samples).astype(np.float32))
            self.assertEqual(features.shape, (frames, 80))
            self.assertEqual(features.dtype, np.float32)
            self.assertTrue(np.isfinite(features).all())
            self.assertLess(float(np.max(np.abs(features.mean(0)))), 1e-4)

    def test_invalid_features_are_rejected(self):
        for audio in [np.zeros(399), np.zeros((400, 2)), np.full(2560, np.nan)]:
            with self.assertRaises(ValueError):
                fbank(audio)

    def test_module_import_does_not_import_torch(self):
        code = "import sys; import muter.pvad; assert 'torch' not in sys.modules; assert 'modelscope' not in sys.modules"
        subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent, check=True,
                       timeout=15, capture_output=True)


@unittest.skipUnless((MODEL_DIR / "detector.onnx").exists(), "Local exported PVAD models missing")
class GuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.voice = np.random.default_rng(4).normal(0, .05, 48000).astype(np.float32)
        cls.models = Guard.load_models()

    def test_models_are_shared_but_session_history_is_independent(self):
        first, second = Guard(self.voice), Guard(self.voice)
        self.assertIs(first.models, second.models)
        p = first.push(self.voice[:CHUNK_SAMPLES])
        self.assertGreaterEqual(p, 0)
        self.assertLessEqual(p, 1)
        self.assertEqual(len(second._buf), 0)
        first.reset()
        self.assertEqual(len(first._buf), 0)

    def test_fixed_chunk_size_and_nonfinite_input(self):
        guard = Guard(self.voice)
        for x in [np.zeros(640), np.full(2560, np.nan), np.zeros((2560, 2))]:
            with self.assertRaises(ValueError):
                guard.push(x)

    def test_reference_duration_and_silence(self):
        for voice in [np.zeros(48000), np.ones(47999, np.float32), np.full(48000, np.nan)]:
            with self.assertRaises(ValueError):
                Guard(voice)

    def test_missing_weights_fail_without_network_fallback(self):
        with tempfile.TemporaryDirectory() as path:
            with self.assertRaises(FileNotFoundError):
                Guard.load_models(path)

    def test_bad_thread_count(self):
        for count in [True, 0, -1, 1.5]:
            with self.assertRaises(ValueError):
                Guard.load_models(threads=count)


if __name__ == "__main__":
    unittest.main()
