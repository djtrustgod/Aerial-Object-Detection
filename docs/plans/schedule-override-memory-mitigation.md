# Schedule Override + Recording Memory Mitigation

Branch: `dev/schedule-override-memory-mitigation`

## Symptoms

Two issues surfaced on 2026-05-17 while observing the running server:

1. **Sticky schedule override.** The dashboard "detection" toggle was meant to be a temporary override of the configured schedule (e.g. 21:00–05:00). In practice, clicking ON set both `_detection_enabled=True` and `_schedule_override=True` and the override never cleared, so the schedule was bypassed forever (until OFF or a restart). The system had been detecting and writing clips all morning despite being well outside the configured window.

2. **Full-resolution recording memory peaks.** Resident memory oscillated between ~3 GB and ~8 GB during active recording. With `clip_full_resolution: true`, every captured frame during a recording was `.copy()`'d into two unbounded Python lists (`_record_frames_raw` *and* `_record_frames_clean`) until the clip finalized. At 1080p × 60s × ~24 fps × 2 buffers, that's ~17 GB held in RAM at peak. The pre-buffer deques additionally held ~1 GB of stale frames during idle periods because they were never cleared when detection went idle.

## Resolution

### Part A — Schedule override auto-clears at the next schedule transition

[src/pipeline.py](../../src/pipeline.py)

- Added `_override_baseline: bool | None` to the `Pipeline`. When `set_detection_enabled()` is called and the schedule is enabled, the current value of `_is_in_schedule()` is captured as the baseline and `_schedule_override` is set to True.
- New `_should_detect()` method replaces the inline gate. While the override is active, it returns the manual `_detection_enabled` value. As soon as `_is_in_schedule()` no longer matches the baseline (i.e. the schedule has crossed a boundary since the toggle), the override is cleared, the override-cleared event is logged, and control falls back to the schedule on subsequent ticks.
- `update_schedule_config()` now resets the override too, since the previously captured baseline becomes meaningless if the window changed.
- The `stats` dict and the `_process_loop` gate both call `_should_detect()` — single source of truth.

Resulting behavior:

| Action | Inside window | Outside window |
|---|---|---|
| Force ON | runs (override is a no-op until window ends, then clears) | runs until next 21:00 boundary, then schedule resumes |
| Force OFF | suppressed until window ends, then schedule resumes | suppressed (no-op until next 21:00 boundary, then clears) |

### Part B — Streaming clip writer + idle pre-buffer cleanup

[src/recording/clip_writer.py](../../src/recording/clip_writer.py), [src/pipeline.py](../../src/pipeline.py)

- Introduced `_ClipEncoder`: per-clip ffmpeg writer thread that pulls frames from a bounded `queue.Queue` and calls `imageio.get_writer().append_data()` as they arrive. `submit()` is non-blocking — on `queue.Full`, the frame is dropped and a rate-limited warning logged so the capture loop is never stalled. `close()` is also non-blocking; the writer polls the queue with a 0.5s timeout and exits when both the queue is empty and the closed flag is set.
- `ClipWriter` no longer accumulates `_record_frames_raw` / `_record_frames_clean` / `_record_frames` lists. On `trigger_recording()` it spawns one encoder for the annotated clip and (if the clean pre-buffer is non-empty) a second encoder for the full-resolution archival clip, drains the pre-buffers into them, and from that point `feed_frame()` calls `encoder.submit(frame.copy())`. The queue depth is sized as `pre_buffer_frames + 30` so pre-buffer fits with slack.
- `_finish_recording()` calls `enc.close()` on each active encoder and moves them to `_draining_encoders`; `flush()` joins those threads.
- Added `clear_buffers()` on `ClipWriter` and called it from `_process_loop` on the active→idle transition (right after `flush()`). Drops the ~540 MB-per-deque pre-buffer that was previously left to age in memory.
- Removed the obsolete `_record_frames*` lists, `_spawn_writer()` pool, `_write_clip()` static method, `_max_concurrent_encoders`, and the over-limit-count defensive ceiling in `feed_frame()` (the queue itself is the bound now).

### Clip quality is preserved

The clean archival clip remains full camera resolution at CRF 18, libx264, ultrafast, yuv420p — identical encoding parameters to before. Streaming changes only **when** frames reach the encoder, not what reaches it.

## Result

- Override behaves like "do what I mean": clicking ON outside the window runs detection until the next 21:00, then the schedule resumes without user intervention.
- In-flight recording memory is bounded by `(pre_buffer_frames + 30) × frame_size × 2 encoders`. At 1080p that's ~500 MB instead of ~17 GB.
- Idle memory drops to baseline within seconds of the active→idle transition because the pre-buffer is dropped.

## Tests

47/47 pass — including a new `tests/test_pipeline_schedule.py` (8 tests covering the override matrix above) and a rewritten `tests/test_clip_writer.py` (streaming pre-buffer, sustained-retrigger memory bound, backpressure dropping, zero-byte cleanup, and the new `clear_buffers()`).

## Verification runbook

See [../test-plans/schedule-override-memory-mitigation.md](../test-plans/schedule-override-memory-mitigation.md) for the live-RTSP runbook.
