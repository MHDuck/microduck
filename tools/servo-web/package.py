"""python tools/servo-web/package.py：打包板端运行文件，保留板上校准、姿态和录音。"""
import hashlib
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parent
output = root / "dist"
output.mkdir(exist_ok=True)
archive = output / "servo-web-board.tar.gz"
files = ["server.py", "feetech.py", "imu_bridge.py", "imu_bus.py", "imu_attitude.js",
         "board_sensors.py", "board_media.py", "board_panel.js", "index.html",
         "requirements.txt", "requirements-board.txt", "deploy-board.sh", "README.md",
         "DEPLOY.md", "NOTICE-board.md"]
for directory in ("model", "defaults", "assets", "vendor", "licenses"):
    files.extend(str(path.relative_to(root)) for path in (root / directory).rglob("*") if path.is_file())
with tarfile.open(archive, "w:gz") as bundle:
    for name in sorted(files):
        bundle.add(root / name, arcname=Path(name).as_posix(), recursive=False)
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(output / "servo-web-board.tar.gz.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
print(f"{archive}\n{archive.stat().st_size:,} bytes\nSHA256 {digest}")
