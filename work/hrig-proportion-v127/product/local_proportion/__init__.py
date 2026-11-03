"""Finite local proportion UI; loading this package never registers or edits."""
def initialize(*args, **kwargs):
    from .session import initialize as start
    return start(*args, **kwargs)
