# Aerial Object Detection

Nighttime aerial object detection system using classical computer vision. Connects to RTSP cameras or video files, detects and classifies moving objects in the sky (aircraft, satellites, UAPs), and serves a real-time web dashboard. Runs entirely on CPU with no deep learning dependencies.

## Features

- **Real-time RTSP streaming** with automatic reconnection
- **Multi-stage detection pipeline**: frame differencing + MOG2 background subtraction, morphological filtering, and contour analysis
- **Centroid-based object tracking** across frames with automatic track lifecycle management
- **Rule-based classification** using FFT blink analysis, trajectory linearity (R²), and speed/acceleration profiling to distinguish aircraft, satellites, and UAPs
- **Event recording** with pre/post-event buffered video clips and SQLite event logging
- **Web dashboard** with live MJPEG stream via WebSocket, detection history, and statistics (Chart.js)
- **Fully configurable** via YAML with CLI overrides

## Requirements

- Python 3.10+
- An RTSP camera or video file for input

## Installation

```bash
git clone https://github.com/your-username/Aerial-Object-Detection.git
cd Aerial-Object-Detection
pip install -e ".[dev]"
```

## Quick Start

```bash
# Run with an RTSP camera
python -m src.main -u rtsp://user:pass@192.168.1.100:554/stream1

# Run with a local video file
python -m src.main -u path/to/video.mp4

# Custom config, host, and port
python -m src.main -c config/default.yaml -u rtsp://... --host 0.0.0.0 --port 9090

# Verbose logging
python -m src.main -u rtsp://... -v
```

Once running, open `http://localhost:8080` in your browser to view the dashboard.

## Configuration

All parameters are in [config/default.yaml](config/default.yaml). Key sections:

| Section | Description |
|---|---|
| `capture` | RTSP URL, reconnect delay, grab timeout |
| `processing` | Resize dimensions, CLAHE, blur, frame skip |
| `detection` | Diff threshold, MOG2 params, morphology, contour filters |
| `tracking` | Max matching distance, disappear timeout, min track length |
| `classification` | FFT blink band, linearity threshold, speed ranges, acceleration variance |
| `recording` | Pre/post buffer duration, clip output dir, database path |
| `web` | Host, port, stream FPS and quality |

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
Classification (FFT blink + trajectory R² + speed analysis)
    |
    v
Recording (buffered clip writer + SQLite event logger)
    |
    v
Web Dashboard (FastAPI + WebSocket MJPEG stream)
```

**Classification rules:**

- **Aircraft**: Periodic blinking detected via FFT in the 0.5-3.0 Hz band, moderate trajectory linearity
- **Satellite**: Highly linear trajectory (R² > 0.85), constant speed in expected range, no blinking
- **UAP**: Erratic trajectory, high acceleration variance, does not match aircraft or satellite profiles

## Project Structure

```
config/default.yaml          # All tunable parameters
src/
  main.py                    # CLI entry point
  config.py                  # Dataclass-based YAML config loader
  pipeline.py                # Orchestrator (grab, process, record, serve)
  capture/stream.py          # Threaded RTSP frame grabber
  processing/
    preprocessor.py          # Resize, CLAHE, blur
    detector.py              # Frame diff + MOG2 + contour extraction
    tracker.py               # Centroid-based multi-object tracker
    classifier.py            # FFT blink + trajectory + speed classifier
  recording/
    models.py                # Data models (TrackedObject, ObjectClass)
    clip_writer.py           # Buffered MP4 clip writer
    event_logger.py          # SQLite event logger (WAL mode)
  web/
    app.py                   # FastAPI application factory
    routes.py                # HTTP routes
    websocket.py             # Live MJPEG stream over WebSocket
    templates/               # Jinja2 templates (dashboard, history, settings)
    static/                  # CSS and JS assets
tests/                       # Unit tests (19 tests)
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
