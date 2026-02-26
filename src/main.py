"""Entry point: CLI argument parsing + pipeline + uvicorn startup."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import uvicorn

from src.config import load_config
from src.pipeline import Pipeline
from src.web.app import create_app


def setup_logging(log_dir: str, verbose: bool = False) -> None:
    """Configure logging to both console and file."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path / "aerial_detect.log"),
        ],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nighttime Aerial Object Detection System"
    )
    parser.add_argument(
        "-c", "--config",
        default="config/default.yaml",
        help="Path to YAML config file (default: config/default.yaml)",
    )
    parser.add_argument(
        "-u", "--url",
        default=None,
        help="RTSP URL or video file path (overrides config)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Web server host (overrides config)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Web server port (overrides config)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Load configuration
    config = load_config(args.config)

    # Apply CLI overrides
    if args.url:
        config.capture.rtsp_url = args.url
    if args.host:
        config.web.host = args.host
    if args.port:
        config.web.port = args.port

    # Setup logging
    setup_logging(config.recording.log_dir, args.verbose)
    logger = logging.getLogger(__name__)
    logger.info("Starting Aerial Object Detection System")
    logger.info("Stream source: %s", config.capture.rtsp_url)
    logger.info("Web dashboard: http://%s:%d", config.web.host, config.web.port)

    # Ensure data directories exist
    Path(config.recording.clip_dir).mkdir(parents=True, exist_ok=True)
    Path(config.recording.db_path).parent.mkdir(parents=True, exist_ok=True)

    # Create pipeline and web app
    pipeline = Pipeline(config)
    app = create_app(pipeline)

    # Start pipeline before web server
    pipeline.start()

    try:
        # Set the asyncio event loop for thread-safe WebSocket publishing
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        pipeline.set_event_loop(loop)

        # Run uvicorn (blocks until shutdown)
        uvicorn.run(
            app,
            host=config.web.host,
            port=config.web.port,
            log_level="info",
            loop="asyncio",
        )
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        pipeline.stop()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
