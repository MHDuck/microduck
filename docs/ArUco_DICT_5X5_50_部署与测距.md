# ArUco DICT_5X5_50：打印、测距与跟随

首次安装见[全新 WSL 与板端部署](视觉功能_全新WSL与板端部署.md)。

## 目标和打印

字典 DICT_5X5_50，默认 ID0（可选 0～49），黑色正方形外边 **50mm**。
[附带 SVG](../vision_follow/aruco_5x5_50_id0_50mm.svg)含白边整图 70mm，按 100% 打印、实测黑色外边。
贴硬平面、避免反光，只放一个相同 ID。重新生成：

```bash
cd "$HOME/microduck-vision"
.venv/bin/python aruco_tools.py marker --id 0 --marker-mm 50 --output marker.svg
.venv/bin/python aruco_tools.py board --cols 9 --rows 6 --square-mm 20 --output board.svg
```

## 距离模式

```bash
cd "$HOME/microduck-vision"
.venv/bin/python serve.py --detector aruco --marker-id 0 --marker-mm 50
```

无内参可识别、注视；相对大小跟随先在期望位置点击“将当前距离设为跟随距离”。
框变小前进、变大后退；这是相对控制，不是可信米数。

米制模式需要同一相机、原始像素方向/裁切的 pinhole 内参。用附带
[9×6 内角点、20mm 格长棋盘](../vision_follow/chessboard_9x6_20mm.svg)采集 20～30 张 raw 照片，至少 12 张有效：

```bash
.venv/bin/python aruco_tools.py calibrate \
  --images calibration-images --cols 9 --rows 6 --square-mm 20 --output camera.json
.venv/bin/python serve.py --detector aruco --marker-id 0 --marker-mm 50 --calibration camera.json
```

RMS>2px 拒绝；成功后仍需尺子验证多个距离。输出是**相机光心到码中心**，不是机身安全间隔。
验证后在网页选择米制跟随、设置目标距离。完整照片采集与传输见[主指南第 11 节](视觉功能_全新WSL与板端部署.md#11-米制距离和相机标定可选)。

## 有效性和控制

四角点在 raw 坐标执行 IPPE_SQUARE，角点再依据 media.video.rotate 转到显示坐标，不重复旋转。
内参包含 coordinate_space=raw、camera_model=pinhole、image_width、image_height、camera_matrix、dist_coeffs；
工具自动生成。需要正深度、正确朝向、边长至少 20px、重投影误差不超过 3px。
裁切/比例不匹配/无效 pose 时停平移，不自动降级为相对大小。更换相机、裁切或实际像素旋转须重标定。

默认 0.5Hz、结果预算 5 秒、HTTP 8 秒、目标保留 12 秒，两帧新图确认。
停走式每段最多 2.5 秒、停稳 1 秒重新识别，移动中不上传、旧图不反复触发。
默认不要求目标或头部朝前才走，可用 --follow-require-front 恢复该限制。
健康/IMU/总线/连接保护独立保留。完整按钮测试、日志与排错见[主指南](视觉功能_全新WSL与板端部署.md)。
