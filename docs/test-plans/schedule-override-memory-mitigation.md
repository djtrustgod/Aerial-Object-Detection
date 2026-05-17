# Test Plan: schedule-override-memory-mitigation

Branch: `dev/schedule-override-memory-mitigation`
Implementation plan: [../plans/schedule-override-memory-mitigation.md](../plans/schedule-override-memory-mitigation.md)

Goal: confirm that (1) the dashboard detection toggle auto-yields to the schedule at the next window boundary, and (2) recording memory peaks stay bounded under sustained re-triggers at full camera resolution.

## Setup

1. `git checkout dev/schedule-override-memory-mitigation`
2. `pip install -e .` (per the project's reinstall reminder)
3. Pick a source — RTSP camera or a sample MP4 via Settings → Test Detection. The clip writer is only exercised meaningfully against a live source that produces detections.
4. Have PowerShell open in a second window for memory sampling.

## T1 — Force-ON outside the window auto-clears at the next boundary

**Setup:** Edit `config/local.yaml` to use a short test window. Pick start/end times such that **the test starts before `start_time`**. Example: if it's 10:00 now, set:
```yaml
schedule:
  enabled: true
  start_time: "10:05"
  end_time: "10:10"
```

**Steps:**
1. Start the server: `python -m src.main -v`.
2. Before 10:05 (outside window): `curl -s http://localhost:8080/api/stats | python -m json.tool` → expect `"detection_active": false`.
3. Click the dashboard "Enable Detection" toggle ON (or `POST /api/detection/toggle '{"enabled": true}'`).
4. Confirm `detection_active=true` and that detections / clips are being written (check `data/logs/aerial_detect.log` for `Started recording clip`).
5. Wait until 10:05. Watch the log for: `Schedule override cleared (schedule transitioned)`. `detection_active` should stay `true` because the window is now open.
6. Wait until 10:10. `detection_active` should flip to `false` cleanly via the schedule — *no* second "override cleared" log line, because the override is already gone.

**Pass criteria:**
- "override cleared" log appears exactly once, at the 10:05 transition.
- After 10:10, detection stops without any further toggle interaction.

**Fail signals:**
- Detection continues after 10:10 → override didn't clear; check that `_should_detect()` is being called in both `stats` and `_process_loop`.
- "override cleared" never appears → either `_is_in_schedule()` returned the same value across the transition (clock issue) or the baseline capture didn't happen.

## T2 — Force-OFF inside the window auto-clears at end of window

**Setup:** Same `local.yaml` window. Start the server just *before* `start_time` so detection is naturally off.

**Steps:**
1. At 10:05 confirm `detection_active=true` (schedule-driven).
2. Toggle detection OFF via the dashboard.
3. Confirm `detection_active=false` and that clips stop being written.
4. Wait for 10:10. Watch for: `Schedule override cleared (schedule transitioned)`. `detection_active` stays `false` because the window just closed anyway.
5. Wait for the *next* `start_time` (edit `local.yaml` to extend it if needed, then restart). Confirm detection resumes via the schedule without any further toggle clicks.

**Pass criteria:**
- After force-OFF, the window's remaining time is honored as OFF.
- At the next window transition the override is gone; the next scheduled ON is taken without intervention.

## T3 — Memory under sustained recording stays bounded

**Setup:** Live RTSP source that produces frequent detections (or run during dusk/dawn with a busy sky). `clip_full_resolution: true` (the default). Start the server with detection enabled and the schedule open (or set `schedule.enabled: false` so the schedule doesn't get in the way).

**Steps:**
1. Identify the python PID: `Get-NetTCPConnection -LocalPort 8080 | Select-Object OwningProcess` → use that PID.
2. Sample memory every 2 seconds for 5 minutes:
   ```powershell
   for ($i=0; $i -lt 150; $i++) {
     $p = Get-Process -Id <PID>
     "{0}s: WS={1:N0} MB, Priv={2:N0} MB, Tracks={3}" -f ($i*2), ($p.WorkingSet64/1MB), ($p.PrivateMemorySize64/1MB), ((Invoke-RestMethod http://localhost:8080/api/stats).active_tracks)
     Start-Sleep -Seconds 2
   }
   ```
3. While sampling, monitor `data/logs/aerial_detect.log` for `Started recording clip` and `Finalizing clip` lines.

**Pass criteria:**
- `WorkingSet` stays under ~1.5 GB across the full sample window even during multiple overlapping recordings. (Pre-fix baseline was 3–8 GB with visible sawtooth.)
- No `Encoder backpressure on ...: N frame(s) dropped` warnings under normal CPU load. (Occasional drops at high concurrency are acceptable but should be < 1% of frames.)
- Clips on disk play back correctly in VLC and frame counts roughly match the logged `Finalizing clip: real=Ys, fps=X` line (allow ±10%).

**Fail signals:**
- WorkingSet climbs above 2 GB with the sawtooth pattern → check that `_record_frames_*` lists really were removed in `clip_writer.py` and that `encoder.submit()` is being called instead of any `list.append`.
- Persistent backpressure warnings → x264 is not keeping up. Check CPU; consider lowering `clip_full_resolution` until investigated.
- Clip files truncated or 0 bytes → check that `flush()` runs `join()` on draining encoders.

## T4 — Idle cleanup drops the pre-buffer

**Steps:**
1. With detection actively recording, note `WorkingSet` (say ~1.2 GB).
2. Toggle detection OFF.
3. Within 1–2 seconds, the active→idle transition should fire `flush()` + `clear_buffers()`.
4. Re-sample memory.

**Pass criteria:**
- Resident memory drops back toward the steady-state baseline (~1 GB or less) within a few seconds. Pre-fix, it stayed elevated at ~3 GB because the pre-buffer deques retained stale full-res frames.

## T5 — Regression: existing flow still works

1. The Test Detection upload page (`Settings → Test Detection`) still produces clips correctly.
2. The history page lists newly recorded events with thumbnails.
3. The dashboard live stream keeps running through the active→idle transition.
4. `python -m pytest tests/ -v` → all 47 tests pass.
