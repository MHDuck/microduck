# 独立语音工程 · dzh_voice

语音模型/依赖在此目录维护，运动模型仍在policies。默认不运行，不加入robotd或systemd自启动。
识别已在旧工程由用户实测；本次master适配尚需板端复验。

- `prepare.py`：下载官方sherpa-onnx KWS、生成关键词及SHA清单；已有正确模型不重下载。
- `recognize.py`：纯识别，不连接robotd，不使能舵机。
- `keyboard_input.py`：可选输入适配器，无socket、无RPC；由master键盘主循环调用。
- `catalog.json`：可维护的模型注册表。模型在`models/<id>`，独立`.venv`，不提交模型/虚拟环境。
- `sync.sh`：默认仅上传语音发布目录；加 `--install` 自动安装板端依赖并在检查通过后切换 `current`，不启动识别或重启 robotd。

仅白名单：“前进”“后退”“停止”“坐下”“起来”。不是任意中文ASR。
短词比原来的带前缀口令更容易被日常对话误触发，先用`--speech preview`测试；默认仍为off。
修改关键词需重新运行prepare，检查`models/<id>/keywords.txt`及manifest；不要只改显示文案。
Python支持3.11/3.12/3.13，NumPy按解释器版本固定，sherpa-onnx仍1.13.8；保持现有版本规则。

完整新板部署直接使用根目录的 `bash scripts/provision-board.sh --pause-btd-on-pair --name Ducky radxa@radxa.local`，已包含本节的准备、同步、音频库安装和模型检查。Ubuntu 主机不兼容的系统 Python 会由主入口自动补充独立 Python 3.12；`setup.sh` 复用已有兼容环境，也可用 `PYTHON=/path/to/python3.12` 指定解释器。

```bash
# 本地Linux/WSL首次准备；Windows也可用独立Python3.12环境运行prepare
sh speech/setup.sh
speech/.venv/bin/python speech/recognize.py --check
sh speech/sync.sh radxa@192.168.2.70
```

只更新语音并自动安装、切换版本时使用 `sh speech/sync.sh --install radxa@192.168.1.198`。安装失败保留旧 `current`；成功只代表依赖与模型加载通过，麦克风测试仍需手动运行。上传包不含电脑的 `.venv`；若本地有 `dist/wheels-cp313-aarch64/*.whl` 完整缓存会一起传送，由板端 pip 选择兼容轮子。

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

`--speech preview`逐条打印识别结果和“仅预览”决策，不发送语音动作；`--speech off`完全不加载识别模块、不启动子进程、
不访问麦克风，与master键盘路径一致。`--walk-voice`/H是播放大笑，与本开关无关。

旧dzh的`control.py/robot_bridge.py`没有迁入：它们是独立运动控制器，不适合与新master键盘并行。
本版本只运行一个键盘控制进程，不得另开旧语音控制器、手柄padd或浏览器动作控制抢写速度。
摄像头/声音/独立嘴部已在新master，未使用旧文件覆盖。

语音前进固定+0.10m/s、后退-0.10m/s，均最长3秒；换向先停止并等待至少2秒。
停止清三轴，坐前零速等待至少2秒。键盘运动/姿态输入接管会取消语音待坐与行走租约；
C/L/H日志和发声键不取消语音限时。语音不能使能/重启/关机/切模式或触发其它技能。
音频心跳0.8秒/状态0.5秒过期、识别退出会停止其拥有的行走，但不干预之后接管的手动指令。
起立、切网、自动恢复与卸力严格走master：语音不另设动作完成时长、不重试toggle。
默认键盘退出会卸力并停止robotd，退出前托住机器人。

### 识别成功但接键盘后看不到/动作很少（2026-10-29修复）

旧界面只把识别词放在状态栏右侧，既会被终端宽度裁掉，又会被随后的心跳覆盖；动作门控拒绝也无原因。
现在每次口令输出`[语音识别]`和`[语音决策]`，预览/未使能/零速等待/姿态或状态过期等均明确显示。
心跳不再覆盖最近3秒识别词；过期/重复/键盘优先丢弃也输出，不把拒绝执行说成“没识别”。
启动打印解析后的speech目录、Python、音频设备、gain和threshold，检查是否与独立测试使用同一发布目录/环境。
两种入口默认都为ALSA同设备、gain=1、threshold=.35；不因“动作少”自动降低阈值或取消安全检查。
可用`--speech-gain 1 --speech-threshold .35`与独立`recognize.py --gain 1 --threshold .35`保持一致。
积压管道按有界批次排空，但Enter重启/起身期间的过期口令仍丢弃，完成后重新说，不自动执行旧口令。
打印“提交速度/请求”只表示发给master，不代表实机已完成动作；拒绝/保护仍以daemon反馈为准。

如果麦克风忙，用`arecord -l`核对设备，先关闭占用该输入的客户端；本脚本不自动停止mediad等服务。
模型manifest是本地完整性校验，不宣称上游独立签名验证。需要离线准备可用`prepare.py --archive 官方包.tar.bz2`。
