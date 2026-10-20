import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("camera-detect-test.py")
SPEC = importlib.util.spec_from_file_location("camera_detect_test", SCRIPT)
camera = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = camera
SPEC.loader.exec_module(camera)


class CameraDetectTests(unittest.TestCase):
    def test_load_config_reads_detection_without_touching_robotd(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "robotd.toml"
            path.write_text("[detect]\nenabled = true\nhz = 2.0\n", encoding="utf-8")
            used, config = camera.load_config(path)
        self.assertEqual(used, path)
        self.assertTrue(config["detect"]["enabled"])

    def test_console_url_adds_mdns_suffix_only_to_simple_names(self):
        self.assertEqual(camera.console_url("radxa"), "http://radxa.local:8080/")
        self.assertEqual(camera.console_url("10.0.0.2"), "http://10.0.0.2:8080/")
        self.assertEqual(camera.console_url("duck.local"), "http://duck.local:8080/")


if __name__ == "__main__":
    unittest.main()
