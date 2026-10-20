#!/usr/bin/env python3
"""Play MicroDuck voice-bank sounds with a safe, expressive beak gesture.

This is deliberately a thin client of robotd.  robotd remains the only owner of
the servo bus and of the single-client ALSA PCM; this script sends only the
public ``robot.sound`` and ``robot.mouth`` intents.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import signal
import socket
import sys
import time
from typing import Callable, Iterable


API_VERSION = 18
DEFAULT_SOCKET = Path("/run/robotd.sock")
DEFAULT_RATE_HZ = 50.0
MOUTH_TAU_S = 0.09


def _laugh_keyframes() -> tuple[tuple[float, float], ...]:
    frames: list[tuple[float, float]] = [(0.0, 0.0)]
    for index in range(14):
        start = index * 0.34
        strength = 0.78 + 0.12 * math.sin(index * 0.8)
        frames.extend(((start + 0.035, strength), (start + 0.25, 0.12)))
    frames.append((5.0, 0.0))
    return tuple(frames)


@dataclass(frozen=True)
class Cue:
    tag: str
    description: str
    duration_s: float
    # (seconds since sound start, opening fraction).  Fractions are 0..1.
    keyframes: tuple[tuple[float, float], ...]


# These are expressive gestures, not waveform-perfect lip sync: robotd chooses a
# random voice-bank variant after receiving the tag.  Durations follow the ranges
# used by sounds/src/voices.rs and finish with a closed mouth.
EMOTIONS: dict[str, Cue] = {
    "happy": Cue(
        "chirp",
        "开心、短促回应",
        0.55,
        ((0.00, 0.00), (0.04, 0.85), (0.22, 0.38), (0.34, 0.72), (0.55, 0.00)),
    ),
    "greeting": Cue(
        "greet",
        "开心地打招呼",
        1.00,
        ((0.00, 0.00), (0.06, 0.78), (0.38, 0.18), (0.50, 0.72), (0.78, 0.14), (1.00, 0.00)),
    ),
    "curious": Cue(
        "inquire",
        "好奇、疑问",
        0.85,
        ((0.00, 0.00), (0.10, 0.24), (0.42, 0.78), (0.66, 0.50), (0.85, 0.00)),
    ),
    "sad": Cue(
        "coo",
        "低落、轻柔（使用官方 coo 音色）",
        1.65,
        ((0.00, 0.00), (0.20, 0.28), (0.58, 0.45), (1.15, 0.32), (1.65, 0.00)),
    ),
    "alarm": Cue(
        "alarm",
        "警觉、害怕或生气",
        0.55,
        ((0.00, 0.00), (0.03, 1.00), (0.20, 0.72), (0.36, 0.30), (0.55, 0.00)),
    ),
    "goodbye": Cue(
        "peck",
        "低沉告别",
        0.40,
        ((0.00, 0.00), (0.025, 0.58), (0.16, 0.24), (0.40, 0.00)),
    ),
    "laughing": Cue(
        "laugh",
        "连续大笑",
        5.00,
        _laugh_keyframes(),
    ),
}


SOUND_TO_EMOTION = {
    "chirp": "happy",
    "greet": "greeting",
    "inquire": "curious",
    "coo": "sad",
    "alarm": "alarm",
    "peck": "goodbye",
    "laugh": "laughing",
}


class RobotdError(RuntimeError):
    pass


class RobotdClient:
    def __init__(self, path: Path, timeout_s: float = 2.0):
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.settimeout(timeout_s)
        self._socket.connect(str(path))
        self._reader = self._socket.makefile("rb")
        self._request_id = 0

    def close(self) -> None:
        self._reader.close()
        self._socket.close()

    def __enter__(self) -> "RobotdClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _send(self, method: str, params: dict[str, object], request_id: int | None = None) -> None:
        message: dict[str, object] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        if request_id is not None:
            message["id"] = request_id
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._socket.sendall(payload.encode("utf-8"))

    def notify(self, method: str, params: dict[str, object]) -> None:
        self._send(method, params)

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self._request_id += 1
        wanted = self._request_id
        self._send(method, params, wanted)
        while True:
            line = self._reader.readline()
            if not line:
                raise RobotdError("robotd 已断开连接")
            response = json.loads(line)
            # Ignore asynchronous notifications or an unrelated response.
            if response.get("id") != wanted:
                continue
            if "error" in response:
                raise RobotdError(f"robotd 返回错误：{response['error']}")
            result = response.get("result") or {}
            if not isinstance(result, dict):
                raise RobotdError(f"robotd 返回了无法识别的结果：{result!r}")
            if result.get("accepted") is False:
                raise RobotdError(str(result.get("reason") or "robotd 拒绝了指令"))
            return result

    def hello(self) -> None:
        self.call("hello", {"api_version": API_VERSION})

    def close_mouth(self) -> None:
        self.notify("robot.mouth", {"open": 0.0})


def _interpolate(keyframes: tuple[tuple[float, float], ...], t_s: float) -> float:
    if t_s <= keyframes[0][0]:
        return keyframes[0][1]
    for (t0, y0), (t1, y1) in zip(keyframes, keyframes[1:]):
        if t_s <= t1:
            span = max(t1 - t0, 1e-9)
            return y0 + (y1 - y0) * ((t_s - t0) / span)
    return keyframes[-1][1]


def mouth_frames(cue: Cue, rate_hz: float, scale: float) -> Iterable[float]:
    """Yield a 0..1 mouth command with the chorale's 90 ms smoothing."""
    dt = 1.0 / rate_hz
    alpha = min(1.0, dt / MOUTH_TAU_S)
    opening = 0.0
    count = max(1, math.ceil(cue.duration_s * rate_hz))
    for index in range(count):
        target = _interpolate(cue.keyframes, index * dt) * scale
        opening += (target - opening) * alpha
        yield min(1.0, max(0.0, opening))
    # Always finish closed; robot.mouth is an unstamped intent and otherwise persists.
    yield 0.0


def play_cue(
    client: RobotdClient,
    cue: Cue,
    *,
    with_mouth: bool,
    rate_hz: float,
    mouth_scale: float,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> None:
    if not with_mouth:
        client.call("robot.sound", {"tag": cue.tag})
        return

    dt = 1.0 / rate_hz
    deadline = monotonic_fn()
    try:
        client.call("robot.sound", {"tag": cue.tag})
        for opening in mouth_frames(cue, rate_hz, mouth_scale):
            client.notify("robot.mouth", {"open": round(opening, 4)})
            deadline += dt
            sleep_fn(max(0.0, deadline - monotonic_fn()))
    finally:
        client.close_mouth()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="复用 robotd 官方声音库，让 MicroDuck 发声并同步开合嘴部。"
    )
    choice = result.add_mutually_exclusive_group()
    choice.add_argument("--emotion", choices=sorted(EMOTIONS), default="happy", help="情绪预设")
    choice.add_argument("--sound", choices=sorted(SOUND_TO_EMOTION), help="直接选择官方声音标签")
    result.add_argument("--list", action="store_true", help="列出预设后退出")
    result.add_argument("--repeat", type=int, default=1, help="重复次数，默认1")
    result.add_argument("--gap", type=float, default=0.25, help="多次播放之间的间隔秒数")
    result.add_argument("--mouth-scale", type=float, default=0.85, help="嘴部最大幅度比例，0～1")
    result.add_argument("--rate-hz", type=float, default=DEFAULT_RATE_HZ, help="嘴部命令频率")
    result.add_argument("--sound-only", action="store_true", help="只播放声音，不控制嘴部")
    result.add_argument("--socket", type=Path, default=DEFAULT_SOCKET, help="robotd Unix socket")
    return result


def print_presets() -> None:
    print("emotion     sound     description")
    for name, cue in EMOTIONS.items():
        print(f"{name:<11} {cue.tag:<9} {cue.description}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.list:
        print_presets()
        return 0
    if args.repeat < 1:
        raise SystemExit("--repeat 必须大于等于1")
    if args.gap < 0.0:
        raise SystemExit("--gap 不能为负数")
    if not 10.0 <= args.rate_hz <= 100.0:
        raise SystemExit("--rate-hz 必须在10～100之间")
    if not 0.0 <= args.mouth_scale <= 1.0:
        raise SystemExit("--mouth-scale 必须在0～1之间")

    emotion = SOUND_TO_EMOTION[args.sound] if args.sound else args.emotion
    cue = EMOTIONS[emotion]
    print(f"准备播放：{emotion} → {cue.tag}（{cue.description}）")
    if not args.sound_only:
        print("独立嘴部模式：不会启用行走策略，也不会给其余14个舵机上力。")

    def interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, interrupt)

    try:
        with RobotdClient(args.socket) as client:
            client.hello()
            for index in range(args.repeat):
                play_cue(
                    client,
                    cue,
                    with_mouth=not args.sound_only,
                    rate_hz=args.rate_hz,
                    mouth_scale=args.mouth_scale,
                )
                if index + 1 < args.repeat:
                    time.sleep(args.gap)
            # `--sound-only` must send no mouth intent at all. Even an apparently harmless
            # final `open: 0` is an edge for robotd's isolated-mouth path: it powers the beak
            # and moves it to closed. Mouth-enabled cues already close in `play_cue`'s finally.
    except (FileNotFoundError, ConnectionRefusedError) as error:
        print(f"无法连接 robotd：{error}", file=sys.stderr)
        return 2
    except (OSError, RobotdError, json.JSONDecodeError) as error:
        print(f"播放失败：{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if args.sound_only:
            print("\n已中止，未发送任何嘴部指令。", file=sys.stderr)
        else:
            print("\n已中止并请求闭嘴。", file=sys.stderr)
        return 130

    if args.sound_only:
        print("播放完成，未发送任何嘴部指令。")
    else:
        print("播放完成，嘴部已闭合。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
