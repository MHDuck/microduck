"""Speech adapter decisions without microphones, subprocesses or robot IO."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch
from keyboard_input import SpeechInput
from remote_input import RemoteInput
import remote_input


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

    def run_startup(self, audio_after=None, audio_until=100, ticks=100):
        clock = [0.]
        ctx = {"enabled": False, "generation": 0, "state": None, "age": None}
        stdin = io.StringIO((json.dumps(ctx) + "\n") * ticks)
        output = io.StringIO()

        def select_input(*args):
            clock[0] += 1.
            return [stdin], [], []

        def poll(speech):
            if audio_after is not None and audio_after <= clock[0] < audio_until:
                return packets(clock[0])
            return []

        with patch.object(remote_input.sys, "argv", ["remote_input.py", "."]), \
             patch.object(remote_input.sys, "stdin", stdin), \
             patch.object(remote_input.time, "monotonic", lambda: clock[0]), \
             patch.object(remote_input.select, "select", select_input), \
             patch("signal.signal"), patch.object(SpeechInput, "start"), \
             patch.object(SpeechInput, "poll", autospec=True, side_effect=poll), \
             patch.object(SpeechInput, "close"), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = remote_input.main()
        return result, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_slow_model_can_become_ready_after_old_twenty_second_limit(self):
        result, messages = self.run_startup(audio_after=35, ticks=40)
        self.assertIsNone(result)
        self.assertTrue(any(not message["ready"] for message in messages))
        self.assertTrue(messages[-1]["ready"])
        self.assertTrue(all(message["drive"] is None and message["action"] is None for message in messages))

    def test_startup_timeout_reports_a_specific_failure(self):
        result, messages = self.run_startup()
        self.assertEqual(result, 1)
        self.assertTrue(messages[-1]["failed"])
        self.assertIn("启动超时（90 秒）", messages[-1]["reason"])
        self.assertFalse(messages[-1]["ready"])

    def test_startup_grace_does_not_extend_audio_loss_deadline(self):
        result, messages = self.run_startup(audio_after=2, audio_until=4)
        self.assertEqual(result, 1)
        self.assertTrue(messages[-1]["failed"])
        self.assertIn("音频心跳中断", messages[-1]["reason"])
        self.assertLess(len(messages), 10)


if __name__ == "__main__":
    unittest.main()
