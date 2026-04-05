"""Test pipeline for processing uploaded video files against detection settings."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from datetime import datetime

import cv2
import numpy as np

from src.config import AppConfig
from src.processing.preprocessor import Preprocessor
from src.processing.detector import Detector
from src.processing.tracker import CentroidTracker
from src.zones import load_zones, point_in_any_zone

logger = logging.getLogger(__name__)

# Module-level singleton for active test
_active_test: TestPipeline | None = None
_lock = threading.Lock()


def get_active_test() -> TestPipeline | None:
    return _active_test


def set_active_test(test: TestPipeline | None) -> None:
    global _active_test
    with _lock:
        _active_test = test


class TestPipeline:
    """Processes an uploaded video through the detection pipeline.

    Supports two modes:
    - Preview mode: streams annotated frames at video FPS via display_frame property
    - Analysis mode: processes entire video at max speed and returns metrics
    """

    def __init__(self, video_path: str, config: AppConfig):
        self._video_path = video_path
        self._config = config

        # Will be initialized when starting
        self._preprocessor: Preprocessor | None = None
        self._detector: Detector | None = None
        self._tracker: CentroidTracker | None = None
        self._exclusion_zones: list[dict] = []

        # State
        self._running = False
        self._complete = False
        self._thread: threading.Thread | None = None
        self._display_frame: np.ndarray | None = None
        self._display_lock = threading.Lock()
        self._current_frame = 0
        self._total_frames = 0
        self._video_fps = 30.0

    @property
    def display_frame(self) -> np.ndarray | None:
        with self._display_lock:
            return self._display_frame.copy() if self._display_frame is not None else None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_complete(self) -> bool:
        return self._complete

    @property
    def progress(self) -> float:
        if self._total_frames == 0:
            return 0.0
        return self._current_frame / self._total_frames

    @property
    def current_frame(self) -> int:
        return self._current_frame

    @property
    def total_frames(self) -> int:
        return self._total_frames

    @property
    def video_fps(self) -> float:
        return self._video_fps

    def _init_components(self) -> None:
        """Create fresh processing component instances from config."""
        self._preprocessor = Preprocessor(self._config.processing)
        self._detector = Detector(self._config.detection)
        self._tracker = CentroidTracker(self._config.tracking)
        self._exclusion_zones = load_zones()

    def start(self) -> None:
        """Start preview mode in a background thread."""
        if self._running:
            return
        self._running = True
        self._complete = False
        self._current_frame = 0
        self._init_components()
        self._thread = threading.Thread(target=self._preview_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the preview loop."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _preview_loop(self) -> None:
        """Process video frames and update display_frame at video FPS."""
        cap = cv2.VideoCapture(self._video_path)
        if not cap.isOpened():
            logger.error("Cannot open video: %s", self._video_path)
            self._running = False
            self._complete = True
            return

        self._video_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self._total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frame_interval = 1.0 / self._video_fps
        frame_num = 0

        try:
            while self._running:
                ret, frame = cap.read()
                if not ret:
                    break

                frame_num += 1
                self._current_frame = frame_num
                timestamp = time.time()

                # Process through detection pipeline
                display = self._preprocessor.resize_only(frame)
                gray = self._preprocessor.process(frame)
                detections = self._detector.detect(gray, frame_num, timestamp)

                if self._exclusion_zones:
                    detections = [d for d in detections
                                  if not point_in_any_zone(d.x, d.y, self._exclusion_zones)]

                tracks = self._tracker.update(detections)
                mature = self._tracker.mature_objects

                # Draw only mature track bounding boxes
                annotated = self._draw_test_overlays(display, mature)

                with self._display_lock:
                    self._display_frame = annotated

                # Pace at video FPS
                time.sleep(frame_interval)
        except Exception:
            logger.exception("Error in test preview loop")
        finally:
            cap.release()
            self._running = False
            self._complete = True
            logger.info("Test preview complete: %d/%d frames", frame_num, self._total_frames)

    def run_analysis(self) -> dict:
        """Process entire video at max speed and return detection metrics.

        This runs synchronously (blocking) and returns aggregate statistics.
        """
        self._init_components()

        cap = cv2.VideoCapture(self._video_path)
        if not cap.isOpened():
            return {"error": f"Cannot open video: {self._video_path}"}

        self._video_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self._total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        frames_with_detections = 0
        all_mature_tracks: dict[int, dict] = {}  # track_id -> stats
        frame_num = 0

        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break

                frame_num += 1
                self._current_frame = frame_num
                timestamp = time.time()

                gray = self._preprocessor.process(frame)
                detections = self._detector.detect(gray, frame_num, timestamp)

                if self._exclusion_zones:
                    detections = [d for d in detections
                                  if not point_in_any_zone(d.x, d.y, self._exclusion_zones)]

                if detections:
                    frames_with_detections += 1

                tracks = self._tracker.update(detections)
                mature = self._tracker.mature_objects

                # Record mature track stats
                for oid, obj in mature.items():
                    if oid not in all_mature_tracks:
                        positions = obj.positions
                        travel = sum(
                            ((positions[i + 1][0] - positions[i][0]) ** 2 +
                             (positions[i + 1][1] - positions[i][1]) ** 2) ** 0.5
                            for i in range(len(positions) - 1)
                        ) if len(positions) > 1 else 0.0
                        all_mature_tracks[oid] = {
                            "track_length": len(positions),
                            "travel_distance": round(travel, 1),
                        }
        finally:
            cap.release()

        # Compute aggregate metrics
        num_mature = len(all_mature_tracks)
        avg_track_length = 0.0
        avg_track_travel = 0.0
        if num_mature > 0:
            avg_track_length = sum(t["track_length"] for t in all_mature_tracks.values()) / num_mature
            avg_track_travel = sum(t["travel_distance"] for t in all_mature_tracks.values()) / num_mature

        return {
            "total_frames": frame_num,
            "frames_with_detections": frames_with_detections,
            "mature_tracks_formed": num_mature,
            "detection_rate": round(frames_with_detections / max(frame_num, 1), 4),
            "avg_track_length": round(avg_track_length, 1),
            "avg_track_travel": round(avg_track_travel, 1),
            "video_fps": round(self._video_fps, 1),
            "settings_used": {
                "detection": asdict(self._config.detection),
                "tracking": asdict(self._config.tracking),
                "processing": {
                    "resize_width": self._config.processing.resize_width,
                    "resize_height": self._config.processing.resize_height,
                    "clahe_clip_limit": self._config.processing.clahe_clip_limit,
                    "blur_kernel": self._config.processing.blur_kernel,
                },
            },
        }

    def _draw_test_overlays(self, frame: np.ndarray,
                            mature_tracks: dict) -> np.ndarray:
        """Draw bounding box rectangles only for mature tracks (History-matching criteria)."""
        annotated = frame.copy()
        color = (0, 255, 0)  # Green

        for obj in mature_tracks.values():
            cx, cy = obj.centroid
            # Draw a bounding box rectangle around the object
            box_half = 15
            x1 = max(0, cx - box_half)
            y1 = max(0, cy - box_half)
            x2 = min(annotated.shape[1], cx + box_half)
            y2 = min(annotated.shape[0], cy + box_half)
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)

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

        # Minimal HUD
        h, w = annotated.shape[:2]
        cv2.putText(annotated, f"Frame: {self._current_frame}/{self._total_frames}",
                    (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        mature_count = len(mature_tracks)
        cv2.putText(annotated, f"Mature Tracks: {mature_count}",
                    (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        # Timestamp
        ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        cv2.putText(annotated, ts, (10, h - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)

        return annotated
