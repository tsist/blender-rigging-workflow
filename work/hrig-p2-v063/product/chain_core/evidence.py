"""Typed finite array evidence shared by bounded native chain studies."""
import base64,hashlib,io,json
from pathlib import Path
import numpy as np

def checked(descriptor):
    path=Path(descriptor['file'])
    if hashlib.sha256(path.read_bytes()).hexdigest()!=descriptor['expected_sha256']:
        raise ValueError('resource_identity_changed:'+str(path))
    return path

class TypedWriter:
    def __init__(self):self.stream=io.BytesIO()
    def pack(self,value):
        if isinstance(value,np.ndarray):
            a=np.asarray(value,dtype='<f4')
            if not np.isfinite(a).all():raise ValueError('nonfinite_evidence')
            raw=a.tobytes();offset=self.stream.tell();self.stream.write(raw)
            return {'offset':offset,'bytes':len(raw),'type':'f','shape':list(a.shape),'sha256':hashlib.sha256(raw).hexdigest()}
        if isinstance(value,dict):return {k:self.pack(v) for k,v in value.items()}
        if isinstance(value,list):return [self.pack(v) for v in value]
        return value
    def write(self,path):
        raw=self.stream.getvalue()
        Path(path).write_text(json.dumps({'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'data':base64.b64encode(raw).decode()}),encoding='utf8')

def dump(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
