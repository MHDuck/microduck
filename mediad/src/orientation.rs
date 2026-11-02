//! One orientation contract for video, raw-frame detection and all consumers.
//! The per-board offset composes with the existing mount angle. Pixels already
//! turned by the pipeline must not be turned again by a browser or detector.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CameraOrientation {
    pub mount_degrees: u32,
    pub pixel_degrees: u32,
    pub consumer_degrees: u32,
}

impl CameraOrientation {
    pub fn new(base: u32, offset: u32, rotate_pixels: bool) -> Result<Self, &'static str> {
        if ![0, 90, 180, 270].contains(&base) || ![0, 90, 180, 270].contains(&offset) {
            return Err("camera rotation and offset must each be 0, 90, 180 or 270 degrees");
        }
        let mount_degrees = (base + offset) % 360;
        Ok(Self {
            mount_degrees,
            pixel_degrees: if rotate_pixels { mount_degrees } else { 0 },
            consumer_degrees: if rotate_pixels { 0 } else { mount_degrees },
        })
    }

    pub fn encoded_size(self, width: u32, height: u32) -> (u32, u32) {
        if matches!(self.pixel_degrees, 90 | 270) {
            (height, width)
        } else {
            (width, height)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upside_down_offset_composes_with_the_existing_quarter_turn() {
        let before = CameraOrientation::new(90, 0, false).unwrap();
        let corrected = CameraOrientation::new(90, 180, false).unwrap();
        assert_eq!(before.consumer_degrees, 90);
        assert_eq!(corrected.consumer_degrees, 270);
        assert_eq!(corrected.pixel_degrees, 0);
        assert_eq!(corrected.encoded_size(1280, 720), (1280, 720));
    }

    #[test]
    fn physically_upright_pixels_have_zero_remaining_rotation() {
        let corrected = CameraOrientation::new(90, 180, true).unwrap();
        assert_eq!(corrected.pixel_degrees, 270);
        assert_eq!(corrected.consumer_degrees, 0);
        assert_eq!(corrected.encoded_size(1280, 720), (720, 1280));
    }

    #[test]
    fn all_cardinal_combinations_rotate_once_and_report_encoded_geometry() {
        for base in [0, 90, 180, 270] {
            for offset in [0, 90, 180, 270] {
                for pixels in [false, true] {
                    let orientation = CameraOrientation::new(base, offset, pixels).unwrap();
                    assert_eq!(
                        (orientation.pixel_degrees + orientation.consumer_degrees) % 360,
                        (base + offset) % 360
                    );
                    let swapped = pixels && matches!(orientation.mount_degrees, 90 | 270);
                    assert_eq!(
                        orientation.encoded_size(1280, 720),
                        if swapped { (720, 1280) } else { (1280, 720) }
                    );
                }
            }
        }
    }

    #[test]
    fn invalid_or_overflowing_angles_are_rejected_before_composition() {
        for invalid in [1, 45, 360, u32::MAX] {
            assert!(CameraOrientation::new(invalid, 0, false).is_err());
            assert!(CameraOrientation::new(90, invalid, true).is_err());
        }
    }
}
