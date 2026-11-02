"""DICT_5X5_50 detection on JPEGs from the existing browser video, never on a camera device."""
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np

DICTIONARY = "DICT_5X5_50"
MAX_PIXELS = 1920 * 1080


def jpeg_dimensions(data):
    """Bound decoded allocation before passing browser JPEGs to OpenCV."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("需要 JPEG 图像")
    pos = 2
    while pos + 3 < len(data):
        if data[pos] != 255:
            raise ValueError("JPEG 头无效")
        while pos < len(data) and data[pos] == 255:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > len(data):
            break
        length = int.from_bytes(data[pos:pos + 2], "big")
        if length < 2 or pos + length > len(data):
            raise ValueError("JPEG 段长度无效")
        if marker in (0xC0, 0xC1, 0xC2):
            if length < 8:
                raise ValueError("JPEG 尺寸头无效")
            h = int.from_bytes(data[pos + 3:pos + 5], "big")
            w = int.from_bytes(data[pos + 5:pos + 7], "big")
            if w < 8 or h < 8 or w * h > MAX_PIXELS:
                raise ValueError("图像尺寸超限，最多 1920×1080 像素")
            return w, h
        if marker == 0xDA:
            break
        pos += length
    raise ValueError("无法读取 JPEG 尺寸")


def upright_points(points, width, height, rotate):
    p = np.asarray(points, dtype=np.float64).copy()
    if rotate == 90:
        return np.column_stack((height - 1 - p[:, 1], p[:, 0])), height, width
    if rotate == 180:
        return np.column_stack((width - 1 - p[:, 0], height - 1 - p[:, 1])), width, height
    if rotate == 270:
        return np.column_stack((p[:, 1], width - 1 - p[:, 0])), height, width
    if rotate != 0:
        raise ValueError("rotate 必须为 0/90/180/270")
    return p, width, height


class Calibration:
    def __init__(self, spec):
        if spec.get("coordinate_space") != "raw" or spec.get("camera_model") != "pinhole":
            raise ValueError("内参必须为未旋转原始视频的 pinhole/raw 标定；鱼眼模型须另行适配")
        self.width, self.height = int(spec["image_width"]), int(spec["image_height"])
        self.k = np.asarray(spec["camera_matrix"], dtype=np.float64)
        self.dist = np.asarray(spec["dist_coeffs"], dtype=np.float64).reshape(-1)
        if (self.width <= 0 or self.height <= 0 or self.k.shape != (3, 3) or
                self.dist.size not in (4, 5, 8, 12, 14) or not np.isfinite(self.k).all() or
                not np.isfinite(self.dist).all() or self.k[0, 0] <= 0 or self.k[1, 1] <= 0 or
                not 0 <= self.k[0, 2] <= self.width or not 0 <= self.k[1, 2] <= self.height or
                not np.allclose(self.k[2], [0, 0, 1]) or abs(self.k[0, 1]) > 1e-9 or abs(self.k[1, 0]) > 1e-9):
            raise ValueError("相机内参/畸变参数无效")

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def scaled(self, width, height):
        sx, sy = width / self.width, height / self.height
        if abs(sx / sy - 1) > .005:
            raise ValueError("分辨率宽高比与标定不同，禁止米制测距")
        k = self.k.copy()
        k[0, :] *= sx
        k[1, :] *= sy
        return k


class ArucoDetector:
    def __init__(self, marker_mm=50.0, calibration=None, max_reprojection_px=3.0):
        cv2.setNumThreads(1)
        if not math.isfinite(marker_mm) or not 1 <= marker_mm <= 1000:
            raise ValueError("黑色码外边长必须为 1～1000 mm")
        self.length = marker_mm / 1000.0
        self.calibration = Calibration.load(calibration) if calibration else None
        self.max_error = max_reprojection_px
        if not hasattr(cv2, "aruco"):
            raise RuntimeError("缺少 cv2.aruco，请在独立环境安装 requirements.txt")
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
        params = cv2.aruco.DetectorParameters()
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        self.detector = cv2.aruco.ArucoDetector(dictionary, params)
        half = self.length / 2
        # IPPE_SQUARE mandates this exact order, paired with decoded canonical corners.
        self.object_points = np.array([[-half, half, 0], [half, half, 0],
                                       [half, -half, 0], [-half, -half, 0]], dtype=np.float64)

    def info(self):
        return {"dictionary": DICTIONARY, "markerMm": self.length * 1000,
                "calibrated": self.calibration is not None,
                "coordinateSpace": "raw", "distanceDefinition": "camera_to_marker_center"}

    def pose(self, corners, width, height):
        if self.calibration is None:
            return {"distance_status": "uncalibrated"}
        try:
            k = self.calibration.scaled(width, height)
        except ValueError:
            return {"distance_status": "resolution_mismatch"}
        edges = np.linalg.norm(np.roll(corners, -1, axis=0) - corners, axis=1)
        if float(edges.min()) < 20:
            return {"distance_status": "marker_too_small"}
        result = cv2.solvePnPGeneric(self.object_points, corners, k, self.calibration.dist,
                                     flags=cv2.SOLVEPNP_IPPE_SQUARE)
        candidates = []
        if result[0]:
            for rvec, tvec in zip(result[1], result[2]):
                rvec, tvec = np.asarray(rvec).reshape(3, 1), np.asarray(tvec).reshape(3, 1)
                if not np.isfinite(rvec).all() or not np.isfinite(tvec).all() or tvec[2, 0] <= 0:
                    continue
                rotation, _ = cv2.Rodrigues(rvec)
                camera_points = (rotation @ self.object_points.T + tvec).T
                if np.any(camera_points[:, 2] <= 0) or abs(rotation[2, 2]) < .25:
                    continue
                projected, _ = cv2.projectPoints(self.object_points, rvec, tvec, k, self.calibration.dist)
                error = float(np.sqrt(np.mean(np.sum((projected.reshape(4, 2) - corners) ** 2, axis=1))))
                distance = float(np.linalg.norm(tvec))
                if not math.isfinite(error) or error > self.max_error or not .05 <= distance <= 10:
                    continue
                candidates.append((error, distance, float(tvec[2, 0]), tvec.flatten().tolist()))
        if not candidates:
            return {"distance_status": "pose_rejected"}
        error, distance, depth, tvec = min(candidates, key=lambda p: p[0])
        return {"distance_status": "calibrated", "distance_m": distance,
                "depth_m": depth, "tvec_m_raw": tvec, "reprojection_px": error}

    def detect(self, jpeg, rotate=0):
        started = time.monotonic()
        expected = jpeg_dimensions(jpeg)
        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None or (image.shape[1], image.shape[0]) != expected:
            raise ValueError("JPEG 解码失败或尺寸不一致")
        decoded_at = time.monotonic()
        height, width = image.shape
        corners, ids, _ = self.detector.detectMarkers(image)
        detected_at = time.monotonic()
        _, out_width, out_height = upright_points([[0, 0]], width, height, rotate)
        boxes = []
        if ids is not None:
            for raw_corners, marker_id in zip(corners, ids.flatten()):
                raw = np.asarray(raw_corners, dtype=np.float64).reshape(4, 2)
                transformed, _, _ = upright_points(raw, width, height, rotate)
                lo, hi = transformed.min(axis=0), transformed.max(axis=0)
                box = {"id": int(marker_id), "score": 1.0, "corners": transformed.tolist(),
                       "size_norm": float(np.mean(np.linalg.norm(np.roll(transformed, -1, axis=0) - transformed, axis=1)) / out_height),
                       "x0": float(max(0, lo[0])), "y0": float(max(0, lo[1])),
                       "x1": float(min(out_width, hi[0])), "y1": float(min(out_height, hi[1]))}
                box.update(self.pose(raw, width, height))
                boxes.append(box)
        finished_at = time.monotonic()
        return {"source": "aruco", "dictionary": DICTIONARY, "width": out_width,
                "height": out_height, "boxes": boxes,
                "decode_ms": (decoded_at - started) * 1000, "detect_ms": (detected_at - decoded_at) * 1000,
                "pose_ms": (finished_at - detected_at) * 1000,
                "took_ms": (finished_at - started) * 1000, "calibrated": self.calibration is not None}
