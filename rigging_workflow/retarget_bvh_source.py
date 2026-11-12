"""Self-contained BVH source parser and Blender 5.2.1 importer.

The module has two deliberately separate layers:

* :func:`load` is a pure-Python, read-only parser for the BVH text format
  (HIERARCHY / ROOT / JOINT / OFFSET / CHANNELS / End Site / MOTION /
  Frames / Frame Time).  It never imports ``bpy``.
* :func:`import_bvh` creates native Blender data (armature object/data, pose
  bones, a layered Action with a native Action Slot, and f-curves).  It only
  imports ``bpy``/``mathutils`` when called.

Units are explicit: ``scale`` is Blender units per source BVH unit.  No CMU,
ASF or other source-specific unit is assumed.  BVH rotation channels are
composed in exactly the order written under each joint.  When the order is a
three-axis permutation, the corresponding Blender Euler mode is the reverse
of that order (the same convention used by Blender's native Euler matrices:
channels ``Zrotation Yrotation Xrotation`` -> mode ``XYZ``).

The armature object orientation is computed with Blender's own
``bpy_extras.io_utils.axis_conversion`` from the declared source
``forward``/``up`` axes to the C94 production world axes: ``-Y`` forward and
``Z`` up.  The source axis convention is never used as the Blender world
convention.

Root position channels follow the archived Blender add-ons reference
(``io_anim_bvh/import_bvh.py``, blob
``5d1b5a1f67da85d455e8614907f45cd140a070e4``, lines 569-575): use
``bvh_loc - root OFFSET`` and then the rest inverse.  That is an old official
implementation reference, not a local proof for nonzero root OFFSET.  Non-root
position channels have different, unverified semantics and are explicitly
rejected rather than treated as local translation increments.

The importer intentionally does not use an older retarget/export pipeline.
It writes only the object/data it creates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, NamedTuple, Optional, Sequence, Tuple


class BvhError(ValueError):
    """Base class for BVH source errors."""


class BvhParseError(BvhError):
    """Raised when the BVH text is malformed or incomplete."""


POSITION_CHANNELS = ("Xposition", "Yposition", "Zposition")
ROTATION_CHANNELS = ("Xrotation", "Yrotation", "Zrotation")
_POSITION_LOOKUP = {name.lower(): name for name in POSITION_CHANNELS}
_ROTATION_LOOKUP = {name.lower(): name for name in ROTATION_CHANNELS}
_ALL_CHANNEL_LOOKUP = {**_POSITION_LOOKUP, **_ROTATION_LOOKUP}
_AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}
_MAX_HIERARCHY_DEPTH = 256
BLENDER_TARGET_FORWARD = "-Y"
BLENDER_TARGET_UP = "Z"


@dataclass
class BvhJoint:
    """One ROOT or JOINT entry from the hierarchy."""

    name: str
    index: int
    parent: Optional[str]
    offset: Tuple[float, float, float]
    channels: Tuple[str, ...]
    channel_start: int
    children: List[str] = field(default_factory=list)
    end_site_offset: Optional[Tuple[float, float, float]] = None
    depth: int = 0

    @property
    def rotation_channels(self) -> Tuple[str, ...]:
        return tuple(name for name in self.channels if name in ROTATION_CHANNELS)

    @property
    def position_channels(self) -> Tuple[str, ...]:
        return tuple(name for name in self.channels if name in POSITION_CHANNELS)

    @property
    def has_rotation(self) -> bool:
        return bool(self.rotation_channels)

    @property
    def has_position(self) -> bool:
        return bool(self.position_channels)

    @property
    def rotation_axes(self) -> Tuple[str, ...]:
        return tuple(name[0].upper() for name in self.rotation_channels)

    @property
    def euler_mode(self) -> Optional[str]:
        """Blender Euler mode, or ``None`` when a quaternion is required.

        Blender's ``Euler((x, y, z), "XYZ").to_matrix()`` equals
        ``Rz @ Ry @ Rx``.  Therefore a BVH channel list applied as
        ``Rc1 @ Rc2 @ Rc3`` maps to the reversed three-axis string.
        """

        axes = self.rotation_axes
        if len(axes) != 3 or len(set(axes)) != 3:
            return None
        if set(axes) != {"X", "Y", "Z"}:
            return None
        return "".join(reversed(axes))

    def channel_index(self, channel_name: str) -> Optional[int]:
        for index, name in enumerate(self.channels):
            if name == channel_name:
                return self.channel_start + index
        return None


@dataclass
class ParsedBvh:
    """Immutable-ish result of :func:`load`; motion rows are tuples."""

    filepath: str
    sha256: str
    byte_size: int
    root_name: str
    joints: Tuple[BvhJoint, ...]
    frame_time: float
    frame_count: int
    channel_names: Tuple[str, ...]
    frames: Tuple[Tuple[float, ...], ...]
    motion_line: int = 0

    @property
    def total_channels(self) -> int:
        return len(self.channel_names)

    @property
    def source_fps(self) -> float:
        return 1.0 / self.frame_time

    @property
    def fps(self) -> float:
        return self.source_fps

    @property
    def duration_seconds(self) -> float:
        return self.frame_count * self.frame_time

    @property
    def duration_span_seconds(self) -> float:
        return max(0, self.frame_count - 1) * self.frame_time

    @property
    def root(self) -> BvhJoint:
        for joint in self.joints:
            if joint.parent is None:
                return joint
        raise BvhError("Parsed BVH has no root")

    @property
    def joint_map(self) -> Dict[str, BvhJoint]:
        return {joint.name: joint for joint in self.joints}

    @property
    def joint_names(self) -> Tuple[str, ...]:
        return tuple(joint.name for joint in self.joints)

    @property
    def end_site_count(self) -> int:
        return sum(1 for joint in self.joints if joint.end_site_offset is not None)

    @property
    def has_position_channels(self) -> bool:
        return any(joint.has_position for joint in self.joints)

    def frame(self, index: int) -> Tuple[float, ...]:
        if index < 0:
            index += self.frame_count
        if not 0 <= index < self.frame_count:
            raise IndexError("frame index out of range: %r" % (index,))
        return self.frames[index]

    def joint_row(self, joint: BvhJoint, frame_index: int) -> Dict[str, float]:
        row = self.frame(frame_index)
        return {
            name: row[joint.channel_start + index]
            for index, name in enumerate(joint.channels)
        }

    def hierarchy(self) -> Dict[str, object]:
        """JSON-safe nested hierarchy, useful for reports."""

        by_name = self.joint_map

        def node(joint: BvhJoint) -> Dict[str, object]:
            result: Dict[str, object] = {
                "name": joint.name,
                "type": "ROOT" if joint.parent is None else "JOINT",
                "offset": list(joint.offset),
                "channels": list(joint.channels),
                "channel_start": joint.channel_start,
                "rotation_order": list(joint.rotation_channels),
                "euler_mode": joint.euler_mode,
                "children": [node(by_name[name]) for name in joint.children],
            }
            if joint.end_site_offset is not None:
                result["end_site_offset"] = list(joint.end_site_offset)
            return result

        return node(self.root)

    def to_metadata(self, include_hierarchy: bool = True) -> Dict[str, object]:
        """Return a compact, JSON-safe description without motion rows."""

        metadata: Dict[str, object] = {
            "format": "BVH_SOURCE_V1",
            "file": self.filepath,
            "sha256": self.sha256,
            "bytes": self.byte_size,
            "motion_line": self.motion_line,
            "root": self.root_name,
            "joint_count": len(self.joints),
            "end_site_count": self.end_site_count,
            "frame_count": self.frame_count,
            "frame_time": self.frame_time,
            "fps": self.source_fps,
            "duration_seconds": self.duration_seconds,
            "duration_span_seconds": self.duration_span_seconds,
            "total_channels": self.total_channels,
            "channel_names": list(self.channel_names),
            "has_position_channels": self.has_position_channels,
            "units": "source units; apply the explicit scale value to obtain Blender units",
            "joints": [
                {
                    "name": joint.name,
                    "parent": joint.parent,
                    "offset": list(joint.offset),
                    "channels": list(joint.channels),
                    "channel_start": joint.channel_start,
                    "children": list(joint.children),
                    "end_site_offset": (
                        list(joint.end_site_offset)
                        if joint.end_site_offset is not None
                        else None
                    ),
                    "rotation_order": list(joint.rotation_channels),
                    "euler_mode": joint.euler_mode,
                }
                for joint in self.joints
            ],
        }
        if include_hierarchy:
            metadata["hierarchy"] = self.hierarchy()
        return metadata


class _TokenStream:
    """Small whitespace/brace tokenizer that tracks source line numbers."""

    def __init__(self, text: str, line: int = 1):
        self.text = text
        self.pos = 0
        self.line = line
        self._peeked: Optional[Tuple[Optional[str], int]] = None

    def _scan(self) -> Tuple[Optional[str], int]:
        text = self.text
        length = len(text)
        pos = self.pos
        line = self.line
        while pos < length and text[pos].isspace():
            if text[pos] == "\n":
                line += 1
            pos += 1
        if pos >= length:
            self.pos = pos
            self.line = line
            return None, line
        char = text[pos]
        if char in "{}":
            self.pos = pos + 1
            self.line = line
            return char, line
        start = pos
        while pos < length and not text[pos].isspace() and text[pos] not in "{}":
            pos += 1
        self.pos = pos
        self.line = line
        return text[start:pos], line

    def peek(self) -> Tuple[Optional[str], int]:
        if self._peeked is None:
            saved_pos, saved_line = self.pos, self.line
            token = self._scan()
            self.pos, self.line = saved_pos, saved_line
            self._peeked = token
        return self._peeked

    def pop(self) -> Tuple[Optional[str], int]:
        token = self.peek()
        self._peeked = None
        if token[0] is not None:
            # Advance past the token that was peeked.
            self._scan()
        return token

    def expect(self, expected: str, context: str = "") -> str:
        token, line = self.pop()
        if token != expected:
            suffix = (" while " + context) if context else ""
            raise BvhParseError(
                "Expected %r at line %d, found %r%s"
                % (expected, line, token, suffix)
            )
        return token  # type: ignore[return-value]


def _parse_float(token: Optional[str], line: int, context: str) -> float:
    if token is None:
        raise BvhParseError("Unexpected end of file while reading %s at line %d" % (context, line))
    try:
        value = float(token)
    except (TypeError, ValueError):
        raise BvhParseError(
            "Invalid number %r for %s at line %d" % (token, context, line)
        ) from None
    if not math.isfinite(value):
        raise BvhParseError(
            "Non-finite number %r for %s at line %d" % (token, context, line)
        )
    return value


def _parse_three_floats(stream: _TokenStream, context: str) -> Tuple[float, float, float]:
    values = []
    for _ in range(3):
        token, line = stream.pop()
        values.append(_parse_float(token, line, context))
    return (values[0], values[1], values[2])


def _canonical_channel(token: str, line: int) -> str:
    canonical = _ALL_CHANNEL_LOOKUP.get(token.lower())
    if canonical is None:
        raise BvhParseError(
            "Unsupported BVH channel %r at line %d (expected X/Y/Zposition or X/Y/Zrotation)"
            % (token, line)
        )
    return canonical


def _split_motion(text: str) -> Tuple[str, str, int]:
    """Split hierarchy text from motion text at a standalone MOTION line."""

    lines = text.splitlines(keepends=True)
    offset = 0
    for index, line in enumerate(lines):
        if line.strip().upper() == "MOTION":
            return text[:offset], text[offset + len(line) :], index + 1
        offset += len(line)
    raise BvhParseError("Missing MOTION section")


def _parse_hierarchy(text: str) -> Tuple[str, Tuple[BvhJoint, ...]]:
    stream = _TokenStream(text)
    token, line = stream.pop()
    if token is None or token.upper() != "HIERARCHY":
        raise BvhParseError("Expected HIERARCHY at line %d" % (line,))

    joints: List[BvhJoint] = []
    by_name: Dict[str, BvhJoint] = {}

    def read_name() -> Tuple[str, int]:
        name_parts: List[str] = []
        name_line = line
        while True:
            token, token_line = stream.peek()
            if token is None:
                raise BvhParseError("Unexpected end of file while reading a joint name")
            if token == "{":
                break
            if token in ("}",):
                raise BvhParseError("Unexpected '}' while reading a joint name at line %d" % token_line)
            token, token_line = stream.pop()
            if not name_parts:
                name_line = token_line
            name_parts.append(token)
        if not name_parts:
            raise BvhParseError("Empty joint name at line %d" % (name_line,))
        return "_".join(name_parts), name_line

    def add_joint(
        node_type: str,
        parent: Optional[str],
        depth: int,
    ) -> BvhJoint:
        nonlocal line
        if depth > _MAX_HIERARCHY_DEPTH:
            raise BvhParseError("BVH hierarchy exceeds %d levels" % (_MAX_HIERARCHY_DEPTH,))
        name, name_line = read_name()
        if name in by_name:
            raise BvhParseError(
                "Duplicate joint name %r at line %d; bone names must be unambiguous"
                % (name, name_line)
            )
        stream.expect("{", "opening %s %s" % (node_type, name))
        token, offset_line = stream.pop()
        if token is None or token.upper() != "OFFSET":
            raise BvhParseError(
                "Expected OFFSET for joint %r at line %d, found %r"
                % (name, offset_line, token)
            )
        offset = _parse_three_floats(stream, "OFFSET of joint %r" % (name,))

        token, channel_line = stream.pop()
        if token is None or token.upper() != "CHANNELS":
            raise BvhParseError(
                "Expected CHANNELS for joint %r at line %d, found %r"
                % (name, channel_line, token)
            )
        count_token, count_line = stream.pop()
        if count_token is None:
            raise BvhParseError("Missing channel count for joint %r" % (name,))
        try:
            declared_count = int(count_token)
        except ValueError:
            raise BvhParseError(
                "Invalid channel count %r for joint %r at line %d"
                % (count_token, name, count_line)
            ) from None
        if declared_count < 0:
            raise BvhParseError("Negative channel count for joint %r" % (name,))

        channels: List[str] = []
        for _ in range(declared_count):
            token, token_line = stream.pop()
            if token is None:
                raise BvhParseError(
                    "Unexpected end of file while reading channels for joint %r" % (name,)
                )
            channels.append(_canonical_channel(token, token_line))

        joint = BvhJoint(
            name=name,
            index=len(joints),
            parent=parent,
            offset=offset,
            channels=tuple(channels),
            channel_start=sum(len(item.channels) for item in joints),
            depth=depth,
        )
        joints.append(joint)
        by_name[name] = joint

        while True:
            token, child_line = stream.peek()
            if token is None:
                raise BvhParseError("Unexpected end of file inside joint %r" % (name,))
            if token == "}":
                stream.pop()
                break
            upper = token.upper()
            if upper in ("JOINT", "ROOT"):
                stream.pop()
                child = add_joint(token, name, depth + 1)
                joint.children.append(child.name)
            elif upper == "END":
                stream.pop()
                token2, line2 = stream.pop()
                if token2 is None or token2.upper() != "SITE":
                    raise BvhParseError(
                        "Expected 'End Site' inside joint %r near line %d"
                        % (name, line2)
                    )
                # Some exporters put a name between 'End Site' and '{'.
                while True:
                    token3, line3 = stream.peek()
                    if token3 is None:
                        raise BvhParseError("Unexpected end of End Site for joint %r" % (name,))
                    if token3 == "{":
                        break
                    stream.pop()
                stream.expect("{", "opening End Site for joint %r" % (name,))
                token4, offset_line2 = stream.pop()
                if token4 is None or token4.upper() != "OFFSET":
                    raise BvhParseError(
                        "Expected End Site OFFSET for joint %r at line %d"
                        % (name, offset_line2)
                    )
                end_offset = _parse_three_floats(stream, "End Site OFFSET of joint %r" % (name,))
                stream.expect("}", "closing End Site for joint %r" % (name,))
                if joint.end_site_offset is not None:
                    raise BvhParseError("Joint %r has more than one End Site" % (name,))
                joint.end_site_offset = end_offset
            else:
                raise BvhParseError(
                    "Unexpected token %r inside joint %r at line %d"
                    % (token, name, child_line)
                )
        return joint

    token, root_line = stream.pop()
    if token is None or token.upper() != "ROOT":
        raise BvhParseError("Expected ROOT at line %d, found %r" % (root_line, token))
    root = add_joint("ROOT", None, 0)
    token, line = stream.pop()
    if token is not None:
        raise BvhParseError("Unexpected token %r after root hierarchy at line %d" % (token, line))
    if root.name not in by_name:
        raise BvhParseError("Root joint was not recorded")
    return root.name, tuple(joints)


def _parse_motion(
    text: str,
    total_channels: int,
    motion_line: int,
) -> Tuple[float, int, Tuple[Tuple[float, ...], ...]]:
    lines = text.splitlines()
    frames: Optional[int] = None
    frame_time: Optional[float] = None
    data_start = 0
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith(("#", ";")):
            index += 1
            continue
        match = re.match(r"^Frames\s*:\s*(\S+)\s*$", stripped, re.IGNORECASE)
        if match:
            try:
                frames = int(match.group(1))
            except ValueError:
                raise BvhParseError(
                    "Invalid Frames value %r at line %d"
                    % (match.group(1), motion_line + index + 1)
                ) from None
            index += 1
            break
        raise BvhParseError(
            "Expected 'Frames:' after MOTION at line %d, found %r"
            % (motion_line + index + 1, stripped)
        )
    if frames is None:
        raise BvhParseError("Missing Frames: line after MOTION")
    if frames < 0:
        raise BvhParseError("Frames value must be non-negative")

    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith(("#", ";")):
            index += 1
            continue
        match = re.match(r"^Frame\s*Time\s*:\s*(\S+)\s*$", stripped, re.IGNORECASE)
        if match:
            frame_time = _parse_float(
                match.group(1),
                motion_line + index + 1,
                "Frame Time",
            )
            index += 1
            break
        raise BvhParseError(
            "Expected 'Frame Time:' after Frames at line %d, found %r"
            % (motion_line + index + 1, stripped)
        )
    if frame_time is None:
        raise BvhParseError("Missing Frame Time: line after Frames")
    if frame_time <= 0.0:
        raise BvhParseError("Frame Time must be greater than zero")
    data_start = index

    expected = frames * total_channels
    values: List[float] = []
    for data_index in range(data_start, len(lines)):
        line = lines[data_index]
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        for token in stripped.split():
            values.append(
                _parse_float(token, motion_line + data_index + 1, "motion data")
            )
            if len(values) > expected:
                raise BvhParseError(
                    "Motion data contains more than the declared %d values (%d channels x %d frames)"
                    % (expected, total_channels, frames)
                )
    if len(values) != expected:
        raise BvhParseError(
            "Motion data has %d values; expected %d (%d channels x %d frames)"
            % (len(values), expected, total_channels, frames)
        )
    rows: List[Tuple[float, ...]] = []
    for frame_index in range(frames):
        start = frame_index * total_channels
        rows.append(tuple(values[start : start + total_channels]))
    return frame_time, frames, tuple(rows)


def load(filepath: os.PathLike | str) -> ParsedBvh:
    """Parse a BVH file without importing Blender.

    The returned object keeps each joint's channel list and channel order
    verbatim.  Motion rows are floats in source units and source rotation
    degrees.  No scale or axis conversion is applied here.
    """

    path = os.path.abspath(os.fspath(filepath))
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise BvhError("Cannot read BVH file %s: %s" % (path, exc)) from exc
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    hierarchy_text, motion_text, motion_line = _split_motion(text)
    root_name, joints = _parse_hierarchy(hierarchy_text)
    channel_names: List[str] = []
    for joint in joints:
        channel_names.extend(joint.channels)
    total_channels = len(channel_names)
    frame_time, frame_count, frames = _parse_motion(
        motion_text,
        total_channels,
        motion_line,
    )
    return ParsedBvh(
        filepath=path,
        sha256=digest,
        byte_size=len(raw),
        root_name=root_name,
        joints=joints,
        frame_time=frame_time,
        frame_count=frame_count,
        channel_names=tuple(channel_names),
        frames=frames,
        motion_line=motion_line,
    )


class BvhImportResult(NamedTuple):
    """Tuple-compatible result of :func:`import_bvh`."""

    rig: object
    action: object
    slot: object
    fps: float
    metadata: Dict[str, object]


def _safe_data_name(name: str, used: Optional[set] = None, fallback: str = "BVH") -> str:
    cleaned = "".join(
        char
        if 32 <= ord(char) and ord(char) != 127 and char not in '\\"'
        else "_"
        for char in str(name)
    ).strip()
    if not cleaned:
        cleaned = fallback
    encoded = cleaned.encode("utf-8")
    if len(encoded) > 63:
        cleaned = encoded[:63].decode("utf-8", errors="ignore").rstrip()
    if not cleaned:
        cleaned = fallback
    if used is not None:
        base = cleaned
        suffix = 1
        while cleaned in used:
            suffix_text = ".%03d" % suffix
            limit = 63 - len(suffix_text)
            cleaned = base[:limit].rstrip() + suffix_text
            suffix += 1
        used.add(cleaned)
    return cleaned


def _axis_vector(axis: str):
    from mathutils import Vector

    value = str(axis).strip().upper()
    if value not in ("X", "Y", "Z", "-X", "-Y", "-Z"):
        raise ValueError("Axis must be one of X, Y, Z, -X, -Y, -Z; got %r" % (axis,))
    sign = -1.0 if value.startswith("-") else 1.0
    index = _AXIS_INDEX[value[-1]]
    components = [0.0, 0.0, 0.0]
    components[index] = sign
    return Vector(components)


def _axis_mapping(forward: str, up: str):
    """Return source->C94 Blender rotation using Blender's axis_conversion.

    The target is the current production world convention: ``-Y`` forward and
    ``Z`` up.  Do not replace this with a hand-built transpose; the actual
    function signature and direction are verified by
    ``verification/probe_axis_v002.py`` in the same Blender runtime.
    """

    from bpy_extras.io_utils import axis_conversion

    forward = str(forward).strip().upper()
    up = str(up).strip().upper()
    forward_vector = _axis_vector(forward)
    up_vector = _axis_vector(up)
    if abs(forward_vector.dot(up_vector)) > 1e-9:
        raise ValueError("forward and up axes must be orthogonal")
    mapping = axis_conversion(
        from_forward=forward,
        from_up=up,
        to_forward=BLENDER_TARGET_FORWARD,
        to_up=BLENDER_TARGET_UP,
    )
    if abs(float(mapping.determinant()) - 1.0) > 1e-6:
        raise ValueError(
            "axis_conversion returned a non-rotation matrix (det=%r)"
            % (mapping.determinant(),)
        )
    return mapping


def _rotation_matrix(joint: BvhJoint, row: Sequence[float]):
    from mathutils import Matrix

    result = Matrix.Identity(3)
    for index, channel in enumerate(joint.channels):
        if channel not in ROTATION_CHANNELS:
            continue
        angle = math.radians(row[joint.channel_start + index])
        axis = channel[0].upper()
        # Matrix multiplication in the written channel order is the BVH
        # composition order: Rc1 @ Rc2 @ ... @ RcN.
        result = result @ Matrix.Rotation(angle, 3, axis)
    return result


def _position_vector(joint: BvhJoint, row: Sequence[float], scale: float):
    from mathutils import Vector

    values = [0.0, 0.0, 0.0]
    for index, channel in enumerate(joint.channels):
        if channel in POSITION_CHANNELS:
            values[_AXIS_INDEX[channel[0].upper()]] = (
                row[joint.channel_start + index] * scale
            )
    return Vector(values)


def _snapshot_scene(scene) -> Dict[str, object]:
    import bpy

    view_layer = getattr(bpy.context, "view_layer", None)
    selected = []
    active = None
    if view_layer is not None:
        selected = list(getattr(view_layer, "objects", []).selected)
        active = view_layer.objects.active
    return {
        "fps": scene.render.fps,
        "fps_base": scene.render.fps_base,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "frame_current": scene.frame_current,
        "frame_subframe": scene.frame_subframe,
        "camera": scene.camera,
        "selected": selected,
        "active": active,
        "use_preview_range": scene.use_preview_range,
        "frame_preview_start": scene.frame_preview_start,
        "frame_preview_end": scene.frame_preview_end,
        "unit_scale_length": scene.unit_settings.scale_length,
    }


def _restore_scene(scene, snapshot: Dict[str, object]) -> None:
    import bpy

    scene.render.fps = int(snapshot["fps"])
    scene.render.fps_base = float(snapshot["fps_base"])
    scene.frame_start = int(snapshot["frame_start"])
    scene.frame_end = int(snapshot["frame_end"])
    scene.use_preview_range = bool(snapshot["use_preview_range"])
    scene.frame_preview_start = int(snapshot["frame_preview_start"])
    scene.frame_preview_end = int(snapshot["frame_preview_end"])
    scene.unit_settings.scale_length = float(snapshot["unit_scale_length"])
    scene.camera = snapshot["camera"]

    view_layer = bpy.context.view_layer
    for obj in list(view_layer.objects):
        try:
            obj.select_set(False)
        except RuntimeError:
            pass
    for obj in snapshot["selected"]:
        if getattr(obj, "name", None) in scene.objects:
            try:
                obj.select_set(True)
            except RuntimeError:
                pass
    active = snapshot["active"]
    if active is not None and getattr(active, "name", None) in scene.objects:
        view_layer.objects.active = active
    else:
        view_layer.objects.active = None
    try:
        scene.frame_set(int(snapshot["frame_current"]), subframe=float(snapshot["frame_subframe"]))
    except (RuntimeError, TypeError):
        scene.frame_current = int(snapshot["frame_current"])


def _json_safe(value):
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def import_bvh(
    filepath: os.PathLike | str,
    scale: float,
    forward: str = "-Z",
    up: str = "Y",
    *,
    name: Optional[str] = None,
):
    """Import a BVH source into the current Blender scene.

    Parameters
    ----------
    filepath:
        Absolute or relative path to the source BVH.
    scale:
        Blender units per source unit.  It is mandatory and is never inferred
        from CMU/ASF or any other source convention.
    forward, up:
        Source axis convention.  The armature object rotation is computed by
        ``bpy_extras.io_utils.axis_conversion`` from these source axes to the
        C94 production world axes: ``-Y`` forward, ``Z`` up.  The default
        ``-Z`` / ``Y`` is the legacy Blender convention and therefore no
        longer produces an identity rotation.  The CMU samples declare source
        ``+Z`` forward / ``Y`` up in their conversion README, so pass
        ``forward="Z", up="Y"`` for those files.  Bone-local pose data stays
        in source axes and is not baked into a retarget hierarchy.

    Notes
    -----
    Root position channels follow the archived Blender add-ons reference
    (``bvh_loc - root OFFSET`` then rest inverse).  That is an old reference,
    not a local proof for nonzero root OFFSET.  Non-root position channels
    are rejected with :class:`BvhError` because their semantics are
    unverified and differ from the archived root reference.

    Returns
    -------
    BvhImportResult
        ``rig, action, slot, fps, metadata``; tuple unpacking is supported.
    """

    if not isinstance(scale, (int, float)) or isinstance(scale, bool):
        raise TypeError("scale must be a number")
    scale = float(scale)
    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError("scale must be a finite positive number")
    if not str(forward).strip() or not str(up).strip():
        raise ValueError("forward and up must be explicit axes")

    parsed = load(filepath)
    non_root_position_joints = [
        joint.name
        for joint in parsed.joints
        if joint.parent is not None and joint.has_position
    ]
    if non_root_position_joints:
        raise BvhError(
            "Unsupported BVH input: non-root position channels are present on "
            "joint(s) %s. Their semantics are not verified and differ from the "
            "archived Blender root-position reference; this importer rejects "
            "them instead of treating them as local translation increments."
            % (", ".join(non_root_position_joints),)
        )
    from mathutils import Matrix, Vector
    import bpy
    from bpy_extras import anim_utils

    scene = bpy.context.scene
    if scene is None:
        raise RuntimeError("Blender has no active scene")
    scene_snapshot = _snapshot_scene(scene)
    mapping = _axis_mapping(forward, up)
    base_name = _safe_data_name(
        name or os.path.splitext(os.path.basename(parsed.filepath))[0],
        fallback="BVH",
    )
    used_bone_names: set = set()
    bone_names: Dict[str, str] = {}
    for joint in parsed.joints:
        bone_names[joint.name] = _safe_data_name(
            joint.name,
            used_bone_names,
            fallback="Joint",
        )

    arm_data = None
    arm_obj = None
    action = None
    slot = None
    created_objects = []
    try:
        arm_data = bpy.data.armatures.new(base_name + "_Armature")
        arm_obj = bpy.data.objects.new(base_name + "_Rig", arm_data)
        collection = bpy.context.collection or scene.collection
        collection.objects.link(arm_obj)
        created_objects.append(arm_obj)
        arm_obj.rotation_mode = "QUATERNION"
        arm_obj.rotation_quaternion = mapping.to_quaternion()
        arm_obj["bvh_source_file"] = parsed.filepath
        arm_obj["bvh_source_sha256"] = parsed.sha256
        arm_obj["bvh_source_fps"] = parsed.source_fps
        arm_obj["bvh_scale"] = scale
        arm_obj["bvh_forward"] = str(forward).strip().upper()
        arm_obj["bvh_up"] = str(up).strip().upper()
        arm_obj["bvh_units"] = "Blender units = source units * scale"
        arm_data["bvh_source_file"] = parsed.filepath
        arm_data["bvh_source_fps"] = parsed.source_fps
        arm_data["bvh_scale"] = scale

        view_layer = bpy.context.view_layer
        bpy.context.view_layer.objects.active = arm_obj
        arm_obj.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            edit_bones: Dict[str, object] = {}
            absolute: Dict[str, object] = {}
            for joint in parsed.joints:
                offset = Vector(joint.offset) * scale
                if joint.parent is None:
                    absolute[joint.name] = offset
                else:
                    absolute[joint.name] = absolute[joint.parent] + offset
                edit_bones[joint.name] = arm_data.edit_bones.new(bone_names[joint.name])

            nonzero_lengths = []
            for joint in parsed.joints:
                offset_vector = Vector(joint.offset) * scale
                if offset_vector.length > 1e-8:
                    nonzero_lengths.append(offset_vector.length)
            average_length = (
                sum(nonzero_lengths) / len(nonzero_lengths)
                if nonzero_lengths
                else 0.1
            )
            if average_length <= 1e-6:
                average_length = 0.1
            up_vector = _axis_vector(up)

            for joint in parsed.joints:
                bone = edit_bones[joint.name]
                head = absolute[joint.name]
                tail = None
                best_length = 0.0
                for child_name in joint.children:
                    candidate = absolute[child_name] - head
                    length = candidate.length
                    if length > best_length + 1e-9:
                        best_length = length
                        tail = absolute[child_name]
                if tail is None and joint.end_site_offset is not None:
                    end_vector = Vector(joint.end_site_offset) * scale
                    if end_vector.length > 1e-8:
                        tail = head + end_vector
                if tail is None or (tail - head).length <= 1e-8:
                    tail = head + up_vector * average_length
                if (tail - head).length <= 1e-6:
                    tail = head + up_vector * max(average_length, 0.1)
                bone.head = head
                bone.tail = tail
                bone.roll = 0.0
                bone.use_connect = False
                if joint.parent is not None:
                    bone.parent = edit_bones[joint.parent]
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")

        for joint in parsed.joints:
            bone = arm_data.bones[bone_names[joint.name]]
            bone["bvh_source_name"] = joint.name
            bone["bvh_offset"] = list(joint.offset)
            bone["bvh_channels"] = list(joint.channels)
            bone["bvh_rotation_order"] = list(joint.rotation_channels)
            bone["bvh_euler_mode"] = joint.euler_mode or "QUATERNION"
            if joint.end_site_offset is not None:
                bone["bvh_end_site_offset"] = list(joint.end_site_offset)

        rest_rotation = {}
        rest_rotation_inverse = {}
        for joint in parsed.joints:
            bone = arm_data.bones[bone_names[joint.name]]
            rotation = bone.matrix_local.to_3x3()
            rest_rotation[joint.name] = rotation
            rest_rotation_inverse[joint.name] = rotation.inverted()

        action = bpy.data.actions.new(base_name + "_Action")
        action["bvh_source_file"] = parsed.filepath
        action["bvh_source_sha256"] = parsed.sha256
        action["bvh_source_fps"] = parsed.source_fps
        action["bvh_frame_time"] = parsed.frame_time
        action["bvh_source_frame_count"] = parsed.frame_count
        action["bvh_action_frame_start"] = 1.0
        action["bvh_action_frame_end"] = float(parsed.frame_count)
        action["bvh_scale"] = scale
        action["bvh_forward"] = str(forward).strip().upper()
        action["bvh_up"] = str(up).strip().upper()
        action["bvh_units"] = "Blender units = source units * scale"
        action["bvh_channel_orders"] = json.dumps(
            {joint.name: list(joint.channels) for joint in parsed.joints},
            ensure_ascii=False,
            sort_keys=True,
        )
        try:
            action.use_frame_range = True
            action.frame_start = 1.0
            action.frame_end = float(parsed.frame_count)
        except (AttributeError, RuntimeError, TypeError):
            pass

        animation_data = arm_obj.animation_data_create()
        slot = action.slots.new(arm_obj.id_type, "BVH")
        channelbag = anim_utils.action_ensure_channelbag_for_slot(action, slot)
        animation_data.action = action
        animation_data.action_slot = slot

        fcurve_count = 0
        key_count = 0
        if parsed.frame_count:
            for joint in parsed.joints:
                bone_name = bone_names[joint.name]
                pose_bone = arm_obj.pose.bones[bone_name]
                escaped_name = bone_name.replace("\\", "\\\\").replace('"', '\\"')

                if joint.has_rotation:
                    euler_mode = joint.euler_mode
                    if euler_mode:
                        pose_bone.rotation_mode = euler_mode
                        values = []
                        previous = None
                        for row in parsed.frames:
                            rotation = _rotation_matrix(joint, row)
                            basis = (
                                rest_rotation_inverse[joint.name]
                                @ rotation
                                @ rest_rotation[joint.name]
                            )
                            euler = (
                                basis.to_euler(euler_mode, previous)
                                if previous is not None
                                else basis.to_euler(euler_mode)
                            )
                            previous = euler
                            values.append((euler.x, euler.y, euler.z))
                        data_path = 'pose.bones["%s"].rotation_euler' % (escaped_name,)
                        for index in range(3):
                            curve = channelbag.fcurves.new(
                                data_path=data_path,
                                index=index,
                                group_name=joint.name,
                            )
                            curve.keyframe_points.add(parsed.frame_count)
                            points = curve.keyframe_points
                            for frame_index, value in enumerate(values):
                                points[frame_index].co = (
                                    float(frame_index + 1),
                                    value[index],
                                )
                                points[frame_index].interpolation = "LINEAR"
                            curve.update()
                        fcurve_count += 3
                        key_count += parsed.frame_count * 3
                    else:
                        pose_bone.rotation_mode = "QUATERNION"
                        values = []
                        previous = None
                        for row in parsed.frames:
                            rotation = _rotation_matrix(joint, row)
                            basis = (
                                rest_rotation_inverse[joint.name]
                                @ rotation
                                @ rest_rotation[joint.name]
                            )
                            quaternion = basis.to_quaternion()
                            if previous is not None and quaternion.dot(previous) < 0.0:
                                quaternion.negate()
                            previous = quaternion
                            values.append(
                                (quaternion.w, quaternion.x, quaternion.y, quaternion.z)
                            )
                        data_path = 'pose.bones["%s"].rotation_quaternion' % (
                            escaped_name,
                        )
                        for index in range(4):
                            curve = channelbag.fcurves.new(
                                data_path=data_path,
                                index=index,
                                group_name=joint.name,
                            )
                            curve.keyframe_points.add(parsed.frame_count)
                            points = curve.keyframe_points
                            for frame_index, value in enumerate(values):
                                points[frame_index].co = (
                                    float(frame_index + 1),
                                    value[index],
                                )
                                points[frame_index].interpolation = "LINEAR"
                            curve.update()
                        fcurve_count += 4
                        key_count += parsed.frame_count * 4

                if joint.has_position:
                    if pose_bone.rotation_mode == "":
                        pose_bone.rotation_mode = "QUATERNION"
                    values = []
                    for row in parsed.frames:
                        local_position = _position_vector(joint, row, scale)
                        if joint.parent is None:
                            # Archived reference: io_anim_bvh/import_bvh.py
                            # blob 5d1b5a1f67da85d455e8614907f45cd140a070e4,
                            # lines 569-575, uses bvh_loc - rest_head_local
                            # for every has_loc bone, then the rest inverse.
                            # This is an old official reference, not a local
                            # proof of nonzero-root semantics.
                            local_position = (
                                local_position - Vector(joint.offset) * scale
                            )
                        basis_position = (
                            rest_rotation_inverse[joint.name] @ local_position
                        )
                        values.append(
                            (basis_position.x, basis_position.y, basis_position.z)
                        )
                    data_path = 'pose.bones["%s"].location' % (escaped_name,)
                    for index in range(3):
                        curve = channelbag.fcurves.new(
                            data_path=data_path,
                            index=index,
                            group_name=joint.name,
                        )
                        curve.keyframe_points.add(parsed.frame_count)
                        points = curve.keyframe_points
                        for frame_index, value in enumerate(values):
                            points[frame_index].co = (
                                float(frame_index + 1),
                                value[index],
                            )
                            points[frame_index].interpolation = "LINEAR"
                        curve.update()
                    fcurve_count += 3
                    key_count += parsed.frame_count * 3

        metadata = parsed.to_metadata(include_hierarchy=True)
        metadata.update(
            {
                "importer": "bvh_source.import_bvh",
                "scale": scale,
                "scale_semantics": "Blender units per source unit",
                "forward": str(forward).strip().upper(),
                "up": str(up).strip().upper(),
                "target_forward": BLENDER_TARGET_FORWARD,
                "target_up": BLENDER_TARGET_UP,
                "axis_mapping_function": "bpy_extras.io_utils.axis_conversion",
                "object_rotation_determinant": float(mapping.determinant()),
                "translation_policy": {
                    "root_position": (
                        "bvh_loc - root OFFSET, then rest inverse; archived "
                        "blender/blender-addons io_anim_bvh/import_bvh.py "
                        "blob 5d1b5a1f67da85d455e8614907f45cd140a070e4 "
                        "lines 569-575; old reference, nonzero root not "
                        "locally verified"
                    ),
                    "non_root_position": (
                        "rejected: semantics unverified; not treated as local "
                        "offset increments"
                    ),
                },
                "object_rotation_quaternion": list(arm_obj.rotation_quaternion),
                "object_rotation_matrix": [
                    [mapping[row][column] for column in range(3)]
                    for row in range(3)
                ],
                "armature_object": arm_obj.name,
                "armature_data": arm_data.name,
                "action": action.name,
                "action_slot": getattr(slot, "name_display", None)
                or getattr(slot, "name", None),
                "action_slot_handle": getattr(slot, "handle", None),
                "action_slot_count": len(action.slots),
                "action_frame_start": 1.0,
                "action_frame_end": float(parsed.frame_count),
                "fcurve_count": fcurve_count,
                "keyframe_count": key_count,
                "bone_names": dict(bone_names),
                "scene_restore": {
                    "fps": int(scene_snapshot["fps"]),
                    "fps_base": float(scene_snapshot["fps_base"]),
                    "frame_start": int(scene_snapshot["frame_start"]),
                    "frame_end": int(scene_snapshot["frame_end"]),
                    "frame_current": int(scene_snapshot["frame_current"]),
                    "camera": getattr(scene_snapshot["camera"], "name", None),
                    "selected": [
                        getattr(obj, "name", None) for obj in scene_snapshot["selected"]
                    ],
                    "active": getattr(scene_snapshot["active"], "name", None),
                    "restored": True,
                },
                "notes": [
                    "Rotation channels are composed in the order written in each CHANNELS line.",
                    "A three-axis permutation uses Blender's reverse-order Euler mode; duplicate axes use quaternion keys.",
                    "The armature object uses axis_conversion from source forward/up to Blender/C94 -Y forward and Z up.",
                    "Root position follows the archived Blender add-ons reference (bvh_loc - root OFFSET, then rest inverse); actual nonzero root OFFSET remains locally unverified.",
                    "Non-root position channels are rejected because their semantics are unverified and differ from the archived root reference.",
                    "Scene fps, frame range, camera, selection and unit scale are restored after import.",
                    "Source timing is recorded exactly as 1 / Frame Time; no scene fps change is required to decode it.",
                ],
            }
        )
        metadata = _json_safe(metadata)
        return BvhImportResult(arm_obj, action, slot, parsed.source_fps, metadata)
    except Exception:
        if arm_obj is not None:
            try:
                bpy.data.objects.remove(arm_obj, do_unlink=True)
            except (ReferenceError, RuntimeError):
                pass
        if action is not None:
            try:
                bpy.data.actions.remove(action)
            except (ReferenceError, RuntimeError):
                pass
        if arm_data is not None and not arm_data.users:
            try:
                bpy.data.armatures.remove(arm_data)
            except (ReferenceError, RuntimeError):
                pass
        raise
    finally:
        _restore_scene(scene, scene_snapshot)


def _metadata_cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Parse a BVH file without Blender")
    parser.add_argument("file")
    parser.add_argument("--json", action="store_true", help="print the full metadata JSON")
    args = parser.parse_args(argv)
    parsed = load(args.file)
    if args.json:
        print(json.dumps(parsed.to_metadata(), indent=2, ensure_ascii=False))
    else:
        print(
            "%s: root=%s joints=%d end_sites=%d frames=%d channels=%d "
            "frame_time=%s fps=%.12f sha256=%s"
            % (
                parsed.filepath,
                parsed.root_name,
                len(parsed.joints),
                parsed.end_site_count,
                parsed.frame_count,
                parsed.total_channels,
                parsed.frame_time,
                parsed.source_fps,
                parsed.sha256,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_metadata_cli())
