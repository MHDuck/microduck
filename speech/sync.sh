#!/bin/sh
# Independent speech-only upload. Never run xh_push or restart robotd here.
set -eu
cd "$(dirname "$0")"
install=no
case "${1:-}" in
    --install) install=yes; shift ;;
    -h|--help) echo 'Usage: sh speech/sync.sh [--install] [user@host]'; exit 0 ;;
esac
board=${1:-radxa@192.168.2.70}
case "$board" in -*) echo 'Invalid SSH destination' >&2; exit 2 ;; esac
[ "$#" -le 1 ] || { echo 'Usage: sh speech/sync.sh [--install] [user@host]' >&2; exit 2; }
python3 -c 'import sys; sys.path.insert(0,"."); from prepare import ROOT,catalog,verify; i,s=catalog(); d=ROOT/"models"/i; verify(d,i,s); assert (d/"keywords_raw.txt").read_bytes() == (ROOT/"keywords_raw.txt").read_bytes(), "Keywords changed; run speech/prepare.py first"'
mkdir -p dist
release="speech-$(date +%Y%m%d-%H%M%S)-$$"
archive="$release.tar.gz"
set -- recognize.py keyboard_input.py prepare.py catalog.json keywords_raw.txt requirements.txt setup.sh README.md ACTIONS.md test_speech.py test_keyboard_input.py models
# Reuse any complete ARM wheels cached by the operator, never a PC virtualenv or .part.
for wheel in dist/wheels-cp313-aarch64/*.whl; do
    [ ! -f "$wheel" ] || set -- "$@" "$wheel"
done
tar -czf "dist/$archive" "$@"
# Unique upload + extraction avoids touching an active preview installation.
ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=6 "$board" 'mkdir -p microduck-speech/releases'
case "${board#*@}" in
    *:*) destination="${board%${board#*@}}[${board#*@}]" ;;
    *) destination=$board ;;
esac
scp -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=6 "dist/$archive" "$destination:microduck-speech/$archive"
ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=6 "$board" "set -eu; cd microduck-speech; mkdir 'releases/$release'; tar -xzf '$archive' -C 'releases/$release'; printf '%s\n' 'Uploaded to ~/microduck-speech/releases/$release'"
if [ "$install" = yes ]; then
    ssh -t -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=6 "$board" "release='$release'
"'
set -eu
sudo apt-get -o Acquire::Retries=3 update
sudo apt-get -o Acquire::Retries=3 install -y python3-venv alsa-utils libportaudio2
cd "$HOME/microduck-speech/releases/$release"
if [ -d dist/wheels-cp313-aarch64 ]; then
    export PIP_FIND_LINKS="$PWD/dist/wheels-cp313-aarch64"
fi
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
sh setup.sh
# Keep the old current until hashes and a no-microphone KWS load have passed.
ln -sfnT "releases/$release" "$HOME/microduck-speech/current"
printf "%s\n" "Speech installed: $HOME/microduck-speech/current" "Recognition is not running; keyboard default remains --speech off."
'
else
    echo 'Not started. Follow speech/README.md for board setup and preview.'
fi
