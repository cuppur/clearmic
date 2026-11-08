import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from muter.diagnostics import export_bundle


class DiagnosticsTests(unittest.TestCase):
    def test_bundle_excludes_recordings_profiles_and_arbitrary_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("app.log", "app.log.1", "voice.wav", "secret.txt", "routing.json"):
                (root / name).write_text("fixture", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "original.wav").write_bytes(b"private audio")
            path = export_bundle(root, {"app_version": "test", "audio_content_included": False})
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(set(archive.namelist()), {"README.txt", "system.json", "app.log", "app.log.1"})
                self.assertFalse(json.loads(archive.read("system.json"))["audio_content_included"])


if __name__ == "__main__":
    unittest.main()
