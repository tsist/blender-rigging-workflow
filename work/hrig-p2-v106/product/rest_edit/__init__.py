"""Finite owned native rest-roll work unit; importing the contract needs no bpy."""

def __getattr__(name):
    if name == 'RestEditTransaction':
        from .native import RestEditTransaction
        return RestEditTransaction
    raise AttributeError(name)

__all__ = ['RestEditTransaction']
