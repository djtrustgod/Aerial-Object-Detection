"""Shared data models for the detection pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ObjectClass(str, Enum):
    AIRCRAFT = "aircraft"
    SATELLITE = "satellite"
    UAP = "uap"
    UNKNOWN = "unknown"


@dataclass
class Detection:
    """A single detected blob in one frame."""
    x: int                    # centroid x
    y: int                    # centroid y
    w: int                    # bounding box width
    h: int                    # bounding box height
    area: float               # contour area in pixels
    brightness: float         # mean brightness of the blob region
    frame_number: int = 0
    timestamp: float = 0.0


@dataclass
class TrackedObject:
    """A tracked object with history across frames."""
    object_id: int
    centroid: tuple[int, int]
    positions: list[tuple[int, int]] = field(default_factory=list)
    brightness_history: list[float] = field(default_factory=list)
    frame_history: list[int] = field(default_factory=list)
    disappeared: int = 0
    classification: ObjectClass = ObjectClass.UNKNOWN
    confidence: float = 0.0
    speed: float = 0.0       # pixels per frame


@dataclass
class DetectionEvent:
    """A completed detection event for logging."""
    event_id: Optional[int] = None
    object_id: int = 0
    classification: ObjectClass = ObjectClass.UNKNOWN
    confidence: float = 0.0
    start_time: float = 0.0
    end_time: float = 0.0
    start_frame: int = 0
    end_frame: int = 0
    avg_x: float = 0.0
    avg_y: float = 0.0
    avg_speed: float = 0.0
    trajectory_length: int = 0
    clip_path: Optional[str] = None
