"""No microphone, network, robot socket or motors."""
import importlib.util
import hashlib
import json
import shlex
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare
import recognize


class SpeechTests(unittest.TestCase):
    def test_setup_version_guard_accepts_313_and_rejects_unsupported_versions(self):
        import builtins
        from types import SimpleNamespace
        setup = (Path(__file__).parent / "setup.sh").read_text(encoding="utf-8")
        gate = next(line for line in setup.splitlines() if line.startswith('"$python" -c '))
        code = shlex.split(gate)[2]
        for version, accepted in (((3, 10, 9), False), ((3, 11, 0), True), ((3, 12, 14), True),
                                  ((3, 13, 5), True), ((3, 14, 0), False)):
            def load(name, *args, **kwargs):
                return SimpleNamespace(version_info=version) if name == "sys" else __import__(name, *args, **kwargs)
            scope = {"__builtins__": dict(vars(builtins), __import__=load)}
            with self.subTest(version=version):
                if accepted:
                    exec(code, scope)
                else:
                    with self.assertRaises(AssertionError):
                        exec(code, scope)

    def test_numpy_version_markers_keep_old_environment_and_cover_313(self):
        # Evaluate these simple markers without requiring pip/packaging in system Python.
        lines = (Path(__file__).parent / "requirements.txt").read_text(encoding="utf-8").splitlines()
        numpy = [line.split(";", 1) for line in lines if line.startswith("numpy==")]
        self.assertEqual(len(numpy), 2)
        for version, expected in (("3.11", "numpy==1.26.4"), ("3.12", "numpy==1.26.4"), ("3.13", "numpy==2.2.6")):
            selected = [pin for pin, marker in numpy
                        if eval(marker.strip(), {"__builtins__": {}}, {"python_version": version})]
            self.assertEqual(selected, [expected])

    def test_digest_without_python_311_file_digest_including_multiple_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.bin"
            with patch.object(prepare.hashlib, "file_digest", None, create=True):
                for data in (b"", b"abc", b"x" * (1024 * 1024 + 17)):
                    path.write_bytes(data)
                    self.assertEqual(prepare.digest(path), hashlib.sha256(data).hexdigest())

    def test_keyword_cli_uses_current_interpreter_scripts_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / "Scripts"
            scripts.mkdir()
            cli = scripts / "sherpa-onnx-cli.exe"
            cli.write_bytes(b"test fixture, never executed")
            with patch.object(prepare.sysconfig, "get_path", return_value=str(scripts)), \
                    patch.object(prepare.sys, "platform", "win32"):
                self.assertEqual(prepare.keyword_cli(), cli)
                cli.unlink()
                with self.assertRaisesRegex(RuntimeError, "Missing"):
                    prepare.keyword_cli()

    def test_preview_is_whitelisted_and_deduplicated(self):
        p = recognize.Preview()
        self.assertIsNone(p.event("走", 0.))
        self.assertIsNone(p.event("未知", 0.))
        self.assertEqual(p.event("前进", 0.)["intent"], "walk")
        self.assertIsNone(p.event("前进", 1.))
        self.assertEqual(p.event("停止", 1.)["intent"], "stop")
        self.assertTrue(p.event("前进", 2.)["preview_only"])
        self.assertEqual(p.event("后退", 2.)["intent"], "backward")
        self.assertIsNone(p.event("鸭子前进", 4.))

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "Install independent speech requirements for PCM tests")
    def test_pcm_conversion_channel_gain_and_bounds(self):
        import numpy as np
        pcm = np.array([[-32768, 123], [32767, 456]], dtype="<i2").tobytes()
        np.testing.assert_allclose(recognize.pcm16(pcm, 2, 0, 2.), [-1., 1.])
        np.testing.assert_allclose(recognize.pcm16(pcm, 2, 1), np.array([123, 456]) / 32768., rtol=1e-6)
        with self.assertRaises(ValueError):
            recognize.pcm16(b"x")

    def test_manifest_detects_missing_and_changed_files(self):
        model_id, spec = prepare.catalog()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = [spec[k] for k in ("encoder", "decoder", "joiner", "tokens", "lexicon")]
            names += ["keywords.txt", "keywords_raw.txt"]
            for name in names:
                (root / name).write_bytes(b"fixture")
            manifest = {"model_id": model_id, "source_url": spec["url"],
                        "files": {name: prepare.digest(root / name) for name in names}}
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            prepare.verify(root, model_id, spec)
            (root / names[0]).write_bytes(b"changed")
            with self.assertRaises(ValueError):
                prepare.verify(root, model_id, spec)

    def test_archive_links_are_rejected_not_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bad.tar.bz2"
            with tarfile.open(archive, "w:bz2") as tar:
                member = tarfile.TarInfo("model/tokens.txt")
                member.type = tarfile.SYMTYPE
                member.linkname = "../../escape"
                tar.addfile(member)
            with self.assertRaises(ValueError):
                prepare.extract_selected(archive, root, ["tokens.txt"])
            self.assertFalse((root / "tokens.txt").exists())

    def test_recognition_has_no_robot_or_service_calls(self):
        tree = __import__("ast").parse(Path(recognize.__file__).read_text(encoding="utf-8"))
        imports = [n for n in __import__("ast").walk(tree) if isinstance(n, __import__("ast").Import)]
        self.assertNotIn("socket", [a.name for n in imports for a in n.names])
        source = Path(recognize.__file__).read_text(encoding="utf-8")
        for text in ("robot.move", "robot.enable", "robot.do", "systemctl", "/run/robotd.sock"):
            self.assertNotIn(text, source)

    @unittest.skipUnless(__import__("os").name == "posix", "ALSA is Linux-only")
    def test_alsa_command_and_cleanup_only_own_child(self):
        with patch.object(recognize.subprocess, "Popen") as popen:
            process = popen.return_value
            process.poll.return_value = None
            with recognize.alsa_capture("plughw:test,0") as child:
                self.assertIs(child, process)
            self.assertEqual(popen.call_args.args[0], ["arecord", "-q", "-D", "plughw:test,0",
                             "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1"])
            process.terminate.assert_called_once()
            process.stdout.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
