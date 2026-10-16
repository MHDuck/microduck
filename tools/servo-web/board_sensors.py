# SPDX-License-Identifier: GPL-3.0-or-later
# BNO08x protocol adapted from Microduck src/imu_reader.py, Copyright 2026 Marc Duclusaud.
# LSM6DSV16X / VL53L5CX setup follows the supplied board monitor scripts.
import math
import os
import struct
import threading
import time


class BoardSensors:
    backend = "bno085"
    demo = False

    def __init__(self):
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.threads = []
        self.frames = {name: {"status": "starting", "message": "等待初始化", "updated": None}
                       for name in ("bno085", "lsm6dsv16x", "vl53l5cx")}

    def start(self):
        for name, read in (("bno085", self._bno), ("lsm6dsv16x", self._lsm), ("vl53l5cx", self._tof)):
            worker = threading.Thread(target=self._worker, args=(name, read), daemon=True, name=name)
            self.threads.append(worker)
            worker.start()

    def stop(self):
        self.stopped.set()
        for worker in self.threads:
            worker.join(timeout=3)

    def _publish(self, name, **frame):
        with self.lock:
            self.frames[name] = frame

    def _worker(self, name, read):
        while not self.stopped.is_set():
            try:
                self._publish(name, status="starting", message="初始化中", updated=None)
                read()
            except Exception as exc:
                self._publish(name, status="error", message=f"{type(exc).__name__}: {exc}", updated=None)
            if self.stopped.wait(5):
                break

    def get_board_status(self):
        now = time.monotonic()
        with self.lock:
            frames = {key: dict(value) for key, value in self.frames.items()}
        for name, frame in frames.items():
            updated = frame.pop("updated", None)
            frame["age_ms"] = None if updated is None else round((now - updated) * 1000)
            if frame["status"] == "live" and (updated is None or now - updated > (5 if name == "vl53l5cx" else 1.5)):
                frame.update(status="stale", message="数据已停滞，等待重新连接")
        return frames

    def get_status(self):
        frame = self.get_board_status()["bno085"]
        live = frame["status"] == "live"
        return {**frame, "type": "imu", "backend": self.backend, "mode": "live",
                "connected": live, "live": live, "ready": int(live), "configured": int(live),
                "default_mount": "y-90", "timestamp_ms": int(time.time() * 1000)}

    def _bno(self):
        import fcntl

        fd = os.open("/dev/i2c-3", os.O_RDWR)
        try:
            fcntl.ioctl(fd, 0x0703, 0x4B)
            sequence = [0] * 6

            def send(channel, payload):
                packet = struct.pack("<HBB", len(payload) + 4, channel, sequence[channel]) + payload
                if os.write(fd, packet) != len(packet):
                    raise OSError("short SHTP write")
                sequence[channel] = (sequence[channel] + 1) & 255

            def receive():
                header = os.read(fd, 4)
                if len(header) != 4:
                    return None
                length, channel, _ = struct.unpack("<HBB", header)
                length &= 0x7FFF
                if length <= 4 or length > 8192 or channel > 5:
                    return None
                data = os.read(fd, length)
                if len(data) == length:
                    return channel, data[4:]
                if len(data) == length - 4:
                    return channel, data
                return None

            send(1, bytes([1]))
            if self.stopped.wait(0.8):
                return
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline and not self.stopped.is_set():
                try:
                    receive()
                except OSError:
                    pass
                self.stopped.wait(0.001)
            for report in (0x08, 0x02, 0x01):
                feature = bytearray(17)
                feature[0:2] = bytes([0xFD, report])
                struct.pack_into("<I", feature, 5, 20000)
                send(2, feature)
            values, stamps, seen = {}, {}, {}
            count = errors = 0
            rate_count, rate_time, hz = 0, time.monotonic(), 0.0
            started = time.monotonic()
            while not self.stopped.is_set():
                now = time.monotonic()
                try:
                    packet = receive()
                except OSError:
                    errors += 1
                    packet = None
                    self.stopped.wait(0.01)
                if packet and packet[0] in (3, 4):
                    payload, offset = packet[1], 0
                    while offset < len(payload):
                        report = payload[offset]
                        size = {0xFB: 5, 0x08: 12, 0x05: 14, 0x01: 10, 0x02: 10}.get(report)
                        if size is None:
                            offset += 1
                            continue
                        if offset + size > len(payload):
                            break
                        seq = payload[offset + 1]
                        if report != 0xFB and seen.get(report) != seq:
                            seen[report] = seq
                            if report in (0x08, 0x05):
                                q = [v / 16384 for v in struct.unpack_from("<hhhh", payload, offset + 4)]
                                norm = math.sqrt(sum(v * v for v in q))
                                if 0.5 < norm < 1.5:
                                    values["quaternion_xyzw"] = [v / norm for v in q]
                                    stamps["quat"] = now
                                    count += 1
                            else:
                                key, scale = ("gyro_dps", 180 / math.pi / 512) if report == 2 else ("accel_g", 1 / 256 / 9.80665)
                                values[key] = [v * scale for v in struct.unpack_from("<hhh", payload, offset + 4)]
                                stamps[key] = now
                        offset += size
                    if "quat" in stamps and "gyro_dps" in stamps:
                        if now - rate_time >= 1:
                            hz = (count - rate_count) / (now - rate_time)
                            rate_count, rate_time = count, now
                        fresh = dict(values)
                        if now - stamps.get("accel_g", started - 10) > 1.5:
                            fresh["accel_g"] = None
                        self._publish("bno085", status="live", updated=min(stamps["quat"], stamps["gyro_dps"]),
                                      message="I²C3 · 0x4B · Game Rotation Vector；偏航为相对角度",
                                      **fresh, sample_count=count, hz=round(hz, 1), error=errors)
                if now - min(stamps.get("quat", started), stamps.get("gyro_dps", started)) > 3:
                    raise RuntimeError("BNO085 超过 3 秒没有新的姿态 / 角速度数据")
                self.stopped.wait(0.001)
        finally:
            os.close(fd)

    def _lsm(self):
        from smbus2 import SMBus

        with SMBus(3) as bus:
            addr = 0x6A
            chip_id = bus.read_byte_data(addr, 0x0F)
            if chip_id != 0x70:
                raise RuntimeError(f"WHO_AM_I=0x{chip_id:02X}，预期 0x70")
            bus.write_byte_data(addr, 0x12, 0x01)
            deadline = time.monotonic() + 1
            while bus.read_byte_data(addr, 0x12) & 1:
                if time.monotonic() > deadline:
                    raise RuntimeError("LSM6DSV16X reset timed out")
                if self.stopped.wait(0.01):
                    return
            bus.write_byte_data(addr, 0x12, 0x44)
            bus.write_byte_data(addr, 0x15, 0x01)
            try:
                bus.write_byte_data(addr, 0x11, 0x05)
                last = time.monotonic()
                count = 0
                while not self.stopped.wait(0.1):
                    if bus.read_byte_data(addr, 0x1E) & 2:
                        raw = bus.read_i2c_block_data(addr, 0x22, 6)
                        gyro = [v * 0.00875 for v in struct.unpack("<hhh", bytes(raw))]
                        last = time.monotonic()
                        count += 1
                        self._publish("lsm6dsv16x", status="live", updated=last, who_am_i=chip_id,
                                      gyro_dps=gyro, sample_count=count,
                                      message="I²C3 · 0x6A · 60 Hz / ±250°/s · 显示 10 Hz · 传感器 XYZ 轴")
                    elif time.monotonic() - last > 1:
                        raise RuntimeError("LSM6DSV16X 超过 1 秒没有新的角速度数据")
            finally:
                bus.write_byte_data(addr, 0x11, 0)

    def _tof(self):
        from vl53l5cx.vl53l5cx import VL53L5CX

        sensor = VL53L5CX(bus_id=4)
        ranging = False
        try:
            if not sensor.is_alive():
                raise RuntimeError("VL53L5CX device/revision ID mismatch")
            self._publish("vl53l5cx", status="starting", updated=None, message="I²C4 · 0x29 · 正在上传测距固件，请稍候")
            sensor.init()
            sensor.set_resolution(64)
            sensor.set_ranging_frequency_hz(5)
            if sensor.get_resolution() != 64 or sensor.get_ranging_frequency_hz() != 5:
                raise RuntimeError("VL53L5CX configuration readback mismatch")
            if self.stopped.is_set():
                return
            sensor.start_ranging()
            ranging = True
            last, count = time.monotonic(), 0
            while not self.stopped.wait(0.01):
                if sensor.check_data_ready():
                    data = sensor.get_ranging_data()
                    distances = []
                    for zone in range(64):
                        i = zone * sensor.nb_target_per_zone
                        valid = data.nb_target_detected[zone] > 0 and data.target_status[i] in (5, 9)
                        distances.append(round(float(data.distance_mm[i])) if valid else None)
                    count += 1
                    last = time.monotonic()
                    self._publish("vl53l5cx", status="live", updated=last, distances_mm=distances,
                                  sample_count=count, message="I²C4 · 0x29 · 8×8 / 5 Hz · mm · 原始分区顺序")
                elif time.monotonic() - last > 5:
                    raise RuntimeError("VL53L5CX 超过 5 秒没有新测距帧")
        finally:
            try:
                if ranging:
                    sensor.stop_ranging()
            finally:
                sensor._i2c_bus.close()
