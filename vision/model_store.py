import hashlib
import json
import os
import urllib.request
from pathlib import Path

MODELS = {
    'efficientdet_lite0': {
        'filename': 'efficientdet_lite0_int8.tflite',
        'url': 'https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/int8/1/efficientdet_lite0.tflite',
        'name': 'EfficientDet-Lite0 INT8',
        'sha256': '0720bf247bd76e6594ea28fa9c6f7c5242be774818997dbbeffc4da460c723bb',
        'source': 'https://developers.google.com/edge/mediapipe/solutions/vision/object_detector',
        'format': 'tflite',
    },
    'yolox_nano': {
        'filename': 'yolox_nano.onnx',
        'url': 'https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx',
        'name': 'YOLOX-Nano · low resource',
        'sha256': 'c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d',
        'source': 'https://github.com/Megvii-BaseDetection/YOLOX/tree/main/demo/ONNXRuntime',
        'format': 'onnx',
    },
}

def model_path(directory, name):
    return Path(directory) / MODELS[name]['filename']

def download_model(directory, name):
    info = MODELS[name]
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    destination = model_path(directory, name)
    partial = destination.with_suffix('.part')
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(info['url'], timeout=60) as response, partial.open('wb') as output:
            while chunk := response.read(1024*1024):
                size += len(chunk)
                if size > 50*1024*1024:
                    raise ValueError('Unexpected model size.')
                digest.update(chunk)
                output.write(chunk)
        with partial.open('rb') as downloaded:
            header = downloaded.read(8)
        if info['format']=='tflite' and (len(header) < 8 or header[4:8] != b'TFL3'):
            raise ValueError('Downloaded file is not a TensorFlow Lite model.')
        if digest.hexdigest()!=info['sha256']:
            raise ValueError('Model hash does not match the pinned official model download.')
        os.replace(partial, destination)
        manifest = {**info, 'bytes': size, 'sha256': digest.hexdigest()}
        destination.with_suffix('.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        return manifest
    finally:
        partial.unlink(missing_ok=True)

def verify_model(directory, name):
    path = model_path(directory, name)
    manifest_path = path.with_suffix('.json')
    if not path.exists() or not manifest_path.exists():
        raise FileNotFoundError('Model not installed. Run: python manage.py download_models')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if hashlib.sha256(path.read_bytes()).hexdigest() != MODELS[name]['sha256'] or manifest['sha256']!=MODELS[name]['sha256']:
        raise ValueError('Model checksum mismatch. Download the model again.')
    return path
