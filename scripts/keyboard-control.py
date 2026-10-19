#!/usr/bin/env python3
import argparse
from contextlib import suppress
import csv
from datetime import datetime
import json
import math
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import termios
import time
import tty


# Same order as duck_control::model::JOINT_IDS.
JOINT_IDS = (20, 21, 22, 23, 24, 30, 31, 32, 33, 34, 10, 11, 12, 13, 14)


def main():
    parser = argparse.ArgumentParser(description="通过本地 robotd 接口控制机器人（Linux/SSH 终端）。")
    parser.add_argument("--socket", default="/run/robotd.sock",
                        help="自定义 socket 退出时只请求卸力；默认 socket 退出时停止 robotd 服务。")
    parser.add_argument("--log-dir", default="logs", help="电流 CSV 日志目录，默认当前目录下的 logs。")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("需要交互终端；SSH 登录后运行这个文件。")

    moves = {
        "a": (0.05, 0.0), "d": (-0.05, 0.0),
        "q": (0.0, 0.30), "e": (0.0, -0.30),
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
            print("W/S 每次增减 vx 0.10 m/s（范围 -0.40～+0.40），松开保持；长按会连续调速。")
            print("A/D 左右横移  Q/E 左右转（松开约 0.3 秒停止横移/转向）  空格速度归零  Esc/Ctrl-C 退出")
            print("实时显示指令速度；vx 持续保持，按空格停止。")
            print("C 开始/停止实时电流显示和 CSV 记录（可在启用模型之前开始）。")
            print("退出会卸力，请先托住机器人；默认连接还会停止 robotd 服务。")
            terminal = sys.stdin.fileno()
            saved = termios.tcgetattr(terminal)
            enabled = False
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
            for signum in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(signum, lambda *_: sys.exit(0))
            try:
                tty.setcbreak(terminal)
                while True:
                    inputs = [terminal] + ([current_socket] if current_socket is not None else [])
                    readable, _, _ = select.select(inputs, [], [], 0.05)
                    if terminal in readable:
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
                            elif key == "c":
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
                                    ])
                                    current_socket = socket.socket(socket.AF_UNIX)
                                    current_socket.settimeout(1.0)
                                    current_socket.connect(args.socket)
                                    for ident, method, params in (
                                        (1, "hello", {"api_version": 17}), (2, "robot.subscribe", {}),
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
                            elif key in ("w", "s") and enabled:
                                vx = round(velocity[0] + (0.10 if key == "w" else -0.10), 2)
                                velocity = (max(-0.40, min(0.40, vx)), velocity[1], velocity[2])
                            elif key in moves and enabled:
                                velocity = (velocity[0], *moves[key])
                                # shortcut: terminal repeats replace key-up events; use evdev for exact release timing.
                                expires = time.monotonic() + 0.30
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
                                                  f"单颗最大 ID{JOINT_IDS[hottest]}={phases[hottest]:.2f} A "
                                                  f"保护{protection} {limits}")
                                values = [tick, 1, total, cutoff, sample["protection_enabled"],
                                          sample["debounce_ticks"]]
                            else:
                                values = ["", 0, "", "", "", ""]
                                phases = [""] * len(JOINT_IDS)
                                current_status = "无有效电流（读取失败或 robotd 未更新）"
                            current_writer.writerow([
                                datetime.now().astimezone().isoformat(timespec="milliseconds"), state["t"],
                                *values, missing_ticks, state["policy"], limits, *movement["requested"], *phases,
                            ])
                    if time.monotonic() >= expires:
                        velocity = (velocity[0], 0.0, 0.0)
                    if enabled:
                        send("robot.move", dict(zip(("vx", "vy", "vyaw"), velocity)))
                    vx, vy, vyaw = velocity
                    status = "已请求启用" if enabled else "待 Enter 启用"
                    now = time.monotonic()
                    if current_socket is not None and now - current_received > 0.5:
                        current_status = "电流数据已过期，等待新数据……"
                    if now >= next_display:
                        if current_socket is not None:
                            print(f"\r\033[K{current_status}", flush=True)
                        print(f"\r\033[K{status} | 指令 vx={vx:+.2f} m/s  vy={vy:+.2f} m/s  "
                              f"vyaw={vyaw:+.2f} rad/s", end="", flush=True)
                        next_display = now + 0.2
            finally:
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
