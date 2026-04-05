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
