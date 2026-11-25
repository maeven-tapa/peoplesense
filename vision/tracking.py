"""Motion-predicted assignment. Counts use observed boxes, never predictions."""
from dataclasses import dataclass, field
import math
import numpy as np
from scipy.optimize import linear_sum_assignment

def iou(a, b):
    overlap = max(0, min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    union = (a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-overlap
    return overlap/union if union>0 else 0

def measurement(box):
    x1,y1,x2,y2 = box[:4]
    return np.array([(x1+x2)/2, (y1+y2)/2, x2-x1, y2-y1], dtype=float)

@dataclass
class Track:
    id: int
    box: tuple
    score: float
    hits: int = 1
    missed: int = 0
    state: object = field(init=False)
    covariance: object = field(init=False)
    def __post_init__(self):
        self.state = np.r_[measurement(self.box), np.zeros(4)]
        self.covariance = np.diag([.01]*4+[1.0]*4)
    @property
    def point(self):
        # Feet are steadier than the top edge of a changing person box.
        return ((self.box[0]+self.box[2])/2, self.box[3])
    @property
    def predicted_box(self):
        cx,cy,w,h = self.state[:4]
        w,h = max(.005,w),max(.005,h)
        return cx-w/2,cy-h/2,cx+w/2,cy+h/2
    def predict(self, dt):
        transition = np.eye(8)
        transition[:4,4:] = np.eye(4)*dt
        self.state = transition@self.state
        self.covariance = transition@self.covariance@transition.T+np.eye(8)*.0001
        self.missed += 1
    def correct(self, box):
        observation = np.zeros((4,8))
        observation[:4,:4] = np.eye(4)
        innovation = measurement(box)-observation@self.state
        noise = observation@self.covariance@observation.T+np.eye(4)*.0005
        gain = self.covariance@observation.T@np.linalg.inv(noise)
        self.state += gain@innovation
        self.covariance = (np.eye(8)-gain@observation)@self.covariance
        self.box = tuple(box[:4])
        self.score = float(box[4])
        self.missed = 0
        self.hits += 1

class PersonTracker:
    def __init__(self, max_missed=24, max_distance=.22, min_hits=3):
        self.tracks = {}
        self.next_id = 1
        self.max_missed = max_missed
        self.max_distance = max_distance
        self.min_hits = min_hits
        self.last_time = None
    def update(self, detections, timestamp):
        if self.last_time is not None and timestamp-self.last_time > 3:
            self.tracks.clear()  # Discontinuous footage must not manufacture crossings.
        dt = max(.01,min(1, timestamp-self.last_time)) if self.last_time is not None else .125
        self.last_time = timestamp
        # A duplicate detector box must not create a second track for one person.
        boxes = []
        for box in sorted(detections, key=lambda item:item[4], reverse=True):
            if not any(iou(box,other)>.75 for other in boxes):
                boxes.append(box)
        existing = list(self.tracks.values())
        for track in existing:
            track.predict(dt)
        unmatched_boxes = set(range(len(boxes)))
        if existing and boxes:
            cost = np.full((len(existing),len(boxes)),1e6)
            for row, track in enumerate(existing):
                predicted = track.predicted_box
                for col, box in enumerate(boxes):
                    center = measurement(box)[:2]
                    distance = float(np.linalg.norm(center-track.state[:2]))
                    overlap = iou(predicted,box)
                    ratio = max((box[2]-box[0])/(predicted[2]-predicted[0]),(predicted[2]-predicted[0])/(box[2]-box[0]),
                                (box[3]-box[1])/(predicted[3]-predicted[1]),(predicted[3]-predicted[1])/(box[3]-box[1]))
                    if distance<=self.max_distance and ratio<3 and (overlap>.01 or distance<.10):
                        cost[row,col] = .6*(1-overlap)+.4*distance/self.max_distance
            rows,cols = linear_sum_assignment(cost)
            for row,col in zip(rows,cols):
                if cost[row,col]<1e6:
                    existing[row].correct(boxes[col])
                    unmatched_boxes.discard(int(col))
        for col in sorted(unmatched_boxes):
            box = boxes[col]
            track = Track(self.next_id,tuple(box[:4]),float(box[4]))
            self.tracks[track.id] = track
            self.next_id += 1
        self.tracks = {key:track for key,track in self.tracks.items() if track.missed<=self.max_missed}
        return [track for track in self.tracks.values() if track.missed==0]

@dataclass
class Crossing:
    track_id: int
    direction: str

@dataclass
class LineState:
    side: int = 0
    anchor: tuple = None
    candidate: int = 0
    confirmations: int = 0
    last_count: float = -math.inf
    last_seen: float = 0

class TripwireCounter:
    def __init__(self, a=(.1,.5), b=(.9,.5), entry_side='positive', tolerance=.025, confirm_frames=2, min_hits=3, cooldown=.4):
        self.a,self.b = a,b
        self.dx,self.dy = b[0]-a[0],b[1]-a[1]
        self.length = math.hypot(self.dx,self.dy)
        if self.length<.05:
            raise ValueError('Counting line is too short.')
        self.entry_sign = 1 if entry_side=='positive' else -1
        self.tolerance = tolerance
        self.confirm_frames = confirm_frames
        self.min_hits = min_hits
        self.cooldown = cooldown
        self.states = {}
    def signed_distance(self, point):
        return (self.dx*(point[1]-self.a[1])-self.dy*(point[0]-self.a[0]))/self.length
    def crosses_segment(self, old, new):
        old_d,new_d = self.signed_distance(old),self.signed_distance(new)
        if old_d*new_d>=0:
            return False
        fraction = old_d/(old_d-new_d)
        px,py = old[0]+fraction*(new[0]-old[0]),old[1]+fraction*(new[1]-old[1])
        projection = ((px-self.a[0])*self.dx+(py-self.a[1])*self.dy)/(self.length**2)
        return 0<=projection<=1
    def update(self, tracks, timestamp, active_ids=None):
        if active_ids is not None:
            self.states = {key:value for key,value in self.states.items() if key in active_ids}
        results = []
        for track in tracks:
            state = self.states.setdefault(track.id,LineState())
            # Do not bridge a long absence with an assumed crossing.
            if state.last_seen and timestamp-state.last_seen>3:
                state = self.states[track.id] = LineState()
            state.last_seen = timestamp
            point = track.point
            distance = self.signed_distance(point)
            side = 1 if distance>self.tolerance else -1 if distance < -self.tolerance else 0
            if not side:
                state.candidate = state.confirmations = 0
                continue
            if state.side==0:
                state.side,state.anchor = side,point
                continue  # A newly visible person establishes a baseline; no count.
            if side==state.side:
                state.anchor = point
                state.candidate = state.confirmations = 0
                continue
            if state.candidate != side:
                state.candidate,state.confirmations = side,1
            else:
                state.confirmations += 1
            if state.confirmations<self.confirm_frames or track.hits<self.min_hits:
                continue
            crosses = self.crosses_segment(state.anchor,point)
            if crosses and timestamp-state.last_count<self.cooldown:
                continue  # Keep the old baseline until a sustained reverse crossing settles.
            if crosses:
                results.append(Crossing(track.id,'in' if side==self.entry_sign else 'out'))
                state.last_count = timestamp
            state.side,state.anchor = side,point
            state.candidate = state.confirmations = 0
        return results
