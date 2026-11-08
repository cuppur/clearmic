"""State tests without constructing Tk windows, opening a mic, or playing audio."""
from types import SimpleNamespace
import queue
import unittest
from unittest.mock import Mock, patch

import numpy as np

from muter.app import App
from muter.audio import LiveAudio
from muter.realtime import AudioBuffer


class Value:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def fake_app():
    model = SimpleNamespace(requires_voice=True)
    return SimpleNamespace(
        poll_timer=None, live=None, busy=False, paused=False, hold_pressed=False,
        ptt_var=Value(False), guard_var=Value(False), events=queue.Queue(), model=model,
        model_generation=5, model_ready=False, voice=None,
        status_var=Value("unchanged"), sync_controls=Mock(), closed=False,
        after=Mock(return_value="timer"), after_cancel=Mock(), poll=Mock(),
    )


class AppGenerationTests(unittest.TestCase):
    def test_stale_ready_and_error_are_ignored_without_modal(self):
        for kind in ["model_ready", "model_error"]:
            for same_model in [False, True]:
                with self.subTest(kind=kind, same_model=same_model):
                    app = fake_app()
                    source = app.model if same_model else SimpleNamespace(requires_voice=True)
                    generation = 4 if same_model else 5
                    payload = (source, generation, .1 if kind == "model_ready" else "old model failed")
                    app.events.put((kind, payload))
                    with patch("muter.app.messagebox.showerror") as modal:
                        App.poll(app)
                    self.assertFalse(app.model_ready)
                    self.assertEqual(app.status_var.get(), "unchanged")
                    app.sync_controls.assert_not_called()
                    modal.assert_not_called()

    def test_current_ready_updates_state_without_modal(self):
        app = fake_app()
        app.events.put(("model_ready", (app.model, app.model_generation, .1)))
        with patch("muter.app.messagebox.showerror") as modal:
            App.poll(app)
        self.assertTrue(app.model_ready)
        app.sync_controls.assert_called_once()
        modal.assert_not_called()

    def test_environment_backend_ready_does_not_request_voice_enrollment(self):
        app = fake_app()
        app.model.requires_voice = False
        app.events.put(("model_ready", (app.model, app.model_generation, .1)))
        with patch("muter.app.messagebox.showerror"):
            App.poll(app)
        self.assertIn("无需录入声音", app.status_var.get())
        self.assertIn("不区分", app.status_var.get())

    def test_model_loader_uses_explicit_model_and_generation(self):
        app = fake_app()
        requested = Mock()
        App._load_model(app, requested, 3)
        requested.load.assert_called_once()
        kind, payload = app.events.get_nowait()
        self.assertEqual(kind, "model_ready")
        self.assertIs(payload[0], requested)
        self.assertEqual(payload[1], 3)

    def test_failed_loader_error_retains_requested_generation(self):
        app = fake_app()
        requested = Mock()
        requested.load.side_effect = ValueError("invalid model")
        with self.assertLogs(level="ERROR"):
            App._load_model(app, requested, 3)
        kind, payload = app.events.get_nowait()
        self.assertEqual(kind, "model_error")
        self.assertIs(payload[0], requested)
        self.assertEqual(payload[1], 3)


class BackendTransmitTests(unittest.TestCase):
    def make_live(self, callback):
        live = LiveAudio(Mock(), None, 0, 1, queue.Queue(), transmit_allowed=callback)
        live.output_rate = 16000
        live.buffer = AudioBuffer(1024)
        live.prefill = 0
        live.ready = True
        return live

    def test_backend_observes_permission_changes_without_gui_polling(self):
        permission = [True]
        live = self.make_live(lambda: permission[0])
        self.assertTrue(live.can_transmit())
        live._input(np.full((160, 1), .5, np.float32), 160, None, False)
        self.assertTrue(live.input_queue.get_nowait()[2])
        permission[0] = False
        live._input(np.full((160, 1), .5, np.float32), 160, None, False)
        self.assertFalse(live.input_queue.get_nowait()[2])
        live.buffer.push(np.ones(160, np.float32))
        output = np.ones((160, 2), np.float32)
        live._output(output, 160, None, False)
        np.testing.assert_array_equal(output, np.zeros_like(output))

    def test_callback_failure_and_explicit_stop_fail_closed(self):
        def failure():
            raise RuntimeError("permission source unavailable")
        live = self.make_live(failure)
        self.assertFalse(live.can_transmit())
        live.buffer.push(np.ones(160, np.float32))
        output = np.ones((160, 2), np.float32)
        live._output(output, 160, None, False)
        self.assertTrue(np.all(output == 0))
        live.transmit_allowed = lambda: True
        live.muted.set()
        self.assertFalse(live.can_transmit())
        live.muted.clear()
        live.stop()
        self.assertFalse(live.can_transmit())


if __name__ == "__main__":
    unittest.main()
