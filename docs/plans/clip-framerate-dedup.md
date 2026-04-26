# Fix low-framerate detection clips

**Status:** proposed, not yet implemented (drafted on branch `dev/clip-framerate-dedup`)

## Symptoms

Recorded `.mp4` detection clips look "low framerate" — visibly stuttery, with what appears to be duplicate frames between distinct moments of motion. Live dashboard playback at 10 FPS looks fine; the issue is in the saved clips themselves.

## Root cause (verified against source on `main` post-PR-#2)

The clip writer is being **over-fed** because the process loop is not paced to the camera's frame arrival.

1. **Grabber updates `_frame` at camera rate.**
   [src/capture/stream.py:115-156](../../src/capture/stream.py#L115-L156) — `cap.grab()` blocks until the next network frame arrives, so `self._frame` is updated at whatever the camera is actually delivering (~20 Hz for the typical RTSP source here).

2. **Process loop has no rate limit.**
   [src/pipeline.py:250-336](../../src/pipeline.py#L250-L336) — every iteration calls `self._grabber.get_frame()`, which returns the *latest* frame regardless of whether it's new. There is no `frame_num` guard.

3. **Clip writer is fed once per loop iteration, not once per camera frame.**
   `_clip_writer.feed_frame(...)` is called from both the skip branch ([src/pipeline.py:283](../../src/pipeline.py#L283)) and the heavy branch ([src/pipeline.py:316](../../src/pipeline.py#L316)) when `in_schedule = True` — every iteration, regardless of frame freshness.

4. **MP4 is tagged with the camera's nominal FPS, not the actual feed rate.**
   [src/pipeline.py:263](../../src/pipeline.py#L263) sets `clip_writer.fps = grabber.fps` once at startup. `grabber.fps` is `cv2.CAP_PROP_FPS`, which reflects what the camera *advertises*, not what the process loop is feeding.
   The writer uses that same value when invoking ffmpeg ([src/recording/clip_writer.py:198](../../src/recording/clip_writer.py#L198)).

If the loop iterates at, say, ~30 Hz on average (most iterations are "light" — 3 of every 4 with `frame_skip=4` skip the heavy detect/track work), each second of capture sends ~30 frames into the writer. The MP4 is tagged `fps=20`. So `30 frames / 20 fps = 1.5 seconds of MP4 playback` for `1 second of real time` → **slow-motion clip with visible duplicate frames**, which reads as "low framerate" to a viewer.

## Why CPU is *not* the bottleneck (resolving the original mis-diagnosis)

Initial hypothesis was the opposite — the loop running *slower* than the camera and therefore feeding fewer frames per second than the writer's tag claimed. That was wrong on this machine: total CPU is well under saturation when the system is running.

Two reasons that mis-led the initial reading:

- The HUD's `FPS:` overlay ([src/pipeline.py:331](../../src/pipeline.py#L331)) only counts iterations that go through the heavy branch — i.e. it reads `loop_rate / frame_skip`, understating the true loop rate by 4×. A HUD reading of "5 FPS" at default config means the loop is ticking at ~20 Hz; "8 FPS" means ~32 Hz (over-feeding).
- "Total CPU low" doesn't preclude single-thread saturation, so the CPU theory wasn't formally disproven, just made unlikely. The over-feeding theory predicts the *exact* visual symptom (slow-mo with duplicate frames) without requiring any saturation.

A secondary suspect, not the primary cause but worth flagging: `cv2.CAP_PROP_FPS` is unreliable on RTSP. Some cameras report a hardcoded `25` or `30` regardless of actual delivery. Even after the dedup fix, if the camera tag is wrong the MP4 timing will still drift.

## Plan

### 1. Frame de-duplication in the process loop (the fix)

Track the last seen `frame_num`. Skip the iteration if the grabber hasn't produced a new frame yet. This is the single load-bearing change.

In [src/pipeline.py](../../src/pipeline.py), inside `_process_loop`:

```python
def _process_loop(self) -> None:
    frame_skip = self._config.processing.frame_skip
    skip_counter = 0
    fps_timer = time.monotonic()
    fps_frame_count = 0
    was_active = False
    fps_set = False
    last_frame_num = -1                                  # NEW

    while self._running:
        try:
            url_ver = self._url_version
            frame, frame_num = self._grabber.get_frame()
            if frame is None:
                ...                                       # unchanged
                continue

            if frame_num == last_frame_num:               # NEW: dedupe
                time.sleep(0.005)                         # short yield, ~200 Hz cap
                continue
            last_frame_num = frame_num
            ...                                           # rest of loop unchanged
```

**Net effect:**
- `feed_frame` called *exactly once per camera frame* — no duplicates in clips
- `frame_skip` semantics tighten: "process every Nth **camera** frame" (was: "every Nth iteration"), which is what the comment in `default.yaml` already implies
- Loop's CPU cost on duplicate frames goes away (small win, not the point)
- MP4s play at real-time speed because frames delivered match the fps tag

Detection rate at default settings becomes `camera_fps / frame_skip = 20 / 4 = 5 Hz`. Pre-dedup it was `loop_rate / frame_skip ≈ 7-8 Hz`. If the lower detection rate matters, drop `frame_skip` to 3 in `config/default.yaml`.

### 2. Diagnostic logging at clip finalize

In `_finish_recording` ([src/recording/clip_writer.py:142-166](../../src/recording/clip_writer.py#L142-L166)), log the frame count, tagged fps, and computed real duration so the ratio is visible going forward.

```python
real_duration = time.monotonic() - self._record_start_time
expected_frames = real_duration * fps
logger.info(
    "Finalizing clip: %d frames, fps=%.1f, real=%0.2fs (expected %.0f frames; ratio %.2f)",
    len(frames), fps, real_duration, expected_frames, len(frames) / max(1, expected_frames),
)
```

Interpretation:
- Ratio ≈ 1.0 → honest
- Ratio > 1 → over-feeding (the bug we're fixing)
- Ratio < 1 → CPU-bound (the original wrong hypothesis; would be a separate problem)

### 3. Camera-FPS sanity check at connect

In `FrameGrabber._connect` ([src/capture/stream.py:84-103](../../src/capture/stream.py#L84-L103)), after reading `CAP_PROP_FPS`, additionally measure arrival rate over the first ~30 frames (e.g. wall-time deltas in `_grab_loop`'s first 30 iterations) and log it alongside the camera-reported value. If they disagree by more than ~10%, log a warning and use the measured value as `self._fps` instead.

This is cheap insurance against the unreliable-`CAP_PROP_FPS` failure mode and is independent of the dedup fix.

## Verification

1. **Before the fix:** start the server with `python -m src.main -v` (or via the launcher), trigger a detection event (or use Settings → Test Detection with a sample MP4), let the clip finalize. Capture the new INFO log line — note the ratio.
2. **Apply the fix.** Restart. Trigger another event.
3. **Compare:** ratio should drop from `>1.0` toward `1.0`.
4. **Visual check:** play both clips back-to-back. Pre-fix should be visibly slow-mo with stutter; post-fix should match the live stream's pace.
5. **Optional `ffprobe` check** (the `imageio_ffmpeg` package bundles ffmpeg; the binary is at `<python>/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe`):
   ```
   ffprobe -v 0 -show_streams clip.mp4 | grep -E "nb_frames|duration|r_frame_rate"
   ```
   Confirm `nb_frames / duration ≈ r_frame_rate`.

## Out of scope (deliberately deferred)

- **`clip_full_resolution: true` (default in [config/default.yaml:45](../../config/default.yaml#L45)).** Each `feed_frame` copies a 1080p frame into a deque AND, when recording, appends another copy to a list. That's the dominant per-frame cost in the loop. Flipping it to `false` would cut memory and per-frame work substantially but produces 640×360 clips — a UX/tradeoff decision, not a bug fix.
- **`imageio` writer settings.** `preset=ultrafast crf=28` for annotated clips, `crf=18` for `_clean.mp4`. Already efficient; not the bottleneck.
- **Irregular RTSP delivery.** The grabber-side measurement (step 3 above) will surface this if it's a factor, but addressing it would mean buffering and resampling — a bigger redesign than this plan covers.

## Suggested commit structure

Three small commits, each independently revertable:

1. **`Add diagnostic logging for clip frame-rate sanity check`** — just step 2 above. Land this first; observe a few clips' ratios; that gives evidence the fix is targeting the right problem.
2. **`Sync process loop to camera frame arrival (dedupe by frame_num)`** — step 1. The actual fix.
3. **`Detect mismatched RTSP camera FPS at connect`** — step 3. Independent insurance.

If you want one commit instead of three, that's fine too — the fix in step 1 is genuinely the load-bearing change, and steps 2 and 3 are diagnostics/insurance.

## Conversation context (in case useful for the next session)

This plan was drafted with a prior Claude session that initially hypothesized CPU-bound under-feeding. The user (machine-owner) correctly pushed back: *"why concern about CPU when this app uses a fraction of the machine's CPU?"* That reframing pointed the diagnosis at over-feeding (process loop faster than camera), which matches the symptom precisely and is what this plan fixes. The earlier wrong hypothesis is preserved in the section above ("Why CPU is *not* the bottleneck") so the next session understands why the fix isn't about CPU optimization despite the original framing.
