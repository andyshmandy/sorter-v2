"""Tuning a camera's controls by trying settings and scoring the frames they give.

Every calibration method captures its frames through the helpers here, the
LLM-guided one (server/camera_calibration_llm.py) included: preview candidate
settings live through the device settings routes, grab the frame that
follows, score it against the target plate. The searches themselves are here
too: the target-plate search for a USB camera and for the Android camera app,
and the exposure-histogram loop.

server/camera_calibration.py finds and scores the target plate in a frame;
server/routers/camera_calibration.py runs a calibration and saves its result.
"""

from __future__ import annotations

import json
import platform
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

import cv2
import numpy as np
from fastapi import HTTPException

from irl.config import cameraDeviceSettingsToDict, parseCameraDeviceSettings, parseCameraPictureSettings
from server import shared_state
from server.camera_calibration import analyze_color_plate_target
from server.routers.camera_device_settings import _android_camera_bytes_request, preview_camera_device_settings

CALIBRATION_METHOD_TARGET_PLATE = "target_plate"
CALIBRATION_METHOD_LLM_GUIDED = "llm_guided"
CALIBRATION_METHOD_EXPOSURE_HISTOGRAM = "exposure_histogram"
EXPOSURE_HISTOGRAM_TARGET_LUMA = 128.0
EXPOSURE_HISTOGRAM_TOLERANCE_LUMA = 3.0
EXPOSURE_HISTOGRAM_MAX_ITERATIONS = 20
EXPOSURE_HISTOGRAM_P_GAIN = 1.4


def _gallery_image_url(role: str, task_id: str, filename: str) -> str:
    """Where the calibration routes serve a frame a calibration run saved."""
    return f"/api/cameras/device-settings/{role}/calibrate-target/{task_id}/gallery/{filename}"


# ---------------------------------------------------------------------------
# Camera controls
# ---------------------------------------------------------------------------


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _clamp_control(value: float, control: Dict[str, Any]) -> float:
    """Clamp a value to a control's min/max range and snap it to the control's
    step, counted from its min."""
    c_min = _as_number(control.get("min"))
    c_max = _as_number(control.get("max"))
    if c_min is not None:
        value = max(c_min, value)
    if c_max is not None:
        value = min(c_max, value)
    step = _as_number(control.get("step"))
    if step is None or step <= 0:
        return float(value)
    origin = c_min or 0.0
    return float(origin + round((value - origin) / step) * step)


def _usb_control_defaults(
    controls: List[Dict[str, Any]],
    current_settings: Dict[str, int | float | bool],
) -> Dict[str, int | float | bool]:
    defaults: Dict[str, int | float | bool] = {}
    for control in controls:
        key = control.get("key")
        if not isinstance(key, str):
            continue
        default = control.get("default")
        if isinstance(default, (int, float, bool)) and not isinstance(default, bool):
            defaults[key] = float(default) if isinstance(default, float) or isinstance(default, int) else default
            continue
        if isinstance(default, bool):
            defaults[key] = default
            continue
        if key in {"auto_exposure", "auto_white_balance", "autofocus"} and isinstance(control.get("kind"), str):
            defaults[key] = True
            continue
        if key in current_settings:
            defaults[key] = current_settings[key]
            continue
        value = control.get("value")
        if isinstance(value, bool):
            defaults[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            defaults[key] = float(value)
    return defaults


# ---------------------------------------------------------------------------
# Frame capture and scoring
# ---------------------------------------------------------------------------


def _capture_frame_for_calibration(
    role: str,
    source: int | str | None,
    *,
    after_timestamp: float | None = None,
    fallback_settings: Dict[str, int | float | bool] | None = None,
    picture_settings: Dict[str, Any] | None = None,
) -> np.ndarray | None:
    from vision.camera import (
        apply_camera_device_settings,
        apply_picture_settings,
    )

    parsed_picture_settings = parseCameraPictureSettings(picture_settings)

    if isinstance(source, str):
        best_frame = None
        for index in range(5):
            try:
                jpg = _android_camera_bytes_request(source, "/snapshot.jpg")
                buffer = np.frombuffer(jpg, dtype=np.uint8)
                frame = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
                if frame is not None and frame.size > 0:
                    best_frame = frame
            except HTTPException:
                pass
            if index < 4:
                time.sleep(0.18)
        if best_frame is not None:
            best_frame = apply_picture_settings(best_frame, parsed_picture_settings)
            return best_frame
        return None

    if not isinstance(source, int):
        return None

    cap = cv2.VideoCapture(source, cv2.CAP_AVFOUNDATION) if platform.system() == "Darwin" else cv2.VideoCapture(source)
    if not cap.isOpened():
        cap.release()
        return None

    try:
        if fallback_settings:
            apply_camera_device_settings(cap, fallback_settings, source=source)
            time.sleep(0.2)
        frame: np.ndarray | None = None
        for _ in range(4):
            ret, current = cap.read()
            if ret and current is not None:
                frame = current
        if frame is None:
            return None
        frame = apply_picture_settings(frame, parsed_picture_settings)
        return frame.copy()
    finally:
        cap.release()


def _grab_live_frame(role: str, after_timestamp: float, timeout: float = 1.0) -> np.ndarray | None:
    """Grab a frame from the running CaptureThread, waiting for one newer than after_timestamp."""
    if shared_state.vision_manager is None or not hasattr(shared_state.vision_manager, "getCaptureThreadForRole"):
        return None
    try:
        capture = shared_state.vision_manager.getCaptureThreadForRole(role)
    except Exception:
        return None
    if capture is None:
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        frame_obj = capture.latest_frame
        if frame_obj is not None and frame_obj.timestamp > after_timestamp and frame_obj.raw is not None:
            return frame_obj.raw.copy()
        time.sleep(0.03)
    # Last resort: return whatever is there
    frame_obj = capture.latest_frame
    if frame_obj is not None and frame_obj.raw is not None:
        return frame_obj.raw.copy()
    return None


def _analyze_candidate_settings(
    role: str,
    source: int | str | None,
    settings: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any] | None, np.ndarray | None]:
    preview_started_at = time.time()
    preview = preview_camera_device_settings(role, settings)
    preview_settings = preview.get("settings", settings)
    if isinstance(source, str):
        applied_settings = dict(preview_settings) if isinstance(preview_settings, dict) else dict(settings)
        time.sleep(1.35)
        frame = _capture_frame_for_calibration(role, source, after_timestamp=preview_started_at, fallback_settings=applied_settings)
    else:
        applied_settings = cameraDeviceSettingsToDict(parseCameraDeviceSettings(preview_settings))
        time.sleep(0.25)
        # Grab from live CaptureThread — no second camera open needed
        frame = _grab_live_frame(role, after_timestamp=preview_started_at)
        if frame is None:
            # Fallback: direct capture (CaptureThread might not be running)
            frame = _capture_frame_for_calibration(role, source, after_timestamp=preview_started_at, fallback_settings=applied_settings)

    if frame is None:
        return applied_settings, None, None
    analysis = analyze_color_plate_target(frame)
    analysis_dict = analysis.to_dict() if analysis is not None else None
    return applied_settings, analysis_dict, frame


def _capture_raw_frame(
    role: str,
    source: int,
    settings: Dict[str, int | float | bool],
) -> np.ndarray | None:
    """Capture a raw frame (no color profile / picture settings) for histogram analysis."""
    from vision.camera import apply_camera_device_settings

    cap = cv2.VideoCapture(source, cv2.CAP_AVFOUNDATION) if platform.system() == "Darwin" else cv2.VideoCapture(source)
    if not cap.isOpened():
        cap.release()
        return None
    try:
        apply_camera_device_settings(cap, settings, source=source)
        time.sleep(0.15)
        frame: np.ndarray | None = None
        for _ in range(4):
            ret, current = cap.read()
            if ret and current is not None:
                frame = current
        return frame.copy() if frame is not None else None
    finally:
        cap.release()


def _camera_analysis_score(analysis: Dict[str, Any] | None) -> float:
    if not isinstance(analysis, dict):
        return float("-inf")
    value = analysis.get("score")
    return float(value) if isinstance(value, (int, float)) else float("-inf")


def _calibration_selection_value(analysis: Dict[str, Any] | None) -> float:
    score = _camera_analysis_score(analysis)
    if not isinstance(analysis, dict):
        return score
    tile_samples = analysis.get("tile_samples")
    if not isinstance(tile_samples, dict) or not tile_samples:
        return score

    important_keys = ("white_top", "white_bottom", "red", "yellow")
    important_matches: List[float] = []
    all_matches: List[float] = []
    for key, raw in tile_samples.items():
        if not isinstance(raw, dict):
            continue
        match_value = raw.get("reference_match_percent")
        if not isinstance(match_value, (int, float)):
            continue
        match = float(match_value)
        all_matches.append(match)
        if key in important_keys:
            important_matches.append(match)

    if not important_matches:
        return score

    min_match = min(important_matches)
    avg_match = float(sum(important_matches) / len(important_matches))
    overall_match = float(sum(all_matches) / len(all_matches)) if all_matches else avg_match
    return score * 0.15 + avg_match + min_match * 1.3 + overall_match * 0.25


# ---------------------------------------------------------------------------
# Exposure histogram
# ---------------------------------------------------------------------------


def _calibrate_exposure_via_histogram(
    role: str,
    source: int | str | None,
    controls: List[Dict[str, Any]],
    current_settings: Dict[str, int | float | bool],
    *,
    report_progress: Callable[[str, float, str, Dict[str, Any] | None], None] | None = None,
    gallery_dir: Path | None = None,
    target_luma: float = EXPOSURE_HISTOGRAM_TARGET_LUMA,
    tolerance: float = EXPOSURE_HISTOGRAM_TOLERANCE_LUMA,
    max_iterations: int = EXPOSURE_HISTOGRAM_MAX_ITERATIONS,
) -> tuple[Dict[str, int | float | bool], Dict[str, Any]]:
    """Proportional-gain exposure calibration driven by frame-luma histogram.

    Captures a frame, measures mean grayscale brightness, adjusts the
    ``exposure`` device control proportionally toward ``target_luma`` (default
    128 = middle gray), settles, captures again, repeats. Converges fast
    on typical Classification-chamber scenes (4 – 8 iterations). No colour
    profile, no LLM round-trip, no target plate — just "make the scene
    middle-gray". Returns the winning settings + an analysis dict for the
    calling task to attach to the progress report.
    """

    if not isinstance(controls, list):
        raise HTTPException(status_code=400, detail="Camera controls are not available for exposure calibration.")

    exposure_spec: Dict[str, Any] | None = None
    for ctrl in controls:
        if isinstance(ctrl, dict) and ctrl.get("key") == "exposure":
            exposure_spec = ctrl
            break
    if exposure_spec is None:
        raise HTTPException(
            status_code=400,
            detail="This camera does not expose a manual exposure control we can drive.",
        )

    exposure_min = float(exposure_spec.get("min") or 1)
    exposure_max = float(exposure_spec.get("max") or 20000)
    exposure_step = max(1.0, float(exposure_spec.get("step") or 1))
    current_exposure = float(current_settings.get("exposure") or exposure_spec.get("value") or exposure_min)
    current_exposure = max(exposure_min, min(exposure_max, current_exposure))

    # Make sure auto_exposure is off before we start poking manual exposure,
    # otherwise the camera may override every write on the very next frame.
    settings = dict(current_settings)
    settings["auto_exposure"] = False
    settings["exposure"] = current_exposure

    if report_progress is not None:
        report_progress(
            "calibrating",
            0.1,
            f"Starting exposure-histogram calibration (target luma ≈ {int(target_luma)}).",
            None,
        )

    trace: list[Dict[str, float]] = []
    best_delta = float("inf")
    best_settings = dict(settings)
    best_luma = 0.0

    for iteration in range(1, max_iterations + 1):
        before = time.time()
        applied, _, frame = _analyze_candidate_settings(role, source, settings)
        frame_to_use = frame
        if frame_to_use is None:
            frame_to_use = _capture_frame_for_calibration(
                role, source, fallback_settings=settings, after_timestamp=before,
            )
        if frame_to_use is None:
            raise HTTPException(status_code=500, detail="Could not capture a frame for histogram calibration.")

        gray = cv2.cvtColor(frame_to_use, cv2.COLOR_BGR2GRAY) if frame_to_use.ndim == 3 else frame_to_use
        mean_luma = float(np.mean(gray))
        delta = target_luma - mean_luma
        trace.append({
            "iteration": iteration,
            "exposure": float(settings["exposure"]),
            "mean_luma": round(mean_luma, 2),
            "delta": round(delta, 2),
        })

        if gallery_dir is not None:
            try:
                stamp = f"hist_{iteration:02d}_exp{int(settings['exposure'])}_l{int(mean_luma)}.jpg"
                cv2.imwrite(str(gallery_dir / stamp), frame_to_use)
            except Exception:
                pass

        if abs(delta) < best_delta:
            best_delta = abs(delta)
            best_settings = dict(applied) if isinstance(applied, dict) else dict(settings)
            best_settings.setdefault("auto_exposure", False)
            best_luma = mean_luma

        if report_progress is not None:
            progress = 0.1 + 0.78 * (iteration / max_iterations)
            report_progress(
                "calibrating",
                min(0.9, progress),
                (
                    f"Iter {iteration}/{max_iterations}: "
                    f"exposure={int(settings['exposure'])} → luma={mean_luma:.0f} "
                    f"(target {int(target_luma)}, Δ={delta:+.1f})."
                ),
                {"iteration": iteration, "mean_luma": mean_luma, "exposure": settings["exposure"]},
            )

        if abs(delta) <= tolerance:
            break

        # Proportional gain — exposure scales roughly linearly with light at
        # moderate values, so a gain just above 1 gets us to target in a handful
        # of steps without overshoot. Clamped to the control's advertised range.
        ratio = target_luma / max(mean_luma, 1.0)
        ratio = max(0.3, min(3.0, ratio))  # per-step safety bound
        new_exposure = float(settings["exposure"]) * (1.0 + (ratio - 1.0) * EXPOSURE_HISTOGRAM_P_GAIN)
        new_exposure = max(exposure_min, min(exposure_max, new_exposure))
        new_exposure = round(new_exposure / exposure_step) * exposure_step
        if abs(new_exposure - settings["exposure"]) < exposure_step:
            # P-controller wants a move smaller than the control resolution —
            # no further progress possible at this setting.
            break
        settings["exposure"] = int(new_exposure) if float(int(new_exposure)) == new_exposure else new_exposure

    analysis = {
        "method": CALIBRATION_METHOD_EXPOSURE_HISTOGRAM,
        "target_luma": target_luma,
        "final_luma": best_luma,
        "final_delta": best_delta,
        "final_exposure": best_settings.get("exposure"),
        "iterations": len(trace),
        "converged": best_delta <= tolerance,
        "trace": trace,
    }

    if report_progress is not None:
        report_progress(
            "saving",
            0.9,
            (
                f"Converged at exposure={best_settings.get('exposure')} "
                f"(luma {best_luma:.0f}, target {int(target_luma)})."
                if best_delta <= tolerance
                else f"Stopped at exposure={best_settings.get('exposure')} "
                f"(luma {best_luma:.0f}, target {int(target_luma)} — not fully converged)."
            ),
            analysis,
        )

    return best_settings, analysis


# ---------------------------------------------------------------------------
# Target-plate search: USB camera
# ---------------------------------------------------------------------------


def _calibrate_usb_camera_device_settings(
    role: str,
    source: int,
    controls: List[Dict[str, Any]],
    current_settings: Dict[str, int | float | bool],
    *,
    report_progress: Callable[[str, float, str, Dict[str, Any] | None], None] | None = None,
    gallery_dir: Path | None = None,
) -> tuple[Dict[str, int | float | bool], Dict[str, Any]]:
    control_by_key = {
        str(control.get("key")): control
        for control in controls
        if isinstance(control, dict) and isinstance(control.get("key"), str)
    }

    exposure_control = control_by_key.get("exposure")
    gain_control = control_by_key.get("gain")
    wb_control = control_by_key.get("white_balance_temperature")
    auto_exposure_control = control_by_key.get("auto_exposure")
    auto_wb_control = control_by_key.get("auto_white_balance")
    saturation_control = control_by_key.get("saturation")
    contrast_control = control_by_key.get("contrast")
    gamma_control = control_by_key.get("gamma")
    brightness_control = control_by_key.get("brightness")
    sharpness_control = control_by_key.get("sharpness")

    defaults = _usb_control_defaults(controls, current_settings)

    # Build baseline: auto off, gain to minimum
    baseline = dict(defaults or current_settings)
    if auto_exposure_control is not None:
        baseline["auto_exposure"] = False
    if auto_wb_control is not None:
        baseline["auto_white_balance"] = False
    if gain_control is not None:
        baseline["gain"] = _as_number(gain_control.get("min")) or 0.0

    total_steps = max(1, 7 + 2 + 4)  # bracketing + neutral + detection
    completed_steps = 0
    gallery_step = 0

    def _report(stage: str, message: str, analysis: Dict[str, Any] | None = None) -> None:
        nonlocal completed_steps
        completed_steps += 1
        if report_progress is not None:
            progress = min(0.9, completed_steps / total_steps)
            report_progress(stage, progress, message, analysis)

    def _save_gallery(
        frame: np.ndarray | None,
        stage: str,
        settings: Dict[str, int | float | bool],
        extra: Dict[str, Any] | None = None,
    ) -> None:
        nonlocal gallery_step
        if gallery_dir is None or frame is None:
            return
        gallery_step += 1
        prefix = f"step_{gallery_step:03d}_{stage}"
        cv2.imwrite(str(gallery_dir / f"{prefix}.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        meta: Dict[str, Any] = {"stage": stage, "step": gallery_step, "settings": {k: v for k, v in settings.items()}}
        if extra:
            meta.update(extra)
        (gallery_dir / f"{prefix}.json").write_text(json.dumps(meta, indent=2, default=str))

    settings = dict(baseline)

    def _apply_and_grab(s: Dict[str, int | float | bool]) -> np.ndarray | None:
        ts = time.time()
        preview_camera_device_settings(role, s)
        time.sleep(0.25)
        frame = _grab_live_frame(role, after_timestamp=ts)
        if frame is None:
            frame = _capture_raw_frame(role, source, s)
        return frame

    # ------------------------------------------------------------------
    # Phase 0: Debevec response curve → direct exposure calculation
    # Capture 5-7 frames at log-spaced exposures, estimate the camera
    # response function, compute optimal exposure from the HDR map.
    # ------------------------------------------------------------------


    if exposure_control is not None:
        exp_min = _as_number(exposure_control.get("min")) or 1.0
        exp_max = _as_number(exposure_control.get("max")) or 10000.0

        # Generate 7 log-spaced exposure values across the range
        # UVC exposure_absolute is typically in 0.1ms units (linear)
        # If min <= 0 (some cameras use log2 scale), use linear spacing instead
        if exp_min > 0:
            bracket_exposures = np.geomspace(exp_min, exp_max, num=7).tolist()
        else:
            bracket_exposures = np.linspace(exp_min, exp_max, num=7).tolist()

        bracket_exposures = [
            _clamp_control(val, exposure_control) for val in bracket_exposures
        ]
        # Deduplicate after quantization
        bracket_exposures = list(dict.fromkeys(bracket_exposures))

        if len(bracket_exposures) >= 3:
            bracket_frames: list[np.ndarray] = []
            bracket_times: list[float] = []

            for i, exp_val in enumerate(bracket_exposures):
                candidate = dict(settings)
                candidate["exposure"] = exp_val
                frame = _apply_and_grab(candidate)
                if frame is not None:
                    bracket_frames.append(frame)
                    # Use exposure value as "time" — ratios are what matter
                    bracket_times.append(max(float(exp_val), 1e-6) if exp_min > 0 else 2.0 ** float(exp_val))
                    _save_gallery(frame, "bracket", candidate, {"exposure": exp_val, "bracket_index": i})
                _report("bracket", f"Bracketing {i + 1}/{len(bracket_exposures)} — exposure={exp_val:.1f}")

            if len(bracket_frames) >= 3:
                times_array = np.array(bracket_times, dtype=np.float32)

                try:
                    calibrate_debevec = cv2.createCalibrateDebevec(samples=50, lambda_=10.0)
                    response = calibrate_debevec.process(bracket_frames, times_array)
                    # response shape: (256, 1, 3) — log exposure for each pixel value per channel

                    merge_debevec = cv2.createMergeDebevec()
                    hdr = merge_debevec.process(bracket_frames, times_array, response)
                    # hdr shape: (H, W, 3) float32 — radiance map

                    response_squeezed = response.squeeze(1)

                    # Calculate optimal exposure from HDR map
                    hdr_gray = cv2.cvtColor(hdr, cv2.COLOR_BGR2GRAY)
                    p99_radiance = float(np.percentile(hdr_gray[hdr_gray > 0], 99))

                    if p99_radiance > 0:
                        # We want p99 to map to pixel value ~235
                        # From response curve: find the exposure time where
                        # the response function outputs 235
                        target_log_exp = float(response_squeezed[235, 1])  # use green channel
                        target_exposure_product = np.exp(target_log_exp)
                        optimal_time = target_exposure_product / p99_radiance

                        # Convert back to UVC exposure units
                        if exp_min > 0:
                            optimal_exposure = optimal_time
                        else:
                            optimal_exposure = np.log2(max(optimal_time, 1e-10))

                        optimal_exposure = _clamp_control(float(optimal_exposure), exposure_control)
                        settings["exposure"] = optimal_exposure

                        # Check if gain is needed
                        if gain_control is not None:
                            frame_check = _apply_and_grab(settings)
                            if frame_check is not None:
                                gray = cv2.cvtColor(frame_check, cv2.COLOR_BGR2GRAY)
                                p99_check = float(np.percentile(gray, 99))
                                if p99_check < 180:
                                    # Need gain boost
                                    gain_needed = 235.0 / max(p99_check, 1.0)
                                    gain_min = _as_number(gain_control.get("min")) or 0.0
                                    gain_max = _as_number(gain_control.get("max")) or 255.0
                                    # Scale gain proportionally
                                    current_gain = float(settings.get("gain", gain_min))
                                    settings["gain"] = _clamp_control(
                                        current_gain + (gain_max - gain_min) * min(gain_needed - 1.0, 1.0) * 0.5,
                                        gain_control,
                                    )

                        _report("bracket", f"Debevec: optimal exposure={optimal_exposure:.1f}")
                    else:
                        _report("bracket", "Debevec: p99 radiance is zero, falling back to mid-range exposure.")
                        settings["exposure"] = _clamp_control((exp_min + exp_max) / 2.0, exposure_control)

                except Exception as exc:
                    _report("bracket", f"Debevec failed ({exc}), falling back to binary search.")
                    # Fallback: simple binary search
                    settings["exposure"] = _clamp_control((exp_min + exp_max) / 2.0, exposure_control)
            else:
                settings["exposure"] = _clamp_control((exp_min + exp_max) / 2.0, exposure_control)
        else:
            settings["exposure"] = _clamp_control((exp_min + exp_max) / 2.0, exposure_control)

    # Quick verify: capture a frame and check histogram
    verify_frame = _apply_and_grab(settings)
    if verify_frame is not None:
        gray = cv2.cvtColor(verify_frame, cv2.COLOR_BGR2GRAY)
        p1 = float(np.percentile(gray, 1))
        p99 = float(np.percentile(gray, 99))
        _save_gallery(verify_frame, "exposure_verify", settings, {"p1": p1, "p99": p99})
        _report("bracket", f"Exposure verify — p1={p1:.0f} p99={p99:.0f}")

        # If way off, do a quick 4-step binary search correction
        if p99 > 250 or p99 < 180:
            exp_min = _as_number(exposure_control.get("min")) or 1.0 if exposure_control else 1.0
            exp_max = _as_number(exposure_control.get("max")) or 10000.0 if exposure_control else 10000.0
            low, high = exp_min, exp_max
            for _ in range(4):
                trial = _clamp_control((low + high) / 2.0, exposure_control) if exposure_control else (low + high) / 2.0
                settings["exposure"] = trial
                f = _apply_and_grab(settings)
                if f is None:
                    continue
                g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
                p99 = float(np.percentile(g, 99))
                if p99 > 240:
                    high = trial
                elif p99 < 230:
                    low = trial
                else:
                    break

    # ------------------------------------------------------------------
    # Phase 1: Set WB / saturation / gamma / contrast to neutral
    # ------------------------------------------------------------------

    def _control_neutral(ctrl: Dict[str, Any] | None) -> float | None:
        """Return the neutral/default value for a control, or midpoint if unknown."""
        if ctrl is None:
            return None
        default = ctrl.get("default")
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            return float(default)
        c_min = _as_number(ctrl.get("min"))
        c_max = _as_number(ctrl.get("max"))
        if c_min is not None and c_max is not None:
            return (c_min + c_max) / 2.0
        return None

    for key, ctrl in [
        ("white_balance_temperature", wb_control),
        ("saturation", saturation_control),
        ("gamma", gamma_control),
        ("contrast", contrast_control),
        ("brightness", brightness_control),
    ]:
        neutral = _control_neutral(ctrl)
        if neutral is not None:
            settings[key] = _clamp_control(neutral, ctrl)
    # Sharpening to minimum (adds artifacts)
    if sharpness_control is not None:
        sharpness_min = _as_number(sharpness_control.get("min"))
        if sharpness_min is not None:
            settings["sharpness"] = sharpness_min

    # Apply neutral settings so the capture thread picks them up
    _apply_and_grab(settings)
    _report("neutral", "Firmware color controls set to neutral.")
    _save_gallery(None, "neutral_settings", settings, {"note": "firmware color controls set to neutral"})

    # ------------------------------------------------------------------
    # Phase 2: Detect the calibration target
    # With good exposure + neutral tone controls, detection should be reliable.
    # ------------------------------------------------------------------

    _report("detection", "Detecting calibration target.")

    best_settings: Dict[str, int | float | bool] = dict(settings)
    best_analysis: Dict[str, Any] | None = None

    # Capture 3 frames, analyze each, keep best
    for attempt in range(3):
        f = _capture_frame_for_calibration(role, source, fallback_settings=settings)
        if f is not None:
            result = analyze_color_plate_target(f)
            if result is not None:
                analysis_dict = result.to_dict()
                if best_analysis is None or _calibration_selection_value(analysis_dict) > _calibration_selection_value(best_analysis):
                    best_analysis = analysis_dict
                    _save_gallery(f, "detection", settings, {"detected": True, "score": result.score, "attempt": attempt})

    # Fallback: try live grab
    if best_analysis is None:
        frame = _apply_and_grab(settings)
        if frame is not None:
            result = analyze_color_plate_target(frame)
            if result is not None:
                best_analysis = result.to_dict()
                _save_gallery(frame, "detection_live", settings, {"detected": True, "score": result.score})

    if best_analysis is None:
        raise HTTPException(
            status_code=400,
            detail="Calibration target not detected. Make sure the 6-color calibration plate "
            "is fully visible and well lit.",
        )

    _report("detection", "Calibration target detected.", best_analysis)
    return best_settings, best_analysis


# ---------------------------------------------------------------------------
# Target-plate search: Android camera app
# ---------------------------------------------------------------------------


def _calibrate_android_camera_device_settings(
    role: str,
    source: str,
    current_settings: Dict[str, Any],
    capabilities: Dict[str, Any],
    *,
    report_progress: Callable[[str, float, str, Dict[str, Any] | None], None] | None = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    white_balance_modes = [
        str(mode)
        for mode in capabilities.get("white_balance_modes", ["auto"])
        if isinstance(mode, str) and mode
    ] or ["auto"]
    preferred_wb_mode = "auto" if "auto" in white_balance_modes else white_balance_modes[0]
    exposure_min = int(capabilities.get("exposure_compensation_min", 0))
    exposure_max = int(capabilities.get("exposure_compensation_max", 0))
    neutral_exposure = int(max(exposure_min, min(exposure_max, 0)))

    base_settings = {
        "exposure_compensation": neutral_exposure,
        "ae_lock": False,
        "awb_lock": False,
        "white_balance_mode": preferred_wb_mode,
        "processing_mode": str(current_settings.get("processing_mode", "standard")),
    }

    best_settings: Dict[str, int | float | bool] | None = None
    best_analysis: Dict[str, Any] | None = None
    total_steps = 1

    def tick(stage: str, progress: float, message: str, analysis: Dict[str, Any] | None = None) -> None:
        if report_progress is not None:
            report_progress(stage, min(0.9, progress), message, analysis)

    steps_done = 0

    def consider(candidate: Dict[str, Any], *, stage: str, message: str) -> None:
        nonlocal best_settings, best_analysis
        nonlocal steps_done
        steps_done += 1
        tick(stage, steps_done / total_steps, message)
        applied_settings, analysis, _ = _analyze_candidate_settings(role, source, candidate)
        if analysis is None:
            return
        if best_analysis is None or _calibration_selection_value(analysis) > _calibration_selection_value(best_analysis):
            best_settings = dict(applied_settings)
            best_analysis = analysis
            tick(stage, steps_done / total_steps, message, analysis)

    exposure_values = sorted(
        {
            exposure_min,
            exposure_max,
            neutral_exposure,
            int(current_settings.get("exposure_compensation", neutral_exposure)),
            *[
                int(round(value))
                for value in np.linspace(exposure_min, exposure_max, num=max(3, min(7, exposure_max - exposure_min + 1))).tolist()
            ],
        }
    )
    total_steps = max(1, 1 + len(exposure_values) + len(white_balance_modes))

    consider(
        base_settings,
        stage="baseline",
        message="Analyzing baseline candidate 1 of 1.",
    )

    for index, exposure_value in enumerate(exposure_values, start=1):
        candidate = dict(base_settings)
        candidate["exposure_compensation"] = int(exposure_value)
        consider(
            candidate,
            stage="exposure_search",
            message=f"Evaluating exposure candidate {index} of {len(exposure_values)}.",
        )

    if best_settings is None or best_analysis is None:
        raise HTTPException(
            status_code=400,
            detail="Calibration target not found. Make sure the 6-color calibration plate is fully visible and not clipped.",
        )

    # Refine exposure: try values around the best with finer steps
    if best_settings is not None and exposure_min != exposure_max:
        best_exp = int(best_settings.get("exposure_compensation", 0))
        refine_values = sorted(
            {
                value
                for value in range(max(exposure_min, best_exp - 2), min(exposure_max, best_exp + 2) + 1)
                if value != best_exp
            }
        )
        total_steps += len(refine_values)
        for index, exp_value in enumerate(refine_values, start=1):
            candidate = dict(best_settings)
            candidate["exposure_compensation"] = int(exp_value)
            candidate["ae_lock"] = False
            candidate["awb_lock"] = False
            consider(
                candidate,
                stage="exposure_refine",
                message=f"Refining exposure candidate {index} of {len(refine_values)}.",
            )

    wb_base = dict(best_settings)
    wb_base["ae_lock"] = False
    wb_base["awb_lock"] = False
    for index, mode in enumerate(white_balance_modes, start=1):
        candidate = dict(wb_base)
        candidate["white_balance_mode"] = mode
        consider(
            candidate,
            stage="white_balance_search",
            message=f"Evaluating white balance candidate {index} of {len(white_balance_modes)}.",
        )

    if best_settings is None or best_analysis is None:
        raise HTTPException(status_code=400, detail="Calibration failed to find usable settings.")

    # Final polish: try the best settings with locks to verify stability
    total_steps += 1
    polish_candidate = dict(best_settings)
    if bool(capabilities.get("supports_ae_lock")):
        polish_candidate["ae_lock"] = True
    if bool(capabilities.get("supports_awb_lock")):
        polish_candidate["awb_lock"] = True
    consider(
        polish_candidate,
        stage="polish_search",
        message="Verifying with exposure and white balance locked.",
    )

    if bool(capabilities.get("supports_ae_lock")):
        best_settings["ae_lock"] = True
    if bool(capabilities.get("supports_awb_lock")):
        best_settings["awb_lock"] = True

    return best_settings, best_analysis
