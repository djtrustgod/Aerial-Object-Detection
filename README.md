<p align="center">
  <img src="src/web/static/img/logo.svg" alt="Aerial Object Detection" width="120">
</p>

# Aerial Object Detection

Nighttime aerial object detection system using classical computer vision. Connects to RTSP cameras or video files, detects small moving lighted objects in the night sky, and serves a real-time web dashboard. Runs entirely on CPU with no deep learning dependencies.

## Features

- **Real-time RTSP streaming** with automatic reconnection
- **Multi-stage detection pipeline**: frame differencing + MOG2 background subtraction, morphological filtering, and contour analysis — tuned for small (2-10 px) lighted objects on compressed, noisy feeds
- **Centroid-based object tracking** across frames with automatic track lifecycle management
- **Event recording** with pre/post-event buffered video clips and SQLite event logging
- **Web dashboard** with live MJPEG stream via WebSocket, detection history, and statistics (Chart.js)
- **Detection scheduling** with configurable time windows and manual override toggle
- **Fully configurable** via YAML with CLI overrides and live settings from the dashboard

## Requirements

- Python 3.10+
- An RTSP camera or video file for input

## Docker Deployment (Recommended)

The easiest way to run the system is with Docker. A single command builds and starts everything.

### First Run

```bash
git clone https://github.com/your-username/Aerial-Object-Detection.git
cd Aerial-Object-Detection
RTSP_URL="rtsp://user:pass@camera-ip/stream1" docker compose up --build -d
```

The dashboard will be available at http://localhost:8080.

On first start, the default configuration is automatically copied into the persistent data directory. No manual config setup is required.

### Updating an Existing Installation

Pull the latest code and rebuild. Your configuration and recorded data are stored outside the repo and will not be affected:

```bash
cd Aerial-Object-Detection
git pull
docker compose up --build -d
```

### Configuration

You can set the RTSP URL in two ways:

- **Environment variable** (no rebuild needed):
  ```bash
  RTSP_URL="rtsp://user:pass@camera-ip/stream1" docker compose up -d
  ```

- **Edit the config file** in the persistent data directory, then restart:
  ```bash
  # Edit the config (see "Data Location" below for the path)
  docker compose restart
  ```

To change the web port:
```bash
WEB_PORT=9090 docker compose up -d
```

### Data Location

All persistent data (config, clips, database, logs) is stored **outside the repo** at a location determined by your OS. You can override it with the `AERIAL_DATA` environment variable.

| OS | Default path | Config file | Recorded clips |
|---|---|---|---|
| **Windows** | `C:\Users\<you>\aerial-detect-data\` | `...\config\default.yaml` | `...\data\clips\` |
| **macOS** | `~/aerial-detect-data/` | `.../config/default.yaml` | `.../data/clips/` |
| **Linux** | `~/aerial-detect-data/` | `.../config/default.yaml` | `.../data/clips/` |

To use a custom location:
```bash
AERIAL_DATA=/mnt/storage/aerial docker compose up --build -d
```

The data directory contains:
```
aerial-detect-data/
  config/
    default.yaml          # Editable configuration
  data/
    clips/                # Recorded detection video clips
    db/                   # SQLite detection database
    logs/                 # Application logs
```

### Useful Commands

```bash
# View logs
docker compose logs -f

# Check container health
docker compose ps

# Stop the service
docker compose down

# Full rebuild (e.g. after dependency changes)
docker compose up --build -d --force-recreate
```

## Manual Installation

If you prefer to run without Docker:

```bash
git clone https://github.com/your-username/Aerial-Object-Detection.git
cd Aerial-Object-Detection
pip install -e ".[dev]"
```

## Quick Start

1. **Set your camera URL** — create `config/local.yaml` (gitignored) with your RTSP address:

    ```yaml
    capture:
      rtsp_url: "rtsp://192.168.1.100/stream1"
    ```

    Supported URL formats:
    - `rtsp://192.168.1.100/stream1` — no auth
    - `rtsp://192.168.1.100:554/stream1` — custom port
    - `rtsp://user:pass@192.168.1.100/stream1` — with credentials
    - `rtsp://user:pass@192.168.1.100:554/stream1` — credentials + port

    You can also pass the URL via CLI flag or environment variable (see below).

2. **Run the server:**

    ```bash
    # Using local.yaml config
    python -m src.main -v

    # Or pass the URL directly
    python -m src.main -u rtsp://192.168.1.100/stream1

    # Or use a local video file
    python -m src.main -u path/to/video.mp4

    # Custom host and port
    python -m src.main --host 0.0.0.0 --port 9090
    ```

3. Open `http://localhost:8080` in your browser to view the dashboard.

## Configuration

All parameters are in [config/default.yaml](config/default.yaml). To override settings locally without committing changes, create `config/local.yaml` — it is loaded on top of the defaults and is gitignored. You can also use environment variables (`RTSP_URL`, `WEB_HOST`, `WEB_PORT`).

Key sections:

| Section | Description |
|---|---|
| `capture` | RTSP URL, reconnect delay, grab timeout |
| `processing` | Resize dimensions, CLAHE, blur, frame skip |
| `detection` | Diff threshold, MOG2 params, morphology, contour filters |
| `tracking` | Max matching distance, disappear timeout, min track length |
| `recording` | Pre/post buffer duration, clip output dir, database path |
| `web` | Host, port, stream FPS and quality |
| `schedule` | Enable/disable detection time window, start/end times |

## How It Works

```
RTSP Stream
    |
    v
Frame Grab (threaded)
    |
    v
Preprocessing (resize, CLAHE, Gaussian blur)
    |
    v
Detection (frame diff + MOG2 -> morphology -> contour filtering)
    |
    v
Tracking (centroid matching across frames)
    |
    v
Recording (buffered clip writer + SQLite event logger)
    |
    v
Web Dashboard (FastAPI + WebSocket MJPEG stream)
```

## Project Structure

```
config/default.yaml          # All tunable parameters (committed)
config/local.yaml            # Local overrides, e.g. RTSP URL (gitignored)
src/
  main.py                    # CLI entry point
  config.py                  # Dataclass-based YAML config loader
  pipeline.py                # Orchestrator (grab, process, record, serve)
  capture/stream.py          # Threaded RTSP frame grabber
  processing/
    preprocessor.py          # Resize, CLAHE, blur
    detector.py              # Frame diff + MOG2 + contour extraction
    tracker.py               # Centroid-based multi-object tracker
  recording/
    models.py                # Data models (Detection, TrackedObject, DetectionEvent)
    clip_writer.py           # Buffered MP4 clip writer
    event_logger.py          # SQLite event logger (WAL mode)
  web/
    app.py                   # FastAPI application factory
    routes.py                # HTTP routes
    websocket.py             # Live MJPEG stream over WebSocket
    templates/               # Jinja2 templates (dashboard, history, settings)
    static/                  # CSS and JS assets
tests/                       # Unit tests (11 tests)
```

## Testing

```bash
python -m pytest tests/ -v
```

## Tech Stack

- **Computer Vision**: OpenCV (headless), NumPy, SciPy
- **Web**: FastAPI, Uvicorn, Jinja2, Chart.js
- **Storage**: SQLite (WAL mode)
- **Architecture**: Multi-threaded pipeline with thread-to-asyncio bridge for real-time WebSocket updates
