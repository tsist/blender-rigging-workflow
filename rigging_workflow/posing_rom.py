"""Small deterministic pose-planning functions without scene or UI writes."""
import math


def shoulder_response(elevation_deg, forward_share, strength=1.0, manual_deg=0.0):
    """Return shoulder lift/protraction for the calibrated posing study."""
    if not all(math.isfinite(v) for v in (elevation_deg, forward_share, strength, manual_deg)):
        raise ValueError("Shoulder inputs must be finite.")
    if not 0 <= strength <= 1 or not 0 <= forward_share <= 1:
        raise ValueError("Shoulder mix and plane weights must lie in [0, 1].")
    excursion = max(0.0, min(70.0, elevation_deg - 25.0))
    lift = min(12.0, excursion * .20) * strength + manual_deg
    protraction = min(8.0, excursion * .14) * forward_share * strength
    return lift, protraction


def multi_axis_review(measures, envelopes, strength=1.0):
    """Continuous advisory level with per-pose reduction and bypass."""
    if not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError("Review strength must lie in [0, 1].")
    result = {}
    for axis, measured in measures.items():
        lo, hi = envelopes[axis]
        if not all(math.isfinite(v) for v in (measured, lo, hi)) or lo >= hi:
            raise ValueError("Invalid ROM envelope.")
        excess = max(lo - measured, measured - hi, 0.0)
        result[axis] = {"measured_deg": measured, "reference_deg": [lo, hi],
                        "advisory_deg": excess * strength, "raw_excess_deg": excess,
                        "state": "bypassed" if strength == 0 else "review" if excess > 0 else "within"}
    return result


def hinge_input(requested_deg, lower_deg, upper_deg):
    """Constrain a calibrated one-axis posing input to its declared range."""
    if not all(math.isfinite(v) for v in (requested_deg, lower_deg, upper_deg)) or lower_deg >= upper_deg:
        raise ValueError("Invalid hinge input or range.")
    return max(lower_deg, min(upper_deg, requested_deg))
