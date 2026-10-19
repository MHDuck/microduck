#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
app_dir=$(pwd -P)
port=/dev/ttyS4
install_only=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --install-only) install_only=1 ;;
        /dev/tty*) port=$1 ;;
        *) echo '用法：bash deploy-board.sh [/dev/ttyS4] [--install-only]' >&2; exit 2 ;;
    esac
    shift
done
board_user=$(id -un)

if [ "$(id -u)" -eq 0 ]; then
    echo '请用 radxa 普通用户执行 bash deploy-board.sh，脚本会在需要时调用 sudo。' >&2
    exit 1
fi
if [[ ! "$port" =~ ^/dev/tty[A-Za-z0-9_-]+$ ]] || { [ "$install_only" -eq 0 ] && [ ! -c "$port" ]; }; then
    echo "串口不存在或格式错误：$port；可指定 /dev/ttyS4 或 /dev/ttyUSB0。" >&2
    exit 1
fi

sudo apt-get update
sudo apt-get install -y python3-venv avahi-daemon psmisc i2c-tools alsa-utils sox v4l-utils \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good
python3 -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-board.txt

getent group i2c >/dev/null || sudo groupadd --system i2c
sudo tee /etc/udev/rules.d/70-servo-web-i2c.rules >/dev/null <<'EOF'
SUBSYSTEM=="i2c-dev", KERNEL=="i2c-[34]", GROUP="i2c", MODE="0660"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=i2c-dev
sudo udevadm settle

# 拍照只需要临时停 / 启 mediad，网页进程仍以普通用户运行。
sudoers_tmp=$(mktemp)
trap 'rm -f "$sudoers_tmp"' EXIT
printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl stop mediad.service, /usr/bin/systemctl start mediad.service\n' "$board_user" > "$sudoers_tmp"
sudo visudo -cf "$sudoers_tmp"
sudo install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/servo-web-mediad

if systemctl is-active --quiet servo-web.service; then
    sudo systemctl stop servo-web.service
fi
if [ "$app_dir" != "$HOME/servo-web" ] && [ -d "$HOME/servo-web" ]; then
    for item in directions.json poses calib; do
        if [ -e "$HOME/servo-web/$item" ] && [ ! -e "$item" ]; then
            cp -a "$HOME/servo-web/$item" "$item"
            echo "已迁移旧调试台数据：$item"
        fi
    done
fi
if [ -f defaults/directions.json ] && [ ! -e directions.json ]; then
    cp defaults/directions.json directions.json
fi
mkdir -p poses
for pose in defaults/poses/*.json; do
    [ -f "$pose" ] || continue
    if [ ! -e "poses/${pose##*/}" ]; then
        cp "$pose" "poses/${pose##*/}"
    fi
done

sudo tee /etc/systemd/system/servo-web.service >/dev/null <<EOF
[Unit]
Description=Microduck servo and board hardware web debugging
After=network.target robotd.service tofd.service padd.service mediad.service
Conflicts=robotd.service tofd.service padd.service mediad.service

[Service]
Type=simple
User=$board_user
SupplementaryGroups=dialout i2c audio video
WorkingDirectory=$app_dir
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/usr/bin/test -c $port
ExecStartPre=/bin/sh -c '! /usr/bin/fuser $port >/dev/null 2>&1'
ExecStart="$app_dir/.venv/bin/python" "$app_dir/server.py" --port $port --ids 10-14,20-24,30-34 --host 0.0.0.0 --http-port 8081 --require-serial --board-hardware
Restart=on-failure
RestartSec=3
TimeoutStopSec=90

[Install]
WantedBy=multi-user.target
EOF
sudo systemd-analyze verify /etc/systemd/system/servo-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now avahi-daemon
if [ "$install_only" -eq 1 ]; then
    echo 'servo-web.service 已安装，尚未启动；调试时先托稳机器人，再执行 sudo systemctl restart servo-web。'
    echo '网页端口：8081；启动调试台会停止 padd / mediad / robotd / tofd。'
    exit 0
fi
echo '即将停止 padd / mediad / robotd / tofd 并启动网页调试，请先托稳机器人。'
# 先停会拉起 robotd 的客户端，避免其自动重启反过来停止调试台。
for unit in padd.service mediad.service robotd.service tofd.service; do
    if [ "$(systemctl show "$unit" -p LoadState --value)" != 'not-found' ]; then
        sudo systemctl stop "$unit"
    fi
done
for device in /dev/i2c-3 /dev/i2c-4; do
    if [ ! -c "$device" ]; then
        echo "提示：$device 不存在；网页会显示传感器错误，请检查板端 overlay。"
    fi
done
sudo systemctl restart servo-web
sleep 10
sudo systemctl --no-pager --full status servo-web
printf '\n网页：http://%s.local:8081 ，也可用板端 IP:8081\n' "$(hostname)"
echo '如需 radxa.local：sudo hostnamectl set-hostname radxa && sudo systemctl restart avahi-daemon'
echo '查看日志：sudo journalctl -u servo-web -n 60 --no-pager'
echo '下次启动：sudo systemctl start servo-web；恢复原控制：sudo systemctl stop servo-web && sudo systemctl start robotd mediad padd'
