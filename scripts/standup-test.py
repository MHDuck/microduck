#!/usr/bin/env python3
"""Run a bounded StandUp-only trial using robotd and a disposable params file."""

from __future__ import annotations

import argparse
from contextlib import suppress
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time


RELEASE = Path("/opt/robot/daemon/current")


def override_section(text: str, section: str, values: dict) -> str:
    """Replace selected scalar keys without rewriting unrelated robot calibration."""
    if text and not text.endswith("\n"):
        text += "\n"
    heading = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines)
                  if (match := heading.match(line)) and match[1] == section), None)
    if start is None:
        lines.extend([f"\n[{section}]\n"])
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if heading.match(lines[i])), len(lines))
    key_pattern = re.compile(r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=")
    retained = [line for line in lines[start + 1:end]
                if not ((match := key_pattern.match(line)) and match[1] in values)]
    assignments = [f"{key} = {json.dumps(value, ensure_ascii=False)}\n"
                   for key, value in values.items()]
    return "".join(lines[:start + 1] + assignments + retained + lines[end:])


def test_config(text: str, model: Path) -> str:
    # Only StandUp is loaded. Zero commands select the stand slot; walking/skills cannot
    # accidentally take over, and seated boot cannot select SitStand.
    text = override_section(text, "policy", {
        "enabled": True, "mode": "walk", "walk": str(model), "stand": str(model),
        "sitstand": "none", "ground_pick": "none", "kick_left": "none",
        "kick_right": "none", "roulade": "none",
    })
    # The fall torque-off latch and limp predictor would interrupt a deliberate floor start.
    # Keep current protection, actuator tuning, calibration and all IMU settings as supplied.
    text = override_section(text, "safety", {"fall_protection": False, "limp_fall": False})
    text = override_section(text, "audio", {"enabled": False})
    text = override_section(text, "chorale", {"accept": False})
    return text


class Client:
    def __init__(self, path: Path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(1.0)
        try:
            self.socket.connect(str(path))
        except BaseException:
            self.socket.close()
            raise
        self.buffer = b""
        self.ident = 0
        self.state = None

    def close(self):
        self.socket.close()

    def read(self):
        while b"\n" not in self.buffer:
            block = self.socket.recv(65536)
            if not block:
                raise ConnectionError("robotd 已断开连接")
            self.buffer += block
        line, self.buffer = self.buffer.split(b"\n", 1)
        message = json.loads(line)
        if message.get("method") == "robot.state":
            self.state = message["params"]
        return message

    def call(self, method, params):
        self.ident += 1
        self.socket.sendall((json.dumps({"jsonrpc": "2.0", "id": self.ident,
                                        "method": method, "params": params}) + "\n").encode())
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            response = self.read()
            if response.get("id") != self.ident:
                continue
            if "error" in response:
                raise RuntimeError(str(response["error"]))
            result = response.get("result", {})
            if result.get("accepted") is False:
                raise RuntimeError(result.get("reason", "robotd 拒绝请求"))
            return result
        raise TimeoutError(f"等待 {method} 超时")


def run(args):
    model = args.model.resolve(strict=True)
    config = test_config(args.params.read_text(encoding="utf-8"), model)
    if args.check:
        print(config)
        print("# 仅检查配置生成与文件存在性；ONNX 维度由 robotd 启动时验证。")
        return 0
    if not args.fake:
        if os.geteuid() != 0:
            raise RuntimeError("板端请使用 sudo python3 运行")
        active = [service for service in ("robotd", "padd", "servo-web.service")
                  if subprocess.run(["systemctl", "is-active", "--quiet", service],
                                    check=False).returncode == 0]
        if active:
            raise RuntimeError("先托住机器人并停止串口控制服务：sudo systemctl stop " + " ".join(active))
    if not args.yes and not sys.stdin.isatty():
        raise RuntimeError("需要交互终端按 Enter；自动测试可显式使用 --yes")
    log_path = args.log or Path(tempfile.gettempdir()) / f"standup-test-{time.time_ns()}.log"
    print(f"模型：{model}\n运行日志：{log_path}", flush=True)
    print("本次只运行 StandUp，速度始终为零；启用后先用约 2 秒回 HOME，再由模型起身。", flush=True)
    print("到时或 Ctrl-C 会卸力，请准备托住机器人。", flush=True)
    client = None
    with tempfile.TemporaryDirectory(prefix="standup-") as directory, log_path.open("w") as log:
        temp = Path(directory)
        params = temp / "params.toml"
        params.write_text(config, encoding="utf-8")
        command = [str(args.robotd), "--params", str(params), "--socket", str(temp / "robot.sock")]
        if args.fake:
            command.append("--fake")
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"robotd 启动失败，见 {log_path}")
                if client is None:
                    try:
                        client = Client(temp / "robot.sock")
                    except OSError:
                        time.sleep(0.1)
                        continue
                    client.call("hello", {"api_version": 18})
                health = client.call("robot.health", {})
                if str(health.get("reason", "")).startswith("policy unavailable:"):
                    raise RuntimeError(f"{health['reason']}；见 {log_path}")
                if health.get("healthy") and (health.get("imu") or {}).get("ready"):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError(f"robotd/IMU 未就绪：{health if client else '未连接'}；见 {log_path}")
            identity = client.call("robot.subscribe", {"hz": 10})
            if identity.get("unavailable"):
                raise RuntimeError(identity["unavailable"])
            if identity.get("stand") != model.name:
                raise RuntimeError(f"实际加载的 stand 模型不符：{identity}")
            if not args.yes:
                input("将机器人放到待测试姿态（趴倒/仰倒/坐倒），保持周围空旷，按 Enter 起身：")
            client.call("robot.stop", {})
            client.call("robot.enable", {"on": True, "toggle": False})
            deadline = time.monotonic() + args.seconds
            next_print = 0.0
            saw_stand = False
            while time.monotonic() < deadline:
                client.call("robot.move", {"vx": 0.0, "vy": 0.0, "vyaw": 0.0})
                client.read()
                state = client.state or {}
                saw_stand |= state.get("policy") == "stand"
                if state.get("policy") == "protection":
                    raise RuntimeError("保护触发，终止测试")
                now = time.monotonic()
                if now >= next_print:
                    print(f"剩余 {max(0, deadline - now):.1f}s  policy={state.get('policy')}  "
                          f"safety={state.get('safety')}", flush=True)
                    next_print = now + 0.5
            if not saw_stand:
                raise RuntimeError(f"未观察到 stand 策略执行，检查日志 {log_path}")
            print("已观察到 StandUp 模型执行；是否成功起身请结合实际姿态判断。")
        finally:
            if client:
                try:
                    client.call("robot.relax", {})
                    time.sleep(0.15)
                    print("已请求卸力。", flush=True)
                except Exception as error:
                    print(f"卸力请求失败：{error}；将终止测试 daemon。", file=sys.stderr)
                client.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            print("测试结束；临时配置已清理。正常 robotd/padd 服务保持停止。", flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="独立限时测试 StandUp；不修改正式配置。")
    parser.add_argument("--model", type=Path, default=RELEASE / "policies/standup_hd1910.onnx")
    parser.add_argument("--params", type=Path, default=Path("/etc/robot/robotd.toml"))
    parser.add_argument("--robotd", type=Path, default=RELEASE / "bin/robotd")
    parser.add_argument("--seconds", type=float, default=10.0, help="启用后的总时长，含约 2 秒 HOME；默认 10")
    parser.add_argument("--log", type=Path, help="保存 daemon 日志的路径，默认 /tmp/standup-test-*.log")
    parser.add_argument("--check", action="store_true", help="只打印临时配置，不启动 daemon、不连接机器人")
    parser.add_argument("--yes", action="store_true", help="跳过 Enter 确认，模型就绪后执行")
    parser.add_argument("--fake", action="store_true", help="启动假硬件，供本地验证流程")
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or not 3.0 <= args.seconds <= 60.0:
        parser.error("--seconds 必须在 3～60 秒之间")
    if os.name != "posix" and not args.check:
        parser.error("执行测试需要 Linux；Windows 上可使用 --check")
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    for signum in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", signal.SIGTERM)):
        signal.signal(signum, interrupted)
    with suppress(KeyboardInterrupt):
        try:
            return run(args)
        except Exception as error:
            print(f"StandUp 测试失败：{error}", file=sys.stderr)
            return 1
    return 130


if __name__ == "__main__":
    raise SystemExit(main())
