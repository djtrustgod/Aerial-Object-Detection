"""Rule-based classifier: FFT blink analysis + trajectory linearity + speed."""

from __future__ import annotations

import numpy as np

from src.config import ClassificationConfig
from src.recording.models import ObjectClass, TrackedObject


class Classifier:
    """Classifies tracked objects as aircraft, satellite, or UAP."""

    def __init__(self, config: ClassificationConfig, fps: float = 30.0):
        self._cfg = config
        self._fps = fps

    @property
    def fps(self) -> float:
        return self._fps

    @fps.setter
    def fps(self, value: float) -> None:
        self._fps = max(1.0, value)

    def classify(self, obj: TrackedObject) -> tuple[ObjectClass, float]:
        """Classify a tracked object based on its history.

        Returns (classification, confidence) where confidence is 0.0-1.0.
        """
        scores = {
            ObjectClass.AIRCRAFT: 0.0,
            ObjectClass.SATELLITE: 0.0,
            ObjectClass.UAP: 0.0,
        }

        # Need enough history for meaningful analysis
        if len(obj.positions) < 5:
            return ObjectClass.UNKNOWN, 0.0

        # --- Blink analysis via FFT ---
        blink_score = self._analyze_blink(obj.brightness_history)
        if blink_score > self._cfg.blink_power_threshold:
            scores[ObjectClass.AIRCRAFT] += 0.4
        else:
            scores[ObjectClass.SATELLITE] += 0.2

        # --- Trajectory linearity ---
        linearity = self._compute_linearity(obj.positions)
        if linearity > self._cfg.linearity_threshold:
            scores[ObjectClass.SATELLITE] += 0.4
        elif linearity > 0.5:
            scores[ObjectClass.AIRCRAFT] += 0.2
        else:
            scores[ObjectClass.UAP] += 0.3

        # --- Speed analysis ---
        speeds = self._compute_speeds(obj.positions)
        if len(speeds) > 0:
            mean_speed = float(np.mean(speeds))
            speed_var = float(np.var(speeds))

            # Satellite: constant speed in expected range
            if (self._cfg.satellite_speed_min <= mean_speed <= self._cfg.satellite_speed_max
                    and speed_var < 1.0):
                scores[ObjectClass.SATELLITE] += 0.3

            # Aircraft: variable speed is normal
            if speed_var < 5.0:
                scores[ObjectClass.AIRCRAFT] += 0.1

            # --- Acceleration variance (UAP indicator) ---
            if len(speeds) >= 3:
                accels = np.diff(speeds)
                accel_var = float(np.var(accels))
                if accel_var > self._cfg.acceleration_var_threshold:
                    scores[ObjectClass.UAP] += 0.4

        # Pick the highest scoring class
        best_class = max(scores, key=scores.get)
        total = sum(scores.values())
        confidence = scores[best_class] / total if total > 0 else 0.0

        return best_class, round(confidence, 3)

    def _analyze_blink(self, brightness_history: list[float]) -> float:
        """Analyze brightness history for periodic blinking using FFT.

        Returns the normalized power in the blink frequency band.
        """
        if len(brightness_history) < 16:
            return 0.0

        signal = np.array(brightness_history, dtype=np.float64)
        # Remove DC component
        signal = signal - np.mean(signal)

        # Zero-pad to next power of 2 for efficiency
        n = len(signal)
        fft_result = np.fft.rfft(signal)
        power = np.abs(fft_result) ** 2
        freqs = np.fft.rfftfreq(n, d=1.0 / self._fps)

        # Find power in blink frequency band
        band_mask = (freqs >= self._cfg.blink_freq_low) & (freqs <= self._cfg.blink_freq_high)
        if not np.any(band_mask):
            return 0.0

        band_power = np.sum(power[band_mask])
        total_power = np.sum(power[1:])  # exclude DC

        if total_power == 0:
            return 0.0

        return float(band_power / total_power)

    def _compute_linearity(self, positions: list[tuple[int, int]]) -> float:
        """Compute trajectory linearity using R² from linear regression.

        Returns R² value (0.0 = erratic, 1.0 = perfectly linear).
        """
        if len(positions) < 3:
            return 0.0

        pts = np.array(positions, dtype=np.float64)
        x, y = pts[:, 0], pts[:, 1]

        # Check if all points are the same (stationary)
        if np.std(x) < 1e-6 and np.std(y) < 1e-6:
            return 0.0

        # Use the axis with more variance as the independent variable
        if np.std(x) >= np.std(y):
            ind, dep = x, y
        else:
            ind, dep = y, x

        if np.std(ind) < 1e-6:
            return 1.0  # Perfectly vertical/horizontal line

        # Linear fit
        coeffs = np.polyfit(ind, dep, 1)
        predicted = np.polyval(coeffs, ind)

        ss_res = np.sum((dep - predicted) ** 2)
        ss_tot = np.sum((dep - np.mean(dep)) ** 2)

        if ss_tot == 0:
            return 1.0

        r_squared = 1.0 - (ss_res / ss_tot)
        return max(0.0, float(r_squared))

    def _compute_speeds(self, positions: list[tuple[int, int]]) -> np.ndarray:
        """Compute per-frame speeds from position history."""
        if len(positions) < 2:
            return np.array([])

        pts = np.array(positions, dtype=np.float64)
        diffs = np.diff(pts, axis=0)
        speeds = np.sqrt(np.sum(diffs ** 2, axis=1))
        return speeds
