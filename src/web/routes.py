"""HTTP routes: dashboard, history, settings, and REST API."""

from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path

import cv2
from fastapi import APIRouter, Form, Request, UploadFile, File
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates

from src.pipeline import Pipeline
from src.web.test_pipeline import TestPipeline, get_active_test, set_active_test
from src.web import video_metadata as vmeta


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


def _remove_files(base_dir: Path, paths: list[str]) -> int:
    """Delete files by name from base_dir, including _clean companion clips.

    Returns count of files removed.
    """
    removed = 0
    for p in paths:
        try:
            name = Path(p).name
            fp = base_dir / name
            if fp.exists():
                fp.unlink()
                removed += 1
            # Also remove the companion clean clip (e.g. clip_xxx_clean.mp4)
            if name.endswith(".mp4"):
                clean_fp = base_dir / name.replace(".mp4", "_clean.mp4")
                if clean_fp.exists():
                    clean_fp.unlink()
                    removed += 1
        except Exception:
            pass
    return removed


def create_router(pipeline: Pipeline, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get("/")
    async def dashboard(request: Request):
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "stats": pipeline.stats,
        })

    @router.get("/history")
    async def history(request: Request, session: int | None = None):
        sessions = pipeline.event_logger.get_sessions()
        if not sessions:
            return templates.TemplateResponse("history.html", {
                "request": request,
                "events": [],
                "sessions": [],
                "current_session": None,
            })

        # Default to newest session (session_id=1)
        current = session if session and 1 <= session <= len(sessions) else 1
        events = pipeline.event_logger.get_events_by_session(current)

        return templates.TemplateResponse("history.html", {
            "request": request,
            "events": events,
            "sessions": sessions,
            "current_session": current,
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
    async def api_events(limit: int = 50):
        events = pipeline.event_logger.get_recent(limit)
        return JSONResponse([
            {
                "event_id": e.event_id,
                "object_id": e.object_id,
                "start_time": e.start_time,
                "end_time": e.end_time,
                "avg_x": e.avg_x,
                "avg_y": e.avg_y,
                "avg_speed": e.avg_speed,
                "travel_distance": e.travel_distance,
                "clip_path": e.clip_path,
                "thumbnail_path": e.thumbnail_path,
            }
            for e in events
        ])

    @router.get("/api/sessions")
    async def api_sessions():
        sessions = pipeline.event_logger.get_sessions()
        result = []
        for s in sessions:
            result.append({
                "session_id": s["session_id"],
                "start_time": s["start_time"],
                "end_time": s["end_time"],
                "event_count": s["event_count"],
                "label": datetime.fromtimestamp(s["start_time"]).strftime("%b %d, %Y %I:%M %p")
                         + " – "
                         + datetime.fromtimestamp(s["end_time"]).strftime("%b %d, %Y %I:%M %p"),
            })
        return JSONResponse(result)

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
        thumbs_base = Path(pipeline.config.recording.thumb_dir)

        if event_ids:
            clip_paths, thumb_paths = pipeline.event_logger.delete_by_ids(event_ids)
            count = len(event_ids)
        else:
            count, clip_paths, thumb_paths = pipeline.event_logger.clear_all()

        files_removed = _remove_files(clips_base, clip_paths)
        files_removed += _remove_files(thumbs_base, thumb_paths)

        return JSONResponse({"status": "ok", "deleted": count, "files_removed": files_removed})

    @router.delete("/api/sessions/{session_id}")
    async def api_delete_session(session_id: int):
        clips_base = Path(pipeline.config.recording.clip_dir)
        thumbs_base = Path(pipeline.config.recording.thumb_dir)

        count, clip_paths, thumb_paths = pipeline.event_logger.delete_by_session(session_id)

        files_removed = _remove_files(clips_base, clip_paths)
        files_removed += _remove_files(thumbs_base, thumb_paths)

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

    # --- Snapshot & Exclusion Zones ---

    @router.get("/api/snapshot")
    async def api_snapshot():
        frame = pipeline.display_frame
        if frame is None:
            return Response(status_code=503, content="No frame available")
        _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return Response(content=jpeg.tobytes(), media_type="image/jpeg")

    @router.get("/api/zones")
    async def api_get_zones():
        return JSONResponse(pipeline.get_zones())

    @router.post("/api/zones")
    async def api_create_zone(request: Request):
        body = await request.json()
        x, y = int(body["x"]), int(body["y"])
        w, h = int(body["w"]), int(body["h"])
        # Snap edges within 10px of frame boundary to be flush
        frame_w = pipeline.config.processing.resize_width
        frame_h = pipeline.config.processing.resize_height
        snap = 10
        if x <= snap:
            w += x; x = 0
        if y <= snap:
            h += y; y = 0
        if x + w >= frame_w - snap:
            w = frame_w - x
        if y + h >= frame_h - snap:
            h = frame_h - y
        zone = {
            "id": secrets.token_hex(4),
            "x": x, "y": y, "w": w, "h": h,
            "label": body.get("label", ""),
        }
        pipeline.add_zone(zone)
        return JSONResponse(zone, status_code=201)

    @router.delete("/api/zones/{zone_id}")
    async def api_delete_zone(zone_id: str):
        if not pipeline.delete_zone(zone_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"status": "ok"})

    @router.get("/zones")
    async def zones_page(request: Request):
        return templates.TemplateResponse("zones.html", {
            "request": request,
            "zones": pipeline.get_zones(),
        })

    # --- Test Video Upload & Analysis ---

    UPLOADS_DIR = Path("data/uploads")
    MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 500 MB

    @router.post("/api/test-video/upload")
    async def api_test_video_upload(
        file: UploadFile = File(...),
        category: str = Form("positive"),
    ):
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        ext = Path(file.filename or "video.mp4").suffix.lower()
        if ext not in (".mp4", ".avi", ".mov", ".mkv"):
            return JSONResponse({"error": "Unsupported format. Use MP4, AVI, MOV, or MKV."},
                                status_code=400)

        contents = await file.read()
        if len(contents) > MAX_UPLOAD_SIZE:
            return JSONResponse({"error": "File too large. Maximum 500 MB."},
                                status_code=413)

        safe_name = Path(file.filename).name
        dest = UPLOADS_DIR / safe_name
        dest.write_bytes(contents)

        vmeta.register_file(safe_name, category)

        size_mb = round(len(contents) / (1024 * 1024), 1)
        return JSONResponse({"status": "ok", "filename": safe_name,
                             "size_mb": size_mb, "category": category})

    @router.get("/api/test-video/files")
    async def api_test_video_files():
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        meta = vmeta.get_all_files_meta()
        files = []
        for f in sorted(UPLOADS_DIR.iterdir()):
            if f.is_file() and f.suffix.lower() in (".mp4", ".avi", ".mov", ".mkv"):
                stat = f.stat()
                file_meta = meta.get(f.name, {})
                files.append({
                    "filename": f.name,
                    "size_mb": round(stat.st_size / (1024 * 1024), 1),
                    "uploaded_at": file_meta.get("uploaded_at", stat.st_mtime),
                    "category": file_meta.get("category", "positive"),
                })
        return JSONResponse(files)

    @router.delete("/api/test-video/files/{filename}")
    async def api_test_video_delete(filename: str):
        safe_name = Path(filename).name
        fp = UPLOADS_DIR / safe_name
        if not fp.exists():
            return JSONResponse({"error": "File not found"}, status_code=404)
        fp.unlink()
        vmeta.remove_file(safe_name)
        return JSONResponse({"status": "ok"})

    @router.patch("/api/test-video/files/{filename}/category")
    async def api_test_video_set_category(filename: str, request: Request):
        body = await request.json()
        category = body.get("category")
        if category not in ("positive", "false_positive"):
            return JSONResponse({"error": "Invalid category"}, status_code=400)
        safe_name = Path(filename).name
        fp = UPLOADS_DIR / safe_name
        if not fp.exists():
            return JSONResponse({"error": "File not found"}, status_code=404)
        vmeta.set_file_category(safe_name, category)
        return JSONResponse({"status": "ok", "category": category})

    @router.post("/api/test-video/bulk-delete")
    async def api_test_video_bulk_delete(request: Request):
        body = await request.json()
        filenames = body.get("filenames", [])
        deleted = 0
        for name in filenames:
            safe_name = Path(name).name
            fp = UPLOADS_DIR / safe_name
            if fp.exists():
                fp.unlink()
                vmeta.remove_file(safe_name)
                deleted += 1
        return JSONResponse({"status": "ok", "deleted": deleted})

    @router.post("/api/test-video/bulk-category")
    async def api_test_video_bulk_category(request: Request):
        body = await request.json()
        filenames = body.get("filenames", [])
        category = body.get("category")
        if category not in ("positive", "false_positive"):
            return JSONResponse({"error": "Invalid category"}, status_code=400)
        updated = 0
        for name in filenames:
            safe_name = Path(name).name
            if (UPLOADS_DIR / safe_name).exists():
                vmeta.set_file_category(safe_name, category)
                updated += 1
        return JSONResponse({"status": "ok", "updated": updated})

    @router.post("/api/test-video/start")
    async def api_test_video_start(request: Request):
        body = await request.json()
        filename = body.get("filename")
        if not filename:
            return JSONResponse({"error": "filename required"}, status_code=400)

        safe_name = Path(filename).name
        fp = UPLOADS_DIR / safe_name
        if not fp.exists():
            return JSONResponse({"error": "File not found"}, status_code=404)

        active = get_active_test()
        if active and active.is_running:
            return JSONResponse({"error": "A test is already running. Stop it first."},
                                status_code=409)

        test = TestPipeline(str(fp), pipeline.config)
        set_active_test(test)
        test.start()
        return JSONResponse({"status": "ok", "filename": safe_name})

    @router.post("/api/test-video/stop")
    async def api_test_video_stop():
        active = get_active_test()
        if active and active.is_running:
            active.stop()
        set_active_test(None)
        return JSONResponse({"status": "ok"})

    @router.get("/api/test-video/status")
    async def api_test_video_status():
        active = get_active_test()
        if active is None:
            return JSONResponse({
                "running": False, "complete": False,
                "progress": 0, "current_frame": 0,
                "total_frames": 0, "fps": 0,
            })
        return JSONResponse({
            "running": active.is_running,
            "complete": active.is_complete,
            "progress": round(active.progress, 4),
            "current_frame": active.current_frame,
            "total_frames": active.total_frames,
            "fps": round(active.video_fps, 1),
        })

    @router.post("/api/test-video/analyze")
    async def api_test_video_analyze(request: Request):
        body = await request.json()
        filename = body.get("filename")
        if not filename:
            return JSONResponse({"error": "filename required"}, status_code=400)

        safe_name = Path(filename).name
        fp = UPLOADS_DIR / safe_name
        if not fp.exists():
            return JSONResponse({"error": "File not found"}, status_code=404)

        test = TestPipeline(str(fp), pipeline.config)
        result = test.run_analysis()

        if "error" in result:
            return JSONResponse(result, status_code=400)

        # Save to analysis history
        file_meta = vmeta.get_file_meta(safe_name)
        category = file_meta["category"] if file_meta else "positive"
        record = vmeta.add_analysis_record(
            safe_name, category,
            {k: v for k, v in result.items() if k != "settings_used"},
            result.get("settings_used", {}),
        )
        result["history_id"] = record["id"]

        return JSONResponse(result)

    @router.get("/api/test-video/history")
    async def api_test_video_history(filename: str | None = None):
        history = vmeta.get_analysis_history(filename)
        return JSONResponse(history)

    @router.delete("/api/test-video/history/{record_id}")
    async def api_test_video_delete_history(record_id: str):
        if vmeta.delete_analysis_record(record_id):
            return JSONResponse({"status": "ok"})
        return JSONResponse({"error": "Not found"}, status_code=404)

    return router
