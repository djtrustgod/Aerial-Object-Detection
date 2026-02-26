"""Tests for the centroid tracker."""

from __future__ import annotations

import pytest

from src.processing.tracker import CentroidTracker
from src.recording.models import Detection


def make_detection(x: int, y: int, frame: int = 0) -> Detection:
    return Detection(x=x, y=y, w=10, h=10, area=50, brightness=200,
                     frame_number=frame)


class TestCentroidTracker:
    def test_registers_new_objects(self, tracking_config):
        """New detections should be registered with unique IDs."""
        tracker = CentroidTracker(tracking_config)
        dets = [make_detection(100, 100), make_detection(300, 300)]
        tracks = tracker.update(dets)

        assert len(tracks) == 2
        ids = list(tracks.keys())
        assert ids[0] != ids[1]

    def test_tracks_moving_object(self, tracking_config):
        """A smoothly moving object should keep the same ID."""
        tracker = CentroidTracker(tracking_config)

        # Frame 1: object at (100, 100)
        tracker.update([make_detection(100, 100, frame=0)])
        # Frame 2: object moved to (110, 100)
        tracker.update([make_detection(110, 100, frame=1)])
        # Frame 3: object moved to (120, 100)
        tracks = tracker.update([make_detection(120, 100, frame=2)])

        assert len(tracks) == 1
        obj = list(tracks.values())[0]
        assert len(obj.positions) == 3
        assert obj.centroid == (120, 100)

    def test_removes_disappeared_objects(self, tracking_config):
        """Objects that disappear for too long should be removed."""
        tracking_config.max_disappeared = 3
        tracker = CentroidTracker(tracking_config)

        # Register an object
        tracker.update([make_detection(100, 100)])

        # Object disappears for max_disappeared+1 frames
        for _ in range(4):
            tracks = tracker.update([])

        assert len(tracks) == 0

    def test_handles_two_objects(self, tracking_config):
        """Two objects moving in parallel should maintain separate IDs."""
        tracker = CentroidTracker(tracking_config)

        for i in range(5):
            dets = [
                make_detection(100 + i * 5, 100, frame=i),
                make_detection(100 + i * 5, 300, frame=i),
            ]
            tracks = tracker.update(dets)

        assert len(tracks) == 2
        positions = [obj.centroid[1] for obj in tracks.values()]
        assert 100 in positions
        assert 300 in positions

    def test_mature_objects(self, tracking_config):
        """Only tracks with enough history should appear in mature_objects."""
        tracking_config.min_track_length = 3
        tracker = CentroidTracker(tracking_config)

        # One frame — not mature yet
        tracker.update([make_detection(100, 100, frame=0)])
        assert len(tracker.mature_objects) == 0

        # Two frames — still not mature
        tracker.update([make_detection(110, 100, frame=1)])
        assert len(tracker.mature_objects) == 0

        # Three frames — now mature
        tracker.update([make_detection(120, 100, frame=2)])
        assert len(tracker.mature_objects) == 1

    def test_computes_speed(self, tracking_config):
        """Speed should be computed from consecutive positions."""
        tracker = CentroidTracker(tracking_config)

        tracker.update([make_detection(100, 100, frame=0)])
        tracker.update([make_detection(110, 100, frame=1)])
        tracks = tracker.update([make_detection(120, 100, frame=2)])

        obj = list(tracks.values())[0]
        assert obj.speed == pytest.approx(10.0, abs=0.1)
