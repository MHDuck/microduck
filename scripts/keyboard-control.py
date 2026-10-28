#!/usr/bin/env python3
import argparse
from collections import deque
from contextlib import ExitStack, closing, suppress
import csv
from datetime import datetime
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import random
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import unicodedata

try:
    import termios
    import tty
except ImportError:  # Unit-testable on Windows; the interactive program remains Linux-only.
    termios = None
    tty = None


# Same order as duck_control::model::JOINT_IDS.
JOINT_IDS = (20, 21, 22, 23, 24, 30, 31, 32, 33, 34, 10, 11, 12, 13, 14)

DEFAULT_WALK_EMOTIONS = "laughing"
DEFAULT_VOICE_GAP_S = 0.0
AUTO_STANDUP_TIMEOUT_S = 5.0
STANDUP_FAILED = "起身失败，摆好姿势后按 Enter 重试"


class VoiceInterrupted(RuntimeError):
    """The robot stopped walking while a mouth animation was in progress."""


def load_voice_module():
    script = Path(__file__).with_name("duck-voice.py")
    spec = importlib.util.spec_from_file_location("duck_voice_runtime", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载语音脚本：{script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def is_walking(enabled, velocity):
    return enabled and any(abs(component) > 1e-3 for component in velocity)


def choose_emotion(emotions, previous, rng):
    choices = [name for name in emotions if name != previous] or list(emotions)
    return rng.choice(choices)


class WalkingVoice:
    """Continuously play configured voice cues while movement is commanded."""

    def __init__(
        self,
        socket_path,
        emotions,
        gap_min,
        gap_max,
        *,
        enabled=False,
        sound_only=False,
        voice_module=None,
        rng=None,
    ):
        self.voice = voice_module or load_voice_module()
        unknown = sorted(set(emotions) - set(self.voice.EMOTIONS))
        if unknown:
            raise ValueError(f"未知语音情绪：{', '.join(unknown)}")
        self.socket_path = Path(socket_path)
        self.emotions = tuple(emotions)
        self.gap_min = gap_min
        self.gap_max = gap_max
        self.sound_only = sound_only
        self.rng = rng or random.Random()
        self._condition = threading.Condition()
        self._enabled = enabled
        self._moving = False
        self._stopped = False
        self._last_emotion = None
        self._status = "语音开启" if enabled else "语音关闭"
        self._thread = threading.Thread(target=self._run, name="walking-voice", daemon=True)
        self._thread.start()

    @property
    def enabled(self):
        with self._condition:
            return self._enabled

    @property
    def status(self):
        with self._condition:
            return self._status

    def toggle(self):
        with self._condition:
            self._enabled = not self._enabled
            self._status = "语音开启" if self._enabled else "语音关闭"
            self._condition.notify_all()
            return self._enabled

    def set_moving(self, moving):
        with self._condition:
            if self._moving != moving:
                self._moving = moving
                self._condition.notify_all()

    def _active(self):
        return self._enabled and self._moving and not self._stopped

    def _sleep_while_active(self, seconds):
        with self._condition:
            interrupted = self._condition.wait_for(
                lambda: not self._active(), timeout=max(0.0, seconds)
            )
            if interrupted:
                raise VoiceInterrupted

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopped or self._active())
                if self._stopped:
                    return
            emotion = choose_emotion(self.emotions, self._last_emotion, self.rng)
            self._last_emotion = emotion
            cue = self.voice.EMOTIONS[emotion]
            try:
                with self.voice.RobotdClient(self.socket_path) as client:
                    client.hello()
                    self.voice.play_cue(
                        client,
                        cue,
                        with_mouth=not self.sound_only,
                        rate_hz=self.voice.DEFAULT_RATE_HZ,
                        mouth_scale=0.85,
                        sleep_fn=self._sleep_while_active,
                    )
                    if self.sound_only:
                        self._sleep_while_active(cue.duration_s)
                with self._condition:
                    self._status = f"刚播放 {emotion}"
                self._sleep_while_active(self.rng.uniform(self.gap_min, self.gap_max))
            except VoiceInterrupted:
                continue
            except Exception as error:
                with self._condition:
                    self._status = f"语音错误: {error}"
                try:
                    self._sleep_while_active(1.0)
                except VoiceInterrupted:
                    pass

    def close(self):
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
        self._thread.join(timeout=3.0)


class RobotdDiagnostics:
    def __init__(self, socket_path):
        self.messages = queue.Queue(maxsize=100)
        self.history = deque(maxlen=100)
        self.verbose = False
        self.runtime_verbose = False
        self.recovery_failed = False
        self.last_error = ""
        self._shown_error = ""
        self._previous = ""
        self._process = None
        self._thread = None
        self._stop = threading.Event()
        self._state = (None, 0.0)
        self._health = {}
        self._health_error = ""
        self._phase = None
        self._fall_config = None
        self._models = {}
        self._policy = None
        if socket_path == "/run/robotd.sock":
            try:
                self._process = subprocess.Popen(
                    ["journalctl", "--unit=robotd", "--follow", "--lines=20", "--output=json", "--no-pager"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                )
                self._thread = threading.Thread(target=self._follow, name="robotd-errors", daemon=True)
                self._thread.start()
            except OSError as error:
                self.report(f"无法读取 robotd 日志：{error}")
        self._state_thread = threading.Thread(
            target=self._watch_state, args=(socket_path,), name="robotd-state", daemon=True,
        )
        self._state_thread.start()

    def _watch_state(self, socket_path):
        while not self._stop.is_set():
            try:
                with socket.socket(socket.AF_UNIX) as stream:
                    stream.settimeout(1.0)
                    stream.connect(socket_path)
                    for ident, method, params in (
                        (1, "hello", {"api_version": 18}),
                        (2, "robot.subscribe", {"hz": 10}),
                    ):
                        stream.sendall((json.dumps({
                            "jsonrpc": "2.0", "id": ident, "method": method, "params": params,
                        }) + "\n").encode())
                    buffer = b""
                    next_health = 0.0
                    while not self._stop.is_set():
                        if time.monotonic() >= next_health:
                            stream.sendall(b'{"jsonrpc":"2.0","id":3,"method":"robot.health","params":{}}\n')
                            if self.runtime_verbose:
                                stream.sendall(b'{"jsonrpc":"2.0","id":2,"method":"robot.subscribe","params":{"hz":10}}\n')
                            next_health = time.monotonic() + 1.0
                        if not select.select([stream], [], [], 0.2)[0]:
                            continue
                        chunk = stream.recv(65536)
                        if not chunk:
                            raise ConnectionError("robotd 状态流已断开")
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            message = json.loads(line)
                            if not isinstance(message, dict):
                                raise ValueError("robotd 状态响应格式错误")
                            result = message.get("result") or {}
                            if not isinstance(result, dict):
                                raise ValueError("robotd 状态结果格式错误")
                            if "error" in message or result.get("accepted") is False:
                                raise RuntimeError(f"状态订阅失败：{message}")
                            if message.get("method") == "robot.state":
                                self._accept_state(message["params"])
                            elif message.get("id") == 2:
                                self._models = result
                            elif message.get("id") == 3:
                                self._health = result
                                self._health_error = "" if result.get("healthy") else str(
                                    result.get("reason") or "robotd 健康检查未通过"
                                )
                                if self._health_error:
                                    self.report(self._health_error)
            except (OSError, ValueError, RuntimeError) as error:
                self._state = (None, 0.0)
                self._health = {}
                self._health_error = str(error)
                self._phase = self._fall_config = None
                self._models = {}
                self._policy = None
                if not self._stop.is_set():
                    self.report(f"实时状态未连接：{error}；正在重连")
                    self._stop.wait(0.5)

    def _accept_state(self, state):
        if not isinstance(state, dict):
            raise ValueError("robotd 状态帧格式错误")
        self._state = (state, time.monotonic())
        policy = (state.get("policy"), self.model_name(state.get("policy")))
        if policy != self._policy:
            previous = f"{self._policy[0]} ({self._policy[1]})" if self._policy else "未订阅"
            self._policy = policy
            self.report(f"策略/模型切换：{previous} -> {policy[0]}；{self.runtime_status()}",
                        fault=False, runtime=True)
        if self.last_error == "robotd 未连接，按 ENTER，启动 DUCK":
            self.last_error = self._shown_error = ""
        safety = state.get("safety", {})
        fall = safety.get("fall") or {}
        limits = state.get("move", {}).get("limited_by", [])
        config = tuple(fall.get(key) for key in (
            "protection_enabled", "gravity_z_threshold", "debounce_ms", "limp_enabled",
        ))
        if config != self._fall_config:
            self._fall_config = config
            if fall:
                self.report(
                    f"实时倾倒配置：锁定={'开' if config[0] else '关'}，阈值 z>{config[1]}，"
                    f"持续 {config[2]} ms，预测降力={'开' if config[3] else '关'}", fault=False,
                )
            else:
                self.report("robotd 未上报倾倒计时和配置，请更新二进制；重力与倒地状态仍可查看", fault=False)
        fault = False
        if "overcurrent" in limits:
            phase, fault = "过流保护已锁定", True
        elif fall.get("recovery_failed"):
            phase, fault = STANDUP_FAILED, True
        elif "fall_protection" in limits:
            phase, fault = "倾倒保护已锁定", True
        elif fall.get("recovery_active"):
            phase = "StandUp 起身中，暂缓倾倒锁定"
        elif fall.get("imu_ready") is False:
            phase = "IMU 未就绪，倾倒判定暂停"
        elif safety.get("limp") or state.get("policy") == "limp_fall":
            phase = "预测倾倒触发，正在降力"
        elif state.get("policy") == "limp_pose":
            phase = "降力结束，正在回 HOME"
        elif safety.get("fallen"):
            phase = "已确认倒地，尚未锁定卸力"
        elif fall.get("elapsed_ms", 0) > 0:
            phase = "倾角超限，正在累计倾倒时间"
        else:
            phase = "未检测到倒地"
        if phase != self._phase:
            self._phase = phase
            self.report(f"{phase}；{self.state_status()}", fault=fault)

    def state_status(self):
        state, received = self._state
        if state is None:
            return "实时状态未连接" + (f"：{self._health_error}" if self._health_error else "")
        age = time.monotonic() - received
        if age > 1.0:
            return f"实时状态已过期 {age:.1f}s" + (f"：{self._health_error}" if self._health_error else "")
        safety = state.get("safety", {})
        fall = safety.get("fall") or {}
        gravity = safety.get("gravity") or []
        gz = f"{gravity[2]:+.2f}" if (
            len(gravity) == 3 and isinstance(gravity[2], (int, float)) and math.isfinite(gravity[2])
        ) else "无效"
        ready = fall.get("imu_ready", (self._health.get("imu") or {}).get("ready"))
        imu = "未就绪" if ready is False else "未知" if ready is None else (
            "新" if fall.get("imu_fresh") else "重复" if fall else "就绪"
        )
        result = f"实时{state.get('policy', '?')} IMU{imu} z={gz} 倒地{'是' if safety.get('fallen') else '否'}"
        if fall:
            result += (f" 锁{'开' if fall['protection_enabled'] else '关'}"
                       f" {fall['elapsed_ms'] / 1000:.1f}/{fall['debounce_ms'] / 1000:.1f}s")
            if fall.get("recovery_active"):
                result += " 起身暂缓"
        else:
            result += " 锁定/计时未知(旧robotd)"
        limits = state.get("move", {}).get("limited_by", [])
        if "overcurrent" in limits:
            result = "【过流已锁定】 " + result
        elif "fall_protection" in limits:
            result = "【倾倒已锁定】 " + result
        return result

    @staticmethod
    def brief_error(message):
        lower = message.lower()
        if ("standup" in lower or "stand-up" in lower or "起身失败" in message) and any(
            word in lower for word in ("未完成", "未收到", "timeout", "timed out", "already attempted", "起身失败")
        ):
            return STANDUP_FAILED
        if "fall protection latched" in lower or "倾倒保护已锁定" in message:
            return "倾倒保护了"
        if "overcurrent protection latched" in lower or "过流保护已锁定" in message:
            return "电机过流保护了"
        motor = re.search(r"\b(?:servo|motor|ping)\s+(?:id[=: ]*)?(\d+)\b|\b(?:read|write) reg \d+ on (\d+)\b", lower)
        if motor:
            return f"{next(group for group in motor.groups() if group)} 电机无响应"
        if "bno085" in lower:
            if "address must" in lower:
                return "BNO085 地址配置错误"
            return "BNO085 数据中断" if "fresh" in lower or "stale" in lower else "BNO085 连接异常"
        if "imu" in lower and any(word in lower for word in ("failed", "stopped", "未就绪")):
            return "IMU 未就绪或连接异常"
        if "onnx" in lower or "policy unavailable" in lower or "未配置 standup" in lower:
            missing = any(word in lower for word in ("no such file", "not found", "does not exist", "未配置"))
            return "ONNX 文件缺失" if missing else "模型加载失败"
        if any(word in lower for word in ("toml", "config", "配置")):
            return "配置文件错误"
        if "motor bus" in lower or "bus read fail" in lower or "servo sync_read" in lower:
            return "电机通信异常"
        if "control loop has not completed" in lower:
            return "robotd 正在启动"
        if "control loop" in lower:
            return "控制循环异常"
        if any(word in lower for word in ("实时状态未连接", "通信中断", "robotd 未连接", "状态流已断开")):
            return "robotd 未连接，按 ENTER，启动 DUCK"
        if "journalctl" in lower or "无法读取 robotd 日志" in message:
            return "robotd 日志不可用"
        return None

    def brief_status(self, enabled):
        state, received = self._state
        if state is None:
            status = "robotd 未连接，按 ENTER，启动 DUCK"
        elif time.monotonic() - received > 1.0:
            status = "机器人状态更新中断"
        else:
            safety = state.get("safety", {})
            fall = safety.get("fall") or {}
            movement = state.get("move", {})
            limits = movement.get("limited_by", [])
            policy = state.get("policy")
            if "overcurrent" in limits:
                return "电机过流保护了"
            elif self.recovery_failed or fall.get("recovery_failed"):
                return "起身失败，卸力请求失败" if "卸力请求失败" in self.last_error else STANDUP_FAILED
            elif "fall_protection" in limits:
                return "倾倒保护了"
            elif policy == "protection":
                status = "保护已触发"
            elif fall.get("recovery_active") or policy == "standup":
                status = "起身中"
            elif fall.get("imu_ready", (self._health.get("imu") or {}).get("ready")) is False:
                status = "IMU 未就绪"
            elif safety.get("limp") or policy == "limp_fall":
                status = "倾倒降力中"
            elif safety.get("fallen"):
                status = "倒地中"
            elif fall.get("elapsed_ms", 0) > 0:
                status = "倾倒中"
            elif policy in ("walk", "roller"):
                moving = any(abs(value) > 0.001 for value in movement.get("applied", []))
                status = ("轮式行走中" if policy == "roller" else "行走中") if moving else "站立中"
            else:
                status = {
                    "stand": "站立中", "homing": "准备站立", "limp_pose": "恢复站姿中",
                    "sit": "坐姿中", "rise": "起身中",
                    "kick_left": "左踢腿中", "kick_right": "右踢腿中",
                    "ground_pick": "拾取中", "roulade": "翻滚中",
                    "held": "控制暂停" if enabled else "未启用，按 ENTER，启动 DUCK",
                }.get(policy, "动作执行中")
        return self.last_error or status

    def model_name(self, policy):
        if policy in ("held", "protection", "homing", "limp_fall", "limp_pose"):
            return "无（当前未执行运动模型）"
        key = {"standup": "stand", "sit": "sitstand", "rise": "sitstand", "roller": "walk"}.get(policy, policy)
        return self._models.get(key) or "未上报"

    def runtime_status(self):
        state, received = self._state
        if state is None or time.monotonic() - received > 1.0:
            return self.state_status()
        movement = state.get("move", {})
        policy = state.get("policy")
        return (f"t={state.get('t', 0):.3f}s 策略={policy}，ONNX={self.model_name(policy)}；"
                f"指令={movement.get('requested')}，应用={movement.get('applied')}；{self.state_status()}")

    def report(self, message, *, fault=True, summary=None, runtime=False):
        message = re.sub(r"\x1b\[[0-9;]*m", "", message).strip()
        if not runtime:
            message = re.sub(r"^\d{4}-\d\d-\d\dT\S+\s+", "", message)
        if not message or message == self._previous:
            return
        self._previous = message
        if fault:
            brief = summary or self.brief_error(message)
            if brief == STANDUP_FAILED:
                self.recovery_failed = True
            if brief == "robotd 正在启动":
                fault = False
            elif brief == "robotd 未连接，按 ENTER，启动 DUCK" and self.last_error:
                pass
            elif brief == "电机通信异常" and re.match(r"\d+ 电机", self.last_error):
                pass
            elif brief or not self.last_error:
                self.last_error = brief or "robotd 出错，按 C 查看详情"
        with suppress(queue.Full):
            self.messages.put_nowait((message, fault, runtime))

    def _follow(self):
        context_pid = None
        context_left = 0
        for line in self._process.stdout:
            try:
                entry = json.loads(line)
            except ValueError:
                self.report(f"journalctl：{line.strip()}")
                continue
            message = str(entry.get("MESSAGE", ""))
            plain = re.sub(r"\x1b\[[0-9;]*m", "", message)
            fall_event = next((label for marker, label in (
                ("fall protection settings", "当前生效的倾倒保护配置"),
                ("fall pending", "倾角超限，正在累计倾倒时间"),
                ("fall detected", "IMU 已确认倒地"),
                ("fall angle recovered", "倾角已恢复，计时清零；已锁定的保护仍保持"),
                ("falling — going limp to land soft", "预测倾倒触发，正在降力"),
                ("landed — posing back to standing", "降力结束，正在回 HOME"),
                ("posed — resuming the selected policy", "HOME 过渡结束，恢复原策略"),
                ("limp-fall abandoned", "预测降力流程中断"),
            ) if marker in plain), None)
            if fall_event is not None:
                context_left = 0
                self.report(f"{fall_event}：{plain}", fault=False)
                continue
            error = re.search(
                r"\bERROR\b|\berror[:=]|\bpanic(?:ked)?\b|\bfailed\b|\bfailure\b|"
                r"\brefused\b|\bnot found\b|\bno such file\b|\bprotection latched\b",
                plain, re.IGNORECASE,
            ) or str(entry.get("PRIORITY", "6")) in ("0", "1", "2", "3")
            if error:
                context_pid, context_left = entry.get("_PID"), 6
                if "fall protection latched" in plain:
                    message = f"倾倒保护已锁定，已请求全身卸力：{plain}"
                elif "overcurrent protection latched" in plain:
                    message = f"过流保护已锁定，已请求全身卸力：{plain}"
                self.report(message)
            elif context_left and entry.get("_PID") == context_pid and not re.search(
                r"\b(?:TRACE|DEBUG|INFO|WARN)\b", plain,
            ):
                context_left -= 1
                self.report(message)
            else:
                context_left = 0
                self.report(message, fault=False, runtime=True)

    def display(self):
        while True:
            try:
                message, fault, runtime = self.messages.get_nowait()
            except queue.Empty:
                return
            self.history.append((message, fault, runtime))
            if self.runtime_verbose or (self.verbose and not runtime):
                print(f"\r\033[K[robotd {'故障' if fault else '状态'}] {message}", flush=True)
            elif fault and self.last_error != self._shown_error:
                self._shown_error = self.last_error
                print(f"\r\033[K{self.last_error}", end="", flush=True)

    def set_verbose(self, enabled):
        self.display()
        self.verbose = enabled
        if enabled:
            for message, fault, runtime in self.history:
                if not runtime:
                    print(f"\r\033[K[robotd {'故障' if fault else '状态'}] {message}", flush=True)

    def toggle_runtime(self):
        self.display()
        self.runtime_verbose = not self.runtime_verbose
        print(f"\r\033[K运行日志已{'开启' if self.runtime_verbose else '关闭'}（行走模式按 L 切换）。", flush=True)
        if self.runtime_verbose:
            for message, fault, _ in self.history:
                print(f"\r\033[K[robotd {'故障' if fault else '状态'}] {message}", flush=True)
            print(f"[运行] {self.runtime_status()}", flush=True)

    def clear(self):
        self.display()
        self._previous = self.last_error = self._shown_error = ""
        self.recovery_failed = False

    def close(self):
        self._stop.set()
        self._state_thread.join(timeout=2)
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
            self._thread.join(timeout=1)
            self._process.stdout.close()
        self.display()


def main():
    parser = argparse.ArgumentParser(description="通过本地 robotd 接口控制机器人（Linux/SSH 终端）。")
    parser.add_argument("--socket", default="/run/robotd.sock",
                        help="默认 socket 的 Enter/退出会重启/停止 robotd；自定义 socket 只请求起身/卸力。")
    parser.add_argument("--log-dir", default="logs", help="电流 CSV 日志目录，默认当前目录下的 logs。")
    parser.add_argument("--walk-voice", action="store_true",
                        help="启动时开启行走连续发声；运行中按 H 可随时切换。")
    parser.add_argument("--voice-emotions", default=DEFAULT_WALK_EMOTIONS,
                        help="情绪列表，逗号分隔；默认只播放五秒大笑。")
    parser.add_argument("--voice-gap-min", type=float, default=DEFAULT_VOICE_GAP_S,
                        help="两段声音的最短间隔秒数，默认 0。")
    parser.add_argument("--voice-gap-max", type=float, default=DEFAULT_VOICE_GAP_S,
                        help="两段声音的最长间隔秒数，默认 0。")
    parser.add_argument("--voice-sound-only", action="store_true",
                        help="行走发声时只播放声音，不控制嘴部。")
    args = parser.parse_args()
    if termios is None or tty is None:
        parser.error("keyboard-control.py 只能在 Linux/WSL/板端终端运行")
    if not sys.stdin.isatty():
        parser.error("需要交互终端；SSH 登录后运行这个文件。")
    if args.voice_gap_min < 0 or args.voice_gap_max < args.voice_gap_min:
        parser.error("语音间隔必须满足 0 <= --voice-gap-min <= --voice-gap-max")
    emotions = tuple(name.strip() for name in args.voice_emotions.split(",") if name.strip())
    if not emotions:
        parser.error("--voice-emotions 至少需要一个情绪")

    walking_voice = WalkingVoice(
        args.socket,
        emotions,
        args.voice_gap_min,
        args.voice_gap_max,
        enabled=args.walk_voice,
        sound_only=args.voice_sound_only,
    )

    turns = {
        "q": 0.30, "e": -0.30,
    }
    skills = {"g": "ground_pick", "z": "kick_left", "v": "kick_right", "x": "roulade"}
    head_keys = {
        "w": ("head_pitch", -0.10), "s": ("head_pitch", 0.10),
        "a": ("head_yaw", 0.10), "d": ("head_yaw", -0.10),
        "i": ("neck_pitch", 0.10), "k": ("neck_pitch", -0.10),
        "j": ("head_roll", -0.10), "l": ("head_roll", 0.10),
    }
    pose_keys = {
        "w": ("z", 0.001, -0.025, 0.010), "s": ("z", -0.001, -0.025, 0.010),
        "i": ("pitch", 0.02, -0.2618, 0.2618), "k": ("pitch", -0.02, -0.2618, 0.2618),
        "j": ("roll", -0.02, -0.2618, 0.2618), "l": ("roll", 0.02, -0.2618, 0.2618),
    }
    with closing(RobotdDiagnostics(args.socket)) as diagnostics, ExitStack() as connection_scope:
        with ExitStack() as reader_scope:
            connection = reader = None
            connected = False
            latest_state = None
            request_id = 0

            def disconnected(error):
                nonlocal connected, enabled, velocity, roll_until, rt_until, lt_until
                nonlocal mouth_active, wheee_active
                connected = enabled = False
                velocity = (0.0, 0.0, 0.0)
                roll_until = rt_until = lt_until = 0.0
                mouth_active = wheee_active = False
                walking_voice.set_moving(False)
                diagnostics.report(f"robotd 通信中断：{error}；按 Enter 重启恢复")

            def send(method, params, ident=None):
                if ident is None and not connected:
                    return
                message = {"jsonrpc": "2.0", "method": method, "params": params}
                if ident is not None:
                    message["id"] = ident
                try:
                    if connection is None:
                        raise ConnectionError("robotd 未连接，请按 Enter 重试")
                    connection.sendall((json.dumps(message) + "\n").encode())
                except OSError as error:
                    disconnected(error)
                    if ident is not None:
                        raise

            def read_message():
                nonlocal latest_state
                line = reader.readline()
                if not line:
                    raise ConnectionError("robotd 已断开连接")
                response = json.loads(line)
                if response.get("method") == "robot.state":
                    latest_state = response["params"]
                return response

            def call(method, params, *, allow_refusal=False):
                nonlocal request_id
                request_id += 1
                try:
                    send(method, params, request_id)
                    deadline = time.monotonic() + 3.0
                    while True:
                        response = read_message()
                        if response.get("id") == request_id:
                            break
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"等待 {method} 超时")
                except OSError as error:
                    disconnected(error)
                    if allow_refusal or method == "robot.stop":
                        return None
                    raise
                if "error" in response:
                    raise RuntimeError(f"robotd 返回异常：{response}")
                result = response.get("result", {})
                if result.get("accepted") is False:
                    if allow_refusal:
                        reason = result.get("reason", "未知原因")
                        diagnostics.report(f"{method} 被拒绝：{reason}",
                                           summary=diagnostics.brief_error(reason) or "操作被拒绝")
                        return None
                    raise RuntimeError(result.get("reason", "robotd 拒绝了指令"))
                return result

            def connect():
                nonlocal connection, reader, connected, latest_state
                connected = False
                reader_scope.close()
                connection_scope.close()
                connection = connection_scope.enter_context(socket.socket(socket.AF_UNIX))
                connection.settimeout(1.0)
                connection.connect(args.socket)
                reader = reader_scope.enter_context(connection.makefile("rb"))
                latest_state = None
                call("hello", {"api_version": 18})
                connected = True

            try:
                connect()
                call("robot.stop", {})
            except OSError as error:
                connected = False
                diagnostics.report(f"robotd 未连接：{error}")
                print("robotd 未连接，按 Enter 重启并起身。")
            print("Enter：重启 robotd，等待 IMU 就绪，调用 StandUp 从地上起身；每次按下都重启。")
            print("W/S 每次增减 vx 0.10 m/s（范围 -0.40～+0.40），松开保持；长按会连续调速。")
            print("A/D 每次增减 vy 0.10 m/s（范围 -0.40～+0.40），松开保持；长按会连续调速。")
            print("Q/E 左右转（松开约 0.3 秒停止转向）  空格速度归零  Esc/Ctrl-C 退出")
            print("T 坐下/站起切换（Enter 启用后使用，切换前速度归零；两次点按请间隔 1 秒以上）。")
            print("C 打开/关闭详细日志：速度、IMU、倾倒计时、策略、电流及原始错误；连接后同时记录电流 CSV。")
            print("L 行走模式下打开/关闭运行日志：ONNX 文件、策略切换、起身/保护和 robotd 原始日志；不开始电流记录。")
            print("Y 头部模式 / B 身体姿态模式，再按返回行走；切换时速度归零。")
            print("头部：W/S 俯仰、A/D 偏航、I/K 颈部俯仰、J/L 侧倾；身体：W/S 高度、I/K 俯仰、J/L 侧倾。")
            print("姿态按键逐次调整，松开保持；空格同时将当前姿态归零。")
            print("G = A 拾取  Z/V = LB/RB 踢腿  X 翻滚（长按连续）  R/F = RT/LT 张嘴与声音。")
            print("M 长按 3 秒切换步行/轮式  P 长按 2 秒坐下并关机；单次按住只触发一次。")
            print("终端以按键重复估算长按和松开；R/F/X 松开约 0.3 秒释放，开关类按键两次点按请间隔 1 秒以上。")
            print("H 开启/关闭行走随机讲话；只在速度指令非零时发声，R/F 手动发声期间暂停。")
            print("退出会卸力，请先托住机器人；默认连接还会停止 robotd 服务。")
            print("主界面只显示当前动作和简短故障；C/L 分别开关，未连接时也可查看；头部/身体模式的 L 仍调整侧倾。")
            terminal = sys.stdin.fileno()
            saved = termios.tcgetattr(terminal)
            enabled = False
            mode = "drive"
            head = dict.fromkeys(("neck_pitch", "head_pitch", "head_yaw", "head_roll"), 0.0)
            pose = dict.fromkeys(("z", "pitch", "roll"), 0.0)
            last_keys = {}
            held_since = {}
            hold_sent = set()
            roll_until = rt_until = lt_until = 0.0
            roll_accepted = False
            wheee_active = False
            mouth_active = False
            shutdown_requested = False
            velocity = (0.0, 0.0, 0.0)
            expires = 0.0
            current_socket = None
            current_log = None
            current_status = ""
            current_buffer = b""
            current_peak = 0.0
            current_received = 0.0
            previous_tick = None
            missing_ticks = 0
            next_display = 0.0
            next_runtime = 0.0
            recovery_started = None

            def recovery_keys():
                if select.select([terminal], [], [], 0)[0]:
                    for key in os.read(terminal, 64).decode(errors="ignore").lower():
                        if key in ("\x1b", "\x03"):
                            raise KeyboardInterrupt
                        if key == "l" and time.monotonic() - last_keys.get(key, -math.inf) > 1.0:
                            diagnostics.toggle_runtime()
                        last_keys[key] = time.monotonic()

            for signum in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(signum, lambda *_: sys.exit(0))
            try:
                tty.setcbreak(terminal)
                while True:
                    diagnostics.display()
                    inputs = [terminal] + ([current_socket] if current_socket is not None else [])
                    readable, _, _ = select.select(inputs, [], [], 0.05)
                    if terminal in readable:
                        keys = os.read(terminal, 64)
                        if not keys:
                            break
                        for key in keys.decode(errors="ignore").lower():
                            if key == "\r":
                                key = "\n"
                            now = time.monotonic()
                            # shortcut: terminals have no key-up events; allow the initial repeat delay.
                            pressed = now - last_keys.get(key, -math.inf) > 1.0
                            last_keys[key] = now
                            if key in ("\x1b", "\x03"):
                                return 0
                            if key == "\n" and pressed:
                                enabled = False
                                recovery_started = None
                                walking_voice.set_moving(False)
                                velocity = (0.0, 0.0, 0.0)
                                expires = 0.0
                                roll_until = rt_until = lt_until = 0.0
                                roll_accepted = mouth_active = wheee_active = False
                                mode = "drive"
                                head = dict.fromkeys(head, 0.0)
                                pose = dict.fromkeys(pose, 0.0)
                                held_since.clear()
                                hold_sent.clear()
                                if current_socket is not None:
                                    current_socket.close()
                                    current_socket = None
                                    current_log.close()
                                    current_log = None
                                    current_status = ""
                                    print(f"\r\033[K电流记录已保存：{log_path}", flush=True)
                                try:
                                    reader_scope.close()
                                    connection_scope.close()
                                    connected = False
                                    if args.socket == "/run/robotd.sock":
                                        print("\r\033[K正在重启 robotd……", flush=True)
                                        subprocess.run(["systemctl", "restart", "robotd"], check=True, timeout=30,
                                                       capture_output=True, text=True)
                                    deadline = time.monotonic() + 20.0
                                    health = {}
                                    while time.monotonic() < deadline:
                                        diagnostics.display()
                                        recovery_keys()
                                        if not connected:
                                            try:
                                                connect()
                                            except OSError:
                                                time.sleep(0.1)
                                                continue
                                        health = call("robot.health", {})
                                        if str(health.get("reason", "")).startswith("policy unavailable:"):
                                            raise RuntimeError(health["reason"])
                                        if health.get("healthy") and (health.get("imu") or {}).get("ready"):
                                            break
                                        time.sleep(0.1)
                                    else:
                                        raise RuntimeError(f"robotd/IMU 未就绪：{health}")
                                    identity = call("robot.subscribe", {"hz": 20})
                                    if identity.get("unavailable") or not identity.get("stand"):
                                        raise RuntimeError(identity.get("unavailable") or "未配置 StandUp 模型")
                                    call("robot.stop", {})
                                    send("robot.head", head)
                                    send("robot.pose", {"active": False})
                                    call("robot.enable", {"on": True, "toggle": False, "stand_up": True})
                                    print("\r\033[K起身中……", flush=True)
                                    diagnostics.report(f"StandUp 模型：{identity['stand']}，最长 12 秒", fault=False)
                                    requested_at = time.monotonic()
                                    deadline = time.monotonic() + 14.0
                                    saw_standup = False
                                    next_progress = requested_at
                                    while time.monotonic() < deadline:
                                        diagnostics.display()
                                        recovery_keys()
                                        read_message()
                                        policy = (latest_state or {}).get("policy")
                                        if policy == "protection" and (saw_standup or time.monotonic() - requested_at > 1.0):
                                            if ((latest_state or {}).get("safety", {}).get("fall") or {}).get("recovery_failed"):
                                                raise RuntimeError(STANDUP_FAILED)
                                            raise RuntimeError("保护再次触发，请排除故障后按 Enter 重试")
                                        saw_standup |= policy == "standup"
                                        if saw_standup and policy == "stand":
                                            break
                                        if (diagnostics.verbose or diagnostics.runtime_verbose) and time.monotonic() >= next_progress:
                                            gravity = (latest_state or {}).get("safety", {}).get("gravity")
                                            print(f"\r\033[K起身 {time.monotonic() - requested_at:.1f}s："
                                                  f"策略={policy}，重力={gravity}（直立时 z 接近 -1）",
                                                  end="", flush=True)
                                            if diagnostics.runtime_verbose:
                                                print(f"\r\033[K[运行] {diagnostics.runtime_status()}", flush=True)
                                            next_progress = time.monotonic() + 1.0
                                    else:
                                        safety = (latest_state or {}).get("safety", {})
                                        raise RuntimeError(
                                            f"未收到 StandUp 完成状态：策略={(latest_state or {}).get('policy')}，"
                                            f"重力={safety.get('gravity')}，倒地={safety.get('fallen')}；"
                                            "将请求卸力，具体未满足条件见 robotd 超时日志"
                                        )
                                    connect()
                                    enabled = True
                                    diagnostics.clear()
                                    print("\r\033[K起身完成，可继续行走。", flush=True)
                                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                                    with suppress(OSError, ValueError, RuntimeError):
                                        call("robot.relax", {})
                                    connected = False
                                    detail = f"恢复失败：{error} {getattr(error, 'stderr', '') or ''}"
                                    diagnostics.report(detail, summary=diagnostics.brief_error(detail) or STANDUP_FAILED)
                                    diagnostics.display()
                                termios.tcflush(terminal, termios.TCIFLUSH)
                                last_keys["\n"] = time.monotonic()
                                break
                            elif key == "l" and pressed and (mode == "drive" or not connected):
                                diagnostics.toggle_runtime()
                                next_runtime = time.monotonic() + 1.0
                            elif diagnostics.recovery_failed and key != "c":
                                continue
                            elif not connected and key != "c":
                                continue
                            elif key == " ":
                                velocity = (0.0, 0.0, 0.0)
                                expires = 0.0
                                roll_until = rt_until = lt_until = 0.0
                                head = dict.fromkeys(head, 0.0)
                                pose = dict.fromkeys(pose, 0.0)
                                call("robot.stop", {})
                                send("robot.head", head)
                                send("robot.pose", {**pose, "active": mode == "body"})
                            elif key in ("y", "b") and pressed:
                                target = "head" if key == "y" else "body"
                                if mode == "body":
                                    send("robot.pose", {"active": False})
                                mode = "drive" if mode == target else target
                                velocity = (0.0, 0.0, 0.0)
                                expires = 0.0
                                pose = dict.fromkeys(pose, 0.0)
                                call("robot.stop", {})
                            elif key in skills and enabled:
                                if pressed:
                                    velocity = (0.0, 0.0, 0.0)
                                    expires = 0.0
                                    roll_until = 0.0
                                    call("robot.stop", {})
                                    result = call("robot.do", {"skill": skills[key]}, allow_refusal=True)
                                    if key == "x":
                                        roll_accepted = result is not None
                                if key == "x" and roll_accepted:
                                    roll_until = now + 0.30
                            elif key in ("r", "f"):
                                walking_voice.set_moving(False)
                                if key == "r":
                                    rt_until = now + 0.30
                                    if pressed:
                                        call("robot.sound", {"tag": "chirp"}, allow_refusal=True)
                                else:
                                    lt_until = now + 0.30
                                    if pressed:
                                        call("robot.sound", {"tag": "wheee", "hold": True}, allow_refusal=True)
                            elif key in ("m", "p"):
                                if pressed:
                                    held_since[key] = now
                                    hold_sent.discard(key)
                                if key not in hold_sent and now - held_since[key] >= (3.0 if key == "m" else 2.0):
                                    hold_sent.add(key)
                                    velocity = (0.0, 0.0, 0.0)
                                    expires = 0.0
                                    roll_until = rt_until = lt_until = 0.0
                                    call("robot.stop", {})
                                    if key == "m":
                                        result = call("robot.mode", {}, allow_refusal=True)
                                        if result is None:
                                            continue
                                        current_mode = result["mode"]
                                        target = "roller" if current_mode == "walk" else "walk"
                                        if call("robot.setMode", {"mode": target}, allow_refusal=True) is not None:
                                            print(f"\r\033[K已请求切换到 {target}。", flush=True)
                                    else:
                                        walking_voice.close()
                                        send("robot.pose", {"active": False})
                                        if mouth_active:
                                            send("robot.mouth", {"open": 0.0})
                                        send("robot.sound", {"tag": "wheee", "hold": False})
                                        if call("robot.shutdown", {}, allow_refusal=True) is not None:
                                            shutdown_requested = True
                                            print("\r\033[K已请求坐下并关机，由 robotd 完成。", flush=True)
                                            return 0
                            elif key == "t" and enabled:
                                if pressed:
                                    velocity = (0.0, 0.0, 0.0)
                                    expires = 0.0
                                    roll_until = 0.0
                                    call("robot.stop", {})
                                    try:
                                        call("robot.do", {"skill": "sit_toggle"})
                                    except (OSError, RuntimeError) as error:
                                        diagnostics.report(f"坐站切换失败：{error}",
                                                           summary=diagnostics.brief_error(str(error)) or "坐站切换失败")
                            elif key == "c" and pressed:
                                if diagnostics.verbose:
                                    if current_socket is not None:
                                        current_socket.close()
                                        current_socket = None
                                        current_log.close()
                                        current_log = None
                                        print(f"\r\033[K电流记录已保存：{log_path}；采样峰值 {current_peak:.3f} A，"
                                              f"缺失拍数 {missing_ticks}", flush=True)
                                    current_status = ""
                                    diagnostics.set_verbose(False)
                                    print("\r\033[K电流详情已关闭。" if diagnostics.runtime_verbose else "\r\033[K已返回控制界面。", flush=True)
                                else:
                                    diagnostics.set_verbose(True)
                                    print("\r\033[K详细日志已开启，再按 C 关闭。", flush=True)
                                    if not connected:
                                        continue
                                    log_dir = Path(args.log_dir).expanduser().resolve()
                                    log_dir.mkdir(parents=True, exist_ok=True)
                                    log_path = log_dir / f"current-{datetime.now():%Y%m%d-%H%M%S-%f}.csv"
                                    current_log = log_path.open("x", encoding="utf-8", newline="", buffering=1)
                                    current_writer = csv.writer(current_log)
                                    current_writer.writerow([
                                        "time", "robot_t_s", "tick", "valid", "input_estimate_a", "cutoff_a",
                                        "protection_enabled", "debounce_ticks", "missing_ticks", "policy",
                                        "limited_by", "vx", "vy", "vyaw",
                                        *[f"id_{ident}_phase_a" for ident in JOINT_IDS],
                                        "input_estimate_abs_a",
                                    ])
                                    current_socket = socket.socket(socket.AF_UNIX)
                                    current_socket.settimeout(1.0)
                                    try:
                                        current_socket.connect(args.socket)
                                        for ident, method, params in (
                                            (1, "hello", {"api_version": 18}), (2, "robot.subscribe", {}),
                                        ):
                                            current_socket.sendall((json.dumps({
                                                "jsonrpc": "2.0", "id": ident, "method": method, "params": params,
                                            }) + "\n").encode())
                                    except OSError as error:
                                        current_socket.close()
                                        current_socket = None
                                        current_log.close()
                                        current_log = None
                                        disconnected(error)
                                        continue
                                    current_socket.setblocking(False)
                                    current_buffer = b""
                                    current_peak = 0.0
                                    current_received = time.monotonic()
                                    previous_tick = None
                                    missing_ticks = 0
                                    current_status = "等待实时电流……"
                                    print(f"\r\033[K电流记录开始：{log_path}（每拍保存，C 停止）", flush=True)
                            elif key == "h" and pressed:
                                on = walking_voice.toggle()
                                print(f"\r\033[K行走随机讲话已{'开启' if on else '关闭'}。", flush=True)
                            elif mode == "head" and key in head_keys and enabled:
                                field, step = head_keys[key]
                                head[field] = max(-2.5, min(2.5, round(head[field] + step, 4)))
                            elif mode == "body" and key in pose_keys and enabled:
                                field, step, low, high = pose_keys[key]
                                pose[field] = max(low, min(high, round(pose[field] + step, 4)))
                            elif mode == "drive" and key in ("w", "s") and enabled:
                                roll_until = 0.0
                                vx = round(velocity[0] + (0.10 if key == "w" else -0.10), 2)
                                velocity = (max(-0.40, min(0.40, vx)), velocity[1], velocity[2])
                            elif mode == "drive" and key in ("a", "d") and enabled:
                                roll_until = 0.0
                                vy = round(velocity[1] + (0.10 if key == "a" else -0.10), 2)
                                velocity = (velocity[0], max(-0.40, min(0.40, vy)), velocity[2])
                            elif mode == "drive" and key in turns and enabled:
                                roll_until = 0.0
                                velocity = (velocity[0], velocity[1], turns[key])
                                # shortcut: terminal repeats replace key-up events; use evdev for exact release timing.
                                expires = time.monotonic() + 0.30
                    if current_socket is not None and current_socket in readable:
                        try:
                            chunk = current_socket.recv(65536)
                        except OSError:
                            chunk = b""
                        if not chunk:
                            current_socket.close()
                            current_socket = None
                            current_log.close()
                            current_log = None
                            current_buffer = b""
                            disconnected("电流状态流已断开")
                            print(f"\r\033[K电流记录已保存：{log_path}", flush=True)
                        current_buffer += chunk
                        while b"\n" in current_buffer:
                            line, current_buffer = current_buffer.split(b"\n", 1)
                            message = json.loads(line)
                            if "error" in message or message.get("result", {}).get("accepted") is False:
                                raise RuntimeError(f"电流订阅失败：{message}")
                            if message.get("method") != "robot.state":
                                continue
                            state = message["params"]
                            sample = state.get("current")
                            current_received = time.monotonic()
                            movement = state["move"]
                            limits = ";".join(movement.get("limited_by", []))
                            valid = False
                            if sample is not None:
                                phases = sample["joints_ma"]
                                total = sample["input_a"]
                                cutoff = sample["cutoff_a"]
                                valid = (len(phases) == len(JOINT_IDS)
                                         and all(isinstance(v, (int, float)) and math.isfinite(v)
                                                 for v in [total, cutoff, *phases]) and cutoff > 0)
                            if valid:
                                phases = [abs(value) / 1000.0 for value in phases]
                                tick = sample["tick"]
                                if previous_tick is not None:
                                    missing_ticks += max(0, tick - previous_tick - 1)
                                previous_tick = tick
                                current_peak = max(current_peak, total)
                                hottest = max(range(len(phases)), key=phases.__getitem__)
                                protection = "开启" if sample["protection_enabled"] else "关闭"
                                current_status = (f"I估={total:.2f}/{cutoff:.2f} A 峰={current_peak:.2f} A "
                                                  f"单颗相电流最大 ID{JOINT_IDS[hottest]}={phases[hottest]:.2f} A "
                                                  f"保护{protection} {limits}")
                                values = [tick, 1, total, cutoff, sample["protection_enabled"],
                                          sample["debounce_ticks"]]
                            else:
                                values = ["", 0, "", "", "", ""]
                                phases = [""] * len(JOINT_IDS)
                                current_status = "无有效电流（读取失败或 robotd 未更新）"
                            current_status = f"策略={state['policy']} | {current_status}"
                            current_writer.writerow([
                                datetime.now().astimezone().isoformat(timespec="milliseconds"), state["t"],
                                *values, missing_ticks, state["policy"], limits, *movement["requested"], *phases,
                                sample.get("input_abs_a", "") if valid else "",
                            ])
                    if time.monotonic() >= expires:
                        velocity = (velocity[0], velocity[1], 0.0)
                    live_state, _ = diagnostics._state
                    live_state = live_state or {}
                    live_safety = live_state.get("safety", {})
                    live_fall = live_safety.get("fall") or {}
                    recovering = live_fall.get("recovery_active") or live_state.get("policy") == "standup"
                    if enabled and recovering:
                        if recovery_started is None:
                            recovery_started = time.monotonic()
                        if time.monotonic() - recovery_started >= AUTO_STANDUP_TIMEOUT_S and not diagnostics.recovery_failed:
                            diagnostics.report(f"自动 StandUp 超过 {AUTO_STANDUP_TIMEOUT_S:g} 秒，已请求卸力", summary=STANDUP_FAILED)
                            try:
                                call("robot.relax", {})
                            except (OSError, ValueError, RuntimeError) as error:
                                diagnostics.report(f"起身失败，卸力请求失败：{error}", summary="起身失败，卸力请求失败")
                    elif not live_safety.get("fallen") and (
                        live_state.get("policy") == "rise"
                        or (live_state.get("policy") == "walk"
                            and math.hypot(*live_state.get("move", {}).get("applied", [])) > 0.05)
                    ):
                        recovery_started = None
                    if live_fall.get("recovery_failed"):
                        diagnostics.recovery_failed = True
                    if diagnostics.recovery_failed:
                        enabled = False
                        roll_until = rt_until = lt_until = 0.0
                    if recovering or diagnostics.recovery_failed:
                        velocity = (0.0, 0.0, 0.0)
                        expires = roll_until = 0.0
                        mode = "drive"
                    if enabled:
                        send("robot.move", dict(zip(("vx", "vy", "vyaw"), velocity)))
                        if mode == "head":
                            send("robot.head", head)
                        elif mode == "body":
                            send("robot.pose", {**pose, "active": True})
                        if time.monotonic() < roll_until:
                            send("robot.do", {"skill": "roulade"})
                    now = time.monotonic()
                    manual_mouth = now < max(rt_until, lt_until)
                    if manual_mouth or mouth_active:
                        send("robot.mouth", {"open": float(manual_mouth)})
                    mouth_active = manual_mouth
                    if now < lt_until:
                        send("robot.sound", {"tag": "wheee", "hold": True})
                    elif wheee_active:
                        send("robot.sound", {"tag": "wheee", "hold": False})
                    wheee_active = now < lt_until
                    walking_voice.set_moving(is_walking(enabled, velocity) and not manual_mouth)
                    vx, vy, vyaw = velocity
                    status = diagnostics.brief_status(enabled)
                    if mode != "drive" and status in ("站立中", "行走中"):
                        status = {"head": "头部控制中", "body": "身体姿态控制中"}[mode]
                    now = time.monotonic()
                    if current_socket is not None and now - current_received > 0.5:
                        current_status = "电流数据已过期，等待新数据……"
                    if diagnostics.runtime_verbose and now >= next_runtime:
                        print(f"\r\033[K[运行] {diagnostics.runtime_status()}", flush=True)
                        next_runtime = now + 1.0
                    if now >= next_display:
                        voice_status = walking_voice.status
                        if diagnostics.verbose:
                            detail = f"v=({vx:+.2f},{vy:+.2f},{vyaw:+.2f}) | {diagnostics.state_status()} | {voice_status}"
                            if mode != "drive":
                                detail += " | " + " ".join(f"{key}={value:+.3f}" for key, value in
                                                          (head if mode == "head" else pose).items())
                            print(f"\r\033[K{detail}", flush=True)
                            if current_socket is not None:
                                print(current_status, flush=True)
                        columns = shutil.get_terminal_size().columns - 1
                        visible = ""
                        for char in status:
                            columns -= 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
                            if columns < 0:
                                break
                            visible += char
                        print(f"\r\033[K{visible}", end="", flush=True)
                        next_display = now + 0.2
            finally:
                walking_voice.close()
                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    signal.signal(signum, signal.SIG_IGN)
                with suppress(termios.error):
                    termios.tcsetattr(terminal, termios.TCSANOW, saved)
                if current_socket is not None:
                    current_socket.close()
                if current_log is not None:
                    with suppress(OSError):
                        current_log.close()
                        print(f"\r\033[K电流记录已保存：{log_path}；采样峰值 {current_peak:.3f} A，"
                              f"缺失拍数 {missing_ticks}", flush=True)
                if not shutdown_requested:
                    with suppress(OSError, ValueError, RuntimeError):
                        send("robot.pose", {"active": False})
                        send("robot.head", dict.fromkeys(head, 0.0))
                        if mouth_active:
                            send("robot.mouth", {"open": 0.0})
                        send("robot.sound", {"tag": "wheee", "hold": False})
                    with suppress(OSError):
                        print("\r\033[K正在停止控制并卸力……", flush=True)
                    with suppress(OSError, ValueError, RuntimeError):
                        call("robot.stop", {})
                    if args.socket == "/run/robotd.sock":
                        with suppress(OSError, ValueError, RuntimeError):
                            call("robot.relax", {})
                        subprocess.run(["systemctl", "stop", "robotd"], check=True, timeout=15)
                        print("robotd 服务已停止。再次运行键盘控制后按 Enter 重启起身。")
                    elif connected:
                        call("robot.relax", {})
                        print("已发送卸力请求。")
                    else:
                        print("robotd 未连接，已退出。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"键盘控制失败：{error}", file=sys.stderr)
        if isinstance(error, FileNotFoundError) and error.filename == "/run/robotd.sock":
            print("robotd socket 不存在；请先运行：sudo systemctl start robotd", file=sys.stderr)
        print("如电机仍在动作，请托住机器人并运行：sudo systemctl stop robotd", file=sys.stderr)
        sys.exit(1)
