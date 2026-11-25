"""ARM64-compatible LiteRT inference for the pinned EfficientDet person model.

Anchor/decode fields come from the models' embedded DETECTOR_METADATA,
following Google's object_detector_metadata_schema.fbs (V001). No MediaPipe
or TensorFlow import is needed on the Pi.
"""
import struct
import zipfile
import cv2
import numpy as np
from flatbuffers import table, number_types as nt
from .model_store import verify_model


def child(node, field):
    offset = node.Offset(4+2*field)
    if not offset:raise ValueError('Required detector metadata is missing.')
    return table.Table(node.Bytes,node.Indirect(node.Pos+offset))


def vector(node, field):
    offset=node.Offset(4+2*field)
    if not offset:raise ValueError('Required detector vector is missing.')
    start=node.Vector(offset)
    return [table.Table(node.Bytes,node.Indirect(start+i*4)) for i in range(node.VectorLen(offset))]


def scalar(node,field,kind,default=0):
    offset=node.Offset(4+2*field)
    return node.Get(kind,node.Pos+offset) if offset else default


def metadata(path):
    from ai_edge_litert import schema_py_generated as schema
    model=schema.Model.GetRootAsModel(path.read_bytes(),0)
    entry=next(model.Metadata(i) for i in range(model.MetadataLength()) if model.Metadata(i).Name()==b'TFLITE_METADATA')
    data=model.Buffers(entry.Buffer()).DataAsNumpy().tobytes()
    root=table.Table(data,struct.unpack_from('<I',data)[0])
    subgraph=vector(root,3)[0]
    custom=next(node for node in vector(subgraph,9) if node.String(node.Pos+node.Offset(4))==b'DETECTOR_METADATA')
    offset=custom.Offset(6)
    data=custom.GetVectorAsNumpy(nt.Uint8Flags,offset).tobytes()
    if data[4:8]!=b'V001':raise ValueError('Unsupported detector metadata version.')
    root=table.Table(data,struct.unpack_from('<I',data)[0])
    anchors=np.array([[scalar(a,i,nt.Float32Flags) for i in range(4)]
                     for a in vector(child(child(root,1),0),0)],dtype=np.float32)
    decode=child(root,2)
    options={'classes':scalar(decode,0,nt.Int32Flags),'boxes':scalar(decode,1,nt.Int32Flags),
        'scales':[scalar(decode,i,nt.Float32Flags) for i in range(6,10)],
        'exponential':scalar(decode,10,nt.BoolFlags),'sigmoid':scalar(decode,11,nt.BoolFlags)}
    # The checksum-pinned official model uses this normalization. Refuse a
    # future unpinned model rather than guessing output order or person label.
    with zipfile.ZipFile(path) as archive:
        labels=archive.read('labels.txt').decode().splitlines()
    return anchors,options,labels.index('person')


class LitePersonDetector:
    def __init__(self,model_dir,model_name='efficientdet_lite0',confidence=.45):
        from ai_edge_litert.interpreter import Interpreter
        path=verify_model(model_dir,model_name)
        self.interpreter=Interpreter(model_path=str(path),num_threads=1)
        self.interpreter.allocate_tensors()
        self.input=self.interpreter.get_input_details()[0]
        outputs=self.interpreter.get_output_details()
        self.boxes=next(o for o in outputs if o['shape'][-1]==4)
        self.scores=next(o for o in outputs if o['shape'][-1]!=4)
        self.anchors,self.options,self.person_index=metadata(path)
        if len(self.anchors)!=self.options['boxes'] or self.scores['shape'][-1]!=self.options['classes']:
            raise ValueError('Detector outputs do not match embedded anchors.')
        self.confidence=confidence

    def detect(self,frame,timestamp_ms):
        height,width=self.input['shape'][1:3]
        pixels=cv2.resize(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB),(int(width),int(height)))
        normalized=(pixels.astype(np.float32)-127.5)/127.5
        if np.issubdtype(self.input['dtype'],np.integer):
            scale,zero=self.input['quantization']
            if scale<=0:raise ValueError('Invalid input quantization.')
            bounds=np.iinfo(self.input['dtype'])
            tensor=np.clip(np.rint(normalized/scale+zero),bounds.min,bounds.max).astype(self.input['dtype'])
        else:tensor=normalized.astype(self.input['dtype'])
        self.interpreter.set_tensor(self.input['index'],tensor[None,...])
        self.interpreter.invoke()
        scores=self.interpreter.get_tensor(self.scores['index'])[0,:,self.person_index]
        if self.options['sigmoid']:scores=1/(1+np.exp(-np.clip(scores,-80,80)))
        indices=np.flatnonzero(scores>=self.confidence)
        # Bound CPU spent in postprocessing for a crowded/high-noise scene.
        indices=indices[np.argsort(scores[indices])[::-1][:300]]
        if not len(indices):return []
        raw=self.interpreter.get_tensor(self.boxes['index'])[0,indices]
        anchors=self.anchors[indices]
        xs,ys,ws,hs=self.options['scales']
        cy=raw[:,0]/ys*anchors[:,3]+anchors[:,1]
        cx=raw[:,1]/xs*anchors[:,2]+anchors[:,0]
        if self.options['exponential']:
            h=np.exp(np.clip(raw[:,2]/hs,-20,20))*anchors[:,3]
            w=np.exp(np.clip(raw[:,3]/ws,-20,20))*anchors[:,2]
        else:h,w=raw[:,2]/hs*anchors[:,3],raw[:,3]/ws*anchors[:,2]
        boxes=np.clip(np.stack([cx-w/2,cy-h/2,cx+w/2,cy+h/2],axis=1),0,1)
        valid=np.isfinite(boxes).all(axis=1)&(boxes[:,2]>boxes[:,0])&(boxes[:,3]>boxes[:,1])
        boxes=boxes[valid];values=scores[indices][valid]
        xywh=np.column_stack([boxes[:,:2],boxes[:,2:]-boxes[:,:2]])
        keep=cv2.dnn.NMSBoxes(xywh.tolist(),values.tolist(),self.confidence,.5,top_k=50)
        return [(*map(float,boxes[int(i)]),float(values[int(i)])) for i in np.asarray(keep).reshape(-1)]

    def close(self):
        self.interpreter=None
