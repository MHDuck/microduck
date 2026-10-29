#!/bin/sh
# Independent speech-only upload. Never run xh_push or restart robotd here.
set -eu
cd "$(dirname "$0")"
board=${1:-radxa@192.168.2.70}
case "$board" in -*) echo 'Invalid SSH destination' >&2; exit 2 ;; esac
[ "$#" -le 1 ] || { echo 'Usage: sh speech/sync.sh [user@host]' >&2; exit 2; }
python3 -c 'import sys; sys.path.insert(0,"."); from prepare import ROOT,catalog,verify; i,s=catalog(); verify(ROOT/"models"/i,i,s)'
mkdir -p dist
release="speech-$(date +%Y%m%d-%H%M%S)-$$"
archive="$release.tar.gz"
tar -czf "dist/$archive" recognize.py keyboard_input.py prepare.py catalog.json keywords_raw.txt requirements.txt setup.sh README.md ACTIONS.md test_speech.py test_keyboard_input.py models
# Unique upload + extraction avoids touching an active preview installation.
ssh -o ConnectTimeout=5 "$board" 'mkdir -p microduck-speech/releases'
scp "dist/$archive" "$board:microduck-speech/$archive"
ssh "$board" "set -eu; cd microduck-speech; mkdir 'releases/$release'; tar -xzf '$archive' -C 'releases/$release'; printf '%s\n' 'Uploaded to ~/microduck-speech/releases/$release' 'Not started. Follow speech/README.md for board setup and preview.'"
