"""Offline, auditable Snow weight-template adapter.

The package is a task-owned development module. It does not modify the public
blenderctl adapters or register a new public CLI command.
"""

from .contract import SCHEMA, TemplateError, canonical_bytes, seal_template, validate_template

__all__ = ["SCHEMA", "TemplateError", "canonical_bytes", "seal_template", "validate_template"]
