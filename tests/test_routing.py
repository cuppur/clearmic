import tempfile
import unittest
from pathlib import Path
from muter.routing import RouteSession


class API:
    def __init__(self): self.roles = {"0":"mic-a", "1":"mic-b", "2":"mic-c"}
    def defaults(self): return self.roles.copy()
    def cable_capture(self): return "cable"
    def set_capture(self, dev, role): self.roles[str(role)] = dev


class RoutingTests(unittest.TestCase):
    def test_all_roles_restored_exactly(self):
        with tempfile.TemporaryDirectory() as t:
            a=API(); before=a.defaults(); r=RouteSession(a,Path(t)/'state.json')
            r.begin(False); self.assertEqual(set(a.roles.values()),{'cable'})
            r.restore(); self.assertEqual(a.roles,before); self.assertFalse(r.path.exists())

    def test_external_user_change_is_respected(self):
        with tempfile.TemporaryDirectory() as t:
            a=API(); r=RouteSession(a,Path(t)/'state.json'); r.begin(False)
            a.roles['1']='user-choice'; r.restore()
            self.assertEqual(a.roles,{'0':'mic-a','1':'user-choice','2':'mic-c'})

    def test_failed_switch_rolls_back(self):
        with tempfile.TemporaryDirectory() as t:
            a=API(); original=a.set_capture
            def change(dev,role):
                if dev=='cable' and role=='1': raise OSError('device error')
                original(dev,role)
            a.set_capture=change; r=RouteSession(a,Path(t)/'state.json')
            with self.assertRaises(OSError):r.begin(False)
            self.assertEqual(a.roles,{'0':'mic-a','1':'mic-b','2':'mic-c'})
