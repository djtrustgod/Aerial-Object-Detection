"""WebSocket endpoints for MJPEG stream and event notifications."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import cv2
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.pipeline import Pipeline

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections for both stream and event channels."""

    def __init__(self):
        self.stream_clients: list[WebSocket] = []
        self.event_clients: list[WebSocket] = []

    async def connect_stream(self, ws: WebSocket) -> None:
        await ws.accept()
        self.stream_clients.append(ws)
        logger.info("Stream client connected (%d total)", len(self.stream_clients))

    async def connect_events(self, ws: WebSocket) -> None:
        await ws.accept()
        self.event_clients.append(ws)
        logger.info("Event client connected (%d total)", len(self.event_clients))

    def disconnect_stream(self, ws: WebSocket) -> None:
        if ws in self.stream_clients:
            self.stream_clients.remove(ws)
        logger.info("Stream client disconnected (%d remaining)",
                     len(self.stream_clients))

    def disconnect_events(self, ws: WebSocket) -> None:
        if ws in self.event_clients:
            self.event_clients.remove(ws)

    async def broadcast_event(self, data: dict) -> None:
        """Broadcast a JSON event to all event clients."""
        message = json.dumps(data)
        disconnected = []
        for ws in self.event_clients:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            self.disconnect_events(ws)


def create_ws_router(pipeline: Pipeline) -> APIRouter:
    router = APIRouter()
    manager = ConnectionManager()

    # Register event callback on pipeline
    def on_event(data: dict):
        """Bridge from pipeline thread to asyncio event loop."""
        asyncio.ensure_future(manager.broadcast_event(data))

    pipeline.add_event_callback(on_event)

    @router.websocket("/ws/stream")
    async def ws_stream(ws: WebSocket):
        """MJPEG stream via WebSocket (binary frames)."""
        await manager.connect_stream(ws)
        quality = pipeline.config.web.stream_quality
        max_fps = pipeline.config.web.stream_fps
        frame_interval = 1.0 / max_fps

        try:
            while True:
                frame = pipeline.display_frame
                if frame is not None:
                    _, buffer = cv2.imencode(
                        ".jpg", frame,
                        [cv2.IMWRITE_JPEG_QUALITY, quality]
                    )
                    await ws.send_bytes(buffer.tobytes())

                await asyncio.sleep(frame_interval)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("Stream WebSocket error")
        finally:
            manager.disconnect_stream(ws)

    @router.websocket("/ws/events")
    async def ws_events(ws: WebSocket):
        """JSON event notifications via WebSocket."""
        await manager.connect_events(ws)
        try:
            while True:
                # Keep connection alive; events pushed via broadcast
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("Events WebSocket error")
        finally:
            manager.disconnect_events(ws)

    return router
