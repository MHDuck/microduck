//! The safety authority.
//!
//! **It owns the only write handle to the robot.** Nothing above it — not the policy, not
//! the arbiter, not a client — holds a [`RobotIo`], so nothing above it *can* command a
//! motor. The invariant is enforced by the borrow checker rather than by a rule someone has
//! to remember when adding the eighth skill the night before a demo.
//!
//! That is the same argument the updater makes about its recovery path: code that only runs
//! once something has already gone wrong is the code most likely to be quietly broken, so
//! make the broken state unrepresentable instead.
//!
//! Two rules, unconditional:
//!
//!  - **Non-finite rejection.** A `NaN` target is not clamped, it is refused outright.
//!  - **Range clamp.** Targets are held inside the actuator's travel.
//!
//! Plus a deadman on the command itself: if intents stop arriving, the velocity goes to
//! zero. **Stop is not limp** — losing comms makes the robot *stand still*, because standing
//! is the safe state for a biped; losing balance is a different event, and it is not this
//! layer's to answer.
//!
//! Overcurrent latches until restart; explicit bounded stand-up may release a fall latch.
//! Both `apply` and `set_torque` enforce any remaining latch.
//! With fall protection disabled, `fallen` remains a report for the optional limp-fall mode.

use std::time::Duration;

use crate::io::{ImuIo, IoError, JointTargets, RobotIo, Sensors};
use crate::model::NUM_JOINTS;
use crate::obs::Command;

/// The XL330's position range: one turn, centred, from the count↔radian conversion.
///
/// This is the *actuator's* travel, not a per-joint anatomical limit — the alpha robot's
/// real joint limits live in the MJCF, which is not vendored here. So this catches a policy
/// emitting `NaN`, an absurd action scale, or a garbage tensor; it will not stop a joint
/// being driven somewhere mechanically unwise. Recorded plainly rather than dressed up,
/// because a limit that looks per-joint but is not would imply protection nobody has.
pub const ACTUATOR_MIN: f64 = -std::f64::consts::PI;
pub const ACTUATOR_MAX: f64 = std::f64::consts::PI;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SafetyConfig {
    pub fall_protection: bool,
    pub overcurrent_protection: bool,
    pub overcurrent_cutoff_a: f64,
    pub overcurrent_debounce_ticks: u32,
    /// Projected-gravity z above which the robot counts as falling. Upright reads about
    /// -1.0 and a robot on its side reads near 0.
    pub fall_gravity_z: f64,
    /// How long that must hold before it counts. Debounced so a hard footfall is not a
    /// fall.
    pub fall_debounce: Duration,
    /// Intent age past which the velocity command is zeroed.
    pub deadman: Duration,
    /// Gain while running.
    pub gain_running: u16,
    /// Gain to yield at rather than fight the floor. Nothing here applies it — `robotd`
    /// commands it during limp-fall — but it lives with the other safety numbers because
    /// it is one: the gain at which the robot stops pushing back.
    pub gain_limp: u16,
}

impl Default for SafetyConfig {
    fn default() -> Self {
        Self {
            // Library callers opt in; robotd enables both through its SafetyParams defaults.
            fall_protection: false,
            overcurrent_protection: false,
            overcurrent_cutoff_a: 18.0,
            overcurrent_debounce_ticks: 2,
            // The prototype's numbers.
            fall_gravity_z: -0.5,
            fall_debounce: Duration::from_millis(200),
            deadman: Duration::from_millis(500),
            gain_running: 200,
            gain_limp: 50,
        }
    }
}

/// Why a commanded value was not applied as asked. Surfaced so a client can be told,
/// rather than watching the robot ignore it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Limit {
    FallProtection,
    Overcurrent,
    /// Intents went stale; the velocity was zeroed.
    Deadman,
    /// A target was outside the actuator's travel.
    Range,
    /// A target was `NaN` or infinite.
    NotFinite,
}

/// What safety did with a tick's worth of targets.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Applied {
    pub limits: Vec<Limit>,
}

impl Applied {
    pub fn limited_by(&self, limit: Limit) -> bool {
        self.limits.contains(&limit)
    }
}

pub struct Safety<T: RobotIo, U: ImuIo> {
    io: T,
    imu: U,
    config: SafetyConfig,
    /// How long gravity has been past the threshold. Reset by any upright sample.
    falling_for: Duration,
    fallen: bool,
    fall_recovery: bool,
    overcurrent_ticks: u32,
    protection: Option<Limit>,
    protection_torque_off: bool,
    /// Tracks the last gain written so an unchanged one is not rewritten every tick — that
    /// would be fifteen bus writes per tick for no reason.
    gain: Option<u16>,
}

impl<T: RobotIo, U: ImuIo> Safety<T, U> {
    pub fn new(io: T, imu: U, config: SafetyConfig) -> Self {
        Self {
            io,
            imu,
            config,
            falling_for: Duration::ZERO,
            fallen: false,
            fall_recovery: false,
            overcurrent_ticks: 0,
            protection: None,
            protection_torque_off: false,
            gain: None,
        }
    }

    pub fn read(&mut self) -> Result<Sensors, IoError> {
        let sensors = match self.io.read() {
            Ok(sensors) => sensors,
            Err(e) => {
                self.overcurrent_ticks = 0;
                self.falling_for = Duration::ZERO;
                return Err(e);
            }
        };
        if self.config.overcurrent_protection && self.protection.is_none() {
            let total_a = sensors.input_current_a.unwrap_or_else(|| {
                sensors.currents_ma.iter().map(|v| v.abs()).sum::<f64>() / 1000.0
            });
            if !total_a.is_finite() || total_a >= self.config.overcurrent_cutoff_a {
                self.overcurrent_ticks = self.overcurrent_ticks.saturating_add(1);
            } else {
                self.overcurrent_ticks = 0;
            }
            if self.overcurrent_ticks >= self.config.overcurrent_debounce_ticks.max(1) {
                self.protection = Some(Limit::Overcurrent);
                tracing::error!(total_a, cutoff_a = self.config.overcurrent_cutoff_a,
                    "overcurrent protection latched; restart robotd after clearing the fault");
            }
        }
        Ok(sensors)
    }

    pub fn protection(&self) -> Option<Limit> {
        self.protection
    }

    pub fn begin_fall_recovery(&mut self) -> Result<(), &'static str> {
        if self.protection == Some(Limit::Overcurrent) {
            return Err("overcurrent protection latched; clear the fault and restart robotd");
        }
        self.protection = None;
        self.protection_torque_off = false;
        self.fall_recovery = true;
        Ok(())
    }

    pub fn end_fall_recovery(&mut self) {
        self.fall_recovery = false;
        self.falling_for = Duration::ZERO;
    }

    pub fn latch_fall_protection(&mut self) {
        if self.protection.is_none() {
            self.protection = Some(Limit::FallProtection);
            tracing::error!("fall protection latched; restart robotd after clearing the fault");
        }
    }

    /// Retry failed torque-off writes even when there are no fresh sensor samples.
    pub fn enforce_protection(&mut self) -> Result<(), IoError> {
        if self.protection.is_some() && !self.protection_torque_off {
            self.set_torque(false)?;
        }
        Ok(())
    }

    /// Bus reads that are not part of the tick, passed through rather than exposing the IO.
    ///
    /// Safety owns the only `RobotIo` handle — that is what makes "nothing commands a motor
    /// except through here" a fact the borrow checker enforces rather than a convention. Slow
    /// sensors and IMU diagnostics read rather than write, so they are no threat to that, but
    /// handing out the handle to reach them would be.
    pub fn slow_sensors(&mut self) -> Result<crate::io::SlowSensors, IoError> {
        self.io.slow_sensors()
    }

    pub fn imu_stale(&self) -> crate::io::ImuStale {
        self.imu.stale()
    }

    pub fn imu_ready(&self) -> bool {
        self.imu.ready()
    }

    /// The IMU half of the composition point.
    ///
    /// `robotd` calls [`RobotIo::read`] for the servo half and then this, on the same tick,
    /// to fill [`Sensors::imu`] — the two buses are independent, so only the caller can say
    /// "one sample of both". A failed [`ImuIo::read`] degrades the tick (the caller keeps its
    /// last sample) rather than failing it.
    ///
    /// Exposing the IMU itself does not break safety's write-handle invariant: the IMU never
    /// commands a motor, and the only `RobotIo` in the crate still lives here.
    pub fn imu(&mut self) -> &mut U {
        &mut self.imu
    }

    pub fn fallen(&self) -> bool {
        self.fallen
    }

    /// Power the joints, so the positions this writes can actually be held.
    ///
    /// Through here because [`Safety`] owns the only [`RobotIo`] handle — the same reason
    /// [`Self::slow_sensors`] is a passthrough. Unlike that one this *does* affect the robot, so it
    /// is worth being explicit about what it is and is not:
    ///
    ///  - It is called when a human enables the policy on a limp robot, once, not per tick.
    ///  - It is **not** called at startup. A `robotd` restarted by an update must leave a standing
    ///    robot standing, and nothing here changes that.
    ///  - A latched protection refuses torque-on until the daemon is restarted.
    pub fn set_torque(&mut self, on: bool) -> Result<(), IoError> {
        if on && self.protection.is_some() {
            return Err(IoError::Bus("safety protection latched; restart robotd after clearing the fault".into()));
        }
        tracing::warn!(on, "torque");
        self.io.set_torque(on)?;
        if !on && self.protection.is_some() {
            self.protection_torque_off = true;
        }
        Ok(())
    }

    /// Power one joint without changing the torque state of the rest of the robot.
    pub fn set_joint_torque(&mut self, joint: usize, on: bool) -> Result<(), IoError> {
        if on && self.protection.is_some() {
            return Err(IoError::Bus(
                "safety protection latched; isolated torque-on refused".into(),
            ));
        }
        self.io.set_joint_torque(joint, on)
    }

    /// Command one isolated joint through the same finite/range checks as a full target set.
    pub fn write_joint(&mut self, joint: usize, target: f64) -> Result<(), IoError> {
        if !target.is_finite() {
            return Err(IoError::Bus("non-finite isolated joint target".to_owned()));
        }
        self.io
            .write_joint(joint, target.clamp(ACTUATOR_MIN, ACTUATOR_MAX))
    }

    /// The gain last written to the servos, or `None` before the first write. This is what
    /// the robot is running at, which is not always what the caller asked for.
    pub fn gain(&self) -> Option<u16> {
        self.gain
    }

    pub fn fall_elapsed(&self) -> Duration {
        self.falling_for
    }

    /// Update fall state from a fresh sample. Call every tick, before [`Self::apply`].
    ///
    /// Debounced in both directions: a robot has to be down for `fall_debounce` to count as
    /// fallen, and one upright sample clears the accumulator. Without the debounce, the
    /// impulse from a firm footfall reads as a fall.
    pub fn observe(&mut self, sensors: &Sensors, dt: Duration) {
        self.observe_with_recovery(sensors, dt, false);
    }

    pub fn observe_with_recovery(&mut self, sensors: &Sensors, dt: Duration, allow_recovery: bool) {
        // An orientation filter that has not converged does not get a vote.
        //
        // The SFLP filter needs a few seconds of samples before its quaternion means
        // anything, and until then projected gravity is not `[0, 0, -1]` — it is whatever the
        // filter is mid-way through deciding, which reads as "above the fall threshold", which
        // reads as "on its side". Two hundred milliseconds of that and an upright robot on a
        // bench latches `fallen` at startup: `apply` writes `gain_limp`, the policy is refused
        // with "the robot is down; stand it up first", and it clears itself a few seconds
        // later leaving the gain behind at 50 with nothing to explain it.
        //
        // Observed on a board: a joint set to 137 by hand read back 50 after five seconds of
        // robotd, while `robotctl monitor` — sampled later, after convergence — reported `ok`.
        //
        // Holding the previous verdict is the safe default in both directions. At startup that
        // is "not fallen", which is what the robot standing on the bench actually is; and a
        // filter that stops being ready mid-run leaves a fallen robot fallen.
        if !self.imu.ready() {
            self.falling_for = Duration::ZERO;
            return;
        }
        // A repeated sample adds no time, but must not erase a sustained tilt.
        if self.imu.stale().run > 0 {
            return;
        }

        let down = sensors.imu.gravity[2] > self.config.fall_gravity_z;
        if down {
            let previous = self.falling_for;
            self.falling_for = self.falling_for.saturating_add(dt);
            if self.falling_for >= self.config.fall_debounce {
                if !self.fallen {
                    tracing::warn!(
                        gravity_z = sensors.imu.gravity[2],
                        elapsed_ms = self.falling_for.as_millis(),
                        fall_protection = self.config.fall_protection,
                        standup_recovery = self.fall_recovery,
                        "fall detected"
                    );
                }
                self.fallen = true;
            } else if previous.is_zero() || self.falling_for.as_secs() > previous.as_secs() {
                tracing::warn!(
                    gravity_z = sensors.imu.gravity[2],
                    threshold_z = self.config.fall_gravity_z,
                    elapsed_ms = self.falling_for.as_millis(),
                    required_ms = self.config.fall_debounce.as_millis(),
                    fall_protection = self.config.fall_protection,
                    standup_recovery = self.fall_recovery,
                    "fall pending"
                );
            }
        } else {
            if !self.falling_for.is_zero() || self.fallen {
                tracing::warn!(
                    gravity_z = sensors.imu.gravity[2],
                    elapsed_ms = self.falling_for.as_millis(),
                    protection = ?self.protection,
                    "fall angle recovered; timer reset"
                );
            }
            self.falling_for = Duration::ZERO;
            self.fallen = false;
        }
        if self.config.fall_protection
            && !self.fall_recovery
            && !allow_recovery
            && self.fallen
            && self.protection.is_none()
        {
            self.latch_fall_protection();
        }
    }

    /// Apply the deadman to a command.
    ///
    /// Zeroes the *twist* only. Head targets are left alone deliberately: a stale head pose
    /// is harmless, while a stale velocity walks the robot into a wall.
    pub fn gate(&self, command: Command, intent_age: Duration) -> (Command, Option<Limit>) {
        if intent_age <= self.config.deadman {
            return (command, None);
        }
        let mut stopped = command;
        stopped.twist = [0.0; 3];
        (stopped, Some(Limit::Deadman))
    }

    /// The only path to the motors.
    ///
    /// `hold` is what to command when the policy must not drive — normally the pose the
    /// robot is already in.
    /// `running_gain` is what the caller wants — the standing policy runs softer than the
    /// walking one, and limp-fall softer still. All of those are control decisions, not
    /// safety ones, so they are passed in rather than second-guessed here.
    pub fn apply(
        &mut self,
        targets: [f64; NUM_JOINTS],
        hold: [f64; NUM_JOINTS],
        running_gain: u16,
    ) -> Result<Applied, IoError> {
        let mut applied = Applied::default();

        if let Some(reason) = self.protection {
            self.enforce_protection()?;
            applied.limits.push(reason);
            return Ok(applied);
        }
        self.set_gain(running_gain)?;

        // Non-finite is refused, not clamped. Clamping `NaN` silently produces a boundary
        // value, which is a plausible-looking joint angle — far worse than declining to
        // move, because the robot would lurch to a limit rather than hold still.
        if targets.iter().any(|v| !v.is_finite()) {
            applied.limits.push(Limit::NotFinite);
            self.io.write(&JointTargets::new(hold))?;
            return Ok(applied);
        }

        let mut safe = targets;
        for value in safe.iter_mut() {
            let clamped = value.clamp(ACTUATOR_MIN, ACTUATOR_MAX);
            if clamped != *value {
                if !applied.limited_by(Limit::Range) {
                    applied.limits.push(Limit::Range);
                }
                *value = clamped;
            }
        }

        self.io.write(&JointTargets::new(safe))?;
        Ok(applied)
    }

    fn set_gain(&mut self, kp: u16) -> Result<(), IoError> {
        if self.gain == Some(kp) {
            return Ok(());
        }
        self.io.set_gain(kp)?;
        self.gain = Some(kp);
        Ok(())
    }

    /// Borrow the wrapped IO. Test-only, and deliberately not public: handing this out in
    /// production would defeat the point of safety owning the writer.
    #[cfg(test)]
    fn io(&self) -> &T {
        &self.io
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::imu::ImuData;
    use crate::io::{FakeImu, FakeIo};
    use crate::model::DEFAULT_POSITION;

    fn upright() -> Sensors {
        Sensors::default() // gravity defaults to [0, 0, -1]
    }

    fn on_its_side() -> Sensors {
        Sensors {
            imu: ImuData {
                gravity: [-1.0, 0.0, 0.0],
                ..ImuData::default()
            },
            ..Sensors::default()
        }
    }

    fn safety() -> Safety<FakeIo, FakeImu> {
        Safety::new(
            FakeIo::at(DEFAULT_POSITION),
            FakeImu::new(),
            SafetyConfig::default(),
        )
    }

    #[test]
    fn an_isolated_mouth_command_does_not_touch_the_other_joints() {
        let mut safety = safety();
        let mouth = crate::model::MOUTH_INDEX;
        safety.set_joint_torque(mouth, true).unwrap();
        safety.write_joint(mouth, 0.25).unwrap();

        assert_eq!(safety.io().last_joint_torque, Some((mouth, true)));
        assert_eq!(safety.io().last_joint_written, Some((mouth, 0.25)));
        assert_eq!(safety.io().writes, 0, "the full 15-joint path must stay idle");
        assert_eq!(
            safety.io().torque_writes,
            0,
            "full-body torque must stay idle"
        );
    }

    /// A hard footfall spikes gravity briefly. Treating that as a fall would drop the robot
    /// mid-stride — which is itself how you cause a fall.
    #[test]
    fn a_brief_tilt_is_not_a_fall() {
        let mut s = safety();
        s.observe(&on_its_side(), Duration::from_millis(100));
        assert!(!s.fallen(), "100ms is under the 200ms debounce");
        s.observe(&upright(), Duration::from_millis(20));
        s.observe(&on_its_side(), Duration::from_millis(100));
        assert!(!s.fallen(), "an upright sample must reset the accumulator");
    }

    /// Sustained means fallen.
    #[test]
    fn a_sustained_tilt_is_a_fall() {
        let mut s = safety();
        for _ in 0..11 {
            s.observe(&on_its_side(), Duration::from_millis(20));
        }
        assert!(s.fallen());
    }

    /// Falling must drop the gain, not merely stop commanding. Refusing to command only
    /// freezes the robot in the pose it fell in; going soft lets it yield.
    /// **The regression.** An orientation filter that has not converged must not be able to
    /// declare a robot fallen.
    ///
    /// On a board this cost an afternoon: an upright robot latched `fallen` within 200 ms of
    /// startup, `apply` wrote `gain_limp`, `padd` was refused with "the robot is down; stand
    /// it up first", and a few seconds later it cleared itself — leaving the servos at kP 50
    /// with `robotctl monitor` reporting `ok`, because the gain is only written when it
    /// changes. Nothing in the observable state explained it.
    #[test]
    fn an_unconverged_imu_cannot_declare_a_fall() {
        let mut imu = FakeImu::new();
        imu.ready = false;
        let mut safety = Safety::new(FakeIo::at(DEFAULT_POSITION), imu, SafetyConfig::default());

        // Gravity well above the threshold — what an unconverged filter reports, and what
        // a robot on its side reports. The difference is only whether the filter is ready.
        let mut sensors = Sensors::default();
        sensors.imu.gravity = [0.0, 0.0, 0.0];

        for _ in 0..50 {
            safety.observe(&sensors, Duration::from_millis(20));
        }
        assert!(
            !safety.fallen(),
            "a robot was called fallen on an orientation filter that had not converged"
        );
    }

    /// And once it has converged, the same sample must be believed — otherwise the guard
    /// above would simply disable fall detection.
    #[test]
    fn a_converged_imu_still_detects_a_fall() {
        let mut imu = FakeImu::new();
        imu.ready = true;
        let mut safety = Safety::new(FakeIo::at(DEFAULT_POSITION), imu, SafetyConfig::default());

        let mut sensors = Sensors::default();
        sensors.imu.gravity = [0.0, 0.0, 0.0];

        for _ in 0..50 {
            safety.observe(&sensors, Duration::from_millis(20));
        }
        assert!(
            safety.fallen(),
            "a converged filter must still report a fall"
        );
    }

    /// With the gate OFF — the default, the prototype's behaviour — a fall changes
    /// nothing about what gets written: the verdict is reported, the policy keeps
    /// driving, the gain stays the caller's. Going limp under someone adjusting the
    /// robot's lean is exactly the annoyance this default removes.
    #[test]
    fn by_default_a_fall_reports_but_does_not_preempt() {
        let mut s = safety();
        for _ in 0..11 {
            s.observe(&on_its_side(), Duration::from_millis(20));
        }
        assert!(s.fallen(), "the verdict is still tracked");

        let mut wanted = DEFAULT_POSITION;
        wanted[0] = 0.9;
        let applied = s
            .apply(
                wanted,
                DEFAULT_POSITION,
                SafetyConfig::default().gain_running,
            )
            .unwrap();
        assert!(applied.limits.is_empty(), "{:?}", applied.limits);
        assert_eq!(
            s.io().last_written.unwrap().positions,
            wanted,
            "the policy keeps driving"
        );
        assert_eq!(s.io().last_gain, Some(SafetyConfig::default().gain_running));
    }

    /// **The fall verdict preempts nothing.** It is tracked, it is published, and it does
    /// not touch the motors: a fallen robot is driven exactly like an upright one, at the
    /// gain the caller asked for.
    ///
    /// This is the contract that lets limp-fall live entirely above this layer — it drops
    /// the gain by *asking*, through the same `apply` as everything else, with no exemption
    /// to special-case. A gate here would have to be bypassed for the pose ramp to move a
    /// robot lying on the floor, and a safety rule with a bypass is not one.
    #[test]
    fn a_fall_does_not_preempt_the_caller() {
        let mut s = safety();
        for _ in 0..11 {
            s.observe(&on_its_side(), Duration::from_millis(20));
        }
        assert!(s.fallen(), "the verdict is still tracked");

        let mut wanted = DEFAULT_POSITION;
        wanted[0] = 0.9;
        let applied = s
            .apply(
                wanted,
                DEFAULT_POSITION,
                SafetyConfig::default().gain_running,
            )
            .unwrap();

        assert!(applied.limits.is_empty(), "{:?}", applied.limits);
        assert_eq!(
            s.io().last_written.unwrap().positions,
            wanted,
            "the caller keeps driving a robot that is down"
        );
        assert_eq!(s.io().last_gain, Some(SafetyConfig::default().gain_running));
    }

    /// The other half: a caller that *wants* to go soft says so, and it goes straight
    /// through. This is exactly what `robotd` does during limp-fall.
    #[test]
    fn a_caller_that_asks_for_the_limp_gain_gets_it() {
        let mut s = safety();
        s.observe(&upright(), Duration::from_millis(20));
        let limp = SafetyConfig::default().gain_limp;

        s.apply(DEFAULT_POSITION, DEFAULT_POSITION, limp).unwrap();
        assert_eq!(s.io().last_gain, Some(limp));
        assert_eq!(
            s.gain(),
            Some(limp),
            "and it is reported as what is running"
        );
    }

    /// `NaN` is refused outright. Clamping it would yield a boundary value — a plausible
    /// joint angle — and the robot would lurch to a limit instead of holding still.
    #[test]
    fn a_non_finite_target_is_refused_not_clamped() {
        let mut s = safety();
        s.observe(&upright(), Duration::from_millis(20));

        let mut poisoned = DEFAULT_POSITION;
        poisoned[3] = f64::NAN;
        let applied = s
            .apply(
                poisoned,
                DEFAULT_POSITION,
                SafetyConfig::default().gain_running,
            )
            .unwrap();

        assert!(applied.limited_by(Limit::NotFinite));
        assert!(!applied.limited_by(Limit::Range), "must not be clamped");
        assert_eq!(s.io().last_written.unwrap().positions, DEFAULT_POSITION);
    }

    /// Out-of-range targets are clamped and reported. Reported matters: a client whose
    /// command was silently altered has no way to know why the robot is not doing as asked.
    #[test]
    fn out_of_range_targets_are_clamped_and_reported() {
        let mut s = safety();
        s.observe(&upright(), Duration::from_millis(20));

        let mut wild = DEFAULT_POSITION;
        wild[2] = 100.0;
        wild[7] = -100.0;
        let applied = s
            .apply(wild, DEFAULT_POSITION, SafetyConfig::default().gain_running)
            .unwrap();

        assert!(applied.limited_by(Limit::Range));
        let written = s.io().last_written.unwrap().positions;
        assert_eq!(written[2], ACTUATOR_MAX);
        assert_eq!(written[7], ACTUATOR_MIN);
    }

    /// An ordinary tick must pass through untouched, or the clamp is silently mangling
    /// normal operation and every other test here proves nothing.
    #[test]
    fn an_ordinary_target_passes_through_unchanged() {
        let mut s = safety();
        s.observe(&upright(), Duration::from_millis(20));

        let applied = s
            .apply(
                DEFAULT_POSITION,
                DEFAULT_POSITION,
                SafetyConfig::default().gain_running,
            )
            .unwrap();
        assert!(applied.limits.is_empty(), "{:?}", applied.limits);
        assert_eq!(s.io().last_written.unwrap().positions, DEFAULT_POSITION);
        assert_eq!(s.io().last_gain, Some(SafetyConfig::default().gain_running));
    }

    /// The deadman zeroes the twist and nothing else. Losing comms should make the robot
    /// stand still — not collapse, and not forget where its head was pointing.
    #[test]
    fn the_deadman_zeroes_the_twist_only() {
        let s = safety();
        let command = Command {
            twist: [0.5, 0.0, 0.3],
            head: [0.1, 0.2, 0.3, 0.4],
            ..Command::default()
        };

        let (fresh, limit) = s.gate(command, Duration::from_millis(100));
        assert_eq!(fresh, command, "a fresh intent passes through");
        assert!(limit.is_none());

        let (stale, limit) = s.gate(command, Duration::from_secs(5));
        assert_eq!(stale.twist, [0.0; 3], "velocity must stop");
        assert_eq!(stale.head, command.head, "head is harmless when stale");
        assert_eq!(limit, Some(Limit::Deadman));
    }

    /// The gain is written once per transition, not once per tick. At 50 Hz the naive
    /// version would be 750 extra bus writes a second, on the bus the control loop needs.
    #[test]
    fn the_gain_is_only_written_when_it_changes() {
        let mut s = safety();
        s.observe(&upright(), Duration::from_millis(20));
        s.apply(
            DEFAULT_POSITION,
            DEFAULT_POSITION,
            SafetyConfig::default().gain_running,
        )
        .unwrap();
        s.apply(
            DEFAULT_POSITION,
            DEFAULT_POSITION,
            SafetyConfig::default().gain_running,
        )
        .unwrap();
        s.apply(
            DEFAULT_POSITION,
            DEFAULT_POSITION,
            SafetyConfig::default().gain_running,
        )
        .unwrap();

        // Three applies, three position writes, but only the first gain write.
        assert_eq!(s.io().writes, 3);
        assert_eq!(s.io().last_gain, Some(SafetyConfig::default().gain_running));
        assert_eq!(s.gain, Some(SafetyConfig::default().gain_running));
    }
}
