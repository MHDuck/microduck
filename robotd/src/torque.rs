//! Report completed torque writes without changing the control or safety logic.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use duck_control::io::{Result, RobotIo};
use duck_control::{JointTargets, NUM_JOINTS, Sensors, SlowSensors};

pub(crate) struct ReportedIo<T> {
    io: T,
    off: Arc<AtomicBool>,
    known_off: [bool; NUM_JOINTS],
}

impl<T> ReportedIo<T> {
    pub(crate) fn new(io: T, off: Arc<AtomicBool>) -> Self {
        off.store(false, Ordering::SeqCst);
        Self {
            io,
            off,
            known_off: [false; NUM_JOINTS],
        }
    }

    fn invalidate(&mut self) {
        self.known_off.fill(false);
        self.off.store(false, Ordering::SeqCst);
    }

    fn observe<R>(&mut self, result: Result<R>) -> Result<R> {
        if result.is_err() {
            self.invalidate();
        }
        result
    }

    fn publish(&self) {
        self.off
            .store(self.known_off.iter().all(|off| *off), Ordering::SeqCst);
    }
}

impl<T: RobotIo> RobotIo for ReportedIo<T> {
    fn read(&mut self) -> Result<Sensors> {
        let result = self.io.read();
        self.observe(result)
    }

    fn write(&mut self, targets: &JointTargets) -> Result<()> {
        let result = self.io.write(targets);
        self.observe(result)
    }

    fn write_joint(&mut self, joint: usize, target: f64) -> Result<()> {
        let result = self.io.write_joint(joint, target);
        self.observe(result)
    }

    fn set_gain(&mut self, kp: u16) -> Result<()> {
        let result = self.io.set_gain(kp);
        self.observe(result)
    }

    fn slow_sensors(&mut self) -> Result<SlowSensors> {
        let result = self.io.slow_sensors();
        self.observe(result)
    }

    fn set_torque(&mut self, on: bool) -> Result<()> {
        // Clear before the write: health is served concurrently with blocking bus IO.
        self.invalidate();
        self.io.set_torque(on)?;
        self.known_off.fill(!on);
        self.publish();
        Ok(())
    }

    fn set_joint_torque(&mut self, joint: usize, on: bool) -> Result<()> {
        self.off.store(false, Ordering::SeqCst);
        let Some(known) = self.known_off.get_mut(joint) else {
            self.invalidate();
            return self.io.set_joint_torque(joint, on);
        };
        *known = false;
        let result = self.io.set_joint_torque(joint, on);
        self.observe(result)?;
        self.known_off[joint] = !on;
        self.publish();
        Ok(())
    }
}

impl<T> Drop for ReportedIo<T> {
    fn drop(&mut self) {
        self.off.store(false, Ordering::SeqCst);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use duck_control::FakeIo;
    use duck_control::io::IoError;
    use duck_control::model::MOUTH_INDEX;

    struct FaultIo {
        io: FakeIo,
        reported: Arc<AtomicBool>,
        fail_torque: bool,
    }

    impl RobotIo for FaultIo {
        fn read(&mut self) -> Result<Sensors> {
            self.io.read()
        }
        fn write(&mut self, targets: &JointTargets) -> Result<()> {
            self.io.write(targets)
        }
        fn write_joint(&mut self, joint: usize, target: f64) -> Result<()> {
            self.io.write_joint(joint, target)
        }
        fn set_gain(&mut self, kp: u16) -> Result<()> {
            self.io.set_gain(kp)
        }
        fn slow_sensors(&mut self) -> Result<SlowSensors> {
            self.io.slow_sensors()
        }
        fn set_torque(&mut self, on: bool) -> Result<()> {
            assert!(
                !self.reported.load(Ordering::SeqCst),
                "clear before touching hardware"
            );
            if self.fail_torque {
                return Err(IoError::Simulated);
            }
            self.io.set_torque(on)
        }
        fn set_joint_torque(&mut self, joint: usize, on: bool) -> Result<()> {
            assert!(
                !self.reported.load(Ordering::SeqCst),
                "clear before touching beak"
            );
            if self.fail_torque {
                return Err(IoError::Simulated);
            }
            self.io.set_joint_torque(joint, on)
        }
    }

    fn fixture() -> (ReportedIo<FaultIo>, Arc<AtomicBool>) {
        let flag = Arc::new(AtomicBool::new(false));
        let io = FaultIo {
            io: FakeIo::new(),
            reported: Arc::clone(&flag),
            fail_torque: false,
        };
        (ReportedIo::new(io, Arc::clone(&flag)), flag)
    }

    #[test]
    fn only_successful_all_joint_shutdown_confirms_unknown_state() {
        let (mut io, flag) = fixture();
        assert!(!flag.load(Ordering::SeqCst));
        io.set_joint_torque(MOUTH_INDEX, false).unwrap();
        assert!(!flag.load(Ordering::SeqCst), "body state remains unknown");
        io.set_torque(false).unwrap();
        assert!(flag.load(Ordering::SeqCst));
        io.set_torque(true).unwrap();
        assert!(!flag.load(Ordering::SeqCst));
    }

    #[test]
    fn isolated_beak_temporarily_revokes_all_off_confirmation() {
        let (mut io, flag) = fixture();
        io.set_torque(false).unwrap();
        io.set_joint_torque(MOUTH_INDEX, true).unwrap();
        assert!(!flag.load(Ordering::SeqCst));
        io.set_joint_torque(MOUTH_INDEX, false).unwrap();
        assert!(flag.load(Ordering::SeqCst));
        assert_eq!(io.io.io.torque, Some(false), "only the beak was powered");
    }

    #[test]
    fn failed_writes_and_lost_bus_never_confirm_torque_off() {
        let (mut io, flag) = fixture();
        io.set_torque(false).unwrap();
        io.io.fail_torque = true;
        assert!(io.set_torque(false).is_err());
        assert!(!flag.load(Ordering::SeqCst));
        io.io.fail_torque = false;
        io.set_torque(false).unwrap();
        io.io.io.fail_next_read = true;
        assert!(io.read().is_err());
        io.read().unwrap();
        assert!(
            !flag.load(Ordering::SeqCst),
            "a recovered position read is not torque confirmation"
        );
        io.set_torque(false).unwrap();
        io.io.fail_torque = true;
        assert!(io.set_joint_torque(MOUTH_INDEX, true).is_err());
        assert!(!flag.load(Ordering::SeqCst));
    }

    #[test]
    fn dropped_control_loop_invalidates_confirmation() {
        let (mut io, flag) = fixture();
        io.set_torque(false).unwrap();
        drop(io);
        assert!(!flag.load(Ordering::SeqCst));
    }
}
