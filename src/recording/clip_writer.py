"""Rolling pre-buffer + streaming MP4 clip writer.

Frames are streamed to ffmpeg via a bounded queue per active encoder, so
memory peak is O(queue depth) rather than O(clip length). This matters at
full camera resolution, where a 60s clip × ~24 fps × ~6 MB/frame × 2 buffers
(annotated + clean) would otherwise grow the resident set to ~17 GB before
the encoder finalizes.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import imageio
import numpy as np

logger = logging.getLogger(__name__)


class _ClipEncoder:
    """Background ffmpeg writer pulling frames from a bounded queue.

    The thread is started immediately on construction; callers push frames
    with ``submit()`` and signal end-of-stream with ``close()``. If the
    encoder can't keep up with capture, ``submit()`` drops the frame rather
    than blocking the capture loop — stalled capture is far worse than a
    handful of dropped frames at the back of a clip.
    """

    def __init__(self, path: str, fps: float, high_quality: bool,
                 max_queue: int) -> None:
        self._path = path
        self._fps = fps
        self._high_quality = high_quality
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, max_queue))
        self._closed = threading.Event()
        self._dropped = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def path(self) -> str:
        return self._path

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    def submit(self, frame: np.ndarray) -> None:
        """Non-blocking enqueue; drops the frame if the queue is full."""
        if self._closed.is_set():
            return
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            self._dropped += 1
            # Log only at thresholds to avoid spamming when the encoder
            # is sustained-behind.
            if self._dropped in (1, 10, 100) or self._dropped % 1000 == 0:
                logger.warning(
                    "Encoder backpressure on %s: %d frame(s) dropped",
                    self._path, self._dropped,
                )

    def close(self) -> None:
        """Signal end-of-stream so the writer thread can drain and finalize.

        Non-blocking: setting the closed flag is enough — the writer polls
        the queue with a timeout and exits once the flag is set and the queue
        has drained.
        """
        if self._closed.is_set():
            return
        self._closed.set()
        # Nudge the writer in case it's waiting on an empty queue. Best-effort;
        # if the queue is full we don't care — the polled flag check handles it.
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def join(self, timeout: float = 15.0) -> None:
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning(
                "Encoder thread for %s did not finish within %.1fs",
                self._path, timeout,
            )

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        # CRF 18 = visually lossless for clean archival clips
        # CRF 28 = smaller file size for annotated clips
        crf = "18" if self._high_quality else "28"
        writer = imageio.get_writer(
            self._path, fps=self._fps,
            codec="libx264",
            output_params=["-crf", crf, "-preset", "ultrafast", "-pix_fmt", "yuv420p"],
        )
        n_written = 0
        try:
            while True:
                try:
                    frame = self._queue.get(timeout=0.5)
                except queue.Empty:
                    if self._closed.is_set():
                        break
                    continue
                if frame is None:
                    break
                if len(frame.shape) == 2:
                    frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
                # imageio expects RGB; OpenCV uses BGR
                writer.append_data(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                n_written += 1
            if self._dropped:
                logger.warning(
                    "Saved clip: %s (%d frames written, %d dropped due to "
                    "encoder backpressure)",
                    self._path, n_written, self._dropped,
                )
            else:
                logger.info("Saved clip: %s (%d frames)", self._path, n_written)
        except Exception:
            logger.exception("Error writing clip: %s", self._path)
        finally:
            try:
                writer.close()
            except Exception:
                pass
            try:
                out = Path(self._path)
                if out.exists() and out.stat().st_size == 0:
                    out.unlink()
                    logger.warning("Removed empty clip: %s", self._path)
            except OSError:
                pass


class ClipWriter:
    """Maintains a rolling frame buffer and streams MP4 clips on demand."""

    def __init__(self, clip_dir: str, pre_buffer_seconds: float = 3.0,
                 post_buffer_seconds: float = 5.0, fps: float = 30.0,
                 full_resolution: bool = False,
                 max_clip_seconds: float = 60.0):
        self._clip_dir = Path(clip_dir)
        self._clip_dir.mkdir(parents=True, exist_ok=True)
        self._pre_buffer_seconds = pre_buffer_seconds
        self._post_buffer_seconds = post_buffer_seconds
        self._fps = fps
        self._full_resolution = full_resolution
        # Hard ceiling on a single clip's duration. Without this, a noisy sky
        # can re-trigger detections every frame, pushing _record_end_deadline
        # forward forever.
        self._max_clip_seconds = max_clip_seconds

        # Rolling pre-buffer
        max_pre_frames = int(pre_buffer_seconds * fps) + 1
        self._buffer: deque[np.ndarray] = deque(maxlen=max_pre_frames)
        self._buffer_raw: deque[np.ndarray] = deque(maxlen=max_pre_frames)
        self._buffer_clean: deque[np.ndarray] = deque(maxlen=max_pre_frames)

        # Recording state
        self._recording = False
        self._record_start_time: float = 0.0
        self._record_end_deadline: float = 0.0
        self._current_clip_path: str | None = None
        # One encoder for the annotated clip + (optionally) one for the clean
        # full-resolution archival clip. Closed when recording finalizes.
        self._active_encoders: list[_ClipEncoder] = []
        # Encoders whose recordings have ended but which may still be
        # draining frames to disk. flush() waits for these to finish.
        self._draining_encoders: list[_ClipEncoder] = []

        self._lock = threading.Lock()

    @property
    def fps(self) -> float:
        return self._fps

    @fps.setter
    def fps(self, value: float) -> None:
        self._fps = max(1.0, value)
        max_pre_frames = int(self._pre_buffer_seconds * self._fps) + 1
        with self._lock:
            self._buffer = deque(self._buffer, maxlen=max_pre_frames)
            self._buffer_raw = deque(self._buffer_raw, maxlen=max_pre_frames)
            self._buffer_clean = deque(self._buffer_clean, maxlen=max_pre_frames)

    def feed_frame(self, frame: np.ndarray,
                   raw_frame: np.ndarray | None = None,
                   clean_frame: np.ndarray | None = None) -> None:
        """Feed a frame to the rolling buffer. If recording, also forward it
        to the active encoder(s).
        """
        with self._lock:
            self._buffer.append(frame.copy())
            if raw_frame is not None and self._full_resolution:
                self._buffer_raw.append(raw_frame.copy())
            if clean_frame is not None:
                self._buffer_clean.append(clean_frame.copy())

            if self._recording:
                # Decide which frame goes to which encoder. The first encoder
                # in _active_encoders is the annotated/main clip; if a second
                # exists it's the clean archival clip.
                if self._full_resolution and raw_frame is not None:
                    main_payload = raw_frame.copy()
                else:
                    main_payload = frame.copy()
                clean_payload = clean_frame.copy() if clean_frame is not None else None

                payloads = [main_payload]
                if clean_payload is not None:
                    payloads.append(clean_payload)

                for enc, payload in zip(self._active_encoders, payloads):
                    enc.submit(payload)

                if time.monotonic() >= self._record_end_deadline:
                    self._finish_recording()

    def trigger_recording(self) -> str | None:
        """Start or extend a recording. Returns the clip path if a new clip is started."""
        with self._lock:
            now = time.monotonic()
            deadline = now + self._post_buffer_seconds

            if self._recording:
                # Extend the recording deadline, but never past the hard ceiling.
                hard_ceiling = self._record_start_time + self._max_clip_seconds
                self._record_end_deadline = min(deadline, hard_ceiling)
                return self._current_clip_path

            # Start new recording
            self._recording = True
            self._record_start_time = now
            self._record_end_deadline = min(
                deadline, now + self._max_clip_seconds
            )

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"clip_{timestamp}.mp4"
            self._current_clip_path = str(self._clip_dir / filename)

            # Queue must hold the entire pre-buffer plus some slack for the
            # capture loop to outrun the encoder briefly without dropping.
            pre_frames = int(self._pre_buffer_seconds * self._fps) + 1
            max_queue = pre_frames + 30

            # Annotated main clip
            main_encoder = _ClipEncoder(
                self._current_clip_path,
                self._fps, high_quality=False, max_queue=max_queue,
            )
            self._active_encoders = [main_encoder]

            # Optional clean archival clip — full camera resolution, CRF 18
            if self._buffer_clean:
                clean_path = self._current_clip_path.replace(".mp4", "_clean.mp4")
                clean_encoder = _ClipEncoder(
                    clean_path,
                    self._fps, high_quality=True, max_queue=max_queue,
                )
                self._active_encoders.append(clean_encoder)

            # Pre-buffer feed: drain the rolling buffers into the encoders
            # synchronously. Queue size is sized for this; if it overflows,
            # the encoder will drop tail frames with a warning rather than
            # block the capture thread.
            if self._full_resolution and self._buffer_raw:
                main_pre = list(self._buffer_raw)
            else:
                main_pre = list(self._buffer)
            for f in main_pre:
                main_encoder.submit(f)

            if len(self._active_encoders) > 1 and self._buffer_clean:
                clean_pre = list(self._buffer_clean)
                for f in clean_pre:
                    self._active_encoders[1].submit(f)

            logger.info("Started recording clip: %s", self._current_clip_path)
            return self._current_clip_path

    def _finish_recording(self) -> None:
        """Close active encoders and reset recording state.

        Must be called with self._lock held. Encoders continue draining their
        queues in their own threads; flush() waits on them.
        """
        real_duration = time.monotonic() - self._record_start_time
        logger.info(
            "Finalizing clip: %s (real=%.2fs, fps=%.1f)",
            self._current_clip_path, real_duration, self._fps,
        )

        for enc in self._active_encoders:
            enc.close()
            self._draining_encoders.append(enc)

        self._active_encoders = []
        self._recording = False
        self._current_clip_path = None

    def clear_buffers(self) -> None:
        """Drop all pre-buffer frames. Call on active→idle transition so the
        ~pre_buffer_seconds × fps full-res frames don't sit in memory.
        """
        with self._lock:
            self._buffer.clear()
            self._buffer_raw.clear()
            self._buffer_clean.clear()

    def flush(self, writer_timeout: float = 15.0) -> None:
        """Force-finish any in-progress recording and wait for encoders to finalize."""
        with self._lock:
            if self._recording:
                self._finish_recording()
            encoders = list(self._draining_encoders)
            self._draining_encoders = []

        for enc in encoders:
            enc.join(timeout=writer_timeout)
