(() => {
  const el = id => document.getElementById(id);
  const buttons = [...document.querySelectorAll('[data-board-action]')];
  const cells = Array.from({length:64}, (_, i) => {
    const cell = document.createElement('span');
    cell.textContent = '—'; cell.title = `分区 ${i}`;
    el('tofGrid').appendChild(cell); return cell;
  });
  const names = {live:'实时', starting:'初始化', stale:'数据停滞', error:'失败', ok:'完成', busy:'进行中', idle:'未测试'};
  let enabled = false, busy = false, sending = false;
  function badge(id, state) {
    el(id).textContent = names[state] || state || '等待';
    el(id).style.color = state === 'error' ? 'var(--bad)' : ['live','ok'].includes(state) ? 'var(--acc)' : 'var(--warn)';
  }
  function setButtons() { buttons.forEach(button => { button.disabled = !enabled || busy || sending; }); }
  function source(id, url) {
    if (url && el(id).getAttribute('src') !== url) el(id).setAttribute('src', url);
  }
  function render(data) {
    enabled = data.enabled === true;
    el('boardPanel').hidden = !enabled;
    if (!enabled) return;
    el('boardConnection').textContent = '已连接';
    el('boardConnection').style.color = 'var(--acc)';
    const {bno085:bno, lsm6dsv16x:lsm, vl53l5cx:tof} = data.sensors;
    badge('bnoStatus', bno.status); el('bnoMessage').textContent = bno.message;
    badge('lsmStatus', lsm.status); el('lsmMessage').textContent = lsm.message;
    el('lsmGyro').textContent = '角速度 XYZ：' + (lsm.status === 'live' && lsm.gyro_dps ?
      lsm.gyro_dps.map(v => v.toFixed(3)).join(' / ') + ' °/s · WHO_AM_I 0x' + lsm.who_am_i.toString(16).toUpperCase() : '—');
    badge('tofStatus', tof.status); el('tofMessage').textContent = tof.message;
    const distances = tof.status === 'live' ? tof.distances_mm || [] : [];
    const valid = distances.filter(Number.isFinite);
    el('tofRange').textContent = valid.length ? `有效 ${valid.length}/64 · 最近 ${Math.min(...valid)} mm · 最远 ${Math.max(...valid)} mm` : '暂无有效距离';
    cells.forEach((cell, i) => {
      const mm = distances[i], ok = Number.isFinite(mm);
      cell.textContent = ok ? mm : '—';
      cell.style.background = ok ? `hsl(${Math.min(220, Math.max(0, mm) / 4000 * 220)},45%,23%)` : '#243029';
    });
    const media = data.media;
    busy = Boolean(media.busy); setButtons();
    for (const group of ['audio','camera']) {
      badge(group + 'Status', media[group].status);
      el(group + 'Message').textContent = media[group].message;
    }
    const files = media.files || {};
    el('recordingResult').hidden = !files['mic.wav'] && !files['mic_left_loud.wav'];
    el('recordingPlayer').hidden = !files['mic_left_loud.wav'];
    el('recordingDownload').hidden = !files['mic_left_loud.wav'];
    source('recordingPlayer', files['mic_left_loud.wav']);
    if (files['mic_left_loud.wav']) el('recordingDownload').href = files['mic_left_loud.wav'];
    el('rawRecordingDownload').hidden = !files['mic.wav'];
    if (files['mic.wav']) el('rawRecordingDownload').href = files['mic.wav'];
    el('cameraResult').hidden = !files['camera.jpg'];
    source('cameraPreview', files['camera.jpg']);
    if (files['camera.jpg']) el('cameraDownload').href = files['camera.jpg'];
  }
  async function poll() {
    try {
      const response = await fetch('/api/board', {cache:'no-store', signal:AbortSignal.timeout(4000)});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch (error) {
      enabled = false; setButtons();
      el('boardConnection').textContent = '连接中断：' + error.message;
      el('boardConnection').style.color = 'var(--bad)';
      ['bnoStatus','lsmStatus','tofStatus'].forEach(id => badge(id, 'stale'));
      el('lsmGyro').textContent = '角速度 XYZ：—';
      cells.forEach(cell => { cell.textContent = '—'; cell.style.background = '#243029'; });
      el('tofRange').textContent = '连接中断，暂无有效距离';
    } finally {
      setTimeout(poll, enabled ? 250 : 2000);
    }
  }
  buttons.forEach(button => button.addEventListener('click', async () => {
    if (!enabled || busy || sending) return;
    const action = button.dataset.boardAction;
    const group = action.startsWith('camera_') ? 'camera' : 'audio';
    sending = true; setButtons();
    el(group + 'Message').textContent = '正在提交…';
    try {
      const response = await fetch('/api/board/action', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action}), signal:AbortSignal.timeout(5000)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      busy = true;
    } catch (error) {
      badge(group + 'Status', 'error');
      el(group + 'Message').textContent = error.message;
    } finally { sending = false; setButtons(); }
  }));
  setButtons(); poll();
})();
