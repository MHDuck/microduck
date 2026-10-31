"""Run with Linux/WSL unittest. SSH, sudo and installers are isolated fakes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "linux", "Deployment scripts run on Linux/WSL")
class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="duck deployment ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.log = self.root / "calls"
        self.env = dict(os.environ, HOME=str(self.home), REMOTE=str(self.remote), STUB_BIN=str(self.bin),
                        CALLS=str(self.log), PATH=f"{self.bin}:/usr/bin:/bin")
        for key in ("PYTHON", "VIRTUAL_ENV", "PYTHONPATH", "DUCK_DEV_SECRET_KEY"):
            self.env.pop(key, None)

    def write(self, path, contents):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)
        return path

    def fake(self, name, body):
        return self.write(self.bin / name, f"#!{sys.executable}\n"
                          "import os, sys, subprocess, pathlib, json, shutil\n"
                          "with open(os.environ['CALLS'], 'a') as f:\n"
                          "    f.write(json.dumps([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')\n"
                          + body)

    def run_script(self, path, *args, success=True):
        result = subprocess.run(["bash", str(path), *args], env=self.env,
                                text=True, capture_output=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def speech_fixture(self):
        speech = self.root / "speech"
        speech.mkdir()
        for path in (ROOT / "speech").iterdir():
            if path.is_file():
                shutil.copyfile(path, speech / path.name)
        catalog = json.loads((speech / "catalog.json").read_text())
        model_id = catalog["default"]
        spec = catalog["models"][model_id]
        model = speech / "models" / model_id
        names = [spec[k] for k in ("encoder", "decoder", "joiner", "tokens", "lexicon")]
        names += ["keywords.txt", "keywords_raw.txt"]
        for name in names:
            self.write(model / name, "fixture")
        shutil.copyfile(speech / "keywords_raw.txt", model / "keywords_raw.txt")
        self.write(model / "manifest.json", json.dumps({
            "model_id": model_id, "source_url": spec["url"],
            "files": {name: hashlib.sha256((model / name).read_bytes()).hexdigest() for name in names},
        }))
        self.write(speech / "setup.sh", '#!/bin/sh\nexit "${SETUP_EXIT:-0}"\n')
        self.fake("ssh", """
env = dict(os.environ, HOME=os.environ['REMOTE'])
sys.exit(subprocess.run(['sh', '-c', sys.argv[-1]], cwd=env['HOME'], env=env).returncode)
""")
        self.fake("scp", """
target = pathlib.Path(os.environ['REMOTE']) / sys.argv[-1].rsplit(':', 1)[1]
shutil.copyfile(sys.argv[-2], target)
""")
        self.fake("sudo", "sys.exit(int(os.environ.get('APT_EXIT', '0')))\n")
        current = self.remote / "microduck-speech/current"
        self.write(current.parent / "releases/old/keep", "old")
        current.symlink_to("releases/old")
        return speech, current

    def test_speech_upload_only_and_payload(self):
        speech, current = self.speech_fixture()
        self.write(speech / "dist/wheels-cp313-aarch64/cached.whl", "wheel")
        self.write(speech / "dist/wheels-cp313-aarch64/incomplete.whl.part", "partial")
        self.write(speech / ".venv/secret", "never upload")
        self.run_script(speech / "sync.sh", "radxa@2001:db8::1")
        self.assertEqual(os.readlink(current), "releases/old")
        self.assertFalse(any(call[0] == "sudo" for call in self.calls()))
        scp = next(call for call in self.calls() if call[0] == "scp")
        self.assertTrue(scp[-1].startswith("radxa@[2001:db8::1]:"))
        with tarfile.open(next((speech / "dist").glob("*.tar.gz"))) as bundle:
            names = bundle.getnames()
        self.assertIn("dist/wheels-cp313-aarch64/cached.whl", names)
        self.assertFalse(any(".venv" in name or name.endswith(".part") for name in names))

    def test_speech_install_switches_only_after_success(self):
        speech, current = self.speech_fixture()
        for failure in ("APT_EXIT", "SETUP_EXIT"):
            with self.subTest(failure=failure):
                self.env[failure] = "7"
                self.run_script(speech / "sync.sh", "--install", "radxa@board", success=False)
                self.assertEqual(os.readlink(current), "releases/old")
                self.env.pop(failure)
        self.run_script(speech / "sync.sh", "--install", "radxa@board")
        self.assertTrue(current.is_dir())
        self.assertNotEqual(os.readlink(current), "releases/old")
        self.assertTrue(any("libportaudio2" in call for call in self.calls()))

    def test_stale_keywords_stop_before_network(self):
        speech, _ = self.speech_fixture()
        self.write(speech / "keywords_raw.txt", "changed")
        self.run_script(speech / "sync.sh", "--install", "radxa@board", success=False)
        self.assertEqual(self.calls(), [])

    def python_fixture(self):
        return self.fake("python3", """
name = pathlib.Path(sys.argv[0])
version = os.environ.get('SYSTEM_PYTHON', '3.14') if name.parent == pathlib.Path(os.environ['STUB_BIN']) or 'duck-build' in str(name) else '3.12'
if sys.argv[1] == '-c':
    if 'import pip' in sys.argv[2]: sys.exit(0)
    sys.version_info = tuple(map(int, version.split('.'))) + (0,)
    exec(sys.argv[2])
elif sys.argv[1:3] == ['-m', 'venv']:
    target = pathlib.Path(sys.argv[3]) / 'bin'
    target.mkdir(parents=True)
    shutil.copyfile(name, target / 'python')
    (target / 'python').chmod(0o755)
    (target / 'python3').symlink_to('python')
    import shlex
    (target / 'activate').write_text('export VIRTUAL_ENV=' + shlex.quote(str(target.parent)) + '\\nexport PATH=' + shlex.quote(str(target)) + ':"$PATH"\\n')
elif sys.argv[1:3] == ['-m', 'pip']:
    sys.exit(int(os.environ.get('PIP_EXIT', '0')))
""")

    def test_setup_uses_managed_python_and_reuses_environment(self):
        setup = self.root / "speech/setup.sh"
        self.write(setup, (ROOT / "speech/setup.sh").read_text())
        python = self.python_fixture()
        managed = self.root / "managed/python"
        self.write(managed, python.read_text())
        self.fake("uv", f"print({str(managed)!r})\n")
        self.run_script(setup)
        self.run_script(setup)
        self.assertEqual(sum(call[1:3] == ["-m", "venv"] for call in self.calls()), 1)
        self.assertEqual(sum(call[0] == "uv" for call in self.calls()), 1)
        self.assertTrue(any(call[1:] == ["recognize.py", "--check"] for call in self.calls()))
        self.log.unlink()
        self.env["PIP_EXIT"] = "8"
        self.run_script(setup, success=False)
        self.assertFalse(any("prepare.py" in call for call in self.calls()))

    def test_setup_rejects_windows_environment_without_changing_it(self):
        setup = self.write(self.root / "speech/setup.sh", (ROOT / "speech/setup.sh").read_text())
        marker = self.write(setup.parent / ".venv/Scripts/python.exe", "existing Windows environment")
        self.run_script(setup, success=False)
        self.assertEqual(marker.read_text(), "existing Windows environment")
        self.assertFalse((setup.parent / ".venv/bin").exists())

    def test_host_bootstrap_installs_missing_tools_and_reuses_them(self):
        script = self.write(self.root / "setup-build.sh", (ROOT / "setup-build.sh").read_text())
        self.python_fixture()
        self.fake("id", "print('1000')\n")
        self.fake("sudo", "sys.exit(subprocess.run(sys.argv[1:]).returncode)\n")
        self.fake("dpkg-query", "print('installed' if pathlib.Path(os.environ['HOME'], 'apt-done').exists() else '')\n")
        self.fake("apt-get", "if 'openssh-client' in sys.argv: pathlib.Path(os.environ['HOME'], 'apt-done').touch()\n")
        self.fake("add-apt-repository", "pass\n")
        installer = '''mkdir -p "$HOME/.cargo/bin"
ln -s "$STUB_BIN/rustup" "$HOME/.cargo/bin/rustup"
printf ':\\n' > "$HOME/.cargo/env"
'''
        self.fake("curl", f"print({installer!r})\n")
        self.fake("rustup", "if 'install' in sys.argv: pathlib.Path(os.environ['HOME'], 'rust-ready').touch()\n")
        self.fake("rustc", "print('rustc 1.90.0' if pathlib.Path(os.environ['HOME'], 'rust-ready').exists() else 'rustc 1.88.0')\n")
        self.fake("cargo", "pass\n")
        self.fake("cargo-zigbuild", "pass\n")
        self.fake("uv", "pass\n")
        self.env["SYSTEM_PYTHON"] = "3.10"
        self.run_script(script)
        calls = self.calls()
        self.assertTrue(any(call[0] == "apt-get" and "openssh-client" in call and "python3-venv" in call for call in calls))
        self.assertIn(["rustup", "toolchain", "install", "stable", "--profile", "minimal"], calls)
        self.assertIn(["uv", "python", "install", "3.12", "--no-bin"], calls)
        self.assertEqual(calls[-1], ["cargo", "fetch", "--locked"])
        self.log.unlink()
        self.env["SYSTEM_PYTHON"] = "3.14"
        self.run_script(script)
        self.assertFalse(any(call[0] in ("apt-get", "curl") or call[1:3] == ["-m", "venv"] for call in self.calls()))
        self.assertIn(["uv", "python", "install", "3.12", "--no-bin"], self.calls())
        self.log.unlink()
        self.env["SYSTEM_PYTHON"] = "3.12"
        self.run_script(script)
        self.assertFalse(any(call[0] == "uv" for call in self.calls()))

    def provision_fixture(self):
        script = self.write(self.root / "scripts/provision-board.sh", (ROOT / "scripts/provision-board.sh").read_text())
        self.write(self.root / "setup-build.sh", '#!/bin/sh\necho bootstrap >> "$CALLS.steps"\n')
        self.write(self.home / ".cargo/env", ":\n")
        self.write(self.home / ".local/share/duck-build/bin/activate", 'VIRTUAL_ENV="$HOME/.local/share/duck-build"\n')
        self.write(self.home / ".duck-keys/local.dev.pub", "fixture")
        for path, step in (("speech/setup.sh", "prepare"), ("scripts/dev-push.sh", "build"),
                           ("xh_push.sh", "runtime"), ("speech/sync.sh", "speech")):
            self.write(self.root / path, f'#!/bin/sh\necho "{step} $* ${{XH_BIN_DIR:-}}" >> "$CALLS.steps"\n'
                       f'[ "${{FAIL_STEP:-}}" != {step} ]\n')
        for path in ("scripts/provision.sh", "deploy/fixture", "dist/fixture", "staged/updaterd"):
            self.write(self.root / path, "fixture")
        self.write(self.root / "tools/servo-web/package.py", "from pathlib import Path\n"
                   "p=Path(__file__).parent/'dist/servo-web-board.tar.gz'\np.parent.mkdir(exist_ok=True)\np.write_bytes(b'fixture')\n")
        self.fake("scp", "pass\n")
        self.fake("sleep", "import time; time.sleep(0.01)\n")
        self.fake("ssh-copy-id", "pathlib.Path(os.environ['HOME'], 'key-installed').touch()\n")
        self.fake("ssh", """
cmd = sys.argv[-1]
home = pathlib.Path(os.environ['HOME'])
if not (home / 'key-installed').exists():
    print('Permission denied', file=sys.stderr); sys.exit(255)
if cmd == 'test -L /opt/robot/daemon/current': sys.exit(int(os.environ.get('NEW_BOARD', '0')))
if 'sudo env ' in cmd:
    (home / 'rebooting').touch(); sys.exit(255)
if cmd == 'true' and (home / 'rebooting').exists():
    (home / 'rebooting').unlink(); sys.exit(255)
if cmd == 'test -f /var/lib/robot/provision.env': sys.exit(1)
if 'wc -c' in cmd: print('0')
""")
        return script

    def test_provision_existing_board_runs_all_stages_and_backs_up(self):
        script = self.provision_fixture()
        self.run_script(script, "--no-ble", "--pause-btd-on-pair", "--name", "Ducky", "radxa@board")
        steps = Path(str(self.log) + ".steps").read_text().splitlines()
        self.assertEqual([line.split()[0] for line in steps][-4:], ["prepare", "build", "runtime", "speech"])
        self.assertIn(f"runtime radxa@board {self.root}/staged", steps)
        self.assertIn("speech --install radxa@board ", steps)
        self.assertTrue(any("before-provision." in call[-1] for call in self.calls() if call[0] == "ssh"))
        self.assertTrue((self.home / ".ssh/id_ed25519.pub").is_file())

    def test_provision_new_board_and_failure_stop(self):
        script = self.provision_fixture()
        self.env["NEW_BOARD"] = "1"
        self.env["FAIL_STEP"] = "runtime"
        result = self.run_script(script, "--no-ble", "radxa@board", success=False)
        self.assertNotIn("provisioning finished", result.stdout)
        steps = Path(str(self.log) + ".steps").read_text()
        self.assertNotIn("speech --install", steps)
        self.assertFalse(any("before-provision." in call[-1] for call in self.calls() if call[0] == "ssh"))
        self.assertTrue(any(call[0] == "scp" and call[-1].endswith("/tmp/team.dev.pub") for call in self.calls()))


if __name__ == "__main__":
    unittest.main()
