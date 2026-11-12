# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixed-step tip springs, with local-Y swing output and no Blender state.

Each call solves one independent bone's captured base trajectory in metres.
Strength blends only the returned swing; the constrained internal state always
runs at full strength. This is elastic bone motion, not a soft-body/collision
solver. The integrator and geometry below are project-owned implementations.
"""
import math
import numbers

import numpy as np


_MAX_SAMPLES = 1_000_000
_Y = np.array((0., 1., 0.), dtype=np.float64)


def _scalar(value, name, minimum, maximum=None, *, positive=False):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, numbers.Real):
        raise ValueError(name + ' must be a finite scalar')
    value = float(value)
    if (not math.isfinite(value) or value < minimum
            or (positive and value == minimum)
            or (maximum is not None and value > maximum)):
        raise ValueError(name + ' is outside its finite supported range')
    return value


def _array(value, name, tail):
    try:
        array = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(name + ' must contain finite numeric values') from exc
    if array.ndim != len(tail) + 1 or array.shape[1:] != tail:
        raise ValueError(name + ' must have shape (N, ' + ', '.join(map(str, tail)) + ')')
    if not 1 <= len(array) <= _MAX_SAMPLES:
        raise ValueError('sample count must be between 1 and ' + str(_MAX_SAMPLES))
    if not np.isfinite(array).all():
        raise ValueError(name + ' contains a non-finite value')
    return array


def _unit(value):
    """Normalize without squaring a potentially tiny or large input vector."""
    scale = float(np.max(np.abs(value)))
    if scale == 0.:
        return None
    scaled = value / scale
    return scaled / float(np.linalg.norm(scaled))


def _swing(direction, limit, previous):
    """Shortest local Y -> direction swing, clipped to the requested cone."""
    cosine = float(np.clip(direction[1], -1., 1.))
    # atan2 retains small angles that acos(1 - epsilon) rounds to zero.
    transverse = math.hypot(float(direction[0]), float(direction[2]))
    angle = math.atan2(transverse, cosine)
    clipped = angle > limit
    angle = min(angle, limit)
    if angle == 0.:
        return np.array((1., 0., 0., 0.)), clipped, angle
    if transverse > 0.:
        axis = np.array((direction[2] / transverse, 0., -direction[0] / transverse))
    else:
        # An exact antipode has no unique swing axis. Continue the last swing's
        # transverse axis, or choose +X deterministically at the first antipode.
        axis = _unit(np.array((previous[1], 0., previous[3])))
        if axis is None:
            axis = np.array((1., 0., 0.))
    half = .5 * angle
    quaternion = np.r_[math.cos(half), axis * math.sin(half)]
    if float(np.dot(quaternion, previous)) < 0.:
        quaternion = -quaternion
    return quaternion, clipped, angle


def _rotated_y(quaternion):
    w, x, y, z = quaternion
    return np.array((2. * (x*y - w*z),
                     1. - 2. * (x*x + z*z),
                     2. * (y*z + w*x)))


def solve(heads, tips, world_to_local, *, dt, frequency_hz=3.0,
          damping_ratio=.7, gravity=(0, 0, 0), max_angle_degrees=30.0,
          strength=1.0):
    """Return local wxyz swing quaternions and matching constrained world tips.

    ``heads``/``tips`` are (N, 3) metre-valued base head/tail trajectories.
    ``world_to_local`` is (N, 3, 3): the inverse of each base bone's actual
    orientation-preserving world linear matrix. The base tail direction is its
    local +Y axis. Scale may be non-uniform; output positions still retain the
    captured world head-to-tail length. ``dt`` is a fixed positive step.

    The first sample is the captured base tip, at zero velocity. Remaining
    samples use implicit Euler for spring acceleration and damping relative to
    target velocity, followed by spherical/cone projection. Velocity loses its
    radial component. Strength scales the final local swing angle only, so it
    does not change the internal full-strength simulation.

    Inputs are never written. Relative rotations contain swing only (no local-Y
    twist). Invalid/non-finite input, singular/reflected matrices, zero bone
    length, arithmetic overflow and over one million samples raise ValueError.
    There is no persistent state, cache, random source, collision or file I/O.
    """
    heads = _array(heads, 'heads', (3,))
    tips = _array(tips, 'tips', (3,))
    matrices = _array(world_to_local, 'world_to_local', (3, 3))
    if len(tips) != len(heads) or len(matrices) != len(heads):
        raise ValueError('all trajectories must have the same sample count')
    dt = _scalar(dt, 'dt', 0., positive=True)
    frequency_hz = _scalar(frequency_hz, 'frequency_hz', 0.)
    damping_ratio = _scalar(damping_ratio, 'damping_ratio', 0.)
    max_angle_degrees = _scalar(max_angle_degrees, 'max_angle_degrees', 0., 180.)
    strength = _scalar(strength, 'strength', 0., 1.)
    try:
        gravity = np.asarray(gravity, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('gravity must be a finite three-vector in m/s^2') from exc
    if gravity.shape != (3,) or not np.isfinite(gravity).all():
        raise ValueError('gravity must be a finite three-vector in m/s^2')

    omega = math.tau * frequency_hz
    step_omega = omega * dt
    spring = step_omega * step_omega
    damping = 2. * damping_ratio * step_omega
    denominator = 1. + damping + spring
    coefficients = (omega, step_omega, spring, damping, denominator)
    if not all(math.isfinite(v) for v in coefficients):
        raise ValueError('frequency, damping and dt produce non-finite solver coefficients')

    try:
        with np.errstate(over='raise', invalid='raise', divide='raise', under='ignore'):
            signs, logs = np.linalg.slogdet(matrices)
            if not ((signs > 0.).all() and np.isfinite(logs).all()):
                raise ValueError('world_to_local matrices must be invertible and orientation-preserving')
            local_to_world = np.linalg.inv(matrices)
            if not np.isfinite(local_to_world).all():
                raise ValueError('world_to_local inverse contains non-finite values')
            base = tips - heads
            scales = np.max(np.abs(base), axis=1)
            if not (scales > 0.).all():
                raise ValueError('every base bone must have a positive head-to-tail length')
            lengths = scales * np.linalg.norm(base / scales[:, None], axis=1)
            if not np.isfinite(lengths).all():
                raise ValueError('base bone length is not finite')

            count = len(heads)
            quaternions = np.empty((count, 4), dtype=np.float64)
            positions = np.empty((count, 3), dtype=np.float64)
            quaternions[0] = (1., 0., 0., 0.)
            positions[0] = tips[0]
            position = tips[0].copy()
            velocity = np.zeros(3)
            previous_full = quaternions[0].copy()
            previous_output = quaternions[0].copy()
            limit = math.radians(max_angle_degrees)
            clamped_count = degenerate_count = 0
            max_angle = max_offset = max_length_error = 0.

            for index in range(1, count):
                target_velocity = (tips[index] - tips[index-1]) / dt
                # This displacement form is algebraically the requested
                # implicit velocity update, avoiding an unnecessary omega^2/dt
                # intermediate. The full target-velocity damping term remains.
                displacement = ((dt * velocity + spring * (tips[index] - position)
                                 + damping * dt * target_velocity + dt * dt * gravity)
                                / denominator)
                velocity = displacement / dt
                proposed = position + displacement
                world_direction = _unit(proposed - heads[index])
                if world_direction is None:
                    world_direction = base[index] / lengths[index]
                    degenerate_count += 1
                local_direction = _unit(matrices[index] @ world_direction)
                if local_direction is None:
                    raise ValueError('world_to_local collapsed a finite direction')
                full, clipped, angle = _swing(local_direction, limit, previous_full)
                clamped_count += int(clipped)
                previous_full = full
                full_world = _unit(local_to_world[index] @ _rotated_y(full))
                if full_world is None:
                    raise ValueError('local_to_world collapsed a finite direction')
                position = heads[index] + lengths[index] * full_world
                velocity -= float(np.dot(velocity, full_world)) * full_world

                if strength == 0.:
                    output = np.array((1., 0., 0., 0.))
                    positions[index] = tips[index]
                elif strength == 1.:
                    output = full.copy()
                    positions[index] = position
                else:
                    # The full swing's axis is independent of its quaternion
                    # sign; reconstruct from its constrained local direction.
                    output, _, _ = _swing(_rotated_y(full), angle * strength, previous_output)
                    output_world = _unit(local_to_world[index] @ _rotated_y(output))
                    if output_world is None:
                        raise ValueError('blended local swing collapsed a finite direction')
                    positions[index] = heads[index] + lengths[index] * output_world
                if float(np.dot(output, previous_output)) < 0.:
                    output = -output
                quaternions[index] = output
                previous_output = output
                max_angle = max(max_angle, angle * strength)
                max_offset = max(max_offset, float(np.linalg.norm(positions[index] - tips[index])))
                max_length_error = max(max_length_error,
                    abs(float(np.linalg.norm(positions[index] - heads[index])) - lengths[index]))
                if not (np.isfinite(position).all() and np.isfinite(velocity).all()
                        and np.isfinite(positions[index]).all() and np.isfinite(output).all()):
                    raise ValueError('elastic state became non-finite')
    except (FloatingPointError, np.linalg.LinAlgError) as exc:
        raise ValueError('elastic trajectory cannot be represented with finite invertible float64 data') from exc

    return {'quaternions': quaternions, 'positions': positions,
            'statistics': {'method': 'fixed_step_implicit_euler_spherical_tip_spring',
                           'sample_count': count, 'dt': dt, 'frequency_hz': frequency_hz,
                           'damping_ratio': damping_ratio, 'strength': strength,
                           'max_angle_degrees': max_angle_degrees,
                           'max_swing_degrees': math.degrees(max_angle),
                           'max_tip_offset_meters': max_offset,
                           'max_length_error_meters': max_length_error,
                           'cone_clamped_samples': clamped_count,
                           'degenerate_direction_samples': degenerate_count,
                           'initialization': 'first base tip; zero velocity',
                           'strength_scope': 'final output only; internal full-strength state unchanged'}}
