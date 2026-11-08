import queue
import unittest
from unittest.mock import Mock

import numpy as np

from muter.audio import AudioBuffer, LiveAudio


class AudioSafetyTests(unittest.TestCase):
    def test_queue_preserves_samples_and_zero_pads_gaps(self):
        buf = AudioBuffer(20)
        buf.push(np.array([1, 2, 3], np.float32))
        buf.push(np.array([4, 5], np.float32))
        first, missing = buf.pop(4)
        np.testing.assert_array_equal(first, [1, 2, 3, 4])
        self.assertEqual(missing, 0)
        second, missing = buf.pop(3)
        np.testing.assert_array_equal(second, [5, 0, 0])
        self.assertEqual(missing, 2)

    def test_queue_cannot_accumulate_unbounded_delay(self):
        buf = AudioBuffer(4)
        buf.push(np.ones(3, np.float32))
        with self.assertRaises(RuntimeError):
            buf.push(np.ones(2, np.float32))
        self.assertEqual(buf.size, 3)

    def live(self):
        live = LiveAudio(Mock(), np.ones(48000, np.float32), 0, 1, queue.Queue())
        live.output_rate = 16000
        live.buffer = AudioBuffer(16000)
        live.ready = True
        live.prefill = 0
        return live

    def test_muted_and_cancelled_callbacks_never_leak_audio(self):
        live = self.live()
        live.buffer.push(np.ones(640, np.float32))
        live.muted.set()
        out = np.ones((320, 2), np.float32)
        live._output(out, 320, None, False)
        self.assertTrue(np.all(out == 0))
        live.muted.clear()
        live.stop()
        out.fill(1)
        live._output(out, 320, None, False)
        self.assertTrue(np.all(out == 0))

    def test_input_overload_fails_closed(self):
        live = self.live()
        for _ in range(live.input_queue.maxsize + 1):
            live._input(np.ones((640, 1), np.float32), 640, None, False)
        self.assertTrue(live.cancel.is_set())
        out = np.ones((320, 2), np.float32)
        live._output(out, 320, None, False)
        self.assertTrue(np.all(out == 0))

    def test_device_callback_error_outputs_silence(self):
        live = self.live()
        live.buffer.push(np.ones(640, np.float32))
        out = np.ones((320, 2), np.float32)
        live._output(out, 320, None, "device disconnected")
        self.assertTrue(live.cancel.is_set())
        self.assertTrue(np.all(out == 0))

    def test_sustained_model_starvation_stops(self):
        live = self.live()
        for _ in range(26):
            out = np.ones((320, 2), np.float32)
            live._output(out, 320, None, False)
            self.assertTrue(np.all(out == 0))
        self.assertTrue(live.cancel.is_set())


if __name__ == "__main__":
    unittest.main()
