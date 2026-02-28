"""YAML configuration loader with dataclass mapping."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class CaptureConfig:
    rtsp_url: str = "rtsp://user:pass@192.168.1.100:554/stream1"
    reconnect_delay: float = 5.0
    grab_timeout: float = 10.0


@dataclass
class ProcessingConfig:
    resize_width: int = 640
    resize_height: int = 360
    clahe_clip_limit: float = 2.0
    clahe_grid_size: int = 8
    blur_kernel: int = 5
    frame_skip: int = 2


@dataclass
class DetectionConfig:
    diff_threshold: int = 25
    mog2_history: int = 500
    mog2_var_threshold: int = 40
    mog2_detect_shadows: bool = False
    morph_kernel_size: int = 3
    morph_erode_iterations: int = 1
    morph_dilate_iterations: int = 2
    min_contour_area: int = 4
    max_contour_area: int = 500
    min_circularity: float = 0.3


@dataclass
class TrackingConfig:
    max_distance: int = 50
    max_disappeared: int = 15
    min_track_length: int = 5


@dataclass
class ClassificationConfig:
    blink_freq_low: float = 0.5
    blink_freq_high: float = 3.0
    blink_power_threshold: float = 0.3
    linearity_threshold: float = 0.85
    satellite_speed_min: float = 1.0
    satellite_speed_max: float = 8.0
    acceleration_var_threshold: float = 2.0


@dataclass
class RecordingConfig:
    clip_pre_buffer: float = 3.0
    clip_post_buffer: float = 5.0
    clip_dir: str = "data/clips"
    db_path: str = "data/db/detections.db"
    log_dir: str = "data/logs"
    jpeg_quality: int = 70


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    stream_fps: int = 10
    stream_quality: int = 70


@dataclass
class ScheduleConfig:
    enabled: bool = False
    start_time: str = "20:00"   # HH:MM, 24-hour
    end_time: str = "06:00"     # HH:MM, 24-hour (overnight window by default)


@dataclass
class AppConfig:
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    classification: ClassificationConfig = field(default_factory=ClassificationConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    web: WebConfig = field(default_factory=WebConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)


def _apply_dict(dc: object, data: dict) -> None:
    """Apply dictionary values onto a dataclass instance, ignoring unknown keys."""
    for key, value in data.items():
        if hasattr(dc, key):
            setattr(dc, key, value)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load configuration from a YAML file, falling back to defaults."""
    config = AppConfig()

    if path is None:
        path = os.environ.get("CONFIG_PATH", "config/default.yaml")

    path = Path(path)
    if path.exists():
        with open(path, "r") as f:
            raw = yaml.safe_load(f) or {}

        section_map = {
            "capture": config.capture,
            "processing": config.processing,
            "detection": config.detection,
            "tracking": config.tracking,
            "classification": config.classification,
            "recording": config.recording,
            "web": config.web,
            "schedule": config.schedule,
        }

        for section_name, dc_instance in section_map.items():
            if section_name in raw and isinstance(raw[section_name], dict):
                _apply_dict(dc_instance, raw[section_name])

    # Environment variable overrides
    env_url = os.environ.get("RTSP_URL")
    if env_url:
        config.capture.rtsp_url = env_url

    env_host = os.environ.get("WEB_HOST")
    if env_host:
        config.web.host = env_host

    env_port = os.environ.get("WEB_PORT")
    if env_port:
        config.web.port = int(env_port)

    return config


def save_config_values(data: dict, path: str | Path | None = None) -> None:
    """Update key/value pairs in the YAML config file, preserving all comments."""
    if path is None:
        path = os.environ.get("CONFIG_PATH", "config/default.yaml")
    path = Path(path)
    if not path.exists():
        return
    text = path.read_text()
    for key, value in data.items():
        escaped = re.escape(key)
        if isinstance(value, bool):
            val_str = "true" if value else "false"
            text = re.sub(rf'(\b{escaped}:\s*)(true|false)', rf'\g<1>{val_str}', text)
        elif isinstance(value, str):
            text = re.sub(rf'({escaped}:\s*)"[^"]*"', rf'\g<1>"{value}"', text)
        else:
            text = re.sub(rf'({escaped}:\s*)[\d.]+', rf'\g<1>{value}', text)
    path.write_text(text)
