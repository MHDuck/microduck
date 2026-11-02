import functools
import contextlib
import io
import http.client
from http.server import ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import threading
import unittest


FOLDER = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("vision_server", FOLDER / "serve.py")
server_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server_module)


class ServerTests(unittest.TestCase):
    def test_low_rate_defaults_and_explicit_slow_parameters(self):
        args = server_module.parse_args(['--detector', 'aruco', '--aruco-hz', '.2'])
        self.assertEqual((args.aruco_hz, args.detection_max_age, args.aruco_http_timeout), (.2, 5, 8))
        self.assertEqual((args.follow_style, args.follow_step_seconds, args.follow_settle_seconds), ('step', 2.5, 1))
        self.assertFalse(args.follow_require_front)
        self.assertTrue(server_module.parse_args(['--follow-require-front']).follow_require_front)
        args = server_module.parse_args(['--detector', 'aruco', '--detection-max-age', '10',
                                        '--aruco-http-timeout', '15', '--target-timeout', '25'])
        self.assertEqual(args.detection_max_age, 10)

    def test_contradictory_or_unbounded_slow_settings_are_rejected(self):
        cases = [['--aruco-hz', '0'], ['--detection-max-age', '10', '--aruco-http-timeout', '8'],
                 ['--detector', 'aruco', '--aruco-hz', '.1', '--target-timeout', '12'],
                 ['--follow-step-seconds', '10'], ['--follow-settle-seconds', '0'], ['--video-timeout', 'nan'],
                 ['--detector', 'aruco', '--detection-max-age', '.8', '--aruco-http-timeout', '2',
                  '--target-timeout', '2.2', '--follow-step-seconds', '4']]
        for argv in cases:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                server_module.parse_args(argv)
            self.assertEqual(error.exception.code, 2)

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(server_module.Handler, directory=str(FOLDER)))
        cls.server.demo_config = {"robotHost": "", "signallingPort": 8443}
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def fetch(self, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_assets_and_configuration(self):
        for path in ("/", "/core.js", "/app.js"):
            status, headers, content = self.fetch(path)
            self.assertEqual(status, 200)
            self.assertTrue(content)
            self.assertEqual(headers["Cache-Control"], "no-store")
        status, _, payload = self.fetch("/config.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["signallingPort"], 8443)

    def test_nonpublic_files_and_traversal_are_not_served(self):
        for path in ("/serve.py", "/../deploy/robotd.toml", "/%2e%2e/AGENTS.md", "/README.md"):
            self.assertEqual(self.fetch(path)[0], 404)

    def test_head_and_favicon(self):
        self.assertEqual(self.fetch("/", "HEAD")[2], b"")
        self.assertEqual(self.fetch("/favicon.ico")[0], 204)

    def test_disabled_aruco_endpoint_is_explicit_and_other_posts_are_not_routed(self):
        status, _, payload = self.fetch('/aruco/detect', 'POST', b'jpeg', {'Content-Type': 'image/jpeg'})
        self.assertEqual(status, 503)
        self.assertIn('ArUco', json.loads(payload)['error'])
        self.assertEqual(self.fetch('/robot.enable', 'POST', b'{}')[0], 404)

    def test_aruco_post_bounds_rotation_and_frame_id(self):
        class Engine:
            def detect(self, data, rotate):
                return {'source': 'aruco', 'width': 10, 'height': 20, 'boxes': [], 'rotate': rotate}
        self.server.aruco_engine = Engine()
        self.server.aruco_lock = threading.Lock()
        try:
            status, _, payload = self.fetch('/aruco/detect?rotate=90&frame_id=7', 'POST', b'jpeg', {'Content-Type': 'image/jpeg'})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(payload)['frame_id'], 7)
            self.assertEqual(json.loads(payload)['rotate'], 90)
            self.assertEqual(self.fetch('/aruco/detect?rotate=45', 'POST', b'jpeg', {'Content-Type': 'image/jpeg'})[0], 400)
            self.assertEqual(self.fetch('/aruco/detect', 'POST', b'jpeg', {'Content-Type': 'text/plain'})[0], 400)
            with self.server.aruco_lock:
                self.assertEqual(self.fetch('/aruco/detect', 'POST', b'jpeg', {'Content-Type': 'image/jpeg'})[0], 429)
        finally:
            self.server.aruco_engine = None


if __name__ == "__main__":
    unittest.main()
