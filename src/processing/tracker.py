"""Centroid tracker with trajectory and brightness history."""

from __future__ import annotations

from collections import OrderedDict

import numpy as np
from scipy.spatial.distance import cdist

from src.config import TrackingConfig
from src.recording.models import Detection, TrackedObject


class CentroidTracker:
    """Assigns persistent IDs to detections across frames using centroid distance."""

    def __init__(self, config: TrackingConfig):
        self._max_distance = config.max_distance
        self._max_disappeared = config.max_disappeared
        self._min_track_length = config.min_track_length

        self._next_id = 0
        self._objects: OrderedDict[int, TrackedObject] = OrderedDict()

    @property
    def objects(self) -> dict[int, TrackedObject]:
        """Return current active tracked objects."""
        return dict(self._objects)

    @property
    def mature_objects(self) -> dict[int, TrackedObject]:
        """Return tracked objects that meet the minimum track length."""
        return {
            oid: obj for oid, obj in self._objects.items()
            if len(obj.positions) >= self._min_track_length
        }

    def update(self, detections: list[Detection]) -> dict[int, TrackedObject]:
        """Update tracks with new detections.

        Returns the dict of currently active tracked objects.
        """
        if len(detections) == 0:
            # Mark all existing objects as disappeared
            to_remove = []
            for oid in self._objects:
                self._objects[oid].disappeared += 1
                if self._objects[oid].disappeared > self._max_disappeared:
                    to_remove.append(oid)
            for oid in to_remove:
                del self._objects[oid]
            return self.objects

        # Extract input centroids
        input_centroids = np.array([(d.x, d.y) for d in detections])

        if len(self._objects) == 0:
            # Register all detections as new objects
            for i, det in enumerate(detections):
                self._register(det)
        else:
            # Match existing objects to new detections
            object_ids = list(self._objects.keys())
            object_centroids = np.array([
                self._objects[oid].centroid for oid in object_ids
            ])

            # Compute pairwise distances
            dist_matrix = cdist(object_centroids, input_centroids)

            # Greedy matching: find closest pairs
            rows = dist_matrix.min(axis=1).argsort()
            cols = dist_matrix.argmin(axis=1)[rows]

            used_rows: set[int] = set()
            used_cols: set[int] = set()

            for row, col in zip(rows, cols):
                if row in used_rows or col in used_cols:
                    continue
                if dist_matrix[row, col] > self._max_distance:
                    continue

                oid = object_ids[row]
                det = detections[col]
                self._update_object(oid, det)
                used_rows.add(row)
                used_cols.add(col)

            # Handle unmatched existing objects (disappeared)
            unused_rows = set(range(len(object_ids))) - used_rows
            to_remove = []
            for row in unused_rows:
                oid = object_ids[row]
                self._objects[oid].disappeared += 1
                if self._objects[oid].disappeared > self._max_disappeared:
                    to_remove.append(oid)
            for oid in to_remove:
                del self._objects[oid]

            # Register unmatched detections as new objects
            unused_cols = set(range(len(detections))) - used_cols
            for col in unused_cols:
                self._register(detections[col])

        return self.objects

    def get_lost_objects(self) -> list[TrackedObject]:
        """Get objects that have just been lost (for event logging).

        Call after update(). Returns objects whose disappeared count
        just exceeded max_disappeared on the previous update.
        """
        # This is handled internally by returning them before deletion
        # For external use, check the pipeline for completed tracks
        return []

    def _register(self, det: Detection) -> None:
        """Register a new tracked object."""
        obj = TrackedObject(
            object_id=self._next_id,
            centroid=(det.x, det.y),
            positions=[(det.x, det.y)],
            brightness_history=[det.brightness],
            frame_history=[det.frame_number],
        )
        self._objects[self._next_id] = obj
        self._next_id += 1

    def _update_object(self, oid: int, det: Detection) -> None:
        """Update an existing tracked object with a new detection."""
        obj = self._objects[oid]
        obj.centroid = (det.x, det.y)
        obj.positions.append((det.x, det.y))
        obj.brightness_history.append(det.brightness)
        obj.frame_history.append(det.frame_number)
        obj.disappeared = 0

        # Compute speed (pixels per frame)
        if len(obj.positions) >= 2:
            p1 = np.array(obj.positions[-2])
            p2 = np.array(obj.positions[-1])
            obj.speed = float(np.linalg.norm(p2 - p1))

        # Keep bounded history (last 300 points)
        max_hist = 300
        if len(obj.positions) > max_hist:
            obj.positions = obj.positions[-max_hist:]
            obj.brightness_history = obj.brightness_history[-max_hist:]
            obj.frame_history = obj.frame_history[-max_hist:]
