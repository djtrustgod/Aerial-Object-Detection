# Overnight OOM fix

## Symptoms

After running through the night, the server produced "out of memory" errors
and "could not start ffmpeg" errors. By morning it was effectively dead.

## Root cause (verified against source, not speculation)

The OOM originates inside the clip writer, not the launcher.

1. **Unbounded in-memory recording buffer.**
   `src/recording/clip_writer.py:41,79` — `_record_frames_raw` is a plain
   Python `list` with no max length. With `clip_full_resolution: true`
   (current default in `config/default.yaml:45`), each frame is a full-res
   `np.ndarray.copy()`, roughly 6 MB at 1080p.

2. **Recording deadline can be extended forever.**
   `src/recording/clip_writer.py:93-96` — every call to `trigger_recording()`
   during an active clip extends `_record_end_deadline` by
   `clip_post_buffer` seconds. Under the nightly schedule (21:00–05:00)
   and a noisy sky, detections fire continuously, so the deadline is
   pushed forward indefinitely, the clip never finalizes, and the frame
   list grows for hours. At 1080p × 30 fps that is ~180 MB of RAM per
   second of sustained triggering.

3. **Unbounded concurrent encoder threads.**
   `src/recording/clip_writer.py:144-149` — `_spawn_writer` creates a new
   daemon thread per clip with no concurrency cap. The list is only
   pruned when *new* writers are spawned. Once disk I/O or RAM get
   tight, encoder threads pile up, each holding its own frame list.

Collapse order: sustained triggers → `_record_frames_raw` grows without
bound → Python RAM footprint balloons → `imageio.get_writer(...)` (which
forks ffmpeg) fails → cascade of "could not start ffmpeg" errors.

## Plan

### Part 1 — Fix the clip writer bleed (priority)

Target file: `src/recording/clip_writer.py`

- **Hard ceiling on clip duration.** Introduce `MAX_CLIP_SECONDS` (e.g.
  60 s). In `trigger_recording()`, when extending an active recording,
  cap the new deadline at `_record_start_time + MAX_CLIP_SECONDS`.
  Once the ceiling is reached, finalize the current clip; subsequent
  triggers start a new one.
- **Defensive cap on frame-list length.** In `feed_frame()`, if
  `len(_record_frames_raw)` exceeds `MAX_CLIP_SECONDS * fps`, call
  `_finish_recording()` immediately. Belt-and-suspenders for the
  deadline check.
- **Cap concurrent encoder threads.** Add `MAX_CONCURRENT_ENCODERS`
  (e.g. 2). In `_spawn_writer`, count alive threads and block (or drop
  with a logged warning) if at capacity. Dropping is preferable to
  blocking `feed_frame`, because blocking the feed thread would stall
  capture.
- **Logging.** Log when the ceiling is hit and when an encoder is
  dropped, so we can see it in the morning.

Tests to add in `tests/test_clip_writer.py`:

- Feeding frames while continuously re-triggering does not grow
  `_record_frames_raw` past the ceiling.
- Spawning more than `MAX_CONCURRENT_ENCODERS` worth of clips drops (or
  queues) the extras and logs a warning.

### Part 2 — Docs

- `CHANGELOG.md` under `[Unreleased]`:
  - Fixed: overnight OOM caused by unbounded clip recording under
    sustained detections.

## Order of work

1. Land the clip writer caps + tests. This is the actual bug; ship it
   first so the next overnight run survives.
2. Docs update in the same turn as each code change (per global rule).

## Open questions

- What should `MAX_CLIP_SECONDS` be? 60 s feels right for an aerial
  detection use case — longer events are either clouds or something
  worth multiple clips. Confirm with user before hard-coding.
- Drop vs. queue over-limit encoders? Dropping is simpler and matches
  the "keep the live pipeline healthy" goal. Confirm.
- Should full-res clips be the default at all? If RAM is tight on the
  deployment hardware, flipping `clip_full_resolution: false` cuts
  frame size ~9×. Worth discussing separately from this fix.
