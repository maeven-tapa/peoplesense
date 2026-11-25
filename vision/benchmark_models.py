"""Compare detectors in fresh processes without opening a camera or writing events."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from .runtime import configure_native_threads


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', choices=['efficientdet_lite0', 'yolox_nano'])
    parser.add_argument('--samples', type=int, default=30)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be positive')
    base = Path(__file__).resolve().parent.parent
    if args.model is None:
        results = []
        for name in ['efficientdet_lite0', 'yolox_nano']:
            output = subprocess.check_output([sys.executable, '-m', 'vision.benchmark_models',
                '--model', name, '--samples', str(args.samples)], cwd=base, text=True)
            results.append(json.loads(output))
        report = {'platform': sys.platform, 'input': '640x427 public pose fixture',
                  'scope': 'Fresh process per model; includes Django/OpenCV. Not a Pi benchmark.',
                  'results': results}
        destination = base / 'artifacts' / 'model-resource-comparison.json'
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report, indent=2))
        return
    configure_native_threads()
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'peoplesense.settings')
    import cv2
    import numpy as np
    from django.conf import settings
    from .detector import PersonDetector
    from .benchmark_preview import working_set_mib
    cv2.setNumThreads(1)
    image = cv2.imread(str(base / 'runtime' / 'verification' / 'pose.jpg'))
    if image is None:
        raise RuntimeError('A local pose fixture is needed; run vision.smoke_detector first.')
    image = cv2.resize(image, (640, round(image.shape[0] * 640 / image.shape[1])))
    detector = PersonDetector(settings.VISION_MODEL_DIR, args.model, .3)
    try:
        for i in range(5):
            detector.detect(image, i)
        timings = []
        max_ram = working_set_mib()
        for i in range(args.samples):
            began = time.perf_counter()
            boxes = detector.detect(image, i + 5)
            timings.append((time.perf_counter() - began) * 1000)
            ram = working_set_mib()
            if ram is not None:
                max_ram = max(max_ram or 0, ram)
        result = {'model': args.model, 'samples': args.samples, 'people': len(boxes),
                  'mean_inference_ms': round(float(np.mean(timings)), 2),
                  'p95_inference_ms': round(float(np.percentile(timings, 95)), 2),
                  'process_working_set_mib': working_set_mib(), 'sampled_peak_working_set_mib': max_ram}
        print(json.dumps(result))
    finally:
        detector.close()


if __name__ == '__main__':
    main()
