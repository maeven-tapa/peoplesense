import math
import json
import os
import sys
import queue
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from types import SimpleNamespace
import cv2
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone
from portal.events import EventConflict, record_event
from portal.models import Device, Detection
from .detector import PersonDetector
from .service import CameraError
from .tracking import PersonTracker, TripwireCounter
from .scene import SceneAnchor
from .snapshots import capture_due, save_snapshot
from .presence import PresenceRecorder
from .preview import PreviewBuffer, preview_path
from .movement import MovementHistory, movement_path

def open_capture(device):
    if device.source_type=='usb':
        capture = cv2.VideoCapture(int(device.camera_source),cv2.CAP_DSHOW if os.name=='nt' else cv2.CAP_V4L2 if sys.platform.startswith('linux') else cv2.CAP_ANY)
        if not capture.isOpened() and os.name=='nt':
            capture.release()
            capture=cv2.VideoCapture(int(device.camera_source),cv2.CAP_ANY)
    elif device.source_type=='network':
        capture = cv2.VideoCapture(device.camera_source,cv2.CAP_FFMPEG,[cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,5000,cv2.CAP_PROP_READ_TIMEOUT_MSEC,5000])
    elif device.source_type=='file':
        if not Path(device.camera_source).is_file():
            raise CameraError('Video file not found. Check the file path on the server computer.')
        capture = cv2.VideoCapture(device.camera_source)
    else:
        raise CameraError('This device has no OpenCV capture source.')
    if not capture.isOpened():
        capture.release()
        raise CameraError('Could not open this camera. Check its source, permissions, power, and network connection.')
    capture.set(cv2.CAP_PROP_BUFFERSIZE,1)
    if device.source_type=='usb':
        # Request a small MJPEG frame before reading; unsupported modes fall back
        # to the camera's actual rate, which the preview measures independently.
        capture.set(cv2.CAP_PROP_FOURCC,cv2.VideoWriter_fourcc(*'MJPG'))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH,640)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT,480)
        capture.set(cv2.CAP_PROP_FPS,device.preview_fps)
    return capture

class LatestFrames:
    """Continuously drain a live stream so slow inference cannot count old frames."""
    def __init__(self,capture,preview=None,target_fps=30):
        self.capture = capture
        self.preview = preview
        self.target_fps = target_fps
        self.pending_fps = None
        self.overlay = None
        self.preview_times = deque(maxlen=60)
        self.frames = queue.Queue(maxsize=1)
        self.stopped = threading.Event()
        self.failed = threading.Event()
        self.thread = threading.Thread(target=self.read,daemon=True)
        self.thread.start()
    def read(self):
        next_preview = 0
        try:
            while not self.stopped.is_set():
                if self.pending_fps is not None:
                    fps,self.pending_fps = self.pending_fps,None
                    self.capture.set(cv2.CAP_PROP_FPS,fps)
                ok,frame = self.capture.read()
                if not ok:
                    self.failed.set()
                    break
                # Bound retained raw frames even when a backend ignores resolution.
                if frame.shape[1]>640 or frame.shape[0]>480:
                    ratio=min(640/frame.shape[1],480/frame.shape[0])
                    frame=cv2.resize(frame,(round(frame.shape[1]*ratio),round(frame.shape[0]*ratio)))
                now=time.monotonic()
                if self.preview and now+.45/self.target_fps >= next_preview:
                    overlay=self.overlay
                    rendered=annotate(frame,*overlay) if overlay else frame
                    ok,jpeg=cv2.imencode('.jpg',rendered,[cv2.IMWRITE_JPEG_QUALITY,70])
                    if ok:
                        self.preview_times.append(now)
                        fps=(len(self.preview_times)-1)/max(.001,now-self.preview_times[0]) if len(self.preview_times)>1 else 0
                        self.preview.publish(jpeg.tobytes(),fps)
                    # Schedule by deadline, not last arrival: USB/timer jitter
                    # must not discard every other frame at the target rate.
                    period=1/self.target_fps
                    next_preview=max(next_preview+period,now+period*.1)
                item = (now,frame)
                try: self.frames.get_nowait()
                except queue.Empty: pass
                try: self.frames.put_nowait(item)
                except queue.Full: pass
        except Exception:
            self.failed.set()
        finally:
            if self.preview:self.preview.close()

    def configure(self, target_fps):
        if target_fps != self.target_fps:
            self.target_fps=target_fps
            self.pending_fps=target_fps

    def set_overlay(self, tracks, device, entries, exits, fps):
        # Tracker objects mutate during inference. Copy only display primitives.
        tracks=tuple(SimpleNamespace(id=t.id,box=tuple(t.box),point=tuple(t.point)) for t in tracks)
        device=SimpleNamespace(camera_mode=device.camera_mode,follow_camera=device.follow_camera,line_tracking_status=device.line_tracking_status)
        self.overlay=(tracks,device,None,entries,exits,fps)
    def get(self):
        try:
            return self.frames.get(timeout=1)
        except queue.Empty:
            if self.failed.is_set():
                raise CameraError('Camera stream disconnected. Stop and restart the camera after checking connectivity.')
            return None
    def close(self):
        self.stopped.set()
        self.thread.join(timeout=6)
        if self.thread.is_alive():
            # The daemon owns a blocked backend; do not release it concurrently.
            return
        self.capture.release()

def annotate(frame,tracks,device,counter,entries,exits,fps):
    frame = frame.copy()
    h,w = frame.shape[:2]
    # The browser draws the editable line, tolerance band, and direction arrow.
    # Publishing a second, saved line here would confuse unsaved adjustments.
    for track in tracks:
        x1,y1,x2,y2 = (int(track.box[i]*(w if i%2==0 else h)) for i in range(4))
        cv2.rectangle(frame,(x1,y1),(x2,y2),(111,214,163),2)
        cv2.circle(frame,(int(track.point[0]*w),int(track.point[1]*h)),4,(111,214,163),-1)
        cv2.putText(frame,f'Person #{track.id}',(x1,max(20,y1-6)),cv2.FONT_HERSHEY_SIMPLEX,.45,(111,214,163),1)
    cv2.rectangle(frame,(0,0),(w,38),(35,30,42),-1)
    label = (f'IN {entries}   OUT {exits}' if device.camera_mode==Device.Mode.TRIPWIRE else 'SURVEILLANCE') + f'   PEOPLE {len(tracks)}   AI {fps:.1f} FPS'
    if device.camera_mode==Device.Mode.TRIPWIRE and device.follow_camera and device.line_tracking_status != 'locked':
        label += '   ALIGNMENT PAUSED'
    cv2.putText(frame,label,(12,25),cv2.FONT_HERSHEY_SIMPLEX,.5,(235,229,246),1)
    return frame

def run_camera(pk,lease,max_frames=None):
    cv2.setNumThreads(1)
    device = Device.objects.get(pk=pk)
    if not Device.objects.filter(pk=pk,worker_token=lease,worker_pid=0).update(worker_pid=os.getpid()):
        raise CameraError('Camera lease is unavailable or another worker owns it.')
    detector = capture = live = presence = preview = movement = None
    entries = exits = processed = 0
    outcome,error = 'finished',''
    try:
        # Import/load inference outside Django request handling.
        detector = PersonDetector(settings.VISION_MODEL_DIR,device.detection_model,device.confidence_threshold)
        capture = open_capture(device)
        file_source = device.source_type=='file'
        video_fps = capture.get(cv2.CAP_PROP_FPS)
        video_fps = video_fps if math.isfinite(video_fps) and video_fps>0 else 25
        preview = PreviewBuffer(preview_path(settings.CAMERA_RUNTIME_DIR,pk,lease),create=True)
        movement = PreviewBuffer(movement_path(settings.CAMERA_RUNTIME_DIR,pk,lease),create=True)
        movement_history = MovementHistory()
        if not file_source:
            live = LatestFrames(capture,preview,device.preview_fps)
        tracker = PersonTracker(max_missed=device.process_fps*3)
        counter = TripwireCounter(a=(device.line_ax,device.line_ay),b=(device.line_bx,device.line_by),entry_side=device.entry_side,tolerance=device.line_tolerance)
        anchor = SceneAnchor(counter.a,counter.b) if device.follow_camera and device.camera_mode==Device.Mode.TRIPWIRE else None
        last_capture = Detection.objects.filter(device=device,has_snapshot=True,is_manual_capture=False).order_by('-timestamp').values_list('timestamp',flat=True).first()
        start_time,last_check,last_inference = time.monotonic(),0,0
        track_generation=0
        presence = PresenceRecorder(device,lease,track_generation) if device.camera_mode==Device.Mode.SURVEILLANCE else None
        frame_times=deque(maxlen=12)
        frame_index,next_sample = 0,0
        while True:
            loop_time = time.monotonic()
            # Read the lease before every inference: stop/restart is immediately authoritative.
            close_old_connections()
            current = Device.objects.get(pk=pk)
            if current.worker_token != lease:
                outcome = 'stopped'
                break
            if loop_time-last_check>=1:
                Device.objects.filter(pk=pk,worker_token=lease).update(worker_last_seen=timezone.now())
                last_check = loop_time
            if current.config_version!=device.config_version:
                if presence:presence.finish()
                source_changed = (current.camera_source,current.source_type)!=(device.camera_source,device.source_type)
                if source_changed:
                    raise CameraError('Camera source changed. Restart the worker to open the new source.')
                if (current.detection_model,current.confidence_threshold)!=(device.detection_model,device.confidence_threshold):
                    detector.close()
                    detector = PersonDetector(settings.VISION_MODEL_DIR,current.detection_model,current.confidence_threshold)
                device = current
                if live:live.configure(device.preview_fps)
                track_generation += 1
                presence = PresenceRecorder(device,lease,track_generation) if device.camera_mode==Device.Mode.SURVEILLANCE else None
                tracker = PersonTracker(max_missed=device.process_fps*3)
                counter = TripwireCounter(a=(device.line_ax,device.line_ay),b=(device.line_bx,device.line_by),entry_side=device.entry_side,tolerance=device.line_tolerance)
                anchor = SceneAnchor(counter.a,counter.b) if device.follow_camera and device.camera_mode==Device.Mode.TRIPWIRE else None
            if file_source:
                ok,frame = capture.read()
                if not ok:
                    break
                timestamp = frame_index/video_fps
                frame_index += 1
                if timestamp+1e-6<next_sample:
                    continue
                next_sample += 1/device.process_fps
                # Playback is paced; video crossing order is not compressed into one DB instant.
                time.sleep(max(0,min(.25,start_time+timestamp-time.monotonic())))
            else:
                item = live.get()
                if item is None:
                    continue
                captured_at,frame = item
                timestamp = max(0,captured_at-start_time)
                if loop_time-last_inference<1/device.process_fps:
                    time.sleep(min(.05,1/device.process_fps))
                    continue
                last_inference = loop_time
            # Limit image cost while preserving aspect ratio and line coordinates.
            if frame.shape[1]>640 or frame.shape[0]>480:
                ratio=min(640/frame.shape[1],480/frame.shape[0])
                frame = cv2.resize(frame,(round(frame.shape[1]*ratio),round(frame.shape[0]*ratio)))
            inference_start = time.monotonic()
            boxes = detector.detect(frame,timestamp*1000)
            can_count = True
            if anchor:
                alignment = anchor.update(frame,boxes,timestamp)
                device.line_ax,device.line_ay = alignment.a
                device.line_bx,device.line_by = alignment.b
                device.line_tracking_status,device.line_tracking_error = alignment.status,alignment.error
                can_count = alignment.can_count
                if not can_count:
                    # Camera movement must not masquerade as a person crossing.
                    tracker = PersonTracker(max_missed=device.process_fps*3)
                    counter = TripwireCounter(a=alignment.a,b=alignment.b,entry_side=device.entry_side,tolerance=device.line_tolerance)
                    track_generation += 1
                else:
                    counter.a,counter.b = alignment.a,alignment.b
                    counter.dx,counter.dy = alignment.b[0]-alignment.a[0],alignment.b[1]-alignment.a[1]
                    counter.length = math.hypot(counter.dx,counter.dy)
            else:
                device.line_tracking_status,device.line_tracking_error = 'fixed',''
            if device.camera_mode == Device.Mode.TRIPWIRE and (settings.HARDWARE_ENABLED or current.line_tracking_status == 'reposition'):
                moving = False
                if settings.HARDWARE_ENABLED:
                    from portal.models import HardwareState
                    moving = HardwareState.load().moving
                if moving or current.line_tracking_status == 'reposition':
                    can_count = False
                    tracker = PersonTracker(max_missed=device.process_fps*3)
                    counter = TripwireCounter(a=counter.a,b=counter.b,entry_side=device.entry_side,tolerance=device.line_tolerance)
                    track_generation += 1
                    device.line_tracking_status = 'reposition' if current.line_tracking_status == 'reposition' else 'moving'
                    device.line_tracking_error = current.line_tracking_error if current.line_tracking_status == 'reposition' else 'Camera rotation in progress; counting is paused.'
            tracks = tracker.update(boxes,timestamp)
            if device.camera_mode == Device.Mode.TRIPWIRE:
                trails = movement_history.update(tracks if can_count else [], set(tracker.tracks), timestamp, track_generation)
                movement.publish(json.dumps({'camera_mode': device.camera_mode, 'config_version': device.config_version,
                    'generation': track_generation, 'published_at': time.monotonic(),
                    'width': frame.shape[1], 'height': frame.shape[0], 'trails': trails}, separators=(',', ':')).encode(), 0)
            if presence:
                try:presence.observe(tracks,set(tracker.tracks),timestamp)
                except EventConflict:
                    if not Device.objects.filter(pk=pk,worker_token=lease).exists():raise
            crossings = counter.update(tracks,timestamp,active_ids=set(tracker.tracks)) if device.camera_mode==Device.Mode.TRIPWIRE else []
            for crossing in crossings if can_count else []:
                event_id = uuid.uuid4()
                track_id = f'{lease}:{track_generation}:{crossing.track_id}'
                try:
                    record_event(device,event_id=event_id,direction=crossing.direction,track_id=track_id,lease=lease,config_version=device.config_version)
                except EventConflict:
                    if Device.objects.filter(pk=pk,worker_token=lease).exists():
                        continue  # Pausing/changing the line during inference invalidates this crossing.
                    raise
                if crossing.direction=='in':entries += 1
                else:exits += 1
            frame_times.append(time.monotonic())
            fps=(len(frame_times)-1)/max(.001,frame_times[-1]-frame_times[0]) if len(frame_times)>1 else min(device.process_fps,1/max(.001,time.monotonic()-inference_start))
            if live:live.set_overlay(tracks,device,entries,exits,fps)
            captured_now = timezone.now()
            needs_snapshot=device.camera_mode==Device.Mode.SURVEILLANCE and tracks and capture_due(device,last_capture,captured_now)
            ok,jpeg=False,None
            if file_source or needs_snapshot:
                annotated = annotate(frame,tracks,device,counter,entries,exits,fps)
                ok,jpeg = cv2.imencode('.jpg',annotated,[cv2.IMWRITE_JPEG_QUALITY,80])
            if ok:
                if file_source:preview.publish(jpeg.tobytes(),fps)
                if needs_snapshot:
                    try:
                        snapshot = save_snapshot(device,jpeg.tobytes(),len(tracks),lease,captured_now)
                        if snapshot:last_capture = snapshot.timestamp
                    except EventConflict:
                        if not Device.objects.filter(pk=pk,worker_token=lease).exists():raise
                    except OSError as exception:
                        raise CameraError('Unable to save surveillance photo. Check disk space and capture-folder permissions.') from exception
            Device.objects.filter(pk=pk,worker_token=lease).update(worker_last_seen=timezone.now(),last_seen=timezone.now(),
                worker_status='running',worker_fps=fps,visible_tracks=len(tracks),status='online')
            # A manual save wins over a frame that was already being processed.
            Device.objects.filter(pk=pk,worker_token=lease,config_version=device.config_version).update(
                line_ax=device.line_ax,line_ay=device.line_ay,line_bx=device.line_bx,line_by=device.line_by,
                line_tracking_status=device.line_tracking_status,line_tracking_error=device.line_tracking_error)
            processed += 1
            if max_frames and processed>=max_frames:
                break
    except KeyboardInterrupt:
        outcome = 'stopped'
    except EventConflict:
        outcome = 'stopped'
    except Exception as exception:
        outcome = 'error'
        # Backend exceptions can contain RTSP credentials; never persist them.
        error = str(exception) if isinstance(exception,CameraError) else 'Camera processing failed. Verify the model, source, and camera dependencies.'
    finally:
        if presence:presence.finish()
        if live:live.close()
        elif capture:capture.release()
        if preview and not live:preview.close()
        if movement:movement.close()
        if detector:detector.close()
        Device.objects.filter(pk=pk,worker_token=lease).update(worker_token=None,worker_pid=0,worker_status=outcome,
            worker_error=error,worker_fps=0,visible_tracks=0,status='offline')
    return {'frames':processed,'entries':entries,'exits':exits,'status':outcome,'error':error}
