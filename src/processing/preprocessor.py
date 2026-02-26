"""Frame preprocessing: resize, grayscale, CLAHE, blur."""

from __future__ import annotations

import cv2
import numpy as np

from src.config import ProcessingConfig


class Preprocessor:
    """Preprocesses frames for detection: resize → grayscale → CLAHE → blur."""

    def __init__(self, config: ProcessingConfig):
        self._width = config.resize_width
        self._height = config.resize_height
        self._blur_k = config.blur_kernel
        self._clahe = cv2.createCLAHE(
            clipLimit=config.clahe_clip_limit,
            tileGridSize=(config.clahe_grid_size, config.clahe_grid_size),
        )

    def process(self, frame: np.ndarray) -> np.ndarray:
        """Apply full preprocessing pipeline. Returns a grayscale, enhanced frame."""
        resized = cv2.resize(frame, (self._width, self._height),
                             interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        enhanced = self._clahe.apply(gray)
        blurred = cv2.GaussianBlur(enhanced, (self._blur_k, self._blur_k), 0)
        return blurred

    def resize_only(self, frame: np.ndarray) -> np.ndarray:
        """Resize frame without grayscale conversion (for display/recording)."""
        return cv2.resize(frame, (self._width, self._height),
                          interpolation=cv2.INTER_AREA)
