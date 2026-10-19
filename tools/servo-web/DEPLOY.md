# Radxa 板端部署（v0.19.0）

工程源代码在当前仓库 `tools/servo-web`；运行包由此目录的 `package.py` 生成。
不再使用父目录中旧的 `tools/servo-web` 或原先根目录 `dist` 中的部署包。

## 随新板初始化安装

当前仓库的 `scripts/provision-board.sh` 默认打包并上传本目录，在重启前以 SSH 登录用户
安装到 `~/microduck/tools/servo-web`，创建 `servo-web.service`。新板首次安装和已有板子的
环境刷新都会安装；使用普通用户（如 `radxa`）连接，脚本按需调用 sudo。

初始化只安装调试台，不启动它，也不设置开机自启。初始化完成后，托稳机器人再执行：

```bash
sudo systemctl restart servo-web
sudo systemctl status servo-web --no-pager -l
```

随后访问板端 IP 的 8081 端口。启动调试台会停止原控制服务；恢复方法见下文。
网页串口选择默认 `/dev/ttyS4`，不会再提示插入 USB；串口列表会保留服务实际连接的端口。
已用旧脚本初始化、提示 `Unit servo-web.service not found` 的板子无需重刷或重跑整套初始化，
按下面的独立部署步骤补装即可。

## Windows PowerShell

```powershell
scp "D:\xh_brushless\机器鸭\microduck-replica\microduck_飞特FORK\tools\servo-web\dist\servo-web-board.tar.gz" radxa@192.168.2.248:~/
ssh radxa@192.168.2.248
```

主机名已能解析时，两处 `192.168.2.248` 都可以换成 `radxa.local`。

## 板端终端

先托稳机器人，再执行：

```bash
mkdir -p ~/microduck/tools/servo-web
tar -xzf ~/servo-web-board.tar.gz -C ~/microduck/tools/servo-web
cd ~/microduck/tools/servo-web
bash deploy-board.sh /dev/ttyS4
```

如果只安装服务、稍后再开始调试，使用 `bash deploy-board.sh /dev/ttyS4 --install-only`。
该模式允许串口设备暂未出现，适用于新板配置 overlay 后、重启前的初始化阶段；
真正启动服务时仍由 `ExecStartPre` 检查串口存在且未被占用。

安装脚本会安装 Python、ALSA、SoX、GStreamer 等依赖，配置串口、I²C、音视频
设备权限，依次停止 `padd` / `mediad` / `robotd` / `tofd` 后启动 `servo-web.service`。
调试服务与这四项服务互斥；只停止 `robotd` 不够，`padd`、`mediad` 重启时会再次拉起它，
导致调试服务被停止，表现为网页刚打开就断线，随后 8081 拒绝连接。
pip 安装固定使用清华 PyPI 镜像 `https://pypi.tuna.tsinghua.edu.cn/simple`。
若旧部署脚本正在从默认源慢速下载，可先按 Ctrl+C，再在本目录执行：

```bash
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash deploy-board.sh /dev/ttyS4
```

脚本按需使用 sudo，服务本身以当前普通用户运行。
标准部署在整个调试期间保持 `mediad` 停止，释放相机和声卡；拍照结束也保持停止。
仅独立运行 Python 程序、未使用上述 systemd 服务时，拍照才会临时停止并恢复原先运行的 `mediad`。
相机操作仅获准通过 sudo 启停 `mediad.service`。

若从旧的 `~/servo-web` 升级，脚本会先停止旧网页服务；新目录没有对应数据时，
复制旧目录的 `directions.json`、`poses/`、`calib/`。已有数据不会被默认值覆盖。
部署包不包含运行时校准、姿态、照片、录音、日志或虚拟环境。

打开 [IP 地址访问](http://192.168.2.248:8081) 或
[主机名访问](http://radxa.local:8081)。8080 是原 `mediad` 的 `duck console` 页面，
调试台使用 8081；调试期间原控制台的 `mediad` 保持停止。若主机名不是 radxa，可执行：

```bash
sudo hostnamectl set-hostname radxa
sudo systemctl restart avahi-daemon
```

主机名访问仍需电脑与板子在可互通的网络中，并支持 mDNS；不是无网络访问。

## 网页功能与硬件配置

| 功能 | 配置与显示 |
|---|---|
| 电机 | `/dev/ttyS4`，1 Mbps；保留单颗 / 全部当前位置零点及临时 ID 对调 |
| BNO085 | `/dev/i2c-3`、`0x4B`；50 Hz 请求 Game Rotation Vector、角速度和加速度；网页姿态及 3D 躯干跟随 |
| LSM6DSV16X | `/dev/i2c-3`、`0x6A`，WHO_AM_I `0x70`；沿用已验证的 60 Hz、±250°/s 配置，10 Hz 读取三轴角速度 |
| VL53L5CX | `/dev/i2c-4`、`0x29`；上传驱动自带固件后读取 8×8、5 Hz 距离；毫米，状态 5 / 9 且有目标才显示距离 |
| 播放 | `hw:CARD=microducki2s3,DEV=0`；附带 `hello_loud.wav`，板端扬声器播放 |
| 录音 | 同一声卡，48 kHz、双声道 S32_LE、5 秒；提取左声道并按交接文档压缩、增强，提供浏览器试听和板端回放 |
| 相机检查 | `media-ctl -d /dev/media0 -p`，区分 IMX219 已注册、链路未注册、媒体设备不存在 |
| 拍照 | `/dev/video0`，NV12 / 1920×1080，io-mode=4，exposure=600，analogue_gain=1024，采集 60 帧保存最后一张 JPEG |

BNO085 的安装方向沿用机器人代码：传感器 +X 向上、+Y 向左、+Z 向后。
页面自动选择对应的“绕 Y −90°”传感器到躯干转换；首次有效姿态自动显示归零。
显示归零不改写电机或 IMU，偏航为相对角度。加速度和角速度使用传感器 XYZ 轴。
LSM6DSV16X 当前显示角速度，没有启用其 SFLP 姿态融合；躯干姿态来自 BNO085。
VL53L5CX 保留原始分区顺序，没有旋转或镜像。

每个传感器独立读取，断开或缺少依赖时单独报错并重试，不伪造硬件读数。
3D 库已随包提供，浏览器无需访问外网；首次安装 Python / 系统依赖仍需要板端联网。
程序不修改启动 overlay；需要板子已经具备交接文档中的 I²C3、I²C4、I²S3 和 IMX219 驱动配置。

照片和录音保存在 `data/`，只保留最近一次测试文件；无自动录音、自动播放或自动拍照。

## 电机过流保护

页面“电机过流保护”提供读取单颗、读取全部、设置单颗、设置全部。
先读取，查看当前原始值、参考 mA、过流开关和保护时间，再填写要设置的原始整数。
“全部”指配置及扫描发现的电机，离线或写入失败会逐颗列出；已写入的电机不会自动回滚。
只有单颗读取成功才自动将读数填入输入框，批量读取不会替你选统一值。

| 项目 | 寄存器 | 本功能行为 |
|---|---|---|
| 保护电流 | 28 / 0x1C，2 字节小端 | 读取 / 设置原始整数 0–2047；参考 mA = 原始值 × 6.5 |
| 过流开关 | 19 / 0x13 的 bit3 | 默认保留；勾选“同时开启”才写入 `原值 OR 8`，不清除其他保护位 |
| 过流保护时间 | 38 / 0x26 | 只读，按原始值 × 10 ms 显示 |
| EEPROM 锁 | 55 / 0x37 | 写入前解锁并检查，结束或出错时重新加锁并检查 |

只在点击设置并确认后写入，不随页面刷新重复写 EEPROM。数值相同也会按解锁流程写入，
以保存可能原先只在 RAM 中生效的值。设置后逐颗回读；模拟模式明确标为模拟电机。
降低阈值或启用保护可能使电机卸力，操作前托稳机器人。

依据本地 `docs/飞特资料/磁编码STS内存表手册-v1.1-2026-08-27.md` 核对寄存器地址，
并于 2026-10-19 对照 [飞特 HLS 内存表](https://ftservo.github.io/ftservo-wiki/reference/parameter/memory-hls/)：
该表给出地址 28 的范围 0–2047、单位 6.5 mA；本地 STS 表另有“最大 500”的矛盾说明，未将它当作 HD1910 的上限。
[飞特 HD-1910-C001 型号页](https://ftservo.github.io/ftservo-wiki/products/models/hd-1910-c001/main/)
的专用内存表仍标为待补充，因此网页保留原始值输入，mA 仅作通用表参考换算，不代表已验证该固件的实际触发电流。
寄存器可填范围不是电机安全工作电流范围。

本功能不改地址 44 的运行时目标电流、扭矩开关或位置目标，不自动重启电机；
HLS 表说明地址 28 会在上电时赋值给 44。它也不修改 `robotd.toml` 的软件保护参数。
实机掉电保持及过流触发尚需板端验证。

## 电机欠压 / 过压保护

页面“电机欠压 / 过压保护”支持单颗和全部读取、设置。直接输入 V，步进 0.1 V，
欠压阈值必须小于过压阈值。单颗读取会将两个阈值填入输入框；只调整其中一个时，
先读取并保留另一个值。设置时会保存两个阈值；全部设置会将这一对阈值写到所有配置及扫描发现的电机。

本地 STS 表与[飞特 HLS 内存表](https://ftservo.github.io/ftservo-wiki/reference/parameter/memory-hls/)一致：

| 项目 | 寄存器 | 本功能行为 |
|---|---|---|
| 欠压阈值 | 15 / 0x0F，1 字节 | 读写原始值 0–254，网页按原始值 / 10 显示 V |
| 过压阈值 | 14 / 0x0E，1 字节 | 同上；网页输入范围 0.0–25.4 V |
| 共用电压保护开关 | 19 / 0x13 的 bit0 | 默认保留；勾选“同时开启”才写入 `原值 OR 1`，保留过流等其他保护位 |

两项阈值共用保护开关，不能分别开关欠压和过压。复用过流设置的解锁、逐项写入回读、重新加锁流程；
两个阈值依次写入，顺序避免中间出现下限不小于上限。通信中断可能只写入其中一项，
失败会单独显示，需重新读取确认；已写入项不自动回滚。
寄存器范围不代表电机允许的供电范围；HD1910 的实际触发及掉电保持尚需板端验证。
修改阈值或开启保护可能使电机卸力，操作前托稳机器人。

## 运行与排查

若网页反复断线，`servo-web` 为 `inactive/dead`、`Result=success`，
同时 `robotd` 已恢复运行、`mediad` 或 `padd` 在自动重启，可先原地修复已安装的服务。
先托稳机器人；命令无需重新安装依赖：

```bash
sudo sed -i.bak \
  -e '/^After=/c\After=network.target robotd.service tofd.service padd.service mediad.service' \
  -e '/^Conflicts=/c\Conflicts=robotd.service tofd.service padd.service mediad.service' \
  /etc/systemd/system/servo-web.service
sudo systemd-analyze verify /etc/systemd/system/servo-web.service &&
sudo systemctl daemon-reload &&
sudo systemctl restart servo-web.service
sleep 10
sudo systemctl show servo-web.service robotd.service mediad.service padd.service \
  -p Id -p ActiveState -p SubState -p Result --no-pager
sudo ss -lntp | grep ':8081'
```

预期调试台为 `active/running`，另外三项为 `inactive/dead`，8081 由 Python 监听。
调试期间不要启动 `mediad`、`padd`、`robotd` 或 `tofd`；它们启动会停止调试台。
此命令只修复已安装服务；下次部署请使用 v0.17.2 或更新的包，避免旧脚本覆盖配置。
仍异常时获取完整日志（`--no-pager` 避免输出停在分页器）：

```bash
sudo journalctl -u servo-web -u robotd -u mediad -u padd -b -n 120 --no-pager -o short-precise
```

若 8080 打开的是 `duck console`，在板端将旧调试台服务切换到 8081：

```bash
cd ~/microduck/tools/servo-web
sed -i 's/--http-port 8080/--http-port 8081/g; s/:8080/:8081/g' deploy-board.sh
sudo sed -i.bak 's/--http-port 8080/--http-port 8081/g' /etc/systemd/system/servo-web.service
sudo systemd-analyze verify /etc/systemd/system/servo-web.service &&
sudo systemctl daemon-reload &&
sudo systemctl restart servo-web.service
sudo systemctl status servo-web.service --no-pager -l
```

随后访问 8081，无需重新安装依赖。检查监听可用 `sudo ss -lntp | grep ':8081'`。

旧部署脚本若提示 `Unit servo-web.service has a bad unit file setting`，其中
`WorkingDirectory` 错误地带了双引号。无需重新安装依赖，在板端执行：

```bash
sed -i '/^WorkingDirectory=/s/"//g' ~/microduck/tools/servo-web/deploy-board.sh
sudo sed -i.bak '/^WorkingDirectory=/s/"//g' /etc/systemd/system/servo-web.service
sudo systemd-analyze verify /etc/systemd/system/servo-web.service &&
sudo systemctl daemon-reload &&
sudo systemctl restart servo-web.service
sudo systemctl status servo-web.service --no-pager -l
```

新版部署脚本已修复该写法，并在重新加载服务前运行 `systemd-analyze verify`。

```bash
sudo systemctl status servo-web --no-pager
sudo journalctl -u servo-web -n 80 --no-pager
sudo systemctl restart servo-web
```

默认不设置开机自启；下次调试执行 `sudo systemctl start servo-web`。
不要让调试服务与原控制服务同时开机自启，以免启动时互斥。
回到原控制服务：

```bash
sudo systemctl stop servo-web
sudo systemctl start robotd mediad padd
# 原本需要独立测距服务时再启动：sudo systemctl start tofd
```

设备检查：

```bash
ls -l /dev/ttyS4 /dev/i2c-3 /dev/i2c-4 /dev/media0 /dev/video0
cat /proc/asound/cards
aplay -l
arecord -l
media-ctl -d /dev/media0 -p | grep -i imx219
```

不要同时运行原监控脚本或其他直接访问这三个传感器的程序。
`VL53L5CX` 的 I²C 超时会单独显示并重试，不会因此停止整个网页服务；应在网页稳定后检查其 I²C4 链路。

## 重新打包

在 Windows 的当前仓库中执行：

```powershell
python tools/servo-web/package.py
```

输出 `tools/servo-web/dist/servo-web-board.tar.gz` 和 SHA256 校验文件。
`.gitignore` 排除部署包、虚拟环境、运行时配置、校准、日志、照片和录音；
`defaults/` 的默认方向和姿态、Hello 样本、模型和本地 3D 库保留在源码中。
