from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from perception.runtime import ProcessorRuntime


class _Processor:
    def __init__(self) -> None:
        self.calls = []

    def infer(self, image, *, conf_threshold=None):
        self.calls.append((image, conf_threshold))
        return [SimpleNamespace(bbox=(1, 2, 3, 4), score=0.75)]


def test_processor_runtime_uses_selected_onnx_factory_and_scores() -> None:
    processor = _Processor()
    image = np.zeros((4, 4, 3), dtype=np.uint8)

    with patch("vision.ml.factory.create_processor", return_value=processor) as factory:
        runtime = ProcessorRuntime(
            model_path="model.onnx",
            imgsz=320,
            runtime="onnx",
            model_family="yolo",
            conf_threshold=0.2,
        )

        assert runtime.inferWithScores(image, conf_threshold=0.3) == [
            ((1, 2, 3, 4), 0.75)
        ]

    factory.assert_called_once_with(
        model_path="model.onnx",
        model_family="yolo",
        runtime="onnx",
        imgsz=320,
        conf_threshold=0.2,
        iou_threshold=0.45,
    )
    assert processor.calls == [(image, 0.3)]