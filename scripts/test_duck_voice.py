import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("duck-voice.py")
SPEC = importlib.util.spec_from_file_location("duck_voice", SCRIPT)
duck_voice = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = duck_voice
SPEC.loader.exec_module(duck_voice)


class FakeClient:
    def __init__(self):
        self.events = []

    def call(self, method, params):
        self.events.append(("call", method, params))
        return {"accepted": True}

    def notify(self, method, params):
        self.events.append(("notify", method, params))

    def close_mouth(self):
        self.events.append(("close", "robot.mouth", {"open": 0.0}))

    def hello(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FailingClient(FakeClient):
    def call(self, method, params):
        self.events.append(("call", method, params))
        raise duck_voice.RobotdError("test failure")


class DuckVoiceTests(unittest.TestCase):
    def test_every_emotion_uses_a_supported_one_shot_tag(self):
        self.assertEqual(
            {cue.tag for cue in duck_voice.EMOTIONS.values()},
            set(duck_voice.SOUND_TO_EMOTION),
        )

    def test_mouth_frames_are_bounded_and_finish_closed(self):
        for cue in duck_voice.EMOTIONS.values():
            frames = list(duck_voice.mouth_frames(cue, 50.0, 0.85))
            self.assertTrue(frames)
            self.assertTrue(all(0.0 <= value <= 1.0 for value in frames))
            self.assertEqual(frames[-1], 0.0)

    def test_scale_reduces_peak_opening(self):
        cue = duck_voice.EMOTIONS["happy"]
        full = max(duck_voice.mouth_frames(cue, 50.0, 1.0))
        half = max(duck_voice.mouth_frames(cue, 50.0, 0.5))
        self.assertLess(half, full)

    def test_sound_is_requested_before_mouth_and_finally_closes(self):
        client = FakeClient()
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        duck_voice.play_cue(
            client,
            duck_voice.EMOTIONS["curious"],
            with_mouth=True,
            rate_hz=50.0,
            mouth_scale=0.8,
            sleep_fn=sleep,
            monotonic_fn=lambda: now[0],
        )
        self.assertEqual(client.events[0][0:2], ("call", "robot.sound"))
        self.assertIn(("notify", "robot.mouth", {"open": 0.0}), client.events)
        self.assertEqual(client.events[-1], ("close", "robot.mouth", {"open": 0.0}))

    def test_failed_sound_request_still_requests_a_closed_mouth(self):
        client = FailingClient()
        with self.assertRaises(duck_voice.RobotdError):
            duck_voice.play_cue(
                client,
                duck_voice.EMOTIONS["alarm"],
                with_mouth=True,
                rate_hz=50.0,
                mouth_scale=0.8,
                sleep_fn=lambda _seconds: None,
            )
        self.assertEqual(client.events[-1], ("close", "robot.mouth", {"open": 0.0}))

    def test_sound_only_sends_no_mouth_command_at_all(self):
        client = FakeClient()
        with patch.object(duck_voice, "RobotdClient", return_value=client):
            result = duck_voice.main(["--emotion", "greeting", "--sound-only"])
        self.assertEqual(result, 0)
        self.assertEqual(
            client.events,
            [("call", "robot.sound", {"tag": "greet"})],
            "sound-only must not close, power, or otherwise touch the mouth",
        )


if __name__ == "__main__":
    unittest.main()
