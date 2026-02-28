"""Pipeline orchestrator: capture → detect → track → classify → record."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable

import cv2
import numpy as np

from src.config import AppConfig, save_config_values
from src.capture.stream import FrameGrabber
from src.processing.preprocessor import Preprocessor
from src.processing.detector import Detector
from src.processing.tracker import CentroidTracker
from src.processing.classifier import Classifier
from src.recording.clip_writer import ClipWriter
from src.recording.event_logger import EventLogger
from src.recording.models import DetectionEvent, ObjectClass, TrackedObject

logger = logging.getLogger(__name__)


class Pipeline:
    """Main processing pipeline orchestrator."""

    def __init__(self, config: AppConfig, config_path: str | None = None):
        self._config = config
        self._config_path = config_path
        self._running = False
        self._thread: threading.Thread | None = None

        # Components
        self._grabber = FrameGrabber(
            url=config.capture.rtsp_url,
            reconnect_delay=config.capture.reconnect_delay,
            grab_timeout=config.capture.grab_timeout,
        )
        self._preprocessor = Preprocessor(config.processing)
        self._detector = Detector(config.detection)
        self._tracker = CentroidTracker(config.tracking)
        self._classifier = Classifier(config.classification)
        self._clip_writer = ClipWriter(
            clip_dir=config.recording.clip_dir,
            pre_buffer_seconds=config.recording.clip_pre_buffer,
            post_buffer_seconds=config.recording.clip_post_buffer,
        )
        self._event_logger = EventLogger(config.recording.db_path)

        # Display frame (with overlays)
        self._display_frame: np.ndarray | None = None
        self._display_lock = threading.Lock()

        # Event subscribers (for WebSocket push)
        self._event_callbacks: list[Callable] = []
        self._loop: asyncio.AbstractEventLoop | None = None

        # Stats
        self._frame_count = 0
        self._fps_actual = 0.0
        self._active_tracks = 0

        # Track completion tracking
        self._prev_track_ids: set[int] = set()

        # Incremented on every URL change so in-flight frames are discarded
        self._url_version: int = 0

    @property
    def display_frame(self) -> np.ndarray | None:
        with self._display_lock:
            if self._display_frame is not None:
                return self._display_frame.copy()
            return None

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "fps": round(self._fps_actual, 1),
            "frame_count": self._frame_count,
            "active_tracks": self._active_tracks,
            "connected": self._grabber.is_connected,
            "detection_active": self._is_in_schedule(),
            "schedule_enabled": self._config.schedule.enabled,
        }

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def event_logger(self) -> EventLogger:
        return self._event_logger

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the asyncio event loop for thread-safe callbacks."""
        self._loop = loop

    def add_event_callback(self, callback: Callable) -> None:
        """Register a callback for detection events."""
        self._event_callbacks.append(callback)

    def start(self) -> None:
        """Start the pipeline (grabber + processing thread)."""
        if self._running:
            return
        self._running = True
        self._grabber.start()
        self._thread = threading.Thread(target=self._process_loop, daemon=True)
        self._thread.start()
        logger.info("Pipeline started")

    def stop(self) -> None:
        """Stop the pipeline gracefully."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None
        self._grabber.stop()
        self._clip_writer.flush()
        self._event_logger.close()
        logger.info("Pipeline stopped")

    def update_stream_url(self, url: str) -> None:
        """Update the RTSP URL and reconnect the stream grabber."""
        self._config.capture.rtsp_url = url
        self._url_version += 1
        self._grabber.set_url(url)
        with self._display_lock:
            self._display_frame = None

    def persist_rtsp_url(self, url: str) -> None:
        """Write the RTSP URL to the config file on disk."""
        save_config_values({"rtsp_url": url}, self._config_path)

    def persist_config_values(self, data: dict) -> None:
        """Write arbitrary key/value pairs to the config file on disk."""
        save_config_values(data, self._config_path)

    def update_capture_config(self, **kwargs: Any) -> None:
        """Update capture settings (reconnect_delay, grab_timeout)."""
        for key, value in kwargs.items():
            if hasattr(self._config.capture, key):
                setattr(self._config.capture, key, value)

    def update_processing_config(self, **kwargs: Any) -> None:
        """Update processing parameters and rebuild the preprocessor."""
        for key, value in kwargs.items():
            if hasattr(self._config.processing, key):
                setattr(self._config.processing, key, value)
        self._preprocessor = Preprocessor(self._config.processing)

    def update_classification_config(self, **kwargs: Any) -> None:
        """Update classification parameters (Classifier references the config object)."""
        for key, value in kwargs.items():
            if hasattr(self._config.classification, key):
                setattr(self._config.classification, key, value)

    def update_recording_config(self, **kwargs: Any) -> None:
        """Update recording parameters."""
        for key, value in kwargs.items():
            if hasattr(self._config.recording, key):
                setattr(self._config.recording, key, value)

    def update_web_config(self, **kwargs: Any) -> None:
        """Update web/stream parameters."""
        for key, value in kwargs.items():
            if hasattr(self._config.web, key):
                setattr(self._config.web, key, value)

    def update_detection_config(self, **kwargs: Any) -> None:
        """Update detection parameters at runtime."""
        for key, value in kwargs.items():
            if hasattr(self._config.detection, key):
                setattr(self._config.detection, key, value)
        # Rebuild detector with new config
        self._detector = Detector(self._config.detection)
        logger.info("Detection config updated: %s", kwargs)

    def update_tracking_config(self, **kwargs: Any) -> None:
        """Update tracking parameters at runtime."""
        for key, value in kwargs.items():
            if hasattr(self._config.tracking, key):
                setattr(self._config.tracking, key, value)

    def update_schedule_config(self, **kwargs: Any) -> None:
        """Update schedule parameters at runtime."""
        for key, value in kwargs.items():
            if hasattr(self._config.schedule, key):
                setattr(self._config.schedule, key, value)

    def _is_in_schedule(self) -> bool:
        """Return True if detection should run now (always True when scheduling is off)."""
        if not self._config.schedule.enabled:
            return True
        now = datetime.now().time()
        start = datetime.strptime(self._config.schedule.start_time, "%H:%M").time()
        end = datetime.strptime(self._config.schedule.end_time, "%H:%M").time()
        if start <= end:            # same-day window e.g. 09:00–17:00
            return start <= now <= end
        else:                       # overnight window e.g. 20:00–06:00
            return now >= start or now <= end

    def _process_loop(self) -> None:
        """Main processing loop running in a background thread."""
        frame_skip = self._config.processing.frame_skip
        skip_counter = 0
        fps_timer = time.monotonic()
        fps_frame_count = 0

        # Set FPS on components once connected
        fps_set = False

        while self._running:
            url_ver = self._url_version
            frame, frame_num = self._grabber.get_frame()
            if frame is None:
                self._fps_actual = 0.0
                fps_frame_count = 0
                fps_timer = time.monotonic()
                time.sleep(0.01)
                continue

            # Set FPS from stream on first frame
            if not fps_set and self._grabber.is_connected:
                stream_fps = self._grabber.fps
                self._classifier.fps = stream_fps
                self._clip_writer.fps = stream_fps
                fps_set = True

            # Frame skipping
            skip_counter += 1
            if skip_counter % frame_skip != 0:
                # Still feed the clip writer for continuous recording
                display = self._preprocessor.resize_only(frame)
                self._stamp_timestamp(display)
                self._clip_writer.feed_frame(display)
                continue

            self._frame_count += 1
            timestamp = time.time()

            # Preprocess
            gray = self._preprocessor.process(frame)
            display = self._preprocessor.resize_only(frame)

            # Detect (gated by schedule)
            in_schedule = self._is_in_schedule()

            if in_schedule:
                detections = self._detector.detect(gray, frame_num, timestamp)
            else:
                detections = []

            # Track (empty list ages out stale tracks naturally)
            tracks = self._tracker.update(detections)
            self._active_tracks = len(tracks)

            if in_schedule:
                # Classify mature tracks
                mature = self._tracker.mature_objects
                for oid, obj in mature.items():
                    cls, conf = self._classifier.classify(obj)
                    obj.classification = cls
                    obj.confidence = conf

                # Check for completed tracks (were active, now gone)
                current_ids = set(tracks.keys())
                self._prev_track_ids = current_ids

                # Record & log for mature tracks
                has_detections = len(mature) > 0
                if has_detections:
                    clip_path = self._clip_writer.trigger_recording()
                    for oid, obj in mature.items():
                        if obj.classification != ObjectClass.UNKNOWN:
                            self._publish_event(obj, clip_path)

            # Draw overlays on display frame (includes timestamp)
            annotated = self._draw_overlays(display, tracks)

            # Feed clip writer with annotated frame
            self._clip_writer.feed_frame(annotated)
            with self._display_lock:
                # Discard if URL changed while this frame was being processed
                if self._url_version == url_ver:
                    self._display_frame = annotated

            # FPS calculation
            fps_frame_count += 1
            elapsed = time.monotonic() - fps_timer
            if elapsed >= 1.0:
                self._fps_actual = fps_frame_count / elapsed
                fps_frame_count = 0
                fps_timer = time.monotonic()

    def _draw_overlays(self, frame: np.ndarray,
                       tracks: dict[int, TrackedObject]) -> np.ndarray:
        """Draw bounding boxes, IDs, and classification labels."""
        annotated = frame.copy()

        color_map = {
            ObjectClass.AIRCRAFT: (0, 255, 0),    # Green
            ObjectClass.SATELLITE: (255, 255, 0),  # Cyan
            ObjectClass.UAP: (0, 0, 255),          # Red
            ObjectClass.UNKNOWN: (128, 128, 128),  # Gray
        }

        for oid, obj in tracks.items():
            cx, cy = obj.centroid
            color = color_map.get(obj.classification, (128, 128, 128))

            # Draw crosshair
            cv2.drawMarker(annotated, (cx, cy), color,
                           cv2.MARKER_CROSS, 15, 1)

            # Draw trajectory
            if len(obj.positions) >= 2:
                pts = np.array(obj.positions, dtype=np.int32)
                cv2.polylines(annotated, [pts], False, color, 1)

            # Label
            label = f"#{oid} {obj.classification.value}"
            if obj.confidence > 0:
                label += f" {obj.confidence:.0%}"
            cv2.putText(annotated, label, (cx + 10, cy - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)

        # HUD overlay
        cv2.putText(annotated, f"FPS: {self._fps_actual:.1f}",
                    (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        cv2.putText(annotated, f"Tracks: {self._active_tracks}",
                    (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        self._stamp_timestamp(annotated)
        return annotated

    def _stamp_timestamp(self, frame: np.ndarray) -> None:
        """Draw current date/time onto frame in-place (bottom-left)."""
        ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        cv2.putText(frame, ts, (10, frame.shape[0] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)

    def _publish_event(self, obj: TrackedObject,
                       clip_path: str | None) -> None:
        """Publish a detection event to callbacks and log to DB."""
        positions = obj.positions
        if not positions:
            return

        avg_x = float(np.mean([p[0] for p in positions]))
        avg_y = float(np.mean([p[1] for p in positions]))
        speeds = []
        for i in range(1, len(positions)):
            dx = positions[i][0] - positions[i - 1][0]
            dy = positions[i][1] - positions[i - 1][1]
            speeds.append((dx ** 2 + dy ** 2) ** 0.5)
        avg_speed = float(np.mean(speeds)) if speeds else 0.0

        event = DetectionEvent(
            object_id=obj.object_id,
            classification=obj.classification,
            confidence=obj.confidence,
            start_time=time.time(),
            end_time=time.time(),
            start_frame=obj.frame_history[0] if obj.frame_history else 0,
            end_frame=obj.frame_history[-1] if obj.frame_history else 0,
            avg_x=avg_x,
            avg_y=avg_y,
            avg_speed=avg_speed,
            trajectory_length=len(positions),
            clip_path=clip_path,
        )

        # Log to DB (deduplicate by object_id — only log once per track)
        if not hasattr(self, '_logged_ids'):
            self._logged_ids: set[int] = set()
        if obj.object_id not in self._logged_ids:
            self._logged_ids.add(obj.object_id)
            event.event_id = self._event_logger.log_event(event)

        # Publish to WebSocket subscribers
        event_data = {
            "type": "detection",
            "event_id": event.event_id,
            "object_id": event.object_id,
            "classification": event.classification.value,
            "confidence": event.confidence,
            "x": avg_x,
            "y": avg_y,
            "speed": avg_speed,
            "trajectory_length": len(positions),
        }

        for callback in self._event_callbacks:
            try:
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(callback, event_data)
                else:
                    callback(event_data)
            except Exception:
                logger.exception("Error in event callback")
