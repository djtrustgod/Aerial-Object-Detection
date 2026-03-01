"""HTTP routes: dashboard, history, settings, and REST API."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from src.pipeline import Pipeline


def _cast(current_value, new_value):
    """Cast new_value to the same type as the existing config attribute."""
    if isinstance(current_value, bool):
        return new_value in (True, "true", "1", "on", 1)
    elif isinstance(current_value, int):
        return int(float(new_value))
    elif isinstance(current_value, float):
        return float(new_value)
    return new_value


def _typed_dict(config_obj, body: dict) -> dict:
    """Return a dict of values from body, cast to match config_obj field types."""
    result = {}
    for key, value in body.items():
        if hasattr(config_obj, key):
            result[key] = _cast(getattr(config_obj, key), value)
    return result


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

    # --- REST API: stats & events ---

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

    @router.post("/api/detection/toggle")
    async def api_detection_toggle(request: Request):
        body = await request.json()
        enabled = bool(body.get("enabled", True))
        pipeline.set_detection_enabled(enabled)
        return JSONResponse({"status": "ok", "detection_enabled": enabled})

    @router.delete("/api/events")
    async def api_clear_events(request: Request):
        try:
            body = await request.json()
            event_ids = body.get("event_ids")
        except Exception:
            event_ids = None

        clips_base = Path(pipeline.config.recording.clip_dir)

        if event_ids:
            clip_paths = pipeline.event_logger.delete_by_ids(event_ids)
            count = len(event_ids)
        else:
            count, clip_paths = pipeline.event_logger.clear_all()

        files_removed = 0
        for cp in clip_paths:
            try:
                p = clips_base / Path(cp).name
                if p.exists():
                    p.unlink()
                    files_removed += 1
            except Exception:
                pass

        return JSONResponse({"status": "ok", "deleted": count, "files_removed": files_removed})

    # --- REST API: settings ---

    @router.post("/api/settings/general")
    async def api_update_general(request: Request):
        body = await request.json()
        updated = {}
        persist = {}

        if "rtsp_url" in body:
            pipeline.update_stream_url(body["rtsp_url"])
            pipeline.persist_rtsp_url(body["rtsp_url"])
            updated["rtsp_url"] = body["rtsp_url"]

        capture_keys = {"reconnect_delay": float, "grab_timeout": float}
        for key, typ in capture_keys.items():
            if key in body:
                val = typ(body[key])
                setattr(pipeline.config.capture, key, val)
                persist[key] = val
                updated[key] = val

        if "clip_dir" in body:
            pipeline.config.recording.clip_dir = body["clip_dir"]
            persist["clip_dir"] = body["clip_dir"]
            updated["clip_dir"] = body["clip_dir"]

        if persist:
            pipeline.persist_config_values(persist)

        return JSONResponse({"status": "ok", "updated": updated})

    @router.post("/api/settings/processing")
    async def api_update_processing(request: Request):
        body = await request.json()
        typed = _typed_dict(pipeline.config.processing, body)
        pipeline.update_processing_config(**typed)
        pipeline.persist_config_values(typed)
        return JSONResponse({"status": "ok", "updated": typed})

    @router.post("/api/settings/detection")
    async def api_update_detection(request: Request):
        body = await request.json()
        typed = _typed_dict(pipeline.config.detection, body)
        pipeline.update_detection_config(**typed)
        pipeline.persist_config_values(typed)
        return JSONResponse({"status": "ok", "updated": typed})

    @router.post("/api/settings/tracking")
    async def api_update_tracking(request: Request):
        body = await request.json()
        typed = _typed_dict(pipeline.config.tracking, body)
        pipeline.update_tracking_config(**typed)
        pipeline.persist_config_values(typed)
        return JSONResponse({"status": "ok", "updated": typed})

    @router.post("/api/settings/classification")
    async def api_update_classification(request: Request):
        body = await request.json()
        typed = _typed_dict(pipeline.config.classification, body)
        pipeline.update_classification_config(**typed)
        pipeline.persist_config_values(typed)
        return JSONResponse({"status": "ok", "updated": typed})

    @router.post("/api/settings/recording")
    async def api_update_recording(request: Request):
        body = await request.json()
        typed = _typed_dict(pipeline.config.recording, body)
        pipeline.update_recording_config(**typed)
        pipeline.persist_config_values(typed)
        return JSONResponse({"status": "ok", "updated": typed})

    @router.post("/api/settings/web")
    async def api_update_web(request: Request):
        body = await request.json()
        typed = _typed_dict(pipeline.config.web, body)
        pipeline.update_web_config(**typed)
        pipeline.persist_config_values(typed)
        return JSONResponse({"status": "ok", "updated": typed})

    @router.post("/api/settings/schedule")
    async def api_update_schedule(request: Request):
        body = await request.json()
        typed = _typed_dict(pipeline.config.schedule, body)
        pipeline.update_schedule_config(**typed)
        pipeline.persist_config_values(typed)
        return JSONResponse({"status": "ok", "updated": typed})

    return router
