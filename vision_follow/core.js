/* Pure visual-servo logic. Browser + Node, no I/O and no motor access. */
(function (root) {
  "use strict";
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const finite = Number.isFinite;
  const DEFAULTS = Object.freeze({
    targetSize: 0.40, sizeBand: 0.15, forward: 0.12, backward: 0.10,
    timeout: 1.2, deadzone: 0.08, gain: 0.7, headRate: 0.35,
    yawLimit: 0.60, pitchLimit: 0.35, vertical: false,
    invertYaw: false, invertPitch: false, frontGate: true,
    targetId: null, distanceMode: "size", targetDistance: .6, distanceBand: .08,
  });
  function boxesFrom(p) {
    if (!p || !finite(p.width) || !finite(p.height) || p.width <= 0 || p.height <= 0 ||
        !Array.isArray(p.boxes) || p.boxes.length > 256) return [];
    return p.boxes.flatMap(b => {
      if (![b.x0, b.y0, b.x1, b.y1, b.score].every(finite) || b.x0 < 0 || b.y0 < 0 ||
          b.x1 > p.width || b.y1 > p.height || b.x1 <= b.x0 || b.y1 <= b.y0) return [];
      const w = (b.x1 - b.x0) / p.width, h = (b.y1 - b.y0) / p.height;
      if (w < 0.015 || h < (Number.isInteger(b.id) ? .01 : .03)) return [];
      return [{ x: (b.x0 + b.x1) / p.width - 1,
        y: (b.y0 + b.y1) / p.height - 1,
        size: finite(b.size_norm) && b.size_norm > 0 && b.size_norm <= 1 ? b.size_norm : h, area: w * h,
        id: Number.isInteger(b.id) ? b.id : null,
        distance: b.distance_status === "calibrated" && finite(b.distance_m) && b.distance_m >= .05 && b.distance_m <= 10 ? b.distance_m : null,
        clipped: b.x0 <= 2 || b.y0 <= 2 || b.x1 >= p.width - 2 || b.y1 >= p.height - 2,
        box: b }];
    });
  }
  function imuEvidenceProblem(evidence) {
    if (!evidence || !finite(evidence.age) || evidence.age < 0 || evidence.age > .5)
      return `缺少 0.5 秒内的 IMU 健康确认（确认年龄=${evidence && finite(evidence.age) ? (evidence.age * 1000).toFixed(0) + " ms" : "未知"}）`;
    const health = evidence.health, imu = health && health.imu;
    if (!imu || imu.ready !== true) return "IMU 驱动未就绪或姿态数据已过期";
    if (health.healthy !== true) return `机器人健康检查未通过：${health.reason || "原因未知"}`;
    if (!Number.isInteger(imu.consecutive_stale_blocks) || imu.consecutive_stale_blocks < 0)
      return "健康接口缺少有效的 IMU 连续重复计数";
    if (imu.consecutive_stale_blocks >= 5) return `IMU 持续重复采样（连续 ${imu.consecutive_stale_blocks} 次）`;
    if (health.bus && health.bus.consecutive_errors > 0) return "舵机总线当前读取失败";
    return "";
  }
  function stateProblem(state, evidence = null) {
    if (!state) return "等待机器人状态";
    const s = state.safety;
    if (state.policy === "standup" || (s && s.fall && s.fall.recovery_active)) return "起身仍在执行（standup / recovery_active=true），请等待结束";
    if (state.policy === "protection" || (s && s.fall && s.fall.recovery_failed)) return "起身失败或保护已锁定（protection / recovery_failed=true）";
    if (!["walk", "stand"].includes(state.policy)) return `当前策略=${state.policy || "未知"}，等待站稳的 walk/stand 状态`;
    if (!s || s.fallen !== false || s.limp !== false) return "倒地、卸力或安全状态未知";
    if (s.fall && s.fall.imu_ready === false) return "IMU 未就绪（imu_ready=false）";
    if (evidence && evidence.health && evidence.health.imu && evidence.health.imu.ready === false)
      return "IMU 驱动未就绪或姿态数据已过期（health.imu.ready=false）";
    if (s.fall && s.fall.imu_fresh === false) {
      // This bit is false even for a single repeated 50 Hz sample. The BNO085 driver
      // separately reports ready=false when its sample age reaches MAX_AGE (100 ms).
      // Only a fresh independent health reply may distinguish a repeat from a dropout.
      const problem = imuEvidenceProblem(evidence);
      if (problem) return `本周期 IMU 无新样本（imu_fresh=false）：${problem}`;
    }
    if (!Array.isArray(state.head) || state.head.length !== 4 || !state.head.every(finite)) return "头部状态无效";
    return "";
  }
  class Tracker {
    constructor(options = {}) { this.configure(options); this.reset(); }
    configure(options) {
      const o = { ...DEFAULTS, ...options };
      for (const k of ["targetSize", "sizeBand", "forward", "backward", "timeout", "deadzone", "gain", "headRate", "yawLimit", "pitchLimit", "targetDistance", "distanceBand"]) {
        if (!finite(o[k])) throw new Error(`参数 ${k} 必须为有限数字`);
      }
      if (o.targetSize < .01 || o.targetSize > .85 || o.sizeBand < .05 || o.sizeBand > .4 ||
          o.forward < 0 || o.forward > .25 || o.backward < 0 || o.backward > .25 ||
          o.timeout < .6 || o.timeout > 60 || o.gain <= 0 || o.gain > 2 ||
          o.headRate <= 0 || o.headRate > .6 || o.deadzone < .02 || o.deadzone > .3 ||
          o.yawLimit <= 0 || o.yawLimit > .7 || o.pitchLimit <= 0 || o.pitchLimit > .4 ||
          o.targetDistance < .2 || o.targetDistance > 3 || o.distanceBand < .03 || o.distanceBand > .3 ||
          !["size", "meters"].includes(o.distanceMode) || typeof o.frontGate !== "boolean" ||
          (o.targetId !== null && (!Number.isInteger(o.targetId) || o.targetId < 0 || o.targetId > 49))) throw new Error("参数超出演示模块限制");
      this.o = o;
    }
    reset(head = [0, 0, 0, 0]) {
      if (!Array.isArray(head) || head.length !== 4 || !head.every(finite)) throw new Error("头部初值无效");
      this.head = head.slice(); this.aim = head.slice(); this.target = null; this.lastDetection = -Infinity;
      this.lastUpdate = null; this.confirmed = 0; this.velocity = 0; this.filteredSize = null;
      this.direction = 0; this.serial = 0;
      this.filteredDistance = null;
    }
    ingest(params, now) {
      let found = boxesFrom(params).filter(b => this.o.targetId === null || b.id === this.o.targetId);
      // Two physical cards with the same ID cannot be distinguished safely.
      if (this.o.targetId !== null && found.length > 1) found = [];
      const previous = this.target;
      let selected = null;
      if (previous && now - this.lastDetection <= this.o.timeout) {
        const matches = found.filter(b => b.id === previous.id && Math.hypot(b.x - previous.x, b.y - previous.y) < .65 &&
          b.size / previous.size > .45 && b.size / previous.size < 2.2);
        matches.sort((a, b) => Math.hypot(a.x - previous.x, a.y - previous.y) - Math.hypot(b.x - previous.x, b.y - previous.y));
        selected = matches[0] || null;
      } else {
        found.sort((a, b) => Math.hypot(a.x, a.y) - Math.hypot(b.x, b.y));
        selected = found[0] || null;
        this.confirmed = 0; this.filteredSize = null; this.lastUpdate = null;
        this.filteredDistance = null;
      }
      if (!selected) {
        this.target = null; this.confirmed = 0; this.velocity = 0; this.direction = 0;
        this.filteredSize = null; this.lastUpdate = null;
        this.filteredDistance = null;
        return null;
      }
      const dt = this.lastUpdate === null ? 0 : clamp(now - this.lastUpdate, 0, .6);
      this.target = selected; this.lastDetection = now; this.lastUpdate = now;
      this.confirmed++; this.serial++;
      this.filteredSize = this.filteredSize === null ? selected.size : .6 * selected.size + .4 * this.filteredSize;
      this.filteredDistance = selected.distance === null ? null : this.filteredDistance === null
        ? selected.distance : .6 * selected.distance + .4 * this.filteredDistance;
      // Integrate only once per *new detection*, never repeatedly against an old image.
      if (this.confirmed >= 2) {
        const step = e => Math.abs(e) < this.o.deadzone ? 0 :
          clamp(this.o.gain * e, -this.o.headRate, this.o.headRate) * dt;
        this.aim[2] = clamp(this.aim[2] + (this.o.invertYaw ? 1 : -1) * step(selected.x), -this.o.yawLimit, this.o.yawLimit);
        if (this.o.vertical) this.aim[1] = clamp(this.aim[1] + (this.o.invertPitch ? -1 : 1) * step(selected.y), -this.o.pitchLimit, this.o.pitchLimit);
      }
      return selected;
    }
    calibrate(now) {
      if (!this.target || this.confirmed < 2 || now - this.lastDetection > this.o.timeout || this.target.clipped) throw new Error("请先让完整目标稳定出现两帧，再标定距离");
      if (this.o.distanceMode === "meters" && this.target.distance === null) throw new Error("当前目标没有有效标定距离，不能设为米制目标");
      this.configure(this.o.distanceMode === "meters"
        ? { ...this.o, targetDistance: this.target.distance } : { ...this.o, targetSize: this.target.size });
      this.velocity = 0; this.direction = 0;
      return this.o.distanceMode === "meters" ? this.o.targetDistance : this.o.targetSize;
    }
    output(now, mode, dt) {
      let reason = "原地注视", desired = 0;
      const valid = this.target && this.confirmed >= 2 && now - this.lastDetection <= this.o.timeout;
      if (valid) {
        const limit = this.o.headRate * clamp(dt, 0, .15);
        for (const i of [1, 2]) this.head[i] += clamp(this.aim[i] - this.head[i], -limit, limit);
      } else this.aim = this.head.slice();
      if (!valid) reason = "目标丢失或等待连续两帧";
      else if (mode === "follow") {
        const t = this.target;
        if (t.clipped || t.size > .9) reason = "目标被裁切，停止平移";
        else if (this.o.frontGate && (Math.abs(t.x) > .35 || Math.abs(this.head[2]) > .22)) reason = "目标偏离机身正前方，仅转头";
        else if (this.o.distanceMode === "meters" && (t.distance === null || this.filteredDistance === null)) reason = "没有有效标定距离，停止平移";
        else {
          // A Schmitt band prevents forward/backward chatter at the preferred size.
          const metric = this.o.distanceMode === "meters";
          const ratio = metric ? 2 - this.filteredDistance / this.o.targetDistance : this.filteredSize / this.o.targetSize;
          const b = metric ? this.o.distanceBand / this.o.targetDistance : this.o.sizeBand;
          if (ratio < 1 - b) this.direction = 1;
          else if (ratio > 1 + b) this.direction = -1;
          else if ((this.direction === 1 && ratio >= 1 - b / 2) ||
                   (this.direction === -1 && ratio <= 1 + b / 2)) this.direction = 0;
          desired = this.direction > 0 ? this.o.forward : this.direction < 0 ? -this.o.backward : 0;
          reason = desired > 0 ? (metric ? "目标较远，前进" : "目标较小，前进")
            : desired < 0 ? (metric ? "目标较近，后退" : "目标较大，后退") : "距离合适，停止";
        }
      }
      // Stop immediately for a gate/loss; acceleration and reversals are limited.
      if (!desired) { this.velocity = 0; this.direction = 0; }
      else {
        if (this.velocity * desired < 0) this.velocity = 0;
        this.velocity += clamp(desired - this.velocity, -.15 * clamp(dt, 0, .15), .15 * clamp(dt, 0, .15));
      }
      return { head: this.head.slice(), vx: this.velocity, desiredVx: desired, vy: 0, vyaw: 0, reason, valid: !!valid };
    }
  }
  // ArUco stop -> acknowledged zero -> settle/new decoded frame -> detect -> bounded move.
  // A result is consumed once. Movement can never be extended by polling an old result.
  class FollowCycle {
    constructor(options = {}) { this.configure(options); this.reset(); }
    configure({ moveSeconds = 2.5, settleSeconds = 1, maxAge = 5 } = {}) {
      if (![moveSeconds, settleSeconds, maxAge].every(finite) || moveSeconds < .2 || moveSeconds > 4 ||
          settleSeconds < .3 || settleSeconds > 5 || maxAge < .8 || maxAge > 15)
        throw new Error("停走跟随参数超出范围");
      this.moveSeconds = moveSeconds; this.settleSeconds = settleSeconds; this.maxAge = maxAge;
      this.acceleration = .3;
    }
    reset() {
      this.phase = "idle"; this.captureAfter = Infinity; this.used = -Infinity; this.deadline = Infinity;
      this.speed = 0; this.plannedSpeed = 0; this.peakSpeed = 0; this.stopReason = null;
    }
    start() { this.reset(); this.phase = "stopping"; }
    brake(reason = "外部停止") {
      if (this.phase !== "idle") this.phase = "stopping";
      this.stopReason = reason; this.speed = 0; this.captureAfter = Infinity;
    }
    acknowledgeStop(now) {
      if (this.phase !== "stopping") return false;
      this.phase = "settling"; this.settleUntil = now + this.settleSeconds; return true;
    }
    canCapture(now, videoAt) {
      if (this.phase === "settling" && now >= this.settleUntil && videoAt >= this.settleUntil) {
        this.phase = "detecting"; this.captureAfter = this.settleUntil;
      }
      return this.phase === "detecting";
    }
    accepts(captured, now) {
      return this.phase === "detecting" && finite(captured) && captured >= this.captureAfter &&
        captured <= now && now - captured <= this.maxAge;
    }
    update(out, captured, now) {
      if (this.phase === "moving") {
        if (now >= this.deadline) this.brake("移动段到时");
        else if (!out.valid) this.brake("目标确认过期或失效");
        else if (Math.sign(out.desiredVx) !== Math.sign(this.speed)) this.brake(out.reason);
      } else if (this.accepts(captured, now) && captured > this.used && out.valid) {
        this.used = captured;
        if (finite(out.desiredVx) && Math.abs(out.desiredVx) > 0 && Math.abs(out.desiredVx) <= .25) {
          this.phase = "moving"; this.speed = out.desiredVx; this.started = now; this.deadline = now + this.moveSeconds;
          this.plannedSpeed = out.desiredVx; this.peakSpeed = 0; this.stopReason = null;
        }
      }
      const vx = this.phase === "moving"
        ? Math.sign(this.speed) * Math.min(Math.abs(this.speed), Math.max(0, now - this.started) * this.acceleration) : 0;
      this.peakSpeed = Math.max(this.peakSpeed, Math.abs(vx));
      const reason = this.phase === "moving" ? `短程${this.speed > 0 ? "前进" : "后退"}，到时停步`
        : this.phase === "stopping" ? "停步，等待零速度应答"
        : this.phase === "settling" ? "已停步，等待稳定及新画面"
        : `停步识别：${out.reason}`;
      return { ...out, vx, reason, phase: this.phase };
    }
  }
  const api = { Tracker, FollowCycle, boxesFrom, stateProblem, imuEvidenceProblem, DEFAULTS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.DuckVision = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
