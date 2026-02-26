"""HTTP routes: dashboard, history, settings, and REST API."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from src.pipeline import Pipeline


def create_router(pipeline: Pipeline, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get("/")
    async def dashboard(request: Request):
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "stats": pipeline.stats,
        })

    @router.get("/history")
    async def history(request: Request):
        events = pipeline.event_logger.get_recent(100)
        return templates.TemplateResponse("history.html", {
            "request": request,
            "events": events,
        })

    @router.get("/settings")
    async def settings(request: Request):
        return templates.TemplateResponse("settings.html", {
            "request": request,
            "config": pipeline.config,
        })

    # --- REST API ---

    @router.get("/api/stats")
    async def api_stats():
        return JSONResponse(pipeline.stats)

    @router.get("/api/events")
    async def api_events(limit: int = 50, classification: str | None = None):
        if classification:
            from src.recording.models import ObjectClass
            try:
                cls = ObjectClass(classification)
                events = pipeline.event_logger.get_by_classification(cls, limit)
            except ValueError:
                return JSONResponse({"error": "Invalid classification"}, 400)
        else:
            events = pipeline.event_logger.get_recent(limit)

        return JSONResponse([
            {
                "event_id": e.event_id,
                "object_id": e.object_id,
                "classification": e.classification.value,
                "confidence": e.confidence,
                "start_time": e.start_time,
                "end_time": e.end_time,
                "avg_x": e.avg_x,
                "avg_y": e.avg_y,
                "avg_speed": e.avg_speed,
                "trajectory_length": e.trajectory_length,
                "clip_path": e.clip_path,
            }
            for e in events
        ])

    @router.get("/api/event-stats")
    async def api_event_stats():
        return JSONResponse(pipeline.event_logger.get_stats())

    @router.post("/api/settings/detection")
    async def api_update_detection(request: Request):
        body = await request.json()
        # Convert string values to appropriate types
        typed = {}
        for key, value in body.items():
            if hasattr(pipeline.config.detection, key):
                attr = getattr(pipeline.config.detection, key)
                if isinstance(attr, int):
                    typed[key] = int(value)
                elif isinstance(attr, float):
                    typed[key] = float(value)
                elif isinstance(attr, bool):
                    typed[key] = bool(value)
                else:
                    typed[key] = value
        pipeline.update_detection_config(**typed)
        return JSONResponse({"status": "ok", "updated": typed})

    @router.post("/api/settings/tracking")
    async def api_update_tracking(request: Request):
        body = await request.json()
        typed = {}
        for key, value in body.items():
            if hasattr(pipeline.config.tracking, key):
                attr = getattr(pipeline.config.tracking, key)
                if isinstance(attr, int):
                    typed[key] = int(value)
                elif isinstance(attr, float):
                    typed[key] = float(value)
                else:
                    typed[key] = value
        pipeline.update_tracking_config(**typed)
        return JSONResponse({"status": "ok", "updated": typed})

    return router
