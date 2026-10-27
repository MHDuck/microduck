# HD1910 本地部署增量（2026-10-20）

本工程从墨恒 Gitee `microduck_rust` 的最新 `master` 克隆，迁移时基线为 `2f9c655`。保留
`origin` 与完整 Git 历史；本地功能只叠加在该基线上。旧目录 `F:\Code\microduck_rust`
仅作为历史对照，不再直接部署。

## 1. 明确保留与明确不迁移的内容

保留：

- `scripts/duck-voice.py`：复用 robotd voice bank，支持 happy/greeting/curious/sad/
  alarm/goodbye/laughing，选择性同步嘴部。
- 嘴部独立控制：机器人全身未启用时，`robot.mouth` 只给第 15 个嘴部舵机上力和写目标，
  不启用策略、不触碰其余 14 个舵机；500 ms 无新嘴部帧后释放嘴部扭矩。
- `scripts/keyboard-control.py` 的行走连续大笑：走路命令非零时连续播放五秒大笑，停走立即
  停止新一段并闭嘴；主控制与发声使用独立 robotd 连接/线程。
- `scripts/camera-detect-test.py`：只读验证官方摄像头流和 MicroDuck detector，不连接
  robotd、不控制舵机。

不迁移旧仓库的：本地 ONNX、`deploy/robotd.toml.walk`、HOME、KP、过流阈值、fall recovery、
控制器/策略切换、舵机模型、`keyboard-control.py` 旧版、`xh_push.sh` 旧版等实验改动。新工程
以墨恒最新实现为准，只在最新版 `xh_push.sh` 中增加上述脚本的上传。

## 2. 单独测试语音与嘴部

板端运行，完全不需要启用行走：

```bash
sudo python3 /opt/robot/daemon/current/scripts/duck-voice.py --list

# 发声并只驱动嘴部舵机
sudo python3 /opt/robot/daemon/current/scripts/duck-voice.py \
  --emotion greeting

# 只发声：不得发送任何 robot.mouth 指令，嘴部不会因本脚本上力
sudo python3 /opt/robot/daemon/current/scripts/duck-voice.py \
  --emotion greeting --sound-only
```

支持的情绪：`happy`、`greeting`、`curious`、`sad`、`alarm`、`goodbye`、`laughing`。
这不是文本转语音，而是对合成声音库的情绪化组合；指定任意文本需要后续单独引入 TTS
和音频播放接口。`laughing` 是本地新增的固定 5 秒、多段落大笑。

安全语义：`--sound-only` 连“闭嘴”命令也不发，因为 `open=0` 同样会让独立嘴部路径给舵机
上力。嘴部动画模式在异常/中断时会显式闭嘴。

### 声音文件与音量

`duck-voice.py` 本身不包含录音，也不直接播放文件。它把 `happy`、`greeting` 等情绪映射成
`chirp`、`greet` 等 `robot.sound` 标签；`robotd` 收到标签后，从
`/var/lib/robot/sounds/<tag>/` 随机选择一个 WAV。WAV 不是仓库里的固定录音，而是安装时由
`sounds ensure-bank` 根据 SoC 序列号合成的每机专属 voice bank。合成配方在
`sounds/src/voices.rs`，文件布局与生成逻辑在 `sounds/src/lib.rs`。

合并后的 v8 voice bank 保留 +3 dB 预渲染增益、大笑音色，以及本地 PCM 输出的软限幅。
预渲染增益来自远程 v7，放大后限制在 −1～+1；写 PCM 时继续使用本地
`tanh(sample * 3) * 0.95` 软限幅，因此最终输出不只是线性增加 3 dB。
部署新版本时 marker 版本变化会自动重生成声音库，也可在板端手动执行：

```bash
sudo /opt/robot/daemon/current/bin/sounds ensure-bank --force
```

板上功放是 MAX98357A，只有 I2S 音频输入，没有 I2C 或 ALSA mixer 音量寄存器；当前
`microduck-i2s3-m0.dts` 也使用 dummy codec。因此 `amixer` 不能继续提高扬声器功放增益。
若数字音频已接近满幅仍不够响，需要按实际功放模块/原理图调整 MAX98357A 的 GAIN 引脚
配置，或更换灵敏度更高且阻抗、功率匹配的扬声器；修改前先确认模块型号、供电、电阻配置
和扬声器额定参数，避免削波、过热或损坏扬声器。

## 3. 行走时连续大笑

```bash
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py --walk-voice
```

- Enter：同手柄 Start，开启/关闭策略；W/S 调整前后速度；A/D 横移；Q/E 转向；空格速度归零。
- H：运行中开关行走发声。未传 `--walk-voice` 时也可按 H 开启；V 保留为右踢腿。
- Y/B 头部/身体模式、T 坐站等键盘映射见 [README](../README.md#ssh-键盘控制)。
- R/F 手动嘴部和声音输入期间暂停行走语音，释放后按行走状态恢复。
- 只要 `vx/vy/vyaw` 任一非零就认为正在行走；速度归零、禁用或退出时不再开始下一段声音。
- 默认只播放固定 5 秒的 `laughing`，段间隔为 0；连续走会一段接一段大笑，但不会重叠。
- 停走时会立即闭嘴并且不再开始下一段；已经交给 ALSA 的当前大笑会自然播放到结尾。

可选参数：

```bash
# 只随机播放声音，不动嘴
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py \
  --walk-voice --voice-sound-only

# 恢复原来的随机情绪和间隔
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py \
  --walk-voice \
  --voice-emotions happy,greeting,curious \
  --voice-gap-min 0.4 --voice-gap-max 1.2
```

语音工作线程使用第二条 Unix socket，不阻塞 50 Hz 行走命令循环。它只观察“当前是否有
非零速度命令”，不改变速度、策略或 SitStand 状态。

## 4. 摄像头流和鸭子检测（不关联舵机）

本地相机按需采集：浏览器点击 `connect` 建立视频连接时开启，最后一个观看者断开后关闭。
无人观看时自动曝光和鸭子检测暂停，网页与视频连接入口保持可用。

最新版官方工程已经包含：

- `mediad` 摄像头 + WebRTC 流；
- 浏览器控制台 `http://<板子>:8080/`；
- `duck-detect` ONNX/ RKNN 模型；
- `mediad` 内置 2 Hz detector，并通过 `media.detections` 向浏览器发检测框；
- NPU/CPU detector benchmark（`duck-bench`）。

首次开启 detector：

```bash
sudo robotctl configure
```

在交互界面设置 `detect.enabled = true`，保持 `detect.hz = 2.0`，保存并接受重启 `mediad`。
2 Hz 是热设计上限；不要为了测试提高频率，否则 Radxa Zero 3 可能过热降频，反而影响行走。

只读诊断：

```bash
sudo python3 /opt/robot/daemon/current/scripts/camera-detect-test.py \
  --host 10.138.16.248 --seconds 20
```

脚本会检查配置、`mediad`、相机统计文件，打印浏览器 URL，并在限定时间内筛选 detector
日志。然后在电脑浏览器打开：

```text
http://10.138.16.248:8080/
```

让另一只 MicroDuck 出现在画面中即可看检测框。这个过程不调用 robotd，不启用模型，
不发送任何舵机指令。`radxa.local` 只有在 WSL/Windows 的 mDNS 能解析时可替代 IP。

## 5. 本地测试

### 独立测试 StandUp（不修改正式配置）

`policies/standup_hd1910.onnx` 已随模型上传，但正式配置的 `policy.stand = "none"`，
所以当前正常行走不会调用它。`robot.init` 只做 HOME 上力/过渡，也不代表 StandUp 推理。

`scripts/standup-test.py` 使用当前板端配置生成临时配置：`walk` 和 `stand` 都指向本次
StandUp 模型（正常零命令时策略标签为 `stand`），关闭 SitStand/其他技能、声音与 chorale，
并在这次进程中关闭 `fall_protection` 和 `limp_fall`，避免打断地面起身。舵机增益、动作缩放、
IMU 和过流配置沿用原文件。正式配置、模型和 daemon 二进制均不改动。

板端先检查，不会打开硬件：

```bash
sudo python3 /opt/robot/daemon/current/scripts/standup-test.py --check
```

实际测试（先托住机器人；停止 daemon 或测试结束卸力时机器人会失去支撑）：

```bash
sudo systemctl stop padd robotd
# 如果 servo-web.service 正在运行，也需要先停止它。
sudo python3 /opt/robot/daemon/current/scripts/standup-test.py --seconds 10
```

脚本启动独立 socket 的测试 daemon，模型与 IMU 就绪后等待 Enter。把机器人放到趴倒、
仰倒或坐倒姿态，再按 Enter。启用后约 2 秒先回 HOME，之后执行 StandUp；默认 10 秒包含
这段过渡。终端显示实际策略和重力向量。时间到、Ctrl-C 或 SSH 挂断会请求 `robot.relax`，
然后结束测试进程；daemon 日志保留在打印的 `/tmp/standup-test-*.log`。脚本只确认模型有执行，
恢复是否成功由实际观察判断。趴倒、仰倒、坐倒应分三次测试。

模型和试验时间可逐次指定，无需手改文件：

```bash
sudo python3 /opt/robot/daemon/current/scripts/standup-test.py \
  --model /opt/robot/daemon/current/policies/standup_hd1910.onnx --seconds 12

# 每次测试结束后服务保持停止；准备好后手动恢复日常 daemon。
sudo systemctl start robotd
# 日常需要手柄时再执行 sudo systemctl start padd
```

这是独立起身模型测试，尚未将 StandUp 接入日常行走的自动倒地恢复状态机。

### 自动化回归

```bash
python3 -m unittest \
  scripts/test_duck_voice.py \
  scripts/test_walking_voice.py \
  scripts/test_camera_detect.py \
  scripts/test_standup_test.py

cargo fmt --all --check
cargo test -p duck-control
cargo test -p robotd intents::tests::mouth_notifications_have_an_edge_and_age_without_enabling_motion
cargo test -p robotd a_limp_mouth_command_only_touches_the_beak
```

核心回归要求：

- `--sound-only` 事件序列中只有 `robot.sound`；
- stopped/zero velocity 时 WalkingVoice 不产生声音；
- limp mouth 测试中 full-body write 和 full-body torque write 均为 0；
- 摄像头测试脚本源码不连接 `/run/robotd.sock`。

## 6. WSL 构建与上传

```bash
cd /mnt/f/Code/microduck_code/microduck_rust

# 新电脑或 Cargo 缓存缺包时先联网补齐；否则 --offline 会在 aho-corasick 等包处失败
cargo fetch --locked

# 离线增量交叉编译并上传 robotd、配置、ONNX、键盘/语音/视觉测试脚本
sh xh_push.sh --build radxa@10.138.16.248
```

如果二进制已构建，仅更新脚本/配置/模型：

```bash
sh xh_push.sh radxa@10.138.16.248
```

首次板端环境初始化才运行：

```bash
./scripts/provision-board.sh --no-ble radxa@10.138.16.248
```

`xh_push.sh` 会校验 robotd 和配置 SHA-256，然后重启 robotd。若 `radxa.local` 报
`Could not resolve hostname`，继续用 IP；这不是代码或 SSH 密码问题，而是本机/WSL mDNS
解析未配置。

## 7. 同步上游

本地功能应保存在单独分支/提交中。更新时：

```bash
git status
git fetch origin
git rebase origin/master
```

若 voice 相关文件冲突，只保留本文列出的最小语义；不要把旧仓库的整份 `robotd/main.rs`
覆盖到新版。解决后重跑第 5 节全部测试，再部署。不要对有未提交改动的工作树直接
`git pull`，也不要使用 `git reset --hard`。
