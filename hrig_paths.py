"""Portable locations for this source-only development distribution."""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parent

def project_path(relative):
    """Resolve shipped code; HRIG_DATA_ROOT may provide optional private data."""
    path=ROOT/relative
    if path.exists():return str(path)
    data=os.environ.get('HRIG_DATA_ROOT')
    return str(Path(data).expanduser().resolve()/relative) if data else str(path)

def configure():
    paths=[ROOT,ROOT/'tools/blenderctl',ROOT/'work/hrig-p2-v045/scripts',
           ROOT/'work/hrig-p2-v065/product/control_core',
           ROOT/'work/hrig-p2-v028/addon']
    paths += sorted((ROOT/'work').glob('*/product'))
    for path in reversed(paths):
        if str(path) not in sys.path:sys.path.insert(0,str(path))
    return ROOT
