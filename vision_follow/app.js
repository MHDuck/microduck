/* Standalone LAN WebRTC client. Never accesses UART or changes daemon configuration. */
"use strict";
const $ = id => document.getElementById(id);
const now = () => performance.now() / 1000;
const { Tracker, FollowCycle, boxesFrom, stateProblem, imuEvidenceProblem } = DuckVision;
let tracker = new Tracker(), config, ws, pc, channel, sessionId, rotation = null;
const followCycle = new FollowCycle();
let pulseTimer = null;
let visionSettings = { hz: .5, maxAge: 5, httpTimeout: 8, targetTimeout: 12, videoTimeout: 10,
  followStyle: "step", moveSeconds: 2.5, settleSeconds: 1, requireFront: false };
let state = null, stateAt = -Infinity, stateT = -Infinity, modeAt = -Infinity, robotMode = "";
let videoAt = -Infinity, lastTick = now(), lastDetectionAt = -Infinity;
let videoProof = "none", videoCallbacks = 0, lastPresented = null, playbackAt = -Infinity;
let mediaStats = null, mediaStatsAt = -Infinity, mediaQuery = null, mediaRequestedAt = -Infinity, mediaError = null, videoLogAt = -Infinity;
let active = false, ownsMotion = false, busy = false, enabling = false, epoch = 0, connecting = false;
let standUpUnsupported = false;
let lastHealth = null, healthAt = -Infinity, healthQuery = null, healthTimer = null, activating = false;
let healthRequestedAt = -Infinity, healthRttMs = null, healthError = null, healthLogAt = -Infinity;
let riseRequest = null, riseTimer = null;
let pollTimer, nextId = 1, lastSummary = -Infinity, lastReason = "", latestDetection = null;
const pending = new Map(), journal = [];
let sizeDistanceSet = false, lastArucoError = -Infinity, lastArucoSample = -Infinity;
let lastArucoIssue = null, lastArucoTimingLog = -Infinity;
const arucoFrames = new DuckAruco.ArucoFrames({ video: $("video"), onResult: acceptDetection, onError: arucoError, onMetrics: arucoMetrics });

function detectorSource() { return $("detector").value || "duck"; }
function stepMode() { return active && $("mode").value === "follow" && visionSettings.followStyle === "step"; }
function configureVision() {
  const s = { hz: Number($("detectionHz").value), maxAge: Number($("detectionMaxAge").value),
    httpTimeout: Number($("httpTimeout").value), targetTimeout: Number($("targetTimeout").value),
    videoTimeout: Number($("videoTimeout").value), followStyle: $("followStyle").value,
    moveSeconds: Number($("moveSeconds").value), settleSeconds: Number($("settleSeconds").value),
    requireFront: $("requireFront").checked };
  if (![s.hz, s.targetTimeout, s.videoTimeout].every(Number.isFinite) || s.hz < .1 || s.hz > 5 ||
      s.targetTimeout < .6 || s.targetTimeout > 60 || s.videoTimeout < 1.5 || s.videoTimeout > 30 ||
      !["step", "continuous"].includes(s.followStyle)) throw new Error("低帧率参数超出范围");
  if (detectorSource() === "aruco" && s.targetTimeout < s.maxAge + Math.max(1 / s.hz, s.maxAge) + .5)
    throw new Error("目标保留时间过短，无法在此检测频率/延迟下确认两帧，请增加目标保留时间");
  if (detectorSource() === "aruco" && s.followStyle === "step" && s.targetTimeout < s.maxAge + s.moveSeconds + .2)
    throw new Error("目标保留时间不足以执行完整移动段，请增加目标保留时间");
  arucoFrames.configure({ maxSide: config?.arucoMaxSide || 640, jpegQuality: config?.arucoJpegQuality || .7,
    maxAge: s.maxAge, httpTimeout: s.httpTimeout });
  followCycle.configure(s); visionSettings = s;
}
function acceptDetection(params, captured) {
  if (stepMode() && (params.source !== "aruco" || !followCycle.accepts(captured, now()))) return;
  if (params.source === "aruco") lastArucoIssue = null;
  latestDetection = params; lastDetectionAt = captured;
  tracker.ingest(params, captured); draw(params);
  journal.push({ time: new Date().toISOString(), event: "detection", captured, data: params });
  if (journal.length > 12000) journal.shift();
}
function arucoError(error) {
  if (detectorSource() !== "aruco") return;
  tracker.ingest({ width: 1, height: 1, boxes: [] }, now());
  lastArucoIssue = error.message;
  $("overlay").replaceChildren();
  if (active && error.recoverable) {
    if (stepMode() && followCycle.phase === "moving") {
      followCycle.brake(); clearTimeout(pulseTimer); pulseTimer = null;
    }
    try { raw("robot.move", { vx: 0, vy: 0, vyaw: 0 }); }
    catch (failure) { stop(`停步发送失败：${failure.message}`); }
    $("status").textContent = "检测暂缓，已停步；等待连续两帧新结果";
  } else if (active) stop(`ArUco：${error.message}`);
  if (now() - lastArucoError > 2) {
    log(error.recoverable ? "ArUco 检测暂缓" : "ArUco 检测错误", { error: error.message, kind: error.kind, timing: error.timing });
    lastArucoError = now();
  }
}
function arucoMetrics(timing) {
  if (detectorSource() !== "aruco") return;
  const ms = v => Number.isFinite(v) ? `${v.toFixed(0)} ms` : "–";
  $("arucoTiming").textContent = `ArUco ${timing.width}×${timing.height} · 编码 ${ms(timing.encode_ms)} · HTTP ${ms(timing.http_ms)}`
    + ` · 服务端 ${ms(timing.server_ms)} · 帧龄 ${ms(timing.age_ms)}`
    + ` · ${timing.fresh_for_control ? "新鲜结果" : "未用于控制"}`;
  if (now() - lastArucoTimingLog >= 2) { log("ArUco 检测耗时", timing); lastArucoTimingLog = now(); }
}
function detectorUi() {
  const marker = detectorSource() === "aruco", info = config && config.aruco;
  $("markerId").disabled = !marker;
  $("metricOption").disabled = !marker || !info || !info.calibrated;
  $("targetDistance").disabled = $("distanceMode").value !== "meters";
  $("targetSize").disabled = $("distanceMode").value === "meters";
  $("arucoInfo").textContent = info
    ? `DICT_5X5_50 · 黑色外边长 ${info.markerMm} mm · ${info.calibrated ? "已载入内参，可估计相机到码的距离" : "未标定，仅注视 / 相对大小跟随"}`
    : "ArUco 未启用：请在独立环境以 --detector aruco 启动服务";
}

function log(event, data = {}) {
  const entry = { time: new Date().toISOString(), event, ...data };
  journal.push(entry); if (journal.length > 12000) journal.shift();
  const el = document.createElement("div"); el.textContent = `${entry.time.slice(11, 19)} ${event} ${JSON.stringify(data)}`;
  $("log").append(el); while ($("log").children.length > 250) $("log").firstChild.remove();
  $("log").scrollTop = $("log").scrollHeight;
}
function connected() { return channel && channel.readyState === "open"; }
function buttons() {
  $("connect").disabled = !!ws || connecting; $("disconnect").disabled = !ws;
  $("enable").disabled = !connected() || enabling || active || activating || !!riseRequest || standUpUnsupported;
  $("start").disabled = !connected() || active || enabling || activating || !!riseRequest;
  $("stop").disabled = !ownsMotion && !active;
  $("calibrate").disabled = !connected() || active;
  $("snapshot").disabled = !connected();
  $("diagnose").disabled = !connected();
  $("previewDetection").disabled = !connected() || detectorSource() !== "aruco";
  $("detectionState").textContent = !connected() ? "识别状态：未连接"
    : detectorSource() !== "aruco" ? "识别状态：使用原鸭子模型"
    : active ? `识别状态：正在为${$("mode").value === "follow" ? "跟随" : "注视"}提供检测`
    : $("previewDetection").checked ? "识别状态：仅识别预览，不控制机器人" : "识别状态：仅视频，未运行识别";
  $("start").textContent = $("mode").value === "follow" ? "启动前后跟随" : "启动注视";
}
function raw(method, params) {
  if (!connected()) throw new Error("控制连接未建立");
  if (channel.bufferedAmount > 16384) throw new Error("控制通道积压");
  channel.send(JSON.stringify({ jsonrpc: "2.0", method, params }));
}
function rpc(method, params = {}, timeout = 1800) {
  if (!connected()) return Promise.reject(new Error("控制连接未建立"));
  if (channel.bufferedAmount > 16384) return Promise.reject(new Error("控制通道积压"));
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`${method} 应答超时`)); }, timeout);
    pending.set(id, { resolve, reject, timer });
    try { channel.send(JSON.stringify({ jsonrpc: "2.0", id, method, params })); }
    catch (e) { clearTimeout(timer); pending.delete(id); reject(e); }
  });
}
function rejectPending() {
  for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error("连接已关闭")); }
  pending.clear();
}
function stop(reason = "人工停止") {
  if (followCycle.phase === "moving") logSegmentEnd(reason);
  const wasOwning = ownsMotion;
  activating = false; enabling = false;
  active = false; ownsMotion = false; epoch++; busy = false;
  tracker.velocity = 0; tracker.direction = 0;
  clearTimeout(pulseTimer); pulseTimer = null; followCycle.reset(); arucoFrames.cancel();
  clearTimeout(riseTimer); riseTimer = null; riseRequest = null;
  // Preview/connecting/disconnecting before any user action must never send a motion command.
  if (wasOwning && connected()) {
    try { raw("robot.move", { vx: 0, vy: 0, vyaw: 0 }); }
    catch (e) { log("停止发送失败，等待 daemon deadman", { error: e.message }); }
    // Do NOT relax or disable: stopping the demo must not make a standing robot collapse.
  }
  $("status").textContent = reason;
  if (wasOwning) log("停止控制", { reason, ...(/IMU|健康/.test(reason)
    ? { health_diagnostic: healthDiagnostic(), policy: state && state.policy, safety: state && state.safety } : {}),
    ...(/视频|画面|摄像头/.test(reason) ? { video_diagnostic: videoDiagnostic(), policy: state && state.policy } : {}) });
  buttons();
}
function disconnect(reason = "已断开") {
  stop(reason); clearInterval(pollTimer); pollTimer = null;
  clearInterval(healthTimer); healthTimer = null;
  lastHealth = null; healthAt = -Infinity; healthQuery = null;
  healthRequestedAt = -Infinity; healthRttMs = null; healthError = null; healthLogAt = -Infinity;
  connecting = false;
  standUpUnsupported = false;
  arucoFrames.cancel(); sizeDistanceSet = false;
  lastArucoIssue = null;
  rejectPending(); const oldChannel = channel, oldPeer = pc, oldWs = ws;
  channel = null; pc = null; ws = null; sessionId = null;
  if (oldChannel) { oldChannel.onclose = null; oldChannel.onmessage = null; oldChannel.onopen = null; }
  if (oldPeer) { oldPeer.onconnectionstatechange = null; oldPeer.ondatachannel = null; oldPeer.ontrack = null; oldPeer.onicecandidate = null; oldPeer.close(); }
  if (oldWs) { oldWs.onclose = null; oldWs.onmessage = null; oldWs.onerror = null; oldWs.close(); }
  $("video").srcObject = null; $("overlay").replaceChildren();
  state = null; stateAt = -Infinity; stateT = -Infinity; modeAt = -Infinity;
  lastDetectionAt = -Infinity; latestDetection = null; rotation = null; videoAt = -Infinity;
  videoProof = "none"; videoCallbacks = 0; lastPresented = null; playbackAt = -Infinity;
  mediaStats = null; mediaStatsAt = -Infinity; mediaQuery = null; mediaRequestedAt = -Infinity; mediaError = null; videoLogAt = -Infinity;
  $("connection").textContent = reason; buttons();
  $("healthSummary").textContent = "机器人 / IMU 状态：未连接";
  $("arucoTiming").textContent = "ArUco 耗时：等待检测";
  $("videoSummary").textContent = "视频链路：未连接";
}
function sendSignal(value) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(value)); }
function videoInfo(info) {
  if (![info.width, info.height, info.rotate].every(Number.isFinite) || info.width <= 0 || info.height <= 0 ||
      ![0, 90, 180, 270].includes(info.rotate)) throw new Error("无效视频旋转信息");
  rotation = info.rotate;
  const quarter = rotation === 90 || rotation === 270;
  const w = quarter ? info.height : info.width, h = quarter ? info.width : info.height;
  $("box").style.aspectRatio = `${w}/${h}`;
  $("box").style.width = h > w ? `min(100%, ${65 * w / h}vh)` : "100%";
  $("video").className = rotation ? `turn${rotation}` : "";
}
function draw(p) {
  if (!p || !Number.isFinite(p.width) || !Number.isFinite(p.height)) return;
  const svg = $("overlay"); svg.setAttribute("viewBox", `0 0 ${p.width} ${p.height}`); svg.replaceChildren();
  for (const b of boxesFrom(p)) {
    const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    for (const [k, v] of Object.entries({ x: b.box.x0, y: b.box.y0, width: b.box.x1 - b.box.x0, height: b.box.y1 - b.box.y0 })) r.setAttribute(k, v);
    if (tracker.target && tracker.target.box === b.box) r.style.stroke = "#ffd46b";
    svg.append(r);
    if (b.id !== null) {
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", b.box.x0); label.setAttribute("y", Math.max(16, b.box.y0 - 5));
      label.setAttribute("fill", "#ffd46b"); label.setAttribute("font-size", "20");
      label.textContent = `ID ${b.id}${b.distance === null ? "" : ` · ${b.distance.toFixed(2)} m`}`; svg.append(label);
    }
  }
  for (const [x1, y1, x2, y2] of [[p.width / 2, 0, p.width / 2, p.height], [0, p.height / 2, p.width, p.height / 2]]) {
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    for (const [k, v] of Object.entries({ x1, y1, x2, y2 })) line.setAttribute(k, v);
    svg.append(line);
  }
}
function onControl(data) {
  let msg;
  try { msg = JSON.parse(data); } catch { stop("控制通道数据无效"); return; }
  if (msg.id !== undefined && msg.id !== null) {
    const p = pending.get(msg.id); if (!p) return;
    pending.delete(msg.id); clearTimeout(p.timer);
    if (msg.error) p.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
    else if (msg.result && msg.result.accepted === false) p.reject(new Error(msg.result.reason || "指令被拒绝"));
    else p.resolve(msg.result);
    return;
  }
  if (msg.error) { stop(msg.error.message || "控制错误"); return; }
  if (msg.method === "media.video") {
    try { videoInfo(msg.params); } catch (e) { stop(e.message); }
  } else if (msg.method === "robot.state") {
    if (!msg.params || !Number.isFinite(msg.params.t)) return;
    // Repeated/out-of-order states do not refresh the safety watchdog.
    if (msg.params.t <= stateT) return;
    stateT = msg.params.t; state = msg.params; stateAt = now();
    healthLabel();
    checkRiseExecution();
    const problem = stateProblem(state, healthEvidence());
    if (active && problem) stop(problem);
  } else if (msg.method === "media.detections") {
    if (detectorSource() === "duck") acceptDetection(msg.params, now());
  }
}
async function pollMode() {
  try {
    const result = await rpc("robot.mode"); robotMode = result.mode; modeAt = now();
    if (active && robotMode !== "walk") stop("机器人不在 walk 模式");
  } catch (e) { if (active) stop(e.message); }
}
function playbackQuality() {
  try {
    const q = $("video").getVideoPlaybackQuality?.();
    if (!q || ![q.totalVideoFrames, q.droppedVideoFrames].every(Number.isFinite) ||
        q.totalVideoFrames < q.droppedVideoFrames || q.droppedVideoFrames < 0) return null;
    return { total: q.totalVideoFrames, dropped: q.droppedVideoFrames, presented: q.totalVideoFrames - q.droppedVideoFrames };
  } catch { return null; }
}
function observePlayback() {
  const q = playbackQuality(), t = now();
  if (q && lastPresented !== null && q.presented > lastPresented && $("video").readyState >= 2 && !$("video").paused) {
    // The frame appeared sometime since our PREVIOUS observation. Use that lower
    // bound rather than stamping old pixels as if they were decoded right now.
    videoAt = Math.max(videoAt, playbackAt); videoProof = "presented_counter";
  }
  lastPresented = q ? q.presented : null; playbackAt = t;
}
function videoDiagnostic() {
  const v = $("video"), track = v.srcObject?.getVideoTracks?.()[0];
  return { frame_age_ms: Number.isFinite(videoAt) ? (now() - videoAt) * 1000 : null,
    timeout_s: visionSettings.videoTimeout, frame_proof: videoProof, callback_count: videoCallbacks,
    playback: playbackQuality(), paused: v.paused, ready_state: v.readyState,
    current_time: v.currentTime, width: v.videoWidth, height: v.videoHeight,
    track_state: track?.readyState, track_muted: track?.muted, peer_state: pc?.connectionState,
    webrtc: mediaStats, stats_age_ms: Number.isFinite(mediaStatsAt) ? (now() - mediaStatsAt) * 1000 : null,
    stats_error: mediaError, detection_pending: !!arucoFrames.job,
    recognition: { preview_enabled: $("previewDetection").checked, control_active: active,
      control_mode: $("mode").value, enabled: detectorSource() === "aruco" && (active || $("previewDetection").checked) },
    settings: visionSettings };
}
function pollVideoStats(force = false) {
  if (mediaQuery) return mediaQuery;
  if (!pc?.getStats || (!force && now() - mediaRequestedAt < 2)) return Promise.resolve(null);
  const peer = pc, queriedAt = now(); mediaRequestedAt = queriedAt;
  let query;
  query = Promise.resolve().then(() => peer.getStats()).then(report => {
    if (pc !== peer) return null;
    const rows = [];
    report.forEach(row => {
      if (row.type !== "inbound-rtp" || (row.kind || row.mediaType) !== "video") return;
      const values = { id: row.id };
      for (const key of ["bytesReceived", "packetsReceived", "packetsLost", "framesReceived", "framesDecoded", "framesDropped",
        "keyFramesDecoded", "framesPerSecond", "freezeCount", "totalFreezesDuration", "jitter", "nackCount", "pliCount", "firCount"])
        if (Number.isFinite(row[key])) values[key] = row[key];
      rows.push(values);
    });
    mediaStats = rows; mediaStatsAt = queriedAt; mediaError = null; return rows;
  }).catch(error => { if (pc === peer) mediaError = error.message; return null; })
    .finally(() => { if (mediaQuery === query) mediaQuery = null; });
  mediaQuery = query; return query;
}
async function playVideo() {
  if (typeof $("video").play !== "function") return;
  try { await $("video").play(); log("视频播放已请求", { paused: $("video").paused }); }
  catch (error) { log("视频播放失败", { error: error.message }); }
}
function healthEvidence() { return { health: lastHealth, age: now() - healthAt }; }
function checkRiseExecution() {
  if (!riseRequest) return;
  const fall = state && state.safety && state.safety.fall;
  if ((state && state.policy === "standup") || (fall && fall.recovery_active)) {
    log("robotd 已开始起身", { policy: state.policy, safety: state.safety });
    $("status").textContent = "robotd 已开始起身；请等待站稳后手动启动注视。";
  } else if (now() - riseRequest.sent >= 3) {
    const reason = "起身请求已入队，但未观察到 robotd 开始恢复；请检查 robotd 的 stand-up refused / overcurrent 日志";
    log("起身尚未执行", { reason, policy: state && state.policy, safety: state && state.safety,
      health_diagnostic: healthDiagnostic() });
    $("status").textContent = reason;
  } else return;
  clearTimeout(riseTimer); riseTimer = null; riseRequest = null;
  buttons();
}
function healthDiagnostic() {
  return { health: lastHealth, age_ms: Number.isFinite(healthAt) ? (now() - healthAt) * 1000 : null,
    rtt_ms: healthRttMs, pending: !!healthQuery,
    pending_age_ms: healthQuery ? (now() - healthRequestedAt) * 1000 : null, last_error: healthError };
}
function healthLabel() {
  const imu = lastHealth && lastHealth.imu, fall = state && state.safety && state.safety.fall;
  $("healthSummary").textContent = `策略=${state ? state.policy : "未知"} · health.imu.ready=${imu ? imu.ready : "未知"}`
    + ` · 连续旧样本=${imu && Number.isInteger(imu.consecutive_stale_blocks) ? imu.consecutive_stale_blocks : "未知"}`
    + ` · imu_ready=${fall ? fall.imu_ready : "未知"} · imu_fresh=${fall ? fall.imu_fresh : "未知"}`
    + ` · recovery_active=${fall ? fall.recovery_active : "未知"}`
    + ` · 健康确认年龄=${Number.isFinite(healthAt) ? ((now() - healthAt) * 1000).toFixed(0) + " ms" : "无"}`
    + ` · 查询往返=${healthRttMs === null ? "未知" : healthRttMs.toFixed(0) + " ms"}${healthQuery ? "（查询中）" : ""}`
    + (lastHealth && lastHealth.reason ? ` · ${lastHealth.reason}` : "");
}
function pollHealth() {
  if (healthQuery) return healthQuery;
  const thisChannel = channel;
  const queriedAt = now();
  healthRequestedAt = queriedAt;
  let query;
  query = rpc("robot.health", {}, 1000).then(health => {
    if (channel !== thisChannel) throw new Error("健康检查期间连接已改变");
    // Include RPC round-trip time in proof age; a delayed reply is not a fresh sample.
    lastHealth = health; healthAt = queriedAt; healthRttMs = (now() - queriedAt) * 1000; healthError = null; healthLabel();
    if ((active || activating || enabling) && now() - healthLogAt >= 2) {
      log("IMU 健康链路", healthDiagnostic()); healthLogAt = now();
    }
    if (active && state && state.safety && state.safety.fall && state.safety.fall.imu_fresh === false) {
      const problem = stateProblem(state, healthEvidence()); if (problem) stop(problem);
    } else if (active && health && health.imu && health.imu.ready === false) stop("IMU 驱动未就绪或姿态数据已过期");
    return health;
  }).catch(error => {
    if (channel === thisChannel) healthError = error.message;
    throw error;
  }).finally(() => {
    if (healthQuery !== query) return;
    healthQuery = null;
    // When RTT already used the request interval, renew immediately rather than
    // waiting for another timer slot. Keep one request and the same 500 ms proof limit.
    if ((active || activating || enabling) && now() - healthRequestedAt >= .2)
      setTimeout(refreshHealth, 0);
  });
  healthQuery = query;
  return query;
}
async function freshHealth() {
  const began = now(), inherited = healthQuery;
  let health = await pollHealth();
  // A preview request can already be old when the user clicks Start. Drain it, then
  // perform at most one new query; never stamp an old reply with its arrival time.
  if (inherited && healthAt < began && now() - healthAt > .25) {
    log("重新获取启动健康确认", healthDiagnostic());
    health = await pollHealth();
  }
  return health;
}
function refreshHealth() {
  if (!connected() || healthQuery) return;
  const interval = active || enabling || activating ? .2 : 2;
  if (now() - healthRequestedAt < interval) return;
  return pollHealth().catch(error => {
    if (active) stop(`健康查询失败：${error.message}`);
    else log("健康查询失败", { error: error.message, health_diagnostic: healthDiagnostic() });
  });
}
function newPeer() {
  pc = new RTCPeerConnection({ iceServers: [] });
  const thisPeer = pc;
  pc.ontrack = e => {
    if (pc !== thisPeer) return;
    if (e.track?.kind !== "video") { log("忽略非视频轨道", { kind: e.track?.kind }); return; }
    const current = $("video").srcObject?.getVideoTracks?.()[0];
    if (current === e.track) return;
    if (ownsMotion) stop("视频轨道已更换，请重新启动控制");
    $("video").srcObject = new MediaStream([e.track]);
    arucoFrames.resetSource();
    videoAt = -Infinity; videoProof = "none"; lastPresented = playbackQuality()?.presented ?? null; playbackAt = now();
    log("接收视频轨道", { id: e.track.id, kind: e.track.kind, state: e.track.readyState, muted: e.track.muted });
    playVideo();
  };
  pc.onicecandidate = e => { if (e.candidate) sendSignal({ type: "peer", sessionId,
    ice: { candidate: e.candidate.candidate, sdpMLineIndex: e.candidate.sdpMLineIndex } }); };
  pc.onconnectionstatechange = () => {
    $("connection").textContent = pc.connectionState;
    if (["failed", "disconnected", "closed"].includes(pc.connectionState)) disconnect("WebRTC 连接断开");
  };
  pc.ondatachannel = e => {
    if (channel) { e.channel.close(); return; }
    channel = e.channel; const thisChannel = channel; channel.onmessage = e => onControl(e.data);
    channel.onclose = () => disconnect("控制通道断开");
    const opened = async () => {
      log("视频控制通道已连接，仅预览"); $("connection").textContent = "已连接 · 仅预览"; buttons();
      try {
        const info = await rpc("media.video");
        if (pc !== thisPeer || channel !== thisChannel) return;
        videoInfo(info);
        const identity = await rpc("robot.subscribe", { hz: 10 });
        if (pc !== thisPeer || channel !== thisChannel) return;
        log("当前模型", identity || {});
        await pollMode();
        if (pc !== thisPeer || channel !== thisChannel) return;
        try { await pollHealth(); } catch (error) { log("健康查询失败", { error: error.message }); }
        if (pc === thisPeer && channel === thisChannel) {
          pollTimer = setInterval(pollMode, 1800);
          healthTimer = setInterval(refreshHealth, 125);
        }
      } catch (error) { if (pc === thisPeer && channel === thisChannel) disconnect(error.message); }
    };
    channel.onopen = opened; if (channel.readyState === "open") opened();
  };
}
async function connect() {
  if (ws || connecting) return;
  connecting = true; buttons();
  try {
    if (!window.RTCPeerConnection) throw new Error("当前浏览器不支持 WebRTC，请使用 Chrome/Edge");
    config = await (await fetch("/config.json")).json();
    $("detector").value = config.detector || "duck";
    $("previewDetection").checked = false;
    for (const [id, value] of Object.entries({ detectionHz: config.arucoHz ?? .5,
      detectionMaxAge: config.detectionMaxAge ?? 5, httpTimeout: config.arucoHttpTimeout ?? 8,
      targetTimeout: config.targetTimeout ?? 12, videoTimeout: config.videoTimeout ?? 10,
      followStyle: config.followStyle || "step", moveSeconds: config.followStepSeconds ?? 2.5,
      settleSeconds: config.followSettleSeconds ?? 1 })) $(id).value = String(value);
    $("requireFront").checked = config.followRequireFront === true;
    configureVision();
    $("markerId").value = String(Number.isInteger(config.markerId) ? config.markerId : 0);
    if (detectorSource() === "aruco") $("targetSize").value = "0.10";
    $("distanceMode").value = config.aruco && config.aruco.calibrated ? "meters" : "size";
    tracker.configure(options()); detectorUi();
    log("检测源", { detector: detectorSource(), aruco: config.aruco || null, timing: visionSettings });
    if (detectorSource() === "aruco") log("仅视频预览", { note: "识别预览默认关闭；可勾选启用识别，或人工启动注视后自动识别" });
    const host = config.robotHost || location.hostname;
    const url = new URL(`http://${host}`); url.protocol = "ws:"; url.port = config.signallingPort;
    ws = new WebSocket(url.origin); const thisSocket = ws;
    connecting = false; buttons(); $("connection").textContent = "正在连接";
    ws.onclose = () => disconnect("信令连接断开");
    ws.onerror = () => { log("信令连接失败", { host, port: config.signallingPort }); disconnect("信令不可达，检查 mediad 和 8443 端口"); };
    // Serialize SDP/ICE: an ICE candidate must not race setRemoteDescription.
    let queue = Promise.resolve();
    ws.onmessage = e => {
      queue = queue.then(async () => {
        if (ws !== thisSocket) return;
        const m = JSON.parse(e.data);
        if (m.type === "welcome") sendSignal({ type: "list" });
        else if (m.type === "list") {
          const producer = m.producers && m.producers[0];
          if (!producer) throw new Error("mediad 无视频生产者");
          log("视频网关版本", producer.meta || {});
          sendSignal({ type: "startSession", peerId: producer.id });
        } else if (m.type === "sessionStarted") { sessionId = m.sessionId; newPeer(); }
        else if (m.type === "peer") {
          if (!pc || (m.sessionId && m.sessionId !== sessionId)) return;
          if (m.sdp) {
            if (m.sdp.type !== "offer") throw new Error("非预期 SDP 类型");
            const negotiatingPeer = pc;
            await negotiatingPeer.setRemoteDescription(m.sdp); const answer = await negotiatingPeer.createAnswer();
            await negotiatingPeer.setLocalDescription(answer);
            if (ws !== thisSocket || pc !== negotiatingPeer) return;
            sendSignal({ type: "peer", sessionId, sdp: { type: "answer", sdp: answer.sdp } });
          } else if (m.ice) await pc.addIceCandidate({ candidate: m.ice.candidate, sdpMLineIndex: m.ice.sdpMLineIndex });
        } else if (m.type === "error") throw new Error(m.details || "信令错误");
        else if (m.type === "endSession") disconnect("视频会话结束");
      }).catch(error => { if (ws === thisSocket) { log("连接错误", { error: error.message }); disconnect(error.message); } });
    };
  } catch (e) { log("连接失败", { error: e.message }); disconnect(e.message); }
}
function options() {
  return { targetSize: Number($("targetSize").value), forward: Number($("forward").value),
    backward: Number($("backward").value), vertical: $("vertical").checked,
    invertYaw: $("invertYaw").checked, invertPitch: $("invertPitch").checked,
    targetId: detectorSource() === "aruco" ? Number($("markerId").value) : null,
    distanceMode: $("distanceMode").value || "size", targetDistance: Number($("targetDistance").value || ".6"),
    timeout: visionSettings.targetTimeout,
    frontGate: visionSettings.followStyle !== "step" || visionSettings.requireFront };
}
function readiness(t) {
  if (!connected()) return "控制连接未建立";
  if (document.hidden) return "页面在后台";
  if (rotation === null) return "等待视频方向信息";
  if (t - videoAt > visionSettings.videoTimeout)
    return `视频无新画面（${Number.isFinite(videoAt) ? (t - videoAt).toFixed(1) + " 秒" : "尚未收到帧"}，期限 ${visionSettings.videoTimeout} 秒）`;
  if (t - stateAt > 1.0) return "机器人状态过期";
  if (t - modeAt > 3.8 || robotMode !== "walk") return "等待 walk 模式确认";
  return stateProblem(state, healthEvidence());
}
function logSegmentEnd(reason) {
  log("本段行走结束", { reason, planned_seconds: followCycle.moveSeconds,
    elapsed_seconds: Math.max(0, now() - followCycle.started), requested_vx: followCycle.plannedSpeed,
    peak_command_abs_vx: followCycle.peakSpeed });
}
async function tick() {
  const t = now(), elapsed = t - lastTick; lastTick = t;
  observePlayback();
  if (connected()) {
    pollVideoStats();
    $("videoSummary").textContent = `视频：距新画面 ${Number.isFinite(videoAt) ? (t - videoAt).toFixed(1) + " s" : "未知"}`
      + ` / 期限 ${visionSettings.videoTimeout} s · 证据 ${videoProof} · 播放${$("video").paused ? "暂停" : "中"}`;
    if (t - videoLogAt >= 3) { log("视频链路", videoDiagnostic()); videoLogAt = t; }
  }
  if (active && elapsed > .4) stop("浏览器控制循环暂停，需重新启动");
  if (active) {
    const reason = readiness(t); if (reason) stop(reason);
  }
  if (stepMode()) followCycle.canCapture(t, videoAt);
  let out = tracker.output(t, active ? $("mode").value : "gaze", elapsed);
  if (stepMode()) {
    const before = followCycle.phase;
    out = followCycle.update(out, lastDetectionAt, t);
    if (out.phase === "moving" && before !== "moving") {
      arucoFrames.cancel();
      const generation = epoch;
      pulseTimer = setTimeout(() => {
        if (!stepMode() || generation !== epoch || followCycle.phase !== "moving") return;
        logSegmentEnd("移动段到时"); followCycle.brake("移动段到时"); tracker.velocity = 0;
        try { raw("robot.move", { vx: 0, vy: 0, vyaw: 0 }); }
        catch (error) { stop(error.message); }
      }, Math.max(0, followCycle.deadline - now()) * 1000);
      log("停走跟随：执行短程移动", { captured: lastDetectionAt, result_age_ms: (t - lastDetectionAt) * 1000,
        seconds: followCycle.moveSeconds, speed_limit: followCycle.speed,
        ramp_seconds: Math.abs(followCycle.speed) / followCycle.acceleration,
        require_front: visionSettings.requireFront });
    } else if (out.phase !== "moving") {
      if (before === "moving") logSegmentEnd(followCycle.stopReason);
      clearTimeout(pulseTimer); pulseTimer = null;
    }
  }
  if (t - lastDetectionAt > visionSettings.targetTimeout) $("overlay").replaceChildren();
  $("metrics").textContent = `策略 ${state ? state.policy : "未知"} · 检测间隔 ${Number.isFinite(lastDetectionAt) ? (t - lastDetectionAt).toFixed(1) : "–"} s\n`
    + `目标高度 ${tracker.target ? (tracker.target.size * 100).toFixed(1) : "–"}% / 期望 ${(tracker.o.targetSize * 100).toFixed(1)}%\n`
    + (detectorSource() === "aruco" ? `ArUco ID ${$("markerId").value} · 相机到码 ${tracker.target && tracker.target.distance !== null ? tracker.target.distance.toFixed(2) + " m" : "无有效标定距离"}\n` : "")
    + `头部 yaw 指令 ${(out.head[2] * 180 / Math.PI).toFixed(1)}° / 实测 ${state && Array.isArray(state.joints) && Number.isFinite(state.joints[7]) ? (state.joints[7] * 180 / Math.PI).toFixed(1) : "–"}°\n`
    + `前后指令 ${active ? out.vx.toFixed(2) : "0.00"} m/s`;
  if (!active) return;
  $("status").textContent = !stepMode() && lastArucoIssue && !out.valid
    ? "检测暂缓，已停步；等待连续两帧新结果" : out.reason;
  if (t - lastSummary >= 1 || lastReason !== out.reason) {
    log("控制", { mode: $("mode").value, phase: out.phase, reason: out.reason, vx: out.vx,
      head: out.head, feedback: state && state.head, joints: state && state.joints,
      policy: state && state.policy }); lastSummary = t; lastReason = out.reason;
  }
  if (busy) return;
  busy = true; const generation = epoch;
  const stopping = stepMode() && followCycle.phase === "stopping" && out.vx === 0;
  try {
    const [moveReply] = await Promise.all([
      rpc("robot.move", { vx: out.vx, vy: 0, vyaw: 0 }, 700),
      // Freeze the last gaze on loss; do not integrate or snap to a neutral pose.
      rpc("robot.head", { neck_pitch: out.head[0], head_pitch: out.head[1], head_yaw: out.head[2], head_roll: out.head[3] }, 700),
    ]);
    if (stopping && (!moveReply || moveReply.accepted !== true)) throw new Error("未确认零速度已接受，不能进入停步检测");
    if (generation === epoch && stopping && followCycle.acknowledgeStop(now())) {
      tracker.reset(out.head); lastDetectionAt = -Infinity; latestDetection = null; $("overlay").replaceChildren();
      log("停走跟随：零速度已确认", { settle_seconds: followCycle.settleSeconds });
    }
  } catch (error) { if (generation === epoch) stop(error.message); }
  finally { if (generation === epoch) busy = false; }
}
$("connect").onclick = connect;
$("disconnect").onclick = () => disconnect("人工断开");
$("enable").onclick = async () => {
  if (enabling || riseRequest) return;
  stop("准备起身"); enabling = true; const generation = epoch; buttons();
  try {
    log("人工请求起身使能");
    const health = await freshHealth();
    if (generation !== epoch) return;
    log("起身前健康检查", { health, health_diagnostic: healthDiagnostic(), policy: state && state.policy, safety: state && state.safety });
    const problem = imuEvidenceProblem(healthEvidence()); if (problem) throw new Error(problem);
    if (!state || now() - stateAt > 1) throw new Error("机器人状态尚未更新，请收到新状态后再请求起身");
    const fall = state && state.safety && state.safety.fall;
    if (fall && fall.imu_ready === false) throw new Error("IMU 未就绪（imu_ready=false），不能请求起身");
    if ((fall && fall.recovery_active) || (state && state.policy === "standup")) throw new Error("起身已经在执行，请等待结束，不重复提交");
    // Explicit StandUp is the daemon's recovery path for a fall latch. The daemon
    // still refuses overcurrent and bad sensors; ordinary enable is never substituted.
    if ((fall && fall.recovery_failed) || state.policy === "protection")
      log("人工请求保护状态下起身", { policy: state.policy, safety: state.safety,
        note: "由 robotd 判断能否恢复；过流保护保留，不自动重试" });
    ownsMotion = true;
    const result = await rpc("robot.enable", { on: true, toggle: false, stand_up: true });
    log("起身请求应答", result || {});
    if (generation !== epoch) return;
    if (!result || result.accepted !== true) throw new Error("未收到 accepted=true，不能确认请求已接受");
    $("status").textContent = "起身请求已入队，等待 robotd 开始执行；不会自动启动注视。";
    riseRequest = { sent: now() }; riseTimer = setTimeout(checkRiseExecution, 3000);
    checkRiseExecution();
  } catch (e) {
    if (generation !== epoch) return;
    // mediad decodes the request before forwarding; either gateway or robotd may be old.
    // Never drop stand_up and retry: ordinary enable has different power/recovery semantics.
    standUpUnsupported = /unknown field [`'"]stand_up[`'"]/.test(e.message);
    const reason = standUpUnsupported
      ? "起身接口版本不匹配：请同步更新 mediad 和 robotd，然后断开重连（只更新 robotctl 不够）"
      : e.message;
    stop(reason); log("起身请求失败", { error: e.message, reason });
  }
  finally { if (generation === epoch) enabling = false; buttons(); }
};
$("diagnose").onclick = async () => {
  try {
    await pollVideoStats(true);
    const health = await pollHealth();
    log("机器人健康检查", { health, health_diagnostic: healthDiagnostic(), video_diagnostic: videoDiagnostic(),
      policy: state && state.policy, safety: state && state.safety });
  } catch (e) { log("健康查询失败", { error: e.message, health_diagnostic: healthDiagnostic() }); }
};
$("resumeVideo").onclick = playVideo;
$("previewDetection").onchange = () => {
  if (active) {
    log("预览识别设置", { enabled: $("previewDetection").checked,
      note: "只影响停止控制后的预览；当前注视/跟随继续识别，启停请用控制按钮" });
    buttons(); return;
  }
  arucoFrames.cancel();
  tracker.reset(state?.head || [0, 0, 0, 0]); latestDetection = null; lastDetectionAt = -Infinity;
  $("overlay").replaceChildren(); lastArucoIssue = null; lastArucoSample = -Infinity;
  log("识别预览开关", { enabled: $("previewDetection").checked, note: "不使能机器人，不启动动作" });
  buttons();
};
$("start").onclick = async () => {
  if (activating || active || enabling || riseRequest) return;
  activating = true; const generation = epoch; buttons();
  try {
    await freshHealth();
    if (generation !== epoch) return;
    const reason = readiness(now()); if (reason) throw new Error(reason);
    configureVision();
    if ($("mode").value === "follow" && visionSettings.followStyle === "step" && detectorSource() !== "aruco")
      throw new Error("停走式跟随需要 ArUco 原始取帧；旧鸭子检测可选择连续跟随");
    if (detectorSource() === "aruco" && !(config && config.aruco)) throw new Error("服务未启用 ArUco，请以 --detector aruco 启动");
    if ($("distanceMode").value === "meters" && !(detectorSource() === "aruco" && config && config.aruco && config.aruco.calibrated))
      throw new Error("米制跟随需要已标定的 ArUco 距离，不会自动退回相对大小");
    if ($("mode").value === "follow" && detectorSource() === "aruco" && $("distanceMode").value !== "meters" && !sizeDistanceSet)
      throw new Error("未标定的 ArUco 跟随，请先把码放在期望位置并点击‘将当前距离设为跟随距离’");
    tracker.configure(options());
    const h = state.head;
    if (Math.abs(h[2]) > tracker.o.yawLimit || Math.abs(h[1]) > tracker.o.pitchLimit || Math.abs(h[0]) > .4 || Math.abs(h[3]) > .2)
      throw new Error("当前头部指令偏离中立过大，请先用键盘回正，再退出键盘程序");
    tracker.reset(h); active = true; ownsMotion = true; epoch++;
    arucoFrames.cancel(); lastArucoSample = -Infinity;
    if (stepMode()) followCycle.start();
    lastTick = now(); lastSummary = -Infinity;
    log("人工启动", { mode: $("mode").value, options: tracker.o, policy: state.policy,
      safety: state.safety, health: lastHealth, health_age_s: now() - healthAt }); buttons();
  } catch (e) {
    if (generation === epoch) {
      log("无法启动", { error: e.message, policy: state && state.policy, safety: state && state.safety,
        health: lastHealth, health_age_s: Number.isFinite(healthAt) ? now() - healthAt : null,
        health_diagnostic: healthDiagnostic() });
      $("status").textContent = e.message;
    }
  } finally { if (generation === epoch) activating = false; buttons(); }
};
$("stop").onclick = () => stop();
$("calibrate").onclick = () => {
  try {
    tracker.configure(options()); const size = tracker.calibrate(now());
    if (tracker.o.distanceMode === "meters") $("targetDistance").value = size.toFixed(3);
    else { $("targetSize").value = size.toFixed(3); sizeDistanceSet = true; }
    log("已设定跟随目标", { basis: tracker.o.distanceMode, value: size });
  } catch (e) { log("标定失败", { error: e.message }); }
};
for (const id of ["mode", "targetSize", "forward", "backward", "vertical", "invertYaw", "invertPitch", "detector", "markerId", "distanceMode", "targetDistance",
  "detectionHz", "detectionMaxAge", "httpTimeout", "targetTimeout", "videoTimeout", "followStyle", "moveSeconds", "settleSeconds", "requireFront"]) {
  $(id).onchange = () => {
    stop("参数已修改，请重新启动");
    if (["detector", "markerId", "distanceMode"].includes(id)) {
      arucoFrames.cancel(); sizeDistanceSet = false;
      tracker.reset(state && state.head ? state.head : [0, 0, 0, 0]);
      $("overlay").replaceChildren(); lastDetectionAt = -Infinity;
    }
    try { configureVision(); tracker.configure(options()); } catch (e) { log("参数错误", { error: e.message }); }
    detectorUi(); buttons();
  };
}
$("download").onclick = () => {
  const blob = new Blob([journal.map(x => JSON.stringify(x)).join("\n") + "\n"], { type: "application/x-ndjson" });
  const url = URL.createObjectURL(blob), a = document.createElement("a");
  a.href = url; a.download = `duck-vision-${new Date().toISOString().replace(/[:.]/g, "-")}.jsonl`; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$("snapshot").onclick = async () => {
  try {
    stop("保存标定照片，控制已停止");
    const blob = await arucoFrames.snapshot(); if (!blob) throw new Error("保存原始图像失败");
    const url = URL.createObjectURL(blob), a = document.createElement("a");
    a.href = url; a.download = `raw-${new Date().toISOString().replace(/[:.]/g, "-")}.png`; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    log("已下载原始标定照片", { width: $("video").videoWidth, height: $("video").videoHeight, rotated: false });
  } catch (e) { log("照片保存失败", { error: e.message }); }
};
addEventListener("keydown", e => {
  if (e.code === "Space" || e.code === "Escape") { e.preventDefault(); stop("空格 / Esc 停止"); }
});
document.addEventListener("visibilitychange", () => { if (document.hidden) stop("页面进入后台，需重新启动"); });
addEventListener("pagehide", () => disconnect("页面关闭"));
// Monitor *decoded video frames*, rather than assuming a live datachannel means a live camera.
if ($("video").requestVideoFrameCallback) {
  const frame = () => { videoAt = now(); videoProof = "frame_callback"; videoCallbacks++; $("video").requestVideoFrameCallback(frame); };
  $("video").requestVideoFrameCallback(frame);
}
setInterval(() => { tick().catch(e => stop(e.message)); }, 100);
function sampleAruco() {
  observePlayback();
  if (connected() && detectorSource() === "aruco" && (active || $("previewDetection").checked) && config && config.aruco && rotation !== null && !document.hidden &&
      now() - videoAt <= visionSettings.videoTimeout &&
      (!stepMode() || followCycle.canCapture(now(), videoAt)) &&
      now() - lastArucoSample >= 1 / visionSettings.hz) {
    lastArucoSample = now();
    return arucoFrames.step(rotation, videoAt);
  }
}
setInterval(sampleAruco, 200);
buttons(); log("模块就绪，默认仅预览，不自动使能或控制");
