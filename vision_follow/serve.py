#!/usr/bin/env python3
"""Independent demo host with optional ArUco JPEG processing. No robot/camera device access."""
import argparse
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit


class Handler(SimpleHTTPRequestHandler):
    def json_response(self, status, value):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path != "/aruco/detect":
            self.json_response(404, {"error": "unknown endpoint"})
            return
        engine = getattr(self.server, "aruco_engine", None)
        if engine is None:
            self.json_response(503, {"error": "ArUco 未启用，请使用独立环境并以 --detector aruco 启动"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 1024 * 1024:
                raise ValueError("JPEG 长度须为 1～1048576 字节")
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "image/jpeg":
                raise ValueError("Content-Type 必须为 image/jpeg")
            query = parse_qs(url.query, max_num_fields=8)
            rotate = int(query.get("rotate", ["0"])[0])
            frame_id = int(query.get("frame_id", ["0"])[0])
            if rotate not in (0, 90, 180, 270) or not 0 <= frame_id <= 2147483647:
                raise ValueError("图像方向或序号无效")
            self.connection.settimeout(getattr(self.server, "aruco_upload_timeout", 9))
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError("JPEG 上传不完整")
        except (ValueError, OSError) as error:
            self.json_response(400, {"error": str(error)})
            return
        if not self.server.aruco_lock.acquire(blocking=False):
            self.json_response(429, {"error": "ArUco 正在处理上一帧，请只开一个控制页面"})
            return
        try:
            result = engine.detect(data, rotate)
            result["frame_id"] = frame_id
            self.json_response(200, result)
        except ValueError as error:
            self.json_response(400, {"error": str(error)})
        except Exception as error:
            self.json_response(500, {"error": f"ArUco 检测失败：{error}"})
        finally:
            self.server.aruco_lock.release()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        self.send_demo(path, body=True)

    def do_HEAD(self):
        self.send_demo(self.path.split("?", 1)[0], body=False)

    def send_demo(self, path, body):
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/config.json":
            payload = json.dumps(self.server.demo_config).encode("utf-8")
            mime = "application/json; charset=utf-8"
        elif path in ("/", "/index.html", "/core.js", "/app.js", "/aruco_client.js",
                      "/aruco_5x5_50_id0_50mm.svg", "/chessboard_9x6_20mm.svg"):
            name = "index.html" if path == "/" else path[1:]
            payload = (Path(self.directory) / name).read_bytes()
            mime = ("text/html; charset=utf-8" if name.endswith("html") else
                    "image/svg+xml" if name.endswith("svg") else "text/javascript; charset=utf-8")
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body:
            self.wfile.write(payload)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--signalling-port", type=int, default=8443)
    parser.add_argument("--robot-host", default="", help="default: browser page hostname")
    parser.add_argument("--detector", choices=("duck", "aruco"), default="duck")
    parser.add_argument("--marker-id", type=int, default=0)
    parser.add_argument("--marker-mm", type=float, default=50, help="measured outer black square side, millimeters")
    parser.add_argument("--calibration", type=Path, help="raw video pinhole camera calibration JSON")
    parser.add_argument("--aruco-hz", type=float, default=.5, help="frame upload ceiling, .1..5 Hz")
    parser.add_argument("--aruco-max-side", type=int, choices=(640, 800, 960), default=640)
    parser.add_argument("--aruco-jpeg-quality", type=float, default=.7)
    parser.add_argument("--detection-max-age", type=float, default=5, help="accepted capture-to-result seconds, .8..15")
    parser.add_argument("--aruco-http-timeout", type=float, default=8, help="HTTP seconds, 2..30; >= detection-max-age")
    parser.add_argument("--target-timeout", type=float, default=12, help="target retention seconds, .6..60")
    parser.add_argument("--video-timeout", type=float, default=10, help="decoded-video freeze seconds, 1.5..30")
    parser.add_argument("--follow-style", choices=("step", "continuous"), default="step")
    parser.add_argument("--follow-step-seconds", type=float, default=2.5)
    parser.add_argument("--follow-settle-seconds", type=float, default=1)
    parser.add_argument("--follow-require-front", action="store_true", help="optional alignment gate for step follow; off by default")
    args = parser.parse_args(argv)
    if not all(1 <= p <= 65535 for p in (args.port, args.signalling_port)):
        parser.error("ports must be 1..65535")
    if not 0 <= args.marker_id <= 49:
        parser.error("DICT_5X5_50 marker ID must be 0..49")
    if not .1 <= args.aruco_hz <= 5:
        parser.error("aruco-hz must be .1..5")
    if not .6 <= args.aruco_jpeg_quality <= .9:
        parser.error("aruco-jpeg-quality must be .6.. .9")
    if not .8 <= args.detection_max_age <= 15 or not 2 <= args.aruco_http_timeout <= 30 or args.aruco_http_timeout < args.detection_max_age:
        parser.error("detection-max-age must be .8..15; aruco-http-timeout must be 2..30 and >= detection-max-age")
    if not .6 <= args.target_timeout <= 60 or not 1.5 <= args.video_timeout <= 30:
        parser.error("target-timeout must be .6..60; video-timeout must be 1.5..30")
    if not .2 <= args.follow_step_seconds <= 4 or not .3 <= args.follow_settle_seconds <= 5:
        parser.error("follow-step-seconds must be .2..4; follow-settle-seconds must be .3..5")
    if args.detector == "aruco" and args.target_timeout < args.detection_max_age + max(1 / args.aruco_hz, args.detection_max_age) + .5:
        parser.error("target-timeout too short for two-frame confirmation at this detection rate/latency")
    if args.detector == "aruco" and args.follow_style == "step" and args.target_timeout < args.detection_max_age + args.follow_step_seconds + .2:
        parser.error("target-timeout too short to finish this movement segment")
    return args


def main():
    args = parse_args()
    engine = None
    if args.detector == "aruco":
        try:
            from aruco_detector import ArucoDetector
            engine = ArucoDetector(args.marker_mm, args.calibration)
        except (ImportError, ValueError, OSError, KeyError) as error:
            raise SystemExit(f"ArUco 启动失败：{error}；请在独立 .venv 中安装 requirements.txt 并检查内参") from error
    folder = Path(__file__).resolve().parent
    handler = functools.partial(Handler, directory=str(folder))
    server = ThreadingHTTPServer((args.bind, args.port), handler)
    server.demo_config = {"robotHost": args.robot_host, "signallingPort": args.signalling_port,
                          "detector": args.detector, "markerId": args.marker_id,
                          "arucoHz": args.aruco_hz,
                          "arucoMaxSide": args.aruco_max_side, "arucoJpegQuality": args.aruco_jpeg_quality,
                          "detectionMaxAge": args.detection_max_age, "arucoHttpTimeout": args.aruco_http_timeout,
                          "targetTimeout": args.target_timeout, "videoTimeout": args.video_timeout,
                          "followStyle": args.follow_style, "followStepSeconds": args.follow_step_seconds,
                          "followSettleSeconds": args.follow_settle_seconds,
                          "followRequireFront": args.follow_require_front,
                          "aruco": engine.info() if engine else None}
    server.aruco_engine = engine
    server.aruco_lock = threading.Lock()
    server.aruco_upload_timeout = args.aruco_http_timeout + 1
    print(f"MicroDuck 独立视觉演示：http://<板子IP>:{args.port}/", flush=True)
    print(f"检测源：{args.detector}；默认仅预览，浏览器手动启动后才控制。", flush=True)
    print("此进程不访问 robotd/舵机/摄像头设备。ArUco 只处理浏览器上传的 JPEG。Ctrl-C 退出。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
