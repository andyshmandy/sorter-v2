"""Router for calibrating a camera role's controls: start a calibration in the
background, follow its progress, and look through the frames it tried.

A run uses one of three methods: the target-plate search or the
exposure-histogram loop (server/camera_calibration_search.py), or the
LLM-guided loop (server/camera_calibration_llm.py). It then saves the settings
it found through the device settings routes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import machine_toml
from irl.config import cameraDeviceSettingsToDict, parseCameraDeviceSettings, parseCameraPictureSettings
from server import shared_state
from server.camera_calibration import analyze_color_plate_target
from server.camera_calibration_llm import (
    DEFAULT_LLM_CALIBRATION_MAX_ITERATIONS,
    _calibrate_camera_device_settings_with_llm,
    _normalize_llm_calibration_iterations,
    _normalize_llm_calibration_model,
    _run_llm_final_review,
)
from server.camera_calibration_search import (
    CALIBRATION_METHOD_EXPOSURE_HISTOGRAM,
    CALIBRATION_METHOD_LLM_GUIDED,
    CALIBRATION_METHOD_TARGET_PLATE,
    EXPOSURE_HISTOGRAM_TARGET_LUMA,
    _calibrate_exposure_via_histogram,
    _calibrate_usb_camera_device_settings,
    _capture_frame_for_calibration,
    _gallery_image_url,
)
from server.routers.camera_device_settings import (
    get_camera_device_settings,
    preview_camera_device_settings,
    save_camera_device_settings,
)
from server.routers.camera_picture_settings import _picture_settings_for_role

router = APIRouter()

DEFAULT_CAMERA_CALIBRATION_METHOD = CALIBRATION_METHOD_TARGET_PLATE
# Each calibration run saves the frames it tried in a folder of its own here.
CALIBRATION_GALLERY_DIR = "/tmp/calibration-gallery"


class CameraCalibrationStartPayload(BaseModel):
    method: Optional[str] = None
    openrouter_model: Optional[str] = None
    max_iterations: Optional[int] = None


def _normalize_camera_calibration_method(value: str | None) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == CALIBRATION_METHOD_LLM_GUIDED:
            return CALIBRATION_METHOD_LLM_GUIDED
        if normalized == CALIBRATION_METHOD_EXPOSURE_HISTOGRAM:
            return CALIBRATION_METHOD_EXPOSURE_HISTOGRAM
    return CALIBRATION_METHOD_TARGET_PLATE


# ---------------------------------------------------------------------------
# Calibration tasks
# ---------------------------------------------------------------------------


def _create_camera_calibration_task(
    role: str,
    provider: str,
    source: int | str | None,
    *,
    method: str = DEFAULT_CAMERA_CALIBRATION_METHOD,
    openrouter_model: str | None = None,
) -> str:
    task_id = uuid4().hex
    task = {
        "task_id": task_id,
        "role": role,
        "provider": provider,
        "source": source,
        "method": method,
        "openrouter_model": openrouter_model,
        "status": "queued",
        "stage": "queued",
        "message": "Queued camera calibration.",
        "progress": 0.0,
        "result": None,
        "analysis_preview": None,
        "advisor_trace": [],
        "error": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with shared_state.camera_calibration_tasks_lock:
        shared_state.camera_calibration_tasks[task_id] = task
    return task_id


def _update_camera_calibration_task(task_id: str, **updates: Any) -> None:
    with shared_state.camera_calibration_tasks_lock:
        task = shared_state.camera_calibration_tasks.get(task_id)
        if task is None:
            return
        task.update(updates)
        task["updated_at"] = time.time()


def _get_camera_calibration_task(task_id: str) -> Dict[str, Any] | None:
    with shared_state.camera_calibration_tasks_lock:
        task = shared_state.camera_calibration_tasks.get(task_id)
        return dict(task) if task is not None else None


def _calibratable_camera_settings(role: str) -> Dict[str, Any]:
    """The role's device settings, when its camera can be calibrated."""
    current_response = get_camera_device_settings(role)
    if current_response.get("source") is None:
        raise HTTPException(status_code=404, detail="No camera is assigned to this role.")
    if not bool(current_response.get("supported")):
        raise HTTPException(status_code=400, detail=current_response.get("message") or "This camera cannot be calibrated through the current control backend.")
    return current_response


def _cleanup_old_gallery_dirs(max_age_seconds: float = 3600.0) -> None:
    """Remove calibration gallery directories older than max_age_seconds."""
    import shutil

    gallery_root = Path(CALIBRATION_GALLERY_DIR)
    if not gallery_root.exists():
        return
    now = time.time()
    for child in gallery_root.iterdir():
        if child.is_dir():
            try:
                age = now - child.stat().st_mtime
                if age > max_age_seconds:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                pass


def _restore_preview_settings(role: str, settings: Dict[str, int | float | bool]) -> None:
    try:
        preview_camera_device_settings(role, settings)
    except Exception:
        pass


def _run_camera_calibration_sync(
    role: str,
    *,
    method: str = DEFAULT_CAMERA_CALIBRATION_METHOD,
    openrouter_model: str | None = None,
    max_iterations: int = DEFAULT_LLM_CALIBRATION_MAX_ITERATIONS,
    report_progress: Callable[[str, float, str, Dict[str, Any] | None], None] | None = None,
    report_trace: Callable[[List[Dict[str, Any]]], None] | None = None,
    task_id: str | None = None,
) -> Dict[str, Any]:
    current_response = _calibratable_camera_settings(role)
    source = current_response.get("source")
    provider = current_response.get("provider")
    normalized_method = _normalize_camera_calibration_method(method)
    normalized_openrouter_model = _normalize_llm_calibration_model(openrouter_model)
    normalized_max_iterations = _normalize_llm_calibration_iterations(max_iterations)

    # Create gallery directory for this calibration run
    _cleanup_old_gallery_dirs()
    gallery_id = task_id or uuid4().hex
    gallery_dir = Path(CALIBRATION_GALLERY_DIR) / gallery_id
    gallery_dir.mkdir(parents=True, exist_ok=True)

    raw_config = machine_toml.read()
    original_picture_settings = _picture_settings_for_role(raw_config, role)

    original_settings = cameraDeviceSettingsToDict(parseCameraDeviceSettings(current_response.get("settings")))

    try:
        calibration_metadata: Dict[str, Any] = {"method": normalized_method}

        if normalized_method == CALIBRATION_METHOD_EXPOSURE_HISTOGRAM:
            controls = current_response.get("controls")
            if not isinstance(controls, list):
                raise HTTPException(
                    status_code=400,
                    detail="This camera does not expose a control list required for histogram exposure calibration.",
                )
            if report_progress is not None:
                report_progress(
                    "preparing",
                    0.05,
                    "Preparing histogram-driven exposure calibration.",
                    None,
                )
            best_settings, analysis = _calibrate_exposure_via_histogram(
                role,
                source,
                controls,
                original_settings,
                report_progress=report_progress,
                gallery_dir=gallery_dir,
            )
            calibration_metadata = {
                "method": normalized_method,
                "target_luma": EXPOSURE_HISTOGRAM_TARGET_LUMA,
                "final_luma": analysis.get("final_luma"),
                "iterations": analysis.get("iterations"),
                "converged": analysis.get("converged"),
            }
        elif normalized_method == CALIBRATION_METHOD_LLM_GUIDED:
            if report_progress is not None:
                report_progress("preparing", 0.05, "Preparing LLM-guided camera calibration.", None)
            best_settings, analysis, calibration_metadata = _calibrate_camera_device_settings_with_llm(
                role,
                source,
                current_response,
                openrouter_model=normalized_openrouter_model,
                max_iterations=normalized_max_iterations,
                report_progress=report_progress,
                report_trace=report_trace,
                gallery_dir=gallery_dir,
            )
        else:
            controls = current_response.get("controls")
            if not isinstance(controls, list) or not isinstance(source, int):
                raise HTTPException(status_code=400, detail="USB camera controls are not available for calibration.")
            if report_progress is not None:
                report_progress("preparing", 0.05, "Preparing USB camera calibration.", None)
            best_settings, analysis = _calibrate_usb_camera_device_settings(
                role,
                source,
                controls,
                original_settings,
                report_progress=report_progress,
                gallery_dir=gallery_dir,
            )

        if report_progress is not None:
            report_progress("saving", 0.91, "Saving calibrated exposure and white balance.", analysis)
        saved = save_camera_device_settings(role, best_settings)
        time.sleep(0.2)

        if normalized_method == CALIBRATION_METHOD_EXPOSURE_HISTOGRAM:
            return {
                "ok": True,
                "method": normalized_method,
                "task_id": task_id,
                "role": role,
                "provider": provider,
                "source": source,
                "applied_settings": saved,
                "calibration": calibration_metadata,
                "analysis": analysis,
                "message": (
                    "Exposure-histogram calibration saved. "
                    f"Final exposure {best_settings.get('exposure')}, "
                    f"final luma {int(analysis.get('final_luma', 0))}."
                ),
            }

        raw_frame = _capture_frame_for_calibration(
            role,
            source,
            fallback_settings=best_settings,
        )
        raw_analysis_obj = analyze_color_plate_target(raw_frame) if raw_frame is not None else None
        raw_analysis = raw_analysis_obj.to_dict() if raw_analysis_obj is not None else analysis
        if report_progress is not None:
            report_progress("verifying", 0.98, "Verifying the calibrated camera settings.", raw_analysis)

        final_frame = raw_frame
        if final_frame is None:
            final_frame = _capture_frame_for_calibration(
                role,
                source,
                fallback_settings=best_settings,
            )
        if final_frame is not None:
            from vision.camera import apply_picture_settings

            final_frame = apply_picture_settings(
                final_frame,
                parseCameraPictureSettings(original_picture_settings),
            )

        final_analysis = analyze_color_plate_target(final_frame) if final_frame is not None else None
        chosen_analysis = final_analysis.to_dict() if final_analysis is not None else raw_analysis

        if (
            normalized_method == CALIBRATION_METHOD_LLM_GUIDED
            and final_frame is not None
        ):
            existing_trace_raw = calibration_metadata.get("trace")
            existing_trace: List[Dict[str, Any]] = (
                list(existing_trace_raw) if isinstance(existing_trace_raw, list) else []
            )
            iteration_numbers = [
                int(entry.get("iteration"))
                for entry in existing_trace
                if isinstance(entry, dict) and isinstance(entry.get("iteration"), int)
            ]
            next_iteration_index = (max(iteration_numbers) + 1) if iteration_numbers else 1
            advisor_history_step = len(existing_trace) + 1
            if report_progress is not None:
                report_progress(
                    "llm_final_review",
                    0.99,
                    "Asking the advisor to sign off on the camera settings.",
                    chosen_analysis,
                )
            review_entry = _run_llm_final_review(
                role=role,
                gallery_dir=gallery_dir,
                openrouter_model=normalized_openrouter_model,
                final_frame=final_frame,
                final_settings=best_settings,
                last_loop_summary=str(calibration_metadata.get("summary") or ""),
                next_iteration_index=next_iteration_index,
                advisor_history_step=advisor_history_step,
            )
            existing_trace.append(review_entry)
            calibration_metadata["trace"] = existing_trace
            calibration_metadata["final_review"] = review_entry
            if report_trace is not None:
                report_trace([dict(entry) for entry in existing_trace])

        if normalized_method == CALIBRATION_METHOD_LLM_GUIDED:
            review_status = ""
            review_obj = calibration_metadata.get("final_review")
            if isinstance(review_obj, dict):
                review_status = str(review_obj.get("status") or "").strip().lower()
            base_msg = "Camera settings were tuned by the LLM advisor."
            if review_status == "approved":
                message = f"{base_msg} Advisor signed off on the image."
            elif review_status == "concerns":
                message = f"{base_msg} Advisor flagged remaining concerns — review the trace."
            else:
                message = base_msg
        else:
            message = "Camera calibrated from the 6-color target plate."
        result = {
            **saved,
            "analysis": chosen_analysis,
            "gallery_id": gallery_id,
            "message": message,
            "method": normalized_method,
        }
        if normalized_method == CALIBRATION_METHOD_LLM_GUIDED:
            result["openrouter_model"] = calibration_metadata.get("openrouter_model")
            result["advisor_trace"] = calibration_metadata.get("trace")
            result["advisor_summary"] = calibration_metadata.get("summary")
            result["advisor_final_review"] = calibration_metadata.get("final_review")
        if report_progress is not None:
            report_progress("completed", 1.0, "Camera calibration finished.", result.get("analysis"))
        return result
    except HTTPException:
        _restore_preview_settings(role, original_settings)
        raise
    except Exception as exc:
        _restore_preview_settings(role, original_settings)
        raise HTTPException(status_code=500, detail=f"Camera calibration failed: {exc}")


def _run_camera_calibration_task(
    task_id: str,
    role: str,
    *,
    method: str = DEFAULT_CAMERA_CALIBRATION_METHOD,
    openrouter_model: str | None = None,
    max_iterations: int = DEFAULT_LLM_CALIBRATION_MAX_ITERATIONS,
) -> None:
    def report_progress(stage: str, progress: float, message: str, analysis: Dict[str, Any] | None = None) -> None:
        _update_camera_calibration_task(
            task_id,
            status="running" if stage != "completed" else "completed",
            stage=stage,
            progress=max(0.0, min(1.0, float(progress))),
            message=message,
            analysis_preview=analysis,
        )

    def report_trace(trace: List[Dict[str, Any]]) -> None:
        _update_camera_calibration_task(
            task_id,
            advisor_trace=trace,
        )

    try:
        _update_camera_calibration_task(
            task_id,
            status="running",
            stage="starting",
            progress=0.01,
            message="Starting camera calibration.",
        )
        result = _run_camera_calibration_sync(
            role,
            method=method,
            openrouter_model=openrouter_model,
            max_iterations=max_iterations,
            report_progress=report_progress,
            report_trace=report_trace,
            task_id=task_id,
        )
        _update_camera_calibration_task(
            task_id,
            status="completed",
            stage="completed",
            progress=1.0,
            message=str(result.get("message") or "Camera calibration finished."),
            result=result,
            error=None,
        )
    except HTTPException as exc:
        _update_camera_calibration_task(
            task_id,
            status="failed",
            stage="failed",
            progress=1.0,
            message="Camera calibration failed.",
            error=str(exc.detail),
        )
    except Exception as exc:
        _update_camera_calibration_task(
            task_id,
            status="failed",
            stage="failed",
            progress=1.0,
            message="Camera calibration failed.",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/cameras/device-settings/{role}/calibrate-target")
def start_camera_device_settings_calibration_from_target(
    role: str,
    payload: CameraCalibrationStartPayload | None = None,
) -> Dict[str, Any]:
    current_response = _calibratable_camera_settings(role)
    source = current_response.get("source")
    provider = str(current_response.get("provider") or "unknown")
    method = _normalize_camera_calibration_method(payload.method if payload is not None else None)
    openrouter_model = _normalize_llm_calibration_model(payload.openrouter_model if payload is not None else None)
    max_iterations = _normalize_llm_calibration_iterations(payload.max_iterations if payload is not None else None)
    if method == CALIBRATION_METHOD_LLM_GUIDED and not os.getenv("OPENROUTER_API_KEY"):
        raise HTTPException(status_code=400, detail="OpenRouter API key is required for LLM-guided calibration.")

    task_id = _create_camera_calibration_task(
        role,
        provider,
        source,
        method=method,
        openrouter_model=openrouter_model if method == CALIBRATION_METHOD_LLM_GUIDED else None,
    )
    thread = threading.Thread(
        target=_run_camera_calibration_task,
        args=(task_id, role),
        kwargs={
            "method": method,
            "openrouter_model": openrouter_model if method == CALIBRATION_METHOD_LLM_GUIDED else None,
            "max_iterations": max_iterations,
        },
        daemon=True,
    )
    thread.start()
    task = _get_camera_calibration_task(task_id)
    assert task is not None
    return {
        "ok": True,
        "started": True,
        "task_id": task_id,
        "role": role,
        "provider": provider,
        "source": source,
        "method": task.get("method"),
        "openrouter_model": task.get("openrouter_model"),
        "status": task.get("status"),
        "stage": task.get("stage"),
        "progress": task.get("progress"),
        "message": task.get("message"),
    }


@router.get("/api/cameras/device-settings/{role}/calibrate-target/{task_id}")
def get_camera_device_settings_calibration_task(role: str, task_id: str) -> Dict[str, Any]:
    task = _get_camera_calibration_task(task_id)
    if task is None or task.get("role") != role:
        raise HTTPException(status_code=404, detail="Calibration task not found.")
    return {
        "ok": True,
        "task_id": task_id,
        "role": role,
        "provider": task.get("provider"),
        "source": task.get("source"),
        "method": task.get("method"),
        "openrouter_model": task.get("openrouter_model"),
        "status": task.get("status"),
        "stage": task.get("stage"),
        "progress": task.get("progress"),
        "message": task.get("message"),
        "result": task.get("result"),
        "analysis_preview": task.get("analysis_preview"),
        "advisor_trace": task.get("advisor_trace"),
        "error": task.get("error"),
    }


# ---------------------------------------------------------------------------
# Calibration debug gallery
# ---------------------------------------------------------------------------


@router.get("/api/cameras/device-settings/{role}/calibrate-target/{task_id}/gallery")
def get_calibration_gallery(role: str, task_id: str) -> Dict[str, Any]:
    """List all frames saved during a calibration run."""
    gallery_dir = Path(CALIBRATION_GALLERY_DIR) / task_id
    if task_id == ".." or not gallery_dir.exists():
        raise HTTPException(status_code=404, detail="Gallery not found for this calibration task.")

    entries: list[Dict[str, Any]] = []
    for json_path in sorted(gallery_dir.glob("*.json")):
        try:
            meta = json.loads(json_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        image_name = json_path.stem + ".jpg"
        image_path = gallery_dir / image_name
        if not image_path.exists():
            continue
        entries.append({
            "filename": image_name,
            "image_url": _gallery_image_url(role, task_id, image_name),
            **meta,
        })

    return {"ok": True, "task_id": task_id, "entries": entries}


@router.get("/api/cameras/device-settings/{role}/calibrate-target/{task_id}/gallery/{filename}")
def get_calibration_gallery_image(role: str, task_id: str, filename: str) -> StreamingResponse:
    """Serve a single saved calibration frame."""
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    image_path = Path(CALIBRATION_GALLERY_DIR) / task_id / filename
    if task_id == ".." or not image_path.exists() or not image_path.suffix == ".jpg":
        raise HTTPException(status_code=404, detail="Image not found.")
    return StreamingResponse(
        open(image_path, "rb"),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )
