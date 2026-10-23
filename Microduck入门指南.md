# Microduck 入门指南

适用对象：已收到完成系统安装、机器人程序部署和出厂配置的 Microduck 的客户。

本文带你完成：连接热点 → SSH 登录 → 可选修改 Wi-Fi → 键盘控制 → 退出和关机。

项目仓库：

- **运行与部署仓库**：[MHDuck/microduck](https://github.com/MHDuck/microduck)，包含机器人运行程序、硬件驱动、部署脚本和使用文档。
- **模型训练仓库**：[MHDuck/microduck_rl](https://github.com/MHDuck/microduck_rl)，用于运动策略训练与 ONNX 模型导出。

> **关于示例回复：**本文中的终端输出均为示例，不是客户设备的实测记录。键盘提示依据编写时的项目脚本整理；设备上的程序版本、系统语言、IP 地址和提示文字可能不同。热点与登录密码采用交付信息，不是 Armbian 通用默认值。请勿把示例输出当作需要输入的命令。

## 0. 使用前准备

需要具备 Windows 和 Linux 命令行的基础操作能力，包括打开终端、输入命令、查看输出和使用 SSH 登录。

准备好手机、电脑、Microduck 和配套电池。先在平整、防滑、远离桌沿和台阶的地面上操作。上电、启用模型、重启控制服务和退出控制前，都要扶稳或托住机身，手指避开运动关节。

本文的命令分为两类：

- **电脑端命令**：在 Windows CMD、PowerShell 或 Linux 终端中执行，例如 `ssh radxa@radxa.local`。
- **机器人端命令**：SSH 登录成功后执行，例如 `sudo systemctl restart robotd`。不要直接在尚未登录的 Windows 终端输入这些命令。

带 `PS C:\Users\Customer>` 或 `radxa@radxa:~$` 的代码块展示完整终端交互。这些是提示符，不需要输入；只输入提示符后面的命令。

## 1. 用手机建立默认 Wi-Fi 热点

打开手机的“个人热点”或“便携式热点”，设置：

| 项目 | 内容 |
| --- | --- |
| 热点名称（SSID） | `duck`，全部小写 |
| 热点密码 | `1234567890` |

热点名称和密码必须完全一致，不要多输空格。保持热点开启；如果手机有“无人连接时自动关闭热点”设置，首次连接时关闭该选项。

按本次交付配置，Microduck 开机后会尝试自动连接 `duck`。若始终连接不上，可将手机热点切换到 2.4 GHz 或兼容模式后再试。

## 2. 给 Microduck 上电，电脑连接同一个热点

1. 扶稳 Microduck，按交付时说明接好配套电池并上电。
2. 将电脑的 Wi-Fi 也连接到 `duck`，密码同样是 `1234567890`。
3. 等待约 1 分钟，让机器人完成系统启动和 Wi-Fi 连接。若尚未出现，可以再等 1～2 分钟。
4. 在手机热点的已连接设备列表中确认电脑和机器人已连接；设备名称可能显示为 `radxa`，也可能只显示 IP 或其他名称。

电脑和机器人必须处在同一局域网内。仅进行 SSH 和键盘控制不要求能够访问互联网，但热点必须允许连接设备之间互相通信。

## 3. 通过 SSH 登录 Microduck

### 3.1 使用 CMD、PowerShell 或 Linux 终端

在**电脑端**输入：

```bash
ssh radxa@radxa.local
```

首次连接时可能显示主机身份确认。以下是**示例交互**，方括号中的内容是说明或占位符：

```text
PS C:\Users\Customer> ssh radxa@radxa.local
The authenticity of host 'radxa.local ([设备实际IP])' can't be established.
ED25519 key fingerprint is SHA256:[设备实际指纹].
Are you sure you want to continue connecting (yes/no/[fingerprint])? yes
Warning: Permanently added 'radxa.local' (ED25519) to the list of known hosts.
radxa@radxa.local's password:
[系统欢迎信息省略]
radxa@radxa:~$
```

确认连接的是本次交付的机器人后，输入 `yes` 并回车。在密码提示处：

**按 3 次空格键，再按回车。密码是连续三个半角空格，不是数字 `3`，也不是文字“三个空格”。**

输入密码时终端通常不会显示字符或星号，这是正常现象。出现类似 `radxa@radxa:~$` 的提示符，表示已经进入机器人。

后面的 `sudo` 命令如果提示 `[sudo] password for radxa:`，在尚未修改账户密码的情况下，也输入这三个空格；如果没有询问密码，直接继续即可。

### 3.2 使用 PuTTY

PuTTY 是独立 SSH 客户端：

1. 打开 PuTTY，在 **Host Name** 中填写 `radxa.local`。
2. **Port** 填 `22`，**Connection type** 选择 `SSH`，点击 **Open**。
3. 首次出现主机密钥提示时，确认是本次交付设备后接受。
4. `login as:` 后输入 `radxa`。
5. 密码输入连续三个空格，再回车。

### 3.3 如果 `radxa.local` 无法连接

若出现 `Could not resolve hostname radxa.local`，到手机热点的已连接设备信息中查看机器人的实际 IP，改为按 IP 登录。例如，**仅当实际 IP 确实是 `192.168.43.120` 时**：

```bash
ssh radxa@192.168.43.120
```

PuTTY 也可以把 Host Name 改成该实际 IP。本文所有 IP 均为示例，不要直接认定设备使用这个地址。

如果使用实际 IP 仍然超时，确认电脑和机器人都连着 `duck`，并检查热点是否限制设备互访。需要时换一部手机建立同名、同密码热点；不要让两个同名热点同时开启。

## 4. 修改机器人保存的 Wi-Fi（可选）

第一次使用可以跳过本节，保持连接 `duck`，直接进入第 5 节。

本节修改的是**机器人连接哪个 Wi-Fi**。更换后，电脑也要连接新的 Wi-Fi，才能继续 SSH 控制。不要先修改手机上现有 `duck` 热点的名称或密码，否则可能先失去机器人连接。

Armbian 从 24.05 起使用 Netplan 描述网络配置；官方 Minimal 镜像通常使用 `systemd-networkd`，CLI/桌面镜像通常使用 NetworkManager。本项目的部署流程还可能迁移网络后端，因此应以设备实际状态为准。[Armbian 官方网络说明](https://docs.armbian.com/user-guide/networking/)

**准备工作：**先确认新 Wi-Fi 的名称和密码，并确保它允许设备互访。扶稳机器人；如果正在键盘控制，先按空格停止移动，托住机身后按 Esc 退出。然后在机器人端执行：

```bash
sudo systemctl stop robotd
```

通常没有输出，返回提示符即可。配置网络期间保持机器人处于有支撑的静止状态。

### 4.1 判断使用哪一种配置方式

在机器人端执行：

```bash
nmcli device status
```

NetworkManager 管理 Wi-Fi 时的**示例输出**：

```text
DEVICE  TYPE      STATE      CONNECTION
wlan0   wifi      connected  duck
lo      loopback  connected  lo
```

若 Wi-Fi 这一行显示 `connected`，按 **4.2** 操作。无线接口名可能不是 `wlan0`，后续请使用实际名称；`CONNECTION` 是连接配置的名称，也可能与热点名称不同。

若提示 `nmcli: command not found`、`NetworkManager is not running`，或 Wi-Fi 显示 `unmanaged`，再执行：

```bash
systemctl is-active systemd-networkd
```

如果输出 `active`，按 **4.3** 操作。两个服务可能同时运行，不要仅凭服务是否启动就判断谁管理 Wi-Fi；如无法判断，保留 `duck`，把这些命令的实际输出交给售后确认。

### 4.2 NetworkManager：用命令连接新 Wi-Fi

以下以无线接口 `wlan0`、新 Wi-Fi 名称 `CustomerWiFi` 为例。请替换为现场实际值。

先扫描：

```bash
sudo nmcli device wifi list ifname wlan0
```

**示例输出，省略部分列：**

```text
IN-USE  SSID          SIGNAL  SECURITY
*       duck          85      WPA2
        CustomerWiFi  78      WPA2
```

再连接新网络，按提示输入**新 Wi-Fi 的密码**：

```bash
sudo nmcli --ask device wifi connect "CustomerWiFi" ifname wlan0 name "microduck-client-wifi"
```

**示例回复：**

```text
Password:
Device 'wlan0' successfully activated with '[连接UUID]'.
```

这里的 `Password:` 是 Wi-Fi 密码；如果此前先出现 `[sudo] password for radxa:`，那一处才是机器人账户密码。`--ask` 会交互询问密码，不必把 Wi-Fi 密码写进命令。[NetworkManager 命令说明](https://networkmanager.dev/docs/api/latest/nmcli.html)

**切换网络可能使当前 SSH 立即断开，因此不一定能看到成功回复。**将电脑连接到 `CustomerWiFi`，等机器人完成连接后重新执行：

```bash
ssh radxa@radxa.local
```

如果主机名失效，改用新路由器或热点设备列表中的机器人实际 IP。登录后先执行 `nmcli device status`，确认无线接口已连接到 `microduck-client-wifi`，再设置保存的连接自动启用：

```bash
sudo nmcli connection modify "microduck-client-wifi" connection.autoconnect yes connection.autoconnect-priority 100
```

该命令通常没有输出。优先级用于自动选择连接，不会立即切换当前网络。保留原有 `duck` 配置，暂时不要删除，便于现场恢复连接。

如果偏好菜单操作，在确认 Wi-Fi 由 NetworkManager 管理后，也可以用 `sudo nmtui`，进入 **Activate a connection（激活连接）**，选择新 Wi-Fi 并输入密码。网络切换后同样需要重新连接 SSH。

### 4.3 Netplan / systemd-networkd：使用新版 Armbian 配置工具

先备份现有 Netplan 配置。在机器人端执行：

```bash
sudo cp -a /etc/netplan "/root/netplan-backup-$(date +%Y%m%d-%H%M%S)"
```

正常情况下没有输出，备份保存在机器人 `/root/` 目录下；这份备份不会自动恢复网络。如果提示 `/etc/netplan` 不存在，先停止修改并联系售后确认网络方式。

新版 Armbian 官方工具提供网络设置入口：

```bash
sudo armbian-config --cmd BNS001
```

该命令通常打开交互菜单，**不是打印一行固定的成功提示**。按屏幕指引完成：

1. 选择无线接口，例如 `wlan0`。
2. 选择目标 Wi-Fi。
3. 输入新 Wi-Fi 密码。
4. IP 获取方式选择 **DHCP（自动获取）**。
5. 确认并应用。

使用方向键选择、Tab 切换按钮、Enter 确认；菜单名称可能因工具版本和语言不同而变化。这一入口及上述流程来自 [Armbian 官方网络配置工具说明](https://docs.armbian.com/config/network/)。

如果已安装的旧版工具不支持 `--cmd BNS001`，运行 `sudo armbian-config`，在 **Network（网络）** 中寻找 Wi-Fi 设置。若提示 `armbian-config: command not found` 或菜单无法使用，先保留默认热点，将报错交给售后；不要为完成首次体验自行更换网络后端或覆盖整套 Netplan 配置。

应用时 SSH 可能断开。将电脑连到新 Wi-Fi，再按第 3 节重新登录。若始终无法连接，在新路由器和原热点的设备列表中分别查找机器人；不要假定更改失败后必定自动回到 `duck`。

### 4.4 确认新网络可用

重新登录后，在机器人端执行：

```bash
hostname -I
```

**示例输出：**

```text
192.168.1.120
```

可能显示多个地址。能在电脑连接新 Wi-Fi 后重新 SSH 登录，才说明控制连接已经恢复。机器人和电脑后续都应连接这个新网络，再进入第 5 节。

## 5. 启动 Microduck 键盘控制

### 5.1 重启控制服务

扶稳机器人，在**机器人端 SSH 终端**执行：

```bash
sudo systemctl restart robotd
```

**正常情况下没有输出，直接返回命令提示符。**不要以为没有文字回复就是没有执行。

等待数秒，再检查：

```bash
systemctl is-active robotd
```

正常运行时输出：

```text
active
```

`active` 仅表示服务进程处于运行状态，不代表电机、传感器和模型全部正常。若输出 `inactive`、`failed`，或一直处于 `activating`，先按第 7 节查看日志。

### 5.2 启动键盘控制程序

```bash
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py
```

看到类似 `托住机器人，按 Enter 启用模型。` 和 `待 Enter 启用` 后，程序正在等待按键。**此时不是 Linux 命令行，不要在这里继续粘贴其他命令。**

### 5.3 扶好鸭子，按 Enter 启用模型

扶稳机身，让双脚在平整地面上处于合适姿态，按一次 **Enter（回车）**，等待机器人启用模型并稳定。

按本项目默认行走配置，启用后由 `walk` 策略控制；如果设备配置了独立站立策略，静止时也可能使用 `stand`。界面显示 `已请求启用` 表示程序已经发出启用请求，仍需观察实际姿态后再让它行走。

**速度为零不代表电机卸力，也不保证机器人完全不踏步。**刚开始请始终在旁保护。

### 5.4 使用 W / S 调整前后速度

| 按键 | 作用 |
| --- | --- |
| `W` | 每按一次，前后指令速度 `vx` 增加 `0.10 m/s` |
| `S` | 每按一次，前后指令速度 `vx` 减少 `0.10 m/s` |
| 空格 | 所有指令速度归零，仍保持模型启用 |

从零开始轻点 W，界面依次显示：

```text
指令 vx=+0.10 m/s
指令 vx=+0.20 m/s
指令 vx=+0.30 m/s
```

这些是**指令速度**，不是实测行走速度。先从 `+0.10 m/s` 试走，稳定后再逐步调整。

**客户入门操作要求：不要超过 `0.30 m/s`；前进和倒退均按速度绝对值不超过 `0.30 m/s` 控制。地面或姿态不稳定时，应进一步降低速度。**

编写时的脚本实际允许 `-0.40～+0.40 m/s`，因此 `0.30` 不是程序自动锁住的上限。长按会连续调速，可能很快超过入门限制，请使用短按并观察数值。

还要注意：

- **松开 W / S 后，前后速度会保持，不会自动停止。**要停下请按空格。
- S 的含义是“减小有符号速度”。从 `+0.10` 按一次 S 到零，再按一次会到 `-0.10`，变成倒退。
- 想立即让指令速度归零时，优先按空格，不要反复按 S 猜测是否已经停下。

### 5.5 坐下和站起

1. 先按空格，确认界面上的 `vx`、`vy`、`vyaw` 均为零。
2. 等机器人姿态稳定，扶好机身，轻点 **T**，请求坐下。
3. 等坐下动作完成并稳定后，再轻点 **T**，请求站起。
4. 两次点按至少间隔 1 秒，并且必须等前一次动作完成；不要长按 T。

T 需要在 Enter 启用后使用。程序也会在发送坐站请求前把速度归零，但操作时仍应先停稳。

编写时的脚本会在 T 请求后打印约 5 秒关节位置，随后结束位置打印；屏幕滚动不一定是故障。`坐站请求已入队` 只表示请求已提交，不能据此判断动作已经完成。若提示坐站切换失败，先保持支撑，查看第 7 节。

### 5.6 其他按键

| 按键 | 作用 |
| --- | --- |
| `A` / `D` | 增减横移速度 `vy`，每次 0.10 m/s，范围 -0.40～+0.40 m/s，松开保持 |
| `Q` / `E` | 左右转向 |
| `C` | 开始 / 停止策略、电流显示和 CSV 记录；首次体验可忽略 |
| `Esc` / `Ctrl+C` | 退出控制，执行卸力；默认连接下同时停止 robotd 服务 |

横移速度 `vy` 与前后速度 `vx` 一样，松开保持，长按连续调速，按空格归零。转向松开后约 0.3 秒归零，**不会清除已经设置的 `vx` 和 `vy`**。初次体验建议先只使用 Enter、W、S、空格、T 和 Esc。

## 6. 退出、再次启动与关机

### 6.1 退出键盘控制

先按空格，等动作稳定，**托住机器人后**按 Esc。退出会卸力，机器人可能失去支撑，不要让它在无人扶持的站立状态下直接退出。

默认连接下，当前脚本的**示例回复**：

```text
正在停止控制并卸力……
robotd 服务已停止。再次使用前运行：sudo systemctl start robotd
radxa@radxa:~$
```

### 6.2 再次启动

仍然使用原 SSH 连接即可。每次重新开始键盘控制时，依次执行：

```bash
sudo systemctl restart robotd
```

等待数秒、确认服务正常后执行：

```bash
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py
```

扶稳机器人，再按 Enter。退出提示中的 `start robotd` 也可以启动已停止的服务；本指南统一使用 `restart robotd`，与日常操作流程一致。

### 6.3 使用完毕后关机

托住机器人并按 Esc 退出控制，安全放稳机身，然后在机器人端输入：

```bash
sudo poweroff
```

SSH 随后可能显示连接关闭，这是正常现象。连接断开本身不代表系统已经完全关机；等待设备完成关机，再按交付说明关闭电源或断开电池。

不要把直接拔电池作为日常关机方式。

## 7. 常见问题与发给售后的信息

| 现象 | 处理方法 |
| --- | --- |
| `ssh` 不是内部或外部命令 | 使用 PuTTY，按第 3.2 节登录 |
| `Could not resolve hostname radxa.local` | 到热点或路由器查看机器人实际 IP，用 IP 登录 |
| `Connection timed out` | 检查同一网络、设备供电、启动是否完成，以及热点是否允许设备互访 |
| `Connection refused` | 确认 IP 属于机器人，等待系统启动；仍失败则联系售后检查 SSH 服务 |
| `Permission denied` | 确认用户名 `radxa`，密码是连续三个半角空格；已修改密码则使用新密码 |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | 先向售后确认是否重装系统或连接到其他设备，确认身份后再处理旧记录 |
| `/run/robotd.sock` 不存在或连接失败 | 扶稳机器人，重启 robotd，等待数秒，再检查服务状态和日志 |
| 找不到 `keyboard-control.py` | 检查是否在机器人 SSH 终端、路径是否完整；联系售后核对交付版本 |
| 松开 W 后仍在前进 | 前后速度保持是当前程序的正常行为；按空格归零 |
| T 没有动作或切换失败 | 确认已按 Enter，先归零并等待上次动作完成；保存实际报错和服务日志 |
| Esc 后再次运行控制程序连接失败 | 退出会停止 robotd；重新执行第 6.2 节的两条命令 |

如果终端无响应、机器人动作异常或连接中断，先在避开关节的情况下扶住机身。能够建立新的 SSH 连接时，可在另一窗口执行：

```bash
sudo systemctl stop robotd
```

停止控制服务可能导致机器人失去支撑，执行前务必托住。若无法远程停止且持续有危险动作，按交付说明切断机器人电源。

排查控制程序时，在机器人端依次执行以下只读命令，并把**实际输出**发给售后：

```bash
systemctl status robotd --no-pager -l
sudo journalctl -u robotd -b -n 80 --no-pager
ls -l /opt/robot/daemon/current/scripts/keyboard-control.py
```

`status` 中 `Active: active (running)` 表示服务运行中；`failed` 表示失败；刚按 Esc 退出后看到 `inactive (dead)` 则符合本指南描述的退出流程。日志内容取决于现场问题，本文不编造故障日志。

Wi-Fi 问题可附上：

```bash
hostname -I
nmcli device status
systemctl is-active systemd-networkd
```

如果某条命令提示不存在，也把该提示一并发回。无需发送 Wi-Fi 密码或包含密码的网络配置文件。

## 8. 完整操作示例：命令与回复

以下假设电脑和机器人已连接 `duck`、已接受过 SSH 主机密钥、服务启动正常，展示从登录到退出的过程。**全部为示例交互，不是设备实测；`[操作：…]` 是人工按键说明，不能输入或粘贴到终端。**

```text
PS C:\Users\Customer> ssh radxa@radxa.local
radxa@radxa.local's password:
[操作：按三次空格，再回车；屏幕不会显示密码]
[系统欢迎信息省略]
radxa@radxa:~$

radxa@radxa:~$ sudo systemctl restart robotd
[sudo] password for radxa:
[操作：如需密码，按三次空格，再回车]
radxa@radxa:~$
[说明：restart 成功时通常没有输出；等待数秒后再检查]

radxa@radxa:~$ systemctl is-active robotd
active

radxa@radxa:~$ sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py
托住机器人，按 Enter 启用模型。
W/S 每次增减 vx 0.10 m/s（范围 -0.40～+0.40），松开保持；长按会连续调速。
A/D 每次增减 vy 0.10 m/s（范围 -0.40～+0.40），松开保持；长按会连续调速。
Q/E 左右转（松开约 0.3 秒停止转向）  空格速度归零  Esc/Ctrl-C 退出
T 坐下/站起切换（Enter 启用后使用，切换前速度归零；两次点按请间隔 1 秒以上）。
每次 T 后打印 5 秒关节位置（5 Hz，14 个关节，实测 / HOME / 偏差，单位 rad）。
实时显示指令速度；vx / vy 持续保持，按空格停止。
C 开始/停止实际策略、电流显示和 CSV 记录（可在启用模型之前开始）。
退出会卸力，请先托住机器人；默认连接还会停止 robotd 服务。
待 Enter 启用 | 指令 vx=+0.00 m/s  vy=+0.00 m/s  vyaw=+0.00 rad/s

[操作：扶稳机身，按 Enter，等待姿态稳定]
已请求启用 | 指令 vx=+0.00 m/s  vy=+0.00 m/s  vyaw=+0.00 rad/s

[操作：轻点一次 W]
已请求启用 | 指令 vx=+0.10 m/s  vy=+0.00 m/s  vyaw=+0.00 rad/s

[操作：稳定后，再轻点一次 W]
已请求启用 | 指令 vx=+0.20 m/s  vy=+0.00 m/s  vyaw=+0.00 rad/s

[操作：轻点一次 S]
已请求启用 | 指令 vx=+0.10 m/s  vy=+0.00 m/s  vyaw=+0.00 rad/s

[操作：按空格]
已请求启用 | 指令 vx=+0.00 m/s  vy=+0.00 m/s  vyaw=+0.00 rad/s

[操作：确认停稳并扶好，轻点一次 T 请求坐下]
坐站请求已入队；开始打印 5 秒位置，偏差 = 实测 - HOME（rad）。
[此处省略关节位置输出；实际数值和打印组数取决于设备运行情况]

[操作：等坐下完成并稳定后，再轻点一次 T 请求站起，两次点按间隔大于 1 秒]
坐站请求已入队；开始打印 5 秒位置，偏差 = 实测 - HOME（rad）。
[此处省略关节位置输出]

[操作：动作完成后按空格，托住机器人，再按 Esc]
正在停止控制并卸力……
robotd 服务已停止。再次使用前运行：sudo systemctl start robotd
radxa@radxa:~$
```

实际界面中的速度状态通常在同一行刷新，上面为了说明按键效果将它拆成多行。程序提示的 `±0.40` 是脚本范围，**客户操作仍不得超过 `±0.30 m/s`，首次从 `+0.10 m/s` 开始。**

需要再次控制时：

```text
radxa@radxa:~$ sudo systemctl restart robotd
radxa@radxa:~$
[操作：等待数秒]
radxa@radxa:~$ sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py
托住机器人，按 Enter 启用模型。
[其余提示同上；扶稳机身后再按 Enter]
```
