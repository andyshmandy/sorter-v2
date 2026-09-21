"""Record the sorter's camera feeds from another machine, for days at a time.

The sorter's backend re-serves each camera's own JPEG bytes
(``/api/recording/raw/{role}``), so tapping a feed costs the sorter nothing:
no extra USB traffic, no decode, no encode. This script does the decoding and
encoding wherever it runs, and writes, per role:

  <out>/<role>/clips/<role>-<UTC>.mp4        compressed video, 10-minute segments
  <out>/<role>/annotated/<role>-<UTC>.mp4    the same, with the live-feed overlay
  <out>/<role>/stills/<role>-<UTC>.jpg       the camera's own JPEG, full quality
  <out>/<role>/events.ndjson                 every inference event
  <out>/<role>/index.csv                     video frame -> source frame
  <out>/status.json                          machine-readable health, rewritten every 30 s
  <out>/recorder.log

Built to be left alone. Every stream reconnects with backoff, each role is
independent, ffmpeg is segmented by the segment muxer so a crash costs at most
the segment in progress, the encoder falls back from NVENC to libx264 by
itself, and it stops before filling the disk.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

import cv2
import numpy as np

STREAM_TIMEOUT_S = 15.0
RECONNECT_MIN_S = 1.0
# A dropped tap freezes the picture until it is back, because the video is
# paced by wall clock and repeats the last frame. Keep the ceiling low: a
# blip should cost a second of frozen video, not half a minute.
RECONNECT_MAX_S = 5.0
REPORT_EVERY_S = 300.0
STATUS_EVERY_S = 30.0
STALE_WARN_S = 15.0

_log_lock = threading.Lock()
_log_file = None


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with _log_lock:
        print(line, flush=True)
        if _log_file is not None:
            try:
                _log_file.write(line + "\n")
                _log_file.flush()
            except Exception:
                pass


def utc_name(ts: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts))


def utc_name_ms(ts: float) -> str:
    """For stills: the capture time to the millisecond, so a still can be matched exactly to
    the inference event for its frame when the overlay is re-drawn offline. Whole seconds
    were ambiguous by up to a second on a turning carousel."""
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(ts)) + f".{int((ts % 1) * 1000):03d}Z"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# overlay: a port of the backend's perception/overlay.py feed renderer
# ---------------------------------------------------------------------------


@dataclass
class Zones:
    frame_h: int
    frame_w: int
    overlay: np.ndarray
    mask: np.ndarray
    secondary: list[tuple[str, str, np.ndarray]]
    colors: dict[str, Any]
    fill_alpha: float
    picture: dict[str, Any]
    _scaled: dict[tuple[int, int], tuple[np.ndarray, np.ndarray, list[np.ndarray]]] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, p: dict[str, Any]) -> "Zones":
        def png(b64: str, flags: int) -> np.ndarray:
            img = cv2.imdecode(np.frombuffer(base64.b64decode(b64), np.uint8), flags)
            if img is None:
                raise ValueError("zone PNG failed to decode")
            return img

        h, w = p["frame_size"]
        return cls(
            frame_h=int(h),
            frame_w=int(w),
            overlay=png(p["overlay_png_b64"], cv2.IMREAD_COLOR),
            mask=png(p["mask_png_b64"], cv2.IMREAD_GRAYSCALE),
            secondary=[(z["id"], z["zone_type"], png(z["mask_png_b64"], cv2.IMREAD_GRAYSCALE)) for z in p.get("secondary", [])],
            colors=p["colors"],
            fill_alpha=float(p.get("zone_fill_alpha", 0.15)),
            picture=p.get("picture_settings") or {},
        )

    def arrays_for(self, h: int, w: int) -> tuple[np.ndarray, np.ndarray, list[np.ndarray]]:
        got = self._scaled.get((h, w))
        if got is not None:
            return got
        if (h, w) == self.overlay.shape[:2]:
            res = (self.overlay, self.mask, [m for _, _, m in self.secondary])
        else:
            res = (
                cv2.resize(self.overlay, (w, h), interpolation=cv2.INTER_NEAREST),
                cv2.resize(self.mask, (w, h), interpolation=cv2.INTER_NEAREST),
                [cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST) for _, _, m in self.secondary],
            )
        self._scaled[(h, w)] = res
        return res


def _c(color: Any) -> tuple[int, int, int]:
    return (int(color[0]), int(color[1]), int(color[2]))


def _scale_bbox(b: list[float], scale: float) -> tuple[int, int, int, int]:
    return (int(b[0] * scale), int(b[1] * scale), int(b[2] * scale), int(b[3] * scale))


def draw_overlay(img: np.ndarray, zones: Zones, event: Optional[dict[str, Any]]) -> None:
    """Composite the live-feed overlay in place: low-opacity zone fill, channel
    outline, secondary zone outlines, green on-channel boxes with track ids,
    merged boxes labelled below. Same colours and weights as the sorter's UI."""
    h, w = img.shape[:2]
    overlay, mask, sec_masks = zones.arrays_for(h, w)
    thick = 1
    zone_pixels = np.any(overlay != 0, axis=2)
    if zone_pixels.any():
        blended = img.copy()
        blended[zone_pixels] = overlay[zone_pixels]
        img[:] = cv2.addWeighted(blended, zones.fill_alpha, img, 1.0 - zones.fill_alpha, 0)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(img, contours, -1, _c(zones.colors["channel_outline"]), thick, cv2.LINE_AA)
    for (_, ztype, _), m in zip(zones.secondary, sec_masks):
        color = zones.colors["secondary_zone"].get(ztype, zones.colors["secondary_zone_default"])
        cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if cs:
            cv2.drawContours(img, cs, -1, _c(color), max(1, thick - 1), cv2.LINE_AA)
    if not event:
        return
    scale = w / float(zones.frame_w) if zones.frame_w else 1.0
    on_color = _c(zones.colors["on_channel"])
    for d in event.get("detections") or []:
        if not d.get("in_primary") and d.get("secondary_zone_ids"):
            x1, y1, x2, y2 = _scale_bbox(d["bbox"], scale)
            cv2.rectangle(img, (x1, y1), (x2, y2), _c(zones.colors["secondary_detection"]), thick, cv2.LINE_AA)
    for b in event.get("on_bboxes") or []:
        x1, y1, x2, y2 = _scale_bbox(b, scale)
        cv2.rectangle(img, (x1, y1), (x2, y2), on_color, thick, cv2.LINE_AA)
    font = cv2.FONT_HERSHEY_SIMPLEX
    fs = 0.4
    for d in event.get("detections") or []:
        tid = d.get("track_id")
        if tid is None:
            continue
        x1, y1, _, _ = _scale_bbox(d["bbox"], scale)
        label = f"#{int(tid)}"
        (tw, th), _ = cv2.getTextSize(label, font, fs, thick)
        ty = max(th + 2, y1 - 2)
        cv2.rectangle(img, (x1, ty - th - 2), (x1 + tw + 2, ty + 2), (0, 0, 0), -1)
        cv2.putText(img, label, (x1 + 1, ty), font, fs, on_color, thick, cv2.LINE_AA)
    merged_color = _c(zones.colors["merged"])
    ids = event.get("merged_track_ids") or []
    for i, b in enumerate(event.get("merged_bboxes") or []):
        x1, y1, x2, y2 = _scale_bbox(b, scale)
        cv2.rectangle(img, (x1, y1), (x2, y2), merged_color, max(2, thick + 1), cv2.LINE_AA)
        tid = ids[i] if i < len(ids) else None
        label = f"merged #{int(tid)}" if tid is not None else "merged"
        (tw, th), _ = cv2.getTextSize(label, font, fs, thick)
        ty = min(h - 2, y2 + th + 3)
        cv2.rectangle(img, (x1, ty - th - 2), (x1 + tw + 2, ty + 2), (0, 0, 0), -1)
        cv2.putText(img, label, (x1 + 1, ty), font, fs, merged_color, thick, cv2.LINE_AA)


def apply_picture(img: np.ndarray, picture: dict[str, Any]) -> np.ndarray:
    """The sorter rotates and flips a frame before perception sees it, so the
    overlay's coordinates are in that geometry. Match it."""
    rot = int(picture.get("rotation", 0) or 0)
    if rot == 90:
        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    elif rot == 180:
        img = cv2.rotate(img, cv2.ROTATE_180)
    elif rot == 270:
        img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if picture.get("flip_horizontal"):
        img = cv2.flip(img, 1)
    if picture.get("flip_vertical"):
        img = cv2.flip(img, 0)
    return img


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------


def _open(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "*/*"}), timeout=STREAM_TIMEOUT_S)


def fetch_json(url: str) -> dict[str, Any]:
    with _open(url) as resp:
        return json.loads(resp.read().decode())


def iter_multipart(resp, boundary: bytes = b"--frame") -> Iterator[tuple[dict[str, str], bytes]]:
    """Yield ``(headers, body)`` per part of a multipart/x-mixed-replace body."""
    while True:
        line = resp.readline()
        if not line:
            return
        if not line.startswith(boundary):
            continue
        headers: dict[str, str] = {}
        while True:
            hl = resp.readline()
            if not hl:
                return
            hl = hl.strip()
            if not hl:
                break
            k, _, v = hl.decode("latin-1").partition(":")
            headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0") or 0)
        body = b""
        while len(body) < length:
            chunk = resp.read(length - len(body))
            if not chunk:
                return
            body += chunk
        yield headers, body


class Backoff:
    def __init__(self) -> None:
        self.delay = RECONNECT_MIN_S

    def wait(self, stop: threading.Event) -> None:
        stop.wait(self.delay)
        self.delay = min(RECONNECT_MAX_S, self.delay * 1.7)

    def reset(self) -> None:
        self.delay = RECONNECT_MIN_S


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------


def encoder_args(encoder: str) -> list[str]:
    if encoder == "hevc_nvenc":
        return ["-c:v", "hevc_nvenc", "-preset", "p4", "-tune", "hq", "-rc", "vbr"]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq", "-rc", "vbr"]
    if encoder == "libx265":
        return ["-c:v", "libx265", "-preset", "veryfast"]
    return ["-c:v", "libx264", "-preset", "veryfast"]


def start_ffmpeg(out_dir: str, role: str, w: int, h: int, fps: float, kbps: int, encoder: str, segment_s: int) -> subprocess.Popen:
    """Raw BGR in on stdin, time-segmented MP4s out. The segment muxer closes
    and opens each file itself, so a kill costs at most the segment in
    progress and every finished clip is independently playable."""
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, f"{role}-%Y%m%dT%H%M%SZ.mp4")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", f"{fps}", "-i", "pipe:0",
        *encoder_args(encoder),
        "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 3}k",
        "-g", str(max(1, int(round(fps * 4)))), "-pix_fmt", "yuv420p",
        "-f", "segment", "-segment_time", str(segment_s), "-segment_format", "mp4",
        # Fragmented MP4: every segment stays playable even if ffmpeg is killed mid-file.
        # A stalled encoder on 2026-09-21 left three 15-minute files with no index.
        "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
        "-reset_timestamps", "1", "-strftime", "1", pattern,
    ]
    env = dict(os.environ, TZ="UTC")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)


def start_ffmpeg_from_url(url: str, out_dir: str, role: str, kbps: int, encoder: str, segment_s: int, fps: float) -> subprocess.Popen:
    """Encode straight from the sorter's tap, with no Python in the path: the
    tap already serves ``multipart/x-mixed-replace`` JPEGs, which is what
    ffmpeg's mpjpeg demuxer reads. ffmpeg does the decode in C and never copies
    a raw frame through a pipe, which is most of the cost of recording a 1440p
    feed. Only the overlay stream needs frames in Python."""
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, f"{role}-%Y%m%dT%H%M%SZ.mp4")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-rw_timeout", str(int(STREAM_TIMEOUT_S * 1e6)),
        "-f", "mpjpeg", "-r", f"{fps}", "-i", url,
        *encoder_args(encoder),
        "-r", f"{fps}",
        "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 3}k",
        "-g", str(max(1, int(round(fps * 4)))), "-pix_fmt", "yuv420p",
        "-f", "segment", "-segment_time", str(segment_s), "-segment_format", "mp4",
        # Fragmented MP4: every segment stays playable even if ffmpeg is killed mid-file.
        # A stalled encoder on 2026-09-21 left three 15-minute files with no index.
        "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
        "-reset_timestamps", "1", "-strftime", "1", "-progress", "pipe:1", pattern,
    ]
    env = dict(os.environ, TZ="UTC")
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def ffmpeg_has_encoder(name: str) -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, timeout=20).stdout.decode()
        return f" {name} " in out
    except Exception:
        return False


def nvenc_works(w: int = 1280, h: int = 720, encoder: str = "hevc_nvenc") -> bool:
    """A two-second encode of a test pattern. NVENC can be present but refuse
    to open a session when another process holds the card's memory, and that
    failure only shows at open time."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", f"testsrc=size={w}x{h}:rate=5", "-t", "1", *encoder_args(encoder), "-f", "null", "-"],
            capture_output=True, timeout=60,
        )
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# per-role recorder
# ---------------------------------------------------------------------------


@dataclass
class RoleStats:
    frames_received: int = 0
    bytes_received: int = 0
    upstream_drops: int = 0
    stills: int = 0
    events: int = 0
    video_frames: int = 0
    held_frames: int = 0
    reconnects: int = 0
    encoder_restarts: int = 0
    first_frame_ts: Optional[float] = None
    last_frame_ts: Optional[float] = None
    last_frame_at: float = 0.0
    size: Optional[tuple[int, int]] = None


class RoleRecorder:
    def __init__(self, args: argparse.Namespace, role: str, stop: threading.Event) -> None:
        self.args = args
        self.role = role
        self.stop = stop
        self.dir = os.path.join(args.out, role)
        self.clips_dir = os.path.join(self.dir, "clips")
        self.annotated_dir = os.path.join(self.dir, "annotated")
        self.stills_dir = os.path.join(self.dir, "stills")
        for d in (self.clips_dir, self.stills_dir):
            os.makedirs(d, exist_ok=True)
        self.stats = RoleStats()
        self.zones: Optional[Zones] = None
        self.latest: Optional[tuple[bytes, float, int]] = None
        self.latest_lock = threading.Lock()
        self.latest_event: Optional[dict[str, Any]] = None
        self.picture: dict[str, Any] = {}
        self.threads: list[threading.Thread] = []
        self.want_clips = "clips" in args.video
        self.want_annotated = "annotated" in args.video
        self.encoder = args.encoder
        self.preferred_encoder = args.encoder
        self.encoder_retry_at = 0.0
        self.clips_encoder = args.encoder
        self.clips_last_at = 0.0
        self.capture_size: Optional[tuple[int, int]] = None
        self.errors: list[str] = []
        if self.want_annotated:
            os.makedirs(self.annotated_dir, exist_ok=True)

    # -- setup -------------------------------------------------------------

    def fetch_zones(self) -> None:
        try:
            self.zones = Zones.from_payload(fetch_json(f"{self.args.backend}/api/recording/zones/{self.role}"))
            self.picture = self.zones.picture
            log(f"[{self.role}] zones: {self.zones.frame_w}x{self.zones.frame_h}, {len(self.zones.secondary)} secondary, picture={self.picture}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # No perception channel for this role (the feeder has none): it
                # can never have an overlay, so record only the plain stream.
                if self.want_annotated:
                    log(f"[{self.role}] no perception channel: recording clips and stills only")
                self.want_annotated = False
                self._fetch_picture()
                return
            log(f"[{self.role}] zones unavailable ({e.code}); the annotated stream waits for them")
            self._fetch_picture()
        except Exception as e:  # noqa: BLE001 - sorter down or still booting: retry later
            log(f"[{self.role}] zones not available yet ({e!r}); the annotated stream waits for them")
            self._fetch_picture()

    def _fetch_picture(self) -> None:
        try:
            for r in fetch_json(f"{self.args.backend}/api/recording/status").get("roles", []):
                if r.get("role") == self.role:
                    self.picture = r.get("picture_settings") or {}
        except Exception:
            pass

    def start(self) -> None:
        targets: list[tuple[Any, str]] = []
        if self.want_clips:
            targets.append((self.clips_loop, "clips"))
        # The Python tap is only needed for stills and for the overlay stream.
        if self.want_annotated or self.args.still_every < 1e11:
            targets.append((self.raw_loop, "tap"))
        if self.want_annotated:
            targets.append((self.events_loop, "events"))
            targets.append((self.video_loop, "overlay"))
        for target, name in targets:
            t = threading.Thread(target=target, name=f"{self.role}-{name}", daemon=True)
            t.start()
            self.threads.append(t)

    def join(self, timeout: float = 90.0) -> None:
        for t in self.threads:
            t.join(timeout=timeout)

    # -- raw stream + stills -----------------------------------------------

    def tap_fps(self) -> float:
        """What the Python side actually consumes: the overlay rate, or just
        enough for the stills when there is no overlay. Asking the sorter for
        this rate means the frames we would drop are never sent at all."""
        if self.want_annotated:
            return float(self.args.fps)
        return max(0.2, min(2.0, 1.0 / max(self.args.still_every, 1.0) * 2))

    def clips_loop(self) -> None:
        """Supervise one ffmpeg that encodes the plain stream straight from the
        tap. If it exits for any reason (the sorter restarting, the camera
        going away, the encoder refusing a session) it is restarted with
        backoff, and the segment muxer means only the file in progress is
        affected."""
        url = f"{self.args.backend}/api/recording/raw/{self.role}?fps={self.args.fps:g}"
        backoff = Backoff()
        while not self.stop.is_set():
            mode = self.capture_size or (1280, 720)
            kbps = self._kbps(mode[0], annotated=False)
            encoder = self.encoder
            proc = start_ffmpeg_from_url(
                url, self.clips_dir, self.role, kbps, encoder, self.args.segment_seconds, float(self.args.fps)
            )
            self.clips_encoder = encoder
            self.clips_last_at = 0.0
            started_at = time.time()
            progress_thread = threading.Thread(target=self.readProgress, args=(proc,), daemon=True)
            progress_thread.start()
            log(f"[{self.role}] plain stream: ffmpeg from the tap at {self.args.fps:g} fps, {kbps} kbps, {encoder}")
            while not self.stop.is_set() and proc.poll() is None:
                if time.time() - max(started_at, self.clips_last_at) > STREAM_TIMEOUT_S * 2:
                    log(f"[{self.role}] plain encoder stalled; restarting")
                    proc.terminate()
                    break
                self.stop.wait(1.0)
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            progress_thread.join(timeout=1)
            if self.stop.is_set():
                return
            err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
            log(f"[{self.role}] plain stream ffmpeg exited rc={proc.returncode}: {err[-250:]}")
            self.errors.append(f"clips: {err[-200:]}")
            self.stats.encoder_restarts += 1
            if encoder != "libx264" and ("OpenEncodeSession" in err or "No capable devices" in err or "out of memory" in err.lower()):
                log(f"[{self.role}] plain stream falling back to libx264 (no GPU session available)")
                self.encoder = "libx264"
                self.encoder_retry_at = time.monotonic() + self.args.encoder_retry_seconds
            backoff.wait(self.stop)

    def readProgress(self, proc: subprocess.Popen) -> None:
        last_frame = 0
        for line in proc.stdout:
            if line.startswith(b"frame="):
                frame = int(line.partition(b"=")[2])
                if frame > last_frame:
                    self.clips_last_at = time.time()
                    last_frame = frame

    def raw_loop(self) -> None:
        url = f"{self.args.backend}/api/recording/raw/{self.role}?fps={self.tap_fps():g}"
        backoff = Backoff()
        last_still = -1e9
        while not self.stop.is_set():
            try:
                with _open(url) as resp:
                    log(f"[{self.role}] raw stream connected")
                    backoff.reset()
                    for headers, body in iter_multipart(resp):
                        if self.stop.is_set():
                            break
                        if headers.get("content-type", "") != "image/jpeg" or not body:
                            continue
                        ts = float(headers.get("x-timestamp", time.time()))
                        seq = int(headers.get("x-seq", "0") or 0)
                        st = self.stats
                        st.frames_received += 1
                        st.bytes_received += len(body)
                        st.upstream_drops = int(headers.get("x-dropped", "0") or 0)
                        st.last_frame_ts = ts
                        st.last_frame_at = time.time()
                        if st.first_frame_ts is None:
                            st.first_frame_ts = ts
                        with self.latest_lock:
                            self.latest = (body, ts, seq)
                        if ts - last_still >= self.args.still_every:
                            last_still = ts
                            path = os.path.join(self.stills_dir, f"{self.role}-{utc_name_ms(ts)}.jpg")
                            tmp = path + ".part"
                            try:
                                with open(tmp, "wb") as f:
                                    f.write(body)
                                os.replace(tmp, path)
                                st.stills += 1
                            except OSError as e:
                                log(f"[{self.role}] still write failed: {e!r}")
            except Exception as e:  # noqa: BLE001 - any network failure: reconnect
                if self.stop.is_set():
                    break
                self.stats.reconnects += 1
                log(f"[{self.role}] raw stream: {e!r}; retry in {backoff.delay:.0f}s")
                backoff.wait(self.stop)

    # -- events stream -----------------------------------------------------

    def events_loop(self) -> None:
        url = f"{self.args.backend}/api/recording/events/{self.role}"
        path = os.path.join(self.dir, "events.ndjson")
        backoff = Backoff()
        while not self.stop.is_set():
            if self.zones is None:
                self.fetch_zones()
                if not self.want_annotated:
                    return
                if self.zones is None:
                    backoff.wait(self.stop)
                    continue
            try:
                with _open(url) as resp, open(path, "a") as out:
                    log(f"[{self.role}] events stream connected")
                    backoff.reset()
                    for line in resp:
                        if self.stop.is_set():
                            break
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        kind = ev.get("type")
                        if kind == "infer":
                            self.latest_event = ev
                            self.stats.events += 1
                        if kind in ("infer", "header"):
                            out.write(line.decode() if isinstance(line, bytes) else line)
            except Exception as e:  # noqa: BLE001
                if self.stop.is_set():
                    break
                self.stats.reconnects += 1
                log(f"[{self.role}] events stream: {e!r}; retry in {backoff.delay:.0f}s")
                backoff.wait(self.stop)

    # -- video -------------------------------------------------------------

    def _kbps(self, w: int, annotated: bool) -> int:
        big = w >= 2000
        if annotated:
            return self.args.annotated_kbps_big if big else self.args.annotated_kbps_small
        return self.args.kbps_big if big else self.args.kbps_small

    def _out_dir(self, name: str) -> str:
        return self.annotated_dir if name == "annotated" else self.clips_dir

    def video_loop(self) -> None:
        fps = float(self.args.fps)
        period = 1.0 / fps
        procs: dict[str, subprocess.Popen] = {}
        index_f = open(os.path.join(self.dir, "index.csv"), "a", newline="")
        index = csv.writer(index_f)
        if index_f.tell() == 0:
            index.writerow(["wall_ts", "source_ts", "source_seq", "held", "w", "h"])
        last_seq = -1
        last_bgr: Optional[np.ndarray] = None
        size: Optional[tuple[int, int]] = None
        next_tick = time.monotonic()
        wrote = 0

        def close_procs() -> None:
            for name, proc in list(procs.items()):
                try:
                    if proc.stdin:
                        proc.stdin.close()
                    proc.wait(timeout=60)
                except Exception as e:  # noqa: BLE001
                    log(f"[{self.role}] ffmpeg {name} did not exit cleanly: {e!r}")
                    proc.kill()
            procs.clear()

        try:
            while not self.stop.is_set():
                now = time.monotonic()
                if now < next_tick:
                    self.stop.wait(min(period, next_tick - now))
                    continue
                next_tick += period
                if next_tick < time.monotonic() - 2 * period:
                    next_tick = time.monotonic() + period
                with self.latest_lock:
                    latest = self.latest
                if latest is None or time.time() - self.stats.last_frame_at > STALE_WARN_S:
                    if procs:
                        log(f"[{self.role}] source stalled; closing annotated clip until frames return")
                        close_procs()
                        self.latest_event = None
                    last_seq = -1
                    continue
                jpeg, src_ts, seq = latest
                held = (src_ts, seq) == last_seq
                if not held:
                    bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                    if bgr is None:
                        continue
                    last_bgr = apply_picture(bgr, self.picture)
                    last_seq = (src_ts, seq)
                if last_bgr is None:
                    continue
                if held:
                    self.stats.held_frames += 1
                h, w = last_bgr.shape[:2]
                if size != (w, h):
                    # First frame, or the camera came back at a different size
                    # (a USB hub reset does that): new files, never a mixed one.
                    close_procs()
                    size = (w, h)
                    self.stats.size = size
                    log(f"[{self.role}] video {w}x{h} @ {fps:g} fps, encoder={self.encoder}")
                if (
                    self.encoder != self.preferred_encoder
                    and self.encoder_retry_at
                    and time.monotonic() >= self.encoder_retry_at
                ):
                    if nvenc_works(w, h, self.preferred_encoder):
                        log(f"[{self.role}] {self.preferred_encoder} is available again; switching back")
                        self.encoder = self.preferred_encoder
                        self.encoder_retry_at = 0.0
                        close_procs()
                        continue
                    self.encoder_retry_at = time.monotonic() + self.args.encoder_retry_seconds
                for name, want in (("annotated", self.want_annotated and self.zones is not None),):
                    if not want or name in procs:
                        continue
                    procs[name] = start_ffmpeg(
                        self._out_dir(name), self.role, w, h, fps,
                        self._kbps(w, name == "annotated"), self.encoder, self.args.segment_seconds,
                    )
                dead = [n for n, pr in procs.items() if pr.poll() is not None]
                for name in dead:
                    proc = procs.pop(name)
                    err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
                    log(f"[{self.role}] ffmpeg {name} exited rc={proc.returncode}: {err[-300:]}")
                    self.errors.append(f"{name}: {err[-200:]}")
                    self.stats.encoder_restarts += 1
                    if self.encoder != "libx264":
                        # The GPU encoder refused a session (another process may
                        # hold the card). Drop everything to the CPU encoder and
                        # let the next tick restart both streams together. Try
                        # the GPU again later: whatever held the card may let go,
                        # and the CPU encoder is the expensive way to do this.
                        log(f"[{self.role}] switching to libx264")
                        self.encoder = "libx264"
                        self.encoder_retry_at = time.monotonic() + self.args.encoder_retry_seconds
                        close_procs()
                        break
                if not procs:
                    continue
                for name, proc in list(procs.items()):
                    frame = last_bgr
                    if name == "annotated":
                        frame = last_bgr.copy()
                        draw_overlay(frame, self.zones, self.latest_event)  # type: ignore[arg-type]
                    try:
                        proc.stdin.write(frame.tobytes())  # type: ignore[union-attr]
                    except (BrokenPipeError, OSError):
                        pass
                self.stats.video_frames += 1
                wrote += 1
                index.writerow([f"{time.time():.3f}", f"{src_ts:.3f}", seq, int(held), w, h])
                if wrote % int(max(1, fps * 60)) == 0:
                    index_f.flush()
        finally:
            index_f.close()
            close_procs()

    # -- reporting ---------------------------------------------------------

    def disk_bytes(self) -> int:
        total = 0
        for root, _, files in os.walk(self.dir):
            for n in files:
                try:
                    total += os.path.getsize(os.path.join(root, n))
                except OSError:
                    pass
        return total

    def status(self) -> dict[str, Any]:
        st = self.stats
        age = time.time() - st.last_frame_at if st.last_frame_at else None
        clips_age = time.time() - self.clips_last_at if self.clips_last_at else None
        return {
            "role": self.role,
            "healthy": age is not None and age < STALE_WARN_S and (
                not self.want_clips or clips_age is not None and clips_age < STALE_WARN_S
            ),
            "seconds_since_clip_progress": round(clips_age, 1) if clips_age is not None else None,
            "seconds_since_frame": round(age, 1) if age is not None else None,
            "frames_received": st.frames_received,
            "bytes_received": st.bytes_received,
            "upstream_drops": st.upstream_drops,
            "stills": st.stills,
            "events": st.events,
            "video_frames": st.video_frames,
            "held_frames": st.held_frames,
            "reconnects": st.reconnects,
            "encoder_restarts": st.encoder_restarts,
            "encoder": self.encoder,
            "clips_encoder": self.clips_encoder,
            "size": list(st.size) if st.size else None,
            "clips": len([n for n in os.listdir(self.clips_dir) if n.endswith(".mp4")]) if os.path.isdir(self.clips_dir) else 0,
            "annotated_clips": len([n for n in os.listdir(self.annotated_dir) if n.endswith(".mp4")]) if os.path.isdir(self.annotated_dir) else 0,
            "disk_bytes": self.disk_bytes(),
            "errors": self.errors[-5:],
        }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", required=True, help="sorter backend base URL, e.g. http://192.168.2.3:8000")
    p.add_argument("--out", required=True, help="output root; one folder per role underneath")
    p.add_argument("--roles", default="", help="comma-separated roles (default: every role with a raw tap)")
    p.add_argument("--hours", type=float, default=0.0, help="stop after this long (0 = run until stopped)")
    p.add_argument("--fps", type=float, default=5.0, help="video frame rate, wall-clock paced")
    p.add_argument("--still-every", type=float, default=20.0, help="seconds between full-quality stills (0 = none)")
    p.add_argument("--video", default="clips,annotated", help="which video streams: clips, annotated, or both")
    p.add_argument("--encoder", default="auto", help="hevc_nvenc, h264_nvenc, libx265, libx264, or auto")
    p.add_argument("--segment-seconds", type=int, default=600, help="length of each video file")
    p.add_argument("--encoder-retry-seconds", type=float, default=600.0,
                   help="after falling back to the CPU encoder, how often to retry the GPU one")
    p.add_argument("--kbps-big", type=int, default=400)
    p.add_argument("--kbps-small", type=int, default=120)
    p.add_argument("--annotated-kbps-big", type=int, default=600)
    p.add_argument("--annotated-kbps-small", type=int, default=200)
    p.add_argument("--max-gb", type=float, default=0.0, help="stop when this output exceeds N GB (0 = no cap)")
    p.add_argument("--min-free-gb", type=float, default=20.0, help="stop when the disk has less than this free")
    a = p.parse_args(argv)
    a.video = [v.strip() for v in a.video.split(",") if v.strip()]
    a.roles = [r.strip() for r in a.roles.split(",") if r.strip()]
    if a.still_every <= 0:
        a.still_every = 1e12
    return a


def discover_roles(backend: str) -> list[str]:
    """One role per PHYSICAL camera. Several roles can name the same device
    (carousel and classification_channel are aliases) and a role with no camera
    configured falls back to another one's device, so both would record the
    same picture twice. Deduplicate on the device index from the camera config,
    and drop any role that has no device of its own."""
    data = fetch_json(f"{backend}/api/recording/status")
    available = [r["role"] for r in data.get("roles", []) if r.get("raw_bytes_available")]
    try:
        config = fetch_json(f"{backend}/api/cameras/config")
    except Exception:
        return available
    roles: list[str] = []
    seen: dict[Any, str] = {}
    for role in available:
        source = config.get(role)
        if source is None:
            log(f"skipping '{role}': no camera configured for it")
            continue
        if source in seen:
            log(f"skipping '{role}': the same camera as '{seen[source]}' (device {source})")
            continue
        seen[source] = role
        roles.append(role)
    return roles


def main(argv: Optional[list[str]] = None) -> int:
    global _log_file
    args = parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    _log_file = open(os.path.join(args.out, "recorder.log"), "a")

    if not args.roles:
        for attempt in range(30):
            try:
                args.roles = discover_roles(args.backend)
                break
            except Exception as e:  # noqa: BLE001 - the sorter may still be booting
                log(f"waiting for the sorter to answer ({e!r})")
                time.sleep(10)
        if not args.roles:
            log("no roles available; nothing to record")
            return 1

    if args.encoder == "auto":
        for candidate in ("hevc_nvenc", "libx264"):
            if candidate.endswith("nvenc"):
                if ffmpeg_has_encoder(candidate) and nvenc_works(encoder=candidate):
                    args.encoder = candidate
                    break
            else:
                args.encoder = candidate
                break
        log(f"encoder: {args.encoder}")

    log(f"recording {args.roles} from {args.backend} into {args.out}: {args.fps:g} fps, "
        f"still every {args.still_every if args.still_every < 1e11 else 0:g}s, "
        f"{args.segment_seconds}s segments, streams={args.video}, encoder={args.encoder}")

    stop = threading.Event()

    def _sig(signum, _frame):
        log(f"signal {signum}: finishing the clips in progress")
        stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    sizes: dict[str, tuple[int, int]] = {}
    for role in args.roles:
        # The mode a camera was ASKED for is not always the one it delivers, and
        # the encoders cost what the frames actually are. Prefer the live size
        # and only fall back to the requested one.
        try:
            modes = fetch_json(f"{args.backend}/api/cameras/capture-modes/{role}")
            for key in ("live", "current"):
                m = modes.get(key) or {}
                if m.get("width") and m.get("height"):
                    sizes[role] = (int(m["width"]), int(m["height"]))
                    break
        except Exception:
            pass

    # Only three GPU encode sessions exist on a consumer card, and they are
    # taken in the order the encoders start. Start the biggest frames first so
    # the GPU does the expensive work and the CPU is left the small feeds.
    args.roles.sort(key=lambda r: -(sizes.get(r, (0, 0))[0] * sizes.get(r, (0, 0))[1]))
    log("start order by frame size: " + ", ".join(f"{r}={sizes.get(r, ('?', '?'))[0]}x{sizes.get(r, ('?', '?'))[1]}" for r in args.roles))

    recorders = []
    for role in args.roles:
        rec = RoleRecorder(args, role, stop)
        rec.capture_size = sizes.get(role)
        rec.fetch_zones()
        rec.start()
        recorders.append(rec)
        # A moment between roles so the GPU sessions are claimed in size order
        # rather than by whichever thread happens to reach ffmpeg first.
        time.sleep(3.0)

    started = time.monotonic()
    deadline = started + args.hours * 3600 if args.hours > 0 else None
    next_report = started + REPORT_EVERY_S
    next_status = started + 5

    while not stop.is_set():
        stop.wait(1.0)
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            log("duration reached")
            break
        if now >= next_status:
            next_status = now + STATUS_EVERY_S
            statuses = [r.status() for r in recorders]
            total = sum(s["disk_bytes"] for s in statuses)
            free = shutil.disk_usage(args.out).free
            elapsed_h = (now - started) / 3600
            payload = {
                "updated_at": time.time(),
                "backend": args.backend,
                "out": args.out,
                "elapsed_hours": round(elapsed_h, 3),
                "disk_bytes": total,
                "disk_free_bytes": free,
                "per_hour_bytes": int(total / elapsed_h) if elapsed_h > 0.01 else None,
                "roles": statuses,
            }
            tmp = os.path.join(args.out, "status.json.part")
            try:
                with open(tmp, "w") as f:
                    json.dump(payload, f, indent=1)
                os.replace(tmp, os.path.join(args.out, "status.json"))
            except OSError:
                pass
            if free < args.min_free_gb * 1e9:
                log(f"disk nearly full ({human(free)} free < {args.min_free_gb} GB): stopping")
                break
            if args.max_gb > 0 and total > args.max_gb * 1e9:
                log(f"output cap reached ({human(total)} > {args.max_gb} GB): stopping")
                break
            for s in statuses:
                if not s["healthy"] and s["seconds_since_frame"] is not None:
                    log(f"[{s['role']}] no frames for {s['seconds_since_frame']:.0f}s")
        if now >= next_report:
            next_report = now + REPORT_EVERY_S
            elapsed_h = (now - started) / 3600
            total = 0
            for r in recorders:
                s = r.status()
                total += s["disk_bytes"]
                log(f"[{r.role}] {s['frames_received']} frames, video {s['video_frames']} (held {s['held_frames']}), "
                    f"{s['clips']} clips, {s['stills']} stills, {s['events']} events, "
                    f"drops {s['upstream_drops']}, reconnects {s['reconnects']}, on disk {human(s['disk_bytes'])}")
            if elapsed_h > 0.01:
                log(f"total {human(total)} in {elapsed_h:.2f} h -> {human(total / elapsed_h)}/h, {human(total / elapsed_h * 12)} per 12 h")

    stop.set()
    for r in recorders:
        r.join()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
