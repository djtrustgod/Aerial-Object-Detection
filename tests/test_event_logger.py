"""Tests for the SQLite event logger."""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from src.recording.event_logger import EventLogger
from src.recording.models import DetectionEvent


@pytest.fixture
def tmp_db(tmp_path):
    """Return a temporary database path."""
    return str(tmp_path / "test.db")


@pytest.fixture
def logger(tmp_db):
    """Return a fresh EventLogger instance."""
    el = EventLogger(tmp_db)
    yield el
    el.close()


class TestEventLogger:
    def test_create_table(self, logger, tmp_db):
        """Table should be created on init."""
        conn = sqlite3.connect(tmp_db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
        conn.close()
        assert "event_id" in cols
        assert "travel_distance" in cols
        assert "thumbnail_path" in cols

    def test_log_and_retrieve(self, logger):
        """Logged events should be retrievable with all fields."""
        event = DetectionEvent(
            object_id=1,
            start_time=1000.0,
            end_time=1010.0,
            start_frame=0,
            end_frame=100,
            avg_x=320.0,
            avg_y=180.0,
            avg_speed=5.5,
            travel_distance=42.5,
            clip_path="clip_test.mp4",
            thumbnail_path="thumb_test.jpg",
        )
        eid = logger.log_event(event)
        assert eid is not None

        events = logger.get_recent(10)
        assert len(events) == 1
        e = events[0]
        assert e.event_id == eid
        assert e.travel_distance == pytest.approx(42.5)
        assert e.thumbnail_path == "thumb_test.jpg"
        assert e.clip_path == "clip_test.mp4"

    def test_migration_adds_columns(self, tmp_path):
        """Existing DB without new columns should get them via migration."""
        db_path = str(tmp_path / "old.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id INTEGER NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                start_frame INTEGER NOT NULL,
                end_frame INTEGER NOT NULL,
                avg_x REAL NOT NULL,
                avg_y REAL NOT NULL,
                avg_speed REAL NOT NULL,
                trajectory_length INTEGER NOT NULL,
                clip_path TEXT
            )
        """)
        conn.commit()
        conn.close()

        el = EventLogger(db_path)
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
        conn.close()
        el.close()

        assert "travel_distance" in cols
        assert "thumbnail_path" in cols

    def test_delete_returns_thumb_paths(self, logger):
        """Deleting events should return both clip and thumbnail paths."""
        e1 = DetectionEvent(
            object_id=1, start_time=1000.0, end_time=1010.0,
            start_frame=0, end_frame=100,
            avg_x=100.0, avg_y=100.0, avg_speed=5.0,
            travel_distance=30.0,
            clip_path="clip_1.mp4",
            thumbnail_path="thumb_1.jpg",
        )
        e2 = DetectionEvent(
            object_id=2, start_time=1020.0, end_time=1030.0,
            start_frame=100, end_frame=200,
            avg_x=200.0, avg_y=200.0, avg_speed=3.0,
            travel_distance=20.0,
            clip_path="clip_2.mp4",
            thumbnail_path="thumb_2.jpg",
        )
        eid1 = logger.log_event(e1)
        eid2 = logger.log_event(e2)

        clip_paths, thumb_paths = logger.delete_by_ids([eid1, eid2])
        assert "clip_1.mp4" in clip_paths
        assert "thumb_1.jpg" in thumb_paths
        assert "thumb_2.jpg" in thumb_paths

    def test_clear_all_returns_thumb_paths(self, logger):
        """clear_all should return thumbnail paths along with clips."""
        logger.log_event(DetectionEvent(
            object_id=1, start_time=1000.0, end_time=1010.0,
            start_frame=0, end_frame=100,
            avg_x=100.0, avg_y=100.0, avg_speed=5.0,
            travel_distance=30.0,
            thumbnail_path="thumb_a.jpg",
        ))
        count, clip_paths, thumb_paths = logger.clear_all()
        assert count == 1
        assert "thumb_a.jpg" in thumb_paths

    def test_get_sessions(self, logger):
        """Events with a 5-hour gap should form two sessions."""
        base = 1700000000.0
        # Session 1: 3 events within minutes
        for i in range(3):
            logger.log_event(DetectionEvent(
                object_id=i, start_time=base + i * 60, end_time=base + i * 60 + 10,
                start_frame=0, end_frame=10,
                avg_x=100.0, avg_y=100.0, avg_speed=5.0,
                travel_distance=30.0,
            ))
        # Session 2: 2 events 5 hours later
        for i in range(2):
            logger.log_event(DetectionEvent(
                object_id=10 + i,
                start_time=base + 18000 + i * 60,
                end_time=base + 18000 + i * 60 + 10,
                start_frame=0, end_frame=10,
                avg_x=200.0, avg_y=200.0, avg_speed=3.0,
                travel_distance=20.0,
            ))

        sessions = logger.get_sessions()
        assert len(sessions) == 2
        # Newest first
        assert sessions[0]["event_count"] == 2
        assert sessions[1]["event_count"] == 3

    def test_get_events_by_session(self, logger):
        """Should return only events from the specified session."""
        base = 1700000000.0
        for i in range(3):
            logger.log_event(DetectionEvent(
                object_id=i, start_time=base + i * 60, end_time=base + i * 60 + 10,
                start_frame=0, end_frame=10,
                avg_x=100.0, avg_y=100.0, avg_speed=5.0,
                travel_distance=30.0,
            ))
        for i in range(2):
            logger.log_event(DetectionEvent(
                object_id=10 + i,
                start_time=base + 18000 + i * 60,
                end_time=base + 18000 + i * 60 + 10,
                start_frame=0, end_frame=10,
                avg_x=200.0, avg_y=200.0, avg_speed=3.0,
                travel_distance=20.0,
            ))

        # Session 1 = newest (2 events)
        events = logger.get_events_by_session(1)
        assert len(events) == 2

        # Session 2 = oldest (3 events)
        events = logger.get_events_by_session(2)
        assert len(events) == 3

    def test_delete_by_session(self, logger):
        """delete_by_session should remove only events in that session."""
        base = 1700000000.0
        for i in range(3):
            logger.log_event(DetectionEvent(
                object_id=i, start_time=base + i * 60, end_time=base + i * 60 + 10,
                start_frame=0, end_frame=10,
                avg_x=100.0, avg_y=100.0, avg_speed=5.0,
                travel_distance=30.0,
                thumbnail_path=f"thumb_{i}.jpg",
            ))
        for i in range(2):
            logger.log_event(DetectionEvent(
                object_id=10 + i,
                start_time=base + 18000 + i * 60,
                end_time=base + 18000 + i * 60 + 10,
                start_frame=0, end_frame=10,
                avg_x=200.0, avg_y=200.0, avg_speed=3.0,
                travel_distance=20.0,
                thumbnail_path=f"thumb_{10 + i}.jpg",
            ))

        # Delete newest session (2 events)
        count, clips, thumbs = logger.delete_by_session(1)
        assert count == 2
        assert len(thumbs) == 2

        # 3 events remain
        remaining = logger.get_all()
        assert len(remaining) == 3
