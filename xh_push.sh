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
                'Default: upload robotd, mediad, padd, policies/*.onnx, config and control/test scripts.' \
                '--build: incrementally build all three daemons with Cargo offline before uploading.' \
                'Restarts robotd and previously running mediad/padd; stopped clients stay stopped.' \
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

for daemon in robotd mediad padd; do
    [ -f "$bin_dir/$daemon" ] || {
        printf 'Missing %s/%s; run sh xh_push.sh --build first.\n' "$bin_dir" "$daemon" >&2
        exit 1
    }
done
[ -f deploy/robotd.toml ] || { echo 'Missing deploy/robotd.toml' >&2; exit 1; }
for script in keyboard-control.py duck-voice.py camera-detect-test.py standup-test.py; do
    [ -f "scripts/$script" ] || { echo "Missing scripts/$script" >&2; exit 1; }
done
set -- policies/*.onnx
[ -f "$1" ] || { echo 'No policies/*.onnx files' >&2; exit 1; }

config_sha=$(sha256sum deploy/robotd.toml)
config_sha=${config_sha%% *}
binary_hashes=$(cd "$bin_dir" && sha256sum robotd mediad padd)
keyboard_sha=$(sha256sum scripts/keyboard-control.py)
keyboard_sha=${keyboard_sha%% *}
printf '==> source config: %s/deploy/robotd.toml\n' "$PWD"
echo "==> uploading to $board"
case "${board#*@}" in
    *:*) destination="${board%${board#*@}}[${board#*@}]" ;;
    *) destination=$board ;;
esac
incoming=".cache/xh-push/upload-$(date +%Y%m%d-%H%M%S)-$$"
ssh "$board" "mkdir -p .cache/xh-push && mkdir '$incoming'"
scp "$bin_dir/robotd" "$bin_dir/mediad" "$bin_dir/padd" deploy/robotd.toml \
    scripts/keyboard-control.py scripts/duck-voice.py scripts/camera-detect-test.py \
    scripts/standup-test.py \
    "$@" "$destination:$incoming/"

echo '==> replacing robotd, mediad, padd, ONNX and config'
ssh -tt -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=6 "$board" "incoming='$incoming'
"'
set -eu
incoming="$HOME/$incoming"
release=$(readlink -e /opt/robot/daemon/current)

echo "==> sudo authentication required to install and restart services"
sudo -v
for daemon in robotd mediad padd; do
    sudo install -m 755 "$incoming/$daemon" "$release/bin/$daemon.xh-new"
done
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

# Stop intent clients before replacing robotd; preserve deliberate keyboard-only operation.
restart_clients=""
for daemon in mediad padd; do
    if systemctl is-active --quiet "$daemon"; then
        restart_clients="$restart_clients $daemon"
    else
        echo "==> $daemon was not running; its binary will be updated and it will stay stopped"
    fi
done
sudo systemctl stop padd mediad robotd
for daemon in robotd mediad padd; do
    sudo mv -f "$release/bin/$daemon.xh-new" "$release/bin/$daemon"
done
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
for daemon in $restart_clients; do
    sudo systemctl start "$daemon"
    systemctl is-active --quiet "$daemon"
done
'
remote_config_sha=$(ssh "$board" 'sha256sum /etc/robot/robotd.toml')
[ "${remote_config_sha%% *}" = "$config_sha" ] || {
    echo 'Config verification failed: /etc/robot/robotd.toml differs from local deploy/robotd.toml' >&2
    exit 1
}
remote_binary_hashes=$(ssh "$board" 'cd /opt/robot/daemon/current/bin && sha256sum robotd mediad padd')
[ "$remote_binary_hashes" = "$binary_hashes" ] || {
    echo 'Binary verification failed: installed robotd/mediad/padd differ from the local build' >&2
    printf 'Expected:\n%s\nInstalled:\n%s\n' "$binary_hashes" "$remote_binary_hashes" >&2
    exit 1
}
remote_keyboard_sha=$(ssh "$board" 'sha256sum /opt/robot/daemon/current/scripts/keyboard-control.py')
[ "${remote_keyboard_sha%% *}" = "$keyboard_sha" ] || {
    echo 'Keyboard script verification failed: installed keyboard-control.py differs from the local file' >&2
    exit 1
}
ssh "$board" "rm -f -- '$incoming/'* && rmdir -- '$incoming'"
echo '==> verified: board config, robotd, mediad, padd and keyboard script SHA-256 match local files'
echo "==> uploaded robotd/mediad/padd on $board; restarted robotd and previously running clients"
echo '==> Exit the old keyboard control process, then run: sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py'
