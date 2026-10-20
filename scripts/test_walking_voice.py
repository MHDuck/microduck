import importlib.util
from pathlib import Path
import random
import sys
import threading
import time
import unittest


SCRIPT = Path(__file__).with_name("keyboard-control.py")
SPEC = importlib.util.spec_from_file_location("keyboard_control", SCRIPT)
keyboard_control = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = keyboard_control
SPEC.loader.exec_module(keyboard_control)


class FakeCue:
    duration_s = 0.01


class FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def hello(self):
        return None


class FakeVoice:
    DEFAULT_RATE_HZ = 50.0
    EMOTIONS = {"happy": FakeCue(), "curious": FakeCue()}

    def __init__(self):
        self.played = []
        self.played_event = threading.Event()

    def RobotdClient(self, _path):
        return FakeClient()

    def play_cue(self, _client, cue, **kwargs):
        self.played.append(cue)
        self.played_event.set()
        kwargs["sleep_fn"](0.01)


class WalkingVoiceTests(unittest.TestCase):
    def test_only_nonzero_enabled_commands_count_as_walking(self):
        self.assertFalse(keyboard_control.is_walking(False, (0.2, 0.0, 0.0)))
        self.assertFalse(keyboard_control.is_walking(True, (0.0, 0.0, 0.0)))
        self.assertTrue(keyboard_control.is_walking(True, (0.1, 0.0, 0.0)))
        self.assertTrue(keyboard_control.is_walking(True, (0.0, 0.0, 0.3)))

    def test_random_choice_avoids_immediate_repeat(self):
        rng = random.Random(7)
        for _ in range(20):
            self.assertEqual(
                keyboard_control.choose_emotion(("happy", "curious"), "happy", rng),
                "curious",
            )

    def test_worker_is_silent_until_motion_starts(self):
        voice = FakeVoice()
        worker = keyboard_control.WalkingVoice(
            "/tmp/not-used.sock",
            ("happy", "curious"),
            10.0,
            10.0,
            enabled=True,
            voice_module=voice,
            rng=random.Random(1),
        )
        try:
            time.sleep(0.03)
            self.assertEqual(voice.played, [])
            worker.set_moving(True)
            self.assertTrue(voice.played_event.wait(0.5))
            worker.set_moving(False)
        finally:
            worker.close()


if __name__ == "__main__":
    unittest.main()
