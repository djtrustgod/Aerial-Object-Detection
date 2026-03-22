# Claude Instructions for Aerial Object Detection

## Running the Dev Server

When the user says **"run dev"**, **"run"**, or similar, do the following:

1. Check if something is already running on port 8080:
   ```
   netstat -ano | grep ":8080"
   ```
2. If a process is found, get its PID and kill it:
   ```
   powershell -Command "Stop-Process -Id <PID> -Force"
   ```
3. Kill any leftover background jobs from this session:
   ```
   kill %1 2>/dev/null
   ```
4. Start the server in the background with verbose logging:
   ```
   cd /c/Users/djtru/Documents/GitHub/Aerial-Object-Detection && python -m src.main -v 2>&1 &
   ```
5. Wait ~4 seconds, then verify it's up:
   ```
   curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/
   ```
6. Report success (200 OK) and remind the user the dashboard is at http://localhost:8080.

**Note:** The RTSP stream URL in `config/default.yaml` is a placeholder — the web dashboard works regardless.

## Version Bumps

When asked to update the version, update **all** of the following locations:

- `pyproject.toml` — `version = "x.y.z"`
- `src/web/app.py` — `FastAPI(..., version="x.y.z")`
- `src/web/templates/base.html` — footer text (`vx.y.z`) and CSS cache-buster query string (`?v=x.y.z`)
