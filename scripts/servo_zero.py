#!/usr/bin/env python3
"""HD-1910 板端校零/点动。依赖：python3-serial；两个 servo_*.py 放在同一目录。"""

import argparse
import json
import os
from pathlib import Path
import select
import shutil
import signal
import struct
import subprocess
import sys
import time

DEFAULT_IDS = list(range(10, 15)) + list(range(20, 25)) + list(range(30, 35))


def servo_id(value):
    value = int(value)
    if not 0 <= value <= 253:
        raise argparse.ArgumentTypeError("ID 必须在 0–253，不能使用广播 ID")
    return value


def parse_ids(value):
    result = []
    try:
        for group in value.split(","):
            ends = group.strip().split("-")
            start, end = int(ends[0]), int(ends[-1])
            if len(ends) > 2 or not 0 <= start <= end <= 253:
                raise ValueError
            result.extend(range(start, end + 1))
    except ValueError:
        raise argparse.ArgumentTypeError("ID 格式：10-14,20-24,30-34 或 10") from None
    return list(dict.fromkeys(result))


class Bus:
    def __init__(self, port, baud):
        try:
            import serial
        except ImportError:
            raise RuntimeError("请先安装依赖：sudo apt install python3-serial") from None
        if sys.platform.startswith("linux") and shutil.which("fuser"):
            busy = subprocess.run(["fuser", port], capture_output=True, text=True)
            if busy.stdout.strip():
                raise RuntimeError(f"串口被进程 {busy.stdout.strip()} 占用；先停止 robotd/网页调试程序")
        self.ser = serial.Serial(port, baud, timeout=0.05, write_timeout=0.2,
                                 exclusive=True if os.name == "posix" else None)

    def close(self):
        self.ser.close()

    def request(self, sid, instruction, params=b"", wait=True, silent=False, status_ok=False):
        if not 0 <= sid <= 253:
            raise ValueError("只允许单个 ID，禁止广播写入")
        body = bytes([sid, len(params) + 2, instruction]) + bytes(params)
        self.ser.reset_input_buffer()
        self.ser.write(b"\xff\xff" + body + bytes([~sum(body) & 255]))
        self.ser.flush()
        if not wait:
            return b""
        header, received = b"", b""
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            byte = self.ser.read(1)
            if not byte:
                break
            received += byte
            header = (header + byte)[-2:]
            if header == b"\xff\xff":
                break
        if not received:
            if silent:
                return b""
            raise TimeoutError(f"ID {sid} 无应答")
        if header != b"\xff\xff":
            raise RuntimeError(f"ID {sid} 应答帧头错误")
        head = self.ser.read(2)
        if len(head) != 2 or head[1] < 2:
            raise RuntimeError(f"ID {sid} 应答长度错误")
        reply = self.ser.read(head[1])
        if len(reply) != head[1] or (sum(head) + sum(reply)) & 255 != 255:
            raise RuntimeError(f"ID {sid} 应答不完整或校验和错误")
        if head[0] != sid:
            raise RuntimeError(f"等待 ID {sid}，却收到 ID {head[0]}")
        if reply[0] and not status_ok:
            raise RuntimeError(f"ID {sid} 状态异常 0x{reply[0]:02x}")
        return reply[1:-1]

    def present(self, sid):
        try:
            self.request(sid, 1, status_ok=True)
            return True
        except TimeoutError:
            return False

    def read(self, sid, addr, size=1):
        data = self.request(sid, 2, bytes([addr, size]))
        if len(data) != size:
            raise RuntimeError(f"ID {sid} 地址 {addr} 应返回 {size} 字节，实际 {len(data)}")
        return int.from_bytes(data, "little")

    def write(self, sid, addr, value, size=1):
        data = value.to_bytes(size, "little")
        self.request(sid, 3, bytes([addr]) + data, silent=True)
        if self.read(sid, addr, size) != value:
            raise RuntimeError(f"ID {sid} 地址 {addr} 写入 {value} 后回读不一致")


def position(bus, sid):
    value = bus.read(sid, 56, 2)
    if value > 4095:
        raise RuntimeError(f"ID {sid} 位置 {value} 超出单圈范围")
    return value


def limits(bus, sid):
    if bus.read(sid, 2) != 0 or bus.read(sid, 30) != 1:
        raise RuntimeError(f"ID {sid} 需要小端、角度分辨率 1（4096 步/圈）")
    if bus.read(sid, 33) not in (0, 4):
        raise RuntimeError(f"ID {sid} 不是位置模式 0/4")
    low, high = bus.read(sid, 9, 2), bus.read(sid, 11, 2)
    if not 0 <= low < high <= 4095:
        raise RuntimeError(f"ID {sid} 不是有效单圈角度范围：{low}–{high}")
    return low, high


def zero(bus, ids):
    records = {}
    for sid in ids:
        low, high = limits(bus, sid)
        if not low <= 2048 <= high:
            raise RuntimeError(f"ID {sid} 的角度限位不包含中位 2048")
        records[sid] = {"position": position(bus, sid), "offset_raw": bus.read(sid, 31, 2),
                        "torque": bus.read(sid, 40), "lock": bus.read(sid, 55)}
    backup = Path.cwd() / f"servo-zero-backup-{time.time_ns()}.json"
    with backup.open("x", encoding="utf-8") as stream:
        json.dump(records, stream, ensure_ascii=False, indent=2)
    print(f"原偏移已保存：{backup}")
    completed = []
    for sid in ids:
        ok = False
        try:
            bus.write(sid, 55, 0)
            bus.write(sid, 40, 0)
            bus.request(sid, 0x0B, silent=True)
            deadline = time.monotonic() + 0.3
            while True:
                current = position(bus, sid)
                if abs(current - 2048) <= 4:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"校准后位置为 {current}；请支撑关节并检查 0x0B 支持情况")
                time.sleep(0.01)
            bus.write(sid, 42, current, 2)
            bus.write(sid, 55, 1)
            bus.write(sid, 40, records[sid]["torque"])
            ok = True
            completed.append(sid)
            print(f"ID {sid}: {records[sid]['position']} → {current}，当前位置已设为 0°")
        finally:
            if not ok:
                for addr, value in ((40, 0), (55, 1)):
                    try:
                        bus.write(sid, addr, value)
                    except (OSError, RuntimeError) as error:
                        print(f"ID {sid} 收尾失败：{error}", file=sys.stderr)
                print(f"校零中断：已完成 {completed}；请检查 ID {sid}，后续未处理。", file=sys.stderr)


def move(bus, sid, target):
    phase = bus.read(sid, 18)
    speed = max(1, round(120 / (1 if phase & 4 else 50)))
    current = bus.read(sid, 44, 2) or min(bus.read(sid, 28, 2), 500)
    if not current:
        raise RuntimeError(f"ID {sid} 目标电流及保护电流均为 0")
    # HD-1910 使用完整 WritePosEx，避免目标电流为 0 时只写位置不动作。
    data = struct.pack("<BHHH", 10, target, current, speed)
    bus.request(sid, 3, bytes([41]) + data, silent=True)
    if bus.read(sid, 42, 2) != target:
        raise RuntimeError(f"ID {sid} 目标位置回读失败")


def interactive(bus, args):
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    enabled, goals = set(), {}
    selected = args.id
    print("先托稳机器人。i 输入 ID；↑ +1°；↓ -1°；z 当前校零；a 全部校零；空格当前卸力；q 退出。")
    print("校零会短暂卸力并保存到电机；退出会卸力本次点动过的电机。")
    print(f"方向系数 {args.direction}（默认 -1 与 robotd 一致）；当前 ID {selected}")

    def key():
        value = os.read(fd, 1)
        if value == b"\x1b":
            for _ in range(2):
                if select.select([fd], [], [], 0.05)[0]:
                    value += os.read(fd, 1)
                else:
                    break
        return value

    try:
        tty.setcbreak(fd)
        while True:
            pressed = key()
            if pressed in (b"q", b"\x1b", b""):
                break
            try:
                if pressed == b"i":
                    termios.tcsetattr(fd, termios.TCSADRAIN, saved)
                    try:
                        candidate = servo_id(input("选择 ID："))
                        position(bus, candidate)
                        selected = candidate
                        goals.pop(selected, None)
                    finally:
                        tty.setcbreak(fd)
                elif pressed in (b"z", b"a"):
                    targets = [selected] if pressed == b"z" else args.ids
                    try:
                        zero(bus, targets)
                    finally:
                        for sid in targets:
                            goals.pop(sid, None)
                elif pressed == b" ":
                    bus.write(selected, 40, 0)
                    enabled.discard(selected)
                    goals.pop(selected, None)
                elif pressed in (b"\x1b[A", b"\x1bOA", b"\x1b[B", b"\x1bOB"):
                    low, high = limits(bus, selected)
                    actual = position(bus, selected)
                    torque = bus.read(selected, 40)
                    if selected not in goals or torque != 1:
                        if not low <= actual <= high:
                            raise RuntimeError("当前位置超出限位，不能使能")
                        move(bus, selected, actual)
                        enabled.add(selected)
                        bus.write(selected, 40, 1)
                        goals[selected] = float(actual)
                    delta = 1 if pressed.endswith(b"A") else -1
                    target = goals[selected] + delta * args.direction * 4096 / 360
                    if not low <= round(target) <= high:
                        raise ValueError("已到电机角度限位")
                    move(bus, selected, round(target))
                    goals[selected] = target
                else:
                    continue
                actual = position(bus, selected)
                angle = (actual - 2048) * 360 / 4096 * args.direction
                target = goals.get(selected, actual)
                goal_angle = (target - 2048) * 360 / 4096 * args.direction
                print(f"ID {selected}  实际 {angle:+.2f}° ({actual})  目标 {goal_angle:+.2f}°")
            except (ValueError, argparse.ArgumentTypeError) as error:
                print(f"输入/范围错误：{error}")
            except (OSError, RuntimeError) as error:
                print(f"操作失败：{error}")
                if selected in enabled:
                    bus.write(selected, 40, 0)
                    enabled.discard(selected)
                goals.pop(selected, None)
            termios.tcflush(fd, termios.TCIFLUSH)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        for sid in sorted(enabled):
            try:
                bus.write(sid, 40, 0)
            except (OSError, RuntimeError) as error:
                print(f"ID {sid} 退出卸力失败：{error}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyS4", help="默认 /dev/ttyS4；USB 使用 /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=1000000)
    parser.add_argument("--ids", type=parse_ids, default=DEFAULT_IDS, help="全部校零范围，默认 10-14,20-24,30-34")
    parser.add_argument("--id", type=servo_id, default=10, help="启动时选中的 ID")
    parser.add_argument("--zero", metavar="all/ID", help="直接校零全部或指定 ID，随后退出")
    parser.add_argument("--direction", type=int, choices=(-1, 1), default=-1, help="角度方向，默认与 robotd 相同")
    args = parser.parse_args()
    if args.baud <= 0:
        parser.error("波特率必须大于 0")
    targets = None
    if args.zero is not None:
        try:
            targets = args.ids if args.zero == "all" else [servo_id(args.zero)]
        except (ValueError, argparse.ArgumentTypeError) as error:
            parser.error(str(error))
    elif os.name != "posix" or not sys.stdin.isatty():
        parser.error("方向键模式需要板端 Linux/SSH 交互终端")
    bus = Bus(args.port, args.baud)
    for signum in (signal.SIGTERM, signal.SIGHUP) if os.name == "posix" else ():
        signal.signal(signum, lambda *_: sys.exit(130))
    try:
        if targets is not None:
            print("请确保机器人已经支撑稳固；开始以当前位置校零。")
            zero(bus, targets)
        else:
            interactive(bus, args)
    finally:
        bus.close()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n已退出。")
        sys.exit(130)
    except (OSError, RuntimeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        sys.exit(1)
