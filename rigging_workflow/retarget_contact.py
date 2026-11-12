"""Read-only foot contact / slip / penetration candidate core.

This module is a pure data kernel: it does not import ``bpy``, does not write
an Action, does not write keyframes, and does not modify a scene.  Callers
supply evaluated world-space heel/ball/toe samples and an explicit ground
plane.  The kernel reports evidence and review candidates; it never corrects.

Important boundaries:

* Ground is never inferred from the lowest foot.  ``plane.status`` must be
  explicitly ``explicit`` or ``unknown``.  ``unknown`` rejects contact and
  absolute penetration decisions.
* ``near`` is two-sided.  Deep penetration is not treated as contact.
* All velocities are per second, normal/tangent split uses the supplied plane,
  and non-uniform timestamps use their real time deltas.
* Windows and hysteresis are expressed in seconds.
* Source ``planted``/``slide`` evidence and target ``gap``/``slip`` evidence
  are separate.  Target slip uses path length, not first-to-last displacement.
* ``PRESERVE`` and ``IN_PLACE`` are explicit metadata.  No absolute foot
  position is compared across root-motion modes.

The output is deterministic for a given JSON-compatible input.  Segment and
issue IDs are content-derived SHA-256 prefixes, so they remain stable across
repeated runs over the same interval evidence.
"""

from __future__ import annotations

import hashlib
import json
import math

SCHEMA_VERSION = "1.0"
MODULE_VERSION = "0.1.1"

SIDES = ("L", "R")
REGIONS = ("heel", "ball", "toe")
ROOT_MODES = ("PRESERVE", "IN_PLACE")
PLANE_STATUSES = ("explicit", "unknown")

WRITES_ACTION = False
WRITES_SCENE = False
READ_ONLY = True

SETTINGS_KEYS = (
    "h_enter",
    "h_exit",
    "h_low",
    "vn_enter",
    "vn_exit",
    "vh_enter",
    "vh_exit",
    "smooth_window_s",
    "enter_hold_s",
    "exit_hold_s",
    "min_support_s",
    "slip_eps",
    "slip_rate",
    "pen_eps",
    "penetration_min_s",
    "target_near_fraction_min",
    "target_gap_fraction_max",
    "slide_direction_cos_min",
    "slide_magnitude_ratio_min",
    "slide_magnitude_ratio_max",
)

_EPS = 1e-12


class ContactInputError(ValueError):
    """Structured contract failure; callers may serialize ``as_dict()``."""

    def __init__(self, code, message, path=None, details=None):
        super().__init__(f"{code}: {message}")
        self.code = str(code)
        self.message = str(message)
        self.path = path
        self.details = details

    def as_dict(self):
        out = {"ok": False, "code": self.code, "message": self.message}
        if self.path is not None:
            out["path"] = self.path
        if self.details is not None:
            out["details"] = self.details
        return out


def _error(code, message, path=None, details=None):
    raise ContactInputError(code, message, path, details)


def _mapping(value, path):
    if not isinstance(value, dict):
        _error("invalid_type", "must be an object", path, {"type": type(value).__name__})
    return value


def _sequence(value, path, minimum=0):
    if not isinstance(value, (list, tuple)):
        _error("invalid_type", "must be an array", path, {"type": type(value).__name__})
    if len(value) < minimum:
        _error("invalid_length", f"must contain at least {minimum} item(s)", path, {"length": len(value)})
    return value


def _finite(value, path):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _error("invalid_number", "must be a finite number", path, {"type": type(value).__name__})
    number = float(value)
    if not math.isfinite(number):
        _error("nonfinite_number", "must be finite", path, {"value": str(value)})
    return number


def _vec3(value, path):
    row = _sequence(value, path, 3)
    if len(row) != 3:
        _error("invalid_vector", "must contain exactly three numbers", path, {"length": len(row)})
    return tuple(_finite(row[i], f"{path}[{i}]") for i in range(3))


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a):
    return math.sqrt(_dot(a, a))


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a, factor):
    return (a[0] * factor, a[1] * factor, a[2] * factor)


def _tangent(vector, normal):
    amount = _dot(vector, normal)
    return _sub(vector, _scale(normal, amount))


def _canonical(value):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        _error("not_json_compatible", "input contains a non-JSON value", None, {"error": str(exc)})


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _stable_id(prefix, key):
    return f"{prefix}:{hashlib.sha256(_canonical(key).encode('utf-8')).hexdigest()[:12]}"


def _round_time(value):
    return round(float(value), 9)


def _settings(raw):
    doc = _mapping(raw, "settings")
    missing = [key for key in SETTINGS_KEYS if key not in doc]
    if missing:
        _error("missing_setting", "all normalized settings must be explicit", "settings", {"missing": missing})
    unknown = sorted(set(doc) - set(SETTINGS_KEYS) - {"notes"})
    if unknown:
        _error("unknown_setting", "unknown settings are rejected to avoid silent typos", "settings", {"unknown": unknown})
    values = {key: _finite(doc[key], f"settings.{key}") for key in SETTINGS_KEYS}

    if values["h_enter"] < 0:
        _error("invalid_setting", "h_enter must be nonnegative", "settings.h_enter")
    if values["h_exit"] < values["h_enter"]:
        _error("invalid_setting", "h_exit must be >= h_enter", "settings.h_exit")
    if values["h_low"] > 0:
        _error("invalid_setting", "h_low must be <= 0 (lower edge of the two-sided near band)", "settings.h_low")
    if values["h_low"] > values["h_enter"]:
        _error("invalid_setting", "h_low must not exceed h_enter", "settings.h_low")
    for key in ("vn_enter", "vn_exit", "vh_enter", "vh_exit", "smooth_window_s",
                "enter_hold_s", "exit_hold_s", "min_support_s", "slip_eps",
                "slip_rate", "pen_eps", "penetration_min_s",
                "slide_magnitude_ratio_min", "slide_magnitude_ratio_max"):
        if values[key] < 0:
            _error("invalid_setting", f"{key} must be nonnegative", f"settings.{key}")
    if values["vn_exit"] < values["vn_enter"]:
        _error("invalid_setting", "vn_exit must be >= vn_enter", "settings.vn_exit")
    if values["vh_exit"] < values["vh_enter"]:
        _error("invalid_setting", "vh_exit must be >= vh_enter", "settings.vh_exit")
    for key in ("target_near_fraction_min", "target_gap_fraction_max"):
        if not 0.0 <= values[key] <= 1.0:
            _error("invalid_setting", f"{key} must be in [0,1]", f"settings.{key}")
    if values["target_gap_fraction_max"] > values["target_near_fraction_min"]:
        _error(
            "invalid_setting",
            "target_gap_fraction_max must be <= target_near_fraction_min",
            "settings.target_gap_fraction_max",
        )
    if not -1.0 <= values["slide_direction_cos_min"] <= 1.0:
        _error("invalid_setting", "slide_direction_cos_min must be in [-1,1]", "settings.slide_direction_cos_min")
    if values["slide_magnitude_ratio_max"] < values["slide_magnitude_ratio_min"]:
        _error(
            "invalid_setting",
            "slide_magnitude_ratio_max must be >= slide_magnitude_ratio_min",
            "settings.slide_magnitude_ratio_max",
        )
    if "notes" in doc and not isinstance(doc["notes"], str):
        _error("invalid_type", "settings.notes must be a string", "settings.notes")
    return values


def _plane(raw, path):
    doc = _mapping(raw, path)
    status = doc.get("status")
    if status not in PLANE_STATUSES:
        _error("invalid_plane", "plane.status must be 'explicit' or 'unknown'", f"{path}.status")
    reference = doc.get("reference")
    if reference is not None and not isinstance(reference, dict):
        _error("invalid_type", "plane.reference must be an object", f"{path}.reference")
    if status == "unknown":
        if "normal" in doc or "d" in doc:
            _error(
                "ambiguous_plane",
                "unknown plane must not carry normal/d; refusing to silently use an unlabelled floor",
                path,
            )
        return {"status": "unknown", "normal": None, "d": None, "reference": reference}
    normal = _vec3(doc.get("normal"), f"{path}.normal")
    length = _norm(normal)
    if abs(length - 1.0) > 1e-6:
        _error("invalid_plane", "explicit plane normal must be unit length", f"{path}.normal", {"norm": length})
    d = _finite(doc.get("d"), f"{path}.d")
    return {"status": "explicit", "normal": normal, "d": d, "reference": reference}


def _root_motion(raw, path):
    doc = _mapping(raw, path)
    mode = doc.get("mode")
    if mode not in ROOT_MODES:
        _error("invalid_root_motion", "root_motion.mode must be PRESERVE or IN_PLACE", f"{path}.mode")
    note = doc.get("spatial_note")
    if note is not None and not isinstance(note, str):
        _error("invalid_type", "root_motion.spatial_note must be a string", f"{path}.spatial_note")
    if mode == "IN_PLACE" and (note is None or not note.strip()):
        _error(
            "missing_in_place_note",
            "IN_PLACE requires a non-empty spatial_note describing the intentional spatial difference",
            f"{path}.spatial_note",
        )
    return {"mode": mode, "spatial_note": note.strip() if note else None}


def _timestamps(raw, path):
    rows = _sequence(raw, path, 2)
    times = []
    for index, value in enumerate(rows):
        times.append(_finite(value, f"{path}[{index}]"))
    for index in range(1, len(times)):
        delta = times[index] - times[index - 1]
        if delta <= 1e-9:
            _error(
                "timestamps_not_strictly_increasing",
                "timestamps_s must be strictly increasing by more than 1e-9 s",
                f"{path}[{index}]",
                {"previous": times[index - 1], "current": times[index]},
            )
    return times


def _validate_reference(raw, path):
    doc = _mapping(raw, path)
    if not doc:
        _error("missing_reference", "reference identity must not be empty", path)
    if "sha256" in doc:
        value = doc["sha256"]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value.lower()):
            _error("invalid_reference", "reference.sha256 must be 64 hexadecimal characters", f"{path}.sha256")
    return doc


def _point_set(raw, path):
    rows = _sequence(raw, path, 1)
    return [_vec3(row, f"{path}[{index}]") for index, row in enumerate(rows)]


def _patch(raw, times, plane, path):
    doc = _mapping(raw, path)
    mode = doc.get("mode")
    count = len(times)
    explicit = plane["status"] == "explicit"
    if mode == "points":
        samples = _sequence(doc.get("samples"), f"{path}.samples", count)
        if len(samples) != count:
            _error(
                "sample_count_mismatch",
                "points samples must match timestamps_s length",
                f"{path}.samples",
                {"expected": count, "actual": len(samples)},
            )
        supplied_min = doc.get("patch_min")
        if supplied_min is not None:
            supplied_min = _sequence(supplied_min, f"{path}.patch_min", count)
            if len(supplied_min) != count:
                _error("sample_count_mismatch", "patch_min must match timestamps_s length", f"{path}.patch_min")
        centers = []
        patch_min = []
        point_counts = []
        for index, raw_points in enumerate(samples):
            points = _point_set(raw_points, f"{path}.samples[{index}]")
            center = tuple(sum(point[axis] for point in points) / len(points) for axis in range(3))
            centers.append(center)
            point_counts.append(len(points))
            if explicit:
                measured = min(_dot(plane["normal"], point) - plane["d"] for point in points)
                if supplied_min is not None:
                    supplied = _finite(supplied_min[index], f"{path}.patch_min[{index}]")
                    tolerance = 1e-7 * max(1.0, abs(measured), abs(supplied))
                    if abs(supplied - measured) > tolerance:
                        _error(
                            "patch_min_mismatch",
                            "supplied patch_min does not match the minimum clearance of the supplied points",
                            f"{path}.patch_min[{index}]",
                            {"supplied": supplied, "measured": measured},
                        )
                    patch_min.append(supplied)
                else:
                    patch_min.append(measured)
            else:
                if supplied_min is not None and any(value is not None for value in supplied_min):
                    _error(
                        "ambiguous_patch_min",
                        "patch_min must be null when plane.status is unknown",
                        f"{path}.patch_min",
                    )
                patch_min.append(None)
        return {
            "mode": "points",
            "centers": centers,
            "patch_min": patch_min,
            "point_counts": point_counts,
        }

    if mode == "center":
        centers_raw = _sequence(doc.get("centers"), f"{path}.centers", count)
        if len(centers_raw) != count:
            _error(
                "sample_count_mismatch",
                "centers must match timestamps_s length",
                f"{path}.centers",
                {"expected": count, "actual": len(centers_raw)},
            )
        centers = [_vec3(row, f"{path}.centers[{index}]") for index, row in enumerate(centers_raw)]
        mins_raw = doc.get("patch_min")
        if mins_raw is None:
            if explicit:
                _error("missing_patch_min", "center samples require patch_min for explicit ground", f"{path}.patch_min")
            patch_min = [None] * count
        else:
            mins_raw = _sequence(mins_raw, f"{path}.patch_min", count)
            if len(mins_raw) != count:
                _error("sample_count_mismatch", "patch_min must match timestamps_s length", f"{path}.patch_min")
            patch_min = []
            for index, value in enumerate(mins_raw):
                if explicit:
                    measured = _finite(value, f"{path}.patch_min[{index}]")
                    center_clearance = _dot(plane["normal"], centers[index]) - plane["d"]
                    tolerance = 1e-7 * max(1.0, abs(center_clearance), abs(measured))
                    if measured > center_clearance + tolerance:
                        _error(
                            "patch_min_above_center",
                            "patch_min cannot exceed the patch centre clearance on explicit ground",
                            f"{path}.patch_min[{index}]",
                            {"patch_min": measured, "center_clearance": center_clearance},
                        )
                    patch_min.append(measured)
                else:
                    if value is not None:
                        _error(
                            "ambiguous_patch_min",
                            "patch_min must be null when plane.status is unknown",
                            f"{path}.patch_min[{index}]",
                        )
                    patch_min.append(None)
        return {"mode": "center", "centers": centers, "patch_min": patch_min, "point_counts": None}

    _error("invalid_patch_mode", "patch.mode must be 'points' or 'center'", f"{path}.mode")


def _subject(raw, label):
    doc = _mapping(raw, label)
    reference = _validate_reference(doc.get("reference"), f"{label}.reference")
    timestamps = _timestamps(doc.get("timestamps_s"), f"{label}.timestamps_s")
    scales = _mapping(doc.get("scales"), f"{label}.scales")
    leg = _finite(scales.get("leg_length_m"), f"{label}.scales.leg_length_m")
    foot = _finite(scales.get("foot_length_m"), f"{label}.scales.foot_length_m")
    if leg <= 0 or foot <= 0:
        _error("scale_invalid", "leg_length_m and foot_length_m must be positive", f"{label}.scales")
    plane = _plane(doc.get("plane"), f"{label}.plane")
    root_motion = _root_motion(doc.get("root_motion"), f"{label}.root_motion")
    sides = _mapping(doc.get("sides"), f"{label}.sides")
    patches = {}
    for side in SIDES:
        side_doc = _mapping(sides.get(side), f"{label}.sides.{side}")
        for region in REGIONS:
            patches[(side, region)] = _patch(
                side_doc.get(region),
                timestamps,
                plane,
                f"{label}.sides.{side}.{region}",
            )
    return {
        "reference": reference,
        "timestamps_s": timestamps,
        "scales": {"leg_length_m": leg, "foot_length_m": foot},
        "plane": plane,
        "root_motion": root_motion,
        "patches": patches,
    }


def _alignment(raw):
    doc = _mapping(raw, "alignment")
    mode = doc.get("mode")
    if mode == "absolute_seconds":
        return {"mode": mode}
    if mode == "pairs":
        rows = _sequence(doc.get("pairs"), "alignment.pairs", 2)
        pairs = []
        for index, row in enumerate(rows):
            pair = _sequence(row, f"alignment.pairs[{index}]", 2)
            if len(pair) != 2:
                _error("invalid_alignment", "each pair must contain [source_s, target_s]", f"alignment.pairs[{index}]")
            pairs.append(
                (
                    _finite(pair[0], f"alignment.pairs[{index}][0]"),
                    _finite(pair[1], f"alignment.pairs[{index}][1]"),
                )
            )
        for index in range(1, len(pairs)):
            if pairs[index][0] <= pairs[index - 1][0] or pairs[index][1] <= pairs[index - 1][1]:
                _error(
                    "invalid_alignment",
                    "alignment pairs must be strictly increasing in both source and target seconds",
                    f"alignment.pairs[{index}]",
                )
        return {"mode": mode, "pairs": pairs}
    if mode == "linear":
        source_start = _finite(doc.get("source_start_s"), "alignment.source_start_s")
        source_end = _finite(doc.get("source_end_s"), "alignment.source_end_s")
        target_start = _finite(doc.get("target_start_s"), "alignment.target_start_s")
        target_end = _finite(doc.get("target_end_s"), "alignment.target_end_s")
        if not source_end > source_start:
            _error("invalid_alignment", "source_end_s must be > source_start_s", "alignment.source_end_s")
        if not target_end > target_start:
            _error("invalid_alignment", "target_end_s must be > target_start_s", "alignment.target_end_s")
        return {
            "mode": mode,
            "source_start_s": source_start,
            "source_end_s": source_end,
            "target_start_s": target_start,
            "target_end_s": target_end,
        }
    _error("invalid_alignment", "alignment.mode must be absolute_seconds, pairs, or linear", "alignment.mode")


def _interp(value, xs, ys):
    if value < xs[0] or value > xs[-1]:
        return None
    for index in range(1, len(xs)):
        if value <= xs[index]:
            left = xs[index - 1]
            right = xs[index]
            if right == left:
                return ys[index]
            ratio = (value - left) / (right - left)
            return ys[index - 1] + ratio * (ys[index] - ys[index - 1])
    return ys[-1]


class Aligner:
    def __init__(self, spec):
        self.spec = spec

    def source_to_target(self, value):
        if self.spec["mode"] == "absolute_seconds":
            return value
        if self.spec["mode"] == "pairs":
            xs = [row[0] for row in self.spec["pairs"]]
            ys = [row[1] for row in self.spec["pairs"]]
            return _interp(value, xs, ys)
        source_start = self.spec["source_start_s"]
        source_end = self.spec["source_end_s"]
        target_start = self.spec["target_start_s"]
        target_end = self.spec["target_end_s"]
        if value < source_start or value > source_end:
            return None
        ratio = (value - source_start) / (source_end - source_start)
        return target_start + ratio * (target_end - target_start)

    def target_to_source(self, value):
        if self.spec["mode"] == "absolute_seconds":
            return value
        if self.spec["mode"] == "pairs":
            xs = [row[1] for row in self.spec["pairs"]]
            ys = [row[0] for row in self.spec["pairs"]]
            return _interp(value, xs, ys)
        source_start = self.spec["source_start_s"]
        source_end = self.spec["source_end_s"]
        target_start = self.spec["target_start_s"]
        target_end = self.spec["target_end_s"]
        if value < target_start or value > target_end:
            return None
        ratio = (value - target_start) / (target_end - target_start)
        return source_start + ratio * (source_end - source_start)


def _derivative(points, times):
    count = len(points)
    if count == 1:
        return [(0.0, 0.0, 0.0)]
    out = []
    for index in range(count):
        if index == 0:
            delta = _sub(points[1], points[0])
            dt = times[1] - times[0]
        elif index == count - 1:
            delta = _sub(points[-1], points[-2])
            dt = times[-1] - times[-2]
        else:
            # Derivative of the three-point Lagrange interpolant at t_i.
            # A surrounding secant is centered at (t_prev+t_next)/2,
            # which is not t_i for nonuniform timestamps.
            before = times[index] - times[index - 1]
            after = times[index + 1] - times[index]
            left = _scale(_sub(points[index], points[index - 1]), 1.0 / before)
            right = _scale(_sub(points[index + 1], points[index]), 1.0 / after)
            out.append(tuple((after * left[axis] + before * right[axis]) /
                             (before + after) for axis in range(3)))
            continue
        out.append(_scale(delta, 1.0 / dt))
    return out


def _smooth_vectors(values, times, window_s):
    if window_s <= 0 or len(values) < 2:
        return [tuple(value) for value in values]
    count = len(times)
    cells = []
    for index in range(count):
        if index == 0:
            left = times[0] - (times[1] - times[0]) * .5
        elif index == count - 1:
            left = (times[index - 1] + times[index]) * .5
        else:
            left = (times[index - 1] + times[index]) * .5
        right = (times[index] + times[index + 1]) * .5 if index < count - 1 else times[-1] + (times[-1] - times[-2]) * .5
        cells.append((left, right))
    out = []
    half = window_s * 0.5
    for index in range(count):
        low = times[index] - half
        high = times[index] + half
        acc = [0.0, 0.0, 0.0]
        weight_sum = 0.0
        for other in range(count):
            left, right = cells[other]
            overlap = max(0.0, min(right, high) - max(left, low))
            if overlap > 0:
                for axis in range(3):
                    acc[axis] += values[other][axis] * overlap
                weight_sum += overlap
        if weight_sum > 0:
            out.append((acc[0] / weight_sum, acc[1] / weight_sum, acc[2] / weight_sum))
        else:
            out.append(tuple(values[index]))
    return out


def _hysteresis(enter, leave, times, enter_hold_s, exit_hold_s, hard_leave=None):
    count = len(times)
    state = [False] * count
    current = False
    pending = None
    for index in range(count):
        if hard_leave is not None and hard_leave[index]:
            current = False
            pending = None
            state[index] = False
            continue
        wanted = (not leave[index]) if current else enter[index]
        if wanted == current:
            pending = None
            state[index] = current
            continue
        if pending is None or pending[0] != wanted:
            pending = (wanted, index)
        required = enter_hold_s if wanted else exit_hold_s
        if times[index] - times[pending[1]] >= required - 1e-12:
            for fill in range(pending[1], index + 1):
                state[fill] = wanted
            current = wanted
            pending = None
        state[index] = current
    return state


def _runs(mask):
    out = []
    start = None
    for index, value in enumerate(mask):
        if value and start is None:
            start = index
        elif not value and start is not None:
            out.append((start, index - 1))
            start = None
    if start is not None:
        out.append((start, len(mask) - 1))
    return out


def _runs_within(mask, start, end):
    return [(start + left, start + right) for left, right in _runs(mask[start:end + 1])]


def _drop_short_runs(mask, times, minimum_s):
    out = list(mask)
    for start, end in _runs(mask):
        if times[end] - times[start] < minimum_s - 1e-12:
            for index in range(start, end + 1):
                out[index] = False
    return out


def _path_length(points, start, end):
    total = 0.0
    for index in range(start + 1, end + 1):
        total += _norm(_sub(points[index], points[index - 1]))
    return total


def _tangent_path_length(points, start, end, normal):
    total = 0.0
    for index in range(start + 1, end + 1):
        total += _norm(_tangent(_sub(points[index], points[index - 1]), normal))
    return total


def _mean(values):
    return sum(values) / len(values) if values else None


def _maximum(values):
    return max(values) if values else None


def _subject_metrics(subject, settings, role):
    times = subject["timestamps_s"]
    plane = subject["plane"]
    explicit = plane["status"] == "explicit"
    normal = plane["normal"]
    d = plane["d"]
    foot = subject["scales"]["foot_length_m"]
    leg = subject["scales"]["leg_length_m"]
    metrics = {}
    for side in SIDES:
        for region in REGIONS:
            patch = subject["patches"][(side, region)]
            centers = patch["centers"]
            raw_patch_min = patch["patch_min"]
            velocity = _smooth_vectors(_derivative(centers, times), times, settings["smooth_window_s"])
            rows = []
            speed_star_values = []
            for index in range(len(times)):
                center = centers[index]
                speed = _norm(velocity[index])
                speed_star = speed / foot
                if explicit:
                    c_center = _dot(normal, center) - d
                    c_center_star = c_center / foot
                    c_min = raw_patch_min[index]
                    c_min_star = c_min / foot
                    vn = _dot(velocity[index], normal)
                    vh = _norm(_tangent(velocity[index], normal))
                    vn_star = vn / leg
                    vh_star = vh / foot
                    penetration_star = max(0.0, -c_min_star)
                    penetration_beyond_eps_star = max(0.0, penetration_star - settings["pen_eps"])
                    near_candidate = settings["h_low"] <= c_min_star <= settings["h_enter"]
                    still_candidate = abs(vn_star) <= settings["vn_enter"]
                    planted_candidate = vh_star <= settings["vh_enter"]
                else:
                    c_center = None
                    c_center_star = None
                    c_min = None
                    c_min_star = None
                    vn = None
                    vh = None
                    vn_star = None
                    vh_star = None
                    penetration_star = None
                    penetration_beyond_eps_star = None
                    near_candidate = False
                    still_candidate = False
                    planted_candidate = False
                speed_star_values.append(speed_star)
                rows.append(
                    {
                        "time_s": times[index],
                        "center": list(center),
                        "patch_min": c_min,
                        "c_center": c_center,
                        "c_center_star": c_center_star,
                        "c_min": c_min,
                        "c_min_star": c_min_star,
                        "speed": speed,
                        "speed_star": speed_star,
                        "vn": vn,
                        "vn_star": vn_star,
                        "vh": vh,
                        "vh_star": vh_star,
                        "penetration_star": penetration_star,
                        "penetration_beyond_eps_star": penetration_beyond_eps_star,
                        "near_candidate": near_candidate,
                        "still_candidate": still_candidate,
                        "planted_candidate": planted_candidate,
                    }
                )
            if explicit and role == "source":
                enter = [row["near_candidate"] for row in rows]
                leave = [
                    (row["c_min_star"] < settings["h_low"]) or (row["c_min_star"] > settings["h_exit"])
                    for row in rows
                ]
                # Deep penetration immediately breaks contact and resets
                # pending entry, even when airborne release is delayed.
                near_state = _hysteresis(
                    enter, leave, times, settings["enter_hold_s"], settings["exit_hold_s"],
                    hard_leave=[row["c_min_star"] < settings["h_low"] for row in rows])
                still_enter = [row["still_candidate"] for row in rows]
                still_leave = [abs(row["vn_star"]) > settings["vn_exit"] for row in rows]
                still_state = _hysteresis(
                    still_enter, still_leave, times, settings["enter_hold_s"], settings["exit_hold_s"]
                )
                planted_enter = [row["planted_candidate"] for row in rows]
                planted_leave = [row["vh_star"] > settings["vh_exit"] for row in rows]
                planted_state = _hysteresis(
                    planted_enter, planted_leave, times, settings["enter_hold_s"], settings["exit_hold_s"]
                )
                support_candidate = [near_state[i] and still_state[i] for i in range(len(times))]
                support = _drop_short_runs(support_candidate, times, settings["min_support_s"])
                planted = [support[i] and planted_state[i] for i in range(len(times))]
                slide = [support[i] and not planted_state[i] for i in range(len(times))]
            elif explicit:
                near_state = [row["near_candidate"] for row in rows]
                still_state = [None] * len(times)
                planted_state = [None] * len(times)
                support_candidate = [None] * len(times)
                support = [None] * len(times)
                planted = [None] * len(times)
                slide = [None] * len(times)
            else:
                near_state = [None] * len(times)
                still_state = [None] * len(times)
                planted_state = [None] * len(times)
                support_candidate = [None] * len(times)
                support = [None] * len(times)
                planted = [None] * len(times)
                slide = [None] * len(times)
            for index, row in enumerate(rows):
                row["near_raw"] = row["near_candidate"]
                row["near"] = near_state[index]
                row["still"] = still_state[index]
                row["planted_gate"] = planted_state[index]
                row["support_candidate"] = support_candidate[index]
                row["support"] = support[index]
                row["planted"] = planted[index]
                row["slide"] = slide[index]
                if not explicit:
                    state = "floor_unresolved"
                elif row["penetration_beyond_eps_star"] > 0:
                    state = "penetration"
                elif role == "source" and row["planted"]:
                    state = "planted"
                elif role == "source" and row["slide"]:
                    state = "slide"
                elif row["near"]:
                    state = "near"
                else:
                    state = "airborne"
                row["state"] = state
            metrics[(side, region)] = {
                "rows": rows,
                "times": times,
                "centers": centers,
                "velocity": velocity,
                "near": near_state,
                "still": still_state,
                "planted_gate": planted_state,
                "support_candidate": support_candidate,
                "support": support,
                "planted": planted,
                "slide": slide,
                "speed_star": speed_star_values,
                "patch": patch,
            }
    return metrics


def _segment_record(role, side, region, kind, start, end, subject, metrics, settings, extra=None):
    times = subject["timestamps_s"]
    plane = subject["plane"]
    foot = subject["scales"]["foot_length_m"]
    leg = subject["scales"]["leg_length_m"]
    centers = metrics["centers"]
    rows = metrics["rows"]
    duration = times[end] - times[start]
    path = _path_length(centers, start, end)
    net_vector = _sub(centers[end], centers[start])
    net = _norm(net_vector)
    if plane["status"] == "explicit":
        normal = plane["normal"]
        path_tangent = _tangent_path_length(centers, start, end, normal)
        net_tangent_vector = _tangent(net_vector, normal)
        net_tangent = _norm(net_tangent_vector)
    else:
        normal = None
        path_tangent = None
        net_tangent_vector = None
        net_tangent = None
    row_slice = rows[start:end + 1]
    record = {
        "id": _stable_id(
            "segment",
            [role, side, region, kind, _round_time(times[start]), _round_time(times[end])],
        ),
        "role": role,
        "side": side,
        "region": region,
        "kind": kind,
        "time_start_s": times[start],
        "time_end_s": times[end],
        "duration_s": duration,
        "frames": end - start + 1,
        "stable": duration >= settings["min_support_s"] - 1e-12,
        "path_length": path,
        "path_length_star": path / foot,
        "path_length_tangent": path_tangent,
        "path_length_tangent_star": None if path_tangent is None else path_tangent / foot,
        "net_displacement": net,
        "net_displacement_star": net / foot,
        "net_displacement_tangent": net_tangent,
        "net_displacement_tangent_star": None if net_tangent is None else net_tangent / foot,
        "mean_speed_star": _mean([row["speed_star"] for row in row_slice]),
        "max_speed_star": _maximum([row["speed_star"] for row in row_slice]),
        "mean_vn_star": None if plane["status"] != "explicit" else _mean([abs(row["vn_star"]) for row in row_slice]),
        "mean_vh_star": None if plane["status"] != "explicit" else _mean([row["vh_star"] for row in row_slice]),
        "max_penetration_star": None
        if plane["status"] != "explicit"
        else _maximum([row["penetration_star"] for row in row_slice]),
        "max_penetration_beyond_eps_star": None
        if plane["status"] != "explicit"
        else _maximum([row["penetration_beyond_eps_star"] for row in row_slice]),
    }
    if normal is not None:
        record["target_net_tangent_vector"] = list(net_tangent_vector) if role == "target" else None
        record["source_net_tangent_vector"] = list(net_tangent_vector) if role == "source" else None
    else:
        record["target_net_tangent_vector"] = None
        record["source_net_tangent_vector"] = None
    if extra:
        record.update(extra)
    return record


def _span_record(role, side, region, kind, start, end, subject):
    times = subject["timestamps_s"]
    return {
        "id": _stable_id(
            "span",
            [role, side, region, kind, _round_time(times[start]), _round_time(times[end])],
        ),
        "role": role,
        "side": side,
        "region": region,
        "kind": kind,
        "time_start_s": times[start],
        "time_end_s": times[end],
        "duration_s": times[end] - times[start],
        "frames": end - start + 1,
    }


def _coverage(metrics, role):
    out = {}
    for side in SIDES:
        out[side] = {}
        for region in REGIONS:
            rows = metrics[(side, region)]["rows"]
            entry = {
                "frames": len(rows),
                "contact_evaluated": rows[0]["near"] is not None,
                "support_frames": None,
                "planted_frames": None,
                "slide_frames": None,
                "near_frames": None,
                "penetration_frames": None,
                "max_penetration_star": None,
            }
            if rows[0]["near"] is not None:
                entry["near_frames"] = sum(1 for row in rows if row["near"])
                entry["penetration_frames"] = sum(
                    1 for row in rows if (row["penetration_beyond_eps_star"] or 0.0) > 0
                )
                entry["max_penetration_star"] = max(
                    (row["penetration_star"] or 0.0) for row in rows
                )
            if role == "source" and rows[0]["support"] is not None:
                entry["support_frames"] = sum(1 for row in rows if row["support"])
                entry["planted_frames"] = sum(1 for row in rows if row["planted"])
                entry["slide_frames"] = sum(1 for row in rows if row["slide"])
            out[side][region] = entry
    return out


def _phase(metrics, role):
    out = {}
    if role != "source":
        return out
    for side in SIDES:
        rows_by_region = {region: metrics[(side, region)]["rows"] for region in REGIONS}
        times = [row["time_s"] for row in rows_by_region["heel"]]
        regions = {}
        firsts = []
        for region in REGIONS:
            rows = rows_by_region[region]
            supported = [row for row in rows if row["support"]]
            if supported:
                first = supported[0]["time_s"]
                last = supported[-1]["time_s"]
                firsts.append((first, region))
            else:
                first = None
                last = None
            regions[region] = {
                "first_support_s": first,
                "last_support_s": last,
                "support_frames": len(supported),
            }
        firsts.sort(key=lambda item: (item[0], REGIONS.index(item[1])))
        any_support = [
            any(rows_by_region[region][index]["support"] for region in REGIONS)
            for index in range(len(times))
        ]
        episodes = []
        for start, end in _runs(any_support):
            episode_regions = {}
            episode_firsts = []
            for region in REGIONS:
                supported = [
                    rows_by_region[region][index]
                    for index in range(start, end + 1)
                    if rows_by_region[region][index]["support"]
                ]
                if supported:
                    first = supported[0]["time_s"]
                    last = supported[-1]["time_s"]
                    episode_firsts.append((first, region))
                else:
                    first = None
                    last = None
                episode_regions[region] = {
                    "first_support_s": first,
                    "last_support_s": last,
                    "support_frames": len(supported),
                }
            episode_firsts.sort(key=lambda item: (item[0], REGIONS.index(item[1])))
            episodes.append(
                {
                    "time_start_s": times[start],
                    "time_end_s": times[end],
                    "regions": episode_regions,
                    "order": [region for _, region in episode_firsts],
                    "heel_before_ball": (
                        episode_regions["heel"]["first_support_s"] is not None
                        and episode_regions["ball"]["first_support_s"] is not None
                        and episode_regions["heel"]["first_support_s"]
                        < episode_regions["ball"]["first_support_s"]
                    ),
                    "ball_before_toe": (
                        episode_regions["ball"]["first_support_s"] is not None
                        and episode_regions["toe"]["first_support_s"] is not None
                        and episode_regions["ball"]["first_support_s"]
                        < episode_regions["toe"]["first_support_s"]
                    ),
                }
            )
        out[side] = {
            "regions": regions,
            "order": [region for _, region in firsts],
            "episodes": episodes,
            "heel_before_ball": (regions["heel"]["first_support_s"] is not None
                                 and regions["ball"]["first_support_s"] is not None
                                 and regions["heel"]["first_support_s"] < regions["ball"]["first_support_s"]),
            "ball_before_toe": (regions["ball"]["first_support_s"] is not None
                                and regions["toe"]["first_support_s"] is not None
                                and regions["ball"]["first_support_s"] < regions["toe"]["first_support_s"]),
        }
    return out


def _segment_summary(segments):
    return {
        "count": len(segments),
        "total_duration_s": sum(segment["duration_s"] for segment in segments),
        "items": segments,
    }


def _make_issue(kind, subject, side, region, start, end, evidence, severity, needs_review,
                defect, blocked=None, notes=None):
    return {
        "id": None,
        "kind": kind,
        "subject": subject,
        "side": side,
        "region": region,
        "time_start_s": start,
        "time_end_s": end,
        "severity": severity,
        "needs_review": bool(needs_review),
        "defect": bool(defect),
        "blocked": list(blocked) if blocked else [],
        "evidence": evidence,
        "notes": notes,
    }


def _dedupe_issue_ids(issues):
    used = set()
    for issue in sorted(issues, key=lambda item: (
        item["time_start_s"] if item["time_start_s"] is not None else -math.inf,
        item["kind"],
        item["subject"],
        item["side"] or "",
        item["region"] or "",
    )):
        key = [
            issue["kind"],
            issue["subject"],
            issue["side"],
            issue["region"],
            None if issue["time_start_s"] is None else _round_time(issue["time_start_s"]),
            None if issue["time_end_s"] is None else _round_time(issue["time_end_s"]),
        ]
        base = _stable_id(issue["kind"], key)
        issue_id = base
        suffix = 2
        while issue_id in used:
            issue_id = f"{base}-{suffix}"
            suffix += 1
        used.add(issue_id)
        issue["id"] = issue_id
    return sorted(issues, key=lambda item: (
        item["time_start_s"] if item["time_start_s"] is not None else -math.inf,
        item["kind"],
        item["subject"],
        item["side"] or "",
        item["region"] or "",
    ))


def _comparison_id(source_segment_id, target_side, target_region, window=None,
                   source_start=None, source_end=None):
    return _stable_id(
        "comparison",
        [
            source_segment_id,
            target_side,
            target_region,
            window,
            None if source_start is None else _round_time(source_start),
            None if source_end is None else _round_time(source_end),
        ],
    )


def _target_window(target, target_side, target_region, target_start, target_end):
    times = target["timestamps_s"]
    indices = [
        index
        for index, value in enumerate(times)
        if target_start - 1e-9 <= value <= target_end + 1e-9
    ]
    if len(indices) < 2:
        return indices, None
    return indices, (indices[0], indices[-1])


def _target_window_metrics(target, target_side, target_region, indices, metrics):
    rows = [metrics[(target_side, target_region)]["rows"][index] for index in indices]
    centers = metrics[(target_side, target_region)]["centers"]
    times = target["timestamps_s"]
    plane = target["plane"]
    foot = target["scales"]["foot_length_m"]
    start = indices[0]
    end = indices[-1]
    path3 = 0.0
    path_tangent = 0.0 if plane["status"] == "explicit" else None
    for left, right in zip(indices, indices[1:]):
        delta = _sub(centers[right], centers[left])
        path3 += _norm(delta)
        if path_tangent is not None:
            path_tangent += _norm(_tangent(delta, plane["normal"]))
    net_vector = _sub(centers[end], centers[start])
    net = _norm(net_vector)
    net_tangent_vector = _tangent(net_vector, plane["normal"]) if plane["status"] == "explicit" else None
    net_tangent = _norm(net_tangent_vector) if net_tangent_vector is not None else None
    near_fraction = sum(1 for row in rows if row["near_raw"]) / len(rows)
    return {
        "target_sample_start_s": times[start],
        "target_sample_end_s": times[end],
        "target_samples": len(indices),
        "target_path_length": path3,
        "target_path_length_star": path3 / foot,
        "target_path_length_tangent": path_tangent,
        "target_path_length_tangent_star": None if path_tangent is None else path_tangent / foot,
        "target_net_displacement": net,
        "target_net_displacement_star": net / foot,
        "target_net_displacement_tangent": net_tangent,
        "target_net_displacement_tangent_star": None if net_tangent is None else net_tangent / foot,
        "target_net_tangent_vector": None if net_tangent_vector is None else list(net_tangent_vector),
        "target_near_fraction": near_fraction,
        "target_max_penetration_star": max((row["penetration_star"] or 0.0) for row in rows),
        "target_max_penetration_beyond_eps_star": max(
            (row["penetration_beyond_eps_star"] or 0.0) for row in rows
        ),
    }


def _normal_alignment(normal_a, normal_b):
    # Compare only direction; opposite normals are a frame mismatch, not 1.0.
    return _dot(normal_a, normal_b)


def _analyze_comparison(source, target, source_metrics, target_metrics, settings, aligner):
    comparisons = []
    issues = []
    source_explicit = source["plane"]["status"] == "explicit"
    target_explicit = target["plane"]["status"] == "explicit"
    if not source_explicit or not target_explicit:
        return comparisons, issues, "not_evaluated_floor_unresolved"

    source_times = source["timestamps_s"]
    target_times = target["timestamps_s"]
    source_plane = source["plane"]
    target_plane = target["plane"]
    normals_aligned = _normal_alignment(source_plane["normal"], target_plane["normal"]) >= 1.0 - 1e-6

    for side in SIDES:
        for region in REGIONS:
            for kind in ("planted", "slide"):
                mask = source_metrics[(side, region)][kind]
                for start, end in _runs(mask):
                    source_segment = _segment_record(
                        "source", side, region, kind, start, end, source, source_metrics[(side, region)], settings
                    )
                    if not source_segment["stable"]:
                        continue
                    source_start = source_times[start]
                    source_end = source_times[end]
                    target_start = aligner.source_to_target(source_start)
                    target_end = aligner.source_to_target(source_end)
                    if target_start is None or target_end is None:
                        issue = _make_issue(
                            "alignment_coverage_incomplete",
                            "comparison",
                            side,
                            region,
                            source_start,
                            source_end,
                            {
                                "source_interval_s": [source_start, source_end],
                                "source_segment_id": source_segment["id"],
                            },
                            "warning",
                            True,
                            False,
                            notes="The explicit frame map does not cover this source interval.",
                        )
                        issues.append(issue)
                        continue
                    indices, window = _target_window(target, side, region, target_start, target_end)
                    if window is None:
                        issue = _make_issue(
                            "insufficient_target_samples",
                            "comparison",
                            side,
                            region,
                            source_start,
                            source_end,
                            {
                                "source_segment_id": source_segment["id"],
                                "target_interval_s": [target_start, target_end],
                                "target_samples": len(indices),
                            },
                            "warning",
                            True,
                            False,
                        )
                        issues.append(issue)
                        continue
                    target_values = _target_window_metrics(
                        target, side, region, indices, target_metrics
                    )
                    target_path = target_values["target_path_length_tangent_star"]
                    target_path_for_decision = target_path
                    if target_path_for_decision is None:
                        target_path_for_decision = target_values["target_path_length_star"]
                    target_duration = target_values["target_sample_end_s"] - target_values["target_sample_start_s"]
                    source_path = source_segment["path_length_tangent_star"]
                    if source_path is None:
                        source_path = source_segment["path_length_star"]
                    source_net = source_segment["net_displacement_tangent_star"]
                    if source_net is None:
                        source_net = source_segment["net_displacement_star"]
                    classification = "not_evaluated"
                    needs_review = False
                    severity = "info"
                    defect = False
                    notes = None
                    if target_values["target_max_penetration_beyond_eps_star"] > 0:
                        classification = "target_penetration"
                        needs_review = True
                        severity = "warning"
                        defect = True
                        notes = "Target penetrates beyond the explicit numerical tolerance."
                    elif target_values["target_near_fraction"] < settings["target_gap_fraction_max"]:
                        classification = "contact_transfer_gap"
                        needs_review = True
                        severity = "warning"
                        defect = True
                        notes = "Target patch is outside the two-sided near band during a source support interval."
                    elif target_values["target_near_fraction"] < settings["target_near_fraction_min"]:
                        classification = "partial_contact_coverage"
                        needs_review = True
                        severity = "warning"
                        defect = False
                        notes = "Only part of the target support window is near the explicit floor."
                    elif kind == "planted":
                        slip = target_path_for_decision > settings["slip_eps"]
                        rate = (
                            target_duration > 0
                            and target_path_for_decision / target_duration > settings["slip_rate"]
                        )
                        if slip or rate:
                            classification = "target_slip"
                            needs_review = True
                            severity = "warning"
                            defect = True
                            notes = "Source is planted but target path length exceeds the configured slip evidence."
                        else:
                            classification = "target_stable"
                    else:
                        if target_path_for_decision <= settings["slip_eps"]:
                            classification = "overconstraint_or_source_slide"
                            needs_review = True
                            severity = "warning"
                            defect = False
                            notes = (
                                "Source is sliding but target is nearly fixed; this may be an intentional "
                                "over-constraint or a source-side slide, not an automatic target defect."
                            )
                        elif not normals_aligned:
                            classification = "source_target_slide_frame_mismatch"
                            needs_review = True
                            severity = "warning"
                            defect = False
                            notes = "Source and target floor frames differ; direction comparison was not asserted."
                        else:
                            source_vector = source_segment.get("source_net_tangent_vector")
                            target_vector = target_values.get("target_net_tangent_vector")
                            if source_vector is None or target_vector is None:
                                classification = "source_target_slide_direction_unknown"
                                needs_review = True
                                severity = "warning"
                                defect = False
                            else:
                                source_length = _norm(tuple(source_vector))
                                target_length = _norm(tuple(target_vector))
                                if source_length <= 1e-12 or target_length <= 1e-12:
                                    classification = "source_target_slide_direction_unknown"
                                    needs_review = True
                                    severity = "warning"
                                    defect = False
                                else:
                                    cosine = _dot(tuple(source_vector), tuple(target_vector)) / (
                                        source_length * target_length
                                    )
                                    ratio = (
                                        target_length / target["scales"]["foot_length_m"]
                                    ) / (source_length / source["scales"]["foot_length_m"])
                                    target_values["slide_normalized_magnitude_ratio"] = ratio
                                    target_values["slide_direction_cosine"] = cosine
                                    consistent = (
                                        cosine >= settings["slide_direction_cos_min"]
                                        and settings["slide_magnitude_ratio_min"]
                                        <= ratio
                                        <= settings["slide_magnitude_ratio_max"]
                                    )
                                    if consistent:
                                        classification = "source_consistent_slide"
                                        needs_review = True
                                        severity = "info"
                                        defect = False
                                        notes = (
                                            "Synchronized intentional slide evidence: source and target "
                                            "move in a compatible direction/magnitude. Review only; no defect."
                                        )
                                    else:
                                        classification = "source_target_slide_mismatch"
                                        needs_review = True
                                        severity = "warning"
                                        defect = False
                                        notes = "Source slide and target slide differ in direction or normalized magnitude."
                    comparison = {
                        "id": _comparison_id(
                            source_segment["id"], side, region, "source_interval", source_start, source_end
                        ),
                        "source_segment_id": source_segment["id"],
                        "side": side,
                        "region": region,
                        "source_kind": kind,
                        "source_interval_s": [source_start, source_end],
                        "target_interval_s": [target_start, target_end],
                        "source_path_length_star": source_path,
                        "source_net_displacement_star": source_net,
                        "target_path_length_star": target_path,
                        "target_path_length_3d_star": target_values["target_path_length_star"],
                        "target_net_displacement_star": target_values["target_net_displacement_tangent_star"],
                        "target_net_displacement_3d_star": target_values["target_net_displacement_star"],
                        "target_near_fraction": target_values["target_near_fraction"],
                        "target_max_penetration_star": target_values["target_max_penetration_star"],
                        "target_max_penetration_beyond_eps_star": target_values[
                            "target_max_penetration_beyond_eps_star"
                        ],
                        "target_samples": target_values["target_samples"],
                        "classification": classification,
                        "needs_review": needs_review,
                        "severity": severity,
                        "defect": defect,
                        "notes": notes,
                        "evidence": target_values,
                    }
                    comparisons.append(comparison)
                    if classification in (
                        "contact_transfer_gap",
                        "partial_contact_coverage",
                        "target_slip",
                        "overconstraint_or_source_slide",
                        "source_target_slide_mismatch",
                        "source_target_slide_frame_mismatch",
                        "source_target_slide_direction_unknown",
                        "source_consistent_slide",
                    ):
                        issue = _make_issue(
                            classification,
                            "comparison",
                            side,
                            region,
                            source_start,
                            source_end,
                            comparison,
                            severity,
                            needs_review,
                            defect,
                            notes=notes,
                        )
                        issues.append(issue)

                    # A long source-planted interval can contain a target
                    # penetration or off-floor window followed by a later
                    # target-near window.  The whole-interval comparison above
                    # is intentionally coverage-oriented.  Add explicit target
                    # near subwindows so a later horizontal drift is not hidden
                    # by an earlier sink/gap.
                    if kind == "planted" and classification != "target_slip":
                        near_mask = [row["near_raw"] for row in target_metrics[(side, region)]["rows"]]
                        for near_start, near_end in _runs(near_mask):
                            near_start_t = target_times[near_start]
                            near_end_t = target_times[near_end]
                            if near_end_t - near_start_t < settings["min_support_s"] - 1e-12:
                                continue
                            overlap_start = max(target_start, near_start_t)
                            overlap_end = min(target_end, near_end_t)
                            if overlap_end - overlap_start < settings["min_support_s"] - 1e-12:
                                continue
                            if near_start_t <= target_start + 1e-6 and near_end_t >= target_end - 1e-6:
                                continue
                            source_overlap_start = aligner.target_to_source(overlap_start)
                            source_overlap_end = aligner.target_to_source(overlap_end)
                            if source_overlap_start is None or source_overlap_end is None:
                                continue
                            source_indices = [
                                index
                                for index, value in enumerate(source_times)
                                if source_overlap_start - 1e-9 <= value <= source_overlap_end + 1e-9
                            ]
                            target_indices = [
                                index
                                for index, value in enumerate(target_times)
                                if overlap_start - 1e-9 <= value <= overlap_end + 1e-9
                            ]
                            if len(source_indices) < 2 or len(target_indices) < 2:
                                continue
                            sub_values = _target_window_metrics(
                                target, side, region, target_indices, target_metrics
                            )
                            if sub_values["target_near_fraction"] < settings["target_near_fraction_min"]:
                                continue
                            sub_target_path = sub_values["target_path_length_tangent_star"]
                            if sub_target_path is None:
                                sub_target_path = sub_values["target_path_length_star"]
                            sub_duration = (
                                sub_values["target_sample_end_s"]
                                - sub_values["target_sample_start_s"]
                            )
                            sub_slip = sub_target_path > settings["slip_eps"]
                            sub_rate = (
                                sub_duration > 0
                                and sub_target_path / sub_duration > settings["slip_rate"]
                            )
                            if not (sub_slip or sub_rate):
                                continue
                            source_a = source_indices[0]
                            source_b = source_indices[-1]
                            source_sub = _segment_record(
                                "source",
                                side,
                                region,
                                "planted_window",
                                source_a,
                                source_b,
                                source,
                                source_metrics[(side, region)],
                                settings,
                            )
                            sub_source_path = source_sub["path_length_tangent_star"]
                            if sub_source_path is None:
                                sub_source_path = source_sub["path_length_star"]
                            sub_comparison = {
                                "id": _comparison_id(
                                    source_segment["id"],
                                    side,
                                    region,
                                    "target_near_subinterval",
                                    source_overlap_start,
                                    source_overlap_end,
                                ),
                                "source_segment_id": source_segment["id"],
                                "parent_source_segment_id": source_segment["id"],
                                "side": side,
                                "region": region,
                                "source_kind": "planted",
                                "window": "target_near_subinterval",
                                "source_interval_s": [source_overlap_start, source_overlap_end],
                                "target_interval_s": [overlap_start, overlap_end],
                                "source_path_length_star": sub_source_path,
                                "source_net_displacement_star": source_sub[
                                    "net_displacement_tangent_star"
                                ]
                                if source_sub["net_displacement_tangent_star"] is not None
                                else source_sub["net_displacement_star"],
                                "target_path_length_star": sub_target_path,
                                "target_path_length_3d_star": sub_values["target_path_length_star"],
                                "target_net_displacement_star": sub_values[
                                    "target_net_displacement_tangent_star"
                                ],
                                "target_net_displacement_3d_star": sub_values[
                                    "target_net_displacement_star"
                                ],
                                "target_near_fraction": sub_values["target_near_fraction"],
                                "target_max_penetration_star": sub_values["target_max_penetration_star"],
                                "target_max_penetration_beyond_eps_star": sub_values[
                                    "target_max_penetration_beyond_eps_star"
                                ],
                                "target_samples": sub_values["target_samples"],
                                "classification": "target_slip",
                                "needs_review": True,
                                "severity": "warning",
                                "defect": True,
                                "notes": (
                                    "Target is near the explicit floor inside this source-planted subwindow, "
                                    "but its path length exceeds the configured slip evidence."
                                ),
                                "evidence": sub_values,
                            }
                            comparisons.append(sub_comparison)
                            issues.append(
                                _make_issue(
                                    "target_slip",
                                    "comparison",
                                    side,
                                    region,
                                    source_overlap_start,
                                    source_overlap_end,
                                    sub_comparison,
                                    "warning",
                                    True,
                                    True,
                                    notes=sub_comparison["notes"],
                                )
                            )

    # Target near while source has no support.  This is a spatial observation,
    # not a contact truth claim and not a target defect.
    for side in SIDES:
        for region in REGIONS:
            target_rows = target_metrics[(side, region)]["rows"]
            near_mask = [row["near_raw"] for row in target_rows]
            for start, end in _runs(near_mask):
                if target_times[end] - target_times[start] < settings["min_support_s"] - 1e-12:
                    continue
                source_start = aligner.target_to_source(target_times[start])
                source_end = aligner.target_to_source(target_times[end])
                if source_start is None or source_end is None:
                    continue
                source_indices = [
                    index
                    for index, value in enumerate(source_times)
                    if source_start - 1e-9 <= value <= source_end + 1e-9
                ]
                if not source_indices:
                    continue
                any_support = any(
                    source_metrics[(side, region)]["support"][index]
                    for index in source_indices
                )
                if not any_support:
                    issue = _make_issue(
                        "target_unexpected_support",
                        "target",
                        side,
                        region,
                        target_times[start],
                        target_times[end],
                        {
                            "target_interval_s": [target_times[start], target_times[end]],
                            "source_interval_s": [source_start, source_end],
                            "target_near_frames": end - start + 1,
                            "source_support_frames_in_interval": 0,
                        },
                        "info",
                        True,
                        False,
                        notes=(
                            "Target is near the explicit floor while source has no support evidence. "
                            "This is a review observation, not a contact truth assertion."
                        ),
                    )
                    issues.append(issue)
    if source["root_motion"]["mode"] != target["root_motion"]["mode"]:
        # No root trajectory is supplied to compensate these world-space
        # paths. Keep the measurements, but do not sign their horizontal
        # discrepancy as a transfer defect across PRESERVE / IN_PLACE.
        horizontal = {
            "target_slip", "overconstraint_or_source_slide",
            "source_target_slide_mismatch", "source_target_slide_frame_mismatch",
            "source_target_slide_direction_unknown", "source_consistent_slide",
        }
        changed = set()
        for comparison in comparisons:
            if comparison["classification"] not in horizontal:
                continue
            comparison["evidence"]["uncompensated_classification"] = comparison["classification"]
            comparison.update(
                classification="root_mode_spatial_review", defect=False,
                needs_review=True, severity="info",
                notes="Root modes differ; horizontal paths are uncompensated. Review the retained measurements with the intended root removal.",
            )
            changed.add(id(comparison))
        for issue in issues:
            if id(issue["evidence"]) in changed:
                comparison = issue["evidence"]
                issue.update(kind="root_mode_spatial_review", defect=False,
                             needs_review=True, severity="info", notes=comparison["notes"])
    return comparisons, issues, "evaluated"


def analyze_contact(payload):
    """Analyze a source/target foot-contact document.

    Returns a JSON-compatible dictionary.  Raises :class:`ContactInputError`
    for contract violations.  No file, scene, Action or keyframe is written.
    """

    doc = _mapping(payload, "input")
    if doc.get("schema_version") != SCHEMA_VERSION:
        _error(
            "unsupported_schema",
            f"schema_version must be {SCHEMA_VERSION}",
            "schema_version",
            {"actual": doc.get("schema_version")},
        )
    source = _subject(doc.get("source"), "source")
    target = _subject(doc.get("target"), "target")
    settings = _settings(doc.get("settings"))
    aligner_spec = _alignment(doc.get("alignment"))
    aligner = Aligner(aligner_spec)

    source_metrics = _subject_metrics(source, settings, "source")
    target_metrics = _subject_metrics(target, settings, "target")

    source_segments = []
    source_support_segments = []
    source_foot_flat = []
    source_dual_support = []
    source_penetration = []
    target_near_segments = []
    target_penetration = []

    for side in SIDES:
        for region in REGIONS:
            metrics = source_metrics[(side, region)]
            for start, end in _runs(metrics["support"]):
                source_support_segments.append(
                    _segment_record("source", side, region, "support", start, end, source, metrics, settings)
                )
            for kind in ("planted", "slide"):
                for start, end in _runs(metrics[kind]):
                    source_segments.append(
                        _segment_record("source", side, region, kind, start, end, source, metrics, settings)
                    )
            pen_mask = [
                row["penetration_beyond_eps_star"] is not None
                and row["penetration_beyond_eps_star"] > 0
                for row in metrics["rows"]
            ]
            for start, end in _runs(pen_mask):
                record = _segment_record(
                    "source", side, region, "penetration", start, end, source, metrics, settings
                )
                record["stable"] = record["duration_s"] >= settings["penetration_min_s"] - 1e-12
                source_penetration.append(record)
        flat = [
            all(source_metrics[(side, region)]["support"][index] for region in REGIONS)
            for index in range(len(source["timestamps_s"]))
        ]
        for start, end in _runs(flat):
            source_foot_flat.append(_span_record("source", side, "all", "foot_flat", start, end, source))

    dual = []
    for index in range(len(source["timestamps_s"])):
        left = any(source_metrics[("L", region)]["support"][index] for region in REGIONS)
        right = any(source_metrics[("R", region)]["support"][index] for region in REGIONS)
        dual.append(left and right)
    for start, end in _runs(dual):
        source_dual_support.append(_span_record("source", "L+R", "all", "dual_support", start, end, source))

    for side in SIDES:
        for region in REGIONS:
            metrics = target_metrics[(side, region)]
            near_mask = [row["near_raw"] for row in metrics["rows"]]
            for start, end in _runs(near_mask):
                record = _segment_record("target", side, region, "near", start, end, target, metrics, settings)
                record["stable"] = record["duration_s"] >= settings["min_support_s"] - 1e-12
                target_near_segments.append(record)
            pen_mask = [
                row["penetration_beyond_eps_star"] is not None
                and row["penetration_beyond_eps_star"] > 0
                for row in metrics["rows"]
            ]
            for start, end in _runs(pen_mask):
                record = _segment_record("target", side, region, "penetration", start, end, target, metrics, settings)
                record["stable"] = record["duration_s"] >= settings["penetration_min_s"] - 1e-12
                target_penetration.append(record)

    comparisons, comparison_issues, comparison_status = _analyze_comparison(
        source, target, source_metrics, target_metrics, settings, aligner
    )

    issues = []
    if source["plane"]["status"] == "unknown":
        issues.append(
            _make_issue(
                "floor_unresolved",
                "source",
                None,
                None,
                source["timestamps_s"][0],
                source["timestamps_s"][-1],
                {"plane_status": "unknown"},
                "blocked",
                True,
                False,
                blocked=["source_contact", "source_absolute_penetration"],
                notes="No implicit Z=0 or lowest-foot ground estimate is permitted.",
            )
        )
    if target["plane"]["status"] == "unknown":
        issues.append(
            _make_issue(
                "floor_unresolved",
                "target",
                None,
                None,
                target["timestamps_s"][0],
                target["timestamps_s"][-1],
                {"plane_status": "unknown"},
                "blocked",
                True,
                False,
                blocked=["target_contact", "target_absolute_penetration", "target_gap_slip"],
                notes="No implicit Z=0 or lowest-foot ground estimate is permitted.",
            )
        )
    if source["root_motion"]["mode"] != target["root_motion"]["mode"]:
        issues.append(
            _make_issue(
                "root_mode_mismatch",
                "comparison",
                None,
                None,
                max(source["timestamps_s"][0], target["timestamps_s"][0]),
                min(source["timestamps_s"][-1], target["timestamps_s"][-1]),
                {
                    "source_mode": source["root_motion"]["mode"],
                    "target_mode": target["root_motion"]["mode"],
                    "source_spatial_note": source["root_motion"]["spatial_note"],
                    "target_spatial_note": target["root_motion"]["spatial_note"],
                },
                "info",
                True,
                False,
                notes=(
                    "Root modes differ. Only support-local clearance and displacement evidence is "
                    "compared; intentional root removal is not treated as a defect."
                ),
            )
        )
    if source["plane"]["status"] == "explicit" and not source_segments:
        issues.append(
            _make_issue(
                "no_source_support",
                "source",
                None,
                None,
                source["timestamps_s"][0],
                source["timestamps_s"][-1],
                {"support_segments": 0},
                "info",
                True,
                False,
                notes="Explicit floor is available, but this clip contains no stable support segment.",
            )
        )

    for record in source_penetration:
        if record["stable"]:
            issues.append(
                _make_issue(
                    "source_penetration",
                    "source",
                    record["side"],
                    record["region"],
                    record["time_start_s"],
                    record["time_end_s"],
                    record,
                    "info",
                    True,
                    False,
                    notes="Source reference penetration is quantified; it is not a target correction.",
                )
            )
    for record in target_penetration:
        if record["stable"]:
            issues.append(
                _make_issue(
                    "target_penetration",
                    "target",
                    record["side"],
                    record["region"],
                    record["time_start_s"],
                    record["time_end_s"],
                    record,
                    "warning",
                    True,
                    True,
                    notes="Absolute penetration is signed only because ground is explicit.",
                )
            )
    issues.extend(comparison_issues)
    issues = _dedupe_issue_ids(issues)

    source_frames = []
    target_frames = []
    for index in range(len(source["timestamps_s"])):
        source_frames.append(
            {
                "time_s": source["timestamps_s"][index],
                "sides": {
                    side: {
                        region: dict(source_metrics[(side, region)]["rows"][index])
                        for region in REGIONS
                    }
                    for side in SIDES
                },
            }
        )
    for index in range(len(target["timestamps_s"])):
        target_frames.append(
            {
                "time_s": target["timestamps_s"][index],
                "sides": {
                    side: {
                        region: dict(target_metrics[(side, region)]["rows"][index])
                        for region in REGIONS
                    }
                    for side in SIDES
                },
            }
        )

    source_payload = {
        "reference": source["reference"],
        "timestamps_s": source["timestamps_s"],
        "scales": source["scales"],
        "plane": source["plane"],
        "root_motion": source["root_motion"],
        "contact_evaluated": source["plane"]["status"] == "explicit",
        "frames": source_frames,
        "segments": {
            "support": source_support_segments,
            "planted": [item for item in source_segments if item["kind"] == "planted"],
            "slide": [item for item in source_segments if item["kind"] == "slide"],
            "foot_flat": source_foot_flat,
            "dual_support": source_dual_support,
            "penetration": source_penetration,
        },
        "phase": _phase(source_metrics, "source"),
        "coverage": _coverage(source_metrics, "source"),
    }
    target_payload = {
        "reference": target["reference"],
        "timestamps_s": target["timestamps_s"],
        "scales": target["scales"],
        "plane": target["plane"],
        "root_motion": target["root_motion"],
        "contact_evaluated": target["plane"]["status"] == "explicit",
        "frames": target_frames,
        "segments": {
            "near": target_near_segments,
            "penetration": target_penetration,
        },
        "coverage": _coverage(target_metrics, "target"),
    }

    comparison_counts = {}
    for item in comparisons:
        comparison_counts[item["classification"]] = comparison_counts.get(item["classification"], 0) + 1
    issue_counts = {}
    for item in issues:
        issue_counts[item["kind"]] = issue_counts.get(item["kind"], 0) + 1

    source_support_frames = sum(
        1 for side in SIDES for region in REGIONS
        for row in source_metrics[(side, region)]["rows"] if row["support"]
    )
    source_planted_frames = sum(
        1 for side in SIDES for region in REGIONS
        for row in source_metrics[(side, region)]["rows"] if row["planted"]
    )
    source_slide_frames = sum(
        1 for side in SIDES for region in REGIONS
        for row in source_metrics[(side, region)]["rows"] if row["slide"]
    )
    target_near_frames = sum(
        1 for side in SIDES for region in REGIONS
        for row in target_metrics[(side, region)]["rows"] if row["near_raw"]
    )
    target_penetration_frames = sum(
        1 for side in SIDES for region in REGIONS
        for row in target_metrics[(side, region)]["rows"]
        if (row["penetration_beyond_eps_star"] or 0.0) > 0
    )

    summary = {
        "contact_evaluated": source["plane"]["status"] == "explicit",
        "target_contact_evaluated": target["plane"]["status"] == "explicit",
        "comparison_status": comparison_status,
        "source_frames": len(source["timestamps_s"]),
        "target_frames": len(target["timestamps_s"]),
        "source_duration_s": source["timestamps_s"][-1] - source["timestamps_s"][0],
        "target_duration_s": target["timestamps_s"][-1] - target["timestamps_s"][0],
        "source_support_frames": source_support_frames,
        "source_planted_frames": source_planted_frames,
        "source_slide_frames": source_slide_frames,
        "source_foot_flat_segments": len(source_foot_flat),
        "source_dual_support_segments": len(source_dual_support),
        "target_near_frames": target_near_frames,
        "target_penetration_frames": target_penetration_frames,
        "source_segment_counts": {
            "support": len(source_support_segments),
            "planted": sum(1 for item in source_segments if item["kind"] == "planted"),
            "slide": sum(1 for item in source_segments if item["kind"] == "slide"),
            "foot_flat": len(source_foot_flat),
            "dual_support": len(source_dual_support),
            "penetration": len(source_penetration),
        },
        "target_segment_counts": {
            "near": len(target_near_segments),
            "penetration": len(target_penetration),
        },
        "comparison_counts": comparison_counts,
        "issue_counts": issue_counts,
        "issue_total": len(issues),
        "needs_review_total": sum(1 for item in issues if item["needs_review"]),
        "blocked_total": sum(1 for item in issues if item["blocked"]),
        "floor_source": source["plane"]["status"],
        "floor_target": target["plane"]["status"],
        "root_mode_source": source["root_motion"]["mode"],
        "root_mode_target": target["root_motion"]["mode"],
        "no_ground_inference": True,
        "no_correction": True,
    }

    output = {
        "schema_version": SCHEMA_VERSION,
        "producer": {
            "name": "retarget_contact",
            "version": MODULE_VERSION,
            "read_only": True,
            "writes_action": WRITES_ACTION,
            "writes_scene": WRITES_SCENE,
            "uses_bpy": False,
        },
        "input_sha256": _digest(payload),
        "settings": settings,
        "alignment": aligner_spec,
        "source": source_payload,
        "target": target_payload,
        "comparison": {
            "status": comparison_status,
            "alignment": aligner_spec,
            "items": comparisons,
            "counts": comparison_counts,
        },
        "issues": issues,
        "summary": summary,
        "limitations": [
            "Heuristic evidence only; not contact ground truth.",
            "Only explicit planes permit absolute penetration; unknown planes reject contact/penetration.",
            "Deep penetration is not coerced into contact by a one-sided clearance test.",
            "Target slip uses path length, never first-to-last displacement alone.",
            "Synchronized intentional sliding is review evidence, not an automatic defect.",
            "PRESERVE and IN_PLACE are not compared by absolute foot position.",
            "No correction, foot lock, root edit, constraint edit, weight edit or keyframe write is performed.",
        ],
    }
    return output


def analyze_contact_or_error(payload):
    """Convenience wrapper returning ``ok`` envelopes for runners."""

    try:
        result = analyze_contact(payload)
        return {"ok": True, "result": result}
    except ContactInputError as exc:
        return {"ok": False, "error": exc.as_dict()}
