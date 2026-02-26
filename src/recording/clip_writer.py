"""Rolling pre-buffer + threaded MP4 clip writer."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class ClipWriter:
    """Maintains a rolling frame buffer and writes MP4 clips on demand."""

    def __init__(self, clip_dir: str, pre_buffer_seconds: float = 3.0,
                 post_buffer_seconds: float = 5.0, fps: float = 30.0):
        self._clip_dir = Path(clip_dir)
        self._clip_dir.mkdir(parents=True, exist_ok=True)
        self._pre_buffer_seconds = pre_buffer_seconds
        self._post_buffer_seconds = post_buffer_seconds
        self._fps = fps

        # Rolling pre-buffer
        max_pre_frames = int(pre_buffer_seconds * fps) + 1
        self._buffer: deque[np.ndarray] = deque(maxlen=max_pre_frames)

        # Recording state
        self._recording = False
        self._record_frames: list[np.ndarray] = []
        self._record_start_time: float = 0.0
        self._record_end_deadline: float = 0.0
        self._current_clip_path: str | None = None

        self._lock = threading.Lock()

    @property
    def fps(self) -> float:
        return self._fps

    @fps.setter
    def fps(self, value: float) -> None:
        self._fps = max(1.0, value)
        max_pre_frames = int(self._pre_buffer_seconds * self._fps) + 1
        with self._lock:
            self._buffer = deque(self._buffer, maxlen=max_pre_frames)

    def feed_frame(self, frame: np.ndarray) -> None:
        """Feed a frame to the rolling buffer. If recording, also capture it."""
        with self._lock:
            self._buffer.append(frame.copy())

            if self._recording:
                self._record_frames.append(frame.copy())
                if time.monotonic() >= self._record_end_deadline:
                    self._finish_recording()

    def trigger_recording(self) -> str | None:
        """Start or extend a recording. Returns the clip path if a new clip is started."""
        with self._lock:
            now = time.monotonic()
            deadline = now + self._post_buffer_seconds

            if self._recording:
                # Extend the recording deadline
                self._record_end_deadline = deadline
                return self._current_clip_path

            # Start new recording
            self._recording = True
            self._record_start_time = now
            self._record_end_deadline = deadline

            # Include pre-buffer frames
            self._record_frames = list(self._buffer)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"clip_{timestamp}.mp4"
            self._current_clip_path = str(self._clip_dir / filename)

            logger.info("Started recording clip: %s", self._current_clip_path)
            return self._current_clip_path

    def _finish_recording(self) -> None:
        """Finish recording and write clip to disk in a background thread."""
        frames = self._record_frames.copy()
        clip_path = self._current_clip_path

        self._recording = False
        self._record_frames = []
        self._current_clip_path = None

        if frames and clip_path:
            thread = threading.Thread(
                target=self._write_clip, args=(frames, clip_path),
                daemon=True,
            )
            thread.start()

    @staticmethod
    def _write_clip(frames: list[np.ndarray], path: str) -> None:
        """Write frames to an MP4 file (runs in a background thread)."""
        if not frames:
            return

        h, w = frames[0].shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        fps = 15.0  # Write at a reasonable playback FPS

        writer = cv2.VideoWriter(path, fourcc, fps, (w, h))
        try:
            for frame in frames:
                if len(frame.shape) == 2:
                    frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
                writer.write(frame)
            logger.info("Saved clip: %s (%d frames)", path, len(frames))
        except Exception:
            logger.exception("Error writing clip: %s", path)
        finally:
            writer.release()

    def flush(self) -> None:
        """Force-finish any in-progress recording."""
        with self._lock:
            if self._recording:
                self._finish_recording()
