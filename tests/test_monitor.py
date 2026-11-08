import queue
import unittest
from unittest.mock import Mock, patch

import numpy as np

from muter.audio import LiveAudio
from muter.device_policy import choose_monitor


class MonitorTests(unittest.TestCase):
    def test_never_selects_cable_or_speakers_for_monitoring(self):
        candidates = [(1, {"name": "CABLE Input"}), (2, {"name": "Redmi Speakers"}),
                      (3, {"name": "Headphones (Xiaomi Stereo)"})]
        self.assertEqual(choose_monitor(candidates, default=1), 3)
        with self.assertRaisesRegex(RuntimeError, "未检测到耳机"):
            choose_monitor(candidates[:2])

    def test_hfp_microphone_prefers_matching_duplex_endpoint(self):
        candidates = [(1, {"name": "耳机 (Xiaomi Stereo)"}),
                      (2, {"name": "耳机 (Xiaomi Hands-Free AG Audio)"})]
        self.assertEqual(choose_monitor(candidates, "耳机 (Xiaomi Hands-Free AG Audio)"), 2)
        self.assertEqual(choose_monitor(candidates, "USB Microphone"), 1)

    def test_meeting_stream_rejects_original_audio_switch(self):
        live = LiveAudio(Mock(), None, 0, 1, queue.Queue())
        with self.assertRaises(ValueError):
            live.set_monitor_original(True)
        self.assertFalse(live.monitor_original.is_set())

    def test_backend_rejects_virtual_monitor_output_before_opening_stream(self):
        model = Mock()
        model.stream.return_value.push.return_value = np.zeros(0, np.float32)
        live = LiveAudio(model, None, 0, 1, queue.Queue(), monitor_only=True)
        with patch("muter.audio.stream_config", return_value=(16000, 1)), \
             patch("muter.audio.sd.query_devices", return_value={"name": "CABLE Input"}), \
             patch("muter.audio.sd.OutputStream") as output, self.assertLogs(level="ERROR"):
            live._run()
        output.assert_not_called()
        self.assertIn("实体耳机", live.error)

    def test_live_ab_switch_changes_waveform_and_clears_original_tail(self):
        model = Mock()
        model.stream.return_value.push.side_effect = lambda x: np.zeros_like(x)
        live = LiveAudio(model, None, 0, 1, queue.Queue(), monitor_only=True)
        clock = [100.0]
        rendered = [[], [], []]

        def capture(*args):
            for phase in range(3):
                live.set_monitor_original(phase == 1)
                for packet in range(100):
                    clock[0] += .01
                    output = np.empty((160, 1), np.float32)
                    live._output(output, 160, None, False)
                    if packet >= 30:
                        rendered[phase].append(output.copy())
                    yield (clock[0], np.full(160, .25, np.float32), True), False, .01
            live.stop()

        live._capture = capture
        with patch("muter.audio.stream_config", return_value=(16000, 1)), \
             patch("muter.audio.sd.query_devices", return_value={"name": "Headphones"}), \
             patch("muter.audio.time.monotonic", side_effect=lambda: clock[0]), \
             patch("muter.audio.sd.OutputStream") as output:
            output.return_value.__enter__.return_value.active = True
            live._run()
        self.assertIsNone(live.error)
        levels = [float(np.sqrt(np.mean(np.square(np.concatenate(samples))))) for samples in rendered]
        self.assertLess(levels[0], 1e-5)
        self.assertGreater(levels[1], .2)
        self.assertLess(levels[2], 1e-5)


if __name__ == "__main__":
    unittest.main()
