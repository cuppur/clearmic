import unittest
import numpy as np
from muter.realtime import AudioBuffer, ClockController, LiveSettings, PermissionTimeline


class RealtimeTests(unittest.TestCase):
    def test_forbidden_audio_never_enters_resampler_history(self):
        import queue
        import time
        from unittest.mock import Mock, patch
        from muter.audio import LiveAudio
        model = Mock()
        live = LiveAudio(model, np.ones(48000, np.float32), 0, 1, queue.Queue())
        seen = []
        class Stream:
            def push(self, audio):
                if len(audio) == 3200:  # runtime-kernel warmup
                    return np.zeros(0, np.float32)
                seen.append(audio.copy())
                live.cancel.set()
                return np.zeros(len(audio), np.float32)
        model.stream.side_effect = lambda _: Stream()
        for i in range(10):
            # Prior to a key press the room contains a strong signal. After
            # the press input is zero. No older signal may escape via soxr.
            live.input_queue.put((time.monotonic(), np.full(480, .5 if i < 2 else 0, np.float32), i >= 2))
        with patch("muter.audio.stream_config", side_effect=[(48000, 1), (16000, 1)]), \
             patch("muter.audio.sd.OutputStream") as output, patch("muter.audio.sd.InputStream") as source:
            output.return_value.__enter__.return_value.active = True
            source.return_value.__enter__.return_value.active = True
            live._run()
        self.assertIsNone(live.error)
        self.assertTrue(seen)
        self.assertLess(float(np.max(np.abs(np.concatenate(seen)))), 1e-6)

    def test_capture_permission_survives_model_delay_and_release(self):
        p = PermissionTimeline()
        p.append(640, False)
        p.append(640, True)
        first = p.apply(np.ones(400, np.float32))
        np.testing.assert_array_equal(first, np.zeros(400))
        second = p.apply(np.ones(500, np.float32))
        np.testing.assert_array_equal(second[:240], np.zeros(240))
        np.testing.assert_array_equal(second[240:], np.ones(260))
        p.append(640, False)
        tail = p.apply(np.ones(1020, np.float32))
        np.testing.assert_array_equal(tail[:380], np.ones(380))
        np.testing.assert_array_equal(tail[380:], np.zeros(640))

    def test_ring_wrap_and_stereo_render(self):
        b = AudioBuffer(8)
        b.push(np.arange(6, dtype=np.float32))
        np.testing.assert_array_equal(b.pop(5)[0], np.arange(5))
        b.push(np.arange(6, 12, dtype=np.float32))
        out = np.ones((8, 2), np.float32)
        missing = b.pop_into(out)
        self.assertEqual(missing, 1)
        np.testing.assert_array_equal(out[:, 0], [5, 6, 7, 8, 9, 10, 11, 0])
        np.testing.assert_array_equal(out[:, 0], out[:, 1])
        self.assertEqual(b.size, 0)

    def test_clock_corrects_both_directions_for_simulated_30_minutes(self):
        for drift in [-.0005, -.0001, .0001, .0005]:
            c = ClockController()
            level = .1
            for i in range(45000):
                adjustment = c.update(i * .04, level)
                level += (drift - adjustment) * .04
                self.assertGreater(level, .06)
                self.assertLess(level, .14)
            self.assertAlmostEqual(adjustment, drift, delta=.00002)

    def test_clock_ignores_packet_jitter(self):
        c = ClockController()
        ratios = [c.update(i * .04, .1 + (.02 if i % 2 else -.02)) for i in range(5000)]
        self.assertLess(abs(ratios[-1]), .00015)

    def test_settings_reject_unbounded_latency_or_tiny_chunks(self):
        with self.assertRaises(ValueError):
            LiveSettings(buffer_ms=1000)
        with self.assertRaises(ValueError):
            LiveSettings(chunk_ms=1)

    def test_missing_partial_block_is_cleared_after_failure(self):
        import queue
        from unittest.mock import Mock
        from muter.audio import LiveAudio
        live = LiveAudio(Mock(), np.zeros(48000, np.float32), 0, 1, queue.Queue())
        live.output_rate = 16000
        live.buffer = AudioBuffer(640)
        live.buffer.push(np.ones(20, np.float32))
        live.ready = True
        live.starved_frames = 7999
        out = np.ones((320, 2), np.float32)
        live._output(out, 320, None, False)
        self.assertTrue(live.cancel.is_set())
        self.assertTrue(np.all(out == 0))


if __name__ == "__main__":
    unittest.main()
