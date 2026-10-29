"""No microphone/robot. Guards plus fake recognizer process tests."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from keyboard_input import SpeechInput


def state(now, policy="stand", **safety):
    return {"t": now, "policy": policy,
            "safety": {"fallen": False, "limp": False, "gravity": [0., 0., -1.], **safety}}


def event(now, intent):
    word = {"walk": "前进", "backward": "后退", "stop": "停止", "sit": "坐下", "rise": "起来"}[intent]
    return {"keyword": word, "intent": intent, "preview_only": True, "monotonic_s": now}


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.voice = SpeechInput("on", ".")

    def decide(self, now, intent=None, **kwargs):
        packets = [{"kind": "audio_heartbeat", "monotonic_s": now}]
        if intent:
            packets.append(event(now, intent))
        context = dict(enabled=True, mode="drive", state=state(now), state_at=now, velocity=(0., 0., 0.))
        context.update(kwargs)
        return self.voice.decide(packets, now, **context)

    def test_constructor_and_import_do_not_start_process(self):
        self.assertIsNone(self.voice.process)

    def test_keyword_followed_by_heartbeat_is_logged_and_not_overwritten(self):
        self.voice.mode = "preview"
        packets = [event(10., "walk"), {"kind": "audio_heartbeat", "monotonic_s": 10.}]
        self.assertIsNone(self.voice.decide(packets, 10., enabled=False, mode="drive",
                          state={}, state_at=0., velocity=(0., 0., 0.)))
        self.assertEqual(self.voice.status, "前进")
        text = '\n'.join(self.voice.messages)
        self.assertIn('[语音识别] 前进', text)
        self.assertIn('preview仅识别', text)
        self.decide(10.5)
        self.assertEqual(self.voice.status, "前进")

    def test_unarmed_and_dwell_rejections_are_visible(self):
        self.decide(1., "walk", enabled=False)
        self.assertIn('人工按Enter', '\n'.join(self.voice.messages))
        self.decide(2., "walk")
        self.assertIn('零速等待2秒', '\n'.join(self.voice.messages))
        self.decide(4.1, "walk")
        self.assertIn('walk；提交速度', '\n'.join(self.voice.messages))

    def test_manual_priority_and_expired_events_are_observable_not_executed(self):
        kwargs = dict(enabled=True, mode="drive", state=state(4.), state_at=4., velocity=(0., 0., 0.))
        self.assertIsNone(self.voice.decide([event(4., "sit")], 4., manual_input=True, **kwargs))
        self.assertIsNone(self.voice.pending_sit)
        self.assertIn('键盘输入优先', '\n'.join(self.voice.messages))
        self.assertIsNone(self.voice.decide([event(1., "walk")], 4., **kwargs))
        self.assertIn('事件过期', '\n'.join(self.voice.messages))

    def test_audio_fault_and_state_rejections_have_separate_reasons(self):
        self.voice.audio_at = None
        self.voice.decide([event(1., "walk")], 1., enabled=True, mode="drive",
                          state=state(1.), state_at=1., velocity=(0., 0., 0.))
        self.assertIn('音频心跳过期', self.voice.reason)
        self.decide(2., "walk", state_at=0.)
        self.assertIn('robot.state过期', self.voice.reason)

    def test_start_forwards_matching_gain_threshold_and_reports_resolved_path(self):
        with tempfile.TemporaryDirectory() as directory:
            messages = []
            voice = SpeechInput('preview', directory, '/usr/bin/python3', gain=1.2, threshold=.4, report=messages.append)
            with patch('keyboard_input.subprocess.Popen') as popen:
                voice.start()
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index('--gain')+1], '1.2')
            self.assertEqual(command[command.index('--threshold')+1], '0.4')
            self.assertIn(str(Path(directory).resolve()), messages[0])
            self.assertIn('mode=preview', messages[0])

    def test_off_is_not_a_recognizer(self):
        with self.assertRaises(ValueError):
            SpeechInput("off", ".")

    def test_preview_never_controls(self):
        self.voice.mode = "preview"
        for intent in ("walk", "backward", "sit", "rise", "stop"):
            self.assertIsNone(self.decide(10., intent))

    def test_voice_cannot_enable_and_needs_zero_dwell(self):
        self.assertIsNone(self.decide(1., "walk", enabled=False))
        self.assertIsNone(self.decide(2., "walk"))
        self.assertEqual(self.decide(4.1, "walk"), "walk")

    def test_walk_is_bounded_and_stop_wins_same_packet(self):
        self.decide(1.)
        self.assertEqual(self.decide(3.1, "walk"), "walk")
        self.assertEqual(self.decide(6.2, velocity=(.1, 0., 0.)), "stop")
        self.assertEqual(self.voice.decide([event(7., "walk"), event(7., "stop")], 7., enabled=True,
                         mode="drive", state=state(7.), state_at=7., velocity=(0., 0., 0.)), "stop")

    def test_backward_direction_limits_and_manual_takeover(self):
        self.assertIsNone(self.decide(1., "backward", enabled=False))
        self.decide(2.)
        self.assertEqual(self.decide(4.1, "backward"), "backward")
        self.assertEqual(self.voice.velocity_for("backward"), (-.1, 0., 0.))
        self.assertEqual(self.voice.velocity_for("walk"), (.1, 0., 0.))
        self.assertEqual(self.decide(7.2, velocity=(-.1, 0., 0.)), "stop")
        self.voice.owns_walk = True
        self.voice.cancel()
        self.assertIsNone(self.decide(8., velocity=(-.2, 0., 0.)))

    def test_backward_needs_stopped_dwell_and_fault_stops_it(self):
        self.assertIsNone(self.decide(1., "backward", velocity=(.1, 0., 0.)))
        self.decide(2.)
        self.assertIsNone(self.decide(3., "backward"))
        self.assertEqual(self.decide(4.1, "backward"), "backward")
        self.voice.failed = True
        self.assertEqual(self.decide(4.2, velocity=(-.1, 0., 0.)), "stop")

    def test_conflicting_directions_stop_and_keyword_contract_matches(self):
        import recognize
        from keyboard_input import KEYWORDS
        self.assertEqual(KEYWORDS, recognize.COMMANDS)
        words = [line.split(' @') for line in Path(__file__).with_name('keywords_raw.txt').read_text(encoding='utf-8').splitlines()]
        self.assertEqual({word for word, tag in words}, set(KEYWORDS))
        self.assertTrue(all(word == tag for word, tag in words))
        self.decide(1.)
        self.voice.audio_at = 4.
        self.assertEqual(self.voice.decide([event(4., "walk"), event(4., "backward")], 4., enabled=True,
                         mode="drive", state=state(4.), state_at=4., velocity=(0., 0., 0.)), "stop")

    def test_sit_stops_then_waits_two_seconds(self):
        self.assertEqual(self.decide(1., "sit", velocity=(.1, 0., 0.)), "stop_for_sit")
        self.assertIsNone(self.decide(1.1))
        self.assertEqual(self.decide(3.2), "sit")

    def test_absolute_sit_rise_are_idempotent(self):
        self.assertIsNone(self.decide(1., "sit", state=state(1., "sit")))
        self.assertEqual(self.decide(2., "rise", state=state(2., "sit")), "rise")
        self.assertIsNone(self.decide(2.1, "rise", state=state(2.1, "rise")))
        self.assertIsNone(self.decide(4., "rise"))

    def test_head_body_or_recovery_cancels_pending_intent(self):
        for changes in ({"mode": "head"}, {"mode": "body"},
                        {"state": state(4., "standup")}, {"state": state(4., fallen=True)}):
            self.setUp()
            self.decide(1., "sit", velocity=(.1, 0., 0.))
            self.assertIsNone(self.decide(4., **changes))
            self.assertIsNone(self.voice.pending_sit)

    def test_audio_exit_stale_state_and_repeated_state_stop_owned_walk(self):
        for fault in ("audio", "failed", "state", "repeated"):
            self.setUp()
            self.decide(1.)
            self.decide(3.1, "walk")
            self.voice.audio_at = 3.1
            if fault == "failed":
                self.voice.failed = True
            now = 4.1
            context = dict(enabled=True, mode="drive", state=state(now), state_at=now, velocity=(.1, 0., 0.))
            packets = [] if fault == "audio" else [{"kind": "audio_heartbeat", "monotonic_s": now}]
            if fault == "state":
                context["state_at"] = 2.
            if fault == "repeated":
                context["state"] = state(3.1)
            self.assertEqual(self.voice.decide(packets, now, **context), "stop", fault)

    def test_keyboard_takeover_cancels_voice_lease_not_manual_velocity(self):
        self.voice.owns_walk = True
        self.voice.pending_sit = 1.
        self.voice.cancel()
        self.assertIsNone(self.decide(100., velocity=(.2, .1, 0.)))
        self.assertIsNone(self.voice.pending_sit)

    def test_stale_duplicate_forged_and_unknown_packets_are_ignored(self):
        self.decide(1.)
        ctx = dict(enabled=True, mode="drive", state=state(4.), state_at=4., velocity=(0., 0., 0.))
        self.voice.audio_at = 4.
        for packet in (event(1., "walk"), {**event(4., "walk"), "intent": "rise"},
                       {**event(4., "walk"), "preview_only": False},
                       {**event(4., "walk"), "monotonic_s": float("nan")}, None):
            self.assertIsNone(self.voice.decide([packet], 4., **ctx))
        self.assertEqual(self.voice.decide([event(4., "walk")], 4., **ctx), "walk")
        self.assertIsNone(self.voice.decide([event(4., "walk")], 4., **ctx))

    def test_fault_does_not_stop_unowned_keyboard_motion(self):
        self.voice.failed = True
        self.assertIsNone(self.decide(2., velocity=(.1, 0., 0.)))

    def test_stop_also_works_in_head_mode_for_keyboard_velocity(self):
        self.assertEqual(self.decide(2., "stop", mode="head", velocity=(.1, 0., 0.)), "stop")

    def test_safety_limited_movement_does_not_accept_voice_walk(self):
        self.decide(1.)
        blocked = state(4.)
        blocked["move"] = {"limited_by": ["overcurrent"]}
        self.assertIsNone(self.decide(4., "walk", state=blocked))

    def test_keyboard_default_and_no_rust_migration(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts/keyboard-control.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        speech_arg = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and n.args
                          and isinstance(n.args[0], ast.Constant) and n.args[0].value == "--speech")
        self.assertEqual(next(k.value.value for k in speech_arg.keywords if k.arg == "default"), "off")
        self.assertNotIn("socket", (root / "speech/keyboard_input.py").read_text(encoding="utf-8").split('"""', 2)[-1])


@unittest.skipUnless(sys.platform == "linux", "Linux pipe polling")
class ProcessTests(unittest.TestCase):
    def test_master_keyboard_pty_with_optional_preview_and_on(self):
        # Reuse master's real Unix server + PTY + Enter/turn/walk/space/exit
        # battery, adding a real fake KWS child. Its deliberately incomplete
        # robot state must never authorize speech motion, even in on mode.
        path = Path(__file__).resolve().parents[1] / "scripts/test_keyboard_control.py"
        spec = importlib.util.spec_from_file_location("master_keyboard_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_popen = __import__("subprocess").Popen
        for mode in ("preview", "on"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                Path(directory, "recognize.py").write_text(
                    'import json,time\nwhile True:\n t=time.monotonic()\n'
                    ' print(json.dumps({"kind":"audio_heartbeat","monotonic_s":t}),flush=True)\n'
                    ' print(json.dumps({"keyword":"前进","intent":"walk","preview_only":True,"monotonic_s":t}),flush=True)\n'
                    ' time.sleep(.2)\n', encoding="utf-8")
                def popen(command, *args, **kwargs):
                    if str(module.keyboard_control.__file__) in command:
                        command = [*command, "--speech", mode, "--speech-dir", directory,
                                   "--speech-python", sys.executable]
                    return real_popen(command, *args, **kwargs)
                with patch("subprocess.Popen", side_effect=popen):
                    module.KeyboardTerminalTests("test_turn_persists_without_repeats_and_space_stops_it").test_turn_persists_without_repeats_and_space_stops_it()

    def test_real_child_heartbeat_exit_and_cleanup_without_robot(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "recognize.py").write_text(
                'import json,time\nprint(json.dumps({"kind":"audio_heartbeat","monotonic_s":time.monotonic()}),flush=True)\ntime.sleep(.4)\n',
                encoding="utf-8")
            voice = SpeechInput("on", directory, sys.executable).start()
            try:
                packets = []
                deadline = time.monotonic() + 2.
                while not packets and time.monotonic() < deadline:
                    packets.extend(voice.poll())
                    time.sleep(.01)
                self.assertEqual(packets[0]["kind"], "audio_heartbeat")
                voice.process.wait(timeout=2.)
                voice.poll()
                self.assertTrue(voice.failed)
            finally:
                voice.close()
            self.assertIsNotNone(voice.process.returncode)


if __name__ == "__main__":
    unittest.main()
