/* Browser-side bounded raw-frame uploader. No control/RPC access. */
(function (root) {
  "use strict";
  class ArucoFrames {
    constructor({ video, onResult, onError, onMetrics = () => {}, clock = () => performance.now() / 1000,
      fetcher = (...args) => fetch(...args), canvas = null, maxSide = 640, jpegQuality = .7, maxAge = 5, httpTimeout = 8 }) {
      this.video = video; this.onResult = onResult; this.onError = onError;
      this.onMetrics = onMetrics; this.configure({ maxSide, jpegQuality, maxAge, httpTimeout });
      this.clock = clock; this.fetcher = fetcher;
      this.canvas = canvas || document.createElement("canvas");
      this.job = null; this.sequence = 0; this.lastVideoTime = -1; this.lastDecodedAt = -Infinity; this.nextTry = -Infinity;
    }
    configure({ maxSide = 640, jpegQuality = .7, maxAge = 5, httpTimeout = 8 } = {}) {
      if (![640, 800, 960].includes(maxSide) || !Number.isFinite(jpegQuality) || jpegQuality < .6 || jpegQuality > .9)
        throw new Error("ArUco 上传尺寸/质量参数无效");
      if (!Number.isFinite(maxAge) || maxAge < .8 || maxAge > 15 || !Number.isFinite(httpTimeout) ||
          httpTimeout < 2 || httpTimeout > 30 || httpTimeout < maxAge)
        throw new Error("ArUco 检测等待参数无效");
      this.maxSide = maxSide; this.jpegQuality = jpegQuality;
      this.maxAge = maxAge; this.httpTimeout = httpTimeout;
    }
    cancel() {
      if (this.job) this.job.abort.abort();
      this.job = null; this.lastVideoTime = -1; this.nextTry = -Infinity;
    }
    resetSource() { this.cancel(); this.lastDecodedAt = -Infinity; }
    async step(rotate, decodedAt = this.clock()) {
      if (this.job || this.clock() < this.nextTry || this.video.readyState < 2 || !this.video.videoWidth || !this.video.videoHeight ||
          this.video.currentTime === this.lastVideoTime || ![0, 90, 180, 270].includes(rotate) ||
          !Number.isFinite(decodedAt) || decodedAt <= this.lastDecodedAt || decodedAt > this.clock() || this.clock() - decodedAt > this.maxAge) return;
      const job = { abort: new AbortController(), captured: decodedAt, copied: this.clock(), id: ++this.sequence,
        maxAge: this.maxAge, httpTimeout: this.httpTimeout };
      this.job = job; this.lastVideoTime = this.video.currentTime; this.lastDecodedAt = decodedAt;
      const scale = Math.min(1, this.maxSide / Math.max(this.video.videoWidth, this.video.videoHeight));
      this.canvas.width = Math.round(this.video.videoWidth * scale);
      this.canvas.height = Math.round(this.video.videoHeight * scale);
      let timeout, requestAt = null;
      const timing = { frame_id: job.id, width: this.canvas.width, height: this.canvas.height,
        jpeg_quality: this.jpegQuality, decoded_age_ms: (job.copied - job.captured) * 1000,
        phase: "encode", fresh_for_control: false };
      const failure = (message, kind, recoverable) => Object.assign(new Error(message), { kind, recoverable });
      try {
        // Copy the unrotated encoded video; calibration/pose stay in this raw optical frame.
        this.canvas.getContext("2d").drawImage(this.video, 0, 0, this.canvas.width, this.canvas.height);
        const blob = await new Promise(resolve => this.canvas.toBlob(resolve, "image/jpeg", this.jpegQuality));
        if (this.job !== job) return;
        timing.encode_ms = (this.clock() - job.copied) * 1000;
        timing.jpeg_bytes = blob ? blob.size : 0;
        if (!blob || this.clock() - job.captured > job.maxAge) throw failure("ArUco JPEG 编码失败或取帧过期", "capture", true);
        requestAt = this.clock(); timing.phase = "http";
        // Both budgets are configurable independently of the IMU/control watchdogs.
        timeout = setTimeout(() => job.abort.abort(), job.httpTimeout * 1000);
        const response = await this.fetcher(`/aruco/detect?rotate=${rotate}&frame_id=${job.id}`, {
          method: "POST", headers: { "Content-Type": "image/jpeg" }, body: blob, signal: job.abort.signal,
        });
        const result = await response.json();
        if (this.job !== job) return;
        timing.http_ms = (this.clock() - requestAt) * 1000;
        timing.server_ms = result.took_ms;
        timing.server_decode_ms = result.decode_ms;
        timing.server_detect_ms = result.detect_ms;
        timing.server_pose_ms = result.pose_ms;
        timing.ids = Array.isArray(result.boxes) ? result.boxes.map(b => b.id).filter(Number.isInteger) : [];
        if (!response.ok) throw failure(result.error || `ArUco HTTP ${response.status}`, "http", response.status === 429);
        if (result.frame_id !== job.id || result.source !== "aruco") throw failure("ArUco 序号或来源无效", "protocol", false);
        if (this.clock() - job.captured > job.maxAge)
          throw failure(`ArUco 已识别但结果超过 ${job.maxAge} 秒，不用于控制`, "stale", true);
        timing.phase = "complete"; timing.fresh_for_control = true;
        result.client_timing = { ...timing, age_ms: (this.clock() - job.captured) * 1000 };
        this.onResult(result, job.captured);
      } catch (error) {
        if (this.job === job) {
          let report = error;
          if (error.name === "AbortError") report = failure(`ArUco 上传/处理超过 ${job.httpTimeout} 秒，暂停等待新帧`, "timeout", true);
          else if (error.name === "TypeError") report = failure(`ArUco 网络上传失败：${error.message}`, "network", true);
          timing.error_kind = report.kind || "processing";
          timing.age_ms = (this.clock() - job.captured) * 1000;
          if (requestAt !== null && timing.http_ms === undefined) timing.http_ms = (this.clock() - requestAt) * 1000;
          report.timing = { ...timing };
          if (report.recoverable) this.nextTry = this.clock() + .5;
          this.onError(report);
        }
      } finally {
        clearTimeout(timeout);
        if (this.job === job) {
          timing.age_ms = (this.clock() - job.captured) * 1000;
          this.onMetrics(timing); this.job = null;
        }
      }
    }
    async snapshot() {
      if (this.video.readyState < 2 || !this.video.videoWidth || !this.video.videoHeight) throw new Error("无可用视频帧");
      const canvas = document.createElement("canvas");
      canvas.width = this.video.videoWidth; canvas.height = this.video.videoHeight;
      canvas.getContext("2d").drawImage(this.video, 0, 0);
      return new Promise(resolve => canvas.toBlob(resolve, "image/png"));
    }
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { ArucoFrames };
  else root.DuckAruco = { ArucoFrames };
})(typeof globalThis !== "undefined" ? globalThis : this);
