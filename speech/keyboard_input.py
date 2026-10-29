"""Optional KWS input for the MASTER keyboard loop. No robot socket or RPC.

Only returns walk/backward/stop/sit/rise intents; master owns enabling, recovery,
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
from collections import deque

KEYWORDS = {"前进": "walk", "后退": "backward", "停止": "stop", "坐下": "sit", "起来": "rise"}


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


class SpeechInput:
    def __init__(self, mode, directory, python=None, device="plughw:microducki2s3,0", *,
                 gain=1., threshold=.35, report=None):
        if mode not in ("on", "preview"):
            raise ValueError("SpeechInput is only constructed when speech is enabled")
        self.mode, self.directory = mode, Path(directory).expanduser().resolve()
        self.python = python or str(self.directory / ".venv/bin/python")
        self.device = device
        if not finite(gain) or gain <= 0 or not finite(threshold) or not 0 < threshold <= 1:
            raise ValueError("Invalid speech gain/threshold")
        self.gain, self.threshold, self.report = gain, threshold, report
        self.messages = deque(maxlen=100)
        self.last_keyword_at = None
        self.reason = "等待识别"
        self.recognized = []
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
        self.emit(f"[语音启动] mode={self.mode} dir={self.directory} python={self.python} "
                  f"device={self.device} gain={self.gain:g} threshold={self.threshold:g}")
        self.process = subprocess.Popen(
            [self.python, "-u", str(self.directory / "recognize.py"), "--heartbeat",
             "--source", "alsa", "--device", self.device, "--seconds", "86400",
             "--gain", str(self.gain), "--threshold", str(self.threshold)],
            cwd=str(self.directory), stdout=subprocess.PIPE,
        )
        return self

    def emit(self, message):
        self.messages.append(message)
        if self.report:
            self.report(message)

    def poll(self):
        if self.failed:
            return []
        try:
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError("识别进程退出")
            if not select.select([self.process.stdout], [], [], 0)[0]:
                return []
            # Drain a bounded burst after a busy keyboard tick/Enter recovery;
            # old commands remain subject to the original .75s expiry.
            for _ in range(8):
                chunk = os.read(self.process.stdout.fileno(), 8192)
                if not chunk:
                    raise RuntimeError("识别输出断开")
                self.buffer += chunk
                if not select.select([self.process.stdout], [], [], 0)[0]:
                    break
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
            self.emit("[语音故障] " + self.status)
            if self.process is not None and self.process.poll() is None:
                with suppress(OSError):
                    self.process.terminate()
            return []

    def cancel(self):
        """Any physical key takes ownership; don't later stop its latched motion."""
        self.owns_walk = False
        self.pending_sit = None
        self.zero_since = None

    @staticmethod
    def velocity_for(intent):
        return {"walk": (.10, 0., 0.), "backward": (-.10, 0., 0.)}[intent]

    def decide(self, packets, now, *, manual_input=False, **context):
        self.recognized = []
        for event in packets:
            if isinstance(event, dict) and isinstance(event.get("keyword"), str):
                stamp = event.get("monotonic_s")
                age = f"{now-stamp:.3f}s" if finite(stamp) else "invalid"
                self.emit(f"[语音识别] {event['keyword']} intent={event.get('intent')} age={age}")
        if manual_input:
            if any(isinstance(p, dict) and p.get("keyword") for p in packets):
                self.emit("[语音决策] 本拍键盘输入优先，丢弃语音动作")
            packets = [p for p in packets if isinstance(p, dict) and p.get("kind") == "audio_heartbeat"]
        result = self._decide(packets, now, **context)
        if self.recognized or result is not None:
            self.emit(f"[语音决策] {','.join(self.recognized) or '看门狗/定时检查'} -> "
                      f"{result or '不执行'}；{self.reason}")
        return result

    def _decide(self, packets, now, *, enabled, mode, state, state_at, velocity):
        """At most ONE intent per tick. No enable, restart, relax or skill timer."""
        def finish(intent, reason):
            self.reason = reason
            return intent
        events = []
        for event in packets:
            if not isinstance(event, dict):
                continue
            stamp = event.get("monotonic_s")
            if not finite(stamp) or not -.1 <= now - stamp <= .75:
                if event.get("keyword"):
                    self.emit("[语音忽略] 事件过期/时间戳无效（可能主循环正在Enter起身）；请重说")
                continue
            if event.get("kind") == "audio_heartbeat":
                if self.audio_at is None or stamp > self.audio_at:
                    self.audio_at = min(now, stamp)
                if self.last_keyword_at is None or now - self.last_keyword_at > 3.:
                    self.status = "识别预览" if self.mode == "preview" else "语音已开"
                continue
            word = event.get("keyword")
            if not isinstance(word, str) or KEYWORDS.get(word) != event.get("intent") or event.get("preview_only") is not True:
                if word:
                    self.emit("[语音忽略] 词表/意图不匹配；请确认current指向新版语音目录")
                continue
            if stamp <= self.last_events.get(word, -math.inf):
                self.emit("[语音忽略] 重复事件")
                continue
            self.last_events[word] = stamp
            self.status = word
            self.last_keyword_at = now
            self.recognized.append(word)
            events.append(event["intent"])
        if self.mode == "preview":
            return finish(None, "preview仅识别，不驱动动作")
        # A fresh whitelisted stop is safe even in head/body/recovery modes;
        # it must also stop velocity originally entered from the keyboard.
        if enabled and "stop" in events:
            self.cancel()
            return finish("stop", "识别停止，清零速度")
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
            reason = ("未使能，请人工按Enter并等待起身完成" if not enabled else
                      f"当前模式{mode}，请返回行走模式" if mode != "drive" else
                      "robot.state过期/未收到连续新状态" if not fresh else
                      "音频心跳过期/识别故障" if not audio else
                      f"保护/姿态/策略不满足：policy={policy} gravity={gravity} "
                      f"fallen={safety.get('fallen')} limp={safety.get('limp')} limited_by={limits}")
            return finish("stop" if owned else None, reason)
        if self.owns_walk and now >= self.walk_until:
            self.cancel()
            return finish("stop", "语音行走3秒限时到，自动停车")
        if all(abs(v) < .02 for v in velocity) and policy in ("walk", "stand"):
            if self.zero_since is None:
                self.zero_since = now
        else:
            self.zero_since = None
        if now < self.pose_until:
            return finish(None, f"坐起输入去抖，尚余{self.pose_until-now:.1f}秒")
        # No queued sit after a physical key, mode change, stale state or fault.
        if "sit" in events and policy in ("walk", "stand"):
            self.pending_sit = now
            self.owns_walk = False
            return finish("stop_for_sit", "已识别坐下：先停车等待零速至少2秒")
        if self.pending_sit is not None:
            if now - self.pending_sit > 5.:
                self.pending_sit = None
            elif self.zero_since is not None and now - self.zero_since >= 2.:
                self.pending_sit = None
                self.pose_until = now + 1.5  # input debounce only; master controls the actual motion
                return finish("sit", "零速等待完成，提交master坐下请求")
            return finish(None, "坐下请求等待零速至少2秒")
        if "rise" in events and policy == "sit":
            self.pose_until = now + 1.5
            return finish("rise", "当前为sit，提交master起立请求")
        moves = [intent for intent in events if intent in ("walk", "backward")]
        if len(set(moves)) > 1:
            self.cancel()
            return finish("stop", "同批前进/后退冲突，停车")
        if moves and policy in ("walk", "stand") and self.zero_since is not None and now - self.zero_since >= 2.:
            self.owns_walk, self.walk_until = True, now + 3.
            return finish(moves[-1], "提交速度，最多3秒；实际运动仍由master控制")
        reason = (f"当前policy={policy}，请先起来并站稳" if moves and policy not in ("walk", "stand") else
                  "未满足零速等待2秒，请先停止、站稳后重说" if moves else
                  f"当前policy={policy}，无需重复坐下/起来或动作尚未完成")
        return finish(None, reason)

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
