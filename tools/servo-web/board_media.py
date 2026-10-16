import subprocess
import threading
import time
from pathlib import Path


class BoardMedia:
    ACTIONS = {"audio_info", "play_hello", "record", "play_recording", "camera_info", "camera_capture"}
    DEVICE = "hw:CARD=microducki2s3,DEV=0"

    def __init__(self, directory):
        self.directory = Path(directory)
        self.data = self.directory / "data"
        self.lock = threading.Lock()
        self.worker = None
        self.busy = None
        self.state = {"audio": {"status": "idle", "message": "点击检查声卡、播放 Hello 或录音 5 秒"},
                      "camera": {"status": "idle", "message": "点击检查 IMX219 或拍照"}}

    @staticmethod
    def _run(args, timeout=10, check=True):
        result = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f"{args[0]} 退出码 {result.returncode}: {(result.stderr or result.stdout).strip()[-2000:]}")
        return result

    def get_status(self):
        with self.lock:
            state = {key: dict(value) for key, value in self.state.items()}
            state["busy"] = self.busy
        state["files"] = {}
        for name in ("mic.wav", "mic_left_loud.wav", "camera.jpg"):
            path = self.data / name
            if path.is_file():
                state["files"][name] = f"/api/board/media/{name}?v={path.stat().st_mtime_ns}"
        return state

    def start_action(self, action):
        if action not in self.ACTIONS:
            raise ValueError("未知硬件测试操作")
        group = "camera" if action.startswith("camera_") else "audio"
        with self.lock:
            if self.busy:
                raise RuntimeError("另一个硬件测试正在进行，请稍候")
            self.busy = action
            self.state[group] = {"status": "busy", "message": "录音 5 秒中，请对着板端麦克风说话…" if action == "record" else "测试进行中…"}
            self.worker = threading.Thread(target=self._perform, args=(action, group), daemon=True)
            self.worker.start()

    def stop(self):
        if self.worker:
            self.worker.join()

    def _perform(self, action, group):
        try:
            self.data.mkdir(exist_ok=True)
            if action == "audio_info":
                cards = Path("/proc/asound/cards")
                text = cards.read_text() if cards.exists() else "/proc/asound/cards 不存在"
                devices_ok = True
                for command in ("aplay", "arecord"):
                    result = self._run([command, "-l"], check=False)
                    devices_ok = devices_ok and result.returncode == 0
                    text += f"\n{command} -l（退出码 {result.returncode}）\n{result.stdout}{result.stderr}"
                status = "ok" if devices_ok and "microducki2s3" in text else "error"
                result = {"status": status, "message": text.strip()}
            elif action in ("play_hello", "play_recording"):
                path = self.directory / "assets" / "hello_loud.wav" if action == "play_hello" else self.data / "mic_left_loud.wav"
                if not path.is_file():
                    raise RuntimeError("请先完成一次录音" if action == "play_recording" else "hello_loud.wav 不存在")
                self._run(["aplay", "-D", self.DEVICE, str(path)], timeout=15)
                result = {"status": "ok", "message": f"板端播放结束：{path.name}；请确认实际听到声音。"}
            elif action == "record":
                raw = self.data / "mic.pending.wav"
                loud = self.data / "mic_left_loud.pending.wav"
                self._run(["arecord", "-D", self.DEVICE, "-c", "2", "-r", "48000", "-f", "S32_LE", "-d", "5", str(raw)], timeout=12)
                raw.replace(self.data / "mic.wav")
                (self.data / "mic_left_loud.wav").unlink(missing_ok=True)
                self._run(["sox", str(self.data / "mic.wav"), "-r", "48000", "-b", "16", str(loud),
                           "remix", "1", "highpass", "80", "compand", "0.005,0.15",
                           "6:-70,-70,-40,-20,-20,-10,0,-3", "gain", "-n", "-3", "remix", "1", "1"], timeout=10)
                loud.replace(self.data / "mic_left_loud.wav")
                result = {"status": "ok", "message": "录音完成：5 秒 / 48 kHz；已提取左声道并压缩增益。可在本机浏览器试听，或点击板端回放。"}
            elif action == "camera_info":
                result = self._camera_info()
            else:
                result = self._camera_capture()
        except Exception as exc:
            result = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
        with self.lock:
            self.state[group] = {**result, "timestamp_ms": int(time.time() * 1000)}
            self.busy = None

    def _camera_info(self):
        if not Path("/dev/media0").exists():
            return {"status": "error", "message": "/dev/media0 不存在：媒体设备没有建立。"}
        result = self._run(["media-ctl", "-d", "/dev/media0", "-p"])
        lines = [line.strip() for line in result.stdout.splitlines() if "imx219" in line.lower()]
        registered = any("entity" in line.lower() for line in lines)
        message = ("IMX219 已被驱动识别并注册：\n" + "\n".join(lines)) if registered else "该相机链路中没有注册 IMX219。"
        message += "\n/dev/video0 " + ("存在" if Path("/dev/video0").exists() else "不存在")
        return {"status": "ok" if registered else "error", "message": message}

    def _camera_capture(self):
        info = self._camera_info()
        if info["status"] != "ok":
            raise RuntimeError(info["message"])
        current = self._run(["systemctl", "show", "mediad.service", "--property=ActiveState", "--value"]).stdout.strip()
        if current == "deactivating":
            raise RuntimeError("mediad 正在停止，请稍后重试")
        restore = current in ("active", "activating", "reloading")
        pending = self.data / "camera.pending.jpg"
        pending.unlink(missing_ok=True)
        error = None
        try:
            if restore:
                self._run(["sudo", "-n", "systemctl", "stop", "mediad.service"])
            self._run(["gst-launch-1.0", "-e", "v4l2src", "device=/dev/video0", "io-mode=4", "num-buffers=60",
                       'extra-controls=c,exposure=600,analogue_gain=1024', "!",
                       "video/x-raw,format=NV12,width=1920,height=1080", "!", "videoconvert", "!",
                       "jpegenc", "!", "multifilesink", f"location={pending}"], timeout=30)
            if not pending.is_file() or pending.stat().st_size == 0:
                raise RuntimeError("相机命令结束，但没有生成 JPEG")
            pending.replace(self.data / "camera.jpg")
        except Exception as exc:
            error = str(exc)
        finally:
            if restore:
                try:
                    self._run(["sudo", "-n", "systemctl", "start", "mediad.service"], timeout=15)
                except Exception as exc:
                    error = (error + "；" if error else "") + f"恢复 mediad 失败：{exc}；请执行 sudo systemctl start mediad.service"
        if error:
            raise RuntimeError(error)
        return {"status": "ok", "message": info["message"] + "\n拍照成功：1920×1080 JPEG（60 帧中的最后一帧）。" +
                ("mediad 已恢复。" if restore else "mediad 原先未运行，保持停止。")}
