#!/usr/bin/env python3
"""Generate exact-size SVGs and calibrate a pinhole camera from downloaded raw video frames."""
import argparse
import json
from pathlib import Path
import sys

import cv2
import numpy as np


def svg_file(path, width, height, rectangles):
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:g}mm" height="{height:g}mm" viewBox="0 0 {width:g} {height:g}">',
             '<rect width="100%" height="100%" fill="white"/>']
    lines.extend(f'<rect x="{x:.9f}" y="{y:.9f}" width="{w:.9f}" height="{h:.9f}" fill="black"/>'
                 for x, y, w, h in rectangles)
    lines.append('</svg>')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def marker_svg(path, marker_id=0, marker_mm=50, margin_mm=10):
    if not 0 <= marker_id <= 49 or not 1 <= marker_mm <= 1000 or not 5 <= margin_mm <= 100:
        raise ValueError("ID 必须 0..49，黑色边长 1..1000 mm，白边至少 5 mm")
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    bits = cv2.aruco.generateImageMarker(dictionary, marker_id, 7)
    cell = marker_mm / 7
    rectangles = [(margin_mm + x * cell, margin_mm + y * cell, cell, cell)
                  for y in range(7) for x in range(7) if bits[y, x] == 0]
    svg_file(path, marker_mm + margin_mm * 2, marker_mm + margin_mm * 2, rectangles)


def board_svg(path, cols=9, rows=6, square_mm=20):
    if cols < 3 or rows < 3 or not 1 <= square_mm <= 100:
        raise ValueError("棋盘至少 3×3 内角点，格边长须为 1..100 mm")
    rectangles = [(square_mm * (x + 1), square_mm * (y + 1), square_mm, square_mm)
                  for y in range(rows + 1) for x in range(cols + 1) if (x + y) % 2 == 0]
    svg_file(path, (cols + 3) * square_mm, (rows + 3) * square_mm, rectangles)


def calibrate(folder, output, cols=9, rows=6, square_mm=20):
    if cols < 3 or rows < 3 or not 1 <= square_mm <= 100:
        raise ValueError("棋盘参数无效")
    grid = np.zeros((rows * cols, 3), dtype=np.float32)
    grid[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * (square_mm / 1000.0)
    objects, images, used = [], [], []
    size = None
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
            continue
        frame = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if frame is None:
            continue
        current_size = frame.shape[1], frame.shape[0]
        if size is not None and current_size != size:
            raise ValueError("照片分辨率不一致；请只用同一相机模式的原始未旋转照片")
        size = current_size
        found, corners = cv2.findChessboardCornersSB(frame, (cols, rows), cv2.CALIB_CB_NORMALIZE_IMAGE)
        if found:
            objects.append(grid.copy()); images.append(corners); used.append(path.name)
            print(f"accepted {path.name}")
        else:
            print(f"skipped (no complete board) {path.name}")
    if len(images) < 12:
        raise ValueError(f"只有 {len(images)} 张有效照片，至少需要 12 张不同位置/倾角照片，建议 20～30 张")
    rms, k, dist, rvecs, tvecs = cv2.calibrateCamera(objects, images, size, None, None)
    if not np.isfinite(k).all() or not np.isfinite(dist).all() or not np.isfinite(rms) or rms > 2:
        raise ValueError(f"标定质量不合格 RMS={rms:.3f} px；检查清晰度、棋盘参数和不同视角覆盖")
    errors = []
    for obj, img, rvec, tvec in zip(objects, images, rvecs, tvecs):
        projection, _ = cv2.projectPoints(obj, rvec, tvec, k, dist)
        errors.append(float(np.sqrt(np.mean(np.sum((projection.reshape(-1, 2) - img.reshape(-1, 2)) ** 2, axis=1)))))
    result = {"coordinate_space": "raw", "camera_model": "pinhole", "image_width": size[0],
              "image_height": size[1], "camera_matrix": k.tolist(), "dist_coeffs": dist.flatten().tolist(),
              "rms_px": float(rms), "views": used, "per_view_rms_px": errors,
              "board_inner_corners": [cols, rows], "square_mm": square_mm}
    # Validate the same schema as runtime, before writing anything to the destination.
    from aruco_detector import Calibration
    Calibration(result)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"wrote {output}, RMS={rms:.3f} px; verify known real distances before enabling metric follow")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    marker = sub.add_parser('marker')
    marker.add_argument('--id', type=int, default=0)
    marker.add_argument('--marker-mm', type=float, default=50)
    marker.add_argument('--output', type=Path, default=Path('aruco_5x5_50_id0_50mm.svg'))
    board = sub.add_parser('board')
    board.add_argument('--output', type=Path, default=Path('chessboard_9x6_20mm.svg'))
    calibration = sub.add_parser('calibrate')
    calibration.add_argument('--images', type=Path, required=True)
    calibration.add_argument('--output', type=Path, default=Path('camera.json'))
    for command in (board, calibration):
        command.add_argument('--cols', type=int, default=9, help='inner corners, not squares')
        command.add_argument('--rows', type=int, default=6, help='inner corners, not squares')
        command.add_argument('--square-mm', type=float, default=20)
    args = parser.parse_args()
    try:
        if args.command == 'marker':
            marker_svg(args.output, args.id, args.marker_mm)
            print(f"DICT_5X5_50 ID={args.id}; black outer side {args.marker_mm:g} mm; print at 100%, measure after printing")
        elif args.command == 'board':
            board_svg(args.output, args.cols, args.rows, args.square_mm)
            print(f"inner corners {args.cols}x{args.rows}; squares {args.square_mm:g} mm; print at 100%")
        else:
            calibrate(args.images, args.output, args.cols, args.rows, args.square_mm)
    except (ValueError, OSError, cv2.error) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == '__main__':
    main()
