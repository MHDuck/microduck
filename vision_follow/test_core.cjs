const test = require('node:test');
const assert = require('node:assert/strict');
const { Tracker, boxesFrom, stateProblem, imuEvidenceProblem } = require('./core.js');
function picture(x = 0, y = 0, size = .4, w = 720, h = 1280) {
  return { width: w, height: h, boxes: [{ x0: (x + 1) * w / 2 - w * .1,
    x1: (x + 1) * w / 2 + w * .1, y0: (y + 1) * h / 2 - h * size / 2,
    y1: (y + 1) * h / 2 + h * size / 2, score: 1.3 }] };
}
function acquire(tracker, p) { tracker.ingest(p, 0); tracker.ingest(p, .5); }
test('front alignment is optional without changing distance or head limits', () => {
  for (const frontGate of [true, false]) {
    const t = new Tracker({ frontGate }); t.reset([0, 0, .4, 0]); acquire(t, picture(.5, 0, .2));
    const out = t.output(.6, 'follow', .1);
    assert.equal(out.desiredVx, frontGate ? 0 : .12);
    assert.ok(Math.abs(out.head[2]) <= .6);
  }
  assert.throws(() => new Tracker({ frontGate: 'false' }));
});

test('disabling alignment still rejects clipped targets and unavailable metric pose', () => {
  const t = new Tracker({ frontGate: false }); acquire(t, picture(.85, 0, .2));
  assert.equal(t.output(.6, 'follow', .1).desiredVx, 0);
  const metric = new Tracker({ frontGate: false, distanceMode: 'meters' }); acquire(metric, picture(.5, 0, .2));
  assert.equal(metric.output(.6, 'follow', .1).desiredVx, 0);
});

test('normalized errors are invariant to portrait/landscape resolution', () => {
  const a = boxesFrom(picture(.3, -.2, .3))[0], b = boxesFrom(picture(.3, -.2, .3, 1280, 720))[0];
  for (const k of ['x', 'y', 'size']) assert.ok(Math.abs(a[k] - b[k]) < 1e-12);
});
test('new target requires two frames; gaze never translates', () => {
  const t = new Tracker(); t.ingest(picture(.4), 0);
  assert.equal(t.output(0, 'follow', .1).valid, false);
  t.ingest(picture(.4), .5);
  const out = t.output(.5, 'gaze', .1);
  assert.equal(out.vx, 0); assert.ok(out.head[2] < 0); assert.equal(out.head[1], 0);
  assert.ok(Math.abs(out.head[2]) <= .035 + 1e-12);
});
test('head integrates once per detection, not on every timer tick', () => {
  const t = new Tracker(); acquire(t, picture(.5));
  const aim = t.aim[2]; for (let i = 0; i < 6; i++) t.output(.5 + i * .1, 'gaze', .1);
  assert.equal(t.aim[2], aim); assert.ok(Math.abs(t.head[2]) <= Math.abs(aim) + 1e-12);
});
test('vertical tracking is opt-in and correction directions can be reversed', () => {
  const a = new Tracker({ vertical: true }), b = new Tracker({ vertical: true, invertPitch: true, invertYaw: true });
  acquire(a, picture(.3, .3, .3)); acquire(b, picture(.3, .3, .3));
  const x = a.output(.5, 'gaze', .1), y = b.output(.5, 'gaze', .1);
  assert.ok(x.head[1] > 0 && x.head[2] < 0); assert.equal(x.head[1], -y.head[1]); assert.equal(x.head[2], -y.head[2]);
});
test('small target approaches, large target retreats, matching size stops', () => {
  for (const [size, sign] of [[.2, 1], [.6, -1], [.4, 0]]) {
    const t = new Tracker(); acquire(t, picture(0, 0, size));
    const o = t.output(.5, 'follow', .1); assert.equal(Math.sign(o.vx), sign);
    assert.equal(o.vy, 0); assert.equal(o.vyaw, 0);
  }
});
test('stale/empty/malformed detections immediately stop translation and freeze gaze', () => {
  for (const kind of ['stale', 'empty', 'malformed']) {
    const t = new Tracker(); acquire(t, picture(0, 0, .2)); assert.ok(t.output(.5, 'follow', .1).vx > 0);
    if (kind === 'empty') t.ingest({ width: 720, height: 1280, boxes: [] }, .6);
    if (kind === 'malformed') t.ingest({ width: 0, height: 0, boxes: [] }, .6);
    const out = t.output(kind === 'stale' ? 1.8 : .6, 'follow', .1);
    assert.equal(out.vx, 0); assert.equal(out.valid, false);
  }
});
test('target near edge or head turned away never translates', () => {
  const a = new Tracker(); acquire(a, picture(.5, 0, .2)); assert.equal(a.output(.5, 'follow', .1).vx, 0);
  const b = new Tracker(); b.reset([0, 0, .3, 0]); acquire(b, picture(0, 0, .2)); assert.equal(b.output(.5, 'follow', .1).vx, 0);
  const c = new Tracker(); acquire(c, picture(0, .6, .8)); assert.equal(c.output(.5, 'follow', .1).vx, 0);
});
test('size hysteresis stops before reversing and does not chatter near setpoint', () => {
  const t = new Tracker(); acquire(t, picture(0, 0, .3)); assert.ok(t.output(.5, 'follow', .1).vx > 0);
  for (let i = 1; i <= 8; i++) { t.ingest(picture(0, 0, .4), .5 + .1 * i); t.output(.5 + .1 * i, 'follow', .1); }
  assert.equal(t.velocity, 0);
  for (let i = 1; i <= 6; i++) { t.ingest(picture(0, 0, i % 2 ? .38 : .42), 1.3 + .1 * i); assert.equal(t.output(1.3 + .1 * i, 'follow', .1).vx, 0); }
});
test('a reverse request never continues forward while ramping through zero', () => {
  const t = new Tracker(); acquire(t, picture(0, 0, .3));
  for (let i = 0; i < 6; i++) t.output(.5 + i * .05, 'follow', .1);
  assert.ok(t.velocity > 0);
  t.ingest(picture(0, 0, .6), .9);
  assert.ok(t.output(.9, 'follow', .1).vx < 0);
});
test('reacquisition requires confirmation and cannot jump immediately to another duck', () => {
  const t = new Tracker(); acquire(t, picture(-.45, 0, .3));
  assert.equal(t.ingest(picture(.45, 0, .3), .6), null);
  t.ingest(picture(.45, 0, .3), .7); assert.equal(t.output(.7, 'follow', .1).valid, false);
  t.ingest(picture(.45, 0, .3), .8); assert.equal(t.output(.8, 'follow', .1).valid, true);
});
test('distance calibration uses a fresh complete target and validates limits', () => {
  const t = new Tracker(); assert.throws(() => t.calibrate(0)); acquire(t, picture(0, 0, .3));
  assert.ok(Math.abs(t.calibrate(.5) - .3) < 1e-10); assert.throws(() => t.calibrate(2));
  assert.throws(() => new Tracker({ forward: 1 })); assert.throws(() => new Tracker({ gain: NaN }));
});
test('state gate rejects held, skills, unknown safety, fall/recovery and invalid heads', () => {
  const ok = { policy: 'stand', head: [0, 0, 0, 0], safety: { fallen: false, limp: false } };
  assert.equal(stateProblem(ok), '');
  for (const bad of [null, { ...ok, policy: 'sit' }, { ...ok, policy: 'held' }, { ...ok, safety: {} },
    { ...ok, safety: { fallen: true, limp: false } }, { ...ok, safety: { fallen: false, limp: false, fall: { recovery_active: true } } },
    { ...ok, head: [NaN, 0, 0, 0] }]) assert.ok(stateProblem(bad));
});
test('non-probability detector scores accepted; invalid numbers never enter control', () => {
  assert.equal(boxesFrom(picture()).length, 1);
  const p = picture(); p.boxes[0].score = NaN; assert.equal(boxesFrom(p).length, 0);
  const q = picture(); q.boxes[0].x1 = Infinity; assert.equal(boxesFrom(q).length, 0);
});
test('marker ID lock rejects other IDs and metric distances drive in the correct direction', () => {
  for (const [distance, sign] of [[1, 1], [.3, -1], [.6, 0]]) {
    const t = new Tracker({ targetId: 0, distanceMode: 'meters' });
    const p = picture(0, 0, .2); Object.assign(p.boxes[0], { id: 1, distance_status: 'calibrated', distance_m: distance });
    t.ingest(p, 0); assert.equal(t.target, null);
    p.boxes[0].id = 0; acquire(t, p);
    assert.equal(Math.sign(t.output(.5, 'follow', .1).vx), sign);
  }
});
test('metric pose failure stops without falling back to image size or stale filtered distance', () => {
  const t = new Tracker({ targetId: 0, distanceMode: 'meters' }), p = picture(0, 0, .2);
  Object.assign(p.boxes[0], { id: 0, distance_status: 'calibrated', distance_m: 1 });
  acquire(t, p); assert.ok(t.output(.5, 'follow', .1).vx > 0);
  p.boxes[0].distance_status = 'pose_rejected'; t.ingest(p, .7);
  assert.equal(t.output(.7, 'follow', .1).vx, 0); assert.equal(t.filteredDistance, null);
  assert.throws(() => t.calibrate(.7));
});
test('two cards bearing the same requested ID are ambiguous and never drive', () => {
  const t = new Tracker({ targetId: 0 }), p = picture();
  p.boxes[0].id = 0; p.boxes.push({ ...p.boxes[0], x0: p.boxes[0].x0 + 30, x1: p.boxes[0].x1 + 30 });
  acquire(t, p); assert.equal(t.target, null); assert.equal(t.output(.5, 'follow', .1).vx, 0);
});
test('single repeated IMU tick needs a fresh independent health proof, never an unconditional bypass', () => {
  const state = { policy: 'stand', head: [0, 0, 0, 0], safety: { fallen: false, limp: false,
    fall: { recovery_active: false, recovery_failed: false, imu_ready: true, imu_fresh: false } } };
  const proof = { age: .1, health: { healthy: true, imu: { ready: true, consecutive_stale_blocks: 1 }, bus: { consecutive_errors: 0 } } };
  assert.equal(stateProblem(state, proof), '');
  assert.match(stateProblem(state), /imu_fresh=false/);
  assert.ok(stateProblem(state, { ...proof, age: .6 }));
  assert.ok(stateProblem(state, { ...proof, health: { ...proof.health, imu: { ready: false, consecutive_stale_blocks: 1 } } }));
  assert.ok(stateProblem(state, { ...proof, health: { ...proof.health, imu: { ready: true, consecutive_stale_blocks: 5 } } }));
  assert.ok(imuEvidenceProblem({ age: .1, health: { healthy: true, imu: { ready: true } } }));
});
test('healthy proof never overrides recovery, failure, limp or IMU not ready', () => {
  const proof = { age: .1, health: { healthy: true, imu: { ready: true, consecutive_stale_blocks: 0 } } };
  for (const [field, value, expected] of [['recovery_active', true, /recovery_active=true/],
    ['recovery_failed', true, /recovery_failed=true/], ['imu_ready', false, /imu_ready=false/]]) {
    const state = { policy: 'stand', head: [0, 0, 0, 0], safety: { fallen: false, limp: false,
      fall: { recovery_active: false, recovery_failed: false, imu_ready: true, imu_fresh: false, [field]: value } } };
    assert.match(stateProblem(state, proof), expected);
  }
  assert.ok(stateProblem({ policy: 'stand', head: [0, 0, 0, 0], safety: { fallen: false, limp: true } }, proof));
});
