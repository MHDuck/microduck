#!/usr/bin/env python3
import argparse
from contextlib import suppress
import csv
from datetime import datetime
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import select
import signal
import socket
import subprocess
import sys
import threading
import time

try:
    import termios
    import tty
except ImportError:  # Unit-testable on Windows; the interactive program remains Linux-only.
    termios = None
    tty = None


# Same order as duck_control::model::JOINT_IDS.
JOINT_IDS = (20, 21, 22, 23, 24, 30, 31, 32, 33, 34, 10, 11, 12, 13, 14)

# Same order and values as duck_control::model::DEFAULT_POSITION, in radians.
HOME_POSITION = (0.0, -0.0873, -0.4579, -0.0049, 0.4530, 0.3491, 0.3491, 0.0, 0.0,
                 0.0, 0.0, 0.0873, 0.4579, 0.0049, -0.4530)

DEFAULT_WALK_EMOTIONS = "laughing"
DEFAULT_VOICE_GAP_S = 0.0


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


def main():
    parser = argparse.ArgumentParser(description="通过本地 robotd 接口控制机器人（Linux/SSH 终端）。")
    parser.add_argument("--socket", default="/run/robotd.sock",
                        help="自定义 socket 退出时只请求卸力；默认 socket 退出时停止 robotd 服务。")
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
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(1.0)
        try:
            connection.connect(args.socket)
        except OSError as error:
            raise OSError(error.errno, f"连接 robotd socket 失败：{error.strerror}", args.socket) from error
        with connection.makefile("rb") as reader:
            request_id = 0

            def send(method, params, ident=None):
                message = {"jsonrpc": "2.0", "method": method, "params": params}
                if ident is not None:
                    message["id"] = ident
                connection.sendall((json.dumps(message) + "\n").encode())

            def call(method, params, *, allow_refusal=False):
                nonlocal request_id
                request_id += 1
                send(method, params, request_id)
                line = reader.readline()
                if not line:
                    raise ConnectionError("robotd 已断开连接")
                response = json.loads(line)
                if response.get("id") != request_id or "error" in response:
                    raise RuntimeError(f"robotd 返回异常：{response}")
                result = response.get("result", {})
                if result.get("accepted") is False:
                    if allow_refusal:
                        print(f"\r\033[K{method} 被拒绝：{result.get('reason', '未知原因')}", flush=True)
                        return None
                    raise RuntimeError(result.get("reason", "robotd 拒绝了指令"))
                return result

            call("hello", {"api_version": 18})
            call("robot.stop", {})
            print("托住机器人，Enter = 手柄 Start：开启/关闭策略；关闭策略不卸力。")
            print("W/S 每次增减 vx 0.10 m/s（范围 -0.40～+0.40），松开保持；长按会连续调速。")
            print("A/D 每次增减 vy 0.10 m/s（范围 -0.40～+0.40），松开保持；长按会连续调速。")
            print("Q/E 左右转（松开约 0.3 秒停止转向）  空格速度归零  Esc/Ctrl-C 退出")
            print("T 坐下/站起切换（Enter 启用后使用，切换前速度归零；两次点按请间隔 1 秒以上）。")
            print("每次 T 后打印 5 秒关节位置（5 Hz，14 个关节，实测 / HOME / 偏差，单位 rad）。")
            print("实时显示指令速度；vx / vy 持续保持，按空格停止。")
            print("C 开始/停止实际策略、电流显示和 CSV 记录（可在启用模型之前开始）。")
            print("Y 头部模式 / B 身体姿态模式，再按返回行走；切换时速度归零。")
            print("头部：W/S 俯仰、A/D 偏航、I/K 颈部俯仰、J/L 侧倾；身体：W/S 高度、I/K 俯仰、J/L 侧倾。")
            print("姿态按键逐次调整，松开保持；空格同时将当前姿态归零。")
            print("G = A 拾取  Z/V = LB/RB 踢腿  X 翻滚（长按连续）  R/F = RT/LT 张嘴与声音。")
            print("M 长按 3 秒切换步行/轮式  P 长按 2 秒坐下并关机；单次按住只触发一次。")
            print("终端以按键重复估算长按和松开；R/F/X 松开约 0.3 秒释放，开关类按键两次点按请间隔 1 秒以上。")
            print("H 开启/关闭行走随机讲话；只在速度指令非零时发声，R/F 手动发声期间暂停。")
            print("退出会卸力，请先托住机器人；默认连接还会停止 robotd 服务。")
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
            position_socket = None
            position_buffer = b""
            position_until = 0.0
            position_samples = 0
            current_socket = None
            current_log = None
            current_status = ""
            current_buffer = b""
            current_peak = 0.0
            current_received = 0.0
            previous_tick = None
            missing_ticks = 0
            next_display = 0.0
            for signum in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(signum, lambda *_: sys.exit(0))
            try:
                tty.setcbreak(terminal)
                while True:
                    inputs = [terminal] + ([current_socket] if current_socket is not None else [])
                    if position_socket is not None:
                        inputs.append(position_socket)
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
                                velocity = (0.0, 0.0, 0.0)
                                expires = 0.0
                                roll_until = 0.0
                                call("robot.stop", {})
                                result = call("robot.enable", {"on": False, "toggle": True}, allow_refusal=True)
                                if result is not None:
                                    enabled = result["reason"].startswith("enabled")
                                    print(f"\r\033[K{result['reason']}", flush=True)
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
                                        current_mode = call("robot.mode", {})["mode"]
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
                                    except RuntimeError as error:
                                        print(f"\r\033[K坐站切换失败：{error}", flush=True)
                                    else:
                                        position_started = time.monotonic()
                                        position_until = position_started + 5.0
                                        position_samples = 0
                                        if position_socket is not None:
                                            position_socket.close()
                                        position_socket = socket.socket(socket.AF_UNIX)
                                        position_socket.settimeout(1.0)
                                        position_socket.connect(args.socket)
                                        for ident, method, params in (
                                            (1, "hello", {"api_version": 18}),
                                            (2, "robot.subscribe", {"hz": 5}),
                                        ):
                                            position_socket.sendall((json.dumps({
                                                "jsonrpc": "2.0", "id": ident, "method": method, "params": params,
                                            }) + "\n").encode())
                                        position_socket.setblocking(False)
                                        position_buffer = b""
                                        print("\r\033[K坐站请求已入队；开始打印 5 秒位置，偏差 = 实测 - HOME（rad）。", flush=True)
                            elif key == "c" and pressed:
                                if current_socket is not None:
                                    current_socket.close()
                                    current_socket = None
                                    current_log.close()
                                    current_log = None
                                    print(f"\r\033[K电流记录已保存：{log_path}；采样峰值 {current_peak:.3f} A，"
                                          f"缺失拍数 {missing_ticks}", flush=True)
                                    current_status = ""
                                else:
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
                                    current_socket.connect(args.socket)
                                    for ident, method, params in (
                                        (1, "hello", {"api_version": 18}), (2, "robot.subscribe", {}),
                                    ):
                                        current_socket.sendall((json.dumps({
                                            "jsonrpc": "2.0", "id": ident, "method": method, "params": params,
                                        }) + "\n").encode())
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
                    if position_socket is not None and position_socket in readable:
                        chunk = position_socket.recv(65536)
                        if not chunk:
                            raise ConnectionError("关节位置状态流已断开")
                        position_buffer += chunk
                        while b"\n" in position_buffer:
                            line, position_buffer = position_buffer.split(b"\n", 1)
                            message = json.loads(line)
                            if "error" in message or message.get("result", {}).get("accepted") is False:
                                raise RuntimeError(f"位置订阅失败：{message}")
                            if message.get("method") != "robot.state" or time.monotonic() >= position_until:
                                continue
                            state = message["params"]
                            joints = state["joints"]
                            if len(joints) != len(JOINT_IDS) or not all(
                                isinstance(value, (int, float)) and math.isfinite(value) for value in joints
                            ):
                                print("\r\033[K位置反馈无效，跳过此帧。", flush=True)
                                continue
                            freshness = "新反馈" if state.get("current") is not None else "缓存反馈"
                            rows = [f"\r\033[K[T+{time.monotonic() - position_started:.2f}s] "
                                    f"策略={state['policy']} {freshness}",
                                    " ID    实测(rad)    HOME(rad)    偏差(rad)"]
                            for index, (ident, home) in enumerate(zip(JOINT_IDS, HOME_POSITION)):
                                if ident == 34:
                                    continue
                                rows.append(f"{ident:3d} {joints[index]:+12.4f} {home:+12.4f} "
                                            f"{joints[index] - home:+12.4f}")
                            print("\n".join(rows), flush=True)
                            position_samples += 1
                    if position_socket is not None and time.monotonic() >= position_until:
                        position_socket.close()
                        position_socket = None
                        print(f"\r\033[K5 秒位置打印结束，共 {position_samples} 组。", flush=True)
                    if current_socket is not None and current_socket in readable:
                        chunk = current_socket.recv(65536)
                        if not chunk:
                            raise ConnectionError("电流状态流已断开")
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
                    status = "已启用，Enter 停用" if enabled else "已停用，Enter 启用"
                    status += " | " + {"drive": "行走", "head": "头部", "body": "身体姿态"}[mode]
                    if mode != "drive":
                        status += " " + " ".join(f"{key}={value:+.3f}" for key, value in
                                                 (head if mode == "head" else pose).items())
                    now = time.monotonic()
                    if current_socket is not None and now - current_received > 0.5:
                        current_status = "电流数据已过期，等待新数据……"
                    if now >= next_display:
                        if current_socket is not None:
                            print(f"\r\033[K{current_status}", flush=True)
                        print(f"\r\033[K{status} | 指令 vx={vx:+.2f} m/s  vy={vy:+.2f} m/s  "
                              f"vyaw={vyaw:+.2f} rad/s | {walking_voice.status}", end="", flush=True)
                        next_display = now + 0.2
            finally:
                walking_voice.close()
                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    signal.signal(signum, signal.SIG_IGN)
                with suppress(termios.error):
                    termios.tcsetattr(terminal, termios.TCSANOW, saved)
                if position_socket is not None:
                    position_socket.close()
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
                        print("robotd 服务已停止。再次使用前运行：sudo systemctl start robotd")
                    else:
                        call("robot.relax", {})
                        print("已发送卸力请求。")
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
