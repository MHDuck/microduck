#!/bin/sh
set -eu
cd "$(dirname "$0")"

board=radxa@192.168.2.248
build=no
bin_dir=${XH_BIN_DIR:-target/native/aarch64-unknown-linux-gnu/release}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --build) build=yes ;;
        -h|--help)
            printf '%s\n' \
                'Usage: sh xh_push.sh [--build] [user@host]' \
                'Default: upload existing robotd, policies/*.onnx, deploy/robotd.toml.walk and keyboard control.' \
                '--build: incrementally build robotd offline before uploading.' \
                'XH_BIN_DIR overrides target/native/aarch64-unknown-linux-gnu/release.'
            exit 0
            ;;
        -*) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
        *) board=$1 ;;
    esac
    shift
done

if [ "$build" = yes ]; then
    bash build.sh --offline
    bin_dir=target/native/aarch64-unknown-linux-gnu/release
fi

[ -f "$bin_dir/robotd" ] || {
    printf 'Missing %s/robotd; run sh xh_push.sh --build first.\n' "$bin_dir" >&2
    exit 1
}
[ -f deploy/robotd.toml.walk ] || { echo 'Missing deploy/robotd.toml.walk' >&2; exit 1; }
[ -f scripts/keyboard-control.py ] || { echo 'Missing scripts/keyboard-control.py' >&2; exit 1; }
set -- policies/*.onnx
[ -f "$1" ] || { echo 'No policies/*.onnx files' >&2; exit 1; }

echo "==> uploading to $board"
ssh "$board" 'mkdir -p "$HOME/.cache/xh-push"'
scp "$bin_dir/robotd" deploy/robotd.toml.walk scripts/keyboard-control.py "$@" "$board:.cache/xh-push/"

echo '==> replacing robotd, ONNX and config'
ssh -t "$board" '
set -eu
incoming="$HOME/.cache/xh-push"
release=$(readlink -e /opt/robot/daemon/current)

sudo install -m 755 "$incoming/robotd" "$release/bin/robotd.xh-new"
for model in "$incoming"/*.onnx; do
    name=${model##*/}
    sudo install -m 644 "$model" "$release/policies/$name.xh-new"
done
sudo install -m 644 "$incoming/robotd.toml.walk" /etc/robot/robotd.toml.xh-new
sudo install -D -m 644 "$incoming/keyboard-control.py" "$release/scripts/keyboard-control.py.xh-new"
if [ -f /etc/robot/robotd.toml ]; then
    sudo cp -p /etc/robot/robotd.toml /etc/robot/robotd.toml.xh-backup
fi

sudo systemctl stop robotd
sudo mv -f "$release/bin/robotd.xh-new" "$release/bin/robotd"
for model in "$incoming"/*.onnx; do
    name=${model##*/}
    sudo mv -f "$release/policies/$name.xh-new" "$release/policies/$name"
done
sudo mv -f /etc/robot/robotd.toml.xh-new /etc/robot/robotd.toml
sudo mv -f "$release/scripts/keyboard-control.py.xh-new" "$release/scripts/keyboard-control.py"
sudo systemctl start robotd
'
echo "==> uploaded and restarted robotd on $board"
