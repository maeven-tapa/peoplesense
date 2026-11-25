"""Synthetic capture + real models; no webcam, database events, or saved photos."""
import ctypes
import json
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from .runtime import configure_native_threads

configure_native_threads()

os.environ.setdefault('DJANGO_SETTINGS_MODULE','peoplesense.settings')
import django
django.setup()
from django.conf import settings
import cv2
from .detector import PersonDetector
from .preview import PreviewBuffer, preview_path, SIZE
from .worker import LatestFrames


def working_set_mib():
    if os.name != 'nt':
        return None
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in ['PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage']]
    memory=Counters();memory.cb=ctypes.sizeof(memory)
    kernel=ctypes.WinDLL('kernel32');kernel.GetCurrentProcess.restype=wintypes.HANDLE
    psapi=ctypes.WinDLL('psapi');psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(memory),memory.cb):
        raise ctypes.WinError()
    return round(memory.WorkingSetSize/(1024**2),1)


class SyntheticCapture:
    def __init__(self,frame,fps):
        self.frame=frame;self.fps=fps;self.deadline=time.monotonic()
    def read(self):
        self.deadline+=1/self.fps
        time.sleep(max(0,self.deadline-time.monotonic()))
        return True,self.frame
    def release(self):pass
    def set(self,_,fps):self.fps=fps


def main():
    cv2.setNumThreads(1)
    image=cv2.imread(str(settings.BASE_DIR / 'runtime' / 'verification' / 'pose.jpg'))
    if image is None:
        raise RuntimeError('Run vision.smoke_detector first to obtain the public test image.')
    ratio=min(640/image.shape[1],480/image.shape[0])
    image=cv2.resize(image,(round(image.shape[1]*ratio),round(image.shape[0]*ratio)))
    results=[]
    for model in ['efficientdet_lite0','yolox_nano']:
        detector=PersonDetector(settings.VISION_MODEL_DIR,model,.3)
        timestamp=0
        detector.detect(image,timestamp)
        try:
            for mode in ['tripwire','surveillance']:
                for target in [30,60]:
                    with tempfile.TemporaryDirectory() as folder:
                        writer=PreviewBuffer(preview_path(folder,0,'benchmark'),create=True)
                        reader=PreviewBuffer(writer.path)
                        live=LatestFrames(SyntheticCapture(image,target),writer,target)
                        started=time.monotonic();next_inference=started;inferences=0;ram_start=working_set_mib();peak_ram=ram_start or 0;max_queue=0;persons=0
                        try:
                            while time.monotonic()-started<3:
                                _,frame=live.get()
                                now=time.monotonic();max_queue=max(max_queue,live.frames.qsize())
                                if now<next_inference:continue
                                next_inference=now+1/8
                                timestamp+=150
                                boxes=detector.detect(frame,timestamp);persons=max(persons,len(boxes));inferences+=1
                                tracks=[SimpleNamespace(id=i,box=box,point=((box[0]+box[2])/2,box[3])) for i,box in enumerate(boxes)]
                                live.set_overlay(tracks,SimpleNamespace(camera_mode=mode,follow_camera=False,line_tracking_status='fixed'),0,0,8)
                                peak_ram=max(peak_ram,working_set_mib() or 0)
                            latest=reader.read();elapsed=time.monotonic()-started
                            results.append({'model':model,'mode':mode,'preview_target_fps':target,'preview_measured_fps':round(latest[2],1),'ai_measured_fps':round(inferences/elapsed,1),'people_detected':persons,'working_set_start_mib':ram_start,'working_set_peak_mib':peak_ram,'queue_limit':live.frames.maxsize,'observed_queued_frames':max_queue,'ipc_bytes':SIZE})
                        finally:reader.close();live.close()
        finally:detector.close()
    report={'capture':'synthetic, real model inference; no physical USB validation','preview_maximum':'640x480','duration_seconds_per_scenario':3,'results':results}
    (settings.BASE_DIR / 'artifacts').mkdir(exist_ok=True)
    (settings.BASE_DIR / 'artifacts' / 'preview-performance.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
