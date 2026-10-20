<p align="center">
  <img src="https://github.com/user-attachments/assets/c2f7c245-8217-46a1-8d1e-e0ba967cd969" alt="microduck" width="820">
</p>

<h1 align="center">Microduck</h1>

<p align="center">
  <em>A tiny biped robot that moves using reinforcement learning policies.</em>
</p>

<p align="center">
  <a href="https://pollen-robotics.com/microduck"><b>Get yours here</b></a> ·
  <a href="docs/robot/cheatsheet.md">Cheat sheet</a> ·
  <a href="https://github.com/pollen-robotics/microduck_rl">Training the policies</a> ·
  <a href="docs/design/architecture.md">How it works</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

<p align="center">
  <a href="https://github.com/pollen-robotics/microduck/actions/workflows/ci.yml"><img src="https://github.com/pollen-robotics/microduck/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

---

**This repo is the duck's brain.** About 25 cm and 800 g of robot, run by a handful of daemons on a
Rockchip RK3566: a 50 Hz control loop driving fifteen servos from neural policies, the radios and
the camera, and the update machinery that gets new software onto a robot without bricking it.

Everything you need to run a Microduck is here. **If you want one,
[get yours here](https://pollen-robotics.com/microduck).**

The policies it runs are trained next door, in
**[microduck_rl](https://github.com/pollen-robotics/microduck_rl)** — MuJoCo and PPO, the sim2real
recipe, and the export to ONNX that this repo loads.

## 飞特分支：蓝牙手柄配对、控制与模型

使用 `scripts/provision-board.sh` 完成整机安装后，手柄直接配对到机器人板子。
安装程序默认启用 `padd.service`，配对成功后由它读取输入；断开 SSH 不影响控制。
`configd` 负责蓝牙配对，`padd` 负责手柄输入，`robotd` 负责运动策略和舵机。
`btd` 是手机等客户端使用的 BLE 服务，手柄输入不经过它。

### 配对与连接检查

先让手柄进入配对模式：

- **Xbox**：短按 Xbox 键开机，再长按顶部 Sync 小按钮，直到指示灯快速闪烁。
  慢闪表示开机但未进入配对模式；长按 Xbox 键会关机。
- **DualSense**：同时按住 Create 和 PS，直到灯条闪烁。

在**板子的终端**执行：

```bash
sudo robotctl pad pair
robotctl pad status
```

正常应看到手柄 `connected`、`padd active`。配对会同时设置信任，以后打开手柄可自动重连。
发现多个待配对手柄时需要指定 MAC；遗忘后重新配对，需要再次让手柄进入配对模式：

```bash
sudo robotctl pad pair AA:BB:CC:DD:EE:FF
sudo robotctl pad forget AA:BB:CC:DD:EE:FF
```

上面两条分别用于指定配对和遗忘绑定，按需执行。

如果此前启动过 `servo-web`，它会停止 `padd`、`mediad`、`robotd`、`tofd`。
先托稳机器人，再切回手柄控制：

```bash
sudo systemctl stop servo-web
sudo systemctl start robotd padd
```

### 按键映射与对应模型

配对后按 **Start** 启用运动策略，再操作摇杆。以下使用 Xbox 按键名称；
默认步行模式下，摇杆死区为 `0.1`，前后及横移速度上限为 `0.3 m/s`，转速上限为 `1.5 rad/s`。

| 操作 | 功能 | 模型或控制方式 |
|---|---|---|
| Start | 开启／关闭运动策略；关闭时回默认姿态，不是关闭力矩 | 开关策略执行 |
| 左摇杆上下 | 前进／后退 | 共用 `walk`，改变 `vx` |
| 左摇杆左右 | 左右横移 | 共用 `walk`，改变 `vy` |
| 右摇杆左右 | 左右转向 | 共用 `walk`，改变 `vyaw` |
| 松开摇杆 | 速度指令归零 | 配置了 `stand` 时可自动选择站立模型，否则仍使用 `walk` |
| Y | 切换头部模式，再按退出；左摇杆控制头部俯仰／偏航，右摇杆控制颈部俯仰／头部侧倾 | 头部目标角度作为模型输入，没有单独的“摇头模型” |
| B | 切换身体姿态模式，再按退出；左摇杆上下控制高度，右摇杆控制俯仰／侧倾 | 改变身体姿态输入，配置了 `stand` 时使用站立模型 |
| A | 地面拾取 | `ground_pick` |
| LB / RB | 左／右踢腿 | `kick_left` / `kick_right` |
| X | 前滚翻，长按连续翻滚 | `roulade` |
| 方向键下 | 坐下／站起 | 共用 `sitstand`，通过姿态指令区分坐和站 |
| RT / LT | 张嘴；RT 同时触发叫声，LT 按住播放 wheee | 嘴部舵机和声音控制，不经过运动模型 |
| 方向键上长按 3 秒 | 切换步行／轮式模式 | 回默认姿态后重新加载目标模式的模型组合 |
| Select 长按 2 秒 | 请求坐下后关机 | 由 `robotd` 执行关机流程，坐下动作依赖对应策略 |

Y 和 B 模式期间行走速度归零。轮式模式使用独立的摇杆映射，不支持横移；
运行中切换模式不会修改配置文件，重启后仍使用配置中的模式。

**按键映射存在，不代表对应动作模型已经启用。** `robotd` 启动时一次加载并预热已配置的模型；
普通动作按键只是让控制循环选择已加载的模型推理，不会每按一次重新读取 ONNX 文件。
模型每拍结合传感器状态和控制指令计算关节目标，动作不是预录的舵机轨迹。

截至 2026-10-19，仓库 [deploy/robotd.toml](deploy/robotd.toml) 的配置为：

```toml
[policy]
enabled = true
mode = "walk"
walk = "/opt/robot/daemon/current/policies/walk_hd1910_flat_v5.onnx"
stand = "none"
sitstand = "/opt/robot/daemon/current/policies/sitstand_hd1910.onnx"
ground_pick = "none"
kick_left = "none"
kick_right = "none"
roulade = "none"
```

这份配置加载行走和坐站两个模型，没有启用独立站立、拾取、踢腿或翻滚模型。
缺少动作模型时请求会被拒绝，并提示 `no policy configured for that skill`。
头部和身体姿态的实际响应也取决于所用模型的训练能力，不能仅凭按键映射判断支持情况。
板子实际配置以 `/etc/robot/robotd.toml` 为准；修改模型路径后需要重启 `robotd`。

### 配不上或配上后没有响应

Radxa 的 aic8800 蓝牙模块可能需要在建立新绑定时暂停 `btd`。如果正常配对失败，
可在电脑端使用安装脚本的配对兼容参数（替换板子地址）：

```bash
./scripts/provision-board.sh --pause-btd-on-pair radxa@192.168.1.42
```

该参数留下 `/var/lib/robot/weird-ble` 标记，使 `robotctl pad pair` 在配对期间暂停 `btd`、
将蓝牙适配器断电再上电，并在结束后恢复原先运行的 `btd`。
仍无法建立绑定时才考虑 `--weird-ble`，它还会设置 `Privacy = device`。
若配对后反复掉线并出现 `PIN or Key Missing (0x06)`，可能需要改回 `Privacy = off` 并重新配对。
仅移除安装命令中的参数不会撤销已有设置，具体切换步骤见
[安装说明](docs/robot/install-dev.md#the-three-configurations-and-how-to-tell-which-you-have)。
该板子的排障记录不建议用 `systemctl restart bluetooth` 代替适配器断电／上电，
否则可能出现没有可用适配器、必须重启板子才能恢复的情况。

配对后没有响应时，在板端检查：

```bash
robotctl pad status
journalctl -u configd -u padd -u robotd -b -n 80 --no-pager
robotctl monitor
```

在 monitor 中按 `p` 打开手柄原始输入，移动摇杆应看到数值变化。
手柄连接、输入数据、运动服务和模型加载是不同环节；输入正常时还需确认已经按 Start 启用策略，
并查看是否存在模型缺失或保护锁定。完整配对和链路排障见 [手柄配对说明](docs/robot/pair-a-gamepad.md)。

### 对应代码入口

```text
蓝牙手柄 → BlueZ / Linux 输入设备 → padd（gilrs 读取，默认 50 Hz）
        → /run/robotd.sock → robotd → 选择模型并推理 → 舵机
```

| 文件／入口 | 负责内容 |
|---|---|
| [scripts/install.sh](scripts/install.sh) | 安装并启用 `padd.service` 等服务 |
| [scripts/setup-board.sh](scripts/setup-board.sh) 的 `configure_bluetooth` | 配对兼容标记与 Privacy 设置 |
| [robotctl/src/main.rs](robotctl/src/main.rs) 的 `run_pad`、`BtdPaused` | `pad pair/status/forget` 命令，以及配对期间暂停 `btd`、重置适配器 |
| [configd/src/bluez.rs](configd/src/bluez.rs) 的 `pair`、`bond` | 蓝牙扫描、连接、配对和信任 |
| [padd/src/main.rs](padd/src/main.rs) 的 `Args`、按钮事件和 `Mode` 分支 | 频率、死区、速度上限、按键映射、摇杆映射与 IPC 指令 |
| [padd/systemd/padd.service](padd/systemd/padd.service) | 手柄服务启动、输入设备权限和重启策略 |
| [robotd/src/main.rs](robotd/src/main.rs) 的 `apply_intent`、`dispatch`、`build_controller` | 接收控制指令、处理动作请求、构建模型控制器 |
| [robotd/src/control.rs](robotd/src/control.rs) | 动作调度以及 `Walk/Stand/SitStand/GroundPick/KickLeft/KickRight/Roulade` 的选择 |
| [duck-control/src/policy.rs](duck-control/src/policy.rs) 的 `Policy::load`、`Policy::infer` | 加载、预热和调用 ONNX 模型 |
| [duck-control/src/obs.rs](duck-control/src/obs.rs) | 把速度、头部、身体姿态等指令写入模型观测输入 |
| [deploy/robotd.toml](deploy/robotd.toml) 的 `[policy]` | 模型文件路径和启用配置 |

## 飞特分支：倾倒保护与电流保护

`robotd` 默认启用这两项保护，触发任一项会关闭全部 15 个舵机的力矩并锁定。
控制循环继续读取状态，但停止步态、归位和自动恢复；扶正或电流回落不会自动重新使能。
保护锁定时，`robot.enable` / `robot.init` 会返回拒绝原因。

### 原理与旧 Python 代码的对应关系

- **倾倒**：沿用旧 `src/moves/walk.py` 的机体系重力判断。直立时 `gravity.z ≈ -1`，
  侧躺时约为 `0`；`gravity.z > -0.5` 对应躯干偏离竖直超过约 60°。
  当前工程保留 200 ms 防抖，只用已就绪 IMU 的有效更新判断。旧 Python 在倾倒时跳过步态输出，
  这里进一步关闭全部舵机力矩，并禁止后续位置指令重新上力。
- **过流**：保留 18 A / 连续 2 拍默认值。每个 HD1910 的供电估算值为
  `有符号相电流(A) × 有符号PWM占空比 + 0.020 A`，再累加全部舵机。
  电流方向取 BIT15，PWM 方向取 BIT10，占空比幅值最多为 1；电流与 PWM 反向时，回馈项为负。
  旧算法丢弃两个方向后相乘，把回馈也累加成耗电；新算法保留方向，20 mA 静态项取自 HD1910 规格书。
  相电流使用现有的 `6.5 mA/LSB` 换算，PWM 来自同一反馈包的负载字段；不增加总线读取。
  `总估算电流 >= 阈值` 连续两次有效读取即触发，50 Hz 下约 40 ms；低于阈值或总线读取失败清零计数。
  若驱动没有供电电流估算，则退回相电流绝对值之和。这个数是舵机净供电估算，负值表示净回馈，**不是整机电池端实测电流**。
- **停机**：保护先于本拍运动指令执行；`Safety::apply` 拦截位置写入，`Safety::set_torque` 拒绝上力。
  关闭力矩时即使某个 HD1910 不应答，也继续尝试其余舵机；失败会在后续循环重试。
  这很关键：本项目 HD1910 的 `WritePosEx` 带目标力矩，保护后继续写位置可能重新上力。

### 修改保护值

本地修改 [deploy/robotd.toml](deploy/robotd.toml) 的现有 `[safety]` 段，
它是 `xh_push.sh` 上传到板端 `/etc/robot/robotd.toml` 的配置。
首次使用需要把本次编译的 `robotd` 和配置一起部署；仅更新 TOML 到旧程序不会增加保护。
沿用现有流程执行 `sh xh_push.sh --build` 可重新编译并上传；只修改阈值时无需重新编译。
Windows 上可从本工程目录执行 `wsl --exec bash xh_push.sh --build`，按提示输入板端 sudo 密码。
脚本会输出本地配置路径，部署后核对程序及配置的 SHA-256；核对成功才提示完成。
每次上传使用独立临时目录，避免旧缓存模型的权限问题阻断配置更新；校验通过后清理本次临时目录。
若 `servo-web` 正在运行，部署时会停止它，避免与 `robotd` 同时占用舵机总线。

```toml
[safety]
fall_protection = true
fall_gravity_z = -0.5
fall_debounce_ms = 200
overcurrent_protection = true
overcurrent_cutoff_a = 18.0
overcurrent_debounce_ticks = 2
```

改 `overcurrent_cutoff_a` 就是改电流阈值，单位 A。例如改成 `15.0`，会在总估算电流连续两拍
达到或超过 15 A 时关闭力矩。这个示例不是已标定的推荐值；18 A 也只是沿用你旧工程的默认值。
应结合正常行走峰值、堵转负载和实际电源/BMS 参数标定。减小连续拍数更敏感，增大会延迟响应。
阈值必须为有限正数，连续拍数至少为 1；修改配置不需要重新编译，但需要重启服务。

板端可直接编辑 `/etc/robot/robotd.toml`，然后在支撑好机器人后执行：

```bash
sudo systemctl restart robotd
sudo journalctl -u robotd -n 100 --no-pager
```

触发日志包含 `fall protection latched` 或 `overcurrent protection latched`；过流日志还记录
当时的估算电流和阈值。状态流的 `policy` 为 `protection`，`movement.limited_by` 包含
`fall_protection` 或 `overcurrent`。恢复顺序是：支撑并扶正机器人 → 排除卡滞/供电故障 →
重启 `robotd` → 用原来的使能或归位命令恢复运行。重启本身不会主动上力。
直接改板端配置后，也要同步本地 `deploy/robotd.toml`，否则下次 `xh_push.sh` 会覆盖。

### 用键盘记录电流、选择阈值

部署新版 `robotd` 和 `scripts/keyboard-control.py` 后，运行键盘控制时按 **C** 开始实时显示和
记录，再按 C 停止；Enter 使能模型前也可以开始记录。Esc/Ctrl-C 退出时保存日志并按原流程卸力。
默认保存到**运行命令所在目录**的 `logs/current-日期-时间.csv`，终端会打印绝对路径；可用
`--log-dir /home/radxa/microduck-logs` 指定位置。日志是 CSV 文本，可用 Excel 打开。

终端每秒刷新 5 次，显示保护实际比较的 `I估`、阈值、本次记录峰值及电流最大的舵机 ID。
CSV 按 `robotd` 每拍状态保存（正常约 50 Hz），包括 `input_estimate_a`、`cutoff_a`、
`protection_enabled`、`debounce_ticks`、15 路 `id_XX_phase_a`、指令速度、策略和保护原因。
`input_estimate_abs_a` 保留旧算法的结果，便于与修正后的 `input_estimate_a` 逐拍比较；保护与峰值均使用修正值。
`valid=0` 表示没有有效电流，不能当作 0 A；`missing_ticks` 是累计缺失拍数。
有漏采时可能漏掉峰值；电流超过 0.5 秒未更新会提示过期。旧版 daemon 没有电流字段，需要一起更新。
这些是总线反馈及供电估算，未包含主控等耗电，也不能代替电池端电流表；单颗相电流不可直接与整机阈值比较。

建议托住/防护好机器人，分段记录：站立 30–60 秒，低速行走、起停和转向各 30–60 秒，
再覆盖实际使用的速度、地面和电量。按 C 停止并重新开始可得到不同工况的独立文件。
只统计 `valid=1` 且动作正常时的 `input_estimate_a` 峰值，**约 1.3 倍峰值**可作为初步候选，
这是调试余量而非厂商安全额定值。例如峰值 4 A，候选约 5.2 A；还需确认电池/BMS、接插件和
线束允许的电流，并用电池端电流表核对估算误差及主控消耗。没有这些数据，不能认证 18 A 或候选值安全。
保持 `overcurrent_debounce_ticks = 2`，不要先加长防抖来掩盖起停峰值。

判断是否太低：正常动作在 `limited_by=overcurrent` 时停机，且对应电流连续达到阈值，
才需要结合电流曲线排查阈值；若是 `fall_protection` 则不是电流阈值问题。
正常峰值经常超过阈值的约 80% 说明调试余量不足，应先检查卡滞、零位和步态，确认正常后再调整。
整机保护不能保证发现单颗堵转：一颗电流异常而其他舵机很轻载时，总电流可能仍低于阈值。

你使用的 F550 电池尚无放电额定数据。索尼原版 NP-F550 说明书列出 7.2 V 标称、8.4 V 满电和
1500 mAh 容量，但未给出最大放电电流，容量也不等于可输出电流；不能仅凭 F550 型号认定支持 18 A。
参考：[索尼 NP-F550 说明书](https://pro.sony/s3/cms-static-content/operation-manual/3865315141.pdf)。

### 后期关闭或移除

在现有 `[safety]` 段把以下三项设为 `false`，重启服务即可关闭两项新保护以及旧的倾倒自动恢复：

```toml
fall_protection = false
overcurrent_protection = false
limp_fall = false
```

只关闭一项时只改对应开关；若要完全取消倾倒干预，还要关闭 `limp_fall`。
`fall_protection = true` 时旧的 `limp_fall` 序列自动停用，避免与力矩关闭冲突。
不要靠删除配置行关闭保护：缺省值会重新启用这两项。开关关闭后判断不会触发停机，
无需删除代码；底层舵机固件自身的电流/温度保护仍保留。

接手代码时从这些位置看起：

| 文件 | 负责内容 |
|---|---|
| [duck-control/src/ftbus.rs](duck-control/src/ftbus.rs) | 同拍电流估算、逐个关闭舵机力矩 |
| [duck-control/src/io.rs](duck-control/src/io.rs) | `Sensors.input_current_a` 传递估算值 |
| [duck-control/src/safety.rs](duck-control/src/safety.rs) | 电流计数、倾倒防抖、保护锁定、写指令拦截 |
| [robotd/src/main.rs](robotd/src/main.rs) | 绑定配置、执行停机、取消运动、发布保护状态 |
| [robotd-params/src/lib.rs](robotd-params/src/lib.rs) | 参数默认值与合法性检查 |
| [robotd-params/src/registry.rs](robotd-params/src/registry.rs) | 参数编辑器的字段注册 |

如果以后物理删除这两项功能，先用上述开关验证关闭后的行为，再按表中调用链移除保护参数、
状态和检查，以及 `deploy/robotd.toml` 中的新增键；保留原有目标限幅、非法数值检查和指令超时保护。
`SafetyConfig::default()` 保留库调用方的兼容行为，实际 `robotd` 默认开关来自 `SafetyParams`，
调参应改 TOML，不要只改库默认值。

这两项保护覆盖 `robotd` 的控制循环，包括经 IPC 发起的归位、保持和运动。
直接操作总线的独立 `robotd init` 子命令、舵机调试脚本和 `tools/servo-web` 不经过此保护。
软件保护依赖 IMU、总线和舵机正常响应，无法保证截住两次采样之间的电流尖峰，也不能替代 BMS。
关闭力矩后机器人会失去支撑；首次实机验证应吊挂或托住，先验证倾倒停机和恢复流程，再标定电流。

## It does things

<table>
<tr>
<td width="50%">
  <video src="https://github.com/user-attachments/assets/356a6011-8e0d-4b28-bda9-da78646583a3" controls width="100%"></video>
</td>
<td width="50%">
  <video src="https://github.com/user-attachments/assets/abfbf250-1b1c-42cb-8430-00267e2b148a" controls width="100%"></video>

</td>
</tr>
<tr>
<td><b>It walks.</b> Pick up a gamepad and drive.</td>
<td><b>It rolls.</b> Put wheels on, hold D-pad up, and it loads the other brain.</td>
</tr>
<tr>
<td width="50%">
  <video src="https://github.com/user-attachments/assets/7e70c1da-e120-428f-ae0b-f4de62f25984" controls width="100%"></video>
</td>
<td width="50%">
  <video src="https://github.com/user-attachments/assets/3eef63a5-6f84-47cf-90de-e717e6d7f8f0" controls width="100%"></video>
</td>
</tr>
<tr>
<td><b>It picks things up.</b> Beak to the floor, one button.</td>
<td><b>It gets back up.</b> Knock it over and it stands itself up.</td>
</tr>
</table>

It also sits, kicks a ball, rolls forward on command, and quacks in a voice that is its own.

## Where to find things

### You have a duck

| | |
|---|---|
| [Cheat sheet](docs/robot/cheatsheet.md) | Every `robotctl` command: drive, configure, voice, chorale, theremin, wifi, updates, logs. Start here. |
| [Gamepad](docs/robot/cheatsheet.md#gamepad-configd) | The full button mapping, and pairing a pad — [once per pad](docs/robot/pair-a-gamepad.md), plus what to do when it will not bond. |
| [`duckctl`](docs/robot/duckctl.md) | The robot from a laptop over Bluetooth, with no network and no ssh. |
| [Updates](docs/robot/cheatsheet.md#updates-updaterd) | Install, roll back, pin. Every update is verified, health-gated and reversible. |

### You are building on it

| | |
|---|---|
| [Onboarding](docs/onboarding.md) | New here? The path from "clone it" to "watch your code run on a duck": build, fake mode, dev board, dev push, release. |
| [microduck_rl](https://github.com/pollen-robotics/microduck_rl) | Where the policies come from: MuJoCo, PPO, domain randomisation, and the ONNX export this repo loads. |
| [How it works](docs/design/architecture.md) | The whole system on one page — the daemons, the bus, how an update reaches a robot — then a page per part. |
| [Set up a dev board](docs/robot/install-dev.md) | From a blank board to a robot that takes branch builds. |
| [Dev cheat sheet](docs/robot/cheatsheet-dev.md) | Branch builds, release candidates, driving from a laptop, and the restart traps after an update. |
| [Push your branch](docs/robot/dev-push.md) | Build on your machine, install over ssh, about a minute. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Building, testing, layout, conventions, releasing. |
| [Docs index](docs/README.md) | Everything, including the design pages and the open problems. |

## Under the hood

Rust, no framework, one workspace. `robotd` owns the control loop and the motor bus; `updaterd`
installs signed releases and rolls them back when a robot comes up unhealthy; `configd` owns wifi
and identity; `btd` is the Bluetooth path a phone uses; `padd` reads the gamepad; `mediad` streams
the camera over WebRTC; `tofd` serves the depth sensor. They talk over one JSON-RPC contract on
Unix sockets, and every client — the app, the console, the gamepad, your script — sends exactly the
same calls.

The interesting decisions are written down: [`docs/design/`](docs/design/) is why things are the
way they are, and [`docs/project/`](docs/project/) is what has gone wrong and what would close it.

## A note on ducks

No duck was harmed in the making of this robot. Several were consulted.
