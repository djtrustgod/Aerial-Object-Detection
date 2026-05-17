# Lessons Learned

Hard-won lessons from bugs and near-misses in this project. Follow these during development and code review.

---

## 1. Verify the Live Database Schema Before Modifying DB Code

**Incident:** v0.4.0 — The `CREATE_TABLE_SQL` in code didn't include a `trajectory_length` column, but the actual database on disk had it as `NOT NULL` with no default. Every `INSERT` silently failed. Clips were saved to disk (independent of the DB) but zero History events were ever recorded. The bug went unnoticed because validation only checked that the server started and pages loaded.

**Rule:** Before any change that touches database read/write code:

1. Run `PRAGMA table_info(events)` on the **actual database file** and compare it to `CREATE_TABLE_SQL` and `INSERT_SQL` in code.
2. Look for: missing columns, `NOT NULL` constraints without defaults, column ordering mismatches between the schema and queries.
3. After writing changes, do a **live round-trip test**: INSERT a row, SELECT it back, then DELETE it. Don't just check that imports succeed.
4. When migrations exist, verify they handle the actual on-disk state, not just a theoretical upgrade path.

---

## 2. End-to-End Verification Before Declaring a Feature Complete

**Incident:** v0.4.0 — The detection History feature appeared to work (server started, dashboard loaded, clips were written to disk) but the critical DB write path was silently broken. A simple check — "are events actually appearing in the History page after a detection?" — would have caught it immediately.

**Rule:** Don't stop at "the server returned 200." Verify the full data flow:

1. For any feature that **writes data** (DB, files, config), verify the write actually persisted by reading it back through the same path the user would see it.
2. For pipeline features, trace the full path: detection -> event publish -> DB insert -> API response -> UI display. Check each hop.
3. Check that response bodies contain **expected data**, not just a success status code.
4. When a feature has been running for a while, spot-check that it's still producing the expected artifacts (DB rows, files, thumbnails) before moving on.

---

## 3. Don't Run PowerShell Scriptblocks on .NET Background Threads

**Incident:** v0.4.7 — The first cut of `tools/Launch-AerialDetect.ps1` registered PowerShell scriptblocks as `Process.OutputDataReceived` / `ErrorDataReceived` / `Exited` handlers. Those events fire on .NET thread-pool threads. Symptoms cascaded across three rewrites:

1. **Synchronous `$form.Invoke([Action]{...})`** from the handler — deadlock. The background thread held the PS runspace, then blocked waiting for the UI thread; the UI thread needed the runspace to execute the delegate. Window froze, looked like a crash.
2. **`$form.BeginInvoke([Action]{...})`** to break the deadlock — async fire meant `$line`/`$color` were null by the time the action ran. PS closures don't retain function-local variables across an async hop.
3. **`{...}.GetNewClosure()`** to snapshot the values — the GUI vanished on the first child-process write to stdout, with no managed exception, no crash log, no dialog. The PS host process exited outside .NET's exception system.

PowerShell 5.1's runspace is single-threaded and not designed to be re-entered from arbitrary .NET threads. Any path that lets the thread pool call PS code is an unbounded source of these failures.

**Rule:** When integrating PowerShell with .NET event sources that fire on background threads:

1. Do the bridging in **compiled C#** via `Add-Type`. Push to a `ConcurrentQueue` (or similar). Never let a PowerShell scriptblock be the event handler.
2. Drain the queue from the UI thread (a WinForms `Timer` is fine). All PS code stays on one thread.
3. If you must use a PS scriptblock as a delegate, prove it's only invoked synchronously on the runspace's own thread before relying on it.
4. When a hosted process exits without throwing — no dialog, no log, no exception — assume the failure happened in native code below the CLR. Look for cross-thread runtime re-entry, not for a missing `try/catch`.

---

## 4. Match Loop Rate to Source Rate Before Tagging Output

**Incident:** v0.4.7 — Recorded `.mp4` clips played back as visibly stuttery slow-motion. Initial diagnosis blamed CPU-bound under-feeding (loop running slower than the camera, fewer frames than the fps tag claimed). That was backwards: the loop was running *faster* than the camera, not slower, so it was reading the same `_frame` from the grabber multiple times and feeding it to the clip writer. Clips were tagged at the camera's nominal fps but contained duplicate frames — playback at 1.5× real time. Two things misled the early diagnosis:

- The HUD's `FPS:` overlay only counted heavy-branch iterations (skip-branch had a `continue` before the counter increment), so it read `loop_rate / frame_skip`. A reading of "5 FPS" at default `frame_skip=4` actually meant the loop was ticking at ~20 Hz; "8 FPS" meant ~32 Hz (over-feeding).
- "CPU usage is low" doesn't disprove CPU-bound; single-thread saturation is invisible at the total-CPU level. So the CPU theory wasn't formally disproven, just made unlikely. Nothing predicted the *exact* symptom (slow-mo with duplicates) the way over-feeding did.

**Rule:** When a producer-consumer system has independent rates, verify the rate match before tagging output:

1. Any consumer of an upstream stream (frame grabber, network socket, queue) must either consume only when the upstream advances (track a sequence number / version), or block until it does. Iterating "as fast as you can" reads the same item repeatedly when you outpace the producer.
2. Output tagged with a rate (fps in MP4, sample rate in audio, ticks/sec in metrics) must match the rate at which data was actually written to it — not the source's *advertised* rate, not the loop's tick rate. If the loop and producer are decoupled, log both rates side-by-side at startup and watch for drift.
3. Don't trust a HUD/metric that derives from a subset of iterations (e.g. only the heavy branch) without explicitly noting the skip factor in the label. A misreading 5×-off can flip the diagnosis. Either show the raw rate or label the derived one (e.g. "Detection FPS" vs "Loop FPS").
4. When debugging a slow-mo / fast-forward / stutter symptom, log `frames_written / (fps_tag * real_duration)`. Ratio > 1 = over-feeding (duplicates). Ratio < 1 = under-feeding (CPU or network bound). The number tells you which way the loop is racing the source.

**Postscript (verified 2026-05-10):** First post-fix run on the configured RTSP camera produced `Camera FPS mismatch: CAP_PROP_FPS=20.0, measured=35.0`. The camera underreports its own delivery rate by ~75%. That made the "and the camera tag may also be wrong" hedge from the plan into the *primary* effect on this hardware: even with perfect dedup, the writer would have been tagged at the camera's claimed 20 fps while fed at the real 35 fps, leaving the clips slow-mo. Two takeaways: (a) the arrival-rate sanity check was load-bearing, not just insurance — keep it; (b) for any future sensor-fed pipeline, assume the sensor's self-reported rate is a lie until measured, and budget for a measurement step on first connect.

---

## 5. Don't Accumulate Full-Resolution Frames in Python Lists — Stream to the Encoder

**Incident:** v0.5.0 — Live memory observation showed resident set oscillating between 3 GB and 8 GB with a clear sawtooth pattern tied to clip-recording cycles. Investigation found `ClipWriter` was appending `.copy()` of every captured frame to two unbounded Python lists (`_record_frames_raw` and `_record_frames_clean`) for the duration of each clip, then handing the entire lists to a background encoder thread at finalization. With `clip_full_resolution: true` and a 1080p stream, a single 60-second clip held ~17 GB of full-resolution frames in memory at peak. A noisy sky that re-triggered detections every frame would push toward the hard ceiling on every clip. The misleading thing was that the existing `max_clip_seconds × fps` "defensive ceiling" looked like a memory bound — it caps the *clip length*, but the memory bound is still `O(clip_length × frame_size × 2)`, which at 1080p is two orders of magnitude bigger than RAM.

Two separate things compounded the symptom:

- The pre-buffer deques (`_buffer_raw`, `_buffer_clean`) were bounded but never cleared on idle, so ~1 GB of stale full-resolution frames sat in memory across the daytime when the schedule said detection was off.
- A bug elsewhere (sticky `_schedule_override`) meant detection was actually running all day instead of just inside the configured window, which is why the symptom was observable at all — without the override bug, the memory bloat would only have shown up at night.

**Rule:** When a pipeline produces frames at one rate and consumes them at another (encoder, network, disk), don't bridge the gap with an unbounded list:

1. Stream from producer to consumer through a **bounded** queue. The queue depth is the memory bound — pick it deliberately (we used `pre_buffer_frames + 30`).
2. When the consumer can't keep up, drop at the tail with a rate-limited warning rather than blocking the producer or growing the buffer. A capture loop that stalls is worse than a clip with a few missing frames at the end.
3. The encoder/consumer thread should be started **when recording begins**, not at finalization. Streaming means the encoder is making progress the entire time the clip is being captured, not racing to catch up at the end.
4. Don't trust a length-bound (max_clip_seconds, max_packet_count) to bound memory. Frame size × concurrency multiplies it back up. Memory bounds must be expressed in bytes-equivalent units, not items.
5. Bounded buffers must be **cleared on state transitions**, not just bounded. A 90-frame deque is fine; a 90-frame deque that sits full of 6 MB frames for 14 hours of idle time is half a gigabyte of waste.
