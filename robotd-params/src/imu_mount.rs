//! Axis-aligned sensor mounting relative to the body (+X forward, +Y left, +Z up).
use serde::{Deserialize, Serialize};
use std::f64::consts::FRAC_1_SQRT_2 as H;

/// Physical directions of the sensor's positive X, Y and Z axes.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ImuMounting {
    /// Preserve the selected driver's historical mounting.
    #[default]
    DriverDefault,
    XForwardYLeftZUp,
    XForwardYRightZDown,
    XForwardYUpZRight,
    XForwardYDownZLeft,
    XBackYLeftZDown,
    XBackYRightZUp,
    XBackYUpZLeft,
    XBackYDownZRight,
    XLeftYForwardZDown,
    XLeftYBackZUp,
    XLeftYUpZForward,
    XLeftYDownZBack,
    XRightYForwardZUp,
    XRightYBackZDown,
    XRightYUpZBack,
    XRightYDownZForward,
    XUpYForwardZLeft,
    XUpYBackZRight,
    XUpYLeftZBack,
    XUpYRightZForward,
    XDownYForwardZRight,
    XDownYBackZLeft,
    XDownYLeftZForward,
    XDownYRightZBack,
}

pub const IMU_MOUNTING_LABELS: &[&str] = &[
    "driver_default",
    "x_forward_y_left_z_up",
    "x_forward_y_right_z_down",
    "x_forward_y_up_z_right",
    "x_forward_y_down_z_left",
    "x_back_y_left_z_down",
    "x_back_y_right_z_up",
    "x_back_y_up_z_left",
    "x_back_y_down_z_right",
    "x_left_y_forward_z_down",
    "x_left_y_back_z_up",
    "x_left_y_up_z_forward",
    "x_left_y_down_z_back",
    "x_right_y_forward_z_up",
    "x_right_y_back_z_down",
    "x_right_y_up_z_back",
    "x_right_y_down_z_forward",
    "x_up_y_forward_z_left",
    "x_up_y_back_z_right",
    "x_up_y_left_z_back",
    "x_up_y_right_z_forward",
    "x_down_y_forward_z_right",
    "x_down_y_back_z_left",
    "x_down_y_left_z_forward",
    "x_down_y_right_z_back",
];

impl ImuMounting {
    /// Sensor→body quaternion, scalar-first. BNO085 consumes its conjugate.
    pub fn sensor_to_body(self, driver: crate::ImuDriver) -> [f64; 4] {
        match self {
            Self::DriverDefault => match driver {
                crate::ImuDriver::Bno085 => [H, 0.0, -H, 0.0],
                crate::ImuDriver::Lsm6dsv16x => [H, 0.0, H, 0.0],
            },
            Self::XForwardYLeftZUp => [1.0, 0.0, 0.0, 0.0],
            Self::XForwardYRightZDown => [0.0, 1.0, 0.0, 0.0],
            Self::XForwardYUpZRight => [H, H, 0.0, 0.0],
            Self::XForwardYDownZLeft => [H, -H, 0.0, 0.0],
            Self::XBackYLeftZDown => [0.0, 0.0, 1.0, 0.0],
            Self::XBackYRightZUp => [0.0, 0.0, 0.0, 1.0],
            Self::XBackYUpZLeft => [0.0, 0.0, H, H],
            Self::XBackYDownZRight => [0.0, 0.0, H, -H],
            Self::XLeftYForwardZDown => [0.0, H, H, 0.0],
            Self::XLeftYBackZUp => [H, 0.0, 0.0, H],
            Self::XLeftYUpZForward => [0.5, 0.5, 0.5, 0.5],
            Self::XLeftYDownZBack => [0.5, -0.5, -0.5, 0.5],
            Self::XRightYForwardZUp => [H, 0.0, 0.0, -H],
            Self::XRightYBackZDown => [0.0, H, -H, 0.0],
            Self::XRightYUpZBack => [0.5, 0.5, -0.5, -0.5],
            Self::XRightYDownZForward => [0.5, -0.5, 0.5, -0.5],
            Self::XUpYForwardZLeft => [0.5, -0.5, -0.5, -0.5],
            Self::XUpYBackZRight => [0.5, 0.5, -0.5, 0.5],
            Self::XUpYLeftZBack => [H, 0.0, -H, 0.0],
            Self::XUpYRightZForward => [0.0, H, 0.0, H],
            Self::XDownYForwardZRight => [0.5, 0.5, 0.5, -0.5],
            Self::XDownYBackZLeft => [0.5, -0.5, 0.5, 0.5],
            Self::XDownYLeftZForward => [H, 0.0, H, 0.0],
            Self::XDownYRightZBack => [0.0, H, 0.0, -H],
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{ImuDriver, Params};

    fn rotate([w, x, y, z]: [f64; 4], v: [f64; 3]) -> [f64; 3] {
        let cross = |a: [f64; 3], b: [f64; 3]| {
            [
                a[1] * b[2] - a[2] * b[1],
                a[2] * b[0] - a[0] * b[2],
                a[0] * b[1] - a[1] * b[0],
            ]
        };
        let t = cross([x, y, z], v).map(|c| 2.0 * c);
        let c = cross([x, y, z], t);
        std::array::from_fn(|i| v[i] + w * t[i] + c[i])
    }

    #[test]
    fn every_mounting_maps_all_three_sensor_axes_to_its_named_directions() {
        for label in &IMU_MOUNTING_LABELS[1..] {
            let params: Params = toml::from_str(&format!("[imu]\nmounting = \"{label}\"")).unwrap();
            let mount = params.imu.mounting.sensor_to_body(ImuDriver::Bno085);
            assert!((mount.iter().map(|v| v * v).sum::<f64>() - 1.0).abs() < 1e-12);
            assert_eq!(
                mount,
                params.imu.mounting.sensor_to_body(ImuDriver::Lsm6dsv16x)
            );
            let directions: Vec<_> = label.split('_').skip(1).step_by(2).collect();
            for (axis, direction) in directions.into_iter().enumerate() {
                let expected = match direction {
                    "forward" => [1.0, 0.0, 0.0],
                    "back" => [-1.0, 0.0, 0.0],
                    "left" => [0.0, 1.0, 0.0],
                    "right" => [0.0, -1.0, 0.0],
                    "up" => [0.0, 0.0, 1.0],
                    "down" => [0.0, 0.0, -1.0],
                    _ => panic!("unknown direction: {direction}"),
                };
                let mut sensor_axis = [0.0; 3];
                sensor_axis[axis] = 1.0;
                let actual = rotate(mount, sensor_axis);
                for i in 0..3 {
                    assert!(
                        (actual[i] - expected[i]).abs() < 1e-12,
                        "{label}, axis {axis}: {actual:?}"
                    );
                }
            }
        }
        assert!(toml::from_str::<Params>("[imu]\nmounting = \"x_up_y_left_z_forward\"").is_err());
    }

    #[test]
    fn omitted_mounting_preserves_each_drivers_previous_rotation() {
        for (driver, expected) in [
            ("bno085", [H, 0.0, -H, 0.0]),
            ("lsm6dsv16x", [H, 0.0, H, 0.0]),
        ] {
            let params: Params = toml::from_str(&format!("[imu]\ndriver = \"{driver}\"")).unwrap();
            assert_eq!(
                params.imu.mounting.sensor_to_body(params.imu.driver),
                expected
            );
        }
        let shipped: Params = toml::from_str(include_str!("../../deploy/robotd.toml")).unwrap();
        assert_eq!(
            shipped.imu.mounting.sensor_to_body(shipped.imu.driver),
            [H, 0.0, -H, 0.0]
        );
    }
}
