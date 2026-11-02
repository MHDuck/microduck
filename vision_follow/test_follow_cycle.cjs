const test = require('node:test');
const assert = require('node:assert/strict');
const { FollowCycle } = require('./core.js');
const decision = (desiredVx = .12, valid = true) => ({ desiredVx, vx: .12, valid, reason: 'target', head: [0, 0, 0, 0] });
function ready(cycle, now = 0) {
  cycle.start(); cycle.acknowledgeStop(now);
  assert.equal(cycle.canCapture(now + 1, now + 1), true);
}

test('capture waits for zero acknowledgement, settling and a new decoded frame', () => {
  const c = new FollowCycle(); c.start();
  assert.equal(c.canCapture(10, 10), false);
  assert.equal(c.update(decision(), 10, 10).vx, 0);
  c.acknowledgeStop(10);
  assert.equal(c.canCapture(10.9, 10.9), false);
  assert.equal(c.canCapture(12, 10.9), false, 'a pre-settle frame cannot be reused');
  assert.equal(c.canCapture(12, 12), true);
  assert.equal(c.accepts(10.5, 12), false);
});

test('one result triggers one bounded pulse, then requires a new stop and fresh detection', () => {
  const c = new FollowCycle({ moveSeconds: .8 }); ready(c);
  assert.equal(c.update(decision(), 1, 3).phase, 'moving', 'slow result within 5s accepted');
  assert.ok(c.update(decision(), 1, 3.4).vx > 0);
  assert.equal(c.canCapture(3.4, 3.4), false);
  assert.equal(c.update(decision(), 1, 3.81).vx, 0);
  assert.equal(c.phase, 'stopping');
  assert.equal(c.update(decision(), 1, 4).vx, 0);
  c.acknowledgeStop(4); assert.equal(c.canCapture(5, 5), true);
  assert.equal(c.update(decision(), 1, 5).vx, 0, 'used result cannot start another pulse');
  assert.equal(c.update(decision(-.1), 5, 5.2).phase, 'moving');
  assert.ok(c.update(decision(-.1), 5, 5.5).vx < 0);
});

test('lost, clipped/off-axis/invalid-distance decisions and expired results never start movement', () => {
  for (const [out, captured, now] of [[decision(.12, false), 1, 2], [decision(0), 1, 2],
    [decision(), 1, 7], [decision(), .5, 2], [decision(), 3, 2]]) {
    const c = new FollowCycle(); ready(c);
    assert.equal(c.update(out, captured, now).vx, 0);
    assert.equal(c.phase, 'detecting');
  }
});

test('loss or reversal during a pulse brakes instead of switching direction on old data', () => {
  for (const out of [decision(.12, false), decision(-.1), decision(0)]) {
    const c = new FollowCycle(); ready(c); c.update(decision(), 1, 2);
    assert.equal(c.update(out, 1, 2.1).vx, 0); assert.equal(c.phase, 'stopping');
  }
});

test('default segment reaches cruising speed then keeps walking until its fixed deadline', () => {
  const c = new FollowCycle(); ready(c); c.update(decision(), 1, 2);
  assert.equal(c.moveSeconds, 2.5);
  assert.ok(Math.abs(c.update(decision(), 1, 2.4).vx - .12) < 1e-8);
  for (const t of [2.8, 3.2, 3.8, 4.4]) assert.equal(c.update(decision(), 1, t).vx, .12);
  assert.equal(c.deadline, 4.5, 'polling an old decision must not extend the segment');
  assert.equal(c.update(decision(), 1, 4.5).vx, 0);
  assert.equal(c.stopReason, '移动段到时'); assert.equal(c.peakSpeed, .12);
});

test('manual reset cancels every stage and parameter limits bound the pulse', () => {
  const c = new FollowCycle(); ready(c); c.update(decision(), 1, 2); c.reset();
  assert.equal(c.update(decision(), 1, 2.2).vx, 0); assert.equal(c.phase, 'idle');
  assert.throws(() => new FollowCycle({ moveSeconds: 10 }));
  assert.throws(() => new FollowCycle({ settleSeconds: 0 }));
});
