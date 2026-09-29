import queue
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
import cv2
import numpy as np

BLOB_DIR = Path(__file__).parent / "blob"


def getMachineId() -> str:
    from local_state import get_machine_id, set_machine_id

    machine_id = get_machine_id()
    if machine_id is not None:
        return machine_id

    machine_id = str(uuid.uuid4())
    set_machine_id(machine_id)
    return machine_id


def getMachineNickname() -> str | None:
    from toml_config import getMachineNickname as _get
    return _get()


def setMachineNickname(nickname: str | None) -> None:
    from toml_config import setMachineNickname as _set
    _set(nickname)


def getBinCategories() -> list[list[list[list[str]]]] | None:
    from bin_layout_store import get_bin_categories

    return get_bin_categories()


def setBinCategories(categories: list[list[list[list[str]]]]) -> None:
    from bin_layout_store import set_bin_categories

    set_bin_categories(categories)


def getNotInInventoryBins() -> list[list[list[bool]]] | None:
    from bin_layout_store import get_not_in_inventory_bins

    return get_not_in_inventory_bins()


def setNotInInventoryBins(flags: list[list[list[bool]]]) -> None:
    from bin_layout_store import set_not_in_inventory_bins

    set_not_in_inventory_bins(flags)


def getCameraSetup() -> dict | None:
    from toml_config import getCameraSetup as _get
    return _get()


def setCameraSetup(setup: dict) -> None:
    from toml_config import setCameraSetup as _set
    _set(setup)


def getChannelPolygons() -> dict | None:
    from local_state import get_channel_polygons

    return get_channel_polygons()


def setChannelPolygons(polygons: dict) -> None:
    from local_state import set_channel_polygons

    set_channel_polygons(polygons)


def getChuteCalibration() -> dict[str, float] | None:
    from toml_config import getChuteCalibration as _get
    return _get()


def setChuteCalibration(calibration: dict[str, float]) -> None:
    from toml_config import setChuteCalibration as _set
    _set(calibration)


def getClassificationPolygons() -> dict | None:
    from local_state import get_classification_polygons

    return get_classification_polygons()


def setClassificationPolygons(polygons: dict) -> None:
    from local_state import set_classification_polygons

    set_classification_polygons(polygons)


def getClassificationDetectionConfig() -> dict | None:
    from toml_config import getDetectionConfig
    return getDetectionConfig("classification")


def setClassificationDetectionConfig(config: dict) -> None:
    from toml_config import setDetectionConfig
    setDetectionConfig("classification", config)


def getFeederDetectionConfig() -> dict | None:
    from toml_config import getDetectionConfig
    return getDetectionConfig("feeder")


def setFeederDetectionConfig(config: dict) -> None:
    from toml_config import setDetectionConfig
    setDetectionConfig("feeder", config)


def getCarouselDetectionConfig() -> dict | None:
    from toml_config import getDetectionConfig
    return getDetectionConfig("carousel")


def setCarouselDetectionConfig(config: dict) -> None:
    from toml_config import setDetectionConfig
    setDetectionConfig("carousel", config)


def getClassificationTrainingConfig() -> dict | None:
    from local_state import get_classification_training_state

    return get_classification_training_state()


def setClassificationTrainingConfig(config: dict) -> None:
    from local_state import set_classification_training_state

    set_classification_training_state(config)


def getSampleCollectionConfig() -> dict | None:
    from local_state import get_sample_collection_state

    return get_sample_collection_state()


def setSampleCollectionConfig(config: dict) -> None:
    from local_state import set_sample_collection_state

    set_sample_collection_state(config)


def getHiveConfig() -> dict | None:
    from local_state import get_hive_config

    return get_hive_config()


def setHiveConfig(config: dict) -> None:
    from local_state import set_hive_config

    set_hive_config(config)


def getSortingProfileSyncState() -> dict | None:
    from local_state import get_sorting_profile_sync_state

    return get_sorting_profile_sync_state()


def setSortingProfileSyncState(state: dict) -> None:
    from local_state import set_sorting_profile_sync_state

    set_sorting_profile_sync_state(state)


def getApiKeys() -> dict:
    from local_state import get_api_keys

    return get_api_keys()


def setApiKeys(keys: dict) -> None:
    from local_state import set_api_keys

    set_api_keys(keys)


class VideoRecorder:
    _run_dir: Path
    _writers: dict[str, cv2.VideoWriter]
    _fps: int
    _queue: queue.Queue[tuple[str, np.ndarray, float] | None]
    _thread: threading.Thread
    _start_times: dict[str, float]
    _frame_counts: dict[str, int]

    def __init__(self, fps: int = 10):
        self._fps = fps
        self._writers = {}
        self._queue = queue.Queue(maxsize=120)
        self._start_times = {}
        self._frame_counts = {}
        self._last_frames: dict[str, np.ndarray] = {}

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._run_dir = BLOB_DIR / timestamp
        self._run_dir.mkdir(parents=True, exist_ok=True)

        self._thread = threading.Thread(target=self._writerLoop, daemon=True)
        self._thread.start()

    def _getWriter(self, key: str, frame: np.ndarray) -> cv2.VideoWriter:
        if key not in self._writers:
            h, w = frame.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            path = self._run_dir / f"{key}.mp4"
            self._writers[key] = cv2.VideoWriter(str(path), fourcc, self._fps, (w, h))
        return self._writers[key]

    def _writerLoop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            key, frame, ts = item
            writer = self._getWriter(key, frame)

            if key not in self._start_times:
                self._start_times[key] = ts
                self._frame_counts[key] = 0

            elapsed = ts - self._start_times[key]
            target_frame = int(elapsed * self._fps)
            current_count = self._frame_counts[key]

            gap = target_frame - current_count
            if gap > 1:
                fill = self._last_frames.get(key)
                if fill is not None:
                    for _ in range(min(gap - 1, self._fps * 2)):
                        writer.write(fill)
                        self._frame_counts[key] += 1

            writer.write(frame)
            self._frame_counts[key] += 1
            self._last_frames[key] = frame

    def writeFrame(
        self, camera: str, raw: Optional[np.ndarray], annotated: Optional[np.ndarray]
    ) -> None:
        ts = time.time()
        if raw is not None:
            try:
                self._queue.put_nowait((f"{camera}_raw", raw.copy(), ts))
            except queue.Full:
                pass
        if annotated is not None:
            try:
                self._queue.put_nowait((f"{camera}_annotated", annotated.copy(), ts))
            except queue.Full:
                pass

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=10.0)
        for writer in self._writers.values():
            writer.release()
        self._writers.clear()
