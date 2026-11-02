# 独立视觉注视与前后跟随

[视觉模块部署与操作测试](../docs/视觉功能_全新WSL与板端部署.md) ·
[日常测试](../docs/视觉注视与跟随_独立模块测试.md) ·
[ArUco / 测距](../docs/ArUco_DICT_5X5_50_部署与测距.md) ·
[相机方向](../docs/相机方向修正_180度.md)

复用 mediad WebRTC 视频与控制接口，不另开摄像头、不直接写舵机。
默认只预览，人工起身/启动控制后才动作；浏览器保持前台，与键盘/语音/手柄择一使用。

## 已部署主程序后的快速启动

`scripts/provision-board.sh` 一键部署已安装本模块和独立 OpenCV 环境，`vision-follow.service` 开机启动，直接打开 `http://板子IP:8091/` 即可。以下手动同步或启动前，先在板端 `sudo systemctl stop vision-follow`；没有此服务时跳过。退出手动运行后，`sudo systemctl start vision-follow` 恢复默认服务。

WSL 仓库根目录：

```bash
sh vision_follow/push.sh radxa@192.168.2.70
```

板端首次安装：

```bash
cd "$HOME/microduck-vision"
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
.venv/bin/python serve.py --detector aruco --marker-id 0 --marker-mm 50
```

访问 http://板子IP:8091/。DICT_5X5_50、ID0、黑色外边 50mm；SVG 含白边为 70mm。
先视频、识别预览，再人工起身/注视；相对大小跟随先设置“当前距离”。
预览开关只影响非控制阶段，不用勾选也能启动控制。停止 / 空格 / Esc 停控，不卸力。
只做前后跟随，不自动转身、横移或避障。

默认 0.5Hz、640 边长、JPEG 0.7；结果预算 5 秒、HTTP 8 秒、目标保留 12 秒、视频无新帧 10 秒。
停走式：零速应答 → 稳定 1 秒 → 新图两帧 → 移动最多 2.5 秒 → 再停稳识别。
前进上限 0.12、后退 0.10m/s。停走式默认关闭头部/目标正前方门控，--follow-require-front 可恢复；
目标/画面失效及状态、IMU、总线、连接保护仍停止，旧图不能反复触发下一段。

有同一 raw 视频的内参时加 --calibration camera.json，可选米制跟随；
无标定用注视/相对大小，米制 pose 无效不自动降级。
原鸭子检测通知模式不需要 OpenCV，使用连续式：

```bash
python3 "$HOME/microduck-vision/serve.py" --detector duck --follow-style continuous
```

服务参数 --bind 0.0.0.0 --port 8091 --signalling-port 8443；
可在电脑运行并加 --robot-host 板子IP，浏览器访问电脑 localhost:8091。完整参数见 serve.py --help。

## 更新与检查

push.sh 不覆盖主配置/模型、板端 .venv 或内参；先停旧服务，上传后重启、Ctrl-F5 重连。
stand_up 字段需 mediad、robotd 都支持；master 的 xh_push 会同步 mediad。
下载 JSONL 日志后再刷新，不保存视频。离线检查需已安装 OpenCV 的 Python 和 Node：

```bash
node --test vision_follow/test_core.cjs vision_follow/test_follow_cycle.cjs \
  vision_follow/test_aruco_client.cjs vision_follow/test_app.cjs
python3 -m unittest discover -s vision_follow -p 'test_*.py'
```
