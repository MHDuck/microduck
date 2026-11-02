const test = require('node:test');
const assert = require('node:assert/strict');
const { ArucoFrames } = require('./aruco_client.js');
function fixture(fetcher) {
  let t = 1, results = [], errors = [], draws = [], metrics = [];
  const video = { readyState: 4, videoWidth: 1280, videoHeight: 720, currentTime: 10 };
  const canvas = { getContext: () => ({ drawImage: (...args) => draws.push(args) }), toBlob: fn => fn(new Blob(['jpeg'])) };
  const frames = new ArucoFrames({ video, canvas, fetcher, clock: () => t,
    maxAge: .8, httpTimeout: 2,
    onResult: (p, capture) => results.push({ p, capture }), onError: e => errors.push(e), onMetrics: m => metrics.push(m) });
  return { frames, video, canvas, results, errors, draws, metrics, clock: value => { t = value; } };
}
test('uploads raw frames, scales uniformly and preserves the capture timestamp', async () => {
  const f = fixture(async (url, request) => {
    assert.match(url, /rotate=90&frame_id=1/); assert.equal(request.headers['Content-Type'], 'image/jpeg');
    return { ok: true, json: async () => ({ source: 'aruco', frame_id: 1, boxes: [] }) };
  });
  await f.frames.step(90);
  assert.equal(f.canvas.width, 640); assert.equal(f.canvas.height, 360);
  assert.equal(f.results[0].capture, 1); assert.equal(f.errors.length, 0);
  await f.frames.step(90); assert.equal(f.draws.length, 1, 'frozen video must not generate new detections');
});
test('an in-flight upload cannot queue more images; cancellation discards its late result', async () => {
  let reply;
  const f = fixture(() => new Promise(resolve => { reply = resolve; }));
  const first = f.frames.step(0); await new Promise(resolve => setImmediate(resolve));
  f.video.currentTime++; await f.frames.step(0); assert.equal(f.draws.length, 1);
  f.frames.cancel(); reply({ ok: true, json: async () => ({ source: 'aruco', frame_id: 1 }) });
  await first; assert.equal(f.results.length, 0); assert.equal(f.errors.length, 0);
});
test('stale responses, wrong sequence and HTTP errors cannot refresh detections', async () => {
  for (const kind of ['stale', 'sequence', 'http']) {
    const f = fixture(async () => {
      if (kind === 'stale') f.clock(2);
      return { ok: kind !== 'http', status: 503, json: async () => ({ source: 'aruco', frame_id: kind === 'sequence' ? 2 : 1, error: 'disabled' }) };
    });
    await f.frames.step(0); assert.equal(f.results.length, 0); assert.equal(f.errors.length, 1);
  }
});
test('a recognized marker arriving after 800 ms is discarded even with a longer HTTP budget', async () => {
  const f = fixture(async () => {
    f.clock(2.1);
    return { ok: true, json: async () => ({ source: 'aruco', frame_id: 1, took_ms: 80,
      decode_ms: 10, detect_ms: 60, pose_ms: 10, boxes: [{ id: 0 }] }) };
  });
  await f.frames.step(0);
  assert.equal(f.results.length, 0); assert.equal(f.errors[0].kind, 'stale');
  assert.equal(f.errors[0].recoverable, true);
  assert.equal(f.metrics[0].fresh_for_control, false);
  assert.deepEqual(f.metrics[0].ids, [0]);
  assert.equal(f.metrics[0].server_ms, 80);
  assert.ok(f.metrics[0].age_ms > 1000);
});
test('busy responses back off and remain bounded to one request', async () => {
  let calls = 0;
  const f = fixture(async () => { calls++; return { ok: false, status: 429, json: async () => ({ error: 'busy' }) }; });
  await f.frames.step(0); assert.equal(f.errors[0].recoverable, true);
  f.video.currentTime++; f.clock(1.2); await f.frames.step(0); assert.equal(calls, 1);
  f.clock(1.6); await f.frames.step(0); assert.equal(calls, 2);
});
test('upload size can be configured without changing the control freshness deadline', async () => {
  const f = fixture(async () => ({ ok: true, json: async () => ({ source: 'aruco', frame_id: 1, boxes: [] }) }));
  f.frames.configure({ maxSide: 960, jpegQuality: .8 }); await f.frames.step(0);
  assert.equal(f.canvas.width, 960); assert.equal(f.canvas.height, 540);
  assert.equal(f.metrics[0].jpeg_quality, .8);
  assert.throws(() => f.frames.configure({ maxSide: 1920 }));
});

test('a configurable slow detector accepts a 3-second result but rejects one beyond its budget', async () => {
  for (const delay of [3, 6]) {
    const f = fixture(async () => {
      f.clock(1 + delay);
      return { ok: true, json: async () => ({ source: 'aruco', frame_id: 1, boxes: [{ id: 0 }] }) };
    });
    f.frames.configure({ maxAge: 5, httpTimeout: 8 }); await f.frames.step(0);
    assert.equal(f.results.length, delay === 3 ? 1 : 0);
    assert.equal(f.errors.length, delay === 6 ? 1 : 0);
  }
  const f = fixture(async () => {});
  assert.throws(() => f.frames.configure({ maxAge: 10, httpTimeout: 8 }));
});

test('low-FPS frame age includes waiting since decode and cannot be reset by copying its pixels', async () => {
  const f = fixture(async () => {
    f.clock(6);
    return { ok: true, json: async () => ({ source: 'aruco', frame_id: 1, boxes: [{ id: 0 }] }) };
  });
  f.frames.configure({ maxAge: 5, httpTimeout: 8 }); f.clock(3);
  await f.frames.step(0, 0);
  assert.equal(f.results.length, 0); assert.equal(f.errors[0].kind, 'stale');
  assert.equal(f.metrics[0].decoded_age_ms, 3000); assert.equal(f.metrics[0].encode_ms, 0);
  assert.equal(f.metrics[0].age_ms, 6000);
});

test('one decoded frame cannot be uploaded twice even when playback time advances or jobs cancel', async () => {
  let calls = 0;
  const f = fixture(async url => {
    calls++;
    return { ok: true, json: async () => ({ source: 'aruco', frame_id: Number(new URL(url, 'http://local').searchParams.get('frame_id')), boxes: [] }) };
  });
  f.frames.configure({ maxAge: 5, httpTimeout: 8 }); await f.frames.step(0, 1);
  f.video.currentTime++; f.clock(2); await f.frames.step(0, 1);
  f.frames.cancel(); f.video.currentTime++; f.clock(3); await f.frames.step(0, 1);
  assert.equal(calls, 1); assert.equal(f.results.length, 1);
  await f.frames.step(0, 2.9); assert.equal(calls, 2); assert.equal(f.results.length, 2);
});

test('only an explicit source reset allows a new stream to reuse an earlier frame timestamp', async () => {
  let calls = 0;
  const f = fixture(async url => {
    calls++;
    return { ok: true, json: async () => ({ source: 'aruco', frame_id: Number(new URL(url, 'http://local').searchParams.get('frame_id')), boxes: [] }) };
  });
  f.frames.configure({ maxAge: 5, httpTimeout: 8 }); await f.frames.step(0, 1);
  f.clock(2); f.frames.resetSource(); await f.frames.step(0, 1);
  assert.equal(calls, 2); assert.equal(f.errors.length, 0);
});
