# 独立语音工程 · dzh_voice

语音模型/依赖在此目录维护，运动模型仍在policies。默认不运行，不加入robotd或systemd自启动。
识别已在旧工程由用户实测；本次master适配尚需板端复验。

- `prepare.py`：下载官方sherpa-onnx KWS、生成关键词及SHA清单；已有正确模型不重下载。
- `recognize.py`：纯识别，不连接robotd，不使能舵机。
- `keyboard_input.py`：可选输入适配器，无socket、无RPC；由master键盘主循环调用。
- `catalog.json`：可维护的模型注册表。模型在`models/<id>`，独立`.venv`，不提交模型/虚拟环境。
- `sync.sh`：单独上传语音发布目录，不重启服务、不自动切换当前版本。

仅白名单：“鸭子前进”“鸭子停止”“鸭子坐下”“鸭子起来”。不是任意中文ASR。
修改关键词需重新运行prepare，检查`models/<id>/keywords.txt`及manifest；不要只改显示文案。
Python支持3.11/3.12/3.13，NumPy按解释器版本固定，sherpa-onnx仍1.13.8；保持现有版本规则。

```bash
# 本地Linux/WSL首次准备；Windows也可用独立Python3.12环境运行prepare
sh speech/setup.sh
speech/.venv/bin/python speech/recognize.py --check
sh speech/sync.sh radxa@192.168.2.70
```

板端先进入上传输出的`~/microduck-speech/releases/speech-时间戳-编号`，运行`sh setup.sh`、
`.venv/bin/python recognize.py --check`和`.venv/bin/python recognize.py --seconds 30`。
完整部署/安全测试/版本切换和回退见[ACTIONS.md](ACTIONS.md)与[分支部署文档](../docs/dzh_voice_迁移部署与测试.md)。

默认纯键盘：

```bash
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py
```

按需开启，仍人工Enter起身：

```bash
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py --speech on \
  --speech-dir /home/radxa/microduck-speech/current
```

`--speech preview`只识别打印到状态栏，不发送语音动作；`--speech off`完全不加载识别模块、不启动子进程、
不访问麦克风，与master键盘路径一致。`--walk-voice`/H是播放大笑，与本开关无关。

旧dzh的`control.py/robot_bridge.py`没有迁入：它们是独立运动控制器，不适合与新master键盘并行。
本版本只运行一个键盘控制进程，不得另开旧语音控制器、手柄padd或浏览器动作控制抢写速度。
摄像头/声音/独立嘴部已在新master，未使用旧文件覆盖。

语音前进固定0.10m/s、最长3秒；停止清三轴，坐前零速等待至少2秒。键盘运动/姿态输入接管会取消语音待坐与行走租约；
C/L/H日志和发声键不取消语音限时。语音不能使能/重启/关机/切模式或触发其它技能。
音频心跳0.8秒/状态0.5秒过期、识别退出会停止其拥有的行走，但不干预之后接管的手动指令。
起立、切网、自动恢复与卸力严格走master：语音不另设动作完成时长、不重试toggle。
默认键盘退出会卸力并停止robotd，退出前托住机器人。

如果麦克风忙，用`arecord -l`核对设备，先关闭占用该输入的客户端；本脚本不自动停止mediad等服务。
模型manifest是本地完整性校验，不宣称上游独立签名验证。需要离线准备可用`prepare.py --archive 官方包.tar.bz2`。
