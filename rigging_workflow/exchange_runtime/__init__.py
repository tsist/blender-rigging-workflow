# SPDX-License-Identifier: GPL-3.0-or-later
"""Private exchange domain; never registers UI or replaces foreign modules."""
import sys
from pathlib import Path
_root=Path(__file__).resolve().parent
for _name,_module in tuple(sys.modules.items()):
    if _name.startswith(__name__+'.'):
        _file=getattr(_module,'__file__',None)
        if _file is None or not Path(_file).resolve().is_relative_to(_root):
            raise RuntimeError('Foreign qualified exchange namespace occupied: '+_name)
