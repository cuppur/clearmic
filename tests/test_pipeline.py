"""Probability-gate alignment tests; fake models only, no audio devices."""
import threading
import unittest

import numpy as np

from muter.pipeline import BLOCK, GatedStream, GuardedModel


class DelayedEnhancer:
    """Identity waveform with a known 24-sample streaming delay."""
    def __init__(self, delay=24):
        self.delay = delay
        self.pending = np.zeros(0, np.float32)
        self.flush_calls = 0
        self.push_calls = 0

    def push(self, audio):
        self.push_calls += 1
        self.pending = np.concatenate((self.pending, np.asarray(audio, np.float32)))
        count = max(0, len(self.pending) - self.delay)
        result, self.pending = self.pending[:count].copy(), self.pending[count:]
        return result

    def flush(self):
        self.flush_calls += 1
        result, self.pending = self.pending.copy(), np.zeros(0, np.float32)
        return result


class SequenceGuard:
    def __init__(self, probabilities):
        self.values = iter(probabilities)
        self.inputs = []

    def push(self, audio):
        self.inputs.append(np.asarray(audio).copy())
        return next(self.values)


class FakeModel:
    requires_voice = False
    precision = "fp32"
    backend = "fake-delayed"

    def __init__(self):
        self.load_calls = 0
        self.stream_calls = 0
        self.enhancer = None

    def load(self):
        self.load_calls += 1
        return self

    def stream(self, voice):
        self.stream_calls += 1
        self.enhancer = DelayedEnhancer()
        return self.enhancer


def signal(count):
    # Keep values below clipping while preserving identifiable sample positions.
    return (.15 + .1 * np.sin(np.arange(count, dtype=np.float64) * .017)).astype(np.float32)


class GateAlignmentTests(unittest.TestCase):
    def test_soft_gate_holds_one_block_and_preserves_onset_and_partial_tail(self):
        audio = signal(2 * BLOCK + 83)
        stream = GatedStream(DelayedEnhancer(), SequenceGuard([.1, .9, .1]), smooth=True)
        self.assertEqual(len(stream.push(audio[:BLOCK])), 0)
        emitted = stream.push(audio[BLOCK:])
        tail = stream.flush()
        np.testing.assert_array_equal(np.concatenate((emitted, tail)), audio)
        self.assertEqual(len(stream.flush()), 0)

    def test_soft_gate_attenuates_only_the_aligned_low_confidence_audio(self):
        audio = signal(5 * BLOCK)
        stream = GatedStream(DelayedEnhancer(), SequenceGuard([.1, .1, .1, .1, .9]), smooth=True)
        output = np.concatenate((stream.push(audio), stream.flush()))
        np.testing.assert_allclose(output[:3 * BLOCK], .10 * audio[:3 * BLOCK])
        np.testing.assert_array_equal(output[3 * BLOCK + 192:], audio[3 * BLOCK + 192:])

    def test_delayed_output_uses_original_sample_permissions(self):
        audio = signal(3 * BLOCK)
        enhancer = DelayedEnhancer()
        guard = SequenceGuard([.9, .1, .9])
        stream = GatedStream(enhancer, guard)
        output = []
        for start in range(0, len(audio), 640):
            output.append(stream.push(audio[start:start + 640]))
        output.append(stream.flush())
        actual = np.concatenate(output)
        expected = audio.copy()
        expected[BLOCK:2 * BLOCK] = 0
        np.testing.assert_array_equal(actual, expected)
        self.assertEqual(enhancer.flush_calls, 1)

    def test_first_rejected_block_cannot_leak_into_accepted_block(self):
        audio = signal(BLOCK * 2)
        stream = GatedStream(DelayedEnhancer(), SequenceGuard([0, 1]))
        actual = np.concatenate((stream.push(audio), stream.flush()))
        np.testing.assert_array_equal(actual[:BLOCK], np.zeros(BLOCK))
        np.testing.assert_array_equal(actual[BLOCK:], audio[BLOCK:])

    def test_partial_flush_pads_guard_only_and_keeps_exact_length(self):
        audio = signal(BLOCK + 137)
        guard = SequenceGuard([1, 1])
        enhancer = DelayedEnhancer()
        stream = GatedStream(enhancer, guard)
        output = [stream.push(audio[:17]), stream.push(audio[17:]), stream.flush()]
        np.testing.assert_array_equal(np.concatenate(output), audio)
        self.assertEqual(len(guard.inputs), 2)
        self.assertTrue(all(len(block) == BLOCK for block in guard.inputs))
        np.testing.assert_array_equal(guard.inputs[-1][:137], audio[-137:])
        np.testing.assert_array_equal(guard.inputs[-1][137:], np.zeros(BLOCK - 137))
        self.assertEqual(enhancer.flush_calls, 1)

    def test_audio_shorter_than_enhancer_delay_is_preserved_by_flush(self):
        audio = signal(7)
        stream = GatedStream(DelayedEnhancer(), SequenceGuard([1]))
        self.assertEqual(len(stream.push(audio)), 0)
        np.testing.assert_array_equal(stream.flush(), audio)

    def test_repeated_flush_does_not_repeat_tail_or_guard_inference(self):
        audio = signal(83)
        guard = SequenceGuard([1])
        enhancer = DelayedEnhancer()
        stream = GatedStream(enhancer, guard)
        stream.push(audio)
        np.testing.assert_array_equal(stream.flush(), audio)
        self.assertEqual(len(stream.flush()), 0)
        self.assertEqual(len(guard.inputs), 1)
        self.assertEqual(enhancer.flush_calls, 1)
        with self.assertRaises(RuntimeError):
            stream.push(signal(1))

    def test_empty_stream_flush_has_no_output(self):
        enhancer = DelayedEnhancer()
        guard = SequenceGuard([])
        stream = GatedStream(enhancer, guard)
        self.assertEqual(len(stream.push(np.zeros(0, np.float32))), 0)
        self.assertEqual(len(stream.flush()), 0)
        self.assertEqual(guard.inputs, [])

    def test_invalid_probabilities_fail_before_enhancer_receives_audio(self):
        for probability in [float("nan"), float("inf"), -.001, 1.001]:
            with self.subTest(probability=probability):
                enhancer = DelayedEnhancer()
                stream = GatedStream(enhancer, SequenceGuard([probability]))
                with self.assertRaises(RuntimeError):
                    stream.push(signal(BLOCK))
                self.assertEqual(enhancer.push_calls, 0)

    def test_invalid_input_fails_before_guard(self):
        for audio in [np.array([np.nan], np.float32), np.ones((10, 2), np.float32)]:
            guard = SequenceGuard([])
            stream = GatedStream(DelayedEnhancer(), guard)
            with self.assertRaises(ValueError):
                stream.push(audio)
            self.assertEqual(guard.inputs, [])


class GuardedFileTests(unittest.TestCase):
    def test_file_processing_keeps_length_and_rejection_mask(self):
        audio = signal(BLOCK * 2 + 11)
        guard = SequenceGuard([1, 0, 1])
        model = GuardedModel(FakeModel(), guard_factory=lambda voice: guard)
        output, stats = model.process(audio, np.zeros(48000, np.float32), chunk_ms=20)
        expected = audio.copy()
        expected[BLOCK:2 * BLOCK] = 0
        np.testing.assert_array_equal(output, expected)
        self.assertEqual(stats["guard_frame_ms"], 160)
        self.assertAlmostEqual(stats["guard_rejected_fraction"], 1 / 3)

    def test_already_cancelled_job_does_not_load_or_create_models(self):
        base = FakeModel()
        cancel = threading.Event()
        cancel.set()
        model = GuardedModel(base, guard_factory=lambda voice: SequenceGuard([1]))
        with self.assertRaises(InterruptedError):
            model.process(signal(BLOCK), None, cancel=cancel)
        self.assertEqual(base.load_calls, 0)
        self.assertEqual(base.stream_calls, 0)

    def test_cancel_on_last_progress_callback_skips_flush(self):
        base = FakeModel()
        cancel = threading.Event()
        model = GuardedModel(base, guard_factory=lambda voice: SequenceGuard([1]))
        with self.assertRaises(InterruptedError):
            model.process(signal(640), None, cancel=cancel, progress=lambda _: cancel.set())
        self.assertEqual(base.enhancer.flush_calls, 0)


if __name__ == "__main__":
    unittest.main()
