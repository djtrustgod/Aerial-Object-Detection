# Multi-Camera Support (2 Cameras)

## Context
The system currently runs a single Pipeline processing one RTSP stream. The user wants to add a second camera. Camera & connection settings, clip directories, zones, and history should be per-camera. Processing, detection, tracking, recording buffer, schedule, and web settings stay shared to reduce complexity. Max 2 cameras.

---

## Approach: Two Pipeline Instances

Each camera gets its own Pipeline instance (own FrameGrabber thread, Detector, Tracker, ClipWriter, EventLogger, zones file). They share the same config objects for processing/detection/tracking/schedule by reference. A lightweight `PipelineManager` dict-wrapper holds both.

---

## Files to Create

### 1. `src/pipeline_manager.py` (~40 lines)
Simple registry wrapping `dict[str, Pipeline]`:
- `add(cam_id, pipeline)`, `get(cam_id)`, `all()`, `camera_ids`
- `start_all()`, `stop_all()`, `set_event_loop(loop)`

---

## Files to Modify

### 2. `src/config.py`
- **Add `CameraConfig` dataclass**: `name`, `enabled`, `rtsp_url`, `reconnect_delay`, `grab_timeout`, `clip_dir`, `db_path`
- **Add `cameras: dict[str, CameraConfig]`** to `AppConfig` (keep `capture` for backward compat)
- **Remove `clip_dir` and `db_path` from `RecordingConfig`** (moved to per-camera)
- **Update `load_config()`**:
  - Parse `cameras:` section from YAML → create CameraConfig instances
  - **Backward compat**: if no `cameras:` key but `capture:` exists, auto-create `cam1` from old `capture` + `recording.clip_dir`/`db_path`
  - `local.yaml` overlay applies to both shared sections and `cameras:` entries
- **Add `persist_camera_value(cam_id, key, value, path)`** — writes a nested key under `cameras.{cam_id}` in local.yaml

### 3. `config/default.yaml`
Replace `capture:` with `cameras:` section:
```yaml
cameras:
  cam1:
    name: "Camera 1"
    enabled: true
    rtsp_url: ""
    reconnect_delay: 5.0
    grab_timeout: 10.0
    clip_dir: "data/clips/cam1"
    db_path: "data/db/cam1.db"
  cam2:
    name: "Camera 2"
    enabled: false
    rtsp_url: ""
    reconnect_delay: 5.0
    grab_timeout: 10.0
    clip_dir: "data/clips/cam2"
    db_path: "data/db/cam2.db"
```
Remove `clip_dir` and `db_path` from `recording:` section. Keep all other shared sections unchanged.

### 4. `config/local.yaml`
Update to new structure:
```yaml
cameras:
  cam1:
    rtsp_url: "rtsp://..."
```

### 5. `src/pipeline.py`
- **Constructor**: Add `camera_id: str` and `camera_config: CameraConfig` params. Use `camera_config` for FrameGrabber URL, reconnect settings, ClipWriter clip_dir, EventLogger db_path.
- **Zones**: Load from `config/zones_{camera_id}.json` instead of `config/zones.json`. Pass path to all zone load/save calls.
- **`stats` property**: Include `camera_id` and camera `name` in returned dict.
- **`persist_rtsp_url()`**: Call new `persist_camera_value()` helper to write under `cameras.{cam_id}` in local.yaml.
- **`_publish_event()`**: Include `camera_id` in `event_data` dict for WebSocket broadcasts.
- **Backward compat**: Migrate `config/zones.json` → `config/zones_cam1.json` on first load if old file exists.

### 6. `src/main.py`
- Import `PipelineManager`.
- Iterate `config.cameras` items; for each **enabled** camera, create dirs and instantiate `Pipeline(config, cam_id, cam_config, config_path)`.
- Register all in `PipelineManager`.
- Pass manager to `create_app(manager)`.
- `manager.start_all()` / `manager.stop_all()`.
- CLI `-u` flag sets cam1's URL.

### 7. `src/web/app.py`
- `create_app(manager: PipelineManager)` instead of single pipeline.
- Mount `/clips/{cam_id}` static dirs per camera.
- Pass `manager` to `create_router()` and `create_ws_router()`.

### 8. `src/web/websocket.py`
- **`/ws/stream/{cam_id}`** — streams frames from the specified camera's pipeline.
- **`/ws/events`** — single endpoint, broadcasts events from all cameras (each event has `camera_id` field). One `on_event` callback registered per pipeline.
- Per-camera `ConnectionManager` instances (or one shared manager — simpler with one since events merge).

### 9. `src/web/routes.py`
**Camera-specific API routes** (add `{cam_id}` path param):
- `GET /api/stats` — returns dict of both cameras' stats
- `GET /api/events/{cam_id}` — events from that camera
- `GET /api/event-stats/{cam_id}` — hourly stats for that camera
- `POST /api/detection/toggle/{cam_id}` — toggle per camera
- `DELETE /api/events/{cam_id}` — clear events for camera
- `GET /api/snapshot/{cam_id}` — JPEG from camera
- `GET/POST /api/zones/{cam_id}`, `DELETE /api/zones/{cam_id}/{zone_id}` — per-camera zones

**Settings routes** — split:
- `POST /api/settings/camera/{cam_id}` — RTSP URL, reconnect, timeout, clip_dir (per-camera)
- Shared endpoints unchanged (processing, detection, tracking, recording, web, schedule) — iterate `manager.all()` to apply updates to all pipelines

**Page routes**:
- `GET /` — passes all pipelines to template
- `GET /history?camera=cam1` — camera filter param (default: cam1)
- `GET /settings` — passes manager for camera-specific sections
- `GET /zones?camera=cam1` — camera selector param

### 10. `src/web/templates/dashboard.html`
- Two stream cards side by side (flex row), each with own canvas, toggle button, status indicator
- Canvas IDs: `stream-canvas-cam1`, `stream-canvas-cam2`
- If cam2 disabled, hide its card
- Event feed shows events from both cameras with camera label
- Active detections shows per-camera counts

### 11. `src/web/static/js/dashboard.js`
- Two WebSocket connections: `/ws/stream/cam1`, `/ws/stream/cam2`
- One `/ws/events` connection (events have `camera_id` field)
- Stats polling updates per-camera indicators
- Detection toggle targets specific camera

### 12. `src/web/templates/history.html`
- Camera dropdown selector (Cam 1 / Cam 2)
- Fetches from `/api/events/{selected_cam_id}`
- Clip links use `/clips/{cam_id}/filename.mp4`
- Delete targets correct camera

### 13. `src/web/templates/settings.html`
- Replace single "Camera & Connection" card with two cards (one per camera)
- Cam2 card has enable/disable toggle
- Each card submits to `/api/settings/camera/{cam_id}`
- Shared sections unchanged

### 14. `src/web/templates/zones.html`
- Camera dropdown selector at top
- Snapshot loads from `/api/snapshot/{selected_cam_id}`
- Zone CRUD hits `/api/zones/{selected_cam_id}`
- Switching camera reloads snapshot + zone list

### 15. `src/web/templates/base.html`
- No changes needed (nav links stay the same)

### 16. `src/web/static/css/dashboard.css`
- Add dual-stream flex layout (`.dual-stream { display: flex; gap: 1rem; }`)
- Each stream card takes `flex: 1`
- Mobile: stack vertically via media query

---

## CPU Impact

| Component | Per Camera | 2 Cameras |
|-----------|-----------|-----------|
| FrameGrabber thread | ~1-2% | ~2-4% |
| Processing (CLAHE, blur, MOG2, contours) | ~5-15% | ~10-30% |
| WebSocket JPEG encoding | ~1-2% per viewer | ~2-4% |
| **Total estimate** | **~8-18%** | **~15-35%** |

Existing mitigations: frame_skip=4 (8 when idle), no processing outside schedule, 640x360 resolution. The second camera roughly doubles detection CPU but idle-mode optimizations keep it manageable. Can increase frame_skip if needed.

---

## Implementation Order
1. `src/config.py` + `config/default.yaml` + `config/local.yaml` — config restructure
2. `src/pipeline_manager.py` — new file
3. `src/pipeline.py` — camera_id, per-camera paths
4. `src/main.py` — multi-pipeline startup
5. `src/web/app.py` — accept manager
6. `src/web/websocket.py` — per-camera streams
7. `src/web/routes.py` — camera-parameterized routes
8. Templates & CSS — dashboard, history, settings, zones UI updates

## Verification
1. Start with only cam1 enabled — should work identically to current single-camera behavior
2. Enable cam2 in settings or config — second stream appears on dashboard
3. Zones page: switch between cameras, draw zones on each independently
4. History page: filter by camera, verify events are separated
5. Settings: change cam2 RTSP URL, verify it connects independently
6. Restart server — both cameras reconnect, zones/history persist per-camera
7. Monitor CPU usage with both cameras active vs single camera
