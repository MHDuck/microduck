import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aruco_detector import ArucoDetector, Calibration, jpeg_dimensions, upright_points
from aruco_tools import marker_svg, board_svg


def intrinsics():
    return {"coordinate_space": "raw", "camera_model": "pinhole", "image_width": 1280,
            "image_height": 720, "camera_matrix": [[800, 0, 640], [0, 800, 360], [0, 0, 1]],
            "dist_coeffs": [0, 0, 0, 0, 0]}


def marker_image(marker_id=0, pixels=100):
    canvas = np.full((720, 1280), 255, dtype=np.uint8)
    marker = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50), marker_id, pixels)
    x, y = (1280 - pixels) // 2, (720 - pixels) // 2
    canvas[y:y + pixels, x:x + pixels] = marker
    return canvas


def jpeg(image):
    ok, data = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return data.tobytes()


class ArucoTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.calibration = Path(self.folder.name) / 'camera.json'
        self.calibration.write_text(json.dumps(intrinsics()), encoding='utf-8')

    def test_actual_generated_markers_detect_correct_ids_without_fake_distance(self):
        detector = ArucoDetector()
        for marker_id in (0, 1, 49):
            result = detector.detect(jpeg(marker_image(marker_id)))
            self.assertEqual([b['id'] for b in result['boxes']], [marker_id])
            self.assertEqual(result['boxes'][0]['distance_status'], 'uncalibrated')
            self.assertNotIn('distance_m', result['boxes'][0])
        self.assertEqual(detector.detect(jpeg(np.full((720, 1280), 255, np.uint8)))['boxes'], [])

    def test_known_size_front_marker_distance_and_all_mount_rotations(self):
        detector = ArucoDetector(marker_mm=50, calibration=self.calibration)
        for rotate in (0, 90, 180, 270):
            result = detector.detect(jpeg(marker_image()), rotate)
            b = result['boxes'][0]
            self.assertEqual(b['distance_status'], 'calibrated')
            self.assertAlmostEqual(b['distance_m'], .4, delta=.01)
            self.assertEqual((result['width'], result['height']), (720, 1280) if rotate in (90, 270) else (1280, 720))
            self.assertEqual(len(b['corners']), 4)

    def test_pose_round_trip_for_tilted_and_scaled_corners(self):
        detector = ArucoDetector(marker_mm=50, calibration=self.calibration)
        k = np.asarray(intrinsics()['camera_matrix'], np.float64)
        tvec = np.array([[.06], [.02], [.75]])
        rvec = np.array([[math.pi - .25], [.15], [.05]])
        corners, _ = cv2.projectPoints(detector.object_points, rvec, tvec, k, np.zeros(5))
        for scale in (1, .75):
            pose = detector.pose(corners.reshape(4, 2) * scale, int(1280 * scale), int(720 * scale))
            self.assertEqual(pose['distance_status'], 'calibrated')
            self.assertAlmostEqual(pose['distance_m'], float(np.linalg.norm(tvec)), delta=1e-5)
            self.assertLess(pose['reprojection_px'], 1e-5)

    def test_upside_down_mount_offset_matches_once_rotated_pixels(self):
        image = np.roll(marker_image(), shift=(-100, 180), axis=(0, 1))
        detector = ArucoDetector(marker_mm=50, calibration=self.calibration)
        before = detector.detect(jpeg(image), 90)['boxes'][0]
        after = detector.detect(jpeg(image), 270)['boxes'][0]
        # Adding 180 to the old upright direction reverses both image axes, without
        # changing the raw-calibrated physical distance in metadata mode.
        self.assertAlmostEqual(before['x0'] + after['x1'], 720, delta=1)
        self.assertAlmostEqual(before['y0'] + after['y1'], 1280, delta=1)
        self.assertAlmostEqual(before['distance_m'], after['distance_m'], places=8)
        turned = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
        physical = ArucoDetector().detect(jpeg(turned), 0)
        self.assertEqual((physical['width'], physical['height']), (720, 1280))
        b = physical['boxes'][0]
        self.assertEqual(b['id'], after['id'])
        for key in ('x0', 'x1', 'y0', 'y1'):
            self.assertAlmostEqual(b[key], after[key], delta=2)

    def test_reduced_upload_recognizes_marker_and_preserves_physical_scale(self):
        image = cv2.resize(marker_image(), (640, 360), interpolation=cv2.INTER_AREA)
        ok, data = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 70])
        self.assertTrue(ok)
        result = ArucoDetector(marker_mm=50, calibration=self.calibration).detect(data.tobytes())
        self.assertEqual([b['id'] for b in result['boxes']], [0])
        self.assertEqual(result['boxes'][0]['distance_status'], 'calibrated')
        self.assertAlmostEqual(result['boxes'][0]['distance_m'], .4, delta=.025)
        timings = [result[name] for name in ('decode_ms', 'detect_ms', 'pose_ms')]
        self.assertTrue(all(t >= 0 for t in timings))
        self.assertAlmostEqual(sum(timings), result['took_ms'], places=6)

    def test_marker_size_is_physical_scale_and_not_white_margin(self):
        a = ArucoDetector(marker_mm=50, calibration=self.calibration).detect(jpeg(marker_image()))['boxes'][0]
        b = ArucoDetector(marker_mm=100, calibration=self.calibration).detect(jpeg(marker_image()))['boxes'][0]
        self.assertAlmostEqual(b['distance_m'], a['distance_m'] * 2, delta=1e-6)

    def test_calibration_validation_and_resolution_mismatch(self):
        spec = intrinsics(); spec['coordinate_space'] = 'upright'
        with self.assertRaises(ValueError): Calibration(spec)
        spec = intrinsics(); spec['camera_matrix'][0][0] = -1
        with self.assertRaises(ValueError): Calibration(spec)
        detector = ArucoDetector(calibration=self.calibration)
        self.assertEqual(detector.pose(np.array([[10, 10], [50, 10], [50, 50], [10, 50]], np.float64), 800, 600)['distance_status'], 'resolution_mismatch')

    def test_rotation_coordinates_and_invalid_input(self):
        p, w, h = upright_points([[0, 0], [1279, 719]], 1280, 720, 90)
        np.testing.assert_array_equal(p, [[719, 0], [0, 1279]])
        self.assertEqual((w, h), (720, 1280))
        for invalid in (b'not a jpeg', b'\xff\xd8\xff\xc0\x00\x08\x08\xff\xff\xff\xff\x00'):
            with self.assertRaises(ValueError): jpeg_dimensions(invalid)
        with self.assertRaises(ValueError): upright_points([[0, 0]], 1280, 720, 1)

    def test_tiny_marker_and_inconsistent_quadrilateral_do_not_report_metric_distance(self):
        detector = ArucoDetector(calibration=self.calibration)
        tiny = np.array([[630, 350], [640, 350], [640, 360], [630, 360]], np.float64)
        self.assertEqual(detector.pose(tiny, 1280, 720)['distance_status'], 'marker_too_small')
        bad = np.array([[500, 300], [700, 300], [760, 510], [500, 400]], np.float64)
        result = detector.pose(bad, 1280, 720)
        self.assertEqual(result['distance_status'], 'pose_rejected')
        self.assertNotIn('distance_m', result)

    def test_marker_svg_actual_dictionary_bits_and_physical_dimensions(self):
        import xml.etree.ElementTree as et
        path = Path(self.folder.name) / 'marker.svg'
        marker_svg(path)
        svg = et.fromstring(path.read_text(encoding='utf-8'))
        self.assertEqual(svg.attrib['width'], '70mm')
        image = np.full((700, 700), 255, np.uint8)
        for rect in list(svg)[1:]:
            x, y, w, h = [float(rect.attrib[k]) for k in ('x', 'y', 'width', 'height')]
            image[round(y * 10):round((y + h) * 10), round(x * 10):round((x + w) * 10)] = 0
        result = ArucoDetector().detect(jpeg(image))
        self.assertEqual(result['boxes'][0]['id'], 0)
        board_svg(Path(self.folder.name) / 'board.svg')
        with self.assertRaises(ValueError): marker_svg(path, marker_id=50)


if __name__ == '__main__':
    unittest.main()
