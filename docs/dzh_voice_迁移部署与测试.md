# dzh_voice迁移、部署与测试（2026-10-29）

## 2 语音独立同步（不与运动policies混放）

若本地语音models已经准备完整，直接同步，不需重新下载：

```bash
cd /mnt/f/Code/microduck_code/microduck_rust
python3 speech/prepare.py
sh speech/sync.sh radxa@192.168.2.70


如果`previous`已是实体目录同样先人工处理；不要删除原版本或其虚拟环境。
有麦克风设备变化时传`--speech-device`；`arecord -l`可只读查看。

## 3 纯键盘（默认关闭语音）

以下在板端。先停其它控制客户端；如padd正在控制，托住机器人再停止它。不要同时运行两个键盘进程。

```bash
sudo systemctl stop padd
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py --speech off
# 等价于不写 --speech；不要求安装speech，不访问麦克风
```

人工按Enter：按master方式重启robotd并用StandUp起身。确认站稳后W/S、A/D、Q/E三轴调速、松手保持、空格停车；
T坐下，再T起来，观察master衔接。C详细日志、L运行日志。不要用旧dzh的独立standup-test替代此次日常流程。
退出前托住机器人，Esc会按master卸力/停止robotd。此阶段对着麦克风说任何话均不得响应。

## 4 键盘 + 识别预览（先不开语音动作）

```bash
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py --speech preview \
  --speech-dir /home/radxa/microduck-speech/current
```

说“前进、后退、停止、坐下、起来”，状态栏应显示识别结果；**仅说话不应产生任何动作**。
键盘仍可人工Enter及正常控制。确认识别失败/麦克风缺失时仅提示降级，不影响纯键盘。
这里的preview不会禁止人工键盘动作，只禁止语音驱动。

## 5 键盘 + 语音动作

```bash
sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py --speech on \
  --speech-dir /home/radxa/microduck-speech/current
```

仍必须人工Enter起身；语音不支持使能/服务重启。确认站稳、保持零速至少2秒：

| 输入 | 预期 |
|---|---|
| 前进 | 三轴设为(0.10,0,0)，最多3秒自动停车；再次从静止启动仍等2秒 |
| 后退 | 三轴设为(-0.10,0,0)，最多3秒自动停车；换向先停止并等2秒 |
| 停止 | 清三轴；也可停止键盘输入的速度，不取消已开始的技能 |
| 坐下 | 先停车，零速至少2秒，调用一次与T相同的sit_toggle |
| 起来 | 当前policy=sit才调用一次sit_toggle；已经站立不重复切换 |
| 键盘运动/姿态/安全键 | 立即接管、取消语音待坐和行走租约；之后手动速度按master锁存 |
| C/L/H | 日志/播放开关不取消语音行走3秒限制 |

输入适配不判断机械动作已经成功；policy标签只是策略状态。输入去抖不替代master的动作衔接/恢复逻辑。
语音前进只在drive模式和新鲜直立状态开放；头/身体模式、起身恢复、保护或断流时不接受新动作。
识别子进程退出/音频心跳过期/状态过期时，语音拥有的速度归零，保留键盘可用，不自动重连或使能。
本程序关闭语音不需回滚模型：退出托住机器人，重开`--speech off`即可。

不要同时开启`--walk-voice`测试识别率，扬声器声音可能串入麦克风；它是独立播放功能，不是识别开关。

## 6 回归测试（软件）

WSL中只运行假socket/伪终端、合成识别消息，不连接板子：

```bash
cd /mnt/f/Code/microduck_code/microduck_rust
python3 -m unittest scripts/test_keyboard_control.py scripts/test_duck_voice.py \
  scripts/test_walking_voice.py scripts/test_camera_detect.py scripts/test_standup_test.py
python3 -m unittest discover -s speech -p 'test_*.py'
cargo fmt --all --check
cargo check --locked --offline -p robotd
cargo test --locked --offline -p robotd-params -p duck-control -p robotd
```

本机若缺rustfmt需`rustup component add rustfmt`后再检查。没有格式工具不算格式通过。
Windows的旧StandUp路径断言有POSIX路径差异，以Linux板端/WSL测试为准，不改master测试迁就Windows。
真实识别模型测试需要speech环境依赖；系统Python无NumPy会跳过部分数值测试，不等于已验证识别率。

实机至少检查：off纯键盘、preview不驱动、on未Enter不驱动、前进3秒停车、空格与语音停止、坐起幂等、
键盘接管取消待坐、识别进程退出后停车、恢复过程不插入语音动作、退出无残留识别/arecord进程。
托住机器人或先悬空低风险验证断流；不要拔控制板电源模拟错误。

## 7 保存新分支与以后合入master

本次迁移会保存为本地dzh_voice提交，未推送远端。首次发布这个分支只需人工执行
`git push -u origin dzh_voice`。以后新增改动时，确认diff和测试后：

```bash
git branch --show-current  # 必须dzh_voice
git diff --stat
git add AGENTS.md README.md docs/LOCAL_CUSTOMIZATIONS.md \
  docs/dzh_voice_迁移部署与测试.md scripts/keyboard-control.py speech
git commit -m "feat: optional speech input on master keyboard control"
git push -u origin dzh_voice
```

推荐在Gitee创建`dzh_voice -> master`合并请求，测试通过后人工合并。不推送dzh，不强推master。
master之后有新提交时，只在保存干净的dzh_voice执行`git fetch origin && git merge origin/master`，
保留master新衔接并重测，不做整文件旧版本覆盖。
需要直接更新远端master时，先确认工作区干净、fetch且已合入最新master、完成审查，再人工执行
`git push origin dzh_voice:master`；若非快进让Git拒绝，不用`--force`。本轮没有执行任何远端push或修改master。

## 本轮本机验证结果

### 接键盘后无识别输出/似乎成功率低：日志修复

已确认此前状态栏右侧被裁切、心跳覆盖识别词，且门控拒绝未说明原因。独立测试成功率高来自用户实测；
尚无板端日志证明两种入口的底层KWS识别率真的不同，不盲改模型/阈值/保护。
新版逐条输出`[语音识别] 前进 intent=walk age=...`和`[语音决策] 前进 -> ...；原因`。
preview无需Enter即可看识别；on仍必须人工Enter使能、站稳并零速2秒。
若报姿态/状态/音频过期，先按日志排查，勿靠反复喊或下调安全门限绕过。

这次只需更新两份代码，不需要重编译Rust、重传识别权重或重生成关键词：

```bash
# 电脑WSL，位于部署仓库根目录
scp scripts/keyboard-control.py speech/keyboard_input.py radxa@192.168.2.70:/tmp/
```

板端先托住机器人、退出旧键盘与独立recognize（勿让两个识别进程抢麦克风），然后：

```bash
sudo install -m 644 /tmp/keyboard-control.py /opt/robot/daemon/current/scripts/keyboard-control.py
sudo install -m 644 /tmp/keyboard_input.py /home/radxa/microduck-speech/current/keyboard_input.py

sudo python3 /opt/robot/daemon/current/scripts/keyboard-control.py --speech preview \
  --speech-dir /home/radxa/microduck-speech/current --speech-gain 1 --speech-threshold .35
```

每说一个词应出现完整日志行，不依赖C/L或终端宽度。随后托住退出，再启动`--speech on`，
人工Enter完成后，等待零速2秒再说前进/后退；观察是没识别、识别被拒绝，还是请求发出但实机不执行。
若仍没有`[语音识别]`，先核对启动打印的目录是否就是独立测试目录（`pwd -P`），Python是否为同一`.venv`；
用同一设备、gain、threshold做sudo独立识别对照。重启robotd后CPU/麦克风占用、机械噪声差异尚待板端排查。
Windows36项33通过/3平台跳过，Linux36项34通过/2依赖跳过，master键盘4项通过；
含“词后紧跟心跳”、可见拒绝原因、过期/键盘优先、真实假KWS+PTY集成。未在本轮板端实测识别率。

### 2026-10-29 短口令与后退更新

关键词改为前进/后退/停止/坐下/起来，移除旧“鸭子”前缀；新增backward输入，默认-0.10m/s，
沿用前进的3秒限时/零速2秒等待/失联停车。master动作逻辑、安全与默认speech off不改。
关键词缩短后误触发风险更高，先preview，不自动降低识别阈值。同一音频批出现前进和后退时停车。

必须重新生成关键词token和manifest，不需重训或重新导出运动模型；无需Rust重编译。
先退出旧键盘/识别进程（退出前托住），在电脑端执行：

```bash
# 本地Windows独立语音环境（PowerShell）；使用已有模型包，不重下载权重
speech/.venv/Scripts/python.exe speech/prepare.py
speech/.venv/Scripts/python.exe speech/recognize.py --check
```

然后WSL执行`sh xh_push.sh radxa@192.168.2.70`更新键盘脚本（已安装本分支master二进制时无需build），
`sh speech/sync.sh radxa@192.168.2.70`上传新版语音工程；按第2节对新目录运行setup/check/预览后切换current。
也可在Linux独立语音环境用`.venv/bin/python prepare.py`生成。**不能只更新keywords_raw.txt就直接运行**。
板端先preview确认五词都命中，on模式人工Enter后逐个验证：前进/停止、后退/停止、坐下/起来；
测后退3秒停车、前后换向零速等待、键盘接管和断流停车。旧master/dzh分支均不修改。
本轮Windows独立环境31项中28通过/3平台跳过，Linux31项中29通过/2依赖跳过；
另master键盘4项通过。已重新生成五词token/manifest并通过真实KWS加载；尚未上传或实测麦克风/机器人动作。

### 初次迁移记录

- Linux：master键盘/声音/视觉/standup脚本19项通过；speech整组28项中26通过、2缺依赖跳过，
  包括真实假识别子进程、preview/on两种启动方式与master键盘伪终端共存。未使用麦克风或机器人。
- Windows独立speech环境28项中25通过、3平台跳过，包含官方已知词样例的真实CPU模型推理；不代表中文麦克风识别率实测。
- `cargo check --locked --offline -p robotd`通过；duck-control 88项通过。
- master原样Rust测试未全过：robotd 95通过/5失败（重启/初始扭矩、HOME写入、relax次数等假IO断言），
  robotd-params 26通过/1失败（master已自定义示例模型路径，不等于上游默认路径）。
  Rust/配置/模型与7caaae8逐文件无diff，不擅改任务衔接或跳过断言去报全绿。
- rustfmt未安装，格式检查未完成。Python AST/Bash命令语法/git diff检查另行完成。
- 未交叉编译ARM发布二进制、上传或做板端动作验收；上面的`xh_push.sh --build`与测试仍须板端执行。
