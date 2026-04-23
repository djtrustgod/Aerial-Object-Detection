"""SQLite event logger with WAL mode for concurrent reads."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from src.recording.models import DetectionEvent

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL,
    start_time REAL NOT NULL,
    end_time REAL NOT NULL,
    start_frame INTEGER NOT NULL,
    end_frame INTEGER NOT NULL,
    avg_x REAL NOT NULL,
    avg_y REAL NOT NULL,
    avg_speed REAL NOT NULL,
    trajectory_length INTEGER NOT NULL DEFAULT 0,
    travel_distance REAL NOT NULL DEFAULT 0.0,
    clip_path TEXT,
    thumbnail_path TEXT
);
"""

INSERT_SQL = """
INSERT INTO events (
    object_id, start_time, end_time,
    start_frame, end_frame, avg_x, avg_y, avg_speed,
    trajectory_length, travel_distance, clip_path, thumbnail_path
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""

SELECT_ALL_SQL = """
SELECT event_id, object_id, start_time,
       end_time, start_frame, end_frame, avg_x, avg_y, avg_speed,
       trajectory_length, travel_distance, clip_path, thumbnail_path
FROM events ORDER BY start_time DESC
"""

SELECT_RECENT_SQL = SELECT_ALL_SQL + " LIMIT ?"


class EventLogger:
    """Logs detection events to SQLite."""

    def __init__(self, db_path: str):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(CREATE_TABLE_SQL)
        self._conn.commit()

        # Migrate existing databases
        self._migrate()

        logger.info("Event logger initialized: %s", self._db_path)

    def _migrate(self) -> None:
        """Add columns missing from older schema versions."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(events)").fetchall()}
        if "travel_distance" not in cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN travel_distance REAL DEFAULT 0.0")
        if "thumbnail_path" not in cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN thumbnail_path TEXT")

        # Fix trajectory_length NOT NULL without default (from legacy schema).
        # Check if trajectory_length exists but has no default and NOT NULL.
        col_info = {r[1]: r for r in self._conn.execute("PRAGMA table_info(events)").fetchall()}
        if "trajectory_length" in col_info:
            # cid, name, type, notnull, dflt_value, pk
            notnull = col_info["trajectory_length"][3]
            dflt = col_info["trajectory_length"][4]
            if notnull and dflt is None:
                # Recreate table with default to fix the constraint
                logger.info("Migrating trajectory_length column to add DEFAULT 0")
                self._conn.executescript("""
                    CREATE TABLE events_new (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        object_id INTEGER NOT NULL,
                        start_time REAL NOT NULL,
                        end_time REAL NOT NULL,
                        start_frame INTEGER NOT NULL,
                        end_frame INTEGER NOT NULL,
                        avg_x REAL NOT NULL,
                        avg_y REAL NOT NULL,
                        avg_speed REAL NOT NULL,
                        trajectory_length INTEGER NOT NULL DEFAULT 0,
                        travel_distance REAL NOT NULL DEFAULT 0.0,
                        clip_path TEXT,
                        thumbnail_path TEXT
                    );
                    INSERT INTO events_new SELECT
                        event_id, object_id, start_time, end_time,
                        start_frame, end_frame, avg_x, avg_y, avg_speed,
                        trajectory_length, travel_distance, clip_path, thumbnail_path
                    FROM events;
                    DROP TABLE events;
                    ALTER TABLE events_new RENAME TO events;
                """)
        elif "trajectory_length" not in col_info:
            self._conn.execute(
                "ALTER TABLE events ADD COLUMN trajectory_length INTEGER NOT NULL DEFAULT 0"
            )

        self._conn.commit()

    def log_event(self, event: DetectionEvent) -> int:
        """Insert a detection event. Returns the event_id."""
        cursor = self._conn.execute(INSERT_SQL, (
            event.object_id,
            event.start_time,
            event.end_time,
            event.start_frame,
            event.end_frame,
            event.avg_x,
            event.avg_y,
            event.avg_speed,
            event.trajectory_length,
            event.travel_distance,
            event.clip_path,
            event.thumbnail_path,
        ))
        self._conn.commit()
        event_id = cursor.lastrowid
        logger.info("Logged event #%d (object_id=%d)", event_id, event.object_id)
        return event_id

    def get_recent(self, limit: int = 50) -> list[DetectionEvent]:
        """Get the most recent events."""
        cursor = self._conn.execute(SELECT_RECENT_SQL, (limit,))
        return [self._row_to_event(row) for row in cursor.fetchall()]

    def get_all(self) -> list[DetectionEvent]:
        """Get all events ordered by start_time descending."""
        cursor = self._conn.execute(SELECT_ALL_SQL)
        return [self._row_to_event(row) for row in cursor.fetchall()]

    def get_stats(self) -> dict:
        """Get summary statistics with hourly breakdown for the last 24 hours."""
        import time
        cursor = self._conn.execute("SELECT COUNT(*) FROM events")
        total = cursor.fetchone()[0]

        # Hourly event counts for the last 24 hours (covers overnight sessions)
        now = time.time()
        cutoff = now - 86400  # 24 hours ago
        hourly_map = {}
        cursor = self._conn.execute(
            "SELECT strftime('%Y-%m-%d %H', start_time, 'unixepoch', 'localtime') AS slot,"
            " COUNT(*) AS cnt"
            " FROM events"
            " WHERE start_time >= ?"
            " GROUP BY slot",
            (cutoff,),
        )
        for slot, cnt in cursor.fetchall():
            hourly_map[slot] = cnt

        return {"total": total, "hourly_map": hourly_map}

    def get_sessions(self, gap_seconds: int = 14400) -> list[dict]:
        """Get capture sessions inferred from event timestamp gaps.

        A new session starts when the gap between consecutive events
        exceeds gap_seconds (default 4 hours). Returns newest-first.
        """
        cursor = self._conn.execute(
            "SELECT event_id, start_time FROM events ORDER BY start_time ASC"
        )
        rows = cursor.fetchall()
        if not rows:
            return []

        sessions: list[dict] = []
        session_start = rows[0][1]
        session_end = rows[0][1]
        event_count = 1

        for i in range(1, len(rows)):
            ts = rows[i][1]
            if ts - session_end > gap_seconds:
                sessions.append({
                    "session_id": len(sessions) + 1,
                    "start_time": session_start,
                    "end_time": session_end,
                    "event_count": event_count,
                })
                session_start = ts
                event_count = 0
            session_end = ts
            event_count += 1

        # Final session
        sessions.append({
            "session_id": len(sessions) + 1,
            "start_time": session_start,
            "end_time": session_end,
            "event_count": event_count,
        })

        # Return newest-first
        sessions.reverse()
        # Re-number so session_id=1 is the newest
        for i, s in enumerate(sessions):
            s["session_id"] = i + 1

        return sessions

    def get_events_by_session(self, session_id: int, gap_seconds: int = 14400) -> list[DetectionEvent]:
        """Get events belonging to a specific session (1 = newest)."""
        sessions = self.get_sessions(gap_seconds)
        if not sessions or session_id < 1 or session_id > len(sessions):
            return []

        target = sessions[session_id - 1]
        start = target["start_time"]
        end = target["end_time"]

        # Fetch events within the session time range
        cursor = self._conn.execute(
            SELECT_ALL_SQL.replace("ORDER BY start_time DESC", "") +
            " WHERE start_time >= ? AND start_time <= ? ORDER BY start_time DESC",
            (start, end),
        )
        return [self._row_to_event(row) for row in cursor.fetchall()]

    def delete_by_ids(self, event_ids: list[int]) -> tuple[list[str], list[str]]:
        """Delete specific events by ID. Returns (clip_paths, thumb_paths) of deleted events."""
        if not event_ids:
            return [], []
        placeholders = ",".join("?" * len(event_ids))
        cursor = self._conn.execute(
            f"SELECT clip_path, thumbnail_path FROM events WHERE event_id IN ({placeholders})",
            event_ids,
        )
        rows = cursor.fetchall()
        clip_paths = [row[0] for row in rows if row[0]]
        thumb_paths = [row[1] for row in rows if row[1]]
        self._conn.execute(
            f"DELETE FROM events WHERE event_id IN ({placeholders})", event_ids
        )
        self._conn.commit()
        return clip_paths, thumb_paths

    def clear_all(self) -> tuple[int, list[str], list[str]]:
        """Delete all events. Returns (count, clip_paths, thumb_paths) of removed events."""
        cursor = self._conn.execute(
            "SELECT clip_path, thumbnail_path FROM events"
        )
        rows = cursor.fetchall()
        clip_paths = [row[0] for row in rows if row[0]]
        thumb_paths = [row[1] for row in rows if row[1]]
        del_cursor = self._conn.execute("DELETE FROM events")
        self._conn.commit()
        count = del_cursor.rowcount
        logger.info("Cleared %d events from history", count)
        return count, clip_paths, thumb_paths

    def delete_by_session(self, session_id: int, gap_seconds: int = 14400) -> tuple[int, list[str], list[str]]:
        """Delete all events in a session. Returns (count, clip_paths, thumb_paths)."""
        events = self.get_events_by_session(session_id, gap_seconds)
        if not events:
            return 0, [], []
        event_ids = [e.event_id for e in events if e.event_id is not None]
        clip_paths, thumb_paths = self.delete_by_ids(event_ids)
        return len(event_ids), clip_paths, thumb_paths

    def get_referenced_clip_names(self) -> set[str]:
        """Return filenames (basename only) referenced by any event's clip_path,
        plus the derived _clean companion for each annotated clip."""
        names: set[str] = set()
        for row in self._conn.execute("SELECT clip_path FROM events WHERE clip_path IS NOT NULL"):
            raw = row[0]
            # DB may store Windows-style backslashes or POSIX slashes
            name = raw.replace("\\", "/").rsplit("/", 1)[-1]
            names.add(name)
            if name.endswith(".mp4") and not name.endswith("_clean.mp4"):
                names.add(name.replace(".mp4", "_clean.mp4"))
        return names

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    @staticmethod
    def _row_to_event(row: tuple) -> DetectionEvent:
        return DetectionEvent(
            event_id=row[0],
            object_id=row[1],
            start_time=row[2],
            end_time=row[3],
            start_frame=row[4],
            end_frame=row[5],
            avg_x=row[6],
            avg_y=row[7],
            avg_speed=row[8],
            trajectory_length=row[9],
            travel_distance=row[10],
            clip_path=row[11],
            thumbnail_path=row[12],
        )


def sweep_orphan_clips(clip_dir: str | Path, event_logger: EventLogger) -> int:
    """Delete *.mp4 files in clip_dir not referenced by any event in the DB.

    Returns the number of files removed.
    """
    clip_dir = Path(clip_dir)
    if not clip_dir.exists():
        return 0

    referenced = event_logger.get_referenced_clip_names()
    removed = 0
    for fp in clip_dir.glob("*.mp4"):
        if fp.name not in referenced:
            try:
                fp.unlink()
                removed += 1
            except OSError:
                logger.exception("Failed to remove orphan clip: %s", fp)
    if removed:
        logger.info("Startup sweep removed %d orphan clip file(s) from %s", removed, clip_dir)
    return removed
