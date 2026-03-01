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
    trajectory_length INTEGER NOT NULL,
    clip_path TEXT
);
"""

INSERT_SQL = """
INSERT INTO events (
    object_id, start_time, end_time,
    start_frame, end_frame, avg_x, avg_y, avg_speed,
    trajectory_length, clip_path
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""

SELECT_ALL_SQL = """
SELECT event_id, object_id, start_time,
       end_time, start_frame, end_frame, avg_x, avg_y, avg_speed,
       trajectory_length, clip_path
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
        logger.info("Event logger initialized: %s", self._db_path)

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
            event.clip_path,
        ))
        self._conn.commit()
        event_id = cursor.lastrowid
        logger.info("Logged event #%d (object_id=%d)", event_id, event.object_id)
        return event_id

    def get_recent(self, limit: int = 50) -> list[DetectionEvent]:
        """Get the most recent events."""
        cursor = self._conn.execute(SELECT_RECENT_SQL, (limit,))
        return [self._row_to_event(row) for row in cursor.fetchall()]

    def get_stats(self) -> dict:
        """Get summary statistics."""
        cursor = self._conn.execute("SELECT COUNT(*) FROM events")
        total = cursor.fetchone()[0]
        return {"total": total}

    def delete_by_ids(self, event_ids: list[int]) -> list[str]:
        """Delete specific events by ID. Returns clip_paths of deleted events."""
        if not event_ids:
            return []
        placeholders = ",".join("?" * len(event_ids))
        cursor = self._conn.execute(
            f"SELECT clip_path FROM events WHERE event_id IN ({placeholders})",
            event_ids,
        )
        clip_paths = [row[0] for row in cursor.fetchall() if row[0]]
        self._conn.execute(
            f"DELETE FROM events WHERE event_id IN ({placeholders})", event_ids
        )
        self._conn.commit()
        return clip_paths

    def clear_all(self) -> tuple[int, list[str]]:
        """Delete all events. Returns (count, clip_paths) of removed events."""
        cursor = self._conn.execute(
            "SELECT clip_path FROM events WHERE clip_path IS NOT NULL"
        )
        clip_paths = [row[0] for row in cursor.fetchall()]
        del_cursor = self._conn.execute("DELETE FROM events")
        self._conn.commit()
        count = del_cursor.rowcount
        logger.info("Cleared %d events from history", count)
        return count, clip_paths

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
            clip_path=row[10],
        )
