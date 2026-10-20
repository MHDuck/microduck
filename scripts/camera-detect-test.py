#!/usr/bin/env python3
"""Read-only camera and duck-detector diagnostic for a provisioned MicroDuck.

This script never connects to robotd and never sends a servo command. It verifies the
camera service/configuration, prints the browser console URL, and watches mediad's own
detector reports for a bounded time.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import select
import socket
import subprocess
import sys
import time


DEFAULT_CONFIG = Path("/opt/robot/daemon/current/robotd.toml")
FALLBACK_CONFIG = Path("/etc/robot/robotd.toml")
DEFAULT_STATS = Path("/run/mediad/camera.json")


def parse_config(text: str) -> dict:
    """Read only the scalar [media]/[detect] values this diagnostic needs."""
    result: dict[str, dict[str, object]] = {}
    section = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section not in ("media", "detect") or "=" not in line:
            continue
        key, raw_value = (part.strip() for part in line.split("=", 1))
        lowered = raw_value.lower()
        if lowered in ("true", "false"):
            value: object = lowered == "true"
        elif raw_value[:1] in ('"', "'") and raw_value[-1:] == raw_value[:1]:
            value = raw_value[1:-1]
        else:
            try:
                value = float(raw_value) if any(c in raw_value for c in ".eE") else int(raw_value)
            except ValueError:
                value = raw_value
        result.setdefault(section, {})[key] = value
    return result


def load_config(path: Path) -> tuple[Path, dict]:
    candidates = (path, FALLBACK_CONFIG) if path == DEFAULT_CONFIG else (path,)
    for candidate in candidates:
        try:
            return candidate, parse_config(candidate.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
    raise FileNotFoundError(f"找不到配置文件：{', '.join(map(str, candidates))}")


def console_url(host: str | None) -> str:
    name = host or socket.gethostname()
    if "." not in name and not name.replace(".", "").isdigit():
        name = f"{name}.local"
    return f"http://{name}:8080/"


def service_active() -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", "mediad"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def print_stats(path: Path) -> bool:
    try:
        stats = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"相机状态：未发现 {path}（mediad 未运行或尚未发布状态）")
        return False
    except (OSError, json.JSONDecodeError) as error:
        print(f"相机状态：无法读取：{error}")
        return False
    print("相机状态：" + json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return True


def watch_detector(seconds: float) -> None:
    if seconds <= 0:
        return
    print(f"\n观察 mediad 检测日志 {seconds:g} 秒；请让另一只 MicroDuck 出现在画面中……")
    command = [
        "journalctl",
        "-u",
        "mediad",
        "--since",
        "now",
        "--follow",
        "--output=cat",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    deadline = time.monotonic() + seconds
    keywords = ("duck detector", "detection", "npu", "onnx", "rknn")
    found = 0
    try:
        while time.monotonic() < deadline:
            if process.stdout is None:
                break
            remaining = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select([process.stdout], [], [], min(0.5, remaining))
            if not readable:
                continue
            line = process.stdout.readline()
            if line and any(keyword in line.lower() for keyword in keywords):
                print(line.rstrip())
                found += 1
            elif process.poll() is not None:
                break
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
    if found == 0:
        print("本次未截获检测报告；默认 2 Hz、每 20 次汇总一次，建议延长到 15～20 秒。")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="只读测试摄像头流和官方 MicroDuck 检测器。")
    result.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    result.add_argument("--stats", type=Path, default=DEFAULT_STATS)
    result.add_argument("--host", help="浏览器访问用主机名或 IP；默认使用板端 hostname.local")
    result.add_argument("--seconds", type=float, default=15.0, help="观察检测日志秒数，0 表示不观察")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.seconds < 0:
        raise SystemExit("--seconds 不能为负数")
    try:
        config_path, config = load_config(args.config)
    except (OSError, ValueError) as error:
        print(f"配置读取失败：{error}", file=sys.stderr)
        return 2

    detect = config.get("detect", {})
    media = config.get("media", {})
    print(f"配置：{config_path}")
    print(f"mediad：{'active' if service_active() else '未运行'}")
    print(f"摄像头：{'开启' if media.get('camera', True) else '测试图案'}")
    print(f"鸭子检测：{'开启' if detect.get('enabled', False) else '关闭'}")
    print(f"检测频率：{detect.get('hz', 2.0)} Hz（不要为走路测试提高到 2 Hz 以上）")
    print_stats(args.stats)
    print(f"视频与检测框：{console_url(args.host)}")
    print("本脚本不会连接 robotd，不会启用策略，也不会控制任何舵机。")

    if not detect.get("enabled", False):
        print("\n检测尚未开启：运行 sudo robotctl configure，设置 detect.enabled=true，"
              "保存并重启 mediad 后再测。")
        return 3
    watch_detector(args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
