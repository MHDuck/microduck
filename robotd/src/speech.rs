//! Optional microphone control, owned by robotd and off after every restart.
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
use std::time::{Duration, Instant};

use arc_swap::ArcSwapOption;
use duck_ipc_proto::{FeatureResult, RobotState as Frame};
use serde::Deserialize;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::watch;

use super::{Intents, RobotState};

const ADAPTER: &str = concat!(
    include_str!("../../speech/keyboard_input.py"),
    "\n",
    include_str!("../../speech/remote_input.py")
);
const FRESH: Duration = Duration::from_millis(400);

#[derive(Clone, Copy, PartialEq)]
struct Request {
    epoch: u64,
    enabled: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    Stop,
    StopForSit,
    Sit,
    Rise,
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Drive {
    Walk,
    Backward,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Packet {
    pub generation: u64,
    pub drive: Option<Drive>,
    pub action: Option<Action>,
    ready: bool,
    #[serde(default)]
    failed: bool,
    reason: String,
}

pub struct Update {
    pub packet: Packet,
    pub sequence: u64,
    epoch: u64,
    at: Instant,
}

pub struct Speech {
    directory: PathBuf,
    control: watch::Sender<Request>,
    status: Mutex<(u64, FeatureResult)>,
    active: AtomicBool,
    sequence: AtomicU64,
    epoch: AtomicU64,
    latest: ArcSwapOption<Update>,
}

impl Speech {
    pub fn new(directory: PathBuf) -> Self {
        let (control, _) = watch::channel(Request {
            epoch: 0,
            enabled: false,
        });
        Self {
            directory,
            control,
            status: Mutex::new((
                0,
                FeatureResult {
                    accepted: true,
                    available: false,
                    enabled: false,
                    state: "off".into(),
                    reason: "语音识别已关闭".into(),
                },
            )),
            active: AtomicBool::new(false),
            sequence: AtomicU64::new(0),
            epoch: AtomicU64::new(0),
            latest: ArcSwapOption::empty(),
        }
    }

    pub fn enabled(&self) -> bool {
        self.active.load(Ordering::Acquire)
    }

    pub fn request(&self, enabled: Option<bool>) -> FeatureResult {
        let mut status = self.status.lock().unwrap();
        status.1.available = [".venv/bin/python", "recognize.py"]
            .iter()
            .all(|p| self.directory.join(p).is_file());
        if !status.1.available && !status.1.enabled {
            status.1.reason = format!("语音环境未安装：{}", self.directory.display());
        }
        if let Some(enabled) = enabled {
            if enabled && !status.1.available {
                let mut refused = status.1.clone();
                refused.accepted = false;
                refused.reason = format!("语音环境未安装：{}", self.directory.display());
                return refused;
            }
            if status.1.enabled != enabled || status.1.state == "error" {
                status.0 += 1;
                self.epoch.store(status.0, Ordering::Release);
                status.1.enabled = enabled;
                status.1.state = if enabled { "starting" } else { "off" }.into();
                status.1.reason = if enabled {
                    "正在启动麦克风和语音模型"
                } else {
                    "语音识别已关闭"
                }
                .into();
                self.active.store(enabled, Ordering::Release);
                self.latest.store(None);
                self.control.send_replace(Request {
                    epoch: status.0,
                    enabled,
                });
            }
        }
        status.1.clone()
    }

    fn publish(&self, epoch: u64, enabled: bool, state: &str, reason: String) {
        let mut status = self.status.lock().unwrap();
        if status.0 != epoch {
            return;
        }
        status.1.enabled = enabled;
        status.1.state = state.into();
        status.1.reason = reason.chars().take(300).collect();
        self.active.store(enabled, Ordering::Release);
        if !enabled {
            self.latest.store(None);
        }
    }

    pub fn current(&self, manual_generation: u64) -> Option<Arc<Update>> {
        if !self.active.load(Ordering::Acquire) {
            return None;
        }
        self.latest.load_full().filter(|u| {
            u.epoch == self.epoch.load(Ordering::Acquire)
                && u.packet.ready
                && u.packet.generation == manual_generation
                && u.at.elapsed() < FRESH
        })
    }
}

pub fn vision(enabled: Option<bool>) -> FeatureResult {
    FeatureResult {
        accepted: enabled != Some(true),
        available: false,
        enabled: false,
        state: "reserved".into(),
        reason: "视觉识别为预留功能，尚未接入".into(),
    }
}

// Killing the process group also releases arecord, including on daemon/task shutdown.
struct Recognizer(Child);
impl Drop for Recognizer {
    fn drop(&mut self) {
        if let Some(pid) = self.0.id() {
            unsafe {
                libc::kill(-(pid as i32), libc::SIGKILL);
            }
        }
    }
}

pub async fn run(state: Arc<RobotState>, intents: Arc<Intents>) {
    let speech = &state.speech;
    let mut control = speech.control.subscribe();
    loop {
        let request = *control.borrow_and_update();
        if request.enabled {
            if let Err(error) = session(&state, &intents, request, &mut control).await {
                tracing::warn!(%error, "speech recognition stopped");
                speech.publish(request.epoch, false, "error", error.to_string());
            }
            if *control.borrow() != request {
                continue;
            }
        }
        if control.changed().await.is_err() {
            break;
        }
    }
}

async fn session(
    state: &RobotState,
    intents: &Intents,
    request: Request,
    control: &mut watch::Receiver<Request>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let speech = &state.speech;
    let mut command = Command::new("/usr/bin/python3");
    command
        .args(["-u", "-c", ADAPTER])
        .arg(&speech.directory)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .kill_on_drop(true);
    // One group for the adapter, its recognizer, and the ALSA capture process.
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.as_std_mut().process_group(0);
    }
    let mut child = Recognizer(command.spawn()?);
    let mut input = child.0.stdin.take().unwrap();
    let mut output = BufReader::new(child.0.stdout.take().unwrap()).lines();
    let mut frames = state.state_tx.subscribe();
    let mut latest: Option<(Frame, Instant)> = None;
    let mut tick = tokio::time::interval(Duration::from_millis(100));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            _ = control.changed() => return Ok(()),
            line = output.next_line() => {
                let line = line?.ok_or("语音识别进程退出，请查看 robotd 日志")?;
                let packet: Packet = serde_json::from_str(&line)?;
                if packet.failed {
                    return Err(packet.reason.into());
                }
                speech.publish(request.epoch, true, if packet.ready { "on" } else { "starting" }, packet.reason.clone());
                // An off/on request invalidates packets from the old process before its kill lands.
                if *control.borrow() == request {
                    let sequence = speech.sequence.fetch_add(1, Ordering::Relaxed) + 1;
                    speech.latest.store(Some(Arc::new(Update { packet, sequence, epoch: request.epoch, at: Instant::now() })));
                }
            }
            _ = tick.tick() => {
                while let Ok(frame) = frames.try_recv() { latest = Some((frame, Instant::now())); }
                let context = serde_json::json!({
                    "generation": intents.manual_generation(), "enabled": intents.enabled(),
                    "mode": if state.mode.load(Ordering::Relaxed) == super::mode_code(super::Mode::Walk) { "drive" } else { "roller" },
                    "state": latest.as_ref().map(|v| &v.0),
                    "age": latest.as_ref().map(|v| v.1.elapsed().as_secs_f64()),
                });
                let line = format!("{context}\n");
                tokio::time::timeout(Duration::from_millis(400), input.write_all(line.as_bytes())).await??;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_speech_installation_is_not_reported_as_on() {
        let s = Speech::new(PathBuf::from("/not-installed/speech"));
        assert!(!s.request(Some(true)).accepted);
        assert!(!s.request(None).enabled);
        assert!(s.request(Some(false)).accepted);
    }

    #[test]
    fn reserved_vision_cannot_claim_to_be_enabled() {
        let result = vision(Some(true));
        assert!(!result.accepted && !result.enabled && !result.available);
        assert!(vision(Some(false)).accepted);
        assert_eq!(vision(None).state, "reserved");
    }

    #[test]
    fn off_stale_and_manually_superseded_voice_cannot_drive() {
        let s = Speech::new(PathBuf::new());
        let update = |at| {
            Arc::new(Update {
                packet: Packet {
                    generation: 7,
                    drive: Some(Drive::Walk),
                    action: None,
                    ready: true,
                    failed: false,
                    reason: String::new(),
                },
                sequence: 1,
                epoch: 0,
                at,
            })
        };
        s.latest.store(Some(update(Instant::now())));
        assert!(s.current(7).is_none());
        s.active.store(true, Ordering::Release);
        assert!(s.current(7).is_some());
        assert!(s.current(8).is_none());
        s.latest
            .store(Some(update(Instant::now() - Duration::from_secs(1))));
        assert!(s.current(7).is_none());
    }

    #[test]
    fn a_late_worker_cannot_undo_an_off_request() {
        let s = Speech::new(PathBuf::new());
        s.status.lock().unwrap().0 = 2;
        s.publish(1, true, "on", String::new());
        assert!(!s.active.load(Ordering::Acquire));
        assert!(!s.request(None).enabled);
    }

    #[tokio::test]
    async fn recognition_failure_reaches_the_feature_status() {
        let directory = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(directory.path().join(".venv/bin")).unwrap();
        std::os::unix::fs::symlink(
            "/usr/bin/python3",
            directory.path().join(".venv/bin/python"),
        )
        .unwrap();
        std::fs::write(directory.path().join("recognize.py"), "raise SystemExit(2)\n").unwrap();
        let mut state = RobotState::new(&super::super::Params::default(), false, false);
        state.speech = Speech::new(directory.path().into());
        let state = Arc::new(state);
        let task = tokio::spawn(run(Arc::clone(&state), Arc::new(Intents::new())));
        state.speech.request(Some(true));
        let result = tokio::time::timeout(Duration::from_secs(4), async {
            loop {
                let status = state.speech.request(None);
                if status.state == "error" {
                    break status;
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await;
        task.abort();
        let _ = task.await;
        let status = result.expect("recognizer failure was not reported");
        assert!(!status.enabled);
        assert!(status.reason.contains("语音停用"), "{}", status.reason);
    }

    #[tokio::test]
    async fn runtime_opens_and_closes_capture_without_enabling_the_robot() {
        let directory = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(directory.path().join(".venv/bin")).unwrap();
        std::os::unix::fs::symlink(
            "/usr/bin/python3",
            directory.path().join(".venv/bin/python"),
        )
        .unwrap();
        std::fs::write(
            directory.path().join("recognize.py"),
            r#"
import json, os, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
with open('capture-pids', 'w') as f: f.write(f'{os.getpid()} {child.pid}')
while True:
    print(json.dumps({'kind':'audio_heartbeat','monotonic_s':time.monotonic()}), flush=True)
    time.sleep(.05)
"#,
        )
        .unwrap();
        let mut state = RobotState::new(&super::super::Params::default(), false, false);
        state.speech = Speech::new(directory.path().into());
        let state = Arc::new(state);
        let intents = Arc::new(Intents::new());
        let task = tokio::spawn(run(Arc::clone(&state), Arc::clone(&intents)));
        assert!(state.speech.request(Some(true)).accepted);
        let ready = tokio::time::timeout(Duration::from_secs(4), async {
            while state.speech.request(None).state != "on" {
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await;
        let pids =
            std::fs::read_to_string(directory.path().join("capture-pids")).unwrap_or_default();
        state.speech.request(Some(false));
        tokio::time::sleep(Duration::from_millis(500)).await;
        task.abort();
        let _ = task.await;
        assert!(ready.is_ok(), "fake audio never became ready");
        assert!(!intents.enabled(), "listening must not enable motion");
        assert_eq!(intents.snapshot().command.twist, [0.0; 3]);
        assert_eq!(state.speech.request(None).state, "off");
        assert_eq!(pids.split_whitespace().count(), 2);
        for pid in pids.split_whitespace() {
            if let Ok(stat) = std::fs::read_to_string(format!("/proc/{pid}/stat")) {
                assert_eq!(
                    stat.split(") ").nth(1).unwrap().chars().next(),
                    Some('Z'),
                    "capture {pid} survived disable"
                );
            }
        }
    }
}
