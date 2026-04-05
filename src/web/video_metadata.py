"""JSON metadata store for uploaded test videos and analysis history."""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

METADATA_PATH = Path("data/uploads/metadata.json")
VALID_CATEGORIES = ("positive", "false_positive")

_lock = threading.Lock()


def _load() -> dict:
    """Read metadata from disk. Returns default structure if missing or corrupt."""
    try:
        if METADATA_PATH.exists():
            data = json.loads(METADATA_PATH.read_text())
            if isinstance(data, dict):
                data.setdefault("files", {})
                data.setdefault("analysis_history", [])
                return data
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read metadata file, starting fresh")
    return {"files": {}, "analysis_history": []}


def _save(data: dict) -> None:
    """Write metadata atomically (tmp + rename)."""
    METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = METADATA_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(METADATA_PATH)


def register_file(filename: str, category: str) -> None:
    """Register a newly uploaded file with its category."""
    if category not in VALID_CATEGORIES:
        category = "positive"
    with _lock:
        data = _load()
        data["files"][filename] = {
            "category": category,
            "uploaded_at": time.time(),
        }
        _save(data)


def remove_file(filename: str) -> None:
    """Remove a file's metadata entry. Analysis history entries are preserved."""
    with _lock:
        data = _load()
        data["files"].pop(filename, None)
        _save(data)


def set_file_category(filename: str, category: str) -> bool:
    """Set the category for a file. Returns False if invalid category."""
    if category not in VALID_CATEGORIES:
        return False
    with _lock:
        data = _load()
        if filename in data["files"]:
            data["files"][filename]["category"] = category
        else:
            data["files"][filename] = {
                "category": category,
                "uploaded_at": time.time(),
            }
        _save(data)
    return True


def get_all_files_meta() -> dict:
    """Return the full files metadata dict."""
    with _lock:
        return _load()["files"]


def get_file_meta(filename: str) -> dict | None:
    """Return metadata for a single file, or None if not found."""
    with _lock:
        return _load()["files"].get(filename)


def add_analysis_record(filename: str, category: str,
                        metrics: dict, settings_used: dict) -> dict:
    """Append an analysis result to history. Returns the new record."""
    record = {
        "id": secrets.token_hex(4),
        "timestamp": time.time(),
        "filename": filename,
        "category": category,
        "metrics": metrics,
        "settings_used": settings_used,
    }
    with _lock:
        data = _load()
        data["analysis_history"].append(record)
        _save(data)
    return record


def get_analysis_history(filename: str | None = None) -> list[dict]:
    """Return analysis history, newest first. Optionally filter by filename."""
    with _lock:
        history = _load()["analysis_history"]
    if filename:
        history = [r for r in history if r["filename"] == filename]
    return list(reversed(history))


def delete_analysis_record(record_id: str) -> bool:
    """Delete a single history entry by ID. Returns True if found."""
    with _lock:
        data = _load()
        before = len(data["analysis_history"])
        data["analysis_history"] = [
            r for r in data["analysis_history"] if r.get("id") != record_id
        ]
        if len(data["analysis_history"]) < before:
            _save(data)
            return True
    return False
