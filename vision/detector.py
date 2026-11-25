"""OpenCV BGR frames -> lightweight detector -> normalized person boxes."""
import cv2
from django.conf import settings
from .model_store import verify_model

class PersonDetector:
    def __new__(cls, model_dir, model_name='efficientdet_lite0', confidence=.45):
        if model_name == 'yolox_nano':
            from .yolo_detector import YoloNanoDetector
            return YoloNanoDetector(model_dir, model_name, confidence)
        if settings.VISION_BACKEND == 'litert':
            from .litert_detector import LitePersonDetector
            return LitePersonDetector(model_dir,model_name,confidence)
        return super().__new__(cls)

    def __init__(self, model_dir, model_name='efficientdet_lite0', confidence=0.45):
        import mediapipe as mp
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision
        self.mp = mp
        path = verify_model(model_dir, model_name)
        options = vision.ObjectDetectorOptions(
            base_options=python.BaseOptions(model_asset_path=str(path)),
            running_mode=vision.RunningMode.VIDEO,
            score_threshold=confidence, category_allowlist=['person'], max_results=50)
        self.detector = vision.ObjectDetector.create_from_options(options)
        self.last_timestamp = -1

    def detect(self, frame, timestamp_ms):
        timestamp_ms = max(self.last_timestamp+1, int(timestamp_ms))
        self.last_timestamp = timestamp_ms
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        result = self.detector.detect_for_video(image, timestamp_ms)
        height, width = frame.shape[:2]
        boxes = []
        for detection in result.detections:
            box = detection.bounding_box
            x1, y1 = max(0, box.origin_x/width), max(0, box.origin_y/height)
            x2, y2 = min(1, (box.origin_x+box.width)/width), min(1, (box.origin_y+box.height)/height)
            if x2>x1 and y2>y1:
                boxes.append((x1, y1, x2, y2, detection.categories[0].score))
        return boxes

    def close(self):
        self.detector.close()
