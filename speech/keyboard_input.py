"""Optional KWS input for the MASTER keyboard loop. No robot socket or RPC.

Only returns walk/stop/sit/rise intents; master owns enabling, recovery,
policy transitions and safety. Importing this module starts nothing.
"""
import json
from contextlib import suppress
import math
import os
from pathlib import Path
import select
import subprocess
import time

KEYWORDS = {"鸭子前进": "walk", "鸭子停止": "stop", "鸭子坐下": "sit", "鸭子起来": "rise"}


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


class SpeechInput:
    def __init__(self, mode, directory, python=None, device="plughw:microducki2s3,0"):
        if mode not in ("on", "preview"):
            raise ValueError("SpeechInput is only constructed when speech is enabled")
        self.mode, self.directory = mode, Path(directory).expanduser().resolve()
        self.python = python or str(self.directory / ".venv/bin/python")
        self.device = device
        self.process = None
        self.buffer = b""
        self.audio_at = None
        self.last_events = {}
        self.robot_t = self.robot_at = None
        self.owns_walk = False
        self.walk_until = 0.
        self.pending_sit = None
        self.zero_since = None
        self.pose_until = 0.
        self.failed = False
        self.status = "等待音频"

    def start(self):
        self.process = subprocess.Popen(
            [self.python, "-u", str(self.directory / "recognize.py"), "--heartbeat",
             "--source", "alsa", "--device", self.device, "--seconds", "86400"],
            cwd=str(self.directory), stdout=subprocess.PIPE,
        )
        return self

    def poll(self):
        if self.failed:
            return []
        try:
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError("识别进程退出")
            if not select.select([self.process.stdout], [], [], 0)[0]:
                return []
            chunk = os.read(self.process.stdout.fileno(), 8192)
            if not chunk:
                raise RuntimeError("识别输出断开")
            self.buffer += chunk
            if len(self.buffer) > 65536:
                raise ValueError("识别消息过长")
            packets = []
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                packet = json.loads(line)
                if not isinstance(packet, dict):
                    raise ValueError("识别消息不是对象")
                packets.append(packet)
            return packets
        except (OSError, ValueError, RuntimeError) as error:
            self.failed = True
            self.status = str(error) + "，语音停用；键盘仍可用"
            if self.process is not None and self.process.poll() is None:
                with suppress(OSError):
                    self.process.terminate()
            return []

    def cancel(self):
        """Any physical key takes ownership; don't later stop its latched motion."""
        self.owns_walk = False
        self.pending_sit = None
        self.zero_since = None

    def decide(self, packets, now, *, enabled, mode, state, state_at, velocity):
        """At most ONE intent per tick. No enable, restart, relax or skill timer."""
        events = []
        for event in packets:
            if not isinstance(event, dict):
                continue
            stamp = event.get("monotonic_s")
            if not finite(stamp) or not -.1 <= now - stamp <= .75:
                continue
            if event.get("kind") == "audio_heartbeat":
                if self.audio_at is None or stamp > self.audio_at:
                    self.audio_at = min(now, stamp)
                self.status = "识别预览" if self.mode == "preview" else "语音已开"
                continue
            word = event.get("keyword")
            if not isinstance(word, str) or KEYWORDS.get(word) != event.get("intent") or event.get("preview_only") is not True:
                continue
            if stamp <= self.last_events.get(word, -math.inf):
                continue
            self.last_events[word] = stamp
            self.status = word
            events.append(event["intent"])
        if self.mode == "preview":
            return None
        # A fresh whitelisted stop is safe even in head/body/recovery modes;
        # it must also stop velocity originally entered from the keyboard.
        if enabled and "stop" in events:
            self.cancel()
            return "stop"
        # Robot timestamp must advance; repeated frames do not refresh readiness.
        if isinstance(state, dict) and finite(state.get("t")) and state.get("t") != self.robot_t:
            if self.robot_t is not None and state["t"] < self.robot_t:
                self.cancel()
                self.pose_until = now + 2.
            self.robot_t, self.robot_at = state["t"], now
        safety = state.get("safety", {}) if isinstance(state, dict) else {}
        safety = safety if isinstance(safety, dict) else {}
        fall = safety.get("fall") or {}
        fall = fall if isinstance(fall, dict) else {"recovery_active": True}
        gravity = safety.get("gravity", [])
        policy = state.get("policy") if isinstance(state, dict) else None
        movement = state.get("move", {}) if isinstance(state, dict) else {}
        limits = movement.get("limited_by", []) if isinstance(movement, dict) else ["invalid"]
        fresh = (finite(state_at) and 0 <= now - state_at <= .5
                 and self.robot_at is not None and 0 <= now - self.robot_at <= .5)
        audio = self.audio_at is not None and 0 <= now - self.audio_at <= .8 and not self.failed
        safe = (enabled and mode == "drive" and fresh and audio
                and safety.get("fallen") is False and safety.get("limp") is False
                and isinstance(gravity, list) and len(gravity) == 3
                and all(finite(v) for v in gravity) and gravity[2] < -.85
                and not fall.get("recovery_active") and not fall.get("recovery_failed")
                and isinstance(limits, list) and all(isinstance(v, str) for v in limits)
                and not set(limits) - {"deadman"}
                and policy in ("walk", "stand", "sit", "rise"))
        if not safe:
            owned = self.owns_walk
            self.cancel()
            return "stop" if owned else None
        if self.owns_walk and now >= self.walk_until:
            self.cancel()
            return "stop"
        if all(abs(v) < .02 for v in velocity) and policy in ("walk", "stand"):
            if self.zero_since is None:
                self.zero_since = now
        else:
            self.zero_since = None
        if now < self.pose_until:
            return None
        # No queued sit after a physical key, mode change, stale state or fault.
        if "sit" in events and policy in ("walk", "stand"):
            self.pending_sit = now
            self.owns_walk = False
            return "stop_for_sit"
        if self.pending_sit is not None:
            if now - self.pending_sit > 5.:
                self.pending_sit = None
            elif self.zero_since is not None and now - self.zero_since >= 2.:
                self.pending_sit = None
                self.pose_until = now + 1.5  # input debounce only; master controls the actual motion
                return "sit"
            return None
        if "rise" in events and policy == "sit":
            self.pose_until = now + 1.5
            return "rise"
        if "walk" in events and policy in ("walk", "stand") and self.zero_since is not None and now - self.zero_since >= 2.:
            self.owns_walk, self.walk_until = True, now + 3.
            return "walk"
        return None

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2.)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2.)
            self.process.stdout.close()
