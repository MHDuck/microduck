# 视觉功能：全新 WSL 与板端部署

适用分支：**dzh_voice**。硬件为 Radxa ZERO 3W / RK3566、HD1910 舵机、BNO085，
CSI 摄像头按 IMX219 / Raspberry Pi Camera V2 接线。系统按本项目适配的 Armbian ARM64 镜像部署，
构建目标库来自 Debian 13 Trixie；不直接套用任意系统或 USB 摄像头。

## 1. 组件和端口

| 组件 | 板端位置 / 端口 | 用途 |
| --- | --- | --- |
| robotd | /opt/robot/daemon/current/bin/robotd | 策略、电机、IMU、原有安全控制 |
| mediad | 同目录 bin/mediad；8080 控制台、8443 WebRTC 信令 | 唯一摄像头采集入口、视频和控制转发 |
| 独立视觉模块 | ~/microduck-vision；HTTP 8091 | 网页、OpenCV ArUco JPEG 检测 |
| servo-web | HTTP 8081 | 校准，启动后停止控制服务并占用串口 |
| 配置 / 策略 | /etc/robot/robotd.toml；/opt/robot/daemon/current/policies | 本机参数及 61D 输入 / 14D 输出 ONNX |

浏览器复用 mediad 视频，上传选中的原始视频 JPEG 给独立检测服务，通过现有控制接口发运动。
Python 不另开摄像头、不直接写舵机。语音不是视觉依赖，独立安装维护。
默认仅预览，人工起身、启动控制后才动作；**控制浏览器保持前台**。
视觉与键盘、语音、手柄、8080 控制台择一发送控制，不能同时运行多个控制者。

## 2. Windows 安装 WSL

管理员 PowerShell 执行；已有 Ubuntu 24.04 时直接打开，不注销或覆盖已有环境：

```powershell
wsl --install -d Ubuntu-24.04
```

按提示重启、创建 Linux 用户。此后的电脑命令全部在 **WSL Ubuntu 的 Bash** 执行。

## 3. WSL 准备源码和构建环境

### 3.1 软件包

```bash
sudo apt update
sudo apt install -y software-properties-common
sudo add-apt-repository -y universe
sudo apt update
sudo apt install -y \
  git curl ca-certificates openssh-client \
  build-essential pkg-config cmake binutils \
  gcc-aarch64-linux-gnu g++-aarch64-linux-gnu libc6-dev-arm64-cross \
  python3 python3-venv python3-pip libudev-dev libdbus-1-dev libssl-dev \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libgstreamer-plugins-bad1.0-dev \
  nodejs npm bash coreutils findutils grep sed gawk tar gzip xz-utils zstd unzip file
mkdir -p "$HOME/src"
```

### 3.2 导入本次未推送的源码

本批修改只在本地提交，**直接克隆远程暂时取不到这些提交**。
同一 Windows 电脑的新 WSL 从现有仓库克隆为独立 Linux 工作副本，不复制 Windows venv/target：

```bash
SOURCE_REPO=/mnt/f/Code/microduck_code/microduck_rust
git -C "$SOURCE_REPO" branch --show-current
git -C "$SOURCE_REPO" log -6 --oneline
git clone --no-hardlinks --branch dzh_voice "$SOURCE_REPO" "$HOME/src/microduck_rust"
cd "$HOME/src/microduck_rust"
git remote set-url origin https://gitee.com/moheng_robot/microduck_rust.git
git branch --show-current
git log -6 --oneline
git status --short
```

源路径不同则修改 SOURCE_REPO；目标目录必须尚不存在。确认分支为 dzh_voice、提交与源仓库相同。
另一台电脑可在源仓库执行 `git bundle create dzh_voice-vision.bundle dzh_voice`，复制 bundle，
以 `git clone --branch dzh_voice /实际路径/dzh_voice-vision.bundle "$HOME/src/microduck_rust"`
代替 clone，再设置同一 origin。bundle 不推送，保留已有 Git 历史。

只有本批提交以后已发布到远程，才可改用
`git clone --branch dzh_voice https://gitee.com/moheng_robot/microduck_rust.git "$HOME/src/microduck_rust"`。
部署不需要修改 master，也不要整目录覆盖旧代码。

### 3.3 Rust / Zig / ARM64 目标库

```bash
cd "$HOME/src/microduck_rust"
bash setup-build.sh
source "$HOME/.cargo/env"
rustup target add aarch64-unknown-linux-gnu
cargo fetch --locked
DUCK_DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian \
  sh scripts/cross-sysroot.sh
export PKG_CONFIG_SYSROOT_DIR="$PWD/deploy/sysroot"
export PKG_CONFIG_LIBDIR="$PKG_CONFIG_SYSROOT_DIR/usr/lib/aarch64-linux-gnu/pkgconfig:$PKG_CONFIG_SYSROOT_DIR/usr/share/pkgconfig"
export PKG_CONFIG_ALLOW_CROSS=1
export RUSTFLAGS="-L $PKG_CONFIG_SYSROOT_DIR/usr/lib/aarch64-linux-gnu"
```

setup-build.sh 准备 Rust、独立 duck-build 环境内的 cargo-zigbuild 和 ziglang 0.14.1。
sysroot 只下载并解包目标库，不安装到 WSL 系统；WSL 同名库不能代替 ARM64 库。
换终端时重新 source cargo 并执行四条 export；构建路径不要含空格。首次构建需要网络，不先用 --offline。

## 4. 新板子：系统、SSH、模型

将本项目验证的 **Armbian 26.8.1 精简版 ARM64 镜像**写入 TF 卡，首次启动设置 radxa 用户、密码、
Wi-Fi 和 sudo。硬件/镜像要求见[仓库 README](../README.md#2-硬件与接口)，不额外套用其他板版的 DTB/B1 引导替换。
检查供电、摄像头排线、IMU 接线、舵机 ID。刷板子不重置舵机 EEPROM；
新机器人或重新装配仍需校准。安装、校准期间托住机器人，暂不启动运动。

WSL 设置实际板端地址，后面的 WSL 命令沿用 BOARD：

```bash
export BOARD=radxa@192.168.2.70
ssh "$BOARD"
```

核对主机指纹、完成登录后 exit 回 WSL，再配置 SSH 公钥，供初始化脚本重启后自动连接：

```bash
test -f "$HOME/.ssh/id_ed25519.pub" || ssh-keygen -t ed25519
ssh-copy-id "$BOARD"
ssh -o BatchMode=yes "$BOARD" 'id; uname -m; cat /etc/os-release'
```

应为 aarch64，用户能 sudo。重刷后如主机指纹冲突，核对确实是该板子，再 `ssh-keygen -R 真实IP`。
初始化可能重启或改变 DHCP 地址；变化后更新 BOARD。

WSL 检查配置引用的模型是否存在：

```bash
cd "$HOME/src/microduck_rust"
python3 - <<'PY'
from pathlib import Path
import tomllib
cfg = tomllib.loads(Path("deploy/robotd.toml").read_text())
missing = []
for key in ("walk", "stand", "sitstand", "ground_pick", "kick_left", "kick_right", "roulade"):
    value = cfg["policy"].get(key, "none")
    if value == "none":
        continue
    local = Path("policies") / Path(value).name
    print(key, local, "OK" if local.is_file() else "MISSING")
    if not local.is_file():
        missing.append(str(local))
if missing:
    raise SystemExit("先补齐模型：" + ", ".join(missing))
PY
```

核对 deploy/robotd.toml 的 open_microduck 平台、HD1910 串口、BNO085 总线/地址/安装方向、
[media].camera=true 及策略。模型名称不能互相自动替代；嘴部不进入 14D 策略输出。

## 5. WSL：初始化全套系统服务

保持第 3 节构建环境，在仓库根目录执行：

```bash
bash scripts/provision-board.sh --no-ble --pause-btd-on-pair --name Ducky "$BOARD"
```

从当前代码构建/签名完整运行时，配置板端系统依赖、摄像头 overlay、GStreamer、RKAIQ、
ONNX Runtime、系统服务、servo-web，等待重启并输出健康状态。
--no-ble 只跳过电脑通过蓝牙重新寻找板子的步骤，适用于无蓝牙的 WSL。
签名私钥留在仓库外，不放进源码。

**全新板子必须先初始化，不能只 xh_push。**
若板端已有 current 发布目录，此入口只刷新环境，不替换整套运行时；
整体更新使用 `sh scripts/dev-push.sh "$BOARD"`，再执行下一节配置/模型同步。
robotd、mediad、robotctl 应来自同一代码版本。

## 6. WSL：编译并同步配置、模型、控制脚本

```bash
cd "$HOME/src/microduck_rust"
bash build.sh -p robotd -p robotctl -p mediad
sh xh_push.sh "$BOARD"
```

产物在 target/native/aarch64-unknown-linux-gnu/release。
xh_push 上传 robotd、policies/*.onnx、robotd.toml 和键盘/诊断脚本，校验并重启 robotd。
**它不更新 mediad / robotctl**；本次新板子已由第 5 节的完整运行时安装它们。
以后只改 mediad，按[方向修正文档](相机方向修正_180度.md)单独替换，不能只 xh_push。

xh_push 会覆盖 /etc/robot/robotd.toml；板端调试参数先备份合回本地配置。
只改独立视觉 Python/网页则用第 9 节 push，不重新编译 Rust。

## 7. 板端：校准与主程序验收

以下在 SSH 板端终端执行：

```bash
sudo systemctl restart servo-web
```

打开 http://板子IP:8081/，按[校准说明](../校准.md)核对舵机 ID、方向、基准姿态与零点。
servo-web 会停 padd、mediad、robotd、tofd；不能同时跑键盘或视觉运动。

完成后退出校准页，托住机器人，交回设备：

```bash
sudo systemctl stop servo-web
sudo systemctl restart robotd mediad tofd
sudo systemctl stop padd
sudo /opt/robot/daemon/current/bin/robotctl health
sudo systemctl status robotd mediad --no-pager -l
sudo ss -lntp '( sport = :8080 or sport = :8443 )'
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py --speech off
```

健康应 bus ok、IMU ready、循环约 50Hz；累计 stale 可含单次重复，不能忽略持续过期/总线错误。
configure 报未知 mounting 等字段时核对二进制版本，不靠删除硬件配置掩盖混用。
人工 Enter 起身，确认站立、停车、前后走与头部控制正常。视觉依赖模型能响应头部指令。

退出键盘前托住（Esc 沿原流程可能卸力/停止 robotd），再
`sudo systemctl restart robotd mediad`。先在 http://板子IP:8080/ 只连视频观察至少 30 秒，
确认流畅后关闭该视频连接。演示期间不再同时运行键盘、语音、手柄控制。

## 8. 板端：倒装相机追加 180°

当前图像相对原显示上下颠倒，在已安装新版 mediad 的板端配置：

```bash
sudo mkdir -p /etc/systemd/system/mediad.service.d
sudo tee /etc/systemd/system/mediad.service.d/20-camera-orientation.conf >/dev/null <<'EOF'
[Service]
Environment=DUCK_CAMERA_ROTATE_OFFSET=180
Environment=DUCK_CAMERA_ROTATE_PIXELS=false
EOF
sudo systemctl daemon-reload
sudo systemctl restart mediad
sudo journalctl -u mediad -n 30 --no-pager
```

默认 --rotate=90，追加 180 得到 270；以实际启动日志为准。
false 保留零拷贝，统一通过 media.video.rotate 修正网页与检测坐标，**原始编码像素不旋转**；
8080、8091 显示都应转正。其他视频消费者也须应用该元数据。
若要求编码像素本身正向，可设 true，但会增加负载，须验证帧率/温度并重标定相机。
详见[方向模式、构建与回退](相机方向修正_180度.md)。网页不要额外叠加旋转。

## 9. 独立视觉模块：上传、安装、启动

WSL：

```bash
cd "$HOME/src/microduck_rust"
sh vision_follow/push.sh "$BOARD"
```

只同步到 ~/microduck-vision，不覆盖主配置/策略，不重启服务，保留板端 venv 和内参。
板端，radxa 用户执行，不用 sudo 安装 Python 包：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ca-certificates
cd "$HOME/microduck-vision"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
.venv/bin/python -c 'import cv2; print(cv2.__version__); print(hasattr(cv2, "aruco"))'
.venv/bin/python serve.py --detector aruco --marker-id 0 --marker-mm 50
```

预期 4.12.0、True。不要复用 speech/.venv 或训练环境；缺 wheel 可增加
--index-url https://pypi.org/simple，不替换系统 Python。
保持终端运行，Ctrl-C 停止。浏览器打开 **http://板子IP:8091/**，点击“连接摄像头”。
HTTP 8091、8080 和信令 8443 可达，同时需 WebRTC 视频/数据通道网络可达。

| 参数 | 默认 | 用途 |
| --- | --- | --- |
| --aruco-hz | 0.5 | 检测频率上限，允许 0.1～5，实际还取决于耗时 |
| --aruco-max-side / --aruco-jpeg-quality | 640 / 0.7 | JPEG 最长边与质量 |
| --detection-max-age / --aruco-http-timeout | 5 / 8 秒 | 新解码帧到结果预算 / HTTP 等待 |
| --target-timeout / --video-timeout | 12 / 10 秒 | 目标保留 / 无新画面期限 |
| --follow-style | step | 停步识别再移动 |
| --follow-step-seconds / --follow-settle-seconds | 2.5 / 1 秒 | 每段行走含起步 / 停步稳定等待 |
| --follow-require-front | 默认关闭 | 可选要求目标和头部朝前才走 |

CLI 检查目标保留时间能覆盖两帧确认及移动窗口；完整参数见 serve.py --help。
长检测预算不延长独立 IMU 健康确认和控制连接的期限。

## 10. 打印及分阶段实机测试

使用 aruco_5x5_50_id0_50mm.svg：**DICT_5X5_50、ID0、黑色正方形外边 50mm**。
按 100% 打印并实测；整张 SVG 含白边为 70mm，不能把 70 填入 marker-mm。
贴硬平面，避免反光，只放一个 ID0。

| 阶段 | 操作 | 应看到的效果 |
| --- | --- | --- |
| 纯视频 | 连接后不识别、不使能，观察 30 秒 | 正向、持续更新、无运动 |
| 识别预览 | 勾“仅预览时启用 ArUco 识别”，左右移动码 | ID0 框贴合、日志有耗时、无使能动作 |
| 人工起身 | 检查状态，点“起身使能” | 先入队、再由策略确认开始，最后站稳 |
| 原地注视 | 选“原地转头注视”，点“启动注视” | 两帧确认后头部左右跟踪，平移为 0；可选上下看 |
| 设置相对距离 | 停止，期望位置两帧识别稳定后点“将当前距离设为跟随距离” | 保存当前框大小作距离基准 |
| 停走跟随 | 选“转头＋前后跟随”、停走式、图像相对大小，启动后移动码 | 变小前进、变大后退、接近基准停；每段后停稳再识别 |
| 停止 | “停止控制”或空格 / Esc | 清零速度，不卸力、不自动回正头 |
| 故障 | 移出码、断连、切后台 | 按对应期限停步，恢复后需人工再启动 |

预览勾选只是非控制时识别偏好，**不勾选也能启动注视/跟随**，控制中取消勾选不会停止运动。
看“识别状态”确认实际状态；停止按钮结束控制。页面后台、状态过期、真实视频冻结、总线/IMU 故障
仍停止，不用慢检测参数绕过。

起身 accepted 只代表入队；3 秒未看到开始应查 robotd 拒绝日志，不连点或自动改成普通 enable。
倒地起身仍走原 StandUp 恢复和过流保护。注视/跟随必须恢复完成、stand/walk 策略、mode=walk。
头部指令最终由策略执行，目标变化但关节不动时检查模型跟踪能力，不直接加大输出。

停走每轮：零速 accepted 应答 → 稳定 1 秒 → 新画面两帧 → 移动最多 2.5 秒 → 再停。
前进上限 0.12m/s、后退 0.10m/s，加速度 0.3m/s²，前进约 0.4 秒到上限。
默认不因目标/头部偏侧阻止停走式平移，可勾选恢复此限制。
移动中不上传、旧图不触发下一段。不自动转身、横移或避障，先空旷地面托护测试。
时间包含起步且只是上限，距离死区/结果失效/保护可提前停步；指令不代表实测位移。

## 11. 米制距离和相机标定（可选）

注视、相对大小不需要内参。米制模式需要**这台相机、相同原始分辨率/裁切/像素方向**的 pinhole/raw
内参与实测码边长，不能套用别的摄像头或猜焦距。

1. 打印 chessboard_9x6_20mm.svg，9×6 内角点、格长 20mm，整图 240×180mm；实测格长。
2. 网页“保存原始标定照片”，采 20～30 张清晰完整棋盘，覆盖四角、不同倾角和距离。
   这是原始编码像素，倒着看也不手工旋转。JPG 放进板端 ~/microduck-vision/calibration-images/。
3. 停旧 serve.py，在板端执行（实际格长变化时修改参数）：

```bash
cd "$HOME/microduck-vision"
.venv/bin/python aruco_tools.py calibrate \
  --images calibration-images --cols 9 --rows 6 --square-mm 20 --output camera.json
.venv/bin/python serve.py --detector aruco --marker-id 0 --marker-mm 50 --calibration camera.json
```

至少 12 张有效照片，RMS>2px 拒绝；成功仍要用尺子验证多个距离。
显示为**相机光心到码中心**，不是机身/脚底安全间隔。
确认后选网页米制模式、设置目标距离（默认 0.60m），再跟随。
码太小、裁切、重投影异常或分辨率比例不匹配时 pose 无效停平移，不自动回退大小模式。
更换相机/裁切/实际像素旋转后重新标定；只改方向元数据且 raw 像素未变时可保留同一内参。

## 12. 更新、恢复键盘和回退

| 改动 | WSL 操作 | 板端操作 |
| --- | --- | --- |
| 仅 vision_follow | sh vision_follow/push.sh "$BOARD" | Ctrl-C 旧服务，更新后重启原命令；网页 Ctrl-F5 重连 |
| robotd Rust | bash build.sh；sh xh_push.sh "$BOARD" | 脚本重启 robotd，退出旧控制器后重连 |
| 配置 / 模型 | sh xh_push.sh "$BOARD"（需已有 robotd 产物） | 核对配置，脚本重启 robotd |
| mediad Rust | 按方向文档编译/替换 mediad | 重启 mediad、重新连视频 |
| 完整运行时 | sh scripts/dev-push.sh "$BOARD"，随后 xh_push 配置/模型 | 核对全部 daemon/robotctl 版本一致 |

恢复键盘：网页停控并断开，Ctrl-C 退出视觉服务器，板端：

```bash
sudo systemctl restart robotd
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py --speech off
```

更新前可板端备份 `cp -a "$HOME/microduck-vision" "$HOME/microduck-vision.backup-$(date +%Y%m%d-%H%M%S)"`。
回退先停控/停旧进程，恢复对应源码并保留 venv/内参。方向改 offset=0、pixels=false 并重启 mediad 即回原方向。
代码按分类提交逆序 revert，不 reset --hard 覆盖其他工作。

## 13. 排错、日志和电脑端识别

网页“下载演示日志”保存 JSONL，刷新前下载。包含健康年龄/RTT、视频收包/解码/显示帧、
编码/HTTP/OpenCV/帧龄、控制阶段及关节反馈，不保存视频。

板端：

```bash
sudo /opt/robot/daemon/current/bin/robotctl health
sudo systemctl status robotd mediad --no-pager -l
sudo journalctl -u robotd -u mediad --since '-3 minutes' --no-pager
v4l2-ctl --list-devices
for d in /dev/media*; do
  [ ! -e "$d" ] || sudo media-ctl -d "$d" -p
done
```

缺诊断工具时先 `sudo apt install -y v4l-utils`。

| 现象 | 处理方向 |
| --- | --- |
| 信令不可达 | mediad active、8443 监听；8091 能打开不代表视频正常 |
| none has an imx219 entity | 查传感器是否在 media 拓扑；imx219 probe -5 是 I²C 探测失败 |
| 只有 rkisp/dphy | 断电后查排线/供电/型号与 /boot/armbianEnv.txt overlay，不盲改 ISP 格式 |
| unknown field stand_up | mediad、robotd 都要对应版本，仅更新网页/robotctl 不够 |
| 起身没反应 | 区分入队/开始，看 stand-up refused、IMU ready 和总线，不关保护 |
| imu_fresh=false | 单次重复不等于断流，看 0.5 秒内独立 health、连续 stale 和 bus |
| 检测慢/超时 | 看分段耗时，先 640/0.5Hz 默认；可把 JPEG 处理移电脑 |
| 有框头不动 | 查 control_active、policy/mode、head 应答、目标/关节差异、模型 |
| 只晃一下 | 确认服务重启后为 2.5 秒新默认；看 moving 的 elapsed/requested_vx/peak_vx、提前停止原因 |
| 画面冻结 | 停控，对比 8080、8091 纯视频，再开识别；看收包/解码/显示计数 |
| currentTime 走但图不动 | 不是新帧证据；冻结仍停控，“恢复视频播放”不恢复运动 |

板端处理吃紧时，电脑 WSL 独立识别（实际 IP 替换）：

```bash
cd "$HOME/src/microduck_rust"
python3 -m venv .venv-vision
.venv-vision/bin/python -m pip install --only-binary=:all: -r vision_follow/requirements.txt
.venv-vision/bin/python vision_follow/serve.py \
  --detector aruco --marker-id 0 --marker-mm 50 --robot-host 192.168.2.70 --bind 127.0.0.1
```

同机 Windows 浏览器访问 http://localhost:8091/；JPEG 在电脑处理，WebRTC 仍直连板子。
米制模式仍用板端视频内参，不是电脑摄像头内参；同样需要健康状态及人工控制。

## 14. 开发电脑离线检查

先按上一节创建 .venv-vision：

```bash
node --test vision_follow/test_core.cjs vision_follow/test_follow_cycle.cjs \
  vision_follow/test_aruco_client.cjs vision_follow/test_app.cjs
.venv-vision/bin/python -m unittest discover -s vision_follow -p 'test_*.py'
```

仅验证逻辑、合成图像和模拟接口，不驱动机器人，不能代替第 10 节实机验收。
