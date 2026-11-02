/* Browser/daemon protocol harness: no external packages, no physical robot. */
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const { picture } = (() => ({ picture(x = 0, size = .2) { return { width: 720, height: 1280,
  boxes: [{ x0: (x + 1) * 360 - 72, x1: (x + 1) * 360 + 72,
    y0: 640 - 640 * size, y1: 640 + 640 * size, score: 1.3 }] }; } }))();
const settle = () => new Promise(resolve => setImmediate(resolve));
async function harness(settings = {}) {
  let clock = 0, sent = [], drop = null, replyError = null;
  const replyOverrides = new Map();
  const uploads = [];
  let healthReply = { healthy: true, imu: { ready: true, consecutive_stale_blocks: 0 }, bus: { consecutive_errors: 0 } };
  let healthLatency = 0;
  let statsRows = [], statsProvider = null;
  const timers = new Set(), intervals = new Map(), elements = new Map(), events = new Map();
  class Element {
    constructor() { this.children = []; this.style = {}; this.value = ''; this.checked = false; this.disabled = false; this.readyState = 4; }
    append(el) { this.children.push(el); } appendChild(el) { this.append(el); }
    remove() {} replaceChildren() { this.children = []; }
    get firstChild() { return { remove: () => this.children.shift() }; }
    setAttribute(k, v) { this[k] = v; }
    requestVideoFrameCallback(fn) { this.frame = fn; } click() {}
    getContext() { return { drawImage() {} }; }
    toBlob(fn) { fn(new Blob(['fake-jpeg'])); }
  }
  const el = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  for (const [id, value] of Object.entries({ mode: 'gaze', targetSize: '.4', forward: '.12', backward: '.10',
    detector: 'duck', markerId: '0', distanceMode: 'size', targetDistance: '.6' })) el(id).value = value;
  class WS {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 1; WS.last = this; this.sent = []; }
    send(v) { this.sent.push(JSON.parse(v)); } close() { this.readyState = 3; }
    emit(v) { this.onmessage({ data: JSON.stringify(v) }); }
  }
  class Peer {
    constructor() { Peer.last = this; this.connectionState = 'connected'; }
    async getStats() { return statsProvider ? statsProvider() : new Map(statsRows.map((row, n) => [row.id || String(n), row])); }
    close() {} async setRemoteDescription() {} async createAnswer() { return { sdp: 'test' }; }
    async setLocalDescription() {} async addIceCandidate() {}
  }
  const document = { hidden: false, getElementById: el, createElement: () => new Element(),
    createElementNS: () => new Element(), addEventListener: (k, fn) => events.set(k, fn) };
  const context = vm.createContext({ console, URL, Blob, performance: { now: () => clock * 1000 },
    document, location: { hostname: '192.168.2.70' }, WebSocket: WS, RTCPeerConnection: Peer,
    fetch: async (url, request) => {
      if (request && request.method === 'POST') {
        uploads.push({ url, request });
        return { ok: true, json: async () => ({ source: 'aruco', frame_id: Number(new URL(url, 'http://localhost').searchParams.get('frame_id')),
          width: 360, height: 640, boxes: [] }) };
      }
      return { json: async () => ({ robotHost: '', signallingPort: 8443,
      followStyle: 'continuous', arucoHz: 3, detectionMaxAge: .8, arucoHttpTimeout: 2,
      targetTimeout: 2.2, videoTimeout: 1.5, ...settings }) };
    },
    setInterval: (fn, ms) => { intervals.set(ms, fn); return ms; }, clearInterval: ms => intervals.delete(ms),
    setTimeout: (fn, ms) => { const id = setTimeout(() => { timers.delete(id); fn(); }, ms); timers.add(id); return id; },
    clearTimeout: id => { clearTimeout(id); timers.delete(id); },
    addEventListener: (k, fn) => events.set(k, fn), MediaStream: class {
      constructor(tracks) { this.tracks = tracks; }
      getVideoTracks() { return this.tracks.filter(t => t.kind === 'video'); }
    }, AbortController,
  });
  context.window = context;
  for (const file of ['core.js', 'aruco_client.js', 'app.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, file), 'utf8'), context, { filename: file });
  await el('connect').onclick(); const ws = WS.last;
  assert.equal(ws.url, 'ws://192.168.2.70:8443');
  ws.emit({ type: 'welcome' }); await settle(); assert.equal(ws.sent[0].type, 'list');
  ws.emit({ type: 'list', producers: [{ id: 'camera' }] }); await settle(); assert.equal(ws.sent[1].type, 'startSession');
  ws.emit({ type: 'sessionStarted', sessionId: 'demo' }); await settle();
  const channel = { readyState: 'open', bufferedAmount: 0, close() { this.readyState = 'closed'; },
    send(line) {
      const m = JSON.parse(line); sent.push(m); if (m.id === undefined || m.method === drop) return;
      if (replyError && m.method === replyError.method) {
        queueMicrotask(() => channel.onmessage({ data: JSON.stringify({ jsonrpc: '2.0', id: m.id,
          error: { code: -32602, message: replyError.message } }) }));
        return;
      }
      let result = { accepted: true };
      if (m.method === 'media.video') result = { width: 1280, height: 720, rotate: 90 };
      if (m.method === 'robot.subscribe') result = { accepted: true, walk: 'walk.onnx', stand: 'stand.onnx' };
      if (m.method === 'robot.mode') result = { mode: 'walk' };
      if (m.method === 'robot.health') { result = healthReply; clock += healthLatency; }
      if (replyOverrides.has(m.method)) result = replyOverrides.get(m.method);
      queueMicrotask(() => channel.onmessage({ data: JSON.stringify({ jsonrpc: '2.0', id: m.id, result }) }));
    },
    emit(method, params) { this.onmessage({ data: JSON.stringify({ jsonrpc: '2.0', method, params }) }); },
  };
  Peer.last.ondatachannel({ channel }); await settle(); await settle();
  function state(overrides = {}) {
    channel.emit('robot.state', { t: clock + 1, policy: 'walk', head: [0, 0, 0, 0],
      joints: Array(15).fill(0), safety: { fallen: false, limp: false }, ...overrides });
  }
  function frame() { el('video').frame(); }
  async function tick(t, { detection = null, status = true, video = true } = {}) {
    clock = t; if (status) state(typeof status === 'object' ? status : {}); if (video) frame();
    if (detection) channel.emit('media.detections', detection);
    await intervals.get(100)(); await settle();
  }
  return { el, sent, channel, state, frame, tick, events, document, setDrop: name => { drop = name; },
    setError: (method, message) => { replyError = { method, message }; },
    setResult: (method, value) => replyOverrides.set(method, value),
    setHealth: value => { healthReply = value; },
    setHealthLatency: value => { healthLatency = value; },
    setStats: rows => { statsRows = rows; }, setStatsProvider: fn => { statsProvider = fn; },
    pollVideoStats: () => vm.runInContext('pollVideoStats(true)', context),
    videoDiagnostic: () => vm.runInContext('videoDiagnostic()', context),
    track: track => Peer.last.ontrack({ track, streams: [] }),
    pollHealth: () => vm.runInContext('pollHealth()', context),
    pollMode: () => vm.runInContext('pollMode()', context),
    sampleAruco: () => vm.runInContext('sampleAruco()', context), uploads,
    cycle: () => vm.runInContext('({phase: followCycle.phase, captureAfter: followCycle.captureAfter, deadline: followCycle.deadline})', context),
    healthTimer: () => intervals.get(125)(),
    replyHealth: () => {
      const request = sent.filter(x => x.method === 'robot.health').at(-1);
      channel.onmessage({ data: JSON.stringify({ jsonrpc: '2.0', id: request.id, result: healthReply }) });
    },
    clearSent: () => { sent.length = 0; }, setClock: t => { clock = t; },
    marker: (p, t) => vm.runInContext(`acceptDetection(${JSON.stringify(p)}, ${t})`, context),
    arucoError: (message, recoverable) => vm.runInContext(`arucoError(Object.assign(new Error(${JSON.stringify(message)}), { recoverable: ${recoverable} }))`, context),
    close: () => { for (const timer of timers) clearTimeout(timer); } };
}
test('preview receives video/detections but never sends head/move/enable, including disconnect', async () => {
  const h = await harness(); try {
    await h.tick(.1, { detection: picture(.2) }); await h.tick(.6, { detection: picture(.2) });
    h.el('disconnect').onclick();
    assert.equal(h.sent.filter(x => ['robot.head', 'robot.move', 'robot.enable', 'robot.stop'].includes(x.method)).length, 0);
  } finally { h.close(); }
});
test('video metadata rotates the pixels while keeping detections in upright coordinates', async () => {
  const h = await harness(); try {
    assert.equal(h.el('video').className, 'turn90');
    assert.equal(h.el('box').style.aspectRatio, '720/1280');
    await h.tick(.1, { detection: picture() });
    assert.equal(h.el('overlay').viewBox, '0 0 720 1280');
  } finally { h.close(); }
});
test('explicit gaze sends only zero base velocities; hide disarms with an immediate zero', async () => {
  const h = await harness(); try {
    await h.tick(.1); await h.el('start').onclick();
    await h.tick(.2, { detection: picture(.3) });
    for (const t of [.3, .4, .5, .6]) await h.tick(t);
    await h.tick(.7, { detection: picture(.3) });
    assert.ok(h.sent.some(x => x.method === 'robot.head' && x.params.head_yaw < 0));
    assert.ok(h.sent.filter(x => x.method === 'robot.move').every(x => Object.values(x.params).every(v => v === 0)));
    h.document.hidden = true; h.events.get('visibilitychange')();
    assert.equal(h.sent.at(-1).method, 'robot.move'); assert.equal(h.sent.at(-1).params.vx, 0);
    const count = h.sent.length; await h.tick(.8, { detection: picture(.3) }); assert.equal(h.sent.length, count);
  } finally { h.close(); }
});
test('follow approaches only after manual start and confirmation, loss stops in the next tick', async () => {
  const h = await harness(); try {
    await h.tick(.1); h.el('mode').value = 'follow'; h.el('mode').onchange(); await h.el('start').onclick();
    await h.tick(.2, { detection: picture() }); await h.tick(.7, { detection: picture() });
    // A 500 ms timer gap deliberately disarms, so restart after arranging normal 10 Hz cadence.
    await h.el('start').onclick(); await h.tick(.8, { detection: picture() });
    for (const t of [.9, 1, 1.1, 1.2]) await h.tick(t);
    await h.tick(1.3, { detection: picture() });
    assert.ok(h.sent.some(x => x.method === 'robot.move' && x.params.vx > 0));
    await h.tick(1.4, { detection: { width: 720, height: 1280, boxes: [] } });
    assert.equal(h.sent.filter(x => x.method === 'robot.move').at(-1).params.vx, 0);
  } finally { h.close(); }
});
test('stale state, recovery, frozen video and timer stall each stop and require manual restart', async () => {
  for (const reason of ['state', 'recovery', 'video', 'timer']) {
    const h = await harness(); try {
      await h.tick(.1); await h.el('start').onclick(); await h.tick(.2);
      if (reason === 'recovery') { h.setClock(.3); h.state({ safety: { fallen: false, limp: false, fall: { recovery_active: true } } }); }
      else if (reason === 'timer') await h.tick(.8);
      else {
        for (let n = 3; n <= 19; n++) await h.tick(n / 10, { status: reason !== 'state', video: reason !== 'video' });
      }
      assert.equal(h.el('stop').disabled, true, reason);
      assert.equal(h.sent.filter(x => x.method === 'robot.move').at(-1).params.vx, 0);
    } finally { h.close(); }
  }
});
test('repeated state timestamp cannot keep watchdog alive', async () => {
  const h = await harness(); try {
    await h.tick(.1); await h.el('start').onclick();
    for (let n = 2; n <= 13; n++) { h.channel.emit('robot.state', { t: 1.1 }); await h.tick(n / 10, { status: false }); }
    assert.equal(h.el('stop').disabled, true);
  } finally { h.close(); }
});
test('parameters stop before changing mode; enable is explicit, errors are surfaced', async () => {
  const h = await harness(); try {
    await h.tick(.1); await h.el('start').onclick(); await h.tick(.2);
    h.el('mode').value = 'follow'; h.el('mode').onchange(); assert.equal(h.el('stop').disabled, true);
    assert.equal(h.sent.some(x => x.method === 'robot.enable'), false);
    await h.el('enable').onclick(); assert.equal(h.sent.filter(x => x.method === 'robot.enable').length, 1);
    assert.equal(h.el('start').disabled, true, 'queued recovery must not arm gaze');
    h.setClock(.3); h.state({ policy: 'standup' });
    assert.equal(h.el('start').disabled, false);
  } finally { h.close(); }
});
test('control response error disarms instead of silently continuing', async () => {
  const h = await harness(); try {
    await h.tick(.1); await h.el('start').onclick();
    h.channel.emit(undefined, undefined);
    h.channel.onmessage({ data: JSON.stringify({ jsonrpc: '2.0', id: null, error: { message: 'daemon refused' } }) });
    assert.equal(h.el('stop').disabled, true);
    assert.match(h.el('status').textContent, /daemon refused/);
  } finally { h.close(); }
});
test('missing RPC acknowledgement stops, even with fresh states and frames', async () => {
  const h = await harness(); try {
    await h.tick(.1); await h.el('start').onclick(); h.setDrop('robot.head'); await h.tick(.2);
    await new Promise(resolve => setTimeout(resolve, 760));
    assert.equal(h.el('stop').disabled, true);
    assert.match(h.el('status').textContent, /robot.head.*超时/);
    assert.equal(h.sent.filter(x => x.method === 'robot.move').at(-1).params.vx, 0);
  } finally { h.close(); }
});
test('old stand_up schema reports both gateway and daemon, without retrying ordinary enable', async () => {
  const h = await harness(); try {
    await h.tick(.1);
    h.setError('robot.enable', 'unknown field `stand_up`, expected `on` or `toggle`');
    await h.el('enable').onclick();
    const enables = h.sent.filter(x => x.method === 'robot.enable');
    assert.equal(enables.length, 1);
    assert.equal(enables[0].params.stand_up, true);
    assert.match(h.el('status').textContent, /mediad.*robotd/);
    assert.equal(h.el('enable').disabled, true);
    assert.equal(h.el('stop').disabled, true);
    assert.equal(h.sent.some(x => ['robot.init', 'robot.do', 'robot.relax'].includes(x.method)), false);
    assert.ok(h.sent.filter(x => x.method === 'robot.move').every(x => x.params.vx === 0 && x.params.vy === 0 && x.params.vyaw === 0));
    h.el('disconnect').onclick();
  } finally { h.close(); }
});
test('other enable refusals retain the original reason and do not claim a schema mismatch', async () => {
  const h = await harness(); try {
    await h.tick(.1); h.setError('robot.enable', 'no stand-up policy loaded');
    await h.el('enable').onclick();
    assert.equal(h.el('status').textContent, 'no stand-up policy loaded');
    assert.equal(h.el('enable').disabled, false);
    assert.equal(h.sent.filter(x => x.method === 'robot.enable').length, 1);
  } finally { h.close(); }
});
test('ArUco mode ignores duck packets, locks ID 0 and remains preview-only', async () => {
  const h = await harness({ detector: 'aruco', markerId: 0, aruco: { markerMm: 50, calibrated: false } });
  try {
    await h.tick(.1, { detection: picture(.5) });
    assert.match(h.el('metrics').textContent, /目标高度 –%/);
    const p = picture(.3); p.boxes[0].id = 1; h.marker(p, .1); await h.tick(.2);
    assert.match(h.el('metrics').textContent, /目标高度 –%/);
    p.boxes[0].id = 0; h.marker(p, .2); h.marker(p, .3); await h.tick(.3);
    assert.doesNotMatch(h.el('metrics').textContent, /目标高度 –%/);
    assert.equal(h.sent.some(x => ['robot.head', 'robot.move', 'robot.enable'].includes(x.method)), false);
  } finally { h.close(); }
});
test('uncalibrated ArUco cannot start metric follow or silently use a default size', async () => {
  const h = await harness({ detector: 'aruco', markerId: 0, aruco: { markerMm: 50, calibrated: false } });
  try {
    await h.tick(.1); h.el('mode').value = 'follow'; h.el('mode').onchange(); await h.el('start').onclick();
    assert.match(h.el('status').textContent, /将当前距离设为跟随距离/);
    h.el('distanceMode').value = 'meters'; h.el('distanceMode').onchange(); await h.el('start').onclick();
    assert.match(h.el('status').textContent, /米制跟随需要/);
    assert.equal(h.sent.some(x => x.method === 'robot.head'), false);
  } finally { h.close(); }
});
test('calibrated ArUco metric follow stops when the pose becomes invalid', async () => {
  const h = await harness({ detector: 'aruco', markerId: 0, aruco: { markerMm: 50, calibrated: true } });
  try {
    await h.tick(.1); h.el('mode').value = 'follow'; h.el('mode').onchange(); await h.el('start').onclick();
    const p = picture(); Object.assign(p.boxes[0], { id: 0, distance_status: 'calibrated', distance_m: 1 });
    h.marker(p, .2); await h.tick(.2); h.marker(p, .3); await h.tick(.3);
    assert.ok(h.sent.some(x => x.method === 'robot.move' && x.params.vx > 0));
    p.boxes[0].distance_status = 'pose_rejected'; delete p.boxes[0].distance_m;
    h.marker(p, .4); await h.tick(.4);
    assert.equal(h.sent.filter(x => x.method === 'robot.move').at(-1).params.vx, 0);
  } finally { h.close(); }
});
test('temporary ArUco errors stop immediately and resume only after two fresh detections', async () => {
  const h = await harness({ detector: 'aruco', markerId: 0, aruco: { markerMm: 50, calibrated: true } });
  try {
    await h.tick(.1); h.el('mode').value = 'follow'; h.el('mode').onchange(); await h.el('start').onclick();
    const p = picture(); Object.assign(p.boxes[0], { id: 0, distance_status: 'calibrated', distance_m: 1 });
    h.marker(p, .2); await h.tick(.2); h.marker(p, .3); await h.tick(.3);
    assert.ok(h.sent.some(x => x.method === 'robot.move' && x.params.vx > 0));
    h.setClock(.35); h.arucoError('temporary timeout', true);
    assert.equal(h.sent.at(-1).method, 'robot.move'); assert.equal(h.sent.at(-1).params.vx, 0);
    assert.equal(h.el('stop').disabled, false, 'transient error keeps manual control armed');
    h.clearSent(); await h.tick(.4); h.marker(p, .5); await h.tick(.5);
    assert.ok(h.sent.filter(x => x.method === 'robot.move').every(x => x.params.vx === 0));
    h.marker(p, .6); await h.tick(.6);
    assert.ok(h.sent.some(x => x.method === 'robot.move' && x.params.vx > 0));
    h.arucoError('invalid frame protocol', false);
    assert.equal(h.el('stop').disabled, true, 'hard error still requires manual restart');
    h.clearSent(); h.marker(p, .7); await h.tick(.7); h.marker(p, .8); await h.tick(.8);
    assert.equal(h.sent.some(x => x.method === 'robot.move' && x.params.vx > 0), false);
  } finally { h.close(); }
});

test('ArUco errors during preview never send motor commands', async () => {
  const h = await harness({ detector: 'aruco', markerId: 0, aruco: { markerMm: 50, calibrated: false } });
  try {
    await h.tick(.1); h.arucoError('temporary timeout', true); await h.tick(.2);
    h.arucoError('disabled detector', false);
    assert.equal(h.sent.some(x => ['robot.move', 'robot.head', 'robot.enable'].includes(x.method)), false);
  } finally { h.close(); }
});

test('repeated-but-ready IMU sample permits gaze only with fresh health evidence', async () => {
  const h = await harness();
  const status = { safety: { fallen: false, limp: false, fall: {
    recovery_active: false, recovery_failed: false, imu_ready: true, imu_fresh: false } } };
  try {
    h.setHealth({ healthy: true, imu: { ready: true, consecutive_stale_blocks: 1 }, bus: { consecutive_errors: 0 } });
    await h.tick(.1, { status }); await h.el('start').onclick();
    assert.equal(h.el('stop').disabled, false);
    for (let i = 2; i <= 10; i++) {
      h.setClock(i / 10); await h.pollHealth();
      await h.tick(i / 10, { status, detection: i % 3 === 0 ? picture(.3) : null });
    }
    assert.equal(h.el('stop').disabled, false);
    assert.ok(h.sent.some(x => x.method === 'robot.head' && x.params.head_yaw < 0));
    assert.ok(h.sent.filter(x => x.method === 'robot.move').every(x => x.params.vx === 0));
  } finally { h.close(); }
});
test('health proof expiring stops gaze; it is not a permanent IMU bypass', async () => {
  const h = await harness();
  const status = { safety: { fallen: false, limp: false, fall: {
    recovery_active: false, recovery_failed: false, imu_ready: true, imu_fresh: false } } };
  try {
    await h.tick(.1, { status }); await h.el('start').onclick();
    for (let i = 2; i <= 8; i++) await h.tick(i / 10, { status });
    assert.equal(h.el('stop').disabled, true);
    assert.match(h.el('status').textContent, /0.5 秒/);
    assert.equal(h.sent.filter(x => x.method === 'robot.move').at(-1).params.vx, 0);
  } finally { h.close(); }
});
test('real IMU dropout blocks start and power requests even if the duck looks upright', async () => {
  const h = await harness(); try {
    await h.tick(.1);
    h.setHealth({ healthy: false, reason: 'BNO085 has no fresh quaternion and gyro sample',
      imu: { ready: false, consecutive_stale_blocks: 100 } });
    await h.el('start').onclick();
    assert.match(h.el('status').textContent, /IMU 驱动未就绪/);
    await h.el('enable').onclick();
    assert.equal(h.sent.some(x => ['robot.enable', 'robot.head', 'robot.move', 'robot.init'].includes(x.method)), false);
    assert.match(h.el('log').children.map(x => x.textContent).join('\n'), /起身前健康检查/);
  } finally { h.close(); }
});
test('recovery_active is reported explicitly and cannot be overridden by healthy IMU', async () => {
  const h = await harness(); try {
    await h.tick(.1, { status: { safety: { fallen: false, limp: false,
      fall: { recovery_active: true, recovery_failed: false, imu_ready: true, imu_fresh: true } } } });
    await h.el('start').onclick();
    assert.match(h.el('status').textContent, /recovery_active=true/);
    assert.equal(h.sent.some(x => x.method === 'robot.head'), false);
  } finally { h.close(); }
});
test('a read-only health check never enables or moves the robot', async () => {
  const h = await harness(); try {
    await h.tick(.1); await h.el('diagnose').onclick();
    assert.equal(h.sent.some(x => ['robot.enable', 'robot.head', 'robot.move', 'robot.init'].includes(x.method)), false);
    assert.match(h.el('log').children.map(x => x.textContent).join('\n'), /机器人健康检查/);
  } finally { h.close(); }
});
test('a delayed health reply is aged from query time and cannot revalidate a repeated sample', async () => {
  const h = await harness(); try {
    await h.tick(.1, { status: { safety: { fallen: false, limp: false,
      fall: { recovery_active: false, recovery_failed: false, imu_ready: true, imu_fresh: false } } } });
    h.setHealthLatency(.6); await h.el('start').onclick();
    assert.match(h.el('status').textContent, /0.5 秒/);
    assert.equal(h.sent.some(x => ['robot.head', 'robot.move', 'robot.enable'].includes(x.method)), false);
  } finally { h.close(); }
});

test('Start drains an aged preview health request and obtains a new proof before arming', async () => {
  const h = await harness(); try {
    h.setDrop('robot.health'); await h.tick(.1);
    const oldQuery = h.pollHealth();
    h.setClock(.55); h.state({ safety: { fallen: false, limp: false,
      fall: { recovery_active: false, recovery_failed: false, imu_ready: true, imu_fresh: false } } }); h.frame();
    const start = h.el('start').onclick();
    h.setDrop(null); h.replyHealth(); await oldQuery; await start;
    assert.equal(h.el('stop').disabled, false);
    assert.match(h.el('log').children.map(x => x.textContent).join('\n'), /重新获取启动健康确认/);
    assert.equal(h.sent.filter(x => x.method === 'robot.health').length, 3, 'initial, inherited preview, new startup proof');
    assert.equal(h.sent.some(x => x.method === 'robot.enable'), false);
  } finally { h.close(); }
});

test('scheduled health refresh sustains repeated IMU samples with bounded query rate', async () => {
  const h = await harness();
  const status = { safety: { fallen: false, limp: false,
    fall: { recovery_active: false, recovery_failed: false, imu_ready: true, imu_fresh: false } } };
  try {
    await h.tick(.1, { status }); await h.el('start').onclick(); h.clearSent();
    for (let n = 2; n <= 20; n++) {
      h.setClock(n / 10); await h.healthTimer();
      await h.tick(n / 10, { status, detection: n % 3 === 0 ? picture(.3) : null });
    }
    assert.equal(h.el('stop').disabled, false);
    const queries = h.sent.filter(x => x.method === 'robot.health').length;
    assert.ok(queries >= 5 && queries <= 10);
    assert.ok(h.sent.some(x => x.method === 'robot.head'));
    assert.match(h.el('healthSummary').textContent, /健康确认年龄=.*查询往返=/);
  } finally { h.close(); }
});

test('health timer never queues another request while one is pending', async () => {
  const h = await harness(); try {
    h.setDrop('robot.health'); h.setClock(3);
    const query = h.pollHealth(); const count = h.sent.length;
    for (const t of [3.2, 3.4, 3.6]) { h.setClock(t); await h.healthTimer(); }
    assert.equal(h.sent.length, count);
    h.replyHealth(); await query;
    h.setDrop(null);
    await h.el('diagnose').onclick();
    assert.match(h.el('log').children.map(x => x.textContent).join('\n'), /health_diagnostic/);
    assert.equal(h.sent.some(x => ['robot.move', 'robot.head', 'robot.enable'].includes(x.method)), false);
  } finally { h.close(); }
});

test('an active delayed health query renews without waiting for the next timer slot', async () => {
  const h = await harness(); try {
    await h.tick(.1); await h.el('start').onclick(); h.clearSent();
    h.setDrop('robot.health'); h.setClock(.2); const query = h.pollHealth();
    h.setClock(.45); h.replyHealth(); await query;
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(h.sent.filter(x => x.method === 'robot.health').length, 2);
    // The new request remains in flight; later timer slots must not queue more.
    h.setClock(.5); await h.healthTimer();
    assert.equal(h.sent.filter(x => x.method === 'robot.health').length, 2);
    h.setDrop(null); h.replyHealth(); await settle();
    assert.equal(h.el('stop').disabled, false);
  } finally { h.close(); }
});

const slowVision = { detector: 'aruco', markerId: 0, aruco: { markerMm: 50, calibrated: true },
  followStyle: 'step', followStepSeconds: .8, arucoHz: .5, detectionMaxAge: 5, arucoHttpTimeout: 8, targetTimeout: 12, videoTimeout: 10 };
test('visible step recipe sustains full speed despite head/target offset, then stops before the next detection', async () => {
  const h = await harness({ ...slowVision, followStepSeconds: 2.5, followRequireFront: false });
  try {
    await h.tick(.1, { status: { head: [0, 0, .4, 0] } });
    h.el('mode').value = 'follow'; h.el('mode').onchange(); await h.el('start').onclick();
    await pump(h, 2, 12);
    const p = picture(.5); p.source = 'aruco'; Object.assign(p.boxes[0], { id: 0, distance_status: 'calibrated', distance_m: 1 });
    h.setClock(1.3); h.marker(p, 1.3); await h.tick(1.3);
    h.setClock(1.4); h.marker(p, 1.4); await h.tick(1.4); assert.equal(h.cycle().phase, 'moving');
    const speeds = [];
    for (let n = 15; n <= 38; n++) {
      h.setClock(n / 10); if (n % 20 === 0) await h.pollMode();
      await h.tick(n / 10); speeds.push(h.sent.filter(x => x.method === 'robot.move').at(-1).params.vx);
    }
    assert.equal(h.cycle().phase, 'moving');
    assert.ok(speeds.slice(3).every(v => Math.abs(v - .12) < 1e-8));
    assert.ok(speeds.reduce((total, v) => total + v * .1, 0) > .2, 'commanded travel is substantial, not a subsecond startup pulse');
    await h.tick(4); assert.equal(h.sent.filter(x => x.method === 'robot.move').at(-1).params.vx, 0);
    assert.equal(h.cycle().phase, 'settling'); h.clearSent(); await pump(h, 41, 60);
    assert.ok(h.sent.filter(x => x.method === 'robot.move').every(x => x.params.vx === 0), 'no new move without a new stopped-frame decision');
    const logs = h.el('log').children.map(x => x.textContent).join('\n');
    assert.match(logs, /本段行走结束/); assert.match(logs, /peak_command_abs_vx/);
  } finally { h.close(); }
});

function markerPacket() {
  const p = picture(); p.source = 'aruco';
  Object.assign(p.boxes[0], { id: 0, distance_status: 'calibrated', distance_m: 1 }); return p;
}
async function pump(h, first, last, options) {
  for (let n = first; n <= last; n++) {
    h.setClock(n / 10); if (n % 20 === 0) await h.pollMode();
    await h.tick(n / 10, options);
  }
}

test('slow ArUco follow stops for detection, moves once, then rejects all pre-stop results', async () => {
  const h = await harness(slowVision); try {
    Object.assign(h.el('video'), { videoWidth: 1280, videoHeight: 720, currentTime: 1 });
    await h.tick(.1); h.el('mode').value = 'follow'; h.el('mode').onchange(); await h.el('start').onclick();
    await h.sampleAruco(); assert.equal(h.uploads.length, 0, 'no capture before zero acknowledgement');
    await h.tick(.2); await h.sampleAruco(); assert.equal(h.cycle().phase, 'settling');
    await pump(h, 3, 13);
    const p = markerPacket(); h.marker(p, .9);
    assert.equal(h.cycle().phase, 'detecting');
    await pump(h, 14, 33); h.setClock(3.4); h.marker(p, 1.4); await h.tick(3.4);
    assert.equal(h.sent.some(x => x.method === 'robot.move' && x.params.vx > 0), false, 'one slow result is not enough');
    await pump(h, 35, 54); h.setClock(5.5); h.marker(p, 3.5); await h.tick(5.5);
    assert.equal(h.cycle().phase, 'moving'); await h.tick(5.6);
    assert.ok(h.sent.some(x => x.method === 'robot.move' && x.params.vx > 0));
    await h.sampleAruco(); assert.equal(h.uploads.length, 0, 'no upload while moving');
    await pump(h, 57, 64);
    assert.equal(h.sent.filter(x => x.method === 'robot.move').at(-1).params.vx, 0);
    assert.equal(h.cycle().phase, 'settling'); h.clearSent();
    h.marker(p, 3.5); await pump(h, 65, 90);
    assert.ok(h.sent.filter(x => x.method === 'robot.move').every(x => x.params.vx === 0), 'old detections cannot repeat movement');
    assert.equal(h.cycle().phase, 'detecting');
    h.el('video').currentTime++; await h.sampleAruco(); assert.equal(h.uploads.length, 1, 'next detection is captured only after stop/settle');
  } finally { h.close(); }
});

test('low-rate video is tolerated but a pre-stop decoded frame cannot authorize a new capture', async () => {
  const h = await harness(slowVision); try {
    Object.assign(h.el('video'), { videoWidth: 1280, videoHeight: 720, currentTime: 1 });
    await h.tick(.1); h.el('mode').value = 'follow'; h.el('mode').onchange(); await h.el('start').onclick();
    await h.tick(.2); await pump(h, 3, 30, { video: false }); await h.sampleAruco();
    assert.equal(h.el('stop').disabled, false, '2.8s between video frames no longer trips the old 1.5s limit');
    assert.equal(h.cycle().phase, 'settling'); assert.equal(h.uploads.length, 0);
    h.marker(markerPacket(), 2.5); await h.tick(3.1, { video: false });
    assert.ok(h.sent.filter(x => x.method === 'robot.move').every(x => x.params.vx === 0));
    await h.tick(3.2); await h.sampleAruco(); assert.equal(h.uploads.length, 1);
  } finally { h.close(); }
});

test('IMU failure cancels segmented follow and never resumes from later detections', async () => {
  const h = await harness(slowVision); try {
    await h.tick(.1); h.el('mode').value = 'follow'; h.el('mode').onchange(); await h.el('start').onclick();
    await pump(h, 2, 13); h.marker(markerPacket(), 1.3); await h.tick(1.4);
    h.marker(markerPacket(), 1.4); await h.tick(1.5); await h.tick(1.6);
    assert.equal(h.cycle().phase, 'moving');
    h.setHealth({ healthy: false, imu: { ready: false, consecutive_stale_blocks: 20 } }); await h.pollHealth();
    assert.equal(h.el('stop').disabled, true); assert.equal(h.cycle().phase, 'idle');
    assert.equal(h.sent.filter(x => x.method === 'robot.move').at(-1).params.vx, 0);
    h.clearSent(); h.marker(markerPacket(), 1.7); await h.tick(1.7);
    assert.equal(h.sent.some(x => ['robot.move', 'robot.enable'].includes(x.method)), false);
  } finally { h.close(); }
});

test('missing zero-speed acceptance prevents capture and disarms segmented follow', async () => {
  const h = await harness(slowVision); try {
    await h.tick(.1); h.el('mode').value = 'follow'; h.el('mode').onchange(); await h.el('start').onclick();
    h.setResult('robot.move', {}); await h.tick(.2);
    assert.equal(h.el('stop').disabled, true); assert.equal(h.cycle().phase, 'idle');
    assert.match(h.el('status').textContent, /未确认零速度/);
    assert.equal(h.sent.some(x => x.method === 'robot.move' && x.params.vx !== 0), false);
  } finally { h.close(); }
});

function protectedState(failed = false) {
  return { policy: 'protection', safety: { fallen: true, limp: false, gain: null,
    gravity: [.9373, .3093, -.1605], fall: { imu_ready: true, imu_fresh: false,
      recovery_active: false, recovery_failed: failed } } };
}
test('manual StandUp from fall protection is delegated once to daemon; gaze still waits', async () => {
  for (const failed of [false, true]) {
    const h = await harness(); try {
      await h.tick(.1, { status: protectedState(failed) });
      await h.el('start').onclick();
      assert.equal(h.sent.some(x => x.method === 'robot.enable'), false);
      assert.match(h.el('status').textContent, /保护/);
      await h.el('enable').onclick(); await h.el('enable').onclick();
      const requests = h.sent.filter(x => x.method === 'robot.enable');
      assert.equal(requests.length, 1); assert.equal(requests[0].params.stand_up, true);
      assert.equal(requests[0].params.on, true); assert.equal(requests[0].params.toggle, false);
      assert.match(h.el('status').textContent, /已入队/);
      assert.equal(h.sent.some(x => x.method === 'robot.head'), false);
      h.setClock(.2); h.state({ policy: 'standup', safety: { fallen: true, limp: false,
        fall: { imu_ready: true, recovery_active: true, recovery_failed: false } } });
      assert.match(h.el('status').textContent, /robotd 已开始起身/);
      await h.el('enable').onclick();
      assert.equal(h.sent.filter(x => x.method === 'robot.enable').length, 1, 'running recovery cannot be queued again');
      assert.match(h.el('status').textContent, /起身已经在执行/);
      await h.tick(.3); assert.equal(h.sent.some(x => x.method === 'robot.head'), false, 'no automatic gaze after recovery');
    } finally { h.close(); }
  }
});

test('daemon overcurrent refusal is shown without ordinary enable or automatic retry', async () => {
  const h = await harness(); try {
    await h.tick(.1, { status: protectedState() });
    h.setResult('robot.enable', { accepted: false, reason: 'overcurrent protection latched; clear the fault and restart robotd' });
    await h.el('enable').onclick();
    assert.match(h.el('status').textContent, /overcurrent protection latched/);
    const requests = h.sent.filter(x => x.method === 'robot.enable');
    assert.equal(requests.length, 1); assert.equal(requests[0].params.stand_up, true);
    assert.equal(h.sent.some(x => x.method === 'robot.head'), false);
  } finally { h.close(); }
});

test('accepted recovery is not called started while daemon remains in protection', async () => {
  const h = await harness(); try {
    await h.tick(.1, { status: protectedState() }); await h.el('enable').onclick();
    await h.tick(3.2, { status: protectedState() });
    assert.match(h.el('status').textContent, /未观察到 robotd 开始恢复/);
    assert.equal(h.sent.filter(x => x.method === 'robot.enable').length, 1);
    assert.equal(h.sent.some(x => x.method === 'robot.head'), false);
  } finally { h.close(); }
});

test('fresh health ready cannot override state imu_ready=false for manual recovery', async () => {
  const h = await harness(); try {
    const status = protectedState(); status.safety.fall.imu_ready = false;
    await h.tick(.1, { status }); await h.el('enable').onclick();
    assert.match(h.el('status').textContent, /imu_ready=false/);
    assert.equal(h.sent.some(x => x.method === 'robot.enable'), false);
  } finally { h.close(); }
});

test('presented-frame counters keep healthy video alive when frame callbacks stall', async () => {
  const h = await harness(); let count = 0;
  try {
    Object.assign(h.el('video'), { paused: false, getVideoPlaybackQuality: () => ({ totalVideoFrames: count, droppedVideoFrames: 0 }) });
    await h.tick(.1); await h.el('start').onclick();
    for (let n = 2; n <= 25; n++) { count++; await h.tick(n / 10, { video: false }); }
    assert.equal(h.el('stop').disabled, false);
    assert.equal(h.videoDiagnostic().frame_proof, 'presented_counter');
    assert.ok(h.videoDiagnostic().frame_age_ms <= 110);
  } finally { h.close(); }
});

test('dropped frames, transport packets and currentTime do not keep frozen pixels alive', async () => {
  const h = await harness(); let total = 0;
  try {
    Object.assign(h.el('video'), { paused: false, getVideoPlaybackQuality: () => ({ totalVideoFrames: total, droppedVideoFrames: total }) });
    await h.tick(.1); await h.el('start').onclick();
    for (let n = 2; n <= 20; n++) {
      total++; h.el('video').currentTime = n;
      h.setStats([{ id: 'v', type: 'inbound-rtp', kind: 'video', bytesReceived: n * 10000,
        packetsReceived: n * 10, framesReceived: n, framesDecoded: n, framesDropped: n }]);
      await h.pollVideoStats(); await h.tick(n / 10, { video: false });
    }
    assert.equal(h.el('stop').disabled, true);
    assert.match(h.el('status').textContent, /视频无新画面.*期限 1.5 秒/);
    assert.equal(h.sent.filter(x => x.method === 'robot.move').at(-1).params.vx, 0);
    const logs = h.el('log').children.map(x => x.textContent).join('\n');
    assert.match(logs, /video_diagnostic/); assert.match(logs, /bytesReceived/);
    total = 100; h.el('video').getVideoPlaybackQuality = () => ({ totalVideoFrames: total++, droppedVideoFrames: 0 });
    await h.tick(2.1, { video: false }); await h.tick(2.2, { video: false });
    assert.equal(h.el('stop').disabled, true, 'fresh video alone does not rearm control');
  } finally { h.close(); }
});

test('quality counter reset and invalid counters cannot manufacture a new frame', async () => {
  const h = await harness(); let total = 100, dropped = 0;
  try {
    Object.assign(h.el('video'), { paused: false, getVideoPlaybackQuality: () => ({ totalVideoFrames: total, droppedVideoFrames: dropped }) });
    await h.tick(.1); await h.el('start').onclick();
    total = 0; await h.tick(.2, { video: false });
    dropped = 10;
    await pump(h, 3, 20, { video: false });
    assert.equal(h.el('stop').disabled, true); assert.equal(h.videoDiagnostic().playback, null);
  } finally { h.close(); }
});

test('manual video resume is read-only and does not count playback as a fresh frame', async () => {
  const h = await harness(); let plays = 0;
  try {
    h.el('video').paused = true;
    h.el('video').play = async () => { plays++; h.el('video').paused = false; };
    await h.el('resumeVideo').onclick(); await h.el('diagnose').onclick();
    assert.equal(plays, 1);
    assert.equal(h.sent.some(x => ['robot.enable', 'robot.head', 'robot.move'].includes(x.method)), false);
    assert.equal(h.videoDiagnostic().frame_age_ms, null);
  } finally { h.close(); }
});

test('late WebRTC statistics from a disconnected peer cannot repopulate diagnostics', async () => {
  const h = await harness(); let resolveStats;
  try {
    h.setStatsProvider(() => new Promise(resolve => { resolveStats = resolve; }));
    const query = h.pollVideoStats(); await settle(); h.el('disconnect').onclick();
    resolveStats(new Map([['v', { id: 'v', type: 'inbound-rtp', kind: 'video', bytesReceived: 123 }]]));
    await query; assert.equal(h.videoDiagnostic().webrtc, null);
    assert.equal(h.videoDiagnostic().frame_age_ms, null);
  } finally { h.close(); }
});

test('connection defaults to video only; a separate detection preview toggle never moves the robot', async () => {
  const h = await harness(slowVision); try {
    Object.assign(h.el('video'), { videoWidth: 1280, videoHeight: 720, currentTime: 1 });
    for (let n = 1; n <= 25; n++) { await h.tick(n / 10); await h.sampleAruco(); }
    assert.equal(h.uploads.length, 0); assert.equal(h.el('previewDetection').checked, false);
    h.el('previewDetection').checked = true; h.el('previewDetection').onchange();
    await h.tick(2.6); await h.sampleAruco(); assert.equal(h.uploads.length, 1);
    h.el('previewDetection').checked = false; h.el('previewDetection').onchange();
    await h.tick(5); h.el('video').currentTime++; await h.sampleAruco();
    assert.equal(h.uploads.length, 1);
    assert.equal(h.sent.some(x => ['robot.enable', 'robot.head', 'robot.move'].includes(x.method)), false);
  } finally { h.close(); }
});

test('preview never rechecks frozen pixels merely because currentTime keeps advancing', async () => {
  const h = await harness(slowVision); try {
    Object.assign(h.el('video'), { videoWidth: 1280, videoHeight: 720, currentTime: 1 });
    await h.tick(.1); h.el('previewDetection').checked = true; h.el('previewDetection').onchange();
    await h.sampleAruco(); assert.equal(h.uploads.length, 1);
    h.el('video').currentTime++; await h.tick(2.2, { video: false }); await h.sampleAruco();
    assert.equal(h.uploads.length, 1);
    h.el('video').currentTime++; await h.tick(4.3); await h.sampleAruco();
    assert.equal(h.uploads.length, 2);
  } finally { h.close(); }
});

test('audio and duplicate track notifications cannot replace or reset a playing video track', async () => {
  const h = await harness(); try {
    const videoTrack = { kind: 'video', id: 'camera', readyState: 'live', muted: false };
    const audioTrack = { kind: 'audio', id: 'mic', readyState: 'live', muted: false };
    h.track(videoTrack); const stream = h.el('video').srcObject;
    assert.equal(stream.getVideoTracks()[0], videoTrack);
    await h.tick(.1); h.track(audioTrack); h.track(videoTrack);
    assert.equal(h.el('video').srcObject, stream);
    assert.notEqual(h.videoDiagnostic().frame_age_ms, null);
    assert.equal(h.sent.some(x => ['robot.enable', 'robot.head', 'robot.move'].includes(x.method)), false);
  } finally { h.close(); }
});

test('preview preference is independent of active gaze and never stops or resets control', async () => {
  const h = await harness(slowVision); try {
    Object.assign(h.el('video'), { videoWidth: 1280, videoHeight: 720, currentTime: 1 });
    await h.tick(.1); await h.el('start').onclick(); assert.equal(h.el('previewDetection').checked, false);
    await h.tick(.2); h.clearSent();
    for (const checked of [true, false]) {
      h.el('previewDetection').checked = checked; h.el('previewDetection').onchange();
      assert.equal(h.el('stop').disabled, false);
      assert.match(h.el('detectionState').textContent, /正在为注视提供检测/);
      assert.equal(h.sent.length, 0, 'preview preference emits no command while gaze is active');
    }
    await h.sampleAruco(); assert.equal(h.uploads.length, 1, 'gaze still detects with preview unchecked');
    const recognition = h.videoDiagnostic().recognition;
    assert.equal(recognition.preview_enabled, false); assert.equal(recognition.control_active, true);
    assert.equal(recognition.enabled, true);
    h.el('stop').onclick(); assert.equal(h.el('stop').disabled, true);
    assert.equal(h.sent.filter(x => x.method === 'robot.move').at(-1).params.vx, 0);
    h.clearSent(); h.el('video').currentTime++; await h.tick(2.5); await h.sampleAruco();
    assert.equal(h.uploads.length, 1, 'after stopping, unchecked preview does not keep detecting');
    assert.equal(h.sent.some(x => ['robot.move', 'robot.head', 'robot.enable'].includes(x.method)), false);
  } finally { h.close(); }
});

test('checked preview resumes only passive recognition after explicit control stop', async () => {
  const h = await harness(slowVision); try {
    Object.assign(h.el('video'), { videoWidth: 1280, videoHeight: 720, currentTime: 1 });
    await h.tick(.1); h.el('previewDetection').checked = true; h.el('previewDetection').onchange();
    await h.el('start').onclick(); assert.equal(h.el('previewDetection').checked, true);
    await h.tick(.2); await h.sampleAruco(); assert.equal(h.uploads.length, 1);
    h.el('stop').onclick(); h.clearSent();
    h.el('video').currentTime++; await h.tick(2.5); await h.sampleAruco();
    assert.equal(h.uploads.length, 2); assert.equal(h.el('stop').disabled, true);
    assert.match(h.el('detectionState').textContent, /仅识别预览/);
    assert.equal(h.sent.some(x => ['robot.move', 'robot.head', 'robot.enable'].includes(x.method)), false);
  } finally { h.close(); }
});
