"""Tests for the web API routes."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.config import AppConfig, RecordingConfig
from src.recording.event_logger import EventLogger
from src.recording.models import DetectionEvent
from src.web.app import create_app


@pytest.fixture
def tmp_dirs(tmp_path):
    """Create temporary clip and thumb directories."""
    clips = tmp_path / "clips"
    clips.mkdir()
    thumbs = tmp_path / "thumbs"
    thumbs.mkdir()
    db_path = str(tmp_path / "test.db")
    return clips, thumbs, db_path


@pytest.fixture
def pipeline_mock(tmp_dirs):
    """Create a mock pipeline with a real EventLogger."""
    clips, thumbs, db_path = tmp_dirs
    config = AppConfig(
        recording=RecordingConfig(
            clip_dir=str(clips),
            thumb_dir=str(thumbs),
            db_path=db_path,
        ),
    )
    el = EventLogger(db_path)

    pipeline = MagicMock()
    pipeline.config = config
    pipeline.event_logger = el
    pipeline.stats = {
        "fps": 10.0, "frame_count": 100, "active_tracks": 0,
        "connected": True, "detection_active": True,
        "schedule_enabled": False, "detection_enabled": True,
        "cpu_percent": 5.0, "mem_percent": 30.0,
    }
    pipeline.display_frame = None
    pipeline.get_zones.return_value = []

    yield pipeline
    el.close()


@pytest.fixture
def client(pipeline_mock):
    """Create a FastAPI test client."""
    app = create_app(pipeline_mock)
    return TestClient(app)


def _seed_events(logger: EventLogger):
    """Insert test events spanning two sessions."""
    base = 1700000000.0
    for i in range(3):
        logger.log_event(DetectionEvent(
            object_id=i, start_time=base + i * 60, end_time=base + i * 60 + 10,
            start_frame=0, end_frame=10,
            avg_x=100.0, avg_y=100.0, avg_speed=5.0,
            travel_distance=35.0 + i,
            clip_path=f"clip_{i}.mp4",
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


class TestRoutes:
    def test_history_returns_200(self, client):
        """GET /history should return 200."""
        resp = client.get("/history")
        assert resp.status_code == 200

    def test_api_events_has_travel_and_thumbnail(self, client, pipeline_mock):
        """GET /api/events should include travel_distance and thumbnail_path."""
        _seed_events(pipeline_mock.event_logger)
        resp = client.get("/api/events?limit=10")
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) == 5
        assert "travel_distance" in events[0]
        assert "thumbnail_path" in events[0]

    def test_api_sessions(self, client, pipeline_mock):
        """GET /api/sessions should return session list."""
        _seed_events(pipeline_mock.event_logger)
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        sessions = resp.json()
        assert len(sessions) == 2
        assert sessions[0]["session_id"] == 1
        assert "label" in sessions[0]

    def test_delete_removes_thumbnails(self, client, pipeline_mock, tmp_dirs):
        """DELETE /api/events should remove thumbnail files."""
        _, thumbs, _ = tmp_dirs
        _seed_events(pipeline_mock.event_logger)

        # Create actual thumbnail files
        (thumbs / "thumb_0.jpg").write_bytes(b"fake")
        (thumbs / "thumb_1.jpg").write_bytes(b"fake")

        events = pipeline_mock.event_logger.get_recent(10)
        ids = [e.event_id for e in events[:2]]

        resp = client.request("DELETE", "/api/events", json={"event_ids": ids})
        assert resp.status_code == 200
        result = resp.json()
        assert result["status"] == "ok"

    def test_delete_session(self, client, pipeline_mock, tmp_dirs):
        """DELETE /api/sessions/{id} should remove all session events."""
        _seed_events(pipeline_mock.event_logger)

        resp = client.delete("/api/sessions/1")
        assert resp.status_code == 200
        result = resp.json()
        assert result["status"] == "ok"
        assert result["deleted"] == 2

        # Only 3 events should remain
        remaining = pipeline_mock.event_logger.get_all()
        assert len(remaining) == 3

    def test_history_with_session_param(self, client, pipeline_mock):
        """GET /history?session=2 should show the older session."""
        _seed_events(pipeline_mock.event_logger)
        resp = client.get("/history?session=2")
        assert resp.status_code == 200
        assert "Detection History" in resp.text
