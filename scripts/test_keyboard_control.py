import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("keyboard-control.py")
SPEC = importlib.util.spec_from_file_location("keyboard_control_diagnostics", SCRIPT)
keyboard_control = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = keyboard_control
SPEC.loader.exec_module(keyboard_control)


class KeyboardControlTests(unittest.TestCase):
    def test_all_axes_increment_reverse_and_clamp_independently(self):
        for axis, positive, negative, limit in ((0, "w", "s", .4), (1, "a", "d", .4), (2, "q", "e", .3)):
            velocity = (0., 0., 0.)
            velocity = keyboard_control.adjust_velocity(velocity, positive)
            self.assertEqual(velocity[axis], .1)
            self.assertEqual(keyboard_control.adjust_velocity(velocity, negative), (0., 0., 0.))
            for _ in range(20):
                velocity = keyboard_control.adjust_velocity(velocity, positive)
            self.assertEqual(velocity[axis], limit)
            for _ in range(20):
                velocity = keyboard_control.adjust_velocity(velocity, negative)
            self.assertEqual(velocity[axis], -limit)
            self.assertTrue(all(v == 0 for i, v in enumerate(velocity) if i != axis))

    def test_speed_labels_and_units(self):
        text = keyboard_control.velocity_status((.1, -.2, .3))
        for expected in ("指令", "VX=+0.10", "VY=-0.20", "VTHETA=+0.30", "m/s", "rad/s"):
            self.assertIn(expected, text)

    def test_errors_show_full_paths_and_all_servo_ids_without_verbose(self):
        with patch.object(threading.Thread, "start"):
            diagnostics = keyboard_control.RobotdDiagnostics("/tmp/test-only.sock")
        errors = [
            "policy unavailable: loading /opt/robot/policies/missing.onnx: No such file or directory",
            "policy unavailable: loading /opt/robot/policies/other.onnx: No such file or directory",
            "bus read failed: hls servo sync_read: timeout; servo 22 (knee): timed out; servo 32 (head): checksum error",
            "write reg 40 on 14: timed out",
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for error in errors:
                diagnostics.report(error)
                diagnostics.display()
                diagnostics.report(error)
                diagnostics.display()
        for error in errors:
            self.assertEqual(output.getvalue().count(error), 1)
        self.assertIn("/opt/robot/policies/missing.onnx", diagnostics.brief_error(errors[0]))
        self.assertEqual(diagnostics.brief_error(errors[2]), "舵机 ID 22, 32 通信异常")
        self.assertEqual(diagnostics.brief_error(errors[3]), "舵机 ID 14 通信异常")
        self.assertEqual(diagnostics.brief_error("hls servo sync_read: timeout"), "电机通信异常")


@unittest.skipUnless(sys.platform == "linux", "PTY keyboard integration requires Linux")
class KeyboardTerminalTests(unittest.TestCase):
    def test_turn_persists_without_repeats_and_space_stops_it(self):
        import pty

        commands = []
        stopped = threading.Event()
        connections = []
        workers = []

        def serve(connection):
            try:
                with connection, connection.makefile("rb") as reader:
                    for line in reader:
                        request = json.loads(line)
                        method = request["method"]
                        if method == "robot.move":
                            commands.append((time.monotonic(), request["params"]))
                        if "id" not in request:
                            continue
                        result = {"accepted": True}
                        if method == "robot.health":
                            result.update(healthy=True, imu={"ready": True})
                        if method == "robot.subscribe":
                            result["stand"] = "test-stand.onnx"
                        connection.sendall((json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\n").encode())
                        if method == "robot.enable":
                            for policy in ("standup", "stand"):
                                state = {"policy": policy, "safety": {}, "move": {"applied": [0., 0., 0.]}}
                                connection.sendall((json.dumps({"jsonrpc": "2.0", "method": "robot.state", "params": state}) + "\n").encode())
            except (OSError, ValueError):
                pass

        with tempfile.TemporaryDirectory() as temp, socket.socket(socket.AF_UNIX) as server:
            path = str(Path(temp) / "robotd.sock")
            server.bind(path)
            server.listen()
            server.settimeout(.1)

            def accept():
                while not stopped.is_set():
                    try:
                        connection, _ = server.accept()
                    except socket.timeout:
                        continue
                    connections.append(connection)
                    worker = threading.Thread(target=serve, args=(connection,), daemon=True)
                    workers.append(worker)
                    worker.start()

            listener = threading.Thread(target=accept, daemon=True)
            listener.start()
            master, slave = pty.openpty()
            process = subprocess.Popen(
                [sys.executable, str(keyboard_control.__file__), "--socket", path],
                stdin=slave, stdout=slave, stderr=slave,
            )
            os.close(slave)
            output = bytearray()

            def wait_for(predicate, timeout=4.):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if select.select([master], [], [], .02)[0]:
                        output.extend(os.read(master, 65536))
                    if predicate():
                        return
                self.fail(output.decode(errors="replace"))

            try:
                wait_for(lambda: b"VTHETA=" in output)
                os.write(master, b"\n")
                wait_for(lambda: "起身完成".encode() in output)
                os.write(master, b"q")
                wait_for(lambda: any(c["vyaw"] > 0 for _, c in commands))
                first_turn = next(t for t, c in commands if c["vyaw"] > 0)
                wait_for(lambda: any(t - first_turn > .65 and c["vyaw"] == .1 for t, c in commands))
                os.write(master, b"w")
                wait_for(lambda: any(c["vx"] == .1 and c["vyaw"] == .1 for _, c in commands))
                commands.clear()
                os.write(master, b" ")
                wait_for(lambda: any(all(v == 0 for v in c.values()) for _, c in commands))
                os.write(master, b"\x1b")
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0, output.decode(errors="replace"))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
                stopped.set()
                listener.join(timeout=1)
                for connection in connections:
                    with contextlib.suppress(OSError):
                        connection.shutdown(socket.SHUT_RDWR)
                for worker in workers:
                    worker.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
