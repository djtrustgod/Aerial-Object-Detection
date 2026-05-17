"""Tests for Pipeline schedule + override gating logic.

The pipeline's `_should_detect()` is the single source of truth for whether
detection should run on a given iteration. These tests poke that method
directly, bypassing the threaded capture loop. We mock `_is_in_schedule` so
we can simulate schedule transitions without sleeping.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.config import AppConfig, ScheduleConfig
from src.pipeline import Pipeline


@pytest.fixture
def pipeline(tmp_path):
    """A Pipeline with all subsystems unstarted, suitable for logic tests."""
    config = AppConfig()
    config.recording.clip_dir = str(tmp_path / "clips")
    config.recording.thumb_dir = str(tmp_path / "thumbs")
    config.recording.db_path = str(tmp_path / "db" / "test.db")
    config.recording.sweep_orphan_clips = False
    config.capture.rtsp_url = ""
    # Default: schedule on, overnight window (21:00–05:00).
    config.schedule = ScheduleConfig(
        enabled=True, start_time="21:00", end_time="05:00",
    )

    p = Pipeline(config)
    yield p
    # Clean up the event logger so the temp DB closes.
    p.event_logger.close()


def _patch_in_schedule(p: Pipeline, value: bool):
    return patch.object(Pipeline, "_is_in_schedule", return_value=value)


def test_schedule_disabled_toggle_authoritative(pipeline):
    pipeline._config.schedule.enabled = False

    pipeline.set_detection_enabled(True)
    assert pipeline._should_detect() is True

    pipeline.set_detection_enabled(False)
    assert pipeline._should_detect() is False


def test_schedule_on_no_override_follows_window(pipeline):
    # Default: detection_enabled=True at construction.
    with _patch_in_schedule(pipeline, True):
        assert pipeline._should_detect() is True
    with _patch_in_schedule(pipeline, False):
        assert pipeline._should_detect() is False


def test_force_on_outside_window_runs_until_schedule_transitions(pipeline):
    # Pretend we're currently outside the window when the toggle is clicked.
    with _patch_in_schedule(pipeline, False):
        pipeline.set_detection_enabled(True)
        # Override active; detection runs even though the schedule says no.
        assert pipeline._schedule_override is True
        assert pipeline._override_baseline is False
        assert pipeline._should_detect() is True

    # Schedule transitions to in-window: override releases, schedule_says wins.
    with _patch_in_schedule(pipeline, True):
        assert pipeline._should_detect() is True
        assert pipeline._schedule_override is False
        assert pipeline._override_baseline is None

    # Window ends again; no override left, schedule controls.
    with _patch_in_schedule(pipeline, False):
        assert pipeline._should_detect() is False


def test_force_off_inside_window_blocks_until_transition(pipeline):
    with _patch_in_schedule(pipeline, True):
        pipeline.set_detection_enabled(False)
        assert pipeline._schedule_override is True
        assert pipeline._override_baseline is True
        # Detection off despite schedule saying go.
        assert pipeline._should_detect() is False

    # Schedule transitions to out-of-window: override clears, schedule wins.
    with _patch_in_schedule(pipeline, False):
        assert pipeline._should_detect() is False
        assert pipeline._schedule_override is False

    # Next time the window opens, detection resumes via schedule.
    with _patch_in_schedule(pipeline, True):
        assert pipeline._should_detect() is True


def test_force_on_inside_window_is_idempotent_with_schedule(pipeline):
    """Toggling ON when the schedule already says ON is harmless and clears
    naturally at the next transition.
    """
    with _patch_in_schedule(pipeline, True):
        pipeline.set_detection_enabled(True)
        assert pipeline._should_detect() is True

    with _patch_in_schedule(pipeline, False):
        # Override clears; result tracks the schedule.
        assert pipeline._should_detect() is False
        assert pipeline._schedule_override is False


def test_force_off_outside_window_is_idempotent_with_schedule(pipeline):
    with _patch_in_schedule(pipeline, False):
        pipeline.set_detection_enabled(False)
        assert pipeline._should_detect() is False

    with _patch_in_schedule(pipeline, True):
        # Override clears; schedule wins.
        assert pipeline._should_detect() is True
        assert pipeline._schedule_override is False


def test_update_schedule_config_resets_override(pipeline):
    with _patch_in_schedule(pipeline, False):
        pipeline.set_detection_enabled(True)
        assert pipeline._schedule_override is True

    # User changes the schedule window — old baseline no longer means anything.
    pipeline.update_schedule_config(start_time="06:00", end_time="22:00")
    assert pipeline._schedule_override is False
    assert pipeline._override_baseline is None


def test_stats_uses_should_detect(pipeline):
    """The /api/stats payload must reflect the gating logic, including override."""
    with _patch_in_schedule(pipeline, False):
        assert pipeline.stats["detection_active"] is False

        pipeline.set_detection_enabled(True)
        assert pipeline.stats["detection_active"] is True

    # After the schedule transitions, override clears and stats reflects schedule.
    with _patch_in_schedule(pipeline, True):
        assert pipeline.stats["detection_active"] is True

    with _patch_in_schedule(pipeline, False):
        assert pipeline.stats["detection_active"] is False
