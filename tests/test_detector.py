"""Tests for the frame difference + MOG2 detector."""

from __future__ import annotations

import numpy as np
import pytest

from src.processing.detector import Detector
from tests.conftest import add_dot, make_frame, make_moving_dot_sequence


class TestDetector:
    def test_no_detections_on_blank_frames(self, detection_config):
        """Blank frames should produce no detections after warmup."""
        detector = Detector(detection_config)
        frame = make_frame()
        # Feed several identical frames to warm up MOG2
        for i in range(10):
            detections = detector.detect(frame, frame_number=i)
        assert len(detections) == 0

    def test_detects_new_dot(self, detection_config):
        """A new dot appearing should be detected via frame differencing."""
        detector = Detector(detection_config)
        blank = make_frame()

        # Warm up with blank frames
        for i in range(5):
            detector.detect(blank, frame_number=i)

        # Add a dot
        frame_with_dot = add_dot(blank, 320, 180, radius=5, brightness=255)
        detections = detector.detect(frame_with_dot, frame_number=6)

        assert len(detections) >= 1
        # The detection should be near the dot location
        det = detections[0]
        assert abs(det.x - 320) < 15
        assert abs(det.y - 180) < 15

    def test_detects_moving_dot(self, detection_config):
        """A moving dot should be consistently detected."""
        detector = Detector(detection_config)
        frames = make_moving_dot_sequence(n_frames=15, dx=8)

        detected_count = 0
        for i, frame in enumerate(frames):
            detections = detector.detect(frame, frame_number=i)
            if detections:
                detected_count += 1

        # Should detect the dot in most frames (after warmup)
        assert detected_count >= 5

    def test_filters_large_contours(self, detection_config):
        """Contours larger than max_contour_area should be filtered out."""
        detection_config.max_contour_area = 50
        detector = Detector(detection_config)
        blank = make_frame()

        for i in range(5):
            detector.detect(blank, frame_number=i)

        # Add a large dot (radius=20 → area ≈ π*400 ≈ 1257)
        frame = add_dot(blank, 320, 180, radius=20, brightness=255)
        detections = detector.detect(frame, frame_number=6)

        # The large blob should be filtered out
        assert len(detections) == 0

    def test_detection_has_brightness(self, detection_config):
        """Detections should report the blob's brightness."""
        detector = Detector(detection_config)
        blank = make_frame()

        for i in range(5):
            detector.detect(blank, frame_number=i)

        frame = add_dot(blank, 320, 180, radius=5, brightness=200)
        detections = detector.detect(frame, frame_number=6)

        if detections:
            assert detections[0].brightness > 100
