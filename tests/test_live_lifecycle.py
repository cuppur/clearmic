"""Lifecycle regressions with fake models/devices; never opens a microphone."""

import queue
import unittest
from unittest.mock import Mock, patch

import numpy as np

from muter.audio import AudioBuffer, LiveAudio


class LiveLifecycleTests(unittest.TestCase):
    def make_live(self):
        model = Mock()
        model.stream.return_value.push.return_value = np.zeros(0, np.float32)
        return LiveAudio(model, np.ones(48000, np.float32), 0, 1, queue.Queue())

    def test_cancel_before_start_never_opens_audio_devices(self):
        live = self.make_live()
        live.stop()
        with patch("muter.audio.sd.OutputStream") as output, patch("muter.audio.sd.InputStream") as source:
            live._run()
        output.assert_not_called()
        source.assert_not_called()
        self.assertIsNone(live.error)
        self.assertIn(("live_stopped", None), list(live.events.queue))

    def test_cancel_during_model_warmup_never_opens_audio_devices(self):
        live = self.make_live()

        def stop_during_warmup(_):
            live.stop()
            return np.zeros(0, np.float32)

        live.model.stream.return_value.push.side_effect = stop_during_warmup
        with patch("muter.audio.sd.OutputStream") as output, patch("muter.audio.sd.InputStream") as source:
            live._run()
        output.assert_not_called()
        source.assert_not_called()
        self.assertIsNone(live.error)
        self.assertTrue(live.cancel.is_set())

    def test_partial_starvation_zeroes_entire_callback(self):
        live = self.make_live()
        live.output_rate = 1000
        live.buffer = AudioBuffer(1000)
        live.prefill = 0
        live.ready = True
        live.starved_frames = 490
        # Fifteen missing frames cross the 500-frame stop threshold. The five
        # available frames must also be silenced in that same output callback.
        live.buffer.push(np.full(5, .5, np.float32))
        output = np.ones((20, 2), np.float32)
        live._output(output, len(output), None, False)
        self.assertTrue(live.cancel.is_set())
        self.assertIsNotNone(live.error)
        np.testing.assert_array_equal(output, np.zeros_like(output))

    def test_stalled_output_callbacks_fail_even_while_device_is_active(self):
        live = self.make_live()
        # The output driver used to deliver callbacks, but has stopped while
        # reporting active=True. Capture continues to provide fresh audio.
        live.last_output_callback = 99.6
        live.input_queue = Mock()
        live.input_queue.get.return_value = (100.0, np.ones(160, np.float32))
        with patch("muter.audio.stream_config", return_value=(16000, 1)), \
             patch("muter.audio.soxr.ResampleStream"), \
             patch("muter.audio.time.monotonic", return_value=100.0), \
             patch("muter.audio.sd.OutputStream") as output, \
             patch("muter.audio.sd.InputStream") as source:
            output.return_value.__enter__.return_value.active = True
            active_source = Mock(active=True)

            def enter_source():
                live.last_output_callback = 99.6
                return active_source

            source.return_value.__enter__.side_effect = enter_source
            live._run()
        self.assertTrue(live.cancel.is_set())
        self.assertIn("输出设备没有持续接收音频", live.error)
        output.return_value.__exit__.assert_called_once()
        source.return_value.__exit__.assert_called_once()
        self.assertIn(("live_stopped", live.error), list(live.events.queue))


if __name__ == "__main__":
    unittest.main()
