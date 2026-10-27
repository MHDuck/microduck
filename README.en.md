# Microduck · HD1910 + BNO085

**A reinforcement-learning runtime for a small biped robot, adapted for Feetech HD1910 servos and an I²C BNO085 IMU.**

[简体中文](README.md) | **English**

[Hardware](#2-hardware-and-interfaces) · [Policies and training](#3-policies-and-training) · [Build and deploy](#4-build-and-deploy) · [Operation](#5-operation) · [Documentation](#6-repository-layout-and-documentation) · [Acknowledgments](#7-project-origins-and-acknowledgments)

## 1. Overview

This project is a fork of [Pollen Robotics / microduck](https://github.com/pollen-robotics/microduck). It adapts the upstream Rust runtime for **Feetech HD1910 servos** and replaces the IMU interface with a **BNO085 connected over I²C (IIC)**.

This project uses a **Radxa ZERO 3W with 2 GB RAM, no eMMC, and hardware revision v1.1.2**, running **Armbian 26.8.1 Minimal** from a microSD card. ONNX policies drive the robot's joints at a default control rate of 50 Hz. This repository contains motion control, hardware drivers, gamepad input, command-line tools, a web-based servo debugger, and deployment scripts.

| Area | This fork |
| --- | --- |
| Servos | 15 Feetech HD1910 servos with a Feetech bus driver |
| IMU | BNO085 orientation and angular velocity over a separate I²C bus |
| Motion control | Rust control loop and ONNX policy inference |
| Controls | Bluetooth gamepad, board-side CLI, interactive keyboard control over SSH |
| Debugging | Web-based servo tools, state monitoring, and current logging |
| Training workflow | Train and export ONNX in a separate training repository, then deploy here |

This repository focuses on hardware operation and deployment. Its companion training repository is [MHDuck/microduck_rl](https://github.com/MHDuck/microduck_rl). Available motions depend on the deployed policies and configuration.

## 2. Hardware and interfaces

The table below lists this project's hardware and OS. Interface settings are defined in [deploy/robotd.toml](deploy/robotd.toml). Wiring and device paths must match the actual robot.

| Hardware / parameter | Current setting |
| --- | --- |
| Controller | Radxa ZERO 3W / RK3566 |
| Memory | 2 GB |
| Board hardware revision | v1.1.2 |
| Storage and boot | No eMMC; TF (microSD) card |
| Board OS | Armbian 26.8.1 Minimal / ARM64 Linux |
| Platform | `platform.variant = "open_microduck"` |
| Servos | Feetech HD1910 × 15 |
| Servo driver | `bus.driver = "hd1910"` |
| Servo port | `/dev/ttyS4`, 1 Mbps |
| IMU driver | `imu.driver = "bno085"` |
| IMU bus | `/dev/i2c-3` |
| IMU address | `0x4B`; the driver accepts `0x4A` or `0x4B` |
| Control rate | 50 Hz |

> [!IMPORTANT]
> The **B1 bootloader + B1 DTB + Armbian 6.1.115 kernel** procedure described in [fanhao375/microduck-replica](https://github.com/fanhao375/microduck-replica) does not apply to this project's hardware and OS combination. Follow the Armbian 26.8.1 Minimal deployment steps below; this setup does not require that B1 bootloader and device-tree replacement.

The BNO085 driver currently assumes sensor **+X up, +Y left, and +Z backward**. A different mounting orientation requires an adjustment to the [coordinate transform](robotd/src/bno085.rs), followed by verification of the gravity vector with the robot upright.

### Servo IDs

The [joint mapping](duck-control/src/model.rs) defines the IDs:

| IDs | Joints, in the listed ID order |
| --- | --- |
| 10 / 11 / 12 / 13 / 14 | Right hip yaw / hip roll / hip pitch / knee / ankle |
| 20 / 21 / 22 / 23 / 24 | Left hip yaw / hip roll / hip pitch / knee / ankle |
| 30 / 31 / 32 / 33 | Neck pitch / head pitch / head yaw / head roll |
| 34 | Mouth |

ONNX policies control 14 joints; the mouth is controlled separately. Policy array order is not servo ID order. Verify IDs, zero positions, joint directions, and the training home pose before deployment.

## 3. Policies and training

The companion training repository for this project is **[MHDuck/microduck_rl](https://github.com/MHDuck/microduck_rl)**. See that repository for environment setup, policy training, and ONNX export instructions.

The training project originates from [pollen-robotics/microduck_rl](https://github.com/pollen-robotics/microduck_rl), with adaptation references from [LuwuDynamics/xgoduck_rl](https://github.com/LuwuDynamics/xgoduck_rl).

The runtime expects this model interface:

```text
obs [1, 61] → ONNX policy → actions [1, 14]
```

Observation order, joint order, home pose, action scaling, and actuator parameters must also match training. Matching tensor dimensions alone does not make a policy trained for another hardware platform compatible.

The current [policy configuration](deploy/robotd.toml) references:

| Setting | Model / value | Purpose |
| --- | --- | --- |
| `walk` | `walk_hd1910_flat_v5.onnx` | Walking |
| `sitstand` | `sitstand_hd1910.onnx` | Sitting down / standing up |
| `stand` | `"none"` | No separate standing policy enabled |
| `ground_pick`, `kick_left`, `kick_right`, `roulade` | `"none"` | Corresponding skill policies disabled |

> [!IMPORTANT]
> The default policy paths and bundled files are not fully aligned yet: the walking and sit/stand files named above are missing, and the packaging script also references the missing `policies/kp30.onnx`. Supply the required files or update both the policy paths and packaging list before deployment. Other ONNX files in the repository are not substituted automatically.

Models live in [policies/](policies/). Configure their board-side paths in the `[policy]` section of `/etc/robot/robotd.toml`. See [policies/README.md](policies/README.md) for provenance and [scripts/dev-push.sh](scripts/dev-push.sh) for the packaging list. Restart `robotd` after changing policy paths.

## 4. Build and deploy

### 4.1 First boot on the board

Flash **Armbian 26.8.1 Minimal** to a microSD card, insert it into the board, and power on. Follow the first-boot prompts to set the **username, password, and Wi-Fi details**. Confirm that the PC can reach the board over SSH and that the account has sudo privileges.

The examples below use `radxa@192.168.2.90`. Replace this with the username created during first boot and the board's actual IP address.

### 4.2 Prepare Ubuntu under WSL on the PC

In **Ubuntu under WSL** on the PC, configure a domestic APT mirror using the [Tsinghua Ubuntu mirror guide](https://mirrors.tuna.tsinghua.edu.cn/help/ubuntu/). Select the configuration matching the Ubuntu version installed in WSL.

Install the basic tools in the WSL terminal:

```bash
sudo apt update
sudo apt install git curl npm nodejs gcc-aarch64-linux-gnu g++-aarch64-linux-gnu
```

Clone or download this repository and enter its root directory. Use the existing setup script to install Rust, the Python build environment, `cargo-zigbuild`, and Zig, then load the Rust environment and ensure the ARM64 cross-compilation target is installed:

```bash
bash setup-build.sh
source "$HOME/.cargo/env"
rustup target add aarch64-unknown-linux-gnu
```

Rust 1.89 or newer is required. `setup-build.sh` already uses domestic Rust mirrors and the Tsinghua PyPI mirror. See [CONTRIBUTING.md](CONTRIBUTING.md) for full workspace build instructions.

### 4.3 Deploy system services

Prepare the model files and check the serial port, IMU, and policy settings in [deploy/robotd.toml](deploy/robotd.toml). From the repository root in **Ubuntu under WSL on the PC**, run:

```bash
bash scripts/provision-board.sh \
  --pause-btd-on-pair \
  --name Ducky \
  radxa@192.168.2.90
```

The script builds and packages services from the current checkout, configures the board environment and systemd services, and may reboot the board. `--pause-btd-on-pair` configures gamepad pairing compatibility by pausing `btd` during pairing; `--name Ducky` sets the robot's name. The script prepares signing keys for local builds and keeps the private key outside the repository.

Additional packages or system libraries may be needed during installation. You can share the specific error with an AI assistant to identify and install missing dependencies, then continue deployment. The [board installation guide](docs/robot/install-dev.md) provides more context; for this fork, follow the commands in this section and the current scripts.

### 4.4 Upload models and binaries

After system service deployment, run in **Ubuntu under WSL on the PC**:

```bash
bash build.sh
sh xh_push.sh radxa@192.168.2.90
```

The upload entry point is [xh_push.sh](xh_push.sh) in the repository root. `bash build.sh` produces `target/native/aarch64-unknown-linux-gnu/release/robotd`. If that binary is already up to date, run the upload command directly.

The upload script sends `robotd`, `policies/*.onnx`, the control and diagnostic scripts, and `deploy/robotd.toml`. It verifies the daemon binary, keyboard script, and configuration using SHA-256, then reports the deployment result. It also restarts `robotd`. Without `--build`, `xh_push.sh` does not compile automatically: run `bash build.sh` after Rust source changes. Changes limited to the Python control interface do not require rebuilding `robotd`.

**The upload overwrites `/etc/robot/robotd.toml` on the board.** Copy any board-side tuning back into the local configuration first. Use the same commands for subsequent binary or model updates.

Support the robot and exit the old keycontrol process before uploading; launch the keyboard script again afterward. Restarting `robotd` does not reload an already running Python process.

### 4.5 Protect the boot configuration on the microSD card

During this project's microSD-based operation, power loss has been followed by corruption of `/boot/armbianEnv.txt`, preventing the system from booting. **After deploying system services and confirming that the board boots normally**, run on the board:

```bash
sudo chattr +i /boot/armbianEnv.txt
lsattr /boot/armbianEnv.txt
```

Check that the attributes include `i`. This makes the file immutable, preventing modification or deletion; see the [chattr manual](https://man7.org/linux/man-pages/man1/chattr.1.html). It prevents accidental writes but cannot guarantee protection against microSD corruption during unexpected power loss. Run `sudo poweroff` and wait for the board to shut down before disconnecting power.

Before changing boot parameters or device-tree settings, or performing system maintenance that updates this file, remove the protection on the board:

```bash
sudo chattr -i /boot/armbianEnv.txt
```

After maintenance, run `sudo chattr +i /boot/armbianEnv.txt` again to restore protection. Initial provisioning needs to write the boot configuration, so set this attribute after deployment completes.

### 4.6 Check runtime status

After installation or an update, check on the board:

```bash
robotctl health
robotctl monitor
journalctl -u robotd -b -n 80 --no-pager
```

## 5. Operation

Support the robot before first enabling motion, and verify joint zeros, IMU orientation, and policy configuration.

### Bluetooth gamepad

Put the gamepad in pairing mode and run on the board:

```bash
sudo robotctl pad pair
robotctl pad status
```

After pairing, `padd` reads gamepad input and `robotd` controls motion. Common controls use Xbox button names:

| Control | Function |
| --- | --- |
| Start | Enable / disable policy execution; disabling a policy does not disable servo torque |
| Left stick | Forward, backward, and lateral movement |
| Right stick, horizontal | Turn |
| Y | Toggle head control mode |
| B | Toggle body pose mode |
| D-pad down | Sit / stand; requires a `sitstand` policy |
| RT / LT | Mouth and sound control |

A mapped button does not imply that its policy is configured. See the [gamepad guide](docs/robot/pair-a-gamepad.md) for pairing and troubleshooting.

### Keyboard control over SSH

Run in an interactive terminal on the board:

```bash
sudo systemctl stop padd
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py
```

Every accepted **Enter** press runs `systemctl restart robotd`, reconnects, waits for the models and IMU to be ready, then invokes the StandUp model configured by `policy.stand`, currently `standup_hd1910_v6.onnx`. This also works when the service is stopped, walking cannot recover from a fall, or fall/overcurrent protection has disabled the servos: resolve the fault and provide space for the robot to get up before retrying. Enter restarts the service even if the robot is already standing.

Recovery runs StandUp directly, without a HOME or SitStand transition. Velocity commands are zeroed during recovery. Fall latching and predictive fall gain reduction are temporarily suspended; overcurrent protection still follows its configured setting. Completion requires a ready IMU, successful reads, and fresh samples with gravity `z < -0.9` and angular-speed magnitude `< 1 rad/s`, accumulated for 500 ms. Repeated samples pause the timer; a fresh unstable sample or an IMU that is no longer ready resets it. Joint angles do not have to match HOME. The keyboard controller reports completion after observing the policy transition from `standup` to `stand`.

If StandUp has not completed within 12 seconds, the daemon requests torque release. Enter can retry after the fault is resolved. When upgrading from an older version, deploy both the matching `robotd` binary and keyboard script to support the StandUp request and live protection status. Later changes limited to the interface require only a script update. A custom `--socket` requests StandUp without restarting or stopping the default systemd service.

The main interface displays **one short status or fault**, with no appended velocity, IMU readings, gravity, timers, keyboard-enabled flag, or voice-enabled flag. Messages currently appear in Chinese, for example `站立中` (standing), `行走中` (walking), `倾倒中` (tilting), `倒地中` (fallen), and `倾倒保护了` (fall protection triggered). Faults include `22 电机无响应` (motor 22 is not responding), `BNO085 连接异常` (BNO085 connection fault), `ONNX 文件缺失` (missing ONNX file), and `配置文件错误` (configuration error). A motor ID is shown only when the reported error identifies it.

Faults take priority and are cleared after successful Enter recovery. If communication fails, the keyboard controller stops sending motion commands and stays open for recovery. A separate read-only connection subscribes to `robot.state` at 10 Hz and requests `robot.health` once per second, so status updates do not depend on logs or on pressing C. Disconnection or a state stream that has not updated for more than one second produces a short warning; the subscription reconnects automatically.

**C** opens detailed diagnostics: commanded velocity, actual policy, IMU readiness/freshness, gravity z, fall settings and timers, current readings, and raw errors. It also replays the most recent 100 diagnostic messages. C remains available when `robotd` is disconnected. With a working control connection, opening diagnostics also starts current CSV recording; pressing C again closes diagnostics and ends recording. C does not change motion commands: use Space to stop.

CSV files default to `logs/` under the working directory; use `--log-dir` to change the destination. They contain current samples, policy, commanded velocity, and related fields, not the raw error messages shown in the terminal. Enter ends the current CSV recording before restarting the service. If diagnostics remain open after recovery, press C to close them, then C again to start a new recording. With the default socket, a journal follower reads `robotd` service errors and exits with the keyboard program. Custom sockets receive live state but do not follow the default service journal.

After uploading an updated script, exit and relaunch keycontrol. If the main interface still shows the old combined line containing `IMU新`, `z=...`, or `键盘开`, check that the new script was uploaded and the old Python process has exited. `xh_push.sh` verifies the installed keyboard script against the local file.

| Keyboard | Gamepad equivalent | Function |
| --- | --- | --- |
| Enter | — | Restart `robotd`, run StandUp, then allow walking after completion |
| W / S, A / D | Left stick | Adjust forward / lateral velocity in drive mode by 0.10 m/s per press, within ±0.40 m/s; velocity persists after release |
| Q / E | Right stick, horizontal | Turn; yaw command returns to zero about 0.3 seconds after release |
| T | D-pad down | Toggle sit / stand after enabling control; zero velocity first; no joint-position log dump |
| Y / B | Y / B | Toggle head / body pose mode; press the same key again to return to drive mode; switching zeroes velocity |
| G | A | Ground pickup; keyboard A remains a movement key |
| Z / V | LB / RB | Left / right kick |
| X | X | Forward roll; hold to repeat |
| R / F | RT / LT | Open the mouth; R triggers a chirp, F holds the `wheee` sound; release to close |
| Hold M for 3 seconds | Hold D-pad up | Request walking / roller mode switch; movement key behavior stays the same |
| Hold P for 2 seconds | Hold Select | Request sit and shutdown; release torque directly if sitting is unavailable; support the robot first |
| Space | — | Zero velocity and pose targets, and release mouth, sound, and repeated-roll inputs |
| C | — | Open / close detailed diagnostics and start / stop current CSV recording when connected |
| H | — | Toggle voice playback while walking; manual R/F sound input pauses automatic voice playback |
| Esc / Ctrl-C | — | Exit and release torque; the default connection also stops `robotd`; support the robot before exiting |

In head mode, W/S adjusts head pitch, A/D head yaw, I/K neck pitch, and J/L head roll, in 0.10 rad steps within ±2.5 rad. In body pose mode, W/S adjusts height in 0.001 m steps within −0.025 to +0.010 m; I/K adjusts pitch and J/L roll in 0.02 rad steps within ±0.2618 rad. Pose targets persist after release; Space resets them. These modes do not send walking velocity commands.

Terminals do not report key releases, so the script estimates holds and releases from key repeats. R/F/X release after about 0.3 seconds. Leave more than one second between separate presses of Enter, Y/B, T, G, Z/V, C, and H. M/P trigger only once per hold. Missing action models produce a short refusal while keeping the session open; press C for the reason.

T uses `policy.sitstand`, currently `sitstand_v3.onnx`. It does not restart `robotd` and does not replace Enter for getting up after a fall. Stopping `padd` before keyboard control prevents competing commands; restore gamepad control with `sudo systemctl start robotd padd`.

Use `--walk-voice` to enable voice playback while walking at startup. `--voice-sound-only` disables mouth animation for automatic voice playback; manual R/F still controls the mouth. Standalone voice, camera diagnostics, and StandUp testing are documented in [local customizations (Chinese)](docs/LOCAL_CUSTOMIZATIONS.md).

### Web debugging and protection settings

[servo-web](tools/servo-web/README.md) provides servo debugging tools. Starting the service stops `robotd`, `padd`, `mediad`, and `tofd`. After debugging, support the robot and restore gamepad control:

```bash
sudo systemctl stop servo-web
sudo systemctl start robotd padd
```

The current [deploy/robotd.toml](deploy/robotd.toml) sets `fall_protection = true`, `fall_gravity_z = -0.5`, and `fall_debounce_ms = 5000`; `overcurrent_protection = false`. On an installed robot, `/etc/robot/robotd.toml` is authoritative. Press C to inspect the active fall settings and timer.

| Mechanism | Detection and response |
| --- | --- |
| Latched fall protection | With the IMU ready and providing fresh samples, trunk gravity `z > fall_gravity_z` accumulates time until `fall_debounce_ms` is reached. This confirms a fall; when `fall_protection` is enabled, it latches protection and requests torque release throughout the robot. Returning upright does not clear the latch |
| Predictive fall gain reduction | Runs only when `fall_protection = false`, `limp_fall = true`, and the controller is in an eligible state. It predicts continued tipping from IMU gravity and angular velocity, lowers gains, then returns the joints to HOME after landing or a timeout before handing control back to the policy. This is not a torque-off latch |
| Latched overcurrent protection | When `overcurrent_protection` is enabled, estimated servo supply current must exceed the cutoff for the configured number of consecutive ticks before protection latches and requests torque release. The current settings are 18 A and 2 ticks, but the feature is switched off |

A fresh sample within the normal angle range clears the fall timer, as does an IMU that is no longer ready. Repeated samples pause accumulation. The configured five seconds therefore means accumulated time from valid samples; repeated or missing samples can make the actual wait longer. The short status is `倾倒中` while timing, `倒地中` for a confirmed fall without a latch, and `倾倒保护了` once fall protection latches.

To set fall confirmation to five or ten seconds, edit this value in the existing `[safety]` section rather than adding a duplicate section:

```toml
fall_debounce_ms = 5000  # Five seconds; use 10000 for ten seconds.
```

After synchronizing the configuration to the board, resolve faults and provide space for the robot to get up, then press Enter to restart `robotd` and run StandUp. The new configuration takes effect on restart. When deploying with `xh_push.sh`, update the local `deploy/robotd.toml` first so the upload does not overwrite board-side tuning.

Predictive gain reduction uses separate settings: by default, the robot must already be tilted past `z > -0.90`, still be tipping, and be predicted to pass `z > -0.5` within 300 ms. The condition must hold for 60 ms before triggering. `limp_fall_debounce_ms` controls this prediction confirmation time; `limp_fall_max_ms` limits how long the robot remains at reduced gains, defaulting to 1500 ms. Neither is the fall-latch timer `fall_debounce_ms`. Since latched fall protection is currently enabled, this predictive sequence does not run.

Calibrate the overcurrent threshold for the actual power supply and load. Standalone servo debugging tools do not pass through these protections in `robotd`.

## 6. Repository layout and documentation

| Path | Contents |
| --- | --- |
| [robotd/](robotd/) | Motion daemon, skill dispatch, and BNO085 integration |
| [duck-control/](duck-control/) | Servo bus, observation construction, ONNX inference, and protection logic |
| [robotd-params/](robotd-params/) | Runtime parameters and configuration parsing |
| [padd/](padd/), [robotctl/](robotctl/), [duckctl/](duckctl/) | Gamepad service, board-side CLI, and desktop client |
| [configd/](configd/), [btd/](btd/), [mediad/](mediad/), [tof/](tof/) | Networking and pairing, BLE, audio/video, and ToF services |
| [policies/](policies/) | ONNX policies and provenance notes |
| [deploy/](deploy/), [scripts/](scripts/) | Board configuration, installation, and deployment scripts |
| [tools/servo-web/](tools/servo-web/) | Web-based servo debugging |
| [docs/](docs/) | Operation, architecture, and development documentation |

Useful links: [Getting started (Chinese)](Microduck入门指南.md) · [Command reference](docs/robot/cheatsheet.md) · [Architecture](docs/design/architecture.md) · [Development guide](CONTRIBUTING.md) · [Documentation index](docs/README.md).

Some documents retain upstream platform assumptions and example parameters. For this fork, refer to the current source and `deploy/robotd.toml`; on an installed robot, use `/etc/robot/robotd.toml`.

## 7. Project origins and acknowledgments

Thank you to the authors and contributors of the following projects:

| Project | Relationship |
| --- | --- |
| [pollen-robotics/microduck](https://github.com/pollen-robotics/microduck) | Upstream repository and foundation of this runtime fork |
| [fanhao375/microduck](https://github.com/fanhao375/microduck) | Reference for hardware and runtime adaptation |
| [LuwuDynamics/xgoduck_runtime_arduino](https://github.com/LuwuDynamics/xgoduck_runtime_arduino) | Reference for runtime adaptation |
| [pollen-robotics/microduck_rl](https://github.com/pollen-robotics/microduck_rl) | Origin of the training project |
| [LuwuDynamics/xgoduck_rl](https://github.com/LuwuDynamics/xgoduck_rl) | Reference for training adaptation |

## 8. Contributing and licensing

Issues and pull requests are welcome. For hardware reports, include the board and OS versions, servo and IMU models, relevant configuration, policy filenames, and reproduction steps. See [CONTRIBUTING.md](CONTRIBUTING.md) for development conventions.

The root retains the upstream [Apache License 2.0](LICENSE). Some files carry separate license declarations: the [BNO085 driver](robotd/src/bno085.rs) and [web debugger sensor module](tools/servo-web/board_sensors.py) are marked **GPL-3.0-or-later**. See [NOTICE-board.md](tools/servo-web/NOTICE-board.md) for attribution and the license copy.

Consult the notices for individual files, third-party components, and models as well; the root LICENSE should not be treated as a single license covering all content. This README does not change existing copyright or license declarations.
