"""Keep a saved doorway line aligned with small changes in the camera view.

The reference stays fixed to avoid accumulating frame-to-frame drift. Person
regions are excluded. Unreliable alignment latches until the operator saves a
new line; it must never silently choose a different doorway as its reference.
"""
from dataclasses import dataclass
import math
import cv2
import numpy as np


@dataclass
class Alignment:
    a: tuple
    b: tuple
    status: str
    can_count: bool
    error: str = ''


class SceneAnchor:
    def __init__(self, a, b, settle_seconds=.75):
        self.a, self.b = tuple(a), tuple(b)
        self.reference = self.points = None
        self.last_line = np.array([a,b], dtype=float)
        self.last_motion = None
        self.settle_seconds = settle_seconds
        self.lost = ''

    @staticmethod
    def mask(shape, boxes):
        h,w = shape
        mask = np.full(shape,255,np.uint8)
        # Avoid frame edges, which can enter/leave the view when the camera pans.
        mask[:8,:]=mask[-8:,:]=0
        mask[:,:8]=mask[:,-8:]=0
        for box in boxes:
            x1,y1,x2,y2 = box[:4]
            cv2.rectangle(mask,(max(0,int(x1*w)-12),max(0,int(y1*h)-12)),
                          (min(w-1,int(x2*w)+12),min(h-1,int(y2*h)+12)),0,-1)
        return mask

    @staticmethod
    def spread(points, shape):
        h,w = shape
        if len(points)<20:
            return False
        return np.ptp(points[:,0])>w*.35 and np.ptp(points[:,1])>h*.30

    def failure(self, message):
        self.lost = message
        return Alignment(tuple(self.last_line[0]),tuple(self.last_line[1]),'lost',False,message)

    def update(self, frame, boxes, timestamp):
        if self.lost:
            return self.failure(self.lost)
        gray = cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        height = max(1,round(gray.shape[0]*480/gray.shape[1]))
        gray = cv2.resize(gray,(480,height))
        mask = self.mask(gray.shape,boxes)
        if self.reference is None:
            points = cv2.goodFeaturesToTrack(gray,240,.015,9,mask=mask,blockSize=7)
            if points is None or not self.spread(points.reshape(-1,2),gray.shape):
                return self.failure('Not enough doorway detail. Reposition the camera and save the line, or turn off Follow camera movement.')
            self.reference, self.points = gray,points
            self.last_motion = timestamp
            return Alignment(self.a,self.b,'moving',False)
        if gray.shape != self.reference.shape:
            return self.failure('The camera aspect ratio changed. Reposition and save the counting line.')
        current, valid, error = cv2.calcOpticalFlowPyrLK(self.reference,gray,self.points,None,winSize=(31,31),maxLevel=3)
        if current is None:
            return self.failure('Doorway alignment was lost. Reposition and save the counting line.')
        back, back_valid, _ = cv2.calcOpticalFlowPyrLK(gray,self.reference,current,None,winSize=(31,31),maxLevel=3)
        if back is None:
            return self.failure('Doorway alignment was lost. Reposition and save the counting line.')
        source, target = self.points.reshape(-1,2),current.reshape(-1,2)
        h,w = gray.shape
        finite = np.isfinite(target).all(axis=1)
        inside = finite & (target[:,0]>=0)&(target[:,0]<w)&(target[:,1]>=0)&(target[:,1]<h)
        indices = np.flatnonzero(inside)
        allowed = np.zeros(len(target),bool)
        allowed[indices] = mask[target[indices,1].astype(int),target[indices,0].astype(int)]>0
        good = valid.ravel().astype(bool)&back_valid.ravel().astype(bool)&allowed
        good &= np.linalg.norm(source-back.reshape(-1,2),axis=1)<1.5
        good &= error.ravel()<25
        source,target = source[good],target[good]
        if not self.spread(source,gray.shape):
            return self.failure('Doorway detail is obscured or the camera moved too far. Reposition and save the line.')
        matrix,inliers = cv2.estimateAffinePartial2D(source,target,method=cv2.RANSAC,ransacReprojThreshold=2,maxIters=1500,confidence=.99)
        if matrix is None or not np.isfinite(matrix).all():
            return self.failure('Doorway alignment was lost. Reposition and save the counting line.')
        selected = inliers.ravel().astype(bool)
        if selected.sum()<20 or selected.mean()<.65 or not self.spread(source[selected],gray.shape):
            return self.failure('Doorway alignment is uncertain. Reposition and save the counting line.')
        scale = math.hypot(matrix[0,0],matrix[1,0])
        angle = abs(math.atan2(matrix[1,0],matrix[0,0]))
        if not .8<=scale<=1.25 or angle>math.radians(15):
            return self.failure('The camera moved too far. Reposition and save the counting line.')
        reference_line = np.array([self.a,self.b])*[w,h]
        line = (reference_line@matrix[:,:2].T+matrix[:,2])/[w,h]
        if np.any(line<0) or np.any(line>1) or np.linalg.norm(line[1]-line[0])<.05:
            return self.failure('The counting line left the camera view. Reposition and save it.')
        if np.max(np.linalg.norm(line-self.last_line,axis=1))>.002:
            self.last_motion = timestamp
        self.last_line = line
        settled = timestamp-self.last_motion>=self.settle_seconds
        return Alignment(tuple(line[0]),tuple(line[1]),'locked' if settled else 'moving',settled)
