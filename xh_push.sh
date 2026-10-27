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
                'Default: upload robotd, policies/*.onnx, config, keyboard/voice/camera/standup test scripts.' \
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
[ -f deploy/robotd.toml ] || { echo 'Missing deploy/robotd.toml' >&2; exit 1; }
for script in keyboard-control.py duck-voice.py camera-detect-test.py standup-test.py; do
    [ -f "scripts/$script" ] || { echo "Missing scripts/$script" >&2; exit 1; }
done
set -- policies/*.onnx
[ -f "$1" ] || { echo 'No policies/*.onnx files' >&2; exit 1; }

config_sha=$(sha256sum deploy/robotd.toml)
config_sha=${config_sha%% *}
robotd_sha=$(sha256sum "$bin_dir/robotd")
robotd_sha=${robotd_sha%% *}
keyboard_sha=$(sha256sum scripts/keyboard-control.py)
keyboard_sha=${keyboard_sha%% *}
printf '==> source config: %s/deploy/robotd.toml\n' "$PWD"
echo "==> uploading to $board"
incoming=".cache/xh-push/upload-$(date +%Y%m%d-%H%M%S)-$$"
ssh "$board" "mkdir -p .cache/xh-push && mkdir '$incoming'"
scp "$bin_dir/robotd" deploy/robotd.toml \
    scripts/keyboard-control.py scripts/duck-voice.py scripts/camera-detect-test.py \
    scripts/standup-test.py \
    "$@" "$board:$incoming/"

echo '==> replacing robotd, ONNX and config'
ssh -tt "$board" "incoming='$incoming'
"'
set -eu
incoming="$HOME/$incoming"
release=$(readlink -e /opt/robot/daemon/current)

echo "==> sudo authentication required to install and restart robotd"
sudo -v
sudo install -m 755 "$incoming/robotd" "$release/bin/robotd.xh-new"
for model in "$incoming"/*.onnx; do
    name=${model##*/}
    sudo install -m 644 "$model" "$release/policies/$name.xh-new"
done
sudo install -m 644 "$incoming/robotd.toml" /etc/robot/robotd.toml.xh-new
sudo install -D -m 644 "$incoming/keyboard-control.py" "$release/scripts/keyboard-control.py.xh-new"
sudo install -D -m 755 "$incoming/duck-voice.py" "$release/scripts/duck-voice.py.xh-new"
sudo install -D -m 755 "$incoming/camera-detect-test.py" "$release/scripts/camera-detect-test.py.xh-new"
sudo install -D -m 755 "$incoming/standup-test.py" "$release/scripts/standup-test.py.xh-new"
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
sudo mv -f "$release/scripts/duck-voice.py.xh-new" "$release/scripts/duck-voice.py"
sudo mv -f "$release/scripts/camera-detect-test.py.xh-new" "$release/scripts/camera-detect-test.py"
sudo mv -f "$release/scripts/standup-test.py.xh-new" "$release/scripts/standup-test.py"
if systemctl is-active --quiet servo-web.service; then
    echo "==> stopping servo-web so robotd can own the servo bus"
    sudo systemctl stop servo-web.service
fi
sudo systemctl start robotd
systemctl is-active --quiet robotd
'
remote_config_sha=$(ssh "$board" 'sha256sum /etc/robot/robotd.toml')
[ "${remote_config_sha%% *}" = "$config_sha" ] || {
    echo 'Config verification failed: /etc/robot/robotd.toml differs from local deploy/robotd.toml' >&2
    exit 1
}
remote_robotd_sha=$(ssh "$board" 'sha256sum /opt/robot/daemon/current/bin/robotd')
[ "${remote_robotd_sha%% *}" = "$robotd_sha" ] || {
    echo 'Binary verification failed: installed robotd differs from the local build' >&2
    exit 1
}
remote_keyboard_sha=$(ssh "$board" 'sha256sum /opt/robot/daemon/current/scripts/keyboard-control.py')
[ "${remote_keyboard_sha%% *}" = "$keyboard_sha" ] || {
    echo 'Keyboard script verification failed: installed keyboard-control.py differs from the local file' >&2
    exit 1
}
ssh "$board" "rm -f -- '$incoming/'* && rmdir -- '$incoming'"
echo '==> verified: board config, robotd and keyboard script SHA-256 match local files'
echo "==> uploaded and restarted robotd on $board"
echo '==> Exit the old keyboard control process, then run: sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py'
