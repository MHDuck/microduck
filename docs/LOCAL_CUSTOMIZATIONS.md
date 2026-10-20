# HD1910 本地部署增量（2026-10-20）

本工程从墨恒 Gitee `microduck_rust` 的最新 `master` 克隆，迁移时基线为 `2f9c655`。保留
`origin` 与完整 Git 历史；本地功能只叠加在该基线上。旧目录 `F:\Code\microduck_rust`
仅作为历史对照，不再直接部署。

## 1. 明确保留与明确不迁移的内容

保留：

- `scripts/duck-voice.py`：复用 robotd 官方 voice bank，支持 happy/greeting/curious/sad/
  alarm/goodbye，选择性同步嘴部。
- 嘴部独立控制：机器人全身未启用时，`robot.mouth` 只给第 15 个嘴部舵机上力和写目标，
  不启用策略、不触碰其余 14 个舵机；500 ms 无新嘴部帧后释放嘴部扭矩。
- `scripts/keyboard-control.py` 的行走随机讲话：走路命令非零时连续随机说话，停走立即停止
  新话语并闭嘴；主控制与发声使用独立 robotd 连接/线程。
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

支持的情绪：`happy`、`greeting`、`curious`、`sad`、`alarm`、`goodbye`。这不是文本转语音，
而是对官方声音库的情绪化组合；指定任意文本需要后续单独引入 TTS 和音频播放接口。

安全语义：`--sound-only` 连“闭嘴”命令也不发，因为 `open=0` 同样会让独立嘴部路径给舵机
上力。嘴部动画模式在异常/中断时会显式闭嘴。

## 3. 行走时随机讲话

```bash
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py --walk-voice
```

- Enter：启用模型；W/S 调整前后速度；A/D 横移；Q/E 转向；空格速度归零。
- V：运行中开关随机讲话。未传 `--walk-voice` 时也可按 V 开启。
- 只要 `vx/vy/vyaw` 任一非零就认为正在行走；速度归零、禁用或退出时不再开始下一段声音。
- 相邻两次不会选择相同情绪，默认排除较刺耳的 `alarm`。
- 默认话语间隔随机 0.25～0.80 s。连续走会持续讲话，但不会重叠播放。

可选参数：

```bash
# 只随机播放声音，不动嘴
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py \
  --walk-voice --voice-sound-only

# 自定义情绪和间隔
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py \
  --walk-voice \
  --voice-emotions happy,greeting,curious \
  --voice-gap-min 0.4 --voice-gap-max 1.2
```

语音工作线程使用第二条 Unix socket，不阻塞 50 Hz 行走命令循环。它只观察“当前是否有
非零速度命令”，不改变速度、策略或 SitStand 状态。

## 4. 摄像头流和鸭子检测（不关联舵机）

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

```bash
python3 -m unittest \
  scripts/test_duck_voice.py \
  scripts/test_walking_voice.py \
  scripts/test_camera_detect.py

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
