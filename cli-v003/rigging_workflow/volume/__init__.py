# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent CPU volume-partition diffusion. Returns arrays, never writes bpy."""
from .solver import solve, VolumeError

__all__ = ['solve', 'VolumeError']
