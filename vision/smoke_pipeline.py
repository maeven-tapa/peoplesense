"""Exercise real OpenCV decoding, person inference, tracking, and Django writes.

Uses a generated clip based on Google's public pose sample. A temporary DB
keeps verification events separate from the user's building records.
"""
import json
import os
from pathlib import Path
import tempfile
import time

def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--model', choices=['efficientdet_lite0', 'yolox_nano'], default='efficientdet_lite0')
    model_name=parser.parse_args().model
    base=Path(__file__).resolve().parent.parent
    directory=base/'runtime'/'verification'
    directory.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='pipeline-',dir=directory) as temporary:
        temporary=Path(temporary).resolve()
        assert temporary.is_relative_to(directory.resolve())
        os.environ['DJANGO_DB_PATH']=str(temporary/'verify.sqlite3')
        os.environ['DJANGO_SETTINGS_MODULE']='peoplesense.settings'
        import django
        django.setup()
        from django.conf import settings
        from django.core.management import call_command
        from portal.models import Device,Detection
        from .detector import PersonDetector
        from .service import claim_camera
        from .worker import run_camera
        from .smoke_detector import main as detector_check
        import cv2
        import numpy as np
        if not (directory/'pose.jpg').exists():detector_check()
        source=cv2.imread(str(directory/'pose.jpg'))
        detector=PersonDetector(settings.VISION_MODEL_DIR,model_name,confidence=.3)
        person=detector.detect(source,0)[0]
        detector.close()
        h,w=source.shape[:2]
        x1,y1,x2,y2=[int(person[i]*(w if i%2==0 else h)) for i in range(4)]
        crop=source[max(0,y1-20):min(h,y2+15),max(0,x1-20):min(w,x2+20)]
        patch=cv2.resize(crop,(round(crop.shape[1]*220/crop.shape[0]),220))
        video=temporary/'crossings.avi'
        writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'MJPG'),8,(960,540))
        if not writer.isOpened():raise RuntimeError('MJPG test encoder unavailable.')
        positions=[235]*8+list(range(235,421,5))+[420]*16+list(range(420,234,-5))+[235]*8
        for foot in positions:
            frame=np.full((540,960,3),205,np.uint8)
            ph,pw=patch.shape[:2];left=(960-pw)//2;top=foot-ph
            frame[top:top+ph,left:left+pw]=patch
            writer.write(frame)
        writer.release()
        settings.CAMERA_RUNTIME_DIR=temporary/'frames'
        call_command('migrate',verbosity=0)
        camera=Device.objects.create(name='Verification camera',location='Isolated test',source_type='file',camera_source=str(video),
            detection_model=model_name,follow_camera=False,temperature_enabled=False,confidence_threshold=.3,line_ay=.60,line_by=.60)
        began=time.perf_counter()
        result=run_camera(camera.pk,claim_camera(camera.pk))
        directions=list(Detection.objects.order_by('timestamp').values_list('direction',flat=True))
        from django.db import connections
        connections.close_all()  # Windows cannot remove an open SQLite file.
        output={'model':model_name,'test':'real model on a generated forward / hold / reverse clip',
            'result':result,'recorded_directions':directions,'elapsed_seconds':round(time.perf_counter()-began,2),
            'fixture_source':'https://storage.googleapis.com/mediapipe-assets/pose.jpg'}
        (base/'artifacts').mkdir(exist_ok=True)
        filename='yolo-vision-verification.json' if model_name=='yolox_nano' else 'vision-verification.json'
        (base/'artifacts'/filename).write_text(json.dumps(output,indent=2),encoding='utf-8')
        print(json.dumps(output,indent=2))
        if directions!=['in','out'] or result['status']!='finished':
            raise RuntimeError('Expected exactly one entry and one exit in the verification clip.')

if __name__=='__main__':main()
