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

    def boot_fixture(self, name):
        # Relocate all writable system paths; no root access or real inode changes.
        board_temp = tempfile.TemporaryDirectory(prefix="duck-board-")
        self.addCleanup(board_temp.cleanup)
        self.root = Path(board_temp.name)
        source = (ROOT / "scripts" / name).read_text()
        for prefix in ("/boot", "/etc", "/usr/local", "/usr/lib", "/proc/rknpu", "/proc/device-tree"):
            source = source.replace(prefix, str(self.root / prefix.lstrip("/")))
        if name == "setup-board.sh":
            source = source.rsplit('\nmain "$@"', 1)[0] + '''
check_network() { echo network >> "$CALLS.steps"; }
install_onnxruntime() { echo runtime >> "$CALLS.steps"; }
main "$@"
'''
        script = self.write(self.root / "scripts" / name, source)
        self.fake("id", "print('0')\n")
        self.fake("uname", "print('aarch64' if '-m' in sys.argv else 'fixture')\n")
        self.fake("lsattr", "sys.exit(5) if os.environ.get('LSATTR_FAIL') else print(os.environ['BOOT_ATTRS'] + ' ' + sys.argv[-1])\n")
        self.fake("chattr", "sys.exit('must never change boot protection')\n")
        for name in ("systemctl", "modprobe", "udevadm", "aplay", "i2cdetect", "dmesg"):
            self.fake(name, "pass\n")
        self.fake("apt-get", "sys.exit('unexpected apt install in fixture')\n")
        self.fake("dtc", "pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_bytes(b'compiled fixture')\n")
        self.env.update(BOOT_ATTRS="----i---------e-------", DUCK_BOARD_STAGING="1",
                        MOTOR_PORT="/dev/ttyS4", DUCK_LOCAL_DIR=str(self.root))
        self.env["CMDLINE"] = str(self.write(self.root / "cmdline", "console=ttyS4,1500000\n"))
        self.write(self.root / "usr/local/sbin/fixture", "")
        self.write(self.root / "usr/local/lib/fixture", "")
        boot = self.root / "boot"
        self.write(boot / "armbianEnv.txt", "fdtfile=rockchip/rk3566-radxa-zero3.dtb\noverlay_prefix=rk35xx\n"
                   "console=both\noverlays=i2c4-imu custom-overlay\nuser_overlays=custom-user\n")
        overlays = boot / "dtb-fixture/rockchip/overlay"
        for name in ("i2c3-m0", "i2c4-m0", "uart4-m1"):
            self.write(overlays / f"rk3568-{name}.dtbo", "fixture")
        self.write(overlays / "radxa-zero3-rpi-camera-v2.dtbo", "camera fixture")
        self.write(self.root / "deploy/overlays/microduck-i2s3-m0.dts", "audio fixture")
        return script, boot

    def boot_snapshot(self, boot):
        return {str(p.relative_to(boot)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in boot.rglob("*") if p.is_file()}

    def test_immutable_boot_skips_all_writes_but_configures_runtime(self):
        script, boot = self.boot_fixture("setup-board.sh")
        before = self.boot_snapshot(boot)
        result = self.run_script(script)
        self.assertIn("keeping existing device-tree", result.stdout)
        self.assertEqual(self.boot_snapshot(boot), before)
        self.assertFalse(any(call[0] in ("chattr", "dtc") for call in self.calls()))
        self.assertTrue((self.root / "etc/udev/rules.d/99-robot-i2c-imu.rules").is_file())
        self.assertTrue((self.root / "etc/systemd/system/tofd.service.d/10-i2c4.conf").is_file())
        self.assertIn(["systemctl", "mask", "serial-getty@ttyS4.service"], self.calls())
        self.assertEqual(Path(str(self.log) + ".steps").read_text(), "network\nruntime\n")

    def test_unlocked_boot_configures_device_tree_and_console(self):
        script, boot = self.boot_fixture("setup-board.sh")
        self.env["BOOT_ATTRS"] = "--------------e-------"
        self.run_script(script)
        config = (boot / "armbianEnv.txt").read_text()
        for value in ("overlay_prefix=rk3568", "console=display", "microduck-uart4-m1", "radxa-zero3-rpi-camera-v2", "custom-overlay", "custom-user"):
            self.assertIn(value, config)
        self.assertNotIn("i2c4-imu", config)
        self.assertTrue((boot / "overlay-user/microduck-i2s3-m0.dtbo").is_file())
        self.assertFalse(any(call[0] == "chattr" for call in self.calls()))

    def test_unreadable_boot_attributes_stop_before_changes(self):
        script, boot = self.boot_fixture("setup-board.sh")
        before = self.boot_snapshot(boot)
        self.env["LSATTR_FAIL"] = "1"
        result = self.run_script(script, success=False)
        self.assertIn("cannot read attributes", result.stderr)
        self.assertEqual(self.boot_snapshot(boot), before)
        self.assertFalse((self.root / "etc/udev").exists())

    def test_npu_install_respects_immutable_boot_and_keeps_runtime(self):
        script, boot = self.boot_fixture("setup-npu.sh")
        self.write(self.root / "usr/lib/librknnrt.so", "cached runtime")
        self.write(self.root / "usr/lib/librknnrt.version", "v2.3.2\n")
        self.write(self.root / "scripts/rk3568-npu-enable.dts", "npu fixture")
        (boot / "dtb/rockchip/overlay").mkdir(parents=True)
        before = self.boot_snapshot(boot)
        result = self.run_script(script)
        self.assertEqual(self.boot_snapshot(boot), before)
        self.assertIn("already installed", result.stdout)
        self.assertFalse(any(call[0] in ("dtc", "chattr") for call in self.calls()))
        self.env["BOOT_ATTRS"] = "--------------e-------"
        self.run_script(script)
        self.assertIn("npu-enable", (boot / "armbianEnv.txt").read_text())
        self.assertTrue((boot / "dtb/rockchip/overlay/rk3568-npu-enable.dtbo").is_file())

    def push_fixture(self):
        script = self.write(self.root / "xh_push.sh", (ROOT / "xh_push.sh").read_text())
        binaries = self.root / "target/native/aarch64-unknown-linux-gnu/release"
        for name in ("robotd", "mediad", "padd"):
            self.write(binaries / name, f"new {name}")
        self.env["XH_BIN_DIR"] = str(binaries)
        self.write(self.root / "deploy/robotd.toml", "new config")
        self.write(self.root / "policies/walk.onnx", "model")
        for name in ("keyboard-control.py", "duck-voice.py", "camera-detect-test.py", "standup-test.py"):
            self.write(self.root / "scripts" / name, f"new {name}")
        board_temp = tempfile.TemporaryDirectory(prefix="duck-push-")
        self.addCleanup(board_temp.cleanup)
        board_root = Path(board_temp.name)
        self.env["BOARD_ROOT"] = str(board_root)
        release = board_root / "opt/robot/daemon/releases/old"
        for name in ("robotd", "mediad", "padd"):
            self.write(release / "bin" / name, f"old {name}")
        self.write(release / "policies/walk.onnx", "old model")
        self.write(board_root / "etc/robot/robotd.toml", "old config")
        (release.parent.parent / "current").symlink_to("releases/old")
        states = self.write(board_root / "states.json", json.dumps({"robotd": True, "mediad": True, "padd": True}))
        self.env["SERVICE_STATES"] = str(states)
        self.fake("ssh", """
cmd = sys.argv[-1]
root = pathlib.Path(os.environ['BOARD_ROOT'])
if 'sha256sum robotd mediad padd' in cmd and os.environ.get('CORRUPT_BINARY'):
    (root / 'opt/robot/daemon/current/bin' / os.environ['CORRUPT_BINARY']).write_text('corrupt')
for prefix in ('/opt/robot', '/etc/robot'):
    cmd = cmd.replace(prefix, str(root / prefix.lstrip('/')))
env = dict(os.environ, HOME=os.environ['REMOTE'])
sys.exit(subprocess.run(['sh', '-c', cmd], cwd=env['HOME'], env=env).returncode)
""")
        self.fake("scp", """
target = pathlib.Path(os.environ['REMOTE']) / sys.argv[-1].rsplit(':', 1)[1]
for source in sys.argv[1:-1]: shutil.copy(source, target)
""")
        self.fake("sudo", "if sys.argv[1] != '-v': sys.exit(subprocess.run(sys.argv[1:]).returncode)\n")
        self.fake("systemctl", """
path = pathlib.Path(os.environ['SERVICE_STATES'])
states = json.loads(path.read_text())
action = sys.argv[1]
if action == 'is-active': sys.exit(0 if states.get(sys.argv[-1], False) else 3)
if action not in ('stop', 'start'): sys.exit('unexpected systemctl operation')
for service in sys.argv[2:]:
    if action == 'start' and service == os.environ.get('FAIL_SERVICE'): sys.exit(1)
    states[service] = action == 'start'
path.write_text(json.dumps(states))
""")
        return script, binaries, release, states

    def test_push_updates_three_binaries_and_restarts_in_order(self):
        script, binaries, release, states = self.push_fixture()
        self.run_script(script, "radxa@board")
        for name in ("robotd", "mediad", "padd"):
            self.assertEqual((release / "bin" / name).read_bytes(), (binaries / name).read_bytes())
            self.assertTrue(json.loads(states.read_text())[name])
        calls = self.calls()
        scp = next(call for call in calls if call[0] == "scp")
        for name in ("robotd", "mediad", "padd"):
            self.assertIn(str(binaries / name), scp)
        transitions = [call[1:] for call in calls if call[0] == "systemctl" and call[1] in ("start", "stop")]
        self.assertEqual(transitions, [["stop", "padd", "mediad", "robotd"], ["start", "robotd"], ["start", "mediad"], ["start", "padd"]])

    def test_push_keeps_stopped_clients_stopped(self):
        script, _, release, states = self.push_fixture()
        states.write_text(json.dumps({"robotd": True, "mediad": False, "padd": False}))
        self.run_script(script, "radxa@board")
        self.assertEqual(json.loads(states.read_text()), {"robotd": True, "mediad": False, "padd": False})
        for name in ("mediad", "padd"):
            self.assertEqual((release / "bin" / name).read_text(), f"new {name}")

    def test_push_missing_binary_stops_before_ssh(self):
        script, binaries, _, _ = self.push_fixture()
        (binaries / "padd").unlink()
        result = self.run_script(script, "radxa@board", success=False)
        self.assertIn("/padd", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_push_checks_new_binary_hashes(self):
        script, _, _, _ = self.push_fixture()
        for name in ("mediad", "padd"):
            with self.subTest(binary=name):
                self.env["CORRUPT_BINARY"] = name
                result = self.run_script(script, "radxa@board", success=False)
                self.assertIn("Binary verification failed", result.stderr)
                self.assertNotIn("==> verified:", result.stdout)

    def test_push_restart_failure_does_not_report_success(self):
        script, _, _, _ = self.push_fixture()
        self.env["FAIL_SERVICE"] = "mediad"
        result = self.run_script(script, "radxa@board", success=False)
        self.assertNotIn("==> verified:", result.stdout)
        self.assertNotIn(["systemctl", "start", "padd"], self.calls())

    def test_build_defaults_to_three_daemons_and_sets_sysroot(self):
        script = self.write(self.root / "build.sh", (ROOT / "build.sh").read_text())
        self.write(self.root / "scripts/cross-sysroot.sh", "#!/bin/sh\necho 'export PKG_CONFIG_SYSROOT_DIR=/fixture'\n")
        self.fake("cargo", "assert os.environ['PKG_CONFIG_SYSROOT_DIR'] == '/fixture'\n")
        self.run_script(script, "--offline")
        self.assertEqual(self.calls()[-1], ["cargo", "board", "--locked", "--bins", "-p", "robotd", "-p", "mediad", "-p", "padd", "--offline"])
        self.run_script(script, "-p", "robotd", "--offline")
        self.assertEqual(self.calls()[-1], ["cargo", "board", "--locked", "--bins", "-p", "robotd", "--offline"])
        self.log.unlink()
        self.write(self.root / "scripts/cross-sysroot.sh", "#!/bin/sh\necho 'export PKG_CONFIG_SYSROOT_DIR=/fixture'\nexit 1\n")
        self.run_script(script, success=False)
        self.assertEqual(self.calls(), [])

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
        for path in ("scripts/provision.sh", "deploy/fixture", "dist/fixture", "staged/updaterd",
                     "vision_follow/serve.py", "mediad/systemd/20-camera-upside-down.conf.example"):
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
