"""Frame differencing + MOG2 background subtraction + blob detection."""

from __future__ import annotations

import math

import cv2
import numpy as np

from src.config import DetectionConfig
from src.recording.models import Detection


class Detector:
    """Detects moving bright blobs using frame differencing and MOG2."""

    def __init__(self, config: DetectionConfig):
        self._cfg = config
        self._prev_frame: np.ndarray | None = None

        # MOG2 background subtractor
        self._mog2 = cv2.createBackgroundSubtractorMOG2(
            history=config.mog2_history,
            varThreshold=config.mog2_var_threshold,
            detectShadows=config.mog2_detect_shadows,
        )

        # Pre-computed morphological kernel
        self._morph_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (config.morph_kernel_size, config.morph_kernel_size),
        )

    def detect(self, gray_frame: np.ndarray, frame_number: int = 0,
               timestamp: float = 0.0) -> list[Detection]:
        """Detect bright moving objects in a preprocessed grayscale frame.

        Returns a list of Detection objects.
        """
        # Frame differencing mask
        diff_mask = self._frame_diff(gray_frame)

        # MOG2 foreground mask
        mog2_mask = self._mog2.apply(gray_frame)

        # Combine masks (OR)
        if diff_mask is not None:
            combined = cv2.bitwise_or(diff_mask, mog2_mask)
        else:
            combined = mog2_mask

        # Morphological cleanup: erode to remove noise, dilate to fill gaps
        cleaned = cv2.erode(combined, self._morph_kernel,
                            iterations=self._cfg.morph_erode_iterations)
        cleaned = cv2.dilate(cleaned, self._morph_kernel,
                             iterations=self._cfg.morph_dilate_iterations)

        # Store current frame for next diff
        self._prev_frame = gray_frame.copy()

        # Find contours and filter
        contours, _ = cv2.findContours(
            cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        detections = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < self._cfg.min_contour_area:
                continue
            if area > self._cfg.max_contour_area:
                continue

            # Circularity filter
            perimeter = cv2.arcLength(contour, True)
            if perimeter > 0:
                circularity = (4 * math.pi * area) / (perimeter * perimeter)
                if circularity < self._cfg.min_circularity:
                    continue

            # Bounding box and centroid
            x, y, w, h = cv2.boundingRect(contour)
            cx = x + w // 2
            cy = y + h // 2

            # Mean brightness in the blob region
            mask = np.zeros(gray_frame.shape, dtype=np.uint8)
            cv2.drawContours(mask, [contour], -1, 255, -1)
            brightness = float(cv2.mean(gray_frame, mask=mask)[0])

            detections.append(Detection(
                x=cx, y=cy, w=w, h=h,
                area=area, brightness=brightness,
                frame_number=frame_number,
                timestamp=timestamp,
            ))

        return detections

    def _frame_diff(self, gray_frame: np.ndarray) -> np.ndarray | None:
        """Compute absolute frame difference and threshold it."""
        if self._prev_frame is None:
            return None

        diff = cv2.absdiff(self._prev_frame, gray_frame)
        _, thresh = cv2.threshold(
            diff, self._cfg.diff_threshold, 255, cv2.THRESH_BINARY
        )
        return thresh
