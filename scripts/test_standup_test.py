import importlib.util
from pathlib import Path
import sys
import tomllib
import unittest


SCRIPT = Path(__file__).with_name("standup-test.py")
SPEC = importlib.util.spec_from_file_location("standup_test", SCRIPT)
standup_test = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = standup_test
SPEC.loader.exec_module(standup_test)


class StandUpConfigTests(unittest.TestCase):
    def test_trial_uses_only_standup_and_preserves_hardware_and_current_protection(self):
        source = (SCRIPT.parent.parent / "deploy/robotd.toml").read_text(encoding="utf-8")
        original = tomllib.loads(source)
        configured = tomllib.loads(standup_test.test_config(source, Path("/tmp/standup.onnx")))
        for section in ("platform", "bus", "imu", "control"):
            self.assertEqual(configured[section], original[section])
        self.assertEqual(configured["policy"]["gain"], original["policy"]["gain"])
        self.assertEqual(configured["policy"]["stand"], "/tmp/standup.onnx")
        self.assertEqual(configured["policy"]["walk"], "/tmp/standup.onnx")
        for name in ("sitstand", "ground_pick", "kick_left", "kick_right", "roulade"):
            self.assertEqual(configured["policy"][name], "none")
        self.assertFalse(configured["safety"]["fall_protection"])
        self.assertFalse(configured["safety"]["limp_fall"])
        for key, value in original["safety"].items():
            if key not in ("fall_protection", "limp_fall"):
                self.assertEqual(configured["safety"][key], value)

    def test_missing_sections_and_unterminated_last_line_stay_valid_toml(self):
        source = '[bus]\nport = "/dev/ttyS4"'
        configured = standup_test.test_config(source, Path("/tmp/my model.onnx"))
        parsed = tomllib.loads(configured)
        self.assertEqual(parsed["bus"]["port"], "/dev/ttyS4")
        self.assertEqual(parsed["policy"]["stand"], "/tmp/my model.onnx")


if __name__ == "__main__":
    unittest.main()
