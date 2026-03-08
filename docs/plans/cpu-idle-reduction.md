# Reduce CPU Usage When Detection is Inactive

## Context
The pipeline's `_process_loop()` currently does significant per-frame work even when detection is disabled (manually toggled off or outside the schedule window). This includes CLAHE + blur preprocessing, clip writer frame buffering, and full-resolution annotation — none of which serve any purpose when detection can't trigger. The Docker container shows ~20% CPU usage at idle. Goal: cut idle CPU by 60-70%.

## Analysis: What's Expensive

When detection is OFF, every processed frame (every 4th at default `frame_skip=4`) still runs:
1. `preprocessor.process()` — CLAHE + blur (only used as input to detector)
2. `_annotate_fullres()` — copies + annotates full-res frame (only used for clip recording)
3. `clip_writer.feed_frame()` — copies 2 frames into rolling buffer (recording can never trigger)
4. `tracker.update([])` — empty update to age out stale tracks

And every skipped frame (3 out of 4) still runs resize + timestamp + fullres annotate + clip feed.

**Only thing actually needed when idle:** resize frame for display + draw HUD overlay for the dashboard live stream.

## Plan: Single File Change

**File:** `src/pipeline.py` — `_process_loop()` (lines 220-304)

### Change 1: Move `in_schedule` computation before frame-skip logic
Compute the detection state once per iteration right after getting the frame, so all subsequent branching can use it.

### Change 2: Skip all work on skipped frames when detection is OFF
Currently skipped frames still resize, timestamp, annotate full-res, and feed clip writer. When idle, skip all of this — just `time.sleep(0.001)` and continue. The display frame only updates on processed frames anyway.

### Change 3: Double the effective frame skip when detection is OFF
`effective_skip = frame_skip if in_schedule else frame_skip * 2`
With default `frame_skip=4` and 20 FPS stream: active = 5 FPS processing, idle = 2.5 FPS display updates. Still smooth for a live preview.

### Change 4: Skip CLAHE/blur preprocessing when detection is OFF
Only call `preprocessor.resize_only()` (for display). Skip `preprocessor.process()` entirely — the gray frame is only consumed by the detector.

### Change 5: Split into active/idle code paths after resize
**Active path** (detection ON): full pipeline — preprocess, detect, track, record, annotate full-res, feed clip writer.
**Idle path** (detection OFF): just `_draw_overlays(display, {})` for the HUD. No tracking, no clip feeding, no full-res work.

### Change 6: Flush clip writer on active→idle transition
Track `was_active` bool. When transitioning from active to idle, call `self._clip_writer.flush()` to cleanly finish any in-progress recording that would otherwise hang (since `feed_frame()` stops being called).

## Resulting Loop Structure (pseudocode)

```python
was_active = False

while self._running:
    frame, frame_num = self._grabber.get_frame()
    if frame is None: sleep(0.01); continue

    in_schedule = self._detection_enabled and (self._schedule_override or self._is_in_schedule())

    # Flush clip writer on active → idle transition
    if was_active and not in_schedule:
        self._clip_writer.flush()
    was_active = in_schedule

    # Frame skip (doubled when idle)
    skip_counter += 1
    effective_skip = frame_skip if in_schedule else frame_skip * 2
    if skip_counter % effective_skip != 0:
        if in_schedule:
            display = resize_only(frame)
            stamp_timestamp(display)
            raw = annotate_fullres(frame, {})
            clip_writer.feed_frame(display, raw)
        else:
            time.sleep(0.001)  # yield CPU
        continue

    # Processed frame
    display = resize_only(frame)

    if in_schedule:
        gray = preprocessor.process(frame)  # CLAHE + blur
        detections = detector.detect(gray, ...)
        tracks = tracker.update(detections)
        # ... recording logic ...
        annotated = draw_overlays(display, tracks)
        raw = annotate_fullres(frame, tracks)
        clip_writer.feed_frame(annotated, raw)
    else:
        self._active_tracks = 0
        annotated = draw_overlays(display, {})  # HUD only

    # Update display frame (unchanged)
    with self._display_lock:
        if self._url_version == url_ver:
            self._display_frame = annotated

    # FPS calculation (unchanged)
```

## Expected CPU Savings (Detection OFF)

| Operation | Before | After | Savings |
|---|---|---|---|
| CLAHE + blur | Every 4th frame | Never | 100% |
| Resize on skipped frames | 3/4 frames | Never | 100% |
| `_annotate_fullres()` | Every frame | Never | 100% |
| `clip_writer.feed_frame()` | Every frame | Never | 100% |
| `_draw_overlays()` | Every 4th frame | Every 8th frame | 50% |
| Resize on processed frames | Every 4th frame | Every 8th frame | 50% |

**Estimated total: ~60-70% reduction in pipeline thread CPU when idle.**

## Edge Cases
- **Toggle ON mid-loop:** `in_schedule` recomputed every iteration — activates immediately. MOG2 needs a few frames to stabilize (acceptable, already documented).
- **In-progress recording when toggled OFF:** `flush()` call cleanly finishes it.
- **Tracker state:** Stale tracks not aged out during idle. First `tracker.update()` on reactivation handles this naturally.

## Verification
1. Run with detection OFF → confirm CPU drops significantly via `docker stats` or dashboard CPU meter
2. Toggle detection ON → confirm detection resumes normally within a few frames
3. Confirm dashboard live stream stays smooth at ~2.5 FPS when idle
4. Run existing tests: `python -m pytest tests/ -v`
