import unittest
from muter.device_policy import choose, virtual, family

def d(name):return {'name':name}

class DevicePolicyTests(unittest.TestCase):
    def test_virtual_default_never_becomes_capture(self):
        self.assertEqual(choose([(1,d('CABLE Output')), (2,d('立体声混音')), (3,d('Microphone (USB)'))],1,capture=True),3)
    def test_virtual_default_never_becomes_monitor(self):
        self.assertEqual(choose([(1,d('CABLE Input')), (2,d('扬声器 (USB)')), (3,d('HDMI (NVIDIA)'))],1,input_family=family('Microphone (USB)')),2)
    def test_valid_physical_default_is_respected(self):
        self.assertEqual(choose([(1,d('耳机 (BT Stereo)')), (2,d('扬声器 (USB)'))],1),1)
    def test_old_device_index_is_not_required(self):
        self.assertEqual(choose([(99,d('Microphone (USB)')), (100,d('CABLE Output'))],3,capture=True),99)
    def test_only_virtual_devices_is_explicitly_unavailable(self):
        with self.assertRaises(RuntimeError):choose([(1,d('CABLE Output'))],1,capture=True)
    def test_all_virtual_families_are_filtered(self):
        for n in ['CABLE In 16ch','Steam Streaming Microphone','立体声混音','Voicemeeter Output']:
            self.assertTrue(virtual(n))

    def test_playback_automatically_bypasses_virtual_default(self):
        from types import SimpleNamespace
        from pathlib import Path
        from unittest.mock import patch,Mock
        import queue,threading,numpy as np
        from muter.app import App
        app=SimpleNamespace(last_test=Path('test'), cancel=threading.Event(),events=queue.Queue(),
                            launch_job=lambda name,fn:fn())
        with patch('muter.app.sf.read',return_value=(np.zeros((160,1),np.float32),16000)), \
             patch('muter.app.devices',return_value=([],[(32,d('CABLE Input')),(35,d('Speaker'))],(-1,32))), \
             patch('muter.app.stream_config',return_value=(16000,1)), \
             patch('muter.app.sd.OutputStream') as stream:
            App.play(app,'original.wav')
        self.assertEqual(stream.call_args.kwargs['device'],35)

    def test_virtual_input_is_removed_even_when_it_is_windows_default(self):
        from types import SimpleNamespace
        from unittest.mock import patch,Mock
        from test_app_state import Value
        from muter.app import App
        app=SimpleNamespace(busy=False,input_var=Value('41  ·  CABLE Output'),output_var=Value(''),
            input_combo={},output_combo={},update_route=Mock(),status_var=Value(''))
        with patch('muter.app.devices',return_value=([(41,d('CABLE Output')),(45,d('Microphone (USB)'))],[(32,d('CABLE Input')),(35,d('Speaker'))],(41,32))):
            App.refresh_devices(app)
        self.assertEqual(list(app.inputs.values()),[45])
        self.assertIn('Microphone (USB)',app.input_var.get())
