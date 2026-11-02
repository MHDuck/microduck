#!/bin/sh
# Install the independent vision module and camera direction from this checkout.
set -eu

[ "$(id -u)" = 0 ] || { echo "run as root: sudo sh $0" >&2; exit 1; }
operator="${1:-${SUDO_USER:-root}}"
home_dir="$(getent passwd "$operator" | cut -d: -f6)"
[ -n "$home_dir" ] && [ -d "$home_dir" ] || { echo "no home directory for $operator" >&2; exit 1; }
operator_group="$(id -gn "$operator")"
repo="${DUCK_LOCAL_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
vision_dir="$home_dir/microduck-vision"
files="serve.py index.html core.js app.js aruco_client.js aruco_detector.py aruco_tools.py requirements.txt README.md aruco_5x5_50_id0_50mm.svg chessboard_9x6_20mm.svg"
for file in $files; do
    [ -f "$repo/vision_follow/$file" ] || { echo "missing vision file: $file" >&2; exit 1; }
done
camera_source="$repo/mediad/systemd/20-camera-upside-down.conf.example"
[ -f "$camera_source" ] || { echo "missing camera direction configuration" >&2; exit 1; }

apt-get update
apt-get install -y python3 python3-venv python3-pip ca-certificates curl
if systemctl cat vision-follow.service >/dev/null 2>&1; then
    systemctl stop vision-follow.service
fi
install -d -m 755 -o "$operator" -g "$operator_group" "$vision_dir"
for file in $files; do
    install -m 644 -o "$operator" -g "$operator_group" "$repo/vision_follow/$file" "$vision_dir/$file"
done
runuser -u "$operator" -- python3 -m venv "$vision_dir/.venv"
runuser -u "$operator" -- "$vision_dir/.venv/bin/python" -m pip install --only-binary=:all: -r "$vision_dir/requirements.txt"
runuser -u "$operator" -- "$vision_dir/.venv/bin/python" -m pip check
runuser -u "$operator" -- "$vision_dir/.venv/bin/python" -c 'import cv2; cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)); print("OpenCV ArUco ready:", cv2.__version__)'

camera_conf=/etc/systemd/system/mediad.service.d/20-camera-orientation.conf
if [ ! -f "$camera_conf" ]; then
    install -D -m 644 "$camera_source" "$camera_conf"
fi
cat > /etc/systemd/system/vision-follow.service <<EOF
[Unit]
Description=Microduck vision gaze and follow
After=network.target mediad.service
Wants=mediad.service

[Service]
Type=exec
User=$operator
Group=$operator_group
WorkingDirectory="$vision_dir"
ExecStart="$vision_dir/.venv/bin/python" "$vision_dir/serve.py" --detector aruco --marker-id 0 --marker-mm 50
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl restart mediad.service
systemctl enable --now vision-follow.service
curl --fail --silent --show-error --retry 10 --retry-delay 1 --retry-connrefused http://127.0.0.1:8091/config.json
printf '\nVision ready on port 8091; robot motion still requires manual control.\n'
