"""Inference smoke check using Google's public MediaPipe pose example image."""
import json
import os
import time
import urllib.request
import cv2
os.environ.setdefault('DJANGO_SETTINGS_MODULE','peoplesense.settings')
from .detector import PersonDetector
from django.conf import settings

def main():
    directory = settings.BASE_DIR / 'runtime' / 'verification'
    directory.mkdir(parents=True,exist_ok=True)
    image_path = directory/'pose.jpg'
    if not image_path.exists():
        urllib.request.urlretrieve('https://storage.googleapis.com/mediapipe-assets/pose.jpg',image_path)
    image = cv2.imread(str(image_path))
    if image is None:
        raise RuntimeError('Could not load public test image.')
    for model in ['efficientdet_lite0','yolox_nano']:
        detector = PersonDetector(settings.VISION_MODEL_DIR,model,.3)
        began=time.perf_counter()
        detections=detector.detect(image,0)
        elapsed=round((time.perf_counter()-began)*1000,1)
        detector.close()
        if not detections:
            raise RuntimeError(f'{model} did not detect a person in the example.')
        print(json.dumps({'model':model,'inference_ms':elapsed,'person_boxes':detections}))

if __name__=='__main__':main()
