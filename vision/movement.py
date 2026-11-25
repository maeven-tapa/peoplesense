"""Recent observed foot-point paths; no predicted positions or permanent storage."""
import json
import math
import time
from collections import deque
from pathlib import Path

from .preview import PreviewBuffer

MAX_TRACKS = 50
MAX_POINTS = 64
MAX_AGE = 8
MAX_GAP = 1


def movement_path(directory, pk, lease):
    return Path(directory) / f'camera-{int(pk)}-{lease}.movement'


class MovementHistory:
    def __init__(self):
        self.paths = {}
        self.generation = None

    def update(self, tracks, active_ids, timestamp, generation):
        if generation != self.generation:
            self.paths.clear()
            self.generation = generation
        for identity in list(self.paths):
            path = self.paths[identity]
            while path and timestamp - path[0][0] > MAX_AGE:
                path.popleft()
            if identity not in active_ids or not path or timestamp - path[-1][0] > MAX_GAP:
                del self.paths[identity]
        visible = []
        for track in tracks[:MAX_TRACKS]:
            # Ignore provisional tracks and tracks that only have predictions.
            if track.hits < 3 or track.missed:
                continue
            x, y = track.point
            if not all(math.isfinite(value) and 0 <= value <= 1 for value in (x, y)):
                continue
            if track.id not in self.paths and len(self.paths) >= MAX_TRACKS:
                continue
            path = self.paths.setdefault(track.id, deque(maxlen=MAX_POINTS))
            path.append((timestamp, round(x, 5), round(y, 5)))
            if len(path) > 1:
                visible.append({'id': track.id, 'points': [[x, y] for _, x, y in path]})
        return visible


def read_movement(directory, device):
    """Read only the current lease and configuration, dropping stalled overlays."""
    empty = {'alive': device.worker_alive, 'camera_mode': device.camera_mode, 'trails': []}
    if not device.worker_alive or device.camera_mode != 'tripwire':
        return empty
    try:
        reader = PreviewBuffer(movement_path(directory, device.pk, device.worker_token))
        try:
            item = reader.read()
        finally:
            reader.close()
        if item:
            payload = json.loads(item[1])
            age = time.monotonic() - payload['published_at']
            if (0 <= age < 2 and payload['config_version'] == device.config_version
                    and payload['camera_mode'] == 'tripwire'):
                return {**empty, 'generation': payload['generation'], 'width': payload['width'],
                        'height': payload['height'], 'trails': payload['trails']}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return empty
