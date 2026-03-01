"""Shared test fixtures: synthetic frames with white dots on black backgrounds."""

from __future__ import annotations

import numpy as np
import pytest

from src.config import (
    DetectionConfig,
    ProcessingConfig,
    TrackingConfig,
)


@pytest.fixture
def detection_config() -> DetectionConfig:
    return DetectionConfig(
        diff_threshold=20,
        min_contour_area=3,
        max_contour_area=500,
        min_circularity=0.1,
    )


@pytest.fixture
def tracking_config() -> TrackingConfig:
    return TrackingConfig(
        max_distance=50,
        max_disappeared=5,
        min_track_length=3,
    )


@pytest.fixture
def processing_config() -> ProcessingConfig:
    return ProcessingConfig(resize_width=640, resize_height=360)


def make_frame(width: int = 640, height: int = 360) -> np.ndarray:
    """Create a blank black grayscale frame."""
    return np.zeros((height, width), dtype=np.uint8)


def add_dot(frame: np.ndarray, x: int, y: int, radius: int = 4,
            brightness: int = 255) -> np.ndarray:
    """Add a white dot to a grayscale frame."""
    import cv2
    result = frame.copy()
    cv2.circle(result, (x, y), radius, brightness, -1)
    return result


def make_moving_dot_sequence(n_frames: int = 20, start_x: int = 100,
                             start_y: int = 180, dx: int = 5, dy: int = 0,
                             brightness: int = 255) -> list[np.ndarray]:
    """Create a sequence of frames with a dot moving in a straight line."""
    frames = []
    for i in range(n_frames):
        frame = make_frame()
        x = start_x + i * dx
        y = start_y + i * dy
        if 0 <= x < 640 and 0 <= y < 360:
            frame = add_dot(frame, x, y, brightness=brightness)
        frames.append(frame)
    return frames
