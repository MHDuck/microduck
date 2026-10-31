# 网页端控制完整指南

适用于本仓库 master 的 duck console，更新于 2026-10-31。当前机器人地址为 `192.168.3.127`；换网络后请使用新的 IP。

## 1. 打开网页并连接

1. 电脑或手机与机器人连接同一局域网。
2. 使用 Edge / Chrome 打开 `http://192.168.3.127:8080/`。更新程序后先按 **Ctrl+F5** 强制刷新。
3. 点击 **connect**，等待顶部同时显示 **connected** 和 **control open**。
4. 确认 TELEMETRY 持续更新，HEALTH 正常，IMU 就绪。
5. 给机器人留出起身空间，准备好支撑，再点击 **enable · 起身** 或 **init · 起身**。
6. 等待“起身完成，可以行走”，再操作方向键或屏幕摇杆。需要取消并卸力时按 **relax**。

**无需运行 `sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py`。**
网页自身就是控制客户端；该脚本是另一套键盘客户端，同时运行可能产生相互覆盖的指令。
网页由 `mediad` 提供，执行动作的是 `robotd`。

## 2. 为什么网页能打开、连接后却不能控制

链路分为三层，前一层成功不代表后一层成功：

```text
浏览器 -- HTTP :8080 --> mediad 提供网页
浏览器 -- WebSocket :8443 --> 信令服务：发现 producer、交换 SDP/ICE
浏览器 == WebRTC control DataChannel ==> mediad --> /run/robotd.sock --> robotd --> 舵机
```

| 网页显示 | 含义与处理 |
|---|---|
| `no producer` | 信令服务在线，但未发现媒体生产者；检查 mediad 启动日志，等其就绪后 disconnect / connect |
| `no control channel` | 尚未建立命令通道，按钮无法把请求送到 robotd |
| `connected`，但没有 `control open` | WebRTC 已连接，命令通道还未打开；展开 CONSOLE 查看错误 |
| `control open`，HEALTH 异常 | 命令通路已通，需要查看 robotd、IMU、舵机通信或模型错误 |
| `control open`，POLICY 为 `protection` | 机器人处于保护锁定；单纯扶正不会清除已有锁定 |
| 点击起身后一直没有 `standup` | 请求可能已排队，但控制循环随后因传感器、模型或关机状态拒绝；查看 robotd 日志 |

最初截图中的 `no producer` / `no control channel` 表示当时还没有可用的控制通道。
后续出现 `connected` 仍应继续确认 `control open`、HEALTH 和 POLICY。

## 3. enable / init 的起身行为

这两个网页按钮执行相同的流程：检查 `robot.health` 和 `robot.subscribe` 返回的 StandUp 模型，清零旧运动指令，再发送一次显式起身请求。

```json
{"jsonrpc":"2.0","id":101,"method":"robot.enable","params":{"on":true,"toggle":false,"stand_up":true,"ignore_protection":true}}
```

这是会让机器人运动的请求。一般直接使用按钮即可；需要调试协议时，可把完整 JSON 放入网页底部 **CONSOLE → raw** 输入框，点击 **send**。

本次请求的规则：

- 直接调用配置的 `policy.stand` 模型；不先经过 HOME 或 SitStand，不需要重启 robotd。
- 清除已有的倒地／软件过流锁定，在本次恢复过程中暂缓倒地锁定和软件过流检测。
- **最长 12 秒**。有效 IMU 样本满足重力 `z < -0.9`、角速度模长 `< 1 rad/s`，累计稳定达到 500 ms 后判定完成。
- 完成、取消或超时后恢复原有保护配置。原来关闭的保护仍关闭，原来开启的保护重新执行；没有写入永久配置。
- 超时未完成会结束豁免、锁定保护并请求卸力。**relax** 会取消起身并请求卸力。
- 起身期间速度归零，网页暂不发送摇杆、方向键和视线拖动指令；连续重复点击不会延长恢复时间。
- 页面关闭、断网或浏览器计时器暂停不会让豁免永久保留，12 秒期限由 robotd 控制。断网不等于立即取消；重连后检查状态。
- 起身完成后即使仍由同一 StandUp 模型保持站姿，豁免也已结束。普通零速切换到该模型不会重新开启豁免。

仅忽略本次恢复的**倒地和软件过流保护**。模型是否已加载、传感器读取、IMU 就绪、目标值有限性及关节范围检查仍生效；舵机自身的硬件保护和 EEPROM 设置没有改变。

`ignore_protection` 默认是 `false`，只有 `on=true`、`toggle=false`、`stand_up=true` 的组合才能使用它。
键盘 Enter、手柄和自动倒地恢复不会自动获得本次网页请求的豁免。
键盘 Enter 原有的重启服务流程与网页请求不同，但使用同一个 StandUp 起身模型。

注意：网页上的 **init · 起身** 是按钮名称；它现在发送的是上述 `robot.enable` 请求。协议原有的 `robot.init` 仍是原有初始化动作，不等于这个按钮。

## 4. 日常控制

| 操作 | 效果 |
|---|---|
| W / S | 前进 / 后退 |
| A / D | 左右平移 |
| Q / E | 原地转向 |
| 方向键 | 前后移动、左右转向 |
| FORWARD · STRAFE 摇杆 | 前后和横向运动 |
| TURN 摇杆 | 转向 |
| 视频画面拖动 | 请求机器人朝对应方向看 |
| enable · 起身 / init · 起身 | 单次 StandUp 起身，使用上一节的临时豁免 |
| stop | 清零运动意图，保留策略控制；不是卸力或物理急停，也不取消 StandUp |
| relax | 取消动作并请求卸力 |
| shutdown | 坐下并关机 |
| DO 下拉框 + do | 发起一次技能，需对应模型已加载 |
| SOUND 下拉框 + play | 播放声音；wheee 使用按住播放的方式 |

最大输入速度为 0.3 m/s，转速为 1.5 rad/s。持续运动指令以 10 Hz 发送；默认 500 ms 收不到新运动指令时，机器人会将速度归零。这个机制不会切断舵机电源。
在输入框内输入文本时，方向键盘控制不会接管按键。

## 5. 语音识别开关和视觉预留

连接后，在“语音与视觉”区域点击 **开启语音识别**。这是机器人上的麦克风，浏览器无需申请麦克风权限。
状态由机器人返回，依次显示“启动中”“已开启”；启动失败会显示原因。点击 **关闭语音识别** 会结束识别、录音进程，并取消仍由语音控制的运动。

- 单独开启语音不会使能电机或起身。先手动完成 StandUp，再说“前进、后退、停止、坐下、起来”。
- 前进 / 后退为 ±0.10 m/s，最多持续 3 秒；起步、换向和坐下仍需满足原有的零速等待与姿态检查。
- 网页、手柄或键盘的手动运动／姿态指令优先，接管后不会被旧语音请求覆盖。
- 识别进程退出、音频心跳中断或机器人状态过期后，不继续执行语音运动。
- robotd 重启后默认关闭语音；刷新网页、换浏览器或断开网页不会改变已经设置的语音开关，重新连接可读取当前状态。
- 不要同时运行键盘 `--speech on`：它与网页后台会争用同一个麦克风。H 行走发声和声音播放不是这个开关控制的功能。

环境仍使用 `/home/radxa/microduck-speech/current`；未安装时按钮显示不可用，需要按 [语音部署说明](../../speech/README.md) 安装。
适配器编译进 robotd，不需要启动键盘程序，也不新增常驻 systemd 服务。

### 机器人端协议

```json
{"jsonrpc":"2.0","id":401,"method":"robot.speech","params":{}}
{"jsonrpc":"2.0","id":402,"method":"robot.speech","params":{"enabled":true}}
{"jsonrpc":"2.0","id":403,"method":"robot.speech","params":{"enabled":false}}
{"jsonrpc":"2.0","id":404,"method":"robot.vision","params":{}}
```

`{}` 只查询；`enabled` 必须是布尔值。回复字段为 `accepted`、`available`、`enabled`、`state`、`reason`。
`state` 为 `off / starting / on / error / reserved`；开启请求被受理后，需继续查询直到 `on`，不能把 `starting` 当作已经录音就绪。
重复设置同一开关状态不会重复启动进程。

**视觉识别目前仅预留。** 网页按钮禁用并显示预留状态；机器人端 `robot.vision` 查询返回 `available=false`、`enabled=false`、`state=reserved`，请求开启返回 `accepted=false`。关闭请求可成功保持关闭。
该预留接口尚未控制 mediad 原有的鸭子检测管线，也不会让摄像头联动电机。

两个方法由 robotd 提供，可通过网页 WebRTC 或本机 Unix socket 调用；BLE 暂不开放。新版协议为 API v19，需同步更新 robotd 和 mediad。

## 6. 查看状态、日志和故障

展开网页底部 **CONSOLE**，可以看到请求、拒绝原因和连接日志。常用只读请求：

```json
{"jsonrpc":"2.0","id":201,"method":"robot.health","params":{}}
{"jsonrpc":"2.0","id":202,"method":"robot.subscribe","params":{"hz":2}}
{"jsonrpc":"2.0","id":203,"method":"system.info","params":{}}
```

每次发送一行，并使用不同的 id。`robot.subscribe` 的响应包含模型名称，后续 `robot.state` 通知包含策略、运动和保护状态。
`accepted=true` 代表请求已受理，不代表机器人已经站起来；应观察 `standup → stand` 和完成提示。
页面 14 秒没有收到完成状态会提示检查日志，不会自动重新发起起身。

通过 SSH 排查：

```bash
ssh radxa@192.168.3.127
systemctl is-active robotd mediad
sudo journalctl -u robotd -u mediad -n 120 --no-pager
sudo journalctl -u robotd -f
sudo ss -lntp | grep -E ':8080|:8443'
readlink -f /opt/robot/daemon/current
```

重点日志：

| 日志或提示 | 检查内容 |
|---|---|
| `stand-up recovery started`，`ignore_protection=true` | 本次请求已进入临时豁免起身 |
| `stand-up recovery ended` | 起身阶段结束，保护豁免已关闭；看 complete / cancelled 字段 |
| `stand-up timed out; cutting torque` | 12 秒内未满足站稳条件，已经请求卸力 |
| `stand-up refused` | 看 reason；请求受理后仍可能被控制循环拒绝 |
| `stand-up requires fresh sensors and a ready IMU` | 先解决 IMU 或舵机读取问题 |
| `no stand-up policy loaded` | 检查当前模式、policy.stand 路径和 ONNX 加载日志 |
| `unknown field ignore_protection` | mediad 与 robotd 版本不匹配，需要一起更新 |
| `overcurrent protection latched` | 普通控制或恢复完成后触发了软件过流；结合电流和实际负载排查 |
| `servo 15 did not answer ping` | 本机嘴部实际为 ID 34；检查是否仍在运行错误使用 ID 15 的旧 robotd |
| `no robot on the motor bus after ... attempts` | 初始化未通过；查看同一时间的 `cannot open the bus` 具体原因，不能据此认定全部舵机掉线 |

只显示旧的 enable / init 文案时，先 Ctrl+F5；仍旧则检查是否部署了新 mediad。
网页 HTML 编译进 mediad，单独修改板上的 HTML 文件不会更新服务提供的页面。

### servo-web 正常，但 robotd 找不到舵机

2026-10-31 在 `192.168.3.127` 实测：servo-web 的 `/api/info` 返回 `fake=false`，在线列表为 `10–14`、`20–24`、`30–34`；WebSocket 状态流也读到了这 15 颗舵机的位置、电压和温度，嘴部 ID 34 正常应答。故障版本的 robotd 却把嘴部 ID 写成 15，初始化逐颗 ping 时停在这里，尚未进入 StandUp。**15 是舵机数量，不是嘴部总线地址。** 修复应修改 `duck-control/src/model.rs` 的 ID 映射并重新编译 robotd，无需修改实物舵机 ID、接线或起身保护。

两个网页用途不同：`8081` 是直接操作串口的 servo-web 调试台；`8080` 是通过 robotd 控制的 duck console。板上的 `servo-web.service` 与 `robotd`、`mediad`、`padd`、`tofd` 互斥，不能同时占用硬件。使用已修复的程序切回网页控制模式：

```bash
sudo systemctl stop servo-web
sudo systemctl start robotd mediad
systemctl is-active robotd mediad
sudo journalctl -u robotd -n 40 --no-pager -o cat
```

随后打开 `http://192.168.3.127:8080/`，确认健康状态正常再扶稳起身。需要回到 `8081` 调试时，先在控制页按 **relax** 并扶稳，再运行 `sudo systemctl start servo-web`；这会停止上述互斥服务。

键盘日志中的 `[27, 91, ...]` 是 journald 对含 ANSI 颜色码日志输出的 UTF-8 字节数组，并非舵机报文。新版 `keyboard-control.py` 会先解码再显示，并正确提取缺失的舵机 ID。旧脚本可先用上面的 `journalctl ... -o cat` 查看可读原文。

## 7. WebSocket client 与协议

`mediad/webclient/index.html` 就是仓库内可直接使用的客户端。
8443 端口上的 WebSocket 只用于信令，不能把上面的 `robot.enable` JSON 直接发给它控制机器人。
第三方客户端需要完成 producer 发现、SDP/ICE 协商，并接收机器人创建的 `control` DataChannel，再在通道上发送 JSON-RPC。

对应代码：

| 文件 | 作用 |
|---|---|
| `mediad/webclient/index.html` | 页面、WebSocket 信令、WebRTC、按钮和遥测 |
| `mediad/src/web.rs` | HTTP 页面服务 |
| `mediad/src/route.rs` | WebRTC 可调用的方法 |
| `duck-ipc-proto/src/lib.rs` | JSON-RPC 参数和响应定义 |
| `robotd/src/intents.rs` | 单次起身请求及其豁免标志 |
| `robotd/src/main.rs` | 动作调度、12 秒期限、完成／取消／超时处理 |
| `duck-control/src/safety.rs` | 临时过流豁免及结束后的恢复 |

更多传输细节见 [WebRTC 协议设计](../design/remote-webrtc.md)。

## 8. 蓝牙名称和 Wi-Fi

协议支持修改机器人名称，网页 WebRTC 路由也允许 `system.setName`。例如在 raw 输入框发送：

```json
{"jsonrpc":"2.0","id":301,"method":"system.setName","params":{"name":"我的机器鸭"}}
```

`configd` 保存名称，`btd` 默认会在几秒内更新 BLE 广播名称；重新扫描蓝牙查看。
如果 btd 启动时用了 `--name` 固定名称，则该固定值优先，需要先检查启动参数。
这是机器人／BLE 广播名称，不是蓝牙手柄的名字，也不改变 IP 地址。

网页底部的 `net.connect (refused)` 是验证拒绝行为的调试按钮，**不是可用的网页配网入口**。
当前 WebRTC 允许 `net.status` / `net.scan` 查询，但拒绝 `net.connect` / `net.forget` 修改；Wi-Fi 配置应通过 BLE 客户端或机器人本机工具进行。

## 9. 编译和验证

本次参数贯穿 mediad 与 robotd，两个程序必须一同更新。交叉编译：

```bash
bash build.sh -p robotd -p mediad --offline
```

输出位于 `target/native/aarch64-unknown-linux-gnu/release/`。部署前备份当前两个二进制，在机器人停止动作后替换并重启服务，再强制刷新页面。不要通过修改全局 `fall_protection` / `overcurrent_protection` 来代替这次有期限的豁免。

自动测试：

```bash
cargo test --offline --locked -p duck-ipc-proto -p duck-control -p mediad -p robotd -p padd
python -m unittest scripts.test_web_console -v
```

浏览器测试需要 Playwright；`WEB_TEST_BROWSER` 可指定本机 Edge 路径。测试中的 RPC 全部被替换，网络请求被拦截，不会触碰真实机器人。
新增 Rust 测试覆盖旧客户端默认不开启豁免、参数组合校验、请求不会继承豁免、原有过流锁定解除、恢复结束后重新检测电流和倒地、超时锁定保留，以及关节目标检查不受影响。

### 语音／视觉开关验证记录

- 网页回归 12 项通过，包括明确发送开启／关闭值、预留视觉不可开启、语音错误状态和断线后的迟到回复。
- Linux 下语音输入与适配测试 29 项通过；机器人语音后台 5 项专项测试通过，涵盖进程启停、录音子进程退出、不使能电机、旧请求失效等。
- 整个 Rust 工作区 `cargo check --workspace --offline --locked` 通过。工作区协议／BLE／媒体测试通过；robotd 完整测试仍有下节列出的 5 项原有失败。
- robotd 和 mediad 的 aarch64 程序已编译，保存在仓库 `tmp/feature-switch-release/`，SHA-256 记录在同目录 `manifest.json`。构建基于用户提交的 master `0edf110`，包含空闲手柄不取消语音的修正；该修正的专项测试通过。该目录中的 robotd 随下述嘴部 ID 修复同步更新。
- 最初因旧 IP `172.20.10.5` 无法访问而未部署；2026-10-31 18:32 已连接新 IP `192.168.3.127`，核对板上 robotd／mediad 哈希与上述语音开关构建一致，确认程序已安装。真实麦克风与口令动作仍未验证。

### 舵机 ID 修复记录（2026-10-31）

- 修正嘴部总线 ID 为 34，同步诊断示例和本地硬件说明；61 维观测／14 维动作及嘴部索引保持原值。
- 修复键盘客户端对 journald 字节数组日志的解码和故障识别。
- 控制库测试 93 项、Linux 键盘测试 5 项通过；控制库示例编译检查、aarch64 robotd 构建通过。
- 新 robotd SHA-256：`cc5ee9037af27b45f16cdbb467205c2e0efc6cb83036335cf4aede9feaffed1e`。
- 部署包：仓库 `tmp/bus-id-repair.tar.gz`，内有程序、键盘脚本、校验文件和 `install.sh`。安装会备份旧文件，停止 servo-web，启动 robotd／mediad，不发送运动指令。
- 经用户确认，已于 2026-10-31 18:37（北京时间）部署到 `192.168.3.127`，核对板上文件 SHA-256 与本地构建一致。robotd／mediad 为 active，servo-web 为 inactive；本次没有更改服务的开机启用配置。
- 真机验证：`healthy=true`，`startup_failures=0`，`consecutive_errors=0`，控制循环约 `50.04 Hz`，IMU `ready=true`，`torque_off=true`；StandUp 模型为 `standup_hd1910_v6.onnx`。
- 独立 Edge 经网页通道验证 `connected / control open` 和上述健康信息；语音查询为 `available=true, enabled=false, state=off`，视觉为预留状态。验证结果与截图保存在 `tmp/bus-id-repair/`。
- 验证时机器人处于倒地保护锁定、卸力状态；这与先前总线初始化失败不同。没有自动发送 StandUp 或移动请求，实际起身需在现场扶稳后通过网页按钮验证。
- 回退备份分别位于当前 release 的 `bin/robotd.bus-id-backup-20261031-183755` 与 `scripts/keyboard-control.py.bus-id-backup-20261031-183755`。旧 robotd 仍使用错误的嘴部 ID 15，回退会恢复该故障。

### StandUp 验证与部署记录（语音开关改动之前）

- 浏览器回归：8 项通过。
- duck-control：93 项通过；duck-ipc-proto：51 项通过；mediad：42 项通过；padd：6 项通过。
- robotd：101 项通过、5 项失败。将修改前的 `d1e9e7d` 放入独立工作区复测，得到 96 项通过、同样 5 项失败，确认这 5 项不是本次修改引入：
  - `a_restart_asks_for_no_torque`
  - `robot_init_powers_the_joints_and_ramps_home`
  - `robot_relax_cuts_power_and_returns_to_limp`
  - `the_loop_holds_the_pose_it_started_in`
  - `the_loop_waits_for_the_bus_rather_than_giving_up`
- 两个 aarch64 发布程序编译成功；本次变更涉及的 Rust 代码通过格式检查。
- 在线部署前读取到 robotd 健康、IMU 就绪、`robot.safeToRestart.safe=true`，StandUp 文件为 `standup_hd1910_v6.onnx`。
- 网络恢复后，已于 2026-10-31 14:53（北京时间）部署 robotd 和 mediad，重启并确认两个服务正常，恢复了原本运行的 padd / platform-control。
- 独立 Edge 验证 `connected` / `control open`、健康正常、IMU 就绪；发送 `on=false, stand_up=false, ignore_protection=true` 得到预期的 `ignore_protection requires stand_up=true` 拒绝，确认新版参数经过网页通道到达 robotd。该验证没有触发舵机动作，实际起身仍需在现场点击按钮验证。
- 页面仍可能提示 API v18 / v17 不同：`hello` 由此次未更新的 updaterd 回答，不能据此判断 robotd 是旧版；以上实际参数校验已经确认 robotd 支持本次新增字段。

部署目录：`/opt/robot/daemon/releases/0.10.0-dev.local.1791447162.g04b1c00/bin/`。
目录名是既有安装目录，不能用它判断此次二进制是否更新；本次文件 SHA-256：

| 程序 | SHA-256 |
|---|---|
| robotd | `f89a7ac2692fd9c1942fa1254982c283719bf12735c59f0eaa0e7f07f5d26c45` |
| mediad | `2ad895b90818001f62f6bf2b040dd9e0efe48eeb9e43a8feefeee7ffb5b8ba22` |

同目录下保留了本次更新前的备份：

```text
robotd.web-standup-override-backup-20261031-145339
mediad.web-standup-override-backup-20261031-145339
```

需要回退时，在机器人停止动作后同时恢复这两个备份并重启对应服务，随后 Ctrl+F5 刷新网页。
