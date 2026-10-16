#!/usr/bin/env python3
import argparse
from contextlib import suppress
import json
import os
import select
import signal
import socket
import subprocess
import sys
import termios
import time
import tty


def main():
    parser = argparse.ArgumentParser(description="通过本地 robotd 接口控制机器人（Linux/SSH 终端）。")
    parser.add_argument("--socket", default="/run/robotd.sock",
                        help="自定义 socket 退出时只请求卸力；默认 socket 退出时停止 robotd 服务。")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("需要交互终端；SSH 登录后运行这个文件。")

    moves = {
        "w": (0.10, 0.0, 0.0), "s": (-0.10, 0.0, 0.0),
        "a": (0.0, 0.05, 0.0), "d": (0.0, -0.05, 0.0),
        "q": (0.0, 0.0, 0.30), "e": (0.0, 0.0, -0.30),
    }
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(1.0)
        connection.connect(args.socket)
        with connection.makefile("rb") as reader:
            request_id = 0

            def send(method, params, ident=None):
                message = {"jsonrpc": "2.0", "method": method, "params": params}
                if ident is not None:
                    message["id"] = ident
                connection.sendall((json.dumps(message) + "\n").encode())

            def call(method, params):
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
                    raise RuntimeError(result.get("reason", "robotd 拒绝了指令"))
                return result

            call("hello", {"api_version": 17})
            call("robot.stop", {})
            print("托住机器人，按 Enter 启用模型。")
            print("W/S 前后  A/D 左右横移  Q/E 左右转  空格停止  Esc/Ctrl-C 退出")
            print("移动键点动；长按续发，松开后约 0.3 秒停止。显示的是指令速度。")
            print("退出会卸力，请先托住机器人；默认连接还会停止 robotd 服务。")
            terminal = sys.stdin.fileno()
            saved = termios.tcgetattr(terminal)
            enabled = False
            velocity = (0.0, 0.0, 0.0)
            expires = 0.0
            for signum in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(signum, lambda *_: sys.exit(0))
            try:
                tty.setcbreak(terminal)
                while True:
                    readable, _, _ = select.select([terminal], [], [], 0.05)
                    if readable:
                        keys = os.read(terminal, 64)
                        if not keys:
                            break
                        for key in keys.decode(errors="ignore").lower():
                            if key in ("\x1b", "\x03"):
                                return 0
                            if key in ("\r", "\n"):
                                velocity = (0.0, 0.0, 0.0)
                                expires = 0.0
                                call("robot.stop", {})
                                call("robot.enable", {"on": True, "toggle": False})
                                enabled = True
                            elif key == " ":
                                velocity = (0.0, 0.0, 0.0)
                                expires = 0.0
                                call("robot.stop", {})
                            elif key in moves and enabled:
                                velocity = moves[key]
                                # shortcut: terminal repeats replace key-up events; use evdev for exact release timing.
                                expires = time.monotonic() + 0.30
                    if time.monotonic() >= expires:
                        velocity = (0.0, 0.0, 0.0)
                    if enabled:
                        send("robot.move", dict(zip(("vx", "vy", "vyaw"), velocity)))
                    vx, vy, vyaw = velocity
                    status = "已请求启用" if enabled else "待 Enter 启用"
                    print(f"\r\033[K{status} | 指令 vx={vx:+.2f} m/s  vy={vy:+.2f} m/s  "
                          f"vyaw={vyaw:+.2f} rad/s", end="", flush=True)
            finally:
                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    signal.signal(signum, signal.SIG_IGN)
                with suppress(termios.error):
                    termios.tcsetattr(terminal, termios.TCSANOW, saved)
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
        print("如电机仍在动作，请托住机器人并运行：sudo systemctl stop robotd", file=sys.stderr)
        sys.exit(1)
