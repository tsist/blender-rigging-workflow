"""Explicit shared-session coordination for D development workspaces."""
from .coordinator import Workspace, WorkspaceError

__all__ = ['Workspace', 'WorkspaceError']
