//! Leased adjustment of selected joints. Only the control thread performs IO.
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use duck_control::Sensors;
use duck_control::io::{ImuIo, RobotIo};
use duck_control::model::{
    JOINT_IDS, JOINT_NAMES, MOUTH_CLOSED, MOUTH_INDEX, MOUTH_OPEN, NUM_JOINTS,
};
use duck_control::safety::Safety;
use duck_ipc_proto::{Call, JointStatus, JointsOp, JointsParams, JointsResult};

pub const LEASE: Duration = Duration::from_millis(1500);
const FRESH: Duration = Duration::from_millis(200);
// Adjustment is deliberately slow, irrespective of the client's update frequency.
const SPEED: f64 = 0.5;

struct Session {
    token: String,
    seq: u64,
    seen: Instant,
    entered: bool,
}
struct Inner {
    session: Option<Session>,
    sample: Option<([f64; NUM_JOINTS], Instant)>,
    desired: [Option<f64>; NUM_JOINTS],
    written: [Option<f64>; NUM_JOINTS],
    powered: [bool; NUM_JOINTS],
    off: [bool; NUM_JOINTS],
    serial: u64,
    error: Option<String>,
}

impl Default for Inner {
    fn default() -> Self {
        Self {
            session: None,
            sample: None,
            desired: [None; NUM_JOINTS],
            written: [None; NUM_JOINTS],
            powered: [false; NUM_JOINTS],
            off: [false; NUM_JOINTS],
            serial: 0,
            error: None,
        }
    }
}
fn range(index: usize) -> (f64, f64) {
    if index == MOUTH_INDEX {
        return (MOUTH_CLOSED, MOUTH_OPEN);
    }
    let model = kinematics::Model::alpha();
    model
        .joint_range(
            model
                .joint_index(JOINT_NAMES[index])
                .expect("alpha joint exists"),
        )
        .expect("alpha joint has mechanical limits")
}
impl Inner {
    fn active(&self) -> bool {
        self.session.is_some() || self.powered.iter().any(|on| *on)
    }
    fn fresh(&self, now: Instant) -> bool {
        self.sample
            .as_ref()
            .is_some_and(|(_, at)| now.saturating_duration_since(*at) < FRESH)
    }
    fn stop(&mut self, error: Option<String>) {
        self.session = None;
        self.desired.fill(None);
        self.off = self.powered;
        if error.is_some() {
            self.error = error;
        }
    }
    fn result(&self, now: Instant, reason: Option<String>, token: Option<String>) -> JointsResult {
        JointsResult {
            accepted: reason.is_none(),
            reason,
            active: self.active(),
            token,
            lease_ms: LEASE.as_millis() as u64,
            fresh: self.fresh(now),
            error: self.error.clone(),
            joints: (0..NUM_JOINTS)
                .map(|index| {
                    let (min, max) = range(index);
                    JointStatus {
                        index,
                        id: JOINT_IDS[index],
                        name: JOINT_NAMES[index].into(),
                        min,
                        max,
                        position: self.sample.as_ref().map(|(positions, _)| positions[index]),
                        target: self.written[index],
                        enabled: self.powered[index],
                    }
                })
                .collect(),
        }
    }
}

#[derive(Default)]
pub struct Joints {
    inner: Mutex<Inner>,
}
impl Joints {
    pub fn active(&self) -> bool {
        self.inner.lock().unwrap().active()
    }
    pub fn cancel(&self) {
        self.inner.lock().unwrap().stop(None);
    }
    pub fn blocks(&self, call: &Call) -> bool {
        self.active()
            && matches!(
                call,
                Call::RobotMove(_)
                    | Call::RobotHead(_)
                    | Call::RobotLook(_)
                    | Call::RobotPose(_)
                    | Call::RobotMouth(_)
                    | Call::RobotDo(_)
                    | Call::RobotSound(_)
                    | Call::RobotEnable(_)
                    | Call::RobotInit
                    | Call::RobotSetMode(_)
                    | Call::RobotTheremin(_)
                    | Call::RobotChorale(_)
                    | Call::RobotSpeech(duck_ipc_proto::FeatureParams {
                        enabled: Some(true)
                    })
            )
    }
    pub fn request(&self, p: &JointsParams, can_begin: bool) -> JointsResult {
        self.request_at(p, can_begin, Instant::now())
    }
    fn request_at(&self, p: &JointsParams, can_begin: bool, now: Instant) -> JointsResult {
        let mut s = self.inner.lock().unwrap();
        if s.session
            .as_ref()
            .is_some_and(|session| now.saturating_duration_since(session.seen) >= LEASE)
        {
            s.stop(Some("舵机调试会话超时，正在卸力".into()));
        }
        let result = (|| -> Result<Option<String>, String> {
            match p.op {
                JointsOp::Status | JointsOp::Begin => {
                    if p.token.is_some() || p.seq.is_some() || p.id.is_some() || p.radians.is_some()
                    {
                        return Err("status/begin 不接受舵机目标或会话参数".into());
                    }
                    if p.op == JointsOp::Status {
                        return Ok(None);
                    }
                    if s.active() {
                        return Err("已有舵机调试会话或卸力尚未完成".into());
                    }
                    if !can_begin || !s.fresh(now) {
                        return Err(
                            "进入调试前请关闭语音、停止策略并确认已卸力，等待新鲜舵机读数".into(),
                        );
                    }
                    s.serial += 1;
                    let token = format!(
                        "{:x}-{:x}",
                        SystemTime::now()
                            .duration_since(UNIX_EPOCH)
                            .unwrap_or_default()
                            .as_nanos(),
                        s.serial
                    );
                    s.session = Some(Session {
                        token: token.clone(),
                        seq: 0,
                        seen: now,
                        entered: false,
                    });
                    s.desired.fill(None);
                    s.written.fill(None);
                    s.off.fill(false);
                    s.error = None;
                    Ok(Some(token))
                }
                op => {
                    let session = s.session.as_ref().ok_or("舵机调试会话未开启或已过期")?;
                    if p.token.as_deref() != Some(session.token.as_str()) {
                        return Err("舵机调试会话不匹配".into());
                    }
                    let seq = p.seq.ok_or("缺少 seq")?;
                    if seq <= session.seq {
                        return Err("已忽略过期或重复的舵机指令".into());
                    }
                    let joint =
                        p.id.map(|id| {
                            JOINT_IDS
                                .iter()
                                .position(|value| *value == id)
                                .ok_or("未知舵机 ID")
                        })
                        .transpose()?;
                    match op {
                        JointsOp::Keepalive => {
                            if p.id.is_some() || p.radians.is_some() {
                                return Err("keepalive 不接受舵机目标".into());
                            }
                        }
                        JointsOp::Set => {
                            let index = joint.ok_or("set 必须指定 id")?;
                            let radians = p.radians.ok_or("set 必须指定 radians")?;
                            let (min, max) = range(index);
                            if !radians.is_finite() || radians < min - 1e-9 || radians > max + 1e-9
                            {
                                return Err(format!(
                                    "舵机 {} 目标超出机械限位 [{min:.6}, {max:.6}] rad",
                                    JOINT_IDS[index]
                                ));
                            }
                            if !s.fresh(now) {
                                return Err("舵机读数已过期，拒绝动作".into());
                            }
                            let position = s.sample.as_ref().unwrap().0[index];
                            if position < min - 0.002 || position > max + 0.002 {
                                return Err("当前实测角度超出机械限位，请先检查姿态和校准".into());
                            }
                            s.desired[index] = Some(radians.clamp(min, max));
                            s.off[index] = false;
                        }
                        JointsOp::Release => {
                            if p.radians.is_some() {
                                return Err("release 不接受角度".into());
                            }
                            if let Some(index) = joint {
                                s.desired[index] = None;
                                s.off[index] = s.powered[index];
                            } else {
                                s.stop(None);
                                return Ok(None);
                            }
                        }
                        _ => unreachable!(),
                    }
                    let session = s.session.as_mut().unwrap();
                    session.seq = seq;
                    session.seen = now;
                    Ok(None)
                }
            }
        })();
        match result {
            Ok(token) => s.result(now, None, token),
            Err(reason) => s.result(now, Some(reason), None),
        }
    }

    /// Recheck admission in the IO thread: a previously queued init may have run since begin.
    pub fn prepare(&self, sample: Option<&Sensors>, can_enter: bool, now: Instant) -> bool {
        let mut s = self.inner.lock().unwrap();
        if sample.is_some_and(|sample| sample.positions.iter().any(|v| !v.is_finite())) {
            s.sample = None;
        }
        let sample = sample.filter(|sample| sample.positions.iter().all(|v| v.is_finite()));
        if let Some(sample) = sample {
            s.sample = Some((sample.positions, now));
        }
        if s.session.as_ref().is_some_and(|session| !session.entered) {
            if !can_enter || sample.is_none() {
                s.stop(Some("进入条件已变化，未执行任何舵机调试动作".into()));
            } else {
                s.session.as_mut().unwrap().entered = true;
            }
        }
        s.active()
    }

    pub fn tick<T: RobotIo, U: ImuIo>(
        &self,
        safety: &mut Safety<T, U>,
        fresh: bool,
        dt: Duration,
        now: Instant,
    ) {
        let mut s = self.inner.lock().unwrap();
        if !fresh
            || !s.fresh(now)
            || safety.protection().is_some()
            || s.session
                .as_ref()
                .is_some_and(|session| now.saturating_duration_since(session.seen) >= LEASE)
        {
            s.stop(Some(
                "会话超时、读数中断或保护触发，已取消目标并卸力".into(),
            ));
        }
        Self::power_off(&mut s, safety);
        if s.session.is_some() {
            for index in 0..NUM_JOINTS {
                let Some(goal) = s.desired[index] else {
                    continue;
                };
                let (min, max) = range(index);
                let measured = s.sample.as_ref().unwrap().0[index];
                if measured < min - 0.002 || measured > max + 0.002 {
                    s.stop(Some("当前实测角度超出机械限位，已取消目标并卸力".into()));
                    break;
                }
                let result = (|| {
                    if !s.powered[index] {
                        let initial = measured.clamp(min, max);
                        // WritePosEx itself can power HD1910: own it before any attempted write.
                        s.powered[index] = true;
                        safety.write_joint(index, initial)?;
                        safety.set_joint_torque(index, true)?;
                        s.written[index] = Some(initial);
                    }
                    let previous = s.written[index].unwrap();
                    let step = SPEED * dt.as_secs_f64().min(0.1);
                    let next = previous + (goal - previous).clamp(-step, step);
                    if next != previous {
                        safety.write_joint(index, next)?;
                        s.written[index] = Some(next);
                    }
                    Ok::<_, duck_control::io::IoError>(())
                })();
                if let Err(error) = result {
                    s.stop(Some(format!("舵机写入失败，正在卸力：{error}")));
                    break;
                }
            }
        }
        Self::power_off(&mut s, safety);
    }
    fn power_off<T: RobotIo, U: ImuIo>(s: &mut Inner, safety: &mut Safety<T, U>) {
        for index in 0..NUM_JOINTS {
            if !s.off[index] {
                continue;
            }
            match safety.set_joint_torque(index, false) {
                Ok(()) => {
                    s.off[index] = false;
                    s.powered[index] = false;
                    s.written[index] = None;
                }
                Err(error) => {
                    s.stop(Some(format!(
                        "舵机 {} 卸力未确认，将继续重试：{error}",
                        JOINT_IDS[index]
                    )));
                }
            }
        }
    }
    pub fn targets(&self, positions: [f64; NUM_JOINTS]) -> Vec<f64> {
        let s = self.inner.lock().unwrap();
        positions
            .iter()
            .enumerate()
            .map(|(index, value)| s.written[index].unwrap_or(*value))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use duck_control::io::{IoError, Result};
    use duck_control::safety::SafetyConfig;
    use duck_control::{FakeImu, JointTargets, SlowSensors};
    use std::sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    };

    #[derive(Debug, PartialEq)]
    enum Event {
        Position(usize, f64),
        Torque(usize, bool),
    }
    #[derive(Default)]
    struct Bus {
        events: Mutex<Vec<Event>>,
        fail_write: AtomicBool,
        fail_off: AtomicBool,
    }
    struct Io(Arc<Bus>);
    impl RobotIo for Io {
        fn read(&mut self) -> Result<Sensors> {
            Ok(Sensors::default())
        }
        fn write(&mut self, _: &JointTargets) -> Result<()> {
            panic!("must not write unchecked joints")
        }
        fn set_torque(&mut self, _: bool) -> Result<()> {
            panic!("must not power unchecked joints")
        }
        fn set_gain(&mut self, _: u16) -> Result<()> {
            panic!("debug does not alter gains")
        }
        fn slow_sensors(&mut self) -> Result<SlowSensors> {
            Err(IoError::Simulated)
        }
        fn write_joint(&mut self, index: usize, radians: f64) -> Result<()> {
            self.0
                .events
                .lock()
                .unwrap()
                .push(Event::Position(index, radians));
            if self.0.fail_write.swap(false, Ordering::SeqCst) {
                Err(IoError::Simulated)
            } else {
                Ok(())
            }
        }
        fn set_joint_torque(&mut self, index: usize, on: bool) -> Result<()> {
            self.0.events.lock().unwrap().push(Event::Torque(index, on));
            if !on && self.0.fail_off.swap(false, Ordering::SeqCst) {
                Err(IoError::Simulated)
            } else {
                Ok(())
            }
        }
    }
    fn fixture() -> (Joints, Safety<Io, FakeImu>, Arc<Bus>, Instant, String) {
        let joints = Joints::default();
        let now = Instant::now();
        joints.prepare(Some(&Sensors::default()), true, now);
        let begin = joints.request_at(
            &JointsParams {
                op: JointsOp::Begin,
                ..Default::default()
            },
            true,
            now,
        );
        assert!(begin.accepted);
        joints.prepare(Some(&Sensors::default()), true, now);
        let bus = Arc::new(Bus::default());
        let safety = Safety::new(Io(bus.clone()), FakeImu::new(), SafetyConfig::default());
        (joints, safety, bus, now, begin.token.unwrap())
    }
    fn op(
        op: JointsOp,
        token: &str,
        seq: u64,
        id: Option<u8>,
        radians: Option<f64>,
    ) -> JointsParams {
        JointsParams {
            op,
            token: Some(token.into()),
            seq: Some(seq),
            id,
            radians,
        }
    }
    fn status(joints: &Joints, now: Instant) -> JointsResult {
        joints.request_at(&JointsParams::default(), false, now)
    }

    #[test]
    fn admission_requires_confirmed_off_and_fresh_positions_and_rechecks_before_io() {
        let joints = Joints::default();
        let now = Instant::now();
        let begin = JointsParams {
            op: JointsOp::Begin,
            ..Default::default()
        };
        assert!(!joints.request_at(&begin, true, now).accepted);
        joints.prepare(Some(&Sensors::default()), true, now);
        assert!(!joints.request_at(&begin, false, now).accepted);
        assert!(!joints.request_at(&begin, true, now + FRESH).accepted);
        let accepted = joints.request_at(&begin, true, now);
        assert!(accepted.accepted);
        assert!(
            accepted
                .joints
                .iter()
                .all(|j| !j.enabled && j.target.is_none())
        );
        assert!(status(&joints, now).token.is_none());
        assert!(!joints.prepare(Some(&Sensors::default()), false, now));
        assert!(status(&joints, now).error.is_some());
    }

    #[test]
    fn one_explicit_target_only_powers_that_joint_and_slews_from_actual_position() {
        let (joints, mut safety, bus, now, token) = fixture();
        joints.tick(&mut safety, true, Duration::from_millis(20), now);
        assert!(
            bus.events.lock().unwrap().is_empty(),
            "begin and heartbeats never move anything"
        );
        assert!(
            joints
                .request_at(
                    &op(JointsOp::Set, &token, 1, Some(30), Some(0.5)),
                    false,
                    now
                )
                .accepted
        );
        assert!(
            !status(&joints, now).joints[5].enabled,
            "accepted is queued, not completed IO"
        );
        joints.tick(&mut safety, true, Duration::from_millis(20), now);
        assert_eq!(
            *bus.events.lock().unwrap(),
            vec![
                Event::Position(5, 0.0),
                Event::Torque(5, true),
                Event::Position(5, 0.01)
            ]
        );
        let result = status(&joints, now);
        assert_eq!(result.joints.iter().filter(|j| j.enabled).count(), 1);
        assert_eq!(result.joints[5].target, Some(0.01));
        assert!(result.token.is_none());
    }

    #[test]
    fn rejects_limits_unknown_ids_stale_sequences_and_other_sessions_without_io() {
        let (joints, mut safety, bus, now, token) = fixture();
        for p in [
            op(
                JointsOp::Set,
                &token,
                1,
                Some(34),
                Some(std::f64::consts::PI),
            ),
            op(JointsOp::Set, &token, 1, Some(15), Some(0.0)),
            op(JointsOp::Set, &token, 1, Some(34), Some(f64::NAN)),
            op(JointsOp::Set, "old-token", 1, Some(34), Some(0.0)),
        ] {
            assert!(!joints.request_at(&p, false, now).accepted);
        }
        assert!(
            joints
                .request_at(&op(JointsOp::Keepalive, &token, 2, None, None), false, now)
                .accepted
        );
        assert!(
            !joints
                .request_at(
                    &op(JointsOp::Set, &token, 1, Some(34), Some(0.1)),
                    false,
                    now
                )
                .accepted
        );
        assert!(
            !joints
                .request_at(&op(JointsOp::Keepalive, &token, 2, None, None), false, now)
                .accepted
        );
        joints.tick(&mut safety, true, Duration::from_millis(20), now);
        assert!(bus.events.lock().unwrap().is_empty());
        let result = status(&joints, now);
        assert_eq!(result.joints.len(), 15);
        assert_eq!(result.joints[MOUTH_INDEX].id, 34);
        assert_eq!(result.joints[MOUTH_INDEX].max, MOUTH_OPEN);
    }

    #[test]
    fn release_only_cuts_its_joint_and_old_session_cannot_release_a_new_one() {
        let (joints, mut safety, bus, now, token) = fixture();
        for (seq, id) in [(1, 30), (2, 34)] {
            assert!(
                joints
                    .request_at(
                        &op(JointsOp::Set, &token, seq, Some(id), Some(0.0)),
                        false,
                        now
                    )
                    .accepted
            );
        }
        joints.tick(&mut safety, true, Duration::from_millis(20), now);
        bus.events.lock().unwrap().clear();
        assert!(
            joints
                .request_at(
                    &op(JointsOp::Release, &token, 3, Some(30), None),
                    false,
                    now
                )
                .accepted
        );
        joints.tick(&mut safety, true, Duration::from_millis(20), now);
        assert_eq!(*bus.events.lock().unwrap(), vec![Event::Torque(5, false)]);
        assert!(status(&joints, now).joints[MOUTH_INDEX].enabled);
        assert!(
            joints
                .request_at(&op(JointsOp::Release, &token, 4, None, None), false, now)
                .accepted
        );
        assert!(joints.active(), "reserve bus until shutdown completes");
        joints.tick(&mut safety, true, Duration::from_millis(20), now);
        assert!(!joints.active());
        let new = joints.request_at(
            &JointsParams {
                op: JointsOp::Begin,
                ..Default::default()
            },
            true,
            now,
        );
        assert!(new.accepted);
        assert_ne!(new.token.as_ref(), Some(&token));
        assert!(
            !joints
                .request_at(&op(JointsOp::Release, &token, 5, None, None), false, now)
                .accepted
        );
        assert!(joints.active());
    }

    #[test]
    fn lease_expiry_and_lost_read_only_cut_power_and_never_resume_policy() {
        for (fresh, elapsed) in [(true, LEASE), (false, Duration::from_millis(20))] {
            let (joints, mut safety, bus, now, token) = fixture();
            joints.request_at(
                &op(JointsOp::Set, &token, 1, Some(34), Some(0.2)),
                false,
                now,
            );
            joints.tick(&mut safety, true, Duration::from_millis(20), now);
            bus.events.lock().unwrap().clear();
            joints.tick(&mut safety, fresh, Duration::from_millis(20), now + elapsed);
            assert_eq!(
                *bus.events.lock().unwrap(),
                vec![Event::Torque(MOUTH_INDEX, false)]
            );
            assert!(!joints.active());
            assert!(
                !joints
                    .request_at(
                        &op(JointsOp::Keepalive, &token, 2, None, None),
                        false,
                        now + elapsed
                    )
                    .accepted
            );
        }
    }

    #[test]
    fn failed_seed_write_still_owns_possible_torque_and_retries_failed_shutdown() {
        let (joints, mut safety, bus, now, token) = fixture();
        bus.fail_write.store(true, Ordering::SeqCst);
        bus.fail_off.store(true, Ordering::SeqCst);
        joints.request_at(
            &op(JointsOp::Set, &token, 1, Some(34), Some(0.1)),
            false,
            now,
        );
        joints.tick(&mut safety, true, Duration::from_millis(20), now);
        assert!(joints.active());
        assert!(status(&joints, now).error.is_some());
        assert!(joints.blocks(&Call::RobotInit));
        joints.tick(&mut safety, true, Duration::from_millis(20), now);
        assert!(!joints.active());
        assert_eq!(
            *bus.events.lock().unwrap(),
            vec![
                Event::Position(MOUTH_INDEX, 0.0),
                Event::Torque(MOUTH_INDEX, false),
                Event::Torque(MOUTH_INDEX, false)
            ]
        );
    }

    #[test]
    fn nonfinite_or_stale_sample_cannot_continue_writing_old_targets() {
        for invalid in [false, true] {
            let (joints, mut safety, bus, now, token) = fixture();
            joints.request_at(
                &op(JointsOp::Set, &token, 1, Some(34), Some(0.2)),
                false,
                now,
            );
            joints.tick(&mut safety, true, Duration::from_millis(20), now);
            bus.events.lock().unwrap().clear();
            let at = if invalid {
                let mut sample = Sensors::default();
                sample.positions[0] = f64::NAN;
                joints.prepare(Some(&sample), false, now);
                now
            } else {
                now + FRESH
            };
            // A transport-success flag cannot override an invalid/old measurement.
            joints.tick(&mut safety, true, Duration::from_millis(20), at);
            assert_eq!(
                *bus.events.lock().unwrap(),
                vec![Event::Torque(MOUTH_INDEX, false)]
            );
            assert!(!joints.active());
        }
    }

    #[test]
    fn changed_measured_position_is_revalidated_before_first_power_or_position_write() {
        let (joints, mut safety, bus, now, token) = fixture();
        assert!(
            joints
                .request_at(
                    &op(JointsOp::Set, &token, 1, Some(34), Some(0.2)),
                    false,
                    now
                )
                .accepted
        );
        let mut sample = Sensors::default();
        sample.positions[MOUTH_INDEX] = MOUTH_OPEN + 0.2;
        joints.prepare(Some(&sample), false, now);
        joints.tick(&mut safety, true, Duration::from_millis(20), now);
        assert!(
            bus.events.lock().unwrap().is_empty(),
            "do not snap back from an invalid measured pose"
        );
        assert!(!joints.active());
        assert!(status(&joints, now).error.is_some());
    }

    #[test]
    fn blocks_all_other_motion_sources_but_keeps_status_stop_and_speech_off() {
        let (joints, _, _, _, _) = fixture();
        for (method, params) in [
            ("robot.move", serde_json::json!({})),
            ("robot.head", serde_json::json!({})),
            ("robot.pose", serde_json::json!({})),
            ("robot.mouth", serde_json::json!({})),
            ("robot.sound", serde_json::json!({"tag":"chirp"})),
            ("robot.do", serde_json::json!({"skill":"ground_pick"})),
            ("robot.enable", serde_json::json!({"on":true})),
            ("robot.init", serde_json::json!({})),
            ("robot.look", serde_json::json!({"x":1,"y":0,"z":0})),
            ("robot.setMode", serde_json::json!({"mode":"walk"})),
            ("robot.theremin", serde_json::json!({"active":true})),
            ("robot.chorale", serde_json::json!({"active":true})),
        ] {
            let request: duck_ipc_proto::Request = serde_json::from_value(
                serde_json::json!({"jsonrpc":"2.0","id":1,"method":method,"params":params}),
            )
            .unwrap();
            let call = request.as_call().unwrap();
            assert!(joints.blocks(&call), "{method}");
        }
        assert!(
            joints.blocks(&Call::RobotSpeech(duck_ipc_proto::FeatureParams {
                enabled: Some(true)
            }))
        );
        for call in [
            Call::RobotStop,
            Call::RobotHealth,
            Call::RobotRelax,
            Call::RobotSpeech(duck_ipc_proto::FeatureParams {
                enabled: Some(false),
            }),
        ] {
            assert!(!joints.blocks(&call));
        }
    }
}
