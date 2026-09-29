"""Windows camera enumeration: which cameras exist and their id numbers.

Windows has no `v4l2-ctl`/`/sys/class/video4linux` equivalent, so unlike Linux
this can't discover cameras by walking device nodes. `cv2_enumerate_cameras`
wraps the OS's own device enumeration (Media Foundation) and hands back the
same plain integer `index` that `cv2.VideoCapture(index, cv2.CAP_MSMF)`
expects to open that camera — the "id number" cameras are addressed by
everywhere else in this codebase (macOS and Linux included).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import cv2

try:
    from cv2_enumerate_cameras import enumerate_cameras
except Exception:
    enumerate_cameras = None


@dataclass(frozen=True)
class WindowsCameraInfo:
    index: int
    name: str
    vid: int | None
    pid: int | None
    backend: int


def _enumerate_windows_cameras() -> tuple[WindowsCameraInfo, ...]:
    if enumerate_cameras is None:
        return ()
    try:
        enumerated = enumerate_cameras(cv2.CAP_MSMF)
    except Exception:
        return ()

    cameras: list[WindowsCameraInfo] = []
    for camera in enumerated:
        vid = getattr(camera, "vid", None)
        pid = getattr(camera, "pid", None)
        cameras.append(
            WindowsCameraInfo(
                index=int(getattr(camera, "index", -1)),
                name=str(getattr(camera, "name", f"Camera {len(cameras)}")),
                vid=int(vid) if vid is not None else None,
                pid=int(pid) if pid is not None else None,
                backend=int(getattr(camera, "backend", cv2.CAP_MSMF)),
            )
        )
    return tuple(cameras)


@lru_cache(maxsize=1)
def enumerate_windows_cameras() -> tuple[WindowsCameraInfo, ...]:
    return _enumerate_windows_cameras()


_ENUM_TTL_S = 30.0
_last_enum_time: float = 0.0


def refresh_windows_cameras(*, force: bool = False) -> tuple[WindowsCameraInfo, ...]:
    """Re-enumerate at most once per `_ENUM_TTL_S`, like the macOS registry:
    plugging/unplugging a camera should show up without restarting the
    backend, but every settings-page poll doesn't need a fresh OS query."""
    global _last_enum_time
    import time as _time

    now = _time.monotonic()
    if force or (now - _last_enum_time) >= _ENUM_TTL_S:
        enumerate_windows_cameras.cache_clear()
        _last_enum_time = now
    return enumerate_windows_cameras()
