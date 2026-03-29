"""Pipeline orchestrator: capture → detect → track → record."""

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
import psutil

from src.config import AppConfig, save_config_values
from src.zones import load_zones, save_zones, point_in_any_zone
from src.capture.stream import FrameGrabber
from src.processing.preprocessor import Preprocessor
from src.processing.detector import Detector
from src.processing.tracker import CentroidTracker
from src.recording.clip_writer import ClipWriter
from src.recording.event_logger import EventLogger
from src.recording.models import DetectionEvent, TrackedObject

from pathlib import Path

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
        self._clip_writer = ClipWriter(
            clip_dir=config.recording.clip_dir,
            pre_buffer_seconds=config.recording.clip_pre_buffer,
            post_buffer_seconds=config.recording.clip_post_buffer,
            full_resolution=config.recording.clip_full_resolution,
        )
        self._event_logger = EventLogger(config.recording.db_path)

        # Thumbnail directory
        self._thumb_dir = Path(config.recording.thumb_dir)
        self._thumb_dir.mkdir(parents=True, exist_ok=True)

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

        # Manual detection toggle (session-only, resets to True on restart)
        self._detection_enabled: bool = True
        # When True, _detection_enabled bypasses the schedule check entirely
        self._schedule_override: bool = False

        # Exclusion zones
        self._exclusion_zones: list[dict] = load_zones()

    @property
    def display_frame(self) -> np.ndarray | None:
        with self._display_lock:
            if self._display_frame is not None:
                return self._display_frame.copy()
            return None

    @property
    def stats(self) -> dict[str, Any]:
        mem = psutil.virtual_memory()
        return {
            "fps": round(self._fps_actual, 1),
            "frame_count": self._frame_count,
            "active_tracks": self._active_tracks,
            "connected": self._grabber.is_connected,
            "detection_active": self._detection_enabled and (self._schedule_override or self._is_in_schedule()),
            "schedule_enabled": self._config.schedule.enabled,
            "detection_enabled": self._detection_enabled,
            "cpu_percent": psutil.cpu_percent(interval=None),
            "mem_percent": mem.percent,
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
        """Write the RTSP URL to local.yaml (keeps credentials out of default.yaml)."""
        from pathlib import Path
        local_path = Path(self._config_path or "config/default.yaml").parent / "local.yaml"
        # Ensure local.yaml exists with the capture section
        if not local_path.exists():
            local_path.write_text(f'capture:\n  rtsp_url: "{url}"\n')
        else:
            save_config_values({"rtsp_url": url}, local_path)

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

    def set_detection_enabled(self, enabled: bool) -> None:
        """Manually enable or disable detection, overriding the schedule."""
        self._detection_enabled = enabled
        # Force-on sets the override flag so detection runs even outside scheduled hours.
        # Force-off clears it (doesn't matter since enabled=False already gates the loop).
        self._schedule_override = enabled
        logger.info("Detection manually %s", "enabled" if enabled else "disabled")

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
        was_active = False

        # Set FPS on components once connected
        fps_set = False

        while self._running:
            try:
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
                    self._clip_writer.fps = self._grabber.fps
                    fps_set = True

                # Compute detection state once per iteration
                in_schedule = self._detection_enabled and (self._schedule_override or self._is_in_schedule())

                # Flush clip writer on active → idle transition
                if was_active and not in_schedule:
                    self._clip_writer.flush()
                was_active = in_schedule

                # Frame skipping (doubled when idle to save CPU)
                skip_counter += 1
                effective_skip = frame_skip if in_schedule else frame_skip * 2
                if skip_counter % effective_skip != 0:
                    if in_schedule:
                        # Still feed the clip writer for continuous recording
                        display = self._preprocessor.resize_only(frame)
                        self._stamp_timestamp(display)
                        raw_annotated = self._annotate_fullres(frame, {})
                        self._clip_writer.feed_frame(display, raw_annotated)
                    else:
                        time.sleep(0.001)  # yield CPU when idle
                    continue

                self._frame_count += 1
                display = self._preprocessor.resize_only(frame)

                if in_schedule:
                    # Active path: full pipeline
                    timestamp = time.time()
                    gray = self._preprocessor.process(frame)
                    detections = self._detector.detect(gray, frame_num, timestamp)
                    if self._exclusion_zones:
                        detections = [d for d in detections
                                      if not point_in_any_zone(d.x, d.y, self._exclusion_zones)]
                    tracks = self._tracker.update(detections)
                    self._active_tracks = len(tracks)

                    # Check for completed tracks (were active, now gone)
                    mature = self._tracker.mature_objects
                    current_ids = set(tracks.keys())
                    self._prev_track_ids = current_ids

                    # Record & log for mature tracks
                    has_detections = len(mature) > 0
                    if has_detections:
                        clip_path = self._clip_writer.trigger_recording()
                        for oid, obj in mature.items():
                            self._publish_event(obj, clip_path)

                    annotated = self._draw_overlays(display, tracks)
                    raw_annotated = self._annotate_fullres(frame, tracks)
                    self._clip_writer.feed_frame(annotated, raw_annotated)
                else:
                    # Idle path: HUD only, no preprocessing/tracking/recording
                    self._active_tracks = 0
                    annotated = self._draw_overlays(display, {})

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

            except Exception:
                logger.exception("Error in process loop, recovering...")
                time.sleep(0.1)

    def _draw_overlays(self, frame: np.ndarray,
                       tracks: dict[int, TrackedObject]) -> np.ndarray:
        """Draw bounding boxes, IDs, and trajectory overlays."""
        annotated = frame.copy()
        color = (0, 255, 0)  # Green for all tracks

        for oid, obj in tracks.items():
            cx, cy = obj.centroid

            # Draw crosshair
            cv2.drawMarker(annotated, (cx, cy), color,
                           cv2.MARKER_CROSS, 15, 1)

            # Draw trajectory
            if len(obj.positions) >= 2:
                pts = np.array(obj.positions, dtype=np.int32)
                cv2.polylines(annotated, [pts], False, color, 1)

            # Label
            cv2.putText(annotated, f"#{oid}", (cx + 10, cy - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)

        # Exclusion zone overlays
        if self._exclusion_zones:
            overlay = annotated.copy()
            for z in self._exclusion_zones:
                x1, y1 = z["x"], z["y"]
                x2, y2 = x1 + z["w"], y1 + z["h"]
                cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 0, 255), -1)
            cv2.addWeighted(overlay, 0.25, annotated, 0.75, 0, annotated)
            for z in self._exclusion_zones:
                x1, y1 = z["x"], z["y"]
                x2, y2 = x1 + z["w"], y1 + z["h"]
                cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 0, 255), 1)

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

    def _annotate_fullres(self, frame: np.ndarray,
                          tracks: dict[int, TrackedObject]) -> np.ndarray:
        """Draw bounding boxes and timestamp on a full-resolution frame."""
        annotated = frame.copy()
        h, w = annotated.shape[:2]
        sx = w / self._config.processing.resize_width
        sy = h / self._config.processing.resize_height
        scale = (sx + sy) / 2
        color = (0, 255, 0)

        for obj in tracks.values():
            cx, cy = obj.centroid
            # Draw a box around the detection, scaled to full resolution
            box_half = int(15 * scale)
            x1 = int(cx * sx) - box_half
            y1 = int(cy * sy) - box_half
            x2 = int(cx * sx) + box_half
            y2 = int(cy * sy) + box_half
            cv2.rectangle(annotated, (x1, y1), (x2, y2),
                          color, max(1, int(scale)))

        # Timestamp
        ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        font_scale = 0.45 * scale
        thickness = max(1, int(scale))
        cv2.putText(annotated, ts, (int(10 * sx), h - int(10 * sy)),
                    cv2.FONT_HERSHEY_SIMPLEX, font_scale,
                    (255, 255, 255), thickness, cv2.LINE_AA)
        return annotated

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

        # Total travel distance (sum of Euclidean steps)
        travel = sum(
            ((positions[i + 1][0] - positions[i][0]) ** 2 +
             (positions[i + 1][1] - positions[i][1]) ** 2) ** 0.5
            for i in range(len(positions) - 1)
        )

        event = DetectionEvent(
            object_id=obj.object_id,
            start_time=time.time(),
            end_time=time.time(),
            start_frame=obj.frame_history[0] if obj.frame_history else 0,
            end_frame=obj.frame_history[-1] if obj.frame_history else 0,
            avg_x=avg_x,
            avg_y=avg_y,
            avg_speed=avg_speed,
            travel_distance=travel,
            clip_path=clip_path,
        )

        # Log to DB (deduplicate by object_id — only log once per track)
        if not hasattr(self, '_logged_ids'):
            self._logged_ids: set[int] = set()
        if obj.object_id not in self._logged_ids:
            self._logged_ids.add(obj.object_id)

            # Capture thumbnail with detection bounding box
            try:
                thumb_frame = None
                with self._display_lock:
                    if self._display_frame is not None:
                        thumb_frame = self._display_frame.copy()
                if thumb_frame is not None:
                    cx, cy = obj.centroid
                    box_half = 15
                    cv2.rectangle(
                        thumb_frame,
                        (cx - box_half, cy - box_half),
                        (cx + box_half, cy + box_half),
                        (0, 255, 0), 2,
                    )
                    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                    thumb_name = f"thumb_{ts_str}_{obj.object_id}.jpg"
                    thumb_path = self._thumb_dir / thumb_name
                    cv2.imwrite(str(thumb_path), thumb_frame,
                                [cv2.IMWRITE_JPEG_QUALITY, self._config.recording.jpeg_quality])
                    event.thumbnail_path = thumb_name
            except Exception:
                logger.exception("Failed to save thumbnail for object %d", obj.object_id)

            event.event_id = self._event_logger.log_event(event)

        # Publish to WebSocket subscribers
        event_data = {
            "type": "detection",
            "event_id": event.event_id,
            "object_id": event.object_id,
            "x": avg_x,
            "y": avg_y,
            "speed": avg_speed,
            "travel_distance": travel,
        }

        for callback in self._event_callbacks:
            try:
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(callback, event_data)
                else:
                    callback(event_data)
            except Exception:
                logger.exception("Error in event callback")

    # --- Exclusion zones ---

    def get_zones(self) -> list[dict]:
        return list(self._exclusion_zones)

    def set_zones(self, zones: list[dict]) -> None:
        self._exclusion_zones = zones
        save_zones(zones)

    def add_zone(self, zone: dict) -> None:
        zones = list(self._exclusion_zones)
        zones.append(zone)
        self._exclusion_zones = zones
        save_zones(zones)

    def delete_zone(self, zone_id: str) -> bool:
        zones = [z for z in self._exclusion_zones if z["id"] != zone_id]
        if len(zones) == len(self._exclusion_zones):
            return False
        self._exclusion_zones = zones
        save_zones(zones)
        return True
