"""SQLite event logger with WAL mode for concurrent reads."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from src.recording.models import DetectionEvent, ObjectClass

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL,
    classification TEXT NOT NULL,
    confidence REAL NOT NULL,
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
    object_id, classification, confidence, start_time, end_time,
    start_frame, end_frame, avg_x, avg_y, avg_speed,
    trajectory_length, clip_path
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""

SELECT_ALL_SQL = """
SELECT event_id, object_id, classification, confidence, start_time,
       end_time, start_frame, end_frame, avg_x, avg_y, avg_speed,
       trajectory_length, clip_path
FROM events ORDER BY start_time DESC
"""

SELECT_RECENT_SQL = SELECT_ALL_SQL + " LIMIT ?"

SELECT_BY_CLASS_SQL = """
SELECT event_id, object_id, classification, confidence, start_time,
       end_time, start_frame, end_frame, avg_x, avg_y, avg_speed,
       trajectory_length, clip_path
FROM events WHERE classification = ?
ORDER BY start_time DESC LIMIT ?
"""


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
            event.classification.value,
            event.confidence,
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
        logger.info("Logged event #%d: %s (confidence=%.2f)",
                     event_id, event.classification.value, event.confidence)
        return event_id

    def get_recent(self, limit: int = 50) -> list[DetectionEvent]:
        """Get the most recent events."""
        cursor = self._conn.execute(SELECT_RECENT_SQL, (limit,))
        return [self._row_to_event(row) for row in cursor.fetchall()]

    def get_by_classification(self, cls: ObjectClass,
                              limit: int = 50) -> list[DetectionEvent]:
        """Get events filtered by classification."""
        cursor = self._conn.execute(SELECT_BY_CLASS_SQL, (cls.value, limit))
        return [self._row_to_event(row) for row in cursor.fetchall()]

    def get_stats(self) -> dict:
        """Get summary statistics."""
        cursor = self._conn.execute(
            "SELECT classification, COUNT(*) FROM events GROUP BY classification"
        )
        counts = {row[0]: row[1] for row in cursor.fetchall()}
        total = sum(counts.values())
        return {"total": total, "by_class": counts}

    def clear_all(self) -> int:
        """Delete all events. Returns the number of rows removed."""
        cursor = self._conn.execute("DELETE FROM events")
        self._conn.commit()
        count = cursor.rowcount
        logger.info("Cleared %d events from history", count)
        return count

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    @staticmethod
    def _row_to_event(row: tuple) -> DetectionEvent:
        return DetectionEvent(
            event_id=row[0],
            object_id=row[1],
            classification=ObjectClass(row[2]),
            confidence=row[3],
            start_time=row[4],
            end_time=row[5],
            start_frame=row[6],
            end_frame=row[7],
            avg_x=row[8],
            avg_y=row[9],
            avg_speed=row[10],
            trajectory_length=row[11],
            clip_path=row[12],
        )
