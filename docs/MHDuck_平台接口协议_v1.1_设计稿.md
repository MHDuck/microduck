# MHDuck 平台控制接口 v1.0（简化设计稿）

更新：2026-10-30。本文定义算法/部署端提供的接口，不包含平台账号、用户权限、设备绑定及软件发布管理。**新增接口尚未实现，以下为开发约定。**

## 1. 连接

|用途|地址 / 方式|
|---|---|
|控制、设置、状态|WebSocket：`ws://<板子IP>:8000/api/v1/ws`（拟新增）|
|现有视频页面|`http://<板子IP>:8080/`（已有）|
|现有视频信令|`ws://<板子IP>:8443`，WebRTC 信令，不是视频文件地址（已有，端口可配置）|

例如板子为 192.168.2.70，视频页面为 `http://192.168.2.70:8080/`。
App 可先通过 WebView 打开现有页面；原生嵌入视频可复用 `mediad/webclient/index.html` 的 WebRTC 接入代码。
视频不是 MP4/RTSP URL，不能直接赋给普通 `<video src>`。现有页面还含旧控制按钮；正式平台只复用视频部分，不同时使用两套控制入口。

控制连接建立后即可查询和发指令，**不需要登录会话、申请控制权或每秒续期**。
一台机器人同一时刻只接受一个运动输入端：平台多用户分配由平台负责；键盘调试占用时返回 BUSY。
账号、权限、TLS 和公网接入由平台网关/运维负责；该板端接口仅面向可信内网，不得直接裸露公网。
HTTPS 页面需通过平台提供的 WSS 代理连接，不能直接连接明文 WS。

### 1.1 如何建立和保持连接

进入控制页面时创建一次 WebSocket，等待 `onopen` 后发送指令；页面打开期间一直复用这个对象。
**不需要每次操作重新连接，也不需要调用“保持连接”接口。** 空闲不操作时仍保持连接、继续接收状态。

```javascript
const ws = new WebSocket("ws://192.168.2.70:8000/api/v1/ws");
ws.onopen = () => console.log("已连接，可以查询状态");
ws.onmessage = (event) => console.log(JSON.parse(event.data));
ws.onclose = () => console.log("已断连：清空本地目标，停止指令发送定时器");
ws.onerror = () => {
  console.error("连接异常：停止控制并关闭连接");
  ws.close();
};
```

这是连接事件示例；实际页面需在关闭/异常回调里真正清空摇杆和停止运动发送定时器，不能只打印日志。
只有 `ws.readyState === WebSocket.OPEN` 才能发送，但 OPEN 不保证网络仍畅通，需按下表判断。

|检查|谁处理|约定|
|---|---|---|
|WebSocket 保活|板端 WebSocket 服务/客户端库|服务端每 5 秒发标准 Ping，5 秒内未收到对应 Pong 则终止连接；浏览器自动回复，App 库启用自动 Pong|
|状态接收|平台页面/App|正常每 200 ms 收到 state；从 onopen 或最后一帧 state 起连续 2 秒没收到，视为连接不可用，停止本地控制、关闭旧连接|
|运动输入|平台发送、板端检查|按住摇杆才每 50 ms 发速度；500 ms 无新目标自动停车，与 Ping/Pong 无关|

Ping/Pong 是 WebSocket 协议帧，不是 JSON 指令；**不增加 ping、心跳或 renew 业务接口**。
若平台网关中转，两个 WebSocket 链路分别启用标准保活；设备连接失效时，网关通知/关闭对应 App 控制连接。
网关不能在用户停止操作或离线后自行续发旧速度。

### 1.2 哪些情况算断连

|情况|平台处理|板端处理|
|---|---|---|
|主动退出控制页面|按第 1.3 节停止并关闭 WebSocket|收到连接关闭后清理该连接目标|
|收到 onclose / onerror|立即禁用控制、清空目标及发送定时器|确认关闭/异常后清理目标|
|Wi-Fi 断开、拔网线、App 被杀、单向网络故障|不保证立刻触发 onclose；按 2 秒状态超时关闭旧连接|按 Ping/Pong 超时终止失效连接；运动不等这个结果，500 ms 无目标先停车|
|页面隐藏、App 进入后台/锁屏|本版统一停止控制并关闭控制连接；回前台重新连接|收到关闭后清理；若退出通知未送达，超时兜底|
|视频断了，控制连接仍正常|单独提示视频断开；无画面遥控时停止运动|不把视频断开直接当成控制连接断开|

“清理目标”指：行走归零、取消后续连翻、释放头/身体/嘴/声音保持目标、撤销该端语音动作输入。
**断连默认停车保持，不自动全身卸力**；当前不可中断技能仍按原控制器规则收尾。
物理断网不可能总在发生瞬间被双方发现，所以不能把“onclose 后立即处理”当成断网检测手段。
收到 state 但其中 online=false / ready=false，表示控制服务或机器人未就绪，不等于 WebSocket 已断开。

### 1.3 如何正常断开

1. 停止所有连续发送定时器，清空本地摇杆/头部等目标。
2. 若连接仍为 OPEN，发送停止请求：

```json
{"id":"11","cmd":"robot.stop","params":{"reset_pose":true}}
```

3. 收到该请求响应后，或最多等待 1 秒后，关闭连接：

```javascript
ws.close(1000, "user_exit");
```

无需 robot.disconnect 或 control.release 接口。关闭页面/被系统杀掉时可能来不及发送上述请求，板端仍须按超时清理。
若用户要卸力而非站立保持，托护后先调用 `robot.enable {"on":false}`，确认完成后再关闭连接。
控制 WebSocket 与视频 WebRTC 独立：退出控制不等于关掉视频；退出整个页面时，两者分别关闭。

### 1.4 如何重连

旧连接关闭后才能创建新 WebSocket；网络异常可按 1、2、5、10 秒间隔重试，后续最多每 10 秒一次。
用户主动断开或页面在后台时不自动重连；回到前台后可重新连接。
重连后先读取 info/state/settings、等待第一帧新状态，再由用户明确开始控制。
不得恢复旧摇杆值、补发旧坐站/踢腿请求或自动打开语音动作；也不因重连本身再次起身。
旧连接的 id 去重记录不延续到新连接，未确认结果的技能先看实际状态，不自动重做。

## 2. 报文与调用规则

### 2.1 请求、响应

每个 WebSocket 文本帧一个 JSON。请求只有三个顶层字段，均必填：

```json
{"id":"1","cmd":"robot.move","params":{"vx":0.1,"vy":0,"wz":0}}
```

|字段|类型|说明|
|---|---|---|
|id|string|平台生成的请求流水号，1～64 字符；建议用递增数字字符串，如 "1"、"2"；同一连接的新请求不能重复|
|cmd|string|下表接口名称|
|params|object|接口参数；无参数传 `{}`|

成功 / 失败响应：

```json
{"id":"1","ok":true,"code":"OK","message":"","data":{}}
```

```json
{"id":"2","ok":false,"code":"BUSY","message":"正在执行踢腿动作","data":null}
```

响应字段固定为 `id:string、ok:boolean、code:string、message:string、data:object/null`。
每个请求均有响应；连续指令的响应表示目标已接收，不代表机器人已达到目标。
使能、坐站和技能受理时 code=ACCEPTED，结束后通过 action 事件通知，不另设操作查询接口。
参数表中标“可选”的字段可省，其余必填；未知字段或非法值拒绝，不局部执行。

#### id 如何填写

id 只用于“这条响应对应哪条请求”，**不是动作编号、设备编号，也不决定动作执行顺序**。
例如请求 id="1"，对应响应也必须 id="1"；坐下还是行走由 cmd/params 决定，与 id 的内容无关。
"req-001"、"exit-001" 也只是普通字符串，前缀没有协议含义、不绑定接口；本文示例统一使用 "1"、"2" 等格式。
注意传字符串 `"1"`，不是数字 `1`。平台统一维护一个计数器即可，不要每个按钮各自从 1 开始：

```javascript
let requestId = 0;
function sendCommand(cmd, params = {}) {
  if (ws.readyState !== WebSocket.OPEN) throw new Error("连接未就绪");
  const id = String(++requestId);
  ws.send(JSON.stringify({id, cmd, params}));
  return id;
}
```

每次新请求（包括每一帧速度）生成新 id；同一逻辑请求的重复发送才沿用原 id，运动/技能不自动重试。
同一动作的后续 action 事件用 request_id 引用原请求 id；state 等主动推送没有 id。
重连可重新从 "1" 开始。各代码块是独立示例，实际发送时不要照抄固定 id 反复使用。

### 2.2 三条控制规则

1. 连接后自动每秒推送 5 次状态，无需订阅接口。先 `robot.enable on=true`，等 ready=true 后控制。
2. 摇杆/头部/身体/嘴部保持目标时每 50 ms 发一次当前完整值；松手立即发零目标或 `robot.stop`。板端 500 ms 收不到对应连续目标自动复位；标准 Ping/Pong 不能代替运动帧，不需要额外业务心跳或 renew。
3. 连接关闭或按第 1 节判定失效后，板端立即停止新行走和后续连翻；未被感知的断网由连续目标超时兜底。一次性技能按原控制器规则收尾，不中途硬切单脚动作。重连不重放旧指令、不自动继续行走。

连续指令只保留最新目标，不积压执行。客户端不得离线缓存、补发或自动重试运动/技能；
网络明显延迟时停止遥控。500 ms 是连续输入看门狗，不是“一次动作必须在 500 ms 内结束”。
同一连接重复相同 id/cmd/params 返回原受理结果，不重复触发；同 id 内容不同返回 INVALID_PARAMS。断连后不保证去重，不重发无法确认结果的动作。

## 3. 接口

### 3.0 输入模式与动作的关系（先看这里）

**drive / head / body 是三种输入模式，不是 walk 动作的子任务；调用它们之前不要求先执行 action=walk。**

|概念|字段 / 接口|含义|何时设置|
|---|---|---|---|
|机械运动方式|drive_mode；robot.drive_mode|walk=双足 / roller=装轮行驶；来自板端配置|只有更换相应硬件时才切换，不是每次操作的前置步骤|
|输入模式|mode；robot.mode|drive=速度与动作按钮，head=调头，body=调身体|只在改变控制方式时调用一次，后续目标帧不重复切模式|
|动作目标|robot.action|sit / stand / ground_pick / kick_right / roulade 等|用户要执行该动作时调用，不是输入模式的共同入口|

在当前双足配置下 drive_mode=walk，平台仍可在 drive/head/body 之间切换；
**这不表示三个模式都固定运行 Walk ONNX**。实际模型由板端按姿态/动作选择，平台不要自行指定模型。
例如坐姿调头可能继续使用 SitStand，站立身体控制使用当前配置允许的策略；不支持则拒绝该模式。

|当前情况 / 目标|调用顺序（省略 id 和完整信封）|
|---|---|
|刚启动、身体卸力，要行走|enable on=true → 等起身结束且 ready=true（此时为 drive）→ 持续 move|
|已站立且 mode=drive，要行走|直接持续 move；不需要 action=walk|
|已站立，要调头|mode=head → 成功后持续 head；不需要 action=walk|
|已站立，要调身体|mode=body → 成功后持续 pose；不需要 action=walk|
|head/body 调完，要行走|停止旧目标发送 → mode=drive → 成功后持续 move；不需要 action=walk|
|已坐下，要行走或调身体|必要时 mode=drive → action=stand → 等动作结束且就绪 → move，或 mode=body 后 pose|
|已坐下，只要调头|模型支持坐姿调头时 mode=head → head；不必先站起来，否则返回 INVALID_STATE / NOT_SUPPORTED|
|技能正常结束后继续行走|等动作结束、就绪且 mode=drive → 新的 move；不需要补 action=walk|

切 mode 只改变输入方式、清除旧目标，**不会让坐着的机器人自动起立，也不会使能卸力的身体**。
stop（包括 reset_pose=true）不改变 mode，也不把坐姿切回站姿；从 head/body 返回行走仍须 mode=drive。
进入 head/body 前必须已使能、无活动技能/恢复，姿态和模型能力允许；未满足时返回错误，不暗中先执行 walk。

action=walk 保留为 action=stand 的兼容别名：请求起立/站立行走就绪，不固定锁定 Walk 模型、不会自动前进。
平台建议只用 action=stand 表示“起来”，用 move 表示“走”，无需额外增加一个“先进入 walk”的步骤。

### 3.1 基本控制

|cmd|params|功能 / 返回 data|
|---|---|---|
|robot.info|`{}`|设备与程序版本、可用动作、范围、视频地址；见第 5 节|
|robot.state|`{}`|获取当前状态；字段与自动推送一致|
|robot.enable|`{"on":true}`|true=使能并执行正常起身，false=停止并卸力；返回 `{"accepted":true}`|
|robot.move|`{"vx":0.1,"vy":0,"wz":0}`|三轴速度目标；返回实际接收的 `{"vx":0.1,"vy":0,"wz":0}`|
|robot.stop|`{}`|速度归零、停止后续连翻，不卸力；返回 `{}`|
|robot.mode|`{"mode":"head"}`|切 drive / head / body 输入模式；返回 `{"mode":"head"}`|
|robot.head|`{"neck_pitch":0,"head_pitch":0,"head_yaw":0.1,"head_roll":0}`|四头部关节相对默认姿态的目标偏移；返回四个接收值|
|robot.pose|`{"z":0,"roll":0,"pitch":0}`|身体高度/姿态偏移；返回三个接收值|
|robot.mouth|`{"open":0.5}`|嘴开度 0～1；返回 `{"open":0.5}`|
|robot.action|`{"name":"sit"}`|明确执行坐下、起立或技能；返回 `{"accepted":true,"name":"sit"}`|
|robot.shutdown|`{"confirm":true}`|确认后坐下关机；返回 `{"accepted":true}`，断连不代表已物理关机|

`robot.stop` 可选参数 `reset_pose:boolean`，默认 false；true 对应键盘空格：额外归零头/身体、闭嘴、释放持续发声。
`robot.enable on=false` 会失去支撑，应提示托护；它不是“停车”。开机和控制服务重启默认不使能。

|参数|单位 / 方向|
|---|---|
|vx|m/s，正前进，负后退|
|vy|m/s，正左横移，负右横移|
|wz|rad/s，正左转，负右转|
|neck_pitch / head_pitch / head_yaw / head_roll|rad，相对默认关节姿态；不是世界坐标朝向|
|z|m，相对默认身体高度|
|roll / pitch|rad，相对默认身体姿态|
|open|0=闭嘴，1=最大批准开口|

范围读取 `robot.info.limits`，不得把示例值当成所有机器的批准范围。超界返回 OUT_OF_RANGE，不静默截断。
move 三轴、head 四轴、pose 三轴每次完整发送，未操作的轴填 0。

|模式 / 状态|允许操作|
|---|---|
|drive|行走、坐站、一次性技能|
|head|停车后调头；仅模型支持的站姿/坐姿可用|
|body|站立静止时调身体，不与行走同时使用|
|卸力|查询、设置、声音、独立嘴部；不接受行走和身体技能|
|技能/恢复中|拒绝新的行走、调头、切模式和其他技能，返回 BUSY|

切模式自动清除旧目标，不切换另一份“头部模型”。头/身体仍通过原策略输入，嘴部独立。
首次/重新使能过程完成后进入 drive；已使能且就绪时重复 enable=true 为幂等操作，不重启服务、不重复起身，也不改变当前输入模式/坐站目标。

### 3.2 动作参数

`robot.action.params`：

|字段|类型|说明|
|---|---|---|
|name|string|下表动作名|
|repeat|boolean，可选|默认 false；仅 roulade 可为 true，按住时每 50 ms 重发，松手调用 robot.stop|

|name|动作|
|---|---|
|walk|stand 的兼容别名；不是进入 drive/head/body 的前置步骤，不直接前进|
|stand|从坐姿起来 / 停步站立；卸力时先 enable|
|sit|坐下；重复 sit 保持坐下，不切换回站立|
|ground_pick|低头触地并恢复|
|kick_left|左脚踢腿|
|kick_right|右脚踢腿|
|roulade|前滚翻|

任务只使用板端已配置的模型、周期和动作衔接，平台不传持续秒数/力度。
缺模型返回 MODEL_UNAVAILABLE。非 drive 模式先切 drive，再发动作。
sit/stand 为绝对目标，不直接对外暴露 sit_toggle；行走转坐下由板端按现有逻辑停车衔接。
普通 stop 不取消当前不可中断技能；连翻停止只是“不再执行下一次”。

### 3.3 设置与声音

|cmd|params|功能 / 返回 data|
|---|---|---|
|settings.get|`{}`|获取完整 Settings，见下表|
|settings.set|`{"speech_mode":"preview","walk_voice":true}`|只传要修改的字段；返回合并后的完整 Settings|
|robot.sound|`{"name":"laughing","kind":"emotion","sound_only":false}`|播放声音；返回 `{"accepted":true}`|

Settings 字段全集：

|字段|类型|默认值 / 约束|
|---|---|---|
|speech_mode|string|off；可选 off / preview / on|
|speech_gain|number|1；大于 0 且 ≤8|
|speech_threshold|number|0.35；大于 0 且 ≤1|
|walk_voice|boolean|false；是否行走发声|
|voice_emotions|string[]|["laughing"]；1～16 项，取 info.emotions|
|voice_gap_min_s|number|0；0～60 秒|
|voice_gap_max_s|number|0；0～60 秒，须 ≥min|
|voice_sound_only|boolean|false；行走发声是否不驱动嘴|

v1 设置仅对当前运行生效，不新增配置版本、会话覆盖或持久配置接口；服务重启后语音/行走发声默认关闭。
识别模型、Python 环境、声卡和模型文件继续由本地 speech 工程维护，不从接口传执行路径。
参数整体验证通过后更新；识别启动可异步完成，是否可用查看 state.speech_status / error 事件。
开关语音/行走声音不重新编译、不重启 robotd；更换识别设置最多重启识别进程。

|speech_mode|行为|
|---|---|
|off|不启动识别，不访问麦克风|
|preview|只识别、推送识别结果，不动作|
|on|识别并接入行走、坐下/起来；不自动使能，保留现有语音门控|

开启 on 即为本次明确允许语音动作，**不增加 voice.arm 接口**。
口令：前进、后退、停止、坐下、起来；前后走 ±0.1 m/s、最长 3 秒。
人工控制优先、播放声音时暂停动作触发、状态不合适时拒绝；语音与平台/键盘共用动作入口。
平台输入端断连或被键盘接管时撤销该端的语音动作输入，保留识别预览；重新设置 on 才重新允许。

`robot.sound` 参数：

|字段|类型|说明|
|---|---|---|
|name|string|声音标签或情绪名称|
|kind|string，可选|默认 sound；sound=声音标签，emotion=情绪发声|
|sound_only|boolean，可选|默认 false；true 不发送任何嘴部指令，包括闭嘴|
|hold|boolean，可选|默认 false；仅 kind=sound、name=wheee 支持；true 每 50 ms 续发，false 释放|

标签/情绪读取 info.sound_tags / emotions。普通 WAV 已开始后可能自然播放至结尾。
R 键效果：chirp 一次 + 持续 mouth.open=1；F 键效果：wheee hold + 持续开嘴；松开释放各自目标。

### 3.4 可选功能（直接复用已有功能，不作为首批接入依赖）

|cmd|params|返回 data / 说明|
|---|---|---|
|robot.drive_mode|`{"mode":"roller","confirm_hardware":true}`|`{"mode":"roller"}`；walk / roller，先卸力、无技能、模型可用；用户确认物理配件|
|robot.logs|`{"enabled":true}`|`{"enabled":true}`；开关当前连接的 log 事件，默认关闭|
|robot.instrument|`{"active":true}`|`{"active":true}`；开启/关闭 ToF 乐器|
|robot.chorale|`{"active":true,"piece":null}`|`{"active":true,"piece":null}`；piece 为曲目整数或 null（自动），取 info.chorale_piece_ids|

这些方法仅在 info.optional_commands 中出现时可用。confirm_hardware 是用户确认，不代表板端能检测轮子；
平台必须提示现场确认，不能后台自动切换。当前硬件不支持的乐器/合唱返回 NOT_SUPPORTED。
日志/电流 CSV 下载可由平台导出推送数据，不新增板端文件查询、采集任务或下载管理接口；开发键盘原 CSV 保留。

## 4. 调用示例

### 4.1 前进、停止、坐下、起来

```json
{"id":"1","cmd":"robot.info","params":{}}
```

```json
{"id":"2","cmd":"robot.enable","params":{"on":true}}
```

等 state.ready=true，按住前进时每 50 ms 发一次（每帧新 id）：

```json
{"id":"3","cmd":"robot.move","params":{"vx":0.1,"vy":0,"wz":0}}
```

松手停止，然后按坐下/起来按钮：

```json
{"id":"4","cmd":"robot.stop","params":{}}
```

```json
{"id":"5","cmd":"robot.action","params":{"name":"sit"}}
```

```json
{"id":"6","cmd":"robot.action","params":{"name":"stand"}}
```

每个坐站动作结束后再执行下一个，不同时发送。
后退把 vx 改为 -0.1；左/右转用 wz=+0.2/-0.2；左/右横移用 vy=+0.05/-0.05；其他轴为 0，均须在 limits 内。

### 4.2 调头与语音开关

```json
{"id":"7","cmd":"robot.mode","params":{"mode":"head"}}
```

```json
{"id":"8","cmd":"robot.head","params":{"neck_pitch":0,"head_pitch":0,"head_yaw":0.1,"head_roll":0}}
```

head 持续更新；退出时 stop reset_pose=true，然后 mode=drive。

```json
{"id":"9","cmd":"settings.set","params":{"speech_mode":"preview"}}
```

确认识别正常后允许语音动作，同时开启行走发声：

```json
{"id":"10","cmd":"settings.set","params":{"speech_mode":"on","walk_voice":true}}
```

关闭时传 `{"speech_mode":"off","walk_voice":false}`，不需要退出/重启机器人程序。

## 5. 设备信息和状态字段

### 5.1 robot.info 返回 data

|字段|类型|说明|
|---|---|---|
|name|string|设备名称|
|version|string|当前部署程序版本，只读|
|protocol_version|string|固定 1.0|
|actions|string[]|可用的动作 name；没有的动作禁用按钮|
|modes|string[]|可用 drive / head / body|
|drive_modes|string[]|可用 walk / roller|
|optional_commands|string[]|支持的第 3.4 节接口名|
|sound_tags / emotions|string[]|声音标签 / 情绪|
|chorale_piece_ids|integer[]|可用曲目；无则 []|
|limits|object|下表控制范围；对应功能不支持时为 null|
|video|object|`{viewer_url:string,signaling_url:string,transport:"webrtc"}`；取设备实际配置，无视频时为 null|

limits 每个非空字段均为 `[min,max]` 两元素数值数组：
`vx、vy、wz、neck_pitch、head_pitch、head_yaw、head_roll、z、roll、pitch、open`。
范围来自当前模型与部署配置，不在平台写死。

### 5.2 robot.state 返回 data / state 事件 data

|字段|类型|说明|
|---|---|---|
|online|boolean|控制服务是否可达；不代表已使能|
|enabled|boolean / null|舵机/策略使能状态，未知为 null|
|ready|boolean|允许接受新的行走/模式/技能输入；无故障且使能就绪才为 true|
|control_source|string|none / platform / keyboard / gamepad|
|mode|string|drive / head / body / unknown|
|drive_mode|string|walk / roller / unknown|
|posture_target|string|stand / sit / unknown，当前目标|
|posture_actual|string|stand / sit / unknown；无可靠检测即 unknown，不根据策略标签虚构|
|activity|string|idle / walking / rising / sitting / ground_pick / kick_left / kick_right / roulade / recovering / protected / shutting_down / unknown|
|policy / model|string / null|实际策略槽位 / 模型标识，未知为 null|
|velocity|object|`{vx:number,vy:number,wz:number}`，当前应用速度指令，不是实测速度；无有效状态时为 null|
|battery|object|`{voltage_v:number/null,percent:number/null,estimated:boolean,sample_age_ms:integer/null}`|
|temperature|object|`{cpu_c:number/null,motor_max_c:number/null,motor_mean_c:number/null,sample_age_ms:integer/null}`|
|current_a|number / null|已有输入电流测量/估算值，不额外轮询电机|
|current_estimated|boolean|电流是否估算；current_a=null 时不显示数值|
|speech_mode|string|当前有效 off / preview / on，可能因接管/断连降为 preview|
|speech_status|string|stopped / starting / ready / failed|
|walk_voice|boolean|当前行走发声开关|
|fault|object / null|`{code:string,message:string}`，无活动故障为 null|
|state_age_ms|integer / null|底层控制数据最后成功更新距当前的时间，未知为 null|

字段均返回，不可测量项为 null/unknown，不填 0 冒充测量。
电量百分比可为电压估算，CPU/电机温度分开显示；不承诺充电状态、逐关节温度或精确 SOC。
状态超过 500 ms 未更新时 ready=false；电量/温度采样超过 5000 ms 显示过期。
首期不新增“站稳/触球成功”检测算法；无法确认的实际姿态保持 unknown。

## 6. 事件与错误码

事件统一为 `{"event":"名称","data":{...}}`，无 id；通过是否存在 event 区分响应。

|event|data 全部字段|
|---|---|
|state|第 5.2 节状态，自动 5 Hz|
|action|`{request_id:string,name:string,status:string,message:string}`；status=running / finished / failed|
|speech|`{keyword:string,intent:string,executed:boolean,reason:string}`；intent=forward / backward / stop / sit / stand|
|error|`{code:string,message:string}`；异步服务/识别错误|
|log|`{level:string,message:string}`；level=debug / info / warn / error，仅 logs 开启时推送|

action.name 为 enable / relax / shutdown 或动作 name；同一动作从 running 到 finished/failed 使用同一 request_id。
finished 只表示控制流程/技能窗口结束，不保证站稳、触球等实机效果；运行过程观察 state。
重连不回放旧 action 事件；若结果无法确认，先检查实际状态，不自动重做。

|code|含义|
|---|---|
|OK|本次调用成功|
|ACCEPTED|耗时动作已受理，等待事件/状态|
|INVALID_PARAMS|缺参数、类型错误或未知字段|
|OUT_OF_RANGE|目标或设置超界|
|NOT_SUPPORTED|接口/硬件不支持|
|NOT_ENABLED|尚未使能|
|INVALID_STATE|模式或状态不允许|
|BUSY|其他输入端占用、动作执行中或设置处理中|
|MODEL_UNAVAILABLE|所需模型/识别资源不可用|
|DEVICE_ERROR|控制服务、传感器、舵机或识别进程异常|

平台只解析 code，不通过自然语言 message 判断逻辑。
