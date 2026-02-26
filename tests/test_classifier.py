"""Tests for the rule-based classifier."""

from __future__ import annotations

import math

import numpy as np
import pytest

from src.processing.classifier import Classifier
from src.recording.models import ObjectClass, TrackedObject


def make_tracked(positions: list[tuple[int, int]],
                 brightness: list[float] | None = None,
                 object_id: int = 0) -> TrackedObject:
    """Helper to create a TrackedObject with history."""
    if brightness is None:
        brightness = [200.0] * len(positions)
    return TrackedObject(
        object_id=object_id,
        centroid=positions[-1] if positions else (0, 0),
        positions=positions,
        brightness_history=brightness,
        frame_history=list(range(len(positions))),
    )


class TestClassifier:
    def test_too_short_track_is_unknown(self, classification_config):
        """Tracks with fewer than 5 points should be classified as unknown."""
        classifier = Classifier(classification_config, fps=30.0)
        obj = make_tracked([(100, 100), (110, 100), (120, 100)])
        cls, conf = classifier.classify(obj)
        assert cls == ObjectClass.UNKNOWN

    def test_satellite_linear_constant_speed(self, classification_config):
        """A linear, constant-speed track should classify as satellite."""
        classifier = Classifier(classification_config, fps=30.0)

        # Perfectly linear trajectory at constant speed (3 px/frame)
        positions = [(100 + i * 3, 200) for i in range(30)]
        brightness = [180.0] * 30  # Steady brightness
        obj = make_tracked(positions, brightness)

        cls, conf = classifier.classify(obj)
        assert cls == ObjectClass.SATELLITE
        assert conf > 0.4

    def test_aircraft_blinking(self, classification_config):
        """A track with periodic blinking should classify as aircraft."""
        classifier = Classifier(classification_config, fps=30.0)

        # Generate blinking brightness at ~1.5 Hz (in the 0.5-3 Hz band)
        n = 60
        t = np.arange(n) / 30.0
        brightness = (100 + 100 * np.sin(2 * math.pi * 1.5 * t)).tolist()

        # Slightly curved trajectory
        positions = [(100 + i * 2, 200 + int(5 * math.sin(i * 0.1)))
                     for i in range(n)]
        obj = make_tracked(positions, brightness)

        cls, conf = classifier.classify(obj)
        assert cls == ObjectClass.AIRCRAFT

    def test_uap_erratic_movement(self, classification_config):
        """Erratic movement with sudden speed changes should score as UAP."""
        classifier = Classifier(classification_config, fps=30.0)

        # Erratic trajectory with sudden direction/speed changes
        rng = np.random.RandomState(42)
        positions = [(100, 200)]
        for i in range(29):
            dx = int(rng.choice([-15, -5, 0, 5, 15]))
            dy = int(rng.choice([-15, -5, 0, 5, 15]))
            last = positions[-1]
            positions.append((last[0] + dx, last[1] + dy))

        brightness = [180.0 + rng.uniform(-50, 50) for _ in range(30)]
        obj = make_tracked(positions, brightness)

        cls, conf = classifier.classify(obj)
        assert cls == ObjectClass.UAP

    def test_linearity_computation(self, classification_config):
        """Verify linearity returns ~1.0 for a perfect line."""
        classifier = Classifier(classification_config)
        positions = [(i * 10, i * 5) for i in range(20)]
        linearity = classifier._compute_linearity(positions)
        assert linearity > 0.99

    def test_linearity_low_for_circle(self, classification_config):
        """Verify linearity returns low value for circular path."""
        classifier = Classifier(classification_config)
        n = 30
        positions = [
            (200 + int(50 * math.cos(2 * math.pi * i / n)),
             200 + int(50 * math.sin(2 * math.pi * i / n)))
            for i in range(n)
        ]
        linearity = classifier._compute_linearity(positions)
        assert linearity < 0.5

    def test_blink_analysis_detects_periodic(self, classification_config):
        """FFT should detect periodic blinking in the expected band."""
        classifier = Classifier(classification_config, fps=30.0)

        # 2 Hz blink signal
        n = 64
        t = np.arange(n) / 30.0
        brightness = (100 + 80 * np.sin(2 * math.pi * 2.0 * t)).tolist()

        power = classifier._analyze_blink(brightness)
        assert power > 0.3  # Significant power in the blink band

    def test_blink_analysis_low_for_steady(self, classification_config):
        """Steady brightness should have low blink power."""
        classifier = Classifier(classification_config, fps=30.0)
        brightness = [200.0] * 64
        power = classifier._analyze_blink(brightness)
        assert power < 0.1
