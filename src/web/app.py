"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.pipeline import Pipeline
from src.web.routes import create_router
from src.web.websocket import create_ws_router

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"


def create_app(pipeline: Pipeline) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Aerial Object Detection", version="0.1.0")

    # Static files
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Templates
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    # Routes
    app.include_router(create_router(pipeline, templates))
    app.include_router(create_ws_router(pipeline))

    # Serve clip files
    clips_dir = Path(pipeline.config.recording.clip_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/clips", StaticFiles(directory=str(clips_dir)), name="clips")

    return app
