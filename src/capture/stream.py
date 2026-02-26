"""Threaded RTSP/video frame grabber with automatic reconnection."""

from __future__ import annotations

import logging
import threading
import time

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class FrameGrabber:
    """Threaded frame grabber using the grab/retrieve pattern for RTSP streams.

    Also accepts local file paths for development/testing.
    """

    def __init__(self, url: str, reconnect_delay: float = 5.0,
                 grab_timeout: float = 10.0):
        self._url = url
        self._reconnect_delay = reconnect_delay
        self._grab_timeout = grab_timeout

        self._cap: cv2.VideoCapture | None = None
        self._frame: np.ndarray | None = None
        self._frame_number: int = 0
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._connected = False
        self._fps: float = 30.0

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def frame_number(self) -> int:
        return self._frame_number

    def start(self) -> None:
        """Start the background grab thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._grab_loop, daemon=True)
        self._thread.start()
        logger.info("Frame grabber started for: %s", self._url)

    def stop(self) -> None:
        """Stop the grabber and release the capture."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._release()
        logger.info("Frame grabber stopped")

    def get_frame(self) -> tuple[np.ndarray | None, int]:
        """Get the latest frame and its frame number.

        Returns (None, 0) if no frame is available.
        """
        with self._lock:
            if self._frame is not None:
                return self._frame.copy(), self._frame_number
            return None, 0

    def _connect(self) -> bool:
        """Open or reopen the video capture."""
        self._release()
        try:
            self._cap = cv2.VideoCapture(self._url, cv2.CAP_FFMPEG)
            if not self._cap.isOpened():
                logger.warning("Failed to open stream: %s", self._url)
                return False

            # Read stream FPS for timing
            fps = self._cap.get(cv2.CAP_PROP_FPS)
            if fps and fps > 0:
                self._fps = fps

            self._connected = True
            logger.info("Connected to stream: %s (%.1f FPS)", self._url, self._fps)
            return True
        except Exception:
            logger.exception("Error connecting to stream")
            return False

    def _release(self) -> None:
        """Release the video capture."""
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        self._connected = False

    def _grab_loop(self) -> None:
        """Main grab loop running in a background thread."""
        last_grab_time = 0.0

        while self._running:
            # Ensure we have a connection
            if not self._connected:
                if not self._connect():
                    time.sleep(self._reconnect_delay)
                    continue

            try:
                # grab() is non-blocking and just grabs the next frame
                grabbed = self._cap.grab()
                if not grabbed:
                    logger.warning("Grab failed, attempting reconnection...")
                    self._connected = False
                    time.sleep(self._reconnect_delay)
                    continue

                last_grab_time = time.monotonic()

                # retrieve() decodes the grabbed frame
                ret, frame = self._cap.retrieve()
                if ret and frame is not None:
                    with self._lock:
                        self._frame = frame
                        self._frame_number += 1

            except Exception:
                logger.exception("Error in grab loop")
                self._connected = False
                time.sleep(self._reconnect_delay)
                continue

            # Check for grab timeout
            if (time.monotonic() - last_grab_time) > self._grab_timeout:
                logger.warning("Grab timeout exceeded, reconnecting...")
                self._connected = False
