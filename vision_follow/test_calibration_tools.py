from pathlib import Path
import sys
import tempfile
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aruco_tools import calibrate


class CalibrationToolTests(unittest.TestCase):
    def test_insufficient_images_never_write_fake_intrinsics(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'camera.json'
            with self.assertRaises(ValueError): calibrate(Path(folder), output)
            self.assertFalse(output.exists())

    def test_synthetic_perspective_chessboards_calibrate_known_camera(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            board = np.full((560, 800), 255, np.uint8)
            for y in range(7):
                for x in range(10):
                    if (x + y) % 2 == 0: board[y * 80:(y + 1) * 80, x * 80:(x + 1) * 80] = 0
            source = np.array([[0, 0], [800, 0], [800, 560], [0, 560]], np.float32)
            outer = np.array([[-.02, -.02, 0], [.18, -.02, 0], [.18, .12, 0], [-.02, .12, 0]], np.float64)
            k = np.array([[800, 0, 640], [0, 800, 360], [0, 0, 1]], np.float64)
            for n in range(15):
                r = np.array([.15 + .08 * (n % 3), -.3 + .1 * (n % 5), .03 * (n % 4)])
                t = np.array([-.08 + .02 * (n % 4), -.06 + .015 * (n % 3), .48 + .025 * (n % 5)])
                projected, _ = cv2.projectPoints(outer, r, t, k, np.zeros(5))
                transform = cv2.getPerspectiveTransform(source, projected.reshape(4, 2).astype(np.float32))
                image = cv2.warpPerspective(board, transform, (1280, 720), borderValue=255)
                self.assertTrue(cv2.imwrite(str(folder / f'{n:02}.png'), image))
            result = calibrate(folder, folder / 'camera.json')
            self.assertEqual(result['coordinate_space'], 'raw')
            self.assertLess(result['rms_px'], .5)
            self.assertAlmostEqual(result['camera_matrix'][0][0], 800, delta=40)
            self.assertAlmostEqual(result['camera_matrix'][1][1], 800, delta=40)


if __name__ == '__main__':
    unittest.main()
