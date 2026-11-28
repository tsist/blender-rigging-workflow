"""Resolve the controls and deform targets used by secondary dynamics."""

import json

from . import rig_identity


def control(rig, name=None, *, change_bones=False):
    record = rig_identity.read(rig)
    if rig.override_library or rig.data.override_library or (change_bones and rig.data.users != 1):
        raise ValueError("Secondary dynamics requires a local, independently editable rig")
    default = "骨架设置" if record["schema"] == "c94.generated.v1" else "root"
    settings = rig.pose.bones.get(name or default)
    if settings is None or settings.bone.use_deform:
        raise ValueError("The rig needs a valid non-deforming dynamics settings control")
    return settings


def panel_controls(rig, state_key, group=None):
    """Read installed controls for drawing without opening a solver session."""
    if rig is None or state_key not in rig:
        return None, {}
    try:
        state = json.loads(rig[state_key])
        groups = state["groups"]
        if not isinstance(groups, dict) or any(not isinstance(row, dict) for row in groups.values()):
            return None, {}
        row = state if group is None else groups[group]
        resolved = control(rig, row.get("settings_control"))
        return resolved, groups
    except (ValueError, TypeError, KeyError, AttributeError):
        return None, {}


def targets(rig, groups):
    record = rig_identity.read(rig)
    if not isinstance(groups, dict) or not groups:
        raise ValueError("Explicit secondary dynamics target groups are required")
    known = set(record["deforms"])
    result = {}
    used = set()
    for group, names in groups.items():
        if (not isinstance(group, str) or not group.strip() or len(group) > 32
                or not isinstance(names, (list, tuple)) or not names
                or any(not isinstance(name, str) or name not in known for name in names)
                or len(set(names)) != len(names) or used.intersection(names)):
            raise ValueError("Groups must name distinct deform bones from the generated rig metadata")
        result[group] = list(names)
        used.update(names)
    return result
