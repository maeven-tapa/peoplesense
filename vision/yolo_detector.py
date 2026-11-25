"""Pinned YOLOX-Nano ONNX, using OpenCV DNN on one CPU thread.

The official export expects unscaled BGR pixels, top-left letterboxing,
and stride-grid decoding. Only COCO person scores are postprocessed.
No PyTorch, Ultralytics, ONNX Runtime, or TensorFlow import is required.
"""
import cv2
import numpy as np
from .model_store import verify_model


class YoloNanoDetector:
    input_size = 416

    def __init__(self, model_dir, model_name='yolox_nano', confidence=.45):
        path = verify_model(model_dir, model_name)
        cv2.setNumThreads(1)
        self.net = cv2.dnn.readNetFromONNX(str(path))
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self.confidence = confidence
        grids, strides = [], []
        for stride in (8, 16, 32):
            side = self.input_size // stride
            x, y = np.meshgrid(np.arange(side), np.arange(side))
            grids.append(np.stack((x, y), axis=-1).reshape(-1, 2))
            strides.append(np.full((side * side, 1), stride))
        self.grid = np.concatenate(grids).astype(np.float32)
        self.strides = np.concatenate(strides).astype(np.float32)
        self.padded = np.empty((self.input_size, self.input_size, 3), dtype=np.uint8)

    def detect(self, frame, timestamp_ms):
        height, width = frame.shape[:2]
        ratio = min(self.input_size / height, self.input_size / width)
        resized = cv2.resize(frame, (max(1, int(width * ratio)), max(1, int(height * ratio))))
        self.padded.fill(114)
        self.padded[:resized.shape[0], :resized.shape[1]] = resized
        # YOLOX uses BGR 0..255, unlike YOLOv8's RGB 0..1 preprocessing.
        self.net.setInput(cv2.dnn.blobFromImage(self.padded, scalefactor=1., swapRB=False))
        output = self.net.forward()
        if output.shape != (1, len(self.grid), 85):
            raise ValueError('Unexpected YOLOX-Nano output shape. Download the pinned model again.')
        rows = output[0]
        scores = rows[:, 4] * rows[:, 5]  # objectness × COCO class 0 (person)
        indices = np.flatnonzero(np.isfinite(scores) & (scores >= self.confidence))
        indices = indices[np.argsort(scores[indices])[::-1][:300]]
        if not len(indices):
            return []
        raw = rows[indices, :4]
        center = (raw[:, :2] + self.grid[indices]) * self.strides[indices]
        size = np.exp(np.clip(raw[:, 2:4], -20, 20)) * self.strides[indices]
        boxes = np.column_stack((center - size / 2, center + size / 2))
        boxes /= np.array([width, height, width, height], dtype=np.float32) * ratio
        boxes = np.clip(boxes, 0, 1)
        valid = np.isfinite(raw).all(axis=1) & (boxes[:, 2] > boxes[:, 0]) & (boxes[:, 3] > boxes[:, 1])
        boxes, values = boxes[valid], scores[indices][valid]
        xywh = np.column_stack((boxes[:, :2], boxes[:, 2:] - boxes[:, :2]))
        keep = cv2.dnn.NMSBoxes(xywh.tolist(), values.tolist(), self.confidence, .5, top_k=50)
        return [(*map(float, boxes[int(i)]), float(values[int(i)])) for i in np.asarray(keep).reshape(-1)]

    def close(self):
        self.net = None
        self.padded = None
