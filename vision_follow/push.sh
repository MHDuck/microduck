#!/bin/sh
# Only the independent module; never replaces robotd/config/policies or restarts a service.
set -eu
cd "$(dirname "$0")"
board=${1:-radxa@192.168.2.70}
if [ "$board" = --help ] || [ "$board" = -h ]; then
    echo 'Usage: sh vision_follow/push.sh [user@host]'
    echo 'Upload independent duck/ArUco module to ~/microduck-vision; no build, sudo or service changes.'
    exit 0
fi
case "$board" in -*|*[!a-zA-Z0-9@._:-]*) echo 'Invalid user@host' >&2; exit 2 ;; esac
ssh "$board" 'mkdir -p microduck-vision'
scp serve.py index.html core.js app.js aruco_client.js aruco_detector.py aruco_tools.py \
    requirements.txt README.md aruco_5x5_50_id0_50mm.svg chessboard_9x6_20mm.svg "$board:microduck-vision/"
printf '\nUploaded. Duck mode: python3 ~/microduck-vision/serve.py\n'
printf 'ArUco first-time setup: cd ~/microduck-vision && python3 -m venv .venv && .venv/bin/python -m pip install --only-binary=:all: -r requirements.txt\n'
printf 'ArUco start: ~/microduck-vision/.venv/bin/python ~/microduck-vision/serve.py --detector aruco --marker-id 0 --marker-mm 50\n'
printf 'Open http://<board-ip>:8091/\n'
