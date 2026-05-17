"""Tests for ClipWriter, focused on streaming encoders and memory bounds."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from src.recording.clip_writer import ClipWriter, _ClipEncoder


def _make_frame(color: int = 128) -> np.ndarray:
    return np.full((180, 320, 3), color, dtype=np.uint8)


def test_flush_waits_for_encoder_and_writes_nonempty_clip(tmp_path: Path) -> None:
    cw = ClipWriter(
        clip_dir=str(tmp_path),
        pre_buffer_seconds=0.2,
        post_buffer_seconds=0.5,
        fps=15.0,
    )

    # Populate the rolling buffer so trigger_recording starts the clean
    # encoder too (it only spins up when there's a clean pre-buffer).
    for i in range(5):
        cw.feed_frame(_make_frame(50 + i * 20), clean_frame=_make_frame(80 + i * 10))
    clip_path = cw.trigger_recording()
    assert clip_path is not None

    # Feed a few more so the encoders see post-buffer frames.
    for i in range(5):
        cw.feed_frame(_make_frame(100 + i * 10), clean_frame=_make_frame(120 + i * 5))

    cw.flush(writer_timeout=30.0)

    annotated = Path(clip_path)
    clean = Path(clip_path.replace(".mp4", "_clean.mp4"))
    assert annotated.exists(), "annotated clip missing after flush"
    assert clean.exists(), "clean clip missing after flush"
    assert annotated.stat().st_size > 0, "annotated clip is 0 bytes — flush did not wait"
    assert clean.stat().st_size > 0, "clean clip is 0 bytes — flush did not wait"


def test_streaming_caps_memory_under_sustained_retriggers(tmp_path: Path) -> None:
    """A noisy sky that re-triggers every frame must not grow memory forever.

    Under the streaming design, in-flight memory is bounded by the encoder
    queue depth, not by the clip length. Push far more frames than any
    accumulated list would have been allowed and verify the encoder queues
    stay bounded.
    """
    cw = ClipWriter(
        clip_dir=str(tmp_path),
        pre_buffer_seconds=0.1,
        post_buffer_seconds=0.5,
        fps=10.0,
        max_clip_seconds=2.0,
    )

    cw.trigger_recording()
    for _ in range(500):
        cw.feed_frame(_make_frame(), clean_frame=_make_frame())
        cw.trigger_recording()

    # Encoder queue is sized to pre_buffer_frames + 30 = ~31. We allow some
    # head-room for in-flight items in the consumer.
    for enc in cw._active_encoders:
        assert enc.qsize <= 64, f"encoder queue grew to {enc.qsize}"
    for enc in cw._draining_encoders:
        assert enc.qsize <= 64, f"draining encoder queue grew to {enc.qsize}"

    cw.flush(writer_timeout=30.0)


def test_clear_buffers_drops_prebuffer(tmp_path: Path) -> None:
    cw = ClipWriter(
        clip_dir=str(tmp_path),
        pre_buffer_seconds=0.5,
        post_buffer_seconds=0.5,
        fps=10.0,
        full_resolution=True,
    )

    for i in range(8):
        cw.feed_frame(
            _make_frame(50 + i),
            raw_frame=_make_frame(100 + i),
            clean_frame=_make_frame(150 + i),
        )

    assert len(cw._buffer) > 0
    assert len(cw._buffer_raw) > 0
    assert len(cw._buffer_clean) > 0

    cw.clear_buffers()
    assert len(cw._buffer) == 0
    assert len(cw._buffer_raw) == 0
    assert len(cw._buffer_clean) == 0


def test_encoder_drops_when_queue_full(tmp_path: Path, caplog) -> None:
    """A stalled encoder should drop frames and log, not block the producer."""
    import logging
    import threading
    from src.recording import clip_writer as cw_mod

    block = threading.Event()

    class _SlowWriter:
        def __init__(self, path: str) -> None:
            self._path = path
            Path(path).touch()

        def append_data(self, _frame) -> None:
            block.wait(timeout=5.0)

        def close(self) -> None:
            pass

    def fake_get_writer(path, **_kwargs):
        return _SlowWriter(path)

    monkeypatch_target = cw_mod.imageio
    original = monkeypatch_target.get_writer
    monkeypatch_target.get_writer = fake_get_writer
    try:
        enc = _ClipEncoder(
            str(tmp_path / "stuck.mp4"),
            fps=15.0, high_quality=False, max_queue=4,
        )
        with caplog.at_level(logging.WARNING, logger="src.recording.clip_writer"):
            for _ in range(50):
                enc.submit(_make_frame())
            assert enc.dropped > 0, "expected at least one dropped frame"
            backpressure_logs = [
                r for r in caplog.records if "backpressure" in r.getMessage()
            ]
            assert backpressure_logs, "expected a backpressure warning"
    finally:
        block.set()
        enc.close()
        enc.join(timeout=5.0)
        monkeypatch_target.get_writer = original


def test_encoder_removes_zero_byte_output_on_failure(tmp_path: Path, monkeypatch) -> None:
    """If the encoder writes nothing and exits, the 0-byte file is deleted."""
    from src.recording import clip_writer as cw_mod

    class _BrokenWriter:
        def __init__(self, path: str) -> None:
            self._path = path
            Path(path).touch()

        def append_data(self, _frame) -> None:
            raise RuntimeError("simulated ffmpeg failure")

        def close(self) -> None:
            pass

    def fake_get_writer(path, **_kwargs):
        return _BrokenWriter(path)

    monkeypatch.setattr(cw_mod.imageio, "get_writer", fake_get_writer)

    out = tmp_path / "broken.mp4"
    enc = _ClipEncoder(str(out), fps=15.0, high_quality=False, max_queue=8)
    enc.submit(_make_frame())
    enc.close()
    enc.join(timeout=5.0)

    assert not out.exists(), "empty clip should have been deleted"


def test_trigger_during_active_recording_returns_same_path(tmp_path: Path) -> None:
    cw = ClipWriter(
        clip_dir=str(tmp_path),
        pre_buffer_seconds=0.2,
        post_buffer_seconds=0.5,
        fps=10.0,
    )
    for _ in range(3):
        cw.feed_frame(_make_frame(), clean_frame=_make_frame())

    first = cw.trigger_recording()
    second = cw.trigger_recording()
    assert first is not None
    assert first == second, "re-trigger should return the same in-flight clip path"

    cw.flush(writer_timeout=30.0)
