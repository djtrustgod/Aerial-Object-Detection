"""Tests for ClipWriter, focused on flush() waiting for encoders."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from src.recording.clip_writer import ClipWriter


def _make_frame(color: int = 128) -> np.ndarray:
    return np.full((180, 320, 3), color, dtype=np.uint8)


def test_flush_waits_for_encoder_and_writes_nonempty_clip(tmp_path: Path) -> None:
    cw = ClipWriter(
        clip_dir=str(tmp_path),
        pre_buffer_seconds=0.2,
        post_buffer_seconds=0.5,
        fps=15.0,
    )

    # Populate the rolling buffer and start a recording.
    for i in range(5):
        cw.feed_frame(_make_frame(50 + i * 20), clean_frame=_make_frame(80 + i * 10))
    clip_path = cw.trigger_recording()
    assert clip_path is not None

    # Feed a few more so _record_frames / _record_frames_clean have content.
    for i in range(5):
        cw.feed_frame(_make_frame(100 + i * 10), clean_frame=_make_frame(120 + i * 5))

    cw.flush(writer_timeout=30.0)

    annotated = Path(clip_path)
    clean = Path(clip_path.replace(".mp4", "_clean.mp4"))
    assert annotated.exists(), "annotated clip missing after flush"
    assert clean.exists(), "clean clip missing after flush"
    assert annotated.stat().st_size > 0, "annotated clip is 0 bytes — flush did not wait"
    assert clean.stat().st_size > 0, "clean clip is 0 bytes — flush did not wait"


def test_sustained_retriggers_do_not_grow_frame_list_past_ceiling(
    tmp_path: Path,
) -> None:
    """A noisy sky that re-triggers every frame must not grow memory forever."""
    cw = ClipWriter(
        clip_dir=str(tmp_path),
        pre_buffer_seconds=0.1,
        post_buffer_seconds=0.5,
        fps=10.0,
        max_clip_seconds=1.0,  # 1s × 10fps = at most ~11 frames in the list
        max_concurrent_encoders=4,
    )

    cw.trigger_recording()
    # Feed far more frames than the ceiling would allow if re-triggers could
    # extend forever. Re-trigger on every frame to simulate the noisy-sky case.
    for _ in range(100):
        cw.feed_frame(_make_frame(), clean_frame=_make_frame())
        cw.trigger_recording()

    # Once the ceiling is hit, recording should have finalized and been
    # handed off to an encoder thread. The in-memory record lists should
    # be empty or well below the runaway count.
    assert len(cw._record_frames) <= 11
    assert len(cw._record_frames_raw) <= 11
    assert len(cw._record_frames_clean) <= 11

    cw.flush(writer_timeout=30.0)


def test_over_limit_encoders_are_dropped(tmp_path: Path, caplog) -> None:
    """_spawn_writer drops work once the encoder pool is full."""
    import logging
    import threading

    cw = ClipWriter(
        clip_dir=str(tmp_path),
        pre_buffer_seconds=0.1,
        post_buffer_seconds=0.1,
        fps=10.0,
        max_concurrent_encoders=2,
    )

    block = threading.Event()

    def _slow_target(path: str) -> None:
        block.wait(timeout=5.0)

    with caplog.at_level(logging.WARNING, logger="src.recording.clip_writer"):
        cw._spawn_writer(_slow_target, ("clip_a.mp4",))
        cw._spawn_writer(_slow_target, ("clip_b.mp4",))
        cw._spawn_writer(_slow_target, ("clip_c.mp4",))  # should be dropped
        cw._spawn_writer(_slow_target, ("clip_d.mp4",))  # should be dropped

        dropped = [r for r in caplog.records if "at capacity" in r.getMessage()]
        assert len(dropped) == 2, f"expected 2 drops, got {len(dropped)}"

        # Only the first two threads should actually be running.
        alive = [t for t in cw._writer_threads if t.is_alive()]
        assert len(alive) == 2

    # Release the slow targets so the threads exit before the test ends.
    block.set()
    for t in cw._writer_threads:
        t.join(timeout=5.0)


def test_write_clip_removes_zero_byte_output_on_failure(tmp_path: Path, monkeypatch) -> None:
    """If the encoder exits without producing data, the 0-byte file should be deleted."""
    from src.recording import clip_writer as cw_mod

    class _FakeWriter:
        def __init__(self, path: str) -> None:
            self._path = path
            Path(path).touch()

        def append_data(self, _frame) -> None:
            raise RuntimeError("simulated ffmpeg failure")

        def close(self) -> None:
            pass

    def fake_get_writer(path, **_kwargs):
        return _FakeWriter(path)

    monkeypatch.setattr(cw_mod.imageio, "get_writer", fake_get_writer)

    out = tmp_path / "broken.mp4"
    frames = [_make_frame() for _ in range(3)]
    ClipWriter._write_clip(frames, str(out), fps=15.0)

    assert not out.exists(), "empty clip should have been deleted"
