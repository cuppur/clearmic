"""Capture transport regressions; synthetic silence, no hardware or recordings."""
from collections import deque
from contextlib import closing
import queue
import unittest
from unittest.mock import Mock, patch

import numpy as np

from muter.audio import LiveAudio
from muter.realtime import AudioBuffer


class ScriptedQueue:
    def __init__(self, clock, actions):
        self.clock = clock
        self.actions = deque(actions)

    def get(self, timeout):
        action = self.actions.popleft() if self.actions else None
        self.clock[0] += timeout if action is None else .01
        if action is None:
            raise queue.Empty
        return self.clock[0], action, True

    def get_nowait(self):
        raise queue.Empty


class CaptureRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.live = LiveAudio(Mock(), None, 0, 1, queue.Queue())
        self.live.buffer = AudioBuffer(16000)
        self.live.output_rate = 16000
        self.live.prefill = 0
        self.live.ready = True
        self.clock = [100.0]
        self.source = Mock(active=True, latency=.01)

    def run_capture(self, actions):
        self.live.input_queue = ScriptedQueue(self.clock, actions)
        source = patch("muter.audio.sd.InputStream")
        mocked = source.start()
        mocked.return_value.__enter__.return_value = self.source
        self.addCleanup(source.stop)
        timer = patch("muter.audio.time.monotonic", side_effect=lambda: self.clock[0])
        timer.start()
        self.addCleanup(timer.stop)
        waiting = patch.object(self.live.cancel, "wait", return_value=False)
        waiting.start()
        self.addCleanup(waiting.stop)
        return self.live._capture(16000, 1), mocked

    def test_sixty_seconds_of_digital_silence_never_triggers_recovery(self):
        silence = np.zeros(160, np.float32)
        generator, factory = self.run_capture([silence] * 6000)
        with closing(generator):
            for _ in range(6000):
                captured, recovered, _ = next(generator)
                self.assertFalse(recovered)
                self.assertFalse(self.live.cancel.is_set())
                self.assertFalse(self.live.capture_recovering.is_set())
                self.assertEqual(np.count_nonzero(captured[1]), 0)
        self.assertAlmostEqual(self.clock[0], 160.0)
        self.assertEqual(factory.call_count, 1)
        self.assertTrue(self.live.events.empty())

    def test_three_quarter_second_callback_gap_waits_and_recovers(self):
        silence = np.zeros(160, np.float32)
        self.live.buffer.push(np.full(160, .7, np.float32))
        generator, factory = self.run_capture([silence, None, None, None, silence])
        with closing(generator):
            self.assertFalse(next(generator)[1])
            _, recovered, _ = next(generator)
            self.assertTrue(recovered)
            self.assertFalse(self.live.cancel.is_set())
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(self.live.buffer.size, 0)
            output = np.ones((160, 2), np.float32)
            # Recovery silence cannot accumulate a fatal output underrun.
            for _ in range(100):
                self.live._output(output, 160, None, False)
                self.assertTrue(np.all(output == 0))
            self.assertFalse(self.live.cancel.is_set())

    def test_persistent_gap_reopens_same_mic_then_receives_silence(self):
        silence = np.zeros(160, np.float32)
        generator, factory = self.run_capture([None] * 6 + [silence])
        with closing(generator):
            self.assertTrue(next(generator)[1])
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(all(call.kwargs["device"] == 0 for call in factory.call_args_list))
        self.assertFalse(self.live.cancel.is_set())

    def test_persistent_failure_has_bounded_retries(self):
        generator, factory = self.run_capture([])
        with closing(generator), self.assertRaisesRegex(RuntimeError, "自动恢复未成功"):
            next(generator)
        self.assertEqual(factory.call_count, 4)  # Initial open plus three retries.
        self.assertLess(self.clock[0] - 100, 8)
        self.assertTrue(self.live.capture_recovering.is_set())
        self.assertEqual(self.live.buffer.size, 0)

    def test_cancel_during_gap_does_not_reopen(self):
        generator, factory = self.run_capture([])
        def cancel_on_get(timeout):
            self.live.stop()
            raise queue.Empty
        self.live.input_queue.get = cancel_on_get
        with closing(generator), self.assertRaises(StopIteration):
            next(generator)
        self.assertEqual(factory.call_count, 1)
        self.assertIsNone(self.live.error)

    def test_run_resets_processing_history_after_gap(self):
        silence = np.zeros(160, np.float32)
        generator, factory = self.run_capture([silence, None, None, None, silence])
        original_get = self.live.input_queue.get
        def get_with_running_output(timeout):
            try:
                return original_get(timeout)
            finally:
                # The render driver keeps receiving silence during the gap.
                self.live.last_output_callback = self.clock[0]
        self.live.input_queue.get = get_with_running_output
        # Use the scripted generator through the full processing loop, then
        # cancel without a further gap. No frames may survive in the model.
        def capture(*args):
            with closing(generator):
                yield next(generator)
                yield next(generator)
                self.live.stop()
        self.live._capture = capture
        self.live.model.stream.return_value.push.return_value = np.zeros(0, np.float32)
        with patch("muter.audio.stream_config", return_value=(16000, 1)), \
             patch("muter.audio.sd.OutputStream") as output:
            output.return_value.__enter__.return_value.active = True
            self.live._run()
        self.assertIsNone(self.live.error)
        self.assertFalse(self.live.capture_recovering.is_set())
        self.assertEqual(self.live.model.stream.call_count, 3)  # Warm, initial, reset.
        kinds = [kind for kind, _ in self.live.events.queue]
        self.assertEqual(kinds.count("live_started"), 1)
        self.assertEqual(kinds.count("capture_recovered"), 1)


if __name__ == "__main__":
    unittest.main()
