"""Bounded latest-JPEG IPC. Readers never queue old frames or import the AI runtime."""
import mmap
import struct
import time
from pathlib import Path

CAPACITY = 256 * 1024
HEADER = struct.Struct('<QQd')  # sequence, JPEG length, measured FPS
SIZE = HEADER.size + CAPACITY


def preview_path(directory, pk, lease):
    return Path(directory) / f'camera-{int(pk)}-{lease}.preview'


class PreviewBuffer:
    def __init__(self, path, *, create=False):
        self.path = Path(path)
        self.writer = create
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('w+b' if create else 'rb')
        try:
            if create:
                self.file.truncate(SIZE)
            elif self.path.stat().st_size != SIZE:
                raise ValueError('Invalid preview buffer size.')
            self.mapping = mmap.mmap(self.file.fileno(), SIZE, access=mmap.ACCESS_WRITE if create else mmap.ACCESS_READ)
        except Exception:
            self.file.close()
            raise
        self.sequence = 0

    def publish(self, jpeg, fps):
        if not self.writer:
            raise ValueError('Read-only preview buffer.')
        if not 0 < len(jpeg) <= CAPACITY:
            return False
        self.sequence += 2
        # Odd sequence marks a write in progress; readers retry a torn copy.
        struct.pack_into('<Q', self.mapping, 0, self.sequence - 1)
        self.mapping[HEADER.size:HEADER.size + len(jpeg)] = jpeg
        struct.pack_into('<Qd', self.mapping, 8, len(jpeg), fps)
        struct.pack_into('<Q', self.mapping, 0, self.sequence)
        return True

    def read(self):
        for _ in range(3):
            sequence, length, fps = HEADER.unpack_from(self.mapping)
            if not sequence or sequence % 2 or not 0 < length <= CAPACITY:
                continue
            jpeg = self.mapping[HEADER.size:HEADER.size + length]
            if sequence == struct.unpack_from('<Q', self.mapping)[0]:
                return sequence, jpeg, fps
        return None

    def close(self):
        self.mapping.close()
        self.file.close()
        if self.writer:
            try:
                self.path.unlink(missing_ok=True)
            except PermissionError:
                pass  # Windows readers close on lease revocation; next claim cleans up.


def stream_frames(pk, lease, directory, target_fps):
    from django.db import close_old_connections
    from portal.models import Device
    reader = None
    last_sequence = 0
    next_check = 0
    began = time.monotonic()
    next_frame = began
    try:
        while True:
            now = time.monotonic()
            if now >= next_check:
                close_old_connections()
                device = Device.objects.filter(pk=pk, worker_token=lease).first()
                if not device or not device.worker_alive:
                    return
                target_fps = device.preview_fps
                next_check = now + 1
            if reader is None:
                try:
                    reader = PreviewBuffer(preview_path(directory, pk, lease))
                except (FileNotFoundError, ValueError):
                    if now - began > 15:
                        return
            frame = reader.read() if reader else None
            if frame and frame[0] != last_sequence:
                last_sequence, jpeg, _ = frame
                yield b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' + str(len(jpeg)).encode() + b'\r\n\r\n' + jpeg + b'\r\n'
            # Absolute pacing avoids adding socket/JPEG-copy time to every
            # frame interval; slow clients still receive only the latest frame.
            period = 1 / target_fps
            next_frame = max(next_frame + period, time.monotonic())
            time.sleep(max(0, next_frame - time.monotonic()))
    finally:
        if reader:
            reader.close()
        close_old_connections()
