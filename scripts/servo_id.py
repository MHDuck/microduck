#!/usr/bin/env python3
"""HD-1910 板端改 ID/对调 ID。与 servo_zero.py 放在同一目录；依赖 python3-serial。"""

import argparse
import signal
import sys
import time

from servo_zero import Bus, servo_id


def vacant(bus, sid):
    for _ in range(3):
        if bus.present(sid):
            raise RuntimeError(f"ID {sid} 已占用，请换一个空闲 ID")


def change(bus, old, new):
    if old == new:
        raise ValueError("新旧 ID 不能相同")
    if bus.read(old, 5) != old:
        raise RuntimeError(f"ID {old} 回读不一致")
    vacant(bus, new)
    print(f"执行 {old} -> {new}", flush=True)
    locked = False
    try:
        bus.write(old, 55, 0)
        # 改 ID 的应答可能用新 ID 或旧 ID；直接用新地址读回核对。
        bus.request(old, 3, bytes([5, new]), wait=False)
        time.sleep(0.03)
        if bus.read(new, 5) != new:
            raise RuntimeError(f"新 ID {new} 回读不一致")
        bus.write(new, 55, 1)
        locked = True
        if bus.present(old):
            raise RuntimeError(f"旧 ID {old} 仍应答，请检查重复 ID")
        print(f"完成 {old} -> {new}，已回读并加锁", flush=True)
    finally:
        if not locked:
            for sid in (new, old):
                try:
                    if bus.present(sid):
                        bus.write(sid, 55, 1)
                except (OSError, RuntimeError) as error:
                    print(f"ID {sid} 收尾加锁失败：{error}", file=sys.stderr)


def execute(bus, pairs):
    for old, new in pairs:
        try:
            change(bus, old, new)
        except BaseException:
            print(f"中断于 {old} -> {new}；已完成的步骤保持有效，未执行后续步骤。", file=sys.stderr)
            for sid in sorted({sid for pair in pairs for sid in pair}):
                try:
                    online = bus.present(sid)
                    print(f"ID {sid}：{'有应答' if online else '无应答'}", file=sys.stderr)
                except (OSError, RuntimeError) as error:
                    print(f"ID {sid}：{error}", file=sys.stderr)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyS4")
    parser.add_argument("--baud", type=int, default=1000000)
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--set", nargs=2, type=servo_id, metavar=("OLD", "NEW"), help="单个改 ID")
    operations.add_argument("--swap", nargs=2, type=servo_id, metavar=("A", "B"), help="通过临时 ID 对调")
    parser.add_argument("--temp", type=servo_id, default=50, help="对调临时 ID，默认 50")
    args = parser.parse_args()
    if args.baud <= 0:
        parser.error("波特率必须大于 0")
    pairs = None
    if args.set:
        if args.set[0] == args.set[1]:
            parser.error("新旧 ID 不能相同")
        pairs = [args.set]
    elif args.swap:
        a, b = args.swap
        if len({a, b, args.temp}) != 3:
            parser.error("两个电机 ID 和临时 ID 必须互不相同")
        pairs = [(a, args.temp), (b, a), (args.temp, b)]
    elif not sys.stdin.isatty():
        parser.error("请使用 --set OLD NEW 或 --swap A B")
    bus = Bus(args.port, args.baud)
    if sys.platform != "win32":
        for signum in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, lambda *_: sys.exit(130))
    try:
        if pairs is not None:
            if args.swap:
                bus.read(args.swap[0], 5)
                bus.read(args.swap[1], 5)
                vacant(bus, args.temp)
            execute(bus, pairs)
            return
        print("单个改号：set 21 50；对调：swap 21 22（临时 ID 50）；也可 swap 21 22 60；q 退出。")
        print("总线上的 ID 必须唯一；已有重复 ID 时，请单独接入一颗再改号。")
        while True:
            words = input("ID> ").split()
            if not words or words == ["q"]:
                break
            try:
                values = [servo_id(word) for word in words[1:]]
                if words[0] == "set" and len(values) == 2:
                    pairs = [values]
                elif words[0] == "swap" and len(values) in (2, 3):
                    a, b = values[:2]
                    temp = values[2] if len(values) == 3 else args.temp
                    if len({a, b, temp}) != 3:
                        raise ValueError("两个电机 ID 和临时 ID 必须互不相同")
                    bus.read(a, 5)
                    bus.read(b, 5)
                    vacant(bus, temp)
                    pairs = [(a, temp), (b, a), (temp, b)]
                else:
                    raise ValueError("格式：set OLD NEW 或 swap A B [TEMP]")
                execute(bus, pairs)
            except (ValueError, argparse.ArgumentTypeError, OSError, RuntimeError) as error:
                print(f"失败：{error}")
    finally:
        bus.close()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n已退出；已完成的改号步骤保持有效。")
        sys.exit(130)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        sys.exit(1)
