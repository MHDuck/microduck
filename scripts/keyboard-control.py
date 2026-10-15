#!/usr/bin/env python3
import argparse
from contextlib import suppress
import json
import os
import select
import signal
import socket
import sys
import termios
import time
import tty


def main():
    parser = argparse.ArgumentParser(description="通过本地 robotd 接口控制机器人（Linux/SSH 终端）。")
    parser.add_argument("--socket", default="/run/robotd.sock")
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
            print("移动键点动；长按续发，松开后约 0.3 秒停止。退出保持策略启用，不卸力。")
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
                                print("模型已启用；就绪后接管。", flush=True)
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
            finally:
                with suppress(termios.error):
                    termios.tcsetattr(terminal, termios.TCSANOW, saved)
                with suppress(OSError, ValueError, RuntimeError):
                    call("robot.stop", {})
                print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError) as error:
        print(f"键盘控制失败：{error}", file=sys.stderr)
        sys.exit(1)
