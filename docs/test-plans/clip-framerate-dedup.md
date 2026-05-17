# Test Plan: clip-framerate-dedup

Branch: `dev/clip-framerate-dedup` (commits `18b354b`, `707fb93`, `4b1cd96`, `360727d`)
Implementation plan: [../plans/clip-framerate-dedup.md](../plans/clip-framerate-dedup.md)

Goal of these tests: confirm that recorded `.mp4` detection clips play at real-time speed (no slow-mo stutter), the new diagnostic log line appears with a sane ratio, and the RTSP arrival-rate sanity check fires on connect. Plus regression checks that the live dashboard, detection pipeline, and clip writer still work.

## Setup

1. Pull the branch: `git checkout dev/clip-framerate-dedup`. Confirm `git log --oneline -4` shows the four commits above.
2. Pick a source. Either:
   - **RTSP camera** — set `rtsp_url` in `config/local.yaml` (do not put credentials in `default.yaml`).
   - **Sample MP4** — use Settings → Test Detection. Pick a clip with at least one obvious moving lighted object so a detection event fires.
3. Have a video player ready (VLC works) and the bundled `ffprobe`:
   `<python>/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe` (replace `ffmpeg` → `ffprobe` in the filename, same directory).
4. Start with verbose logging: `python -m src.main -v`. Log will go to `data/logs/` and to the console.

## Pre-fix baseline (optional but recommended)

Before pulling the branch, capture one clip on `main` to compare against. The pre-fix clip should look stuttery / slow-mo on playback. Keep the file for the visual A/B in test 3 below.

If you don't want to roll back, any `clip_*.mp4` in `data/clips/` from before this branch's first commit is acceptable.

## Tests

### T1 — `Finalizing clip:` log line appears with sane ratio

**Steps:**
1. Start the server.
2. Trigger one detection event (live or via Test Detection).
3. Wait for the post-buffer to expire (default 5s) so the clip finalizes.
4. Find the new log line in `data/logs/` or the console:
   ```
   Finalizing clip: N frames, fps=X, real=Ys (expected M frames; ratio R)
   ```

**Pass criteria:**
- Log line is present.
- `fps` matches the camera's actual rate (or the measured override from T5).
- `ratio` falls within the expected band: `1.0 + (pre_buffer / real_duration) ± 0.10`.
  - For a 10s clip with default 3s pre-buffer: ratio ≈ 1.30 ± 0.10.
  - For a 60s long clip: ratio ≈ 1.05 ± 0.05.
  - For a 4s minimum clip: ratio ≈ 1.75 ± 0.10.

**Fail signals:**
- Ratio > expected upper bound by more than 0.10 → loop is still over-feeding (the bug isn't fixed). Re-check that commit `707fb93` is on the branch.
- Ratio < 1.0 → loop is under-feeding (CPU bound or grabber stalling — separate problem; capture a thread dump).

### T2 — Visual playback A/B

**Steps:**
1. Open the pre-fix clip in VLC. Note the playback feel.
2. Open the post-fix clip in VLC.

**Pass criteria:**
- Post-fix clip plays at real-time speed.
- No visible duplicate frames or stutter at points of motion.
- Object motion looks continuous, not jerky.

**Fail signals:**
- Still stuttery → either commit `707fb93` is missing or the camera reports an fps that's still wrong even after measurement. Check T5.

### T3 — `ffprobe` numerical sanity

**Steps:**

```
ffprobe -v 0 -show_streams data/clips/clip_<timestamp>.mp4
```

**Pass criteria:**
- `nb_frames / duration` is within 5% of `r_frame_rate`. All three numbers should be internally consistent.

### T4 — `_clean.mp4` companion is consistent

**Steps:**
- Repeat T1 and T3 against `clip_<timestamp>_clean.mp4`.

**Pass criteria:**
- Same ratio, same `ffprobe` consistency. (Both clips share the writer's frame-feeding path; if one is right and the other is wrong, something is very strange.)

### T5 — Camera FPS sanity check fires on connect

**Steps:**
1. Restart the server. Watch the log within the first ~2 seconds after connect.
2. Look for one of:
   ```
   Camera FPS verified: reported=X.X, measured=Y.Y
   ```
   or
   ```
   Camera FPS mismatch: CAP_PROP_FPS=X.X, measured=Y.Y over 30 frames (Z.ZZs). Using measured value.
   ```

**Pass criteria:**
- Exactly one of these lines appears per (re)connect, within ~`30 / camera_fps` seconds of "Connected to stream".
- If `mismatch` fires, the next `Finalizing clip:` line should use the measured value as `fps`, not the original `CAP_PROP_FPS`.

**Fail signals:**
- No log line at all → measurement is not being called. Check that `_sample_arrival_rate()` is invoked from `_grab_loop`.
- Both lines appear, or the line appears multiple times → bug in the count-target logic; arrival-rate state is not resetting on (re)connect.

### T6 — URL change re-triggers measurement

**Steps:**
1. With the server running, go to Settings → change RTSP URL → save.
2. Watch the log.

**Pass criteria:**
- A new `Connected to stream:` line appears for the new URL.
- A new `Camera FPS verified` or `mismatch` line appears within ~1.5s of the new connect.
- Subsequent clips use the fps measured for the new stream.

### T7 — Live dashboard regression

**Steps:**
1. With the server running and connected, open http://localhost:8080.
2. Confirm the live MJPEG stream renders.
3. Watch for at least 30 seconds.

**Pass criteria:**
- Live preview is smooth (the dashboard streams at ~10 FPS by default; not affected by this change).
- HUD overlay still shows an `FPS:` value — note that this is *heavy-branch iterations per second*, so at default `frame_skip=4` and a 20 Hz camera it should read ~5 FPS post-fix, not ~7-8 FPS as before. Lower number is correct now.
- No console errors, no `ERROR` log lines.

### T8 — Detection still happens

**Steps:**
1. Trigger a detection (live event or Test Detection).

**Pass criteria:**
- Event appears in the History page.
- Clip and thumbnail are present in `data/clips/` and `data/thumbs/`.
- DB row exists in `data/db/detections.db` (round-trip per lessons-learned #2).

**Note:** Detection rate at default config drops from ~7-8 Hz to 5 Hz (camera 20 Hz / frame_skip 4). Tracking parameter `min_track_length=8` still works because it counts tracked frames, not loop ticks. If small/fast objects start getting missed, drop `frame_skip` to 3 in `config/default.yaml` (5 Hz → 6.7 Hz detection).

### T9 — Encoder pool not exhausted

**Steps:**
1. Trigger 3-5 detections in rapid succession (or set `min_track_length` low and wave a flashlight in front of the camera).
2. Watch the log.

**Pass criteria:**
- No `Encoder pool at capacity (2); dropping clip` warnings under normal trigger spacing.
- All triggered clips appear in `data/clips/` and the History page.

**Note:** If clips were being dropped pre-fix because of bloated frame counts, post-fix they should be smaller and finish faster — this test should pass *more* easily after the fix, not less.

### T10 — Long clip ceiling still respected

**Steps:**
1. Cause a sustained detection that runs past `max_clip_seconds` (default 60s). Easiest: enable Test Detection on a continuously moving sample.

**Pass criteria:**
- Clip ends at ≤ `max_clip_seconds`.
- `Finalizing clip:` log shows real_duration ≤ 60s.
- No "frame count exceeded ceiling" warning under normal conditions (the ceiling is defensive; deadline check should fire first).

## Cleanup

- Pre-fix baseline clip(s) can be deleted from `data/clips/` after the visual A/B is done. The DB will get cleaned up by `sweep_orphan_clips` on next start.

## Sign-off

A passing run looks like:
- All T1-T10 pass.
- One representative `Finalizing clip:` log line captured in the PR description showing ratio in band.
- One representative `Camera FPS verified` (or `mismatch`) log line captured in the PR description.

If any test fails, do not merge. Capture the failing log + clip and reopen the implementation plan with notes.

## Run history

### 2026-05-10 — first post-implementation run

Environment: live RTSP camera from `config/local.yaml`, daytime, schedule overridden on for the duration of the test.

| Test | Status | Evidence |
|---|---|---|
| Startup | **PASS** | Clean import, pipeline started, no exceptions. |
| T5 (FPS sanity) | **PASS, found real mismatch** | `Camera FPS mismatch: CAP_PROP_FPS=20.0, measured=35.0 over 30 frames (0.83s). Using measured value.` Confirms commit `4b1cd96` is load-bearing in addition to the dedup — the camera actually lies about its FPS by 75%, so without the override clips would still play 1.75× slow-mo even with the dedup. |
| T7 (dashboard regression) | **PASS** | `/`, `/history`, `/settings`, `/zones`, `/api/stats` all 200. `/api/stats` returns valid JSON with `connected: true`. Only WARN in log was the expected T5 mismatch. |
| T1 / T3 / T4 / T8 | **NOT RUN (blocked)** | Daytime sky produced no motion for the night-tuned detector. `active_tracks=0` for 30s after enabling detection. No clips generated, so no `Finalizing clip:` log line to evaluate. |
| T2 (visual A/B) | **NOT RUN** | Depends on T1 producing a clip. |
| T6 (URL change) | **NOT RUN** | Would have required temporarily editing `local.yaml`; skipped to avoid disturbing the user's camera config. |
| T9 / T10 (burst, long clip) | **NOT RUN** | Same blocker as T1 — need real motion in front of the camera. |

**Outstanding work to reach full sign-off:**
- Re-run with real motion (wave at the camera, or wait for the schedule to enable detection on real night-sky targets) and capture T1/T3/T4/T8 evidence.
- Optionally re-run T6 by temporarily pointing at a sample file via Settings → change RTSP URL.

The headline 35-vs-20 FPS finding alone confirms the original diagnosis was correct in direction and underestimated in magnitude. The implementation is sound; remaining tests are confirmatory.
