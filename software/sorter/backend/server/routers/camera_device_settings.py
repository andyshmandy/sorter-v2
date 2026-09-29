"""Router for a camera role's own controls: the UVC controls of a USB camera,
or the settings of the Android camera app when the role's camera is a phone.
Read them, preview a change live, save them, reset them to automatic, and
compare what is saved with what the camera reports.

Camera calibration (server/routers/camera_calibration.py) drives a camera
through the read, preview and save routes here.
"""

from __future__ import annotations

import json
import platform
from typing import Any, Dict, List
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from fastapi import APIRouter, HTTPException

import machine_toml
from irl.config import cameraDeviceSettingsToDict, cameraSettingsForRole, parseCameraDeviceSettings
from server import shared_state
from server.routers.cameras import _camera_source_for_role, _settings_role

router = APIRouter()


def _get_camera_device_settings_table(config: Dict[str, Any]) -> Dict[str, Any]:
    device_settings = config.get("camera_device_settings", {})
    return device_settings if isinstance(device_settings, dict) else {}


def _saved_camera_device_settings(config: Dict[str, Any], role: str) -> Dict[str, int | float | bool]:
    return cameraDeviceSettingsToDict(
        parseCameraDeviceSettings(cameraSettingsForRole(_get_camera_device_settings_table(config), role))
    )


def _assigned_camera_source(role: str) -> int | str:
    source = _camera_source_for_role(machine_toml.read(), role)
    if source is None:
        raise HTTPException(status_code=404, detail="No camera is assigned to this role.")
    return source


# ---------------------------------------------------------------------------
# Android camera app
# ---------------------------------------------------------------------------


def _android_camera_base_url(source: int | str | None) -> str | None:
    if not isinstance(source, str):
        return None
    try:
        parsed = urllib_parse.urlparse(source)
    except Exception:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _android_camera_request(
    source: int | str | None,
    path: str,
    *,
    method: str = "GET",
    payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    base_url = _android_camera_base_url(source)
    if base_url is None:
        raise HTTPException(status_code=400, detail="Camera source is not an Android camera app URL.")

    url = f"{base_url}{path}"
    data = None
    headers: Dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib_request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib_request.urlopen(request, timeout=4) as response:
            body = response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=detail or f"Android camera app returned HTTP {exc.code}.")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to reach Android camera app: {exc}")

    try:
        parsed = json.loads(body)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Android camera app returned invalid JSON: {exc}")

    if not isinstance(parsed, dict):
        raise HTTPException(status_code=502, detail="Android camera app returned an unexpected response.")

    return parsed


def _android_camera_bytes_request(source: int | str | None, path: str) -> bytes:
    base_url = _android_camera_base_url(source)
    if base_url is None:
        raise HTTPException(status_code=400, detail="Camera source is not an Android camera app URL.")

    url = f"{base_url}{path}"
    request = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(request, timeout=4) as response:
            return response.read()
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=detail or f"Android camera app returned HTTP {exc.code}.")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to reach Android camera app: {exc}")


# ---------------------------------------------------------------------------
# USB cameras
# ---------------------------------------------------------------------------


def _camera_service_usb_device_controls(
    role: str,
    source: int,
    saved_settings: Dict[str, int | float | bool],
) -> tuple[List[Dict[str, Any]], Dict[str, int | float | bool]]:
    svc = shared_state.camera_service
    if svc is not None and hasattr(svc, "inspect_device_controls_for_role"):
        try:
            controls, live_settings = svc.inspect_device_controls_for_role(role, source, saved_settings)
            return controls, cameraDeviceSettingsToDict(live_settings or saved_settings)
        except Exception:
            pass
    return [], cameraDeviceSettingsToDict(saved_settings)


def _apply_live_usb_device_settings(
    role: str,
    parsed: Dict[str, int | float | bool],
    *,
    persist: bool,
) -> tuple[Dict[str, int | float | bool], bool]:
    svc = shared_state.camera_service
    if svc is not None and hasattr(svc, "set_device_settings_for_role"):
        try:
            live_result = svc.set_device_settings_for_role(role, parsed, persist=persist)
            if live_result is not None:
                return cameraDeviceSettingsToDict(live_result), True
        except Exception:
            pass

    if shared_state.vision_manager is not None and hasattr(shared_state.vision_manager, "setDeviceSettingsForRole"):
        try:
            live_result = shared_state.vision_manager.setDeviceSettingsForRole(role, parsed, persist=persist)
            if live_result is not None:
                return cameraDeviceSettingsToDict(live_result), True
        except Exception:
            pass

    return dict(parsed), False


def _auto_camera_device_settings_from_controls(
    controls: List[Dict[str, Any]],
) -> Dict[str, bool]:
    auto_keys = {"auto_exposure", "auto_white_balance", "autofocus"}
    settings: Dict[str, bool] = {}
    for control in controls:
        key = control.get("key")
        if key in auto_keys and control.get("kind") == "boolean":
            settings[str(key)] = True
    return settings


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/cameras/device-settings/{role}")
def get_camera_device_settings(role: str) -> Dict[str, Any]:
    config = machine_toml.read()
    source = _camera_source_for_role(config, role)
    if source is None:
        return {
            "ok": True,
            "role": role,
            "source": None,
            "provider": "none",
            "settings": {},
            "controls": [],
            "supported": False,
            "message": "No camera is assigned to this role.",
        }

    if isinstance(source, str):
        try:
            android_data = _android_camera_request(source, "/camera-settings")
        except HTTPException as exc:
            return {
                "ok": True,
                "role": role,
                "source": source,
                "provider": "network-stream",
                "settings": {},
                "controls": [],
                "supported": False,
                "message": str(exc.detail),
            }

        return {
            "ok": True,
            "role": role,
            "source": source,
            "provider": android_data.get("provider", "android-camera-app"),
            "settings": android_data.get("settings", {}),
            "capabilities": android_data.get("capabilities", {}),
            "controls": [],
            "supported": True,
        }

    saved_settings = _saved_camera_device_settings(config, role)
    controls, live_settings = _camera_service_usb_device_controls(role, source, saved_settings)
    current_settings = live_settings or saved_settings
    return {
        "ok": True,
        "role": role,
        "source": source,
        "provider": "usb-opencv",
        "settings": current_settings,
        "controls": controls,
        "supported": bool(controls),
        "message": (
            "Real USB camera controls are available for this camera."
            if controls
            else (
                "This USB camera does not expose adjustable UVC controls on this macOS setup."
                if platform.system() == "Darwin"
                else "This USB camera does not expose adjustable controls through the current capture backend."
            )
        ),
    }


@router.post("/api/cameras/device-settings/{role}/preview")
def preview_camera_device_settings(role: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    source = _assigned_camera_source(role)

    if isinstance(source, str):
        proxied = _android_camera_request(
            source,
            "/camera-settings/preview",
            method="POST",
            payload=payload,
        )
        return {
            "ok": True,
            "role": role,
            "source": source,
            "provider": proxied.get("provider", "android-camera-app"),
            "settings": proxied.get("settings", payload),
            "persisted": False,
            "applied_live": True,
        }

    parsed = cameraDeviceSettingsToDict(parseCameraDeviceSettings(payload))
    applied_settings, applied_live = _apply_live_usb_device_settings(role, parsed, persist=False)

    return {
        "ok": True,
        "role": role,
        "source": source,
        "provider": "usb-opencv",
        "settings": applied_settings,
        "persisted": False,
        "applied_live": applied_live,
    }


@router.post("/api/cameras/device-settings/{role}")
def save_camera_device_settings(role: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    source = _assigned_camera_source(role)

    if isinstance(source, str):
        proxied = _android_camera_request(
            source,
            "/camera-settings",
            method="POST",
            payload=payload,
        )
        return {
            "ok": True,
            "role": role,
            "source": source,
            "provider": proxied.get("provider", "android-camera-app"),
            "settings": proxied.get("settings", payload),
            "persisted": True,
            "applied_live": True,
        }

    parsed = cameraDeviceSettingsToDict(parseCameraDeviceSettings(payload))
    settings_role = _settings_role(role)
    with machine_toml.edit() as config:
        device_settings = _get_camera_device_settings_table(config)
        if settings_role == "classification_channel":
            device_settings.pop("carousel", None)
        if parsed:
            device_settings[settings_role] = dict(parsed)
        else:
            device_settings.pop(settings_role, None)
        config["camera_device_settings"] = device_settings

    applied_settings, applied_live = _apply_live_usb_device_settings(role, parsed, persist=True)

    return {
        "ok": True,
        "role": role,
        "source": source,
        "provider": "usb-opencv",
        "settings": applied_settings,
        "persisted": True,
        "applied_live": applied_live,
        "message": "Camera device settings saved.",
    }


@router.post("/api/cameras/device-settings/{role}/reset-defaults")
def reset_camera_device_settings_to_defaults(role: str) -> Dict[str, Any]:
    source = _assigned_camera_source(role)

    if isinstance(source, str):
        payload = {
            "exposure_compensation": 0,
            "ae_lock": False,
            "awb_lock": False,
            "white_balance_mode": "auto",
            "processing_mode": "standard",
        }
        proxied = _android_camera_request(
            source,
            "/camera-settings",
            method="POST",
            payload=payload,
        )
        return {
            "ok": True,
            "role": role,
            "source": source,
            "provider": proxied.get("provider", "android-camera-app"),
            "settings": proxied.get("settings", payload),
            "persisted": True,
            "applied_live": True,
            "message": "Camera reset to automatic settings.",
        }

    controls, _ = _camera_service_usb_device_controls(role, source, {})
    auto_settings = _auto_camera_device_settings_from_controls(controls)
    if not auto_settings:
        from vision.camera import default_auto_camera_device_settings

        auto_settings = default_auto_camera_device_settings()

    with machine_toml.edit() as config:
        device_settings = _get_camera_device_settings_table(config)
        device_settings.pop(role, None)
        if role in {"classification_channel", "carousel"}:
            device_settings.pop("classification_channel", None)
            device_settings.pop("carousel", None)
        config["camera_device_settings"] = device_settings

    applied_settings, applied_live = _apply_live_usb_device_settings(role, auto_settings, persist=False)
    svc = shared_state.camera_service
    if svc is not None and hasattr(svc, "clear_persisted_device_settings_for_role"):
        try:
            svc.clear_persisted_device_settings_for_role(role)
        except Exception:
            pass

    return {
        "ok": True,
        "role": role,
        "source": source,
        "provider": "usb-opencv",
        "settings": applied_settings,
        "controls": controls,
        "persisted": False,
        "applied_live": applied_live,
        "message": "Camera reset to automatic settings.",
    }


# ---------------------------------------------------------------------------
# Drift between saved and live settings
# ---------------------------------------------------------------------------


def _device_setting_diff(
    key: str,
    saved_value: Any,
    live_value: Any,
    control: Dict[str, Any] | None,
) -> Dict[str, Any] | None:
    if saved_value is None:
        return None
    if live_value is None:
        return None

    if isinstance(saved_value, bool) or isinstance(live_value, bool):
        if bool(saved_value) == bool(live_value):
            return None
        return {"key": key, "saved": bool(saved_value), "live": bool(live_value), "kind": "boolean"}

    try:
        saved_num = float(saved_value)
        live_num = float(live_value)
    except (TypeError, ValueError):
        return None

    step = 1.0
    tol_pct = 0.01
    if isinstance(control, dict):
        step_raw = control.get("step")
        if isinstance(step_raw, (int, float)) and step_raw > 0:
            step = float(step_raw)
        min_raw = control.get("min")
        max_raw = control.get("max")
        if isinstance(min_raw, (int, float)) and isinstance(max_raw, (int, float)) and max_raw > min_raw:
            tol_pct = max(tol_pct, 0.01 * (float(max_raw) - float(min_raw)))
    tolerance = max(step, abs(saved_num) * 0.01, tol_pct * 0.01)
    if abs(saved_num - live_num) <= tolerance:
        return None
    return {"key": key, "saved": saved_num, "live": live_num, "kind": "number"}


@router.get("/api/cameras/device-settings/{role}/diff")
def get_camera_device_settings_diff(role: str) -> Dict[str, Any]:
    config = machine_toml.read()
    source = _camera_source_for_role(config, role)
    if source is None:
        return {
            "ok": True,
            "role": role,
            "source": None,
            "supported": False,
            "saved": {},
            "live": {},
            "diffs": [],
            "message": "No camera is assigned to this role.",
        }

    saved_settings = _saved_camera_device_settings(config, role)

    controls: List[Dict[str, Any]] = []
    live_settings: Dict[str, Any] = {}
    if isinstance(source, int):
        controls, live_settings = _camera_service_usb_device_controls(role, source, saved_settings)
    else:
        # Network-stream (Android) — proxied read
        try:
            android_data = _android_camera_request(source, "/camera-settings")
            raw_settings = android_data.get("settings") or {}
            if isinstance(raw_settings, dict):
                live_settings = {k: v for k, v in raw_settings.items() if isinstance(v, (int, float, bool))}
        except HTTPException as exc:
            return {
                "ok": True,
                "role": role,
                "source": source,
                "supported": False,
                "saved": saved_settings,
                "live": {},
                "diffs": [],
                "message": str(exc.detail),
            }

    controls_by_key: Dict[str, Dict[str, Any]] = {}
    for control in controls:
        key = control.get("key")
        if isinstance(key, str):
            controls_by_key[key] = control

    diffs: List[Dict[str, Any]] = []
    keys = set(saved_settings.keys()) | set(live_settings.keys())
    for key in sorted(keys):
        diff = _device_setting_diff(
            key,
            saved_settings.get(key),
            live_settings.get(key),
            controls_by_key.get(key),
        )
        if diff is not None:
            diffs.append(diff)

    return {
        "ok": True,
        "role": role,
        "source": source,
        "supported": bool(controls) or bool(live_settings),
        "saved": saved_settings,
        "live": live_settings,
        "diffs": diffs,
    }
