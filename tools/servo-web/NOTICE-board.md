# 板端硬件代码与资源

`board_sensors.py` 的 BNO08x 协议读取参考 Microduck `src/imu_reader.py`
（Copyright 2026 Marc Duclusaud，SPDX: GPL-3.0-or-later），并沿用当前工程
`robotd/src/bno085.rs` 的安装方向。许可证副本见 `licenses/microduck-GPL-3.0.txt`。

LSM6DSV16X、VL53L5CX 的初始化与换算复用用户提供的板端项目
`src/lsm6dsv16x_monitor.py`、`src/vl53l5cx_monitor.py`。后者使用安装依赖中的
VL53L5CX 1.0.1 驱动及固件，不在本工程重写底层驱动。

`assets/hello_loud.wav` 复制自同一板端项目，生成与处理流程见其
`docs/i2s3-audio-handoff.md`。录音处理同样使用该文档中已验证的 SoX 参数。

`vendor/three.module.js` 和 `vendor/OrbitControls.js` 为 Three.js 0.160.0
原始发行文件，来自 jsDelivr npm 镜像，许可证见 `vendor/three-LICENSE.txt`。
模型来源与许可见 `model/NOTICE.md`。

原调试台可选的 STM32 总线 IMU / SWD 功能仍保留。其固件校验用 HEX 原样复制到
`assets/imu_to_dxl.hex`，`imu200.py` 保留原有总线模拟工具，避免搬迁后依赖父仓库路径。
板端 BNO085 I²C 模式不使用此固件，也不会给 STM32 烧录。
