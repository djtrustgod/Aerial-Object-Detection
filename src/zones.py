"""Exclusion zone load/save/query helpers."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = "config/zones.json"


def load_zones(path: str = DEFAULT_PATH) -> list[dict]:
    """Load exclusion zones from JSON. Returns [] on missing/malformed file."""
    try:
        data = json.loads(Path(path).read_text())
        if isinstance(data, list):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return []


def save_zones(zones: list[dict], path: str = DEFAULT_PATH) -> None:
    """Write zones list to JSON file."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(zones, indent=2))


def point_in_any_zone(x: int, y: int, zones: list[dict]) -> bool:
    """Return True if (x, y) falls inside any exclusion zone rectangle."""
    for z in zones:
        if z["x"] <= x <= z["x"] + z["w"] and z["y"] <= y <= z["y"] + z["h"]:
            return True
    return False
