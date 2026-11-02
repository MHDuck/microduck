# 视觉功能一键部署与使用

适用：Radxa Zero 3W 的 Armbian 系统，电脑使用 Ubuntu / WSL。本仓库源码、运动模型与校准要求见[主部署说明](../README.md#4-编译与部署)。

## 1. 安装或更新

在电脑的仓库根目录运行，下例 IP 换成实际板子地址：

```bash
bash scripts/provision-board.sh radxa@192.168.3.130
```

这一条命令包含主程序、模型、视觉依赖、相机追加 180° 修正和 `vision-follow` 开机服务。OpenCV / NumPy 在电脑下载并缓存，板端离线安装。已有相机方向配置和内参文件保留。

**会覆盖板端 `/etc/robot/robotd.toml` 并重启主程序。** 调好的参数先同步回仓库的 `deploy/robotd.toml`。

看到最终 **provisioning finished** 和 **vision is ready** 才算完成。中断后重新运行同一条命令即可继续；不需要登录板子补装依赖。

## 2. 日常测试

视觉服务已随系统启动，电脑直接打开 `http://板子IP:8091/`。按[视觉注视与跟随快速测试](视觉注视与跟随_独立模块测试.md)依次测试识别、原地注视、前后跟随。

不要重复上传模块、安装环境或另开 `serve.py`。只有网页打不开、服务异常时，才需要排错或重启服务。

## 3. 必要排错

| 现象 | 处理 |
| --- | --- |
| Address already in use | 8091 已被服务占用，直接使用网页；不要再手动启动 Python 服务。 |
| 网页打不开 | 先确认板子 IP；在板端查看下方服务状态和日志。服务异常时执行 `sudo systemctl restart vision-follow`。 |
| OpenCV 下载连接中断 | 电脑端会自动重试；仍失败时重跑第 1 节命令，复用缓存。 |
| bad unit file setting | 更新电脑端脚本，重跑第 1 节命令，自动覆盖旧服务配置。 |
| 能识别但不跟随 | 看网页拒绝原因，确认机器人站稳，选择跟随模式并设置距离基准。 |
| 画面上下颠倒 | 查看[相机方向设置](相机方向修正_180度.md)，修改配置后重启 mediad。 |

仅在排错时，于板端执行：

```bash
sudo systemctl status vision-follow mediad --no-pager
sudo journalctl -u vision-follow -u mediad -n 50 --no-pager
```

## 4. 可选：米制距离

默认使用图像相对大小，不需要内参。需要按米控制距离时，再按 [ArUco 打印与标定说明](ArUco_DICT_5X5_50_部署与测距.md)采集照片、标定并用尺子验证。
相机、裁切或实际像素方向改变后，需要重新标定。
