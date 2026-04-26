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
