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

The upload script sends `robotd`, `policies/*.onnx`, the keyboard controller, and `deploy/robotd.toml`, verifies the binary and configuration using SHA-256, and restarts `robotd`.

**The upload overwrites `/etc/robot/robotd.toml` on the board.** Copy any board-side tuning back into the local configuration first. Use the same commands for subsequent binary or model updates.

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
sudo systemctl start robotd
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py
```

- **Enter**: enable the policy.
- **W / S**: increase / decrease forward velocity; velocity persists after release. Press **Space** to set it to zero.
- **A / D**: move laterally; **Q / E**: turn.
- **T**: toggle sit / stand; **C**: start / stop current logging to CSV.
- **Esc / Ctrl-C**: exit and release torque. With the default connection, this also stops `robotd`; support the robot before exiting.

### Web debugging and protection settings

[servo-web](tools/servo-web/README.md) provides servo debugging tools. Starting the service stops `robotd`, `padd`, `mediad`, and `tofd`. After debugging, support the robot and restore gamepad control:

```bash
sudo systemctl stop servo-web
sudo systemctl start robotd padd
```

The runtime includes latched torque-off protection for falls and overcurrent. However, both `fall_protection` and `overcurrent_protection` are currently **`false`** in [deploy/robotd.toml](deploy/robotd.toml). These settings are separate from the `limp_fall` gain-reduction behavior.

When latched protection is enabled and triggered, servo torque is disabled. Support and straighten the robot, resolve the fault, then restart `robotd`. Overcurrent detection uses an estimate of servo supply current; calibrate its threshold for the actual power supply and load. Standalone servo debugging tools do not pass through this protection in `robotd`.

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
