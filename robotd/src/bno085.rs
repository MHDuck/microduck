// SPDX-License-Identifier: GPL-3.0-or-later
// Adapted from imu_reader.py, Copyright 2026 Marc Duclusaud.
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use duck_control::imu::ImuData;
use duck_control::io::{ImuIo, ImuStale, IoError};

const MAX_AGE: Duration = Duration::from_millis(100);
type Sample = Option<(Instant, ImuData, u64)>;

pub struct Bno085 {
    stop: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
    latest: Arc<Mutex<Sample>>,
    last_sequence: u64,
    stale: ImuStale,
}

impl Bno085 {
    pub fn open(cfg: &robotd_params::Imu) -> io::Result<Self> {
        // Verified body-to-sensor mounting rotation, wxyz.
        let mount = [std::f64::consts::FRAC_1_SQRT_2, 0.0, std::f64::consts::FRAC_1_SQRT_2, 0.0];
        if !matches!(cfg.address, 0x4a | 0x4b) {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "BNO085 address must be 0x4A or 0x4B"));
        }
        let file = OpenOptions::new().read(true).write(true).open(&cfg.bus)?;
        // I2C_SLAVE: leave a device already owned by a kernel driver alone.
        if unsafe { libc::ioctl(file.as_raw_fd(), 0x0703, libc::c_ulong::from(cfg.address)) } < 0 {
            return Err(io::Error::last_os_error());
        }
        let stop = Arc::new(AtomicBool::new(false));
        let latest = Arc::new(Mutex::new(None));
        let stopped = Arc::clone(&stop);
        let shared = Arc::clone(&latest);
        let worker = thread::Builder::new().name("bno085".into()).spawn(move || {
            let mut sensor = Sensor { file, sequence: [0; 6] };
            if let Err(error) = sensor.run(mount, &stopped, &shared) {
                tracing::error!(%error, "BNO085 reader stopped");
            }
            *shared.lock().unwrap() = None;
        })?;
        tracing::info!(bus = %cfg.bus.display(), address = cfg.address, "BNO085 reader started");
        Ok(Self { stop, worker: Some(worker), latest, last_sequence: 0, stale: ImuStale::default() })
    }
}

impl ImuIo for Bno085 {
    fn read(&mut self) -> duck_control::io::Result<ImuData> {
        let sample = *self.latest.lock().unwrap();
        if let Some((when, data, sequence)) = sample {
            if when.elapsed() < MAX_AGE {
                if sequence == self.last_sequence {
                    self.stale.total += 1;
                    self.stale.run += 1;
                } else {
                    self.stale.run = 0;
                }
                self.last_sequence = sequence;
                return Ok(data);
            }
        }
        self.stale.total += 1;
        self.stale.run += 1;
        Err(IoError::Bus("BNO085 has no fresh quaternion and gyro sample".into()))
    }

    fn ready(&self) -> bool {
        self.latest.lock().unwrap().as_ref().is_some_and(|(when, _, count)| {
            *count >= 25 && when.elapsed() < MAX_AGE
        })
    }

    fn stale(&self) -> ImuStale { self.stale }
}

impl Drop for Bno085 {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

struct Sensor {
    file: File,
    sequence: [u8; 6],
}

impl Sensor {
    fn send(&mut self, channel: usize, payload: &[u8]) -> io::Result<()> {
        let mut packet = Vec::with_capacity(payload.len() + 4);
        packet.extend_from_slice(&((payload.len() + 4) as u16).to_le_bytes());
        packet.extend_from_slice(&[channel as u8, self.sequence[channel]]);
        packet.extend_from_slice(payload);
        if self.file.write(&packet)? != packet.len() {
            return Err(io::Error::new(io::ErrorKind::WriteZero, "short SHTP write"));
        }
        self.sequence[channel] = self.sequence[channel].wrapping_add(1);
        Ok(())
    }

    fn packet(&mut self, buffer: &mut [u8; 8192]) -> io::Result<Option<(u8, usize)>> {
        let mut header = [0_u8; 4];
        if self.file.read(&mut header)? != 4 {
            return Ok(None);
        }
        let length = usize::from(u16::from_le_bytes([header[0], header[1]]) & 0x7fff);
        if length <= 4 || length > buffer.len() || header[2] > 5 {
            return Ok(None);
        }
        // The second I2C read repeats the SHTP header.
        let read = self.file.read(&mut buffer[..length])?;
        if read == length {
            buffer.copy_within(4..length, 0);
        } else if read != length - 4 {
            return Ok(None);
        }
        Ok(Some((header[2], length - 4)))
    }

    fn run(&mut self, mount: [f64; 4], stop: &AtomicBool, latest: &Mutex<Sample>) -> io::Result<()> {
        self.send(1, &[1])?;
        thread::sleep(Duration::from_millis(800));
        let mut buffer = [0_u8; 8192];
        let deadline = Instant::now() + Duration::from_millis(400);
        while Instant::now() < deadline && !stop.load(Ordering::Relaxed) {
            let _ = self.packet(&mut buffer);
            thread::sleep(Duration::from_millis(1));
        }
        for report in [0x08, 0x02] {
            let mut feature = [0_u8; 17];
            feature[0] = 0xfd;
            feature[1] = report;
            feature[5..9].copy_from_slice(&20000_u32.to_le_bytes());
            self.send(2, &feature)?;
        }
        let mut quat = None;
        let mut gyro = None;
        let mut quat_sequence = None;
        let mut gyro_sequence = None;
        let mut sequence = 0;
        let mut sent_quat = None;
        let mut sent_gyro = None;
        let mut last_error = Instant::now();
        while !stop.load(Ordering::Relaxed) {
            match self.packet(&mut buffer) {
                Ok(Some((3 | 4, length))) => {
                    let now = Instant::now();
                    let mut offset = 0;
                    while offset < length {
                        let id = buffer[offset];
                        let size = match id {
                            0xfb => 5,
                            0x08 => 12,
                            0x05 => 14,
                            0x01 | 0x02 => 10,
                            // Other SH-2 reports may share this packet. Resync one byte at
                            // a time, matching the Pi reader, so they do not hide a later
                            // gyro or rotation-vector report in the same payload.
                            _ => {
                                offset += 1;
                                continue;
                            }
                        };
                        if offset + size > length {
                            offset += 1;
                            continue;
                        }
                        let word = |i: usize| f64::from(i16::from_le_bytes([buffer[offset + i], buffer[offset + i + 1]]));
                        match id {
                            0x08 | 0x05 if quat_sequence != Some(buffer[offset + 1]) => {
                                if let Some(q) = normalize([word(10) / 16384.0, word(4) / 16384.0, word(6) / 16384.0, word(8) / 16384.0]) {
                                    quat = Some((now, q));
                                    quat_sequence = Some(buffer[offset + 1]);
                                }
                            }
                            0x02 if gyro_sequence != Some(buffer[offset + 1]) => {
                                gyro = Some((now, [word(4) / 512.0, word(6) / 512.0, word(8) / 512.0]));
                                gyro_sequence = Some(buffer[offset + 1]);
                            }
                            _ => {}
                        }
                        offset += size;
                    }
                    if let (Some((qt, q)), Some((gt, g))) = (quat, gyro) {
                        if now.duration_since(qt.min(gt)) < MAX_AGE && sent_quat != Some(qt) && sent_gyro != Some(gt) {
                            let body = multiply(q, mount);
                            let data = ImuData {
                                quat: body,
                                gyro: inverse_rotate(mount, g),
                                gravity: inverse_rotate(body, [0.0, 0.0, -1.0]),
                                sensor_gyro: g,
                                sensor_gravity: inverse_rotate(q, [0.0, 0.0, 1.0]),
                            };
                            sequence += 1;
                            *latest.lock().unwrap() = Some((qt.min(gt), data, sequence));
                            sent_quat = Some(qt);
                            sent_gyro = Some(gt);
                        }
                    }
                }
                Err(error) => {
                    if last_error.elapsed() >= Duration::from_secs(1) {
                        tracing::warn!(%error, "BNO085 I2C read failed");
                        last_error = Instant::now();
                    }
                    thread::sleep(Duration::from_millis(10));
                }
                _ => {}
            }
            thread::sleep(Duration::from_millis(1));
        }
        Ok(())
    }
}

fn normalize(q: [f64; 4]) -> Option<[f64; 4]> {
    let norm = q.iter().map(|v| v * v).sum::<f64>().sqrt();
    (norm.is_finite() && norm > 1e-9).then(|| q.map(|v| v / norm))
}

fn multiply([w, x, y, z]: [f64; 4], [a, b, c, d]: [f64; 4]) -> [f64; 4] {
    [w*a-x*b-y*c-z*d, w*b+x*a+y*d-z*c, w*c-x*d+y*a+z*b, w*d+x*c-y*b+z*a]
}

fn inverse_rotate([w, x, y, z]: [f64; 4], v: [f64; 3]) -> [f64; 3] {
    let q = multiply(multiply([w, -x, -y, -z], [0.0, v[0], v[1], v[2]]), [w, x, y, z]);
    [q[1], q[2], q[3]]
}
