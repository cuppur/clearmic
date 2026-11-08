from importlib.util import find_spec
from pathlib import Path
import threading
import unittest

import numpy as np

from muter.denoise import GTCRNModel, GTCRNStream, MODEL_PATH


class FakeResult:
    def __init__(self, samples):
        self.samples = samples


class DenoiseContractTests(unittest.TestCase):
    def test_missing_model_is_recoverable(self):
        with self.assertRaises(FileNotFoundError):
            GTCRNModel(model_path=Path("missing-gtcrn-model.onnx")).load()

    def test_cancelled_file_job_does_not_load_model(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(InterruptedError):
            GTCRNModel(model_path="not-present.onnx").process(np.ones(100, np.float32), cancel=cancel)

    def test_invalid_audio_does_not_load_model(self):
        model = GTCRNModel(model_path="not-present.onnx")
        for x in ([], np.zeros((4, 2)), [np.nan], [np.inf]):
            with self.subTest(shape=np.shape(x)), self.assertRaises(ValueError):
                model.process(x)
        with self.assertRaises(ValueError):
            model.process([1], chunk_ms=1)

    def test_incorrect_native_output_is_an_error(self):
        class BadEngine:
            def __call__(self, x, sr):
                return FakeResult(np.full(len(x), np.nan, np.float32))
        with self.assertRaises(RuntimeError):
            GTCRNStream(BadEngine()).push(np.ones(640, np.float32))

    def test_flush_requires_complete_sample_count(self):
        class ShortEngine:
            def __call__(self, x, sr):
                return FakeResult(np.empty(0, np.float32))
            def flush(self):
                return FakeResult(np.empty(0, np.float32))
        stream = GTCRNStream(ShortEngine())
        stream.push(np.ones(640, np.float32))
        with self.assertRaises(RuntimeError):
            stream.flush()

    def test_stream_clips_without_returning_raw_on_error(self):
        class LoudEngine:
            def __call__(self, x, sr):
                return FakeResult(np.ones(len(x), np.float32) * 2)
            def flush(self):
                return FakeResult(np.empty(0, np.float32))
        stream = GTCRNStream(LoudEngine())
        self.assertLessEqual(float(np.max(stream.push(np.zeros(500, np.float32)))), .980001)
        self.assertEqual(len(stream.flush()), 0)
        self.assertEqual(len(stream.flush()), 0)
        with self.assertRaises(RuntimeError):
            stream.push(np.zeros(1, np.float32))


@unittest.skipUnless(find_spec("sherpa_onnx") and MODEL_PATH.exists(), "Optional GTCRN model/runtime absent")
class RealDenoiseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = GTCRNModel().load()
        rng = np.random.default_rng(32)
        t = np.arange(16013) / 16000
        cls.x = (.05 * np.sin(2 * np.pi * 190 * t) + rng.normal(0, .003, len(t))).astype(np.float32)

    def test_output_has_exact_length_for_partial_frame_and_tiny_inputs(self):
        for n in (1, 255, 256, 257, 640, 16013):
            with self.subTest(n=n):
                out, stats = self.model.process(self.x[:n])
                self.assertEqual(out.dtype, np.float32)
                self.assertEqual(len(out), n)
                self.assertTrue(np.isfinite(out).all())
                self.assertLessEqual(float(np.max(np.abs(out))), .980001)
                self.assertEqual(stats["backend"], "gtcrn")

    def test_chunk_boundaries_and_reference_do_not_change_result(self):
        a, _ = self.model.process(self.x, voice=None, chunk_ms=20)
        b, _ = self.model.process(self.x, voice=np.zeros(48000), chunk_ms=40)
        np.testing.assert_allclose(a, b, atol=2e-6, rtol=2e-6)

    def test_native_streams_have_independent_states(self):
        first = self.model.stream()
        first.push(self.x[:640])
        second = self.model.stream()
        a = np.concatenate([second.push(self.x), second.flush()])
        b, _ = self.model.process(self.x)
        np.testing.assert_allclose(a, b, atol=2e-6, rtol=2e-6)


if __name__ == "__main__":
    unittest.main()
