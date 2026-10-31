"""Speech adapter decisions without microphones, subprocesses or robot IO."""
import unittest
from keyboard_input import SpeechInput
from remote_input import RemoteInput


def context(now, generation=0, enabled=True):
    return {"generation": generation, "enabled": enabled, "mode": "drive", "age": 0.,
            "state": {"t": now, "policy": "stand", "move": {"requested": [0., 0., 0.], "limited_by": []},
                      "safety": {"fallen": False, "limp": False, "gravity": [0., 0., -1.],
                                 "fall": {"imu_ready": True, "imu_fresh": True, "recovery_active": False}}}}


def packets(now, word=None, intent=None):
    result = [{"kind": "audio_heartbeat", "monotonic_s": now}]
    if word:
        result.append({"keyword": word, "intent": intent, "monotonic_s": now, "preview_only": True})
    return result


class RemoteInputTests(unittest.TestCase):
    def setUp(self):
        self.adapter = RemoteInput(SpeechInput("on", "."))

    def start_walk(self):
        self.adapter.step(context(1.), packets(1.), 1.)
        result = self.adapter.step(context(3.1), packets(3.1, "前进", "walk"), 3.1)
        self.assertEqual(result["drive"], "walk")

    def test_walking_lease_expires(self):
        self.start_walk()
        result = self.adapter.step(context(6.2), packets(6.2), 6.2)
        self.assertIsNone(result["drive"])
        self.assertEqual(result["action"], "stop")

    def test_manual_takeover_cancels_voice_and_discards_same_batch(self):
        self.start_walk()
        result = self.adapter.step(context(3.2, generation=1), packets(3.2, "后退", "backward"), 3.2)
        self.assertIsNone(result["drive"])
        self.assertIsNone(result["action"])

    def test_stale_state_and_disabled_robot_cannot_walk(self):
        for values in ({"enabled": False}, {"age": 1.}):
            with self.subTest(values=values):
                adapter = RemoteInput(SpeechInput("on", "."))
                ctx = context(1.); ctx.update(values)
                adapter.step(ctx, packets(1.), 1.)
                ctx = context(4.); ctx.update(values)
                result = adapter.step(ctx, packets(4., "前进", "walk"), 4.)
                self.assertIsNone(result["drive"])

    def test_audio_loss_stops_owned_motion(self):
        self.start_walk()
        result = self.adapter.step(context(4.1), [], 4.1)
        self.assertFalse(result["ready"])
        self.assertIsNone(result["drive"])
        self.assertEqual(result["action"], "stop")


if __name__ == "__main__":
    unittest.main()
