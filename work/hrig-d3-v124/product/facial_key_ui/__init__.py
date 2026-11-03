"""Thin UI/session adapter; imports do not register classes or modify Blender."""

def initialize(*args, **kwargs):
    from .session import initialize as start
    return start(*args, **kwargs)
