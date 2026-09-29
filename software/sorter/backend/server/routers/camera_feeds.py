"""Router for the live camera feeds (MJPEG): a thumbnail of any camera by its
device index, for the camera picker, and each role's preview, annotated or raw,
whole or cropped to its channel as the dashboard shows it, shared by everyone
watching the same view.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import time
from typing import Any, Dict

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

import machine_toml
from server import shared_state
from server.routers.cameras import _camera_source_for_role, _open_camera_for_probe
from vision.camera_modes import list_v4l2_modes, preview_capture_mode
from vision.dashboard_crop import apply_dashboard_crop, dashboard_crop_spec
from vision.outputs.mjpeg import MjpegOutput

router = APIRouter()

logger = logging.getLogger(__name__)

# Max width of the MJPEG preview stream (annotated frame is downscaled to
# this before JPEG encoding). Annotation still runs at full capture resolution;
# this only shrinks the encoded-and-transmitted frame. 0 disables the resize.
PREVIEW_MAX_WIDTH = int(os.environ.get("SORTER_PREVIEW_MAX_WIDTH", "960"))

# At most this many preview frames a second per camera view, however fast
# perception runs: the budget for CPU on the machine and bandwidth to browsers.
PREVIEW_MAX_FPS = 10.0


# ---------------------------------------------------------------------------
# Camera picker thumbnails
# ---------------------------------------------------------------------------


def _open_camera_for_preview(index: int) -> cv2.VideoCapture:
    # Thumbnails are 426 px wide: open at a small MJPEG mode rather than the
    # camera's own resolution, which is 4K on a 4K camera.
    if platform.system() != "Linux":
        return _open_camera_for_probe(index)
    mode = preview_capture_mode(list_v4l2_modes(index))
    if mode is None:
        return _open_camera_for_probe(index)
    from vision.camera import _open_capture_source

    return _open_capture_source(
        index, width=mode["width"], height=mode["height"], fps=mode["fps"], fourcc="MJPG"
    )


def _device_capturing_index(index: int):
    """Return the camera-service device already capturing ``index``, if any.

    The picker streams cameras by device index. When that index is assigned to
    a role, the camera service's capture thread already holds /dev/videoN open;
    opening a second VideoCapture on it fights the live pipeline for frames and
    spikes USB/CPU. Reusing the running capture thread avoids the duplicate open.
    """
    service = shared_state.camera_service
    if service is None:
        return None
    seen: set[int] = set()
    for device in service.devices.values():
        if id(device) in seen:
            continue
        seen.add(id(device))
        try:
            if device.capture_thread.getCameraSource() == index:
                return device
        except Exception:
            continue
    return None


@router.get("/api/cameras/stream/{index}")
def camera_stream(index: int):
    """MJPEG thumbnail stream for a single camera by index.

    Served from the running capture thread when the index is already owned by a
    role; only falls back to a direct device open when nothing else holds it.
    """
    def _encode_thumb(frame: np.ndarray) -> bytes:
        thumb = cv2.resize(frame, (426, 240))
        ok, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 60])
        if not ok:
            return b""
        return (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
        )

    shared_device = _device_capturing_index(index)

    if shared_device is not None:
        def generate_shared():
            while True:
                frame_obj = shared_device.latest_frame
                if frame_obj is None or frame_obj.raw is None:
                    time.sleep(0.05)
                    continue
                chunk = _encode_thumb(frame_obj.raw)
                if chunk:
                    yield chunk
                time.sleep(0.1)

        return StreamingResponse(
            generate_shared(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    def generate_direct():
        cap = _open_camera_for_preview(index)
        if not cap.isOpened():
            return
        try:
            # A role that claims this camera needs it more than a picker
            # thumbnail does. A browser can keep a finished preview's stream
            # open, and its handle would stop the role's capture from starting.
            while _device_capturing_index(index) is None:
                ret, frame = cap.read()
                if not ret:
                    break
                chunk = _encode_thumb(frame)
                if chunk:
                    yield chunk
        finally:
            cap.release()

    return StreamingResponse(
        generate_direct(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# ---------------------------------------------------------------------------
# Role previews
# ---------------------------------------------------------------------------


class _SharedFeed:
    """One preview of a camera role (annotated or raw, dashboard crop or not).

    While anyone watches, a producer on the event loop renders and JPEG-encodes
    each new source frame once, in a worker thread, and every viewer streams
    those same bytes. A slow viewer skips frames instead of queueing them."""

    def __init__(self, role: str, annotated: bool, dashboard: bool) -> None:
        self.role = role
        self.annotated = annotated
        self.dashboard = dashboard
        self.viewers = 0
        self.chunk: bytes | None = None
        self.seq = 0
        self.fresh = asyncio.Event()
        self.producer: asyncio.Future | None = None
        self.last_ts: float | None = None
        # (frame size, crop spec, when computed): recomputed now and then so
        # zone edits show up in views that stay open.
        self.crop: tuple[tuple[int, int], Dict[str, Any] | None, float] | None = None

    def _render(self) -> bytes | None:
        """The newest frame as an MJPEG part, or None when nothing is new. Runs
        in a worker thread."""
        gc = shared_state.gc_ref
        ps = getattr(gc, "perception_service", None) if gc is not None else None
        channel_id = ps.channel_id_for_role(self.role) if ps is not None else None
        result = None
        if self.annotated and channel_id is not None:
            # Rendered at preview width once per inference frame, shared.
            result = ps.preview_frame(channel_id, PREVIEW_MAX_WIDTH)
        if result is None:
            # Annotations off, or perception not ready yet: raw pixels from the
            # same shared capture thread, never a VisionManager overlay.
            feed = shared_state.camera_service.get_feed(self.role) if shared_state.camera_service else None
            frame_obj = feed.get_frame(annotated=False) if feed is not None else None
            if frame_obj is None:
                return None
            result = (frame_obj.raw, frame_obj.timestamp)
        frame, frame_ts = result
        if frame_ts == self.last_ts:
            return None
        self.last_ts = frame_ts
        started = time.perf_counter()
        if PREVIEW_MAX_WIDTH > 0 and frame.shape[1] > PREVIEW_MAX_WIDTH:
            scale = PREVIEW_MAX_WIDTH / float(frame.shape[1])
            frame = cv2.resize(
                frame,
                (PREVIEW_MAX_WIDTH, int(round(frame.shape[0] * scale))),
                interpolation=cv2.INTER_AREA,
            )
        if self.dashboard:
            frame_h, frame_w = frame.shape[:2]
            if self.crop is None or self.crop[0] != (frame_w, frame_h) or time.monotonic() - self.crop[2] > 5.0:
                self.crop = ((frame_w, frame_h), dashboard_crop_spec(self.role, frame_w, frame_h), time.monotonic())
            frame = apply_dashboard_crop(frame, self.crop[1])
        chunk = MjpegOutput().encode_chunk(frame, quality=55)
        if gc is not None:
            gc.runtime_stats.observePerfMs(f"preview.{self.role}.frame_age_ms", max(0.0, (time.time() - float(frame_ts)) * 1000.0))
            gc.runtime_stats.observePerfMs(f"preview.{self.role}.encode_ms", (time.perf_counter() - started) * 1000.0)
            gc.runtime_stats.observePerfMs(f"preview.{self.role}.viewers", float(self.viewers))
        return chunk

    async def _produce(self) -> None:
        loop = asyncio.get_running_loop()
        self.crop = None
        while self.viewers:
            started = loop.time()
            try:
                chunk = await loop.run_in_executor(None, self._render)
            except Exception as exc:
                logger.warning(f"camera feed {self.role}: {exc}")
                await asyncio.sleep(1.0)
                continue
            if chunk is None:
                await asyncio.sleep(0.02)
                continue
            self.chunk = chunk
            self.seq += 1
            fresh, self.fresh = self.fresh, asyncio.Event()
            fresh.set()
            await asyncio.sleep(max(0.0, started + 1.0 / PREVIEW_MAX_FPS - loop.time()))
        self.chunk = None

    async def stream(self):
        self.viewers += 1
        if self.producer is None or self.producer.done():
            self.producer = asyncio.ensure_future(self._produce())
        try:
            seq = 0
            while True:
                if self.chunk is None or self.seq == seq:
                    await self.fresh.wait()
                    continue
                seq = self.seq
                yield self.chunk
        finally:
            self.viewers -= 1


_shared_feeds: Dict[tuple[str, bool, bool], _SharedFeed] = {}


@router.get("/api/cameras/feed/{role}")
def camera_feed_by_role(
    role: str,
    annotated: bool = True,
    layer: str = "annotated",
    dashboard: bool = False,
):
    """MJPEG stream for a camera role, shared by everyone watching the same view.

    ``layer`` controls annotation: ``"annotated"`` (default) or ``"raw"``.
    The legacy ``annotated`` bool param is supported for backward compat.
    """
    if _camera_source_for_role(machine_toml.read(), role) is None:
        raise HTTPException(404, f"Camera role '{role}' not configured")
    key = (role, layer == "annotated" and annotated, dashboard)
    feed = _shared_feeds.get(key) or _shared_feeds.setdefault(key, _SharedFeed(*key))
    return StreamingResponse(feed.stream(), media_type="multipart/x-mixed-replace; boundary=frame")
