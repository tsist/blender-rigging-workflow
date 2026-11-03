"""Native control-space edits with explicit refusal and local rollback.

Only unconstrained, disconnected, unanimated control bones are edited. A
space-parent may carry the narrowly verified two-target switch below. This is
not a general constraint inverse or an animation bake implementation.
"""
import json
import math

import bpy
from mathutils import Matrix, Vector

from .collector import _all_ids

DEFAULT_TOLERANCE = 2e-5
SINGULAR_EPS = 1e-10
CHANNELS = ('location','rotation_euler','rotation_quaternion','rotation_axis_angle','scale')


class Refused(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


def plain_matrix(matrix):
    return [[float(v) for v in row] for row in matrix]


def matrix_error(first, second):
    values = [abs(float(first[r][c])-float(second[r][c])) for r in range(4) for c in range(4)]
    if not all(math.isfinite(v) for v in values):
        return float('inf')
    return max(values)


def checked_matrix(value):
    try:
        if len(value) != 4 or any(len(row) != 4 for row in value):
            raise ValueError('shape')
        if any(type(v) not in (float,int) or not math.isfinite(v) for row in value for v in row):
            raise ValueError('finite numeric values required')
        matrix = Matrix(value)
    except (TypeError, ValueError) as exc:
        raise Refused('INVALID_MATRIX', 'Expected finite numeric 4x4 matrix') from exc
    if max(abs(matrix[3][c] - (1.0 if c == 3 else 0.0)) for c in range(4)) > 1e-7:
        raise Refused('NON_AFFINE', 'Matrix bottom row must be (0,0,0,1)')
    if abs(matrix.to_3x3().determinant()) <= SINGULAR_EPS:
        raise Refused('SINGULAR_MATRIX', 'Matrix has no reliable inverse')
    return matrix


def capture_channels(bone):
    return {'rotation_mode':bone.rotation_mode,
            **{key:list(getattr(bone,key)) for key in CHANNELS}}


def restore_channels(bone, state):
    bone.rotation_mode = state['rotation_mode']
    for key in CHANNELS:
        setattr(bone,key,state[key])


def _refresh(context, rig):
    rig.update_tag()
    context.view_layer.update()
    graph = context.evaluated_depsgraph_get()
    evaluated = graph.id_eval_get(rig)
    if evaluated == rig or not evaluated.is_evaluated or evaluated.original != rig:
        raise Refused('NO_EVALUATED_RIG', 'Native lookup returned no distinct evaluated Armature')
    return evaluated


def world_matrix(context, rig, name):
    evaluated = _refresh(context,rig)
    return (evaluated.matrix_world @ evaluated.pose.bones[name].matrix).copy()


def _target(context, rig, name):
    if not isinstance(rig,bpy.types.Object) or rig.type != 'ARMATURE' or rig.pose is None:
        raise Refused('INVALID_RIG','Explicit Armature Object with pose required')
    if context.mode not in {'OBJECT','POSE'}:
        raise Refused('INVALID_MODE','Control editing requires OBJECT or POSE mode')
    if rig.library or not rig.is_editable:
        raise Refused('READ_ONLY_RIG','Linked or noneditable Object is outside this editing scope')
    bone = rig.pose.bones.get(name)
    if bone is None:
        raise Refused('MISSING_BONE','Explicit control bone does not exist')
    if bone.bone.use_connect:
        raise Refused('CONNECTED_CONTROL','Disconnected control bone required; rest hierarchy is never changed')
    if bone.constraints:
        raise Refused('CONSTRAINED_CONTROL','General constraint inversion is unsupported on the edited control')
    animation = rig.animation_data
    if animation:
        if animation.action or len(animation.nla_tracks):
            raise Refused('ANIMATED_RIG','Current-pose editing does not key or replace Action/NLA')
        prefix = bone.path_from_id()
        if any(curve.data_path == prefix+'.'+channel for curve in animation.drivers for channel in CHANNELS):
            raise Refused('DRIVEN_CONTROL','Transform drivers on the edited control would overwrite matching')
    return bone


def _check_locks(bone, basis, tolerance):
    location, rotation, scale = basis.decompose()
    old_location, old_scale = bone.location, bone.scale
    if any(locked and abs(location[i]-old_location[i]) > tolerance for i,locked in enumerate(bone.lock_location)):
        raise Refused('LOCKED_CHANNEL','Requested matching changes a locked location component')
    if any(locked and abs(scale[i]-old_scale[i]) > tolerance for i,locked in enumerate(bone.lock_scale)):
        raise Refused('LOCKED_CHANNEL','Requested matching changes a locked scale component')
    # Rotation locks are mode-dependent, especially quaternion 4D locks. Keep
    # this first editable contract narrow instead of bypassing native UI locks.
    if (any(bone.lock_rotation) or (bone.lock_rotations_4d and bone.lock_rotation_w)):
        before = bone.matrix_basis.to_quaternion()
        if rotation.rotation_difference(before).angle > tolerance:
            raise Refused('LOCKED_ROTATION','Requested matching changes locked rotation')


def _basis_for_world(context, rig, bone, target, tolerance):
    evaluated = _refresh(context,rig)
    rig_world = checked_matrix(plain_matrix(evaluated.matrix_world))
    desired_pose = rig_world.inverted() @ target
    args = {}
    if bone.parent:
        parent_pose = evaluated.pose.bones[bone.parent.name].matrix.copy()
        checked_matrix(plain_matrix(parent_pose))
        args = {'parent_matrix':parent_pose,
                'parent_matrix_local':bone.parent.bone.matrix_local.copy()}
    basis = bone.bone.convert_local_to_pose(desired_pose,bone.bone.matrix_local.copy(),invert=True,**args)
    checked_matrix(plain_matrix(basis))
    location, rotation, scale = basis.decompose()
    recomposed = Matrix.LocRotScale(location,rotation,scale)
    error = matrix_error(basis,recomposed)
    if error > tolerance:
        raise Refused('UNREPRESENTABLE_BASIS','Native inverse requires shear outside local LRS channels')
    _check_locks(bone,recomposed,tolerance)
    return recomposed, error


def _tolerance(value):
    if type(value) not in (float,int) or not math.isfinite(value) or not 1e-7 <= value <= 1e-4:
        raise Refused('INVALID_TOLERANCE','Matrix tolerance must lie within 1e-7..1e-4')
    return float(value)


def _rollback(context, rig, bone, snapshot, selector=None, key=None, old_value=None,
              before_world=None, tolerance=DEFAULT_TOLERANCE):
    result = {'stored_channels_restored':False,'evaluation_verified':False}
    try:
        if selector is not None:
            selector[key] = old_value
        restore_channels(bone,snapshot)
        result['stored_channels_restored'] = capture_channels(bone) == snapshot
        if selector is not None:
            result['stored_channels_restored'] &= selector[key] == old_value
    except Exception as error:
        result['storage_error'] = {'type':type(error).__name__,'message':str(error)}
    try:
        evaluated = _refresh(context,rig)
        result['evaluation_completed'] = True
        if before_world is not None:
            error = matrix_error(evaluated.matrix_world @ evaluated.pose.bones[bone.name].matrix, before_world)
            result['world_max_abs_error'] = error if math.isfinite(error) else None
            result['evaluation_verified'] = result['stored_channels_restored'] and error <= tolerance
    except Exception as error:
        result['evaluation_error'] = {'type':type(error).__name__,'message':str(error)}
    return result


def match_world(context, rig, bone_name, target_world, expected_current_world=None, tolerance=DEFAULT_TOLERANCE):
    """Match one explicit control's actual world matrix, or restore all channels."""
    bone, snapshot = None, None
    try:
        tolerance = _tolerance(tolerance)
        bone = _target(context,rig,bone_name)
        target = checked_matrix(target_world)
        before = checked_matrix(plain_matrix(world_matrix(context,rig,bone_name)))
        if expected_current_world is not None and matrix_error(before,checked_matrix(expected_current_world)) > tolerance:
            raise Refused('BASELINE_CONFLICT','Current control world matrix differs from the expected baseline')
        basis, inverse_error = _basis_for_world(context,rig,bone,target,tolerance)
        snapshot = capture_channels(bone)
        bone.matrix_basis = basis
        actual = world_matrix(context,rig,bone_name)
        error = matrix_error(actual,target)
        if error > tolerance:
            raise Refused('MATCH_ERROR','Native evaluated result does not meet world matrix tolerance')
        return {'ok':True,'operation':'world_match','bone':bone_name,'before_world':plain_matrix(before),
                'after_world':plain_matrix(actual),'target_world':plain_matrix(target),
                'basis_lrs_error':inverse_error,'world_max_abs_error':error,'tolerance':tolerance,
                'scope':'one current-pose control; no keys, rest edits or general constraint inverse'}
    except Exception as error:
        rollback = {'mutation_started':snapshot is not None}
        if snapshot is not None:
            rollback.update(_rollback(context,rig,bone,snapshot,before_world=before,tolerance=tolerance))
        return {'ok':False,'operation':'world_match','bone':bone_name,
                'error':{'code':error.code if isinstance(error,Refused) else 'NATIVE_ERROR','message':str(error)},
                'channels_restored':snapshot is None or rollback['stored_channels_restored'],
                'rollback':rollback}


def _space_selector(rig, control, property_bone, key):
    helper = control.parent
    selector = rig.pose.bones.get(property_bone)
    if helper is None or selector is None or selector == control or selector == helper:
        raise Refused('INVALID_SPACE_STRUCTURE','Independent Settings bone and constrained parent required')
    if key not in selector or type(selector[key]) not in (float,int,bool):
        raise Refused('INVALID_SPACE_PROPERTY','Explicit existing numeric selector property required')
    constraints = list(helper.constraints)
    if len(constraints) != 2 or any(c.type != 'COPY_TRANSFORMS' or c.mute or c.target is None for c in constraints):
        raise Refused('UNSUPPORTED_SPACE_PARENT','Exactly two unmuted Copy Transforms constraints required')
    path = selector.path_from_id()+'['+json.dumps(key,ensure_ascii=False)+']'
    if rig.animation_data and any(curve.data_path == path for curve in rig.animation_data.drivers):
        raise Refused('DRIVEN_SELECTOR','Selector property is itself driven; direct switching is unsupported')
    expected_paths = {c.path_from_id('influence') for c in constraints}
    found = set()
    ids = _all_ids()
    if ids is None:
        raise Refused('ID_BUDGET','Cannot certify selector consumers within the ID budget')
    for owner in ids:
        animation = getattr(owner,'animation_data',None)
        if not animation:
            continue
        for curve in animation.drivers:
            reads = any(t.id == rig and t.data_path == path for var in curve.driver.variables for t in var.targets)
            if not reads:
                continue
            if owner != rig or curve.data_path not in expected_paths:
                raise Refused('SELECTOR_SCOPE','Selector also drives data outside the declared space-parent constraints')
            driver = curve.driver
            if curve.mute or not driver.is_simple_expression or len(driver.variables) != 1:
                raise Refused('UNSUPPORTED_SPACE_DRIVER','Simple single-variable unmuted selector driver required')
            variable = driver.variables[0]
            if variable.type != 'SINGLE_PROP' or variable.targets[0].id != rig or variable.targets[0].data_path != path:
                raise Refused('UNSUPPORTED_SPACE_DRIVER','Selector must use an explicit native single-property variable')
            found.add(curve.data_path)
    if found != expected_paths:
        raise Refused('MISSING_SPACE_DRIVER','Both space-parent influence drivers must read the explicit selector')
    return selector,helper,constraints


def switch_space(context, rig, bone_name, property_bone, key, value,
                 expected_current_world=None, tolerance=DEFAULT_TOLERANCE):
    """Two-space helper switch preserving the unconstrained child's world pose."""
    control, selector, channels, old_value = None,None,None,None
    try:
        tolerance = _tolerance(tolerance)
        if type(value) not in (int,float) or value not in (0,1):
            raise Refused('INVALID_SPACE_VALUE','Only explicit 0/1 space endpoints are supported')
        control = _target(context,rig,bone_name)
        selector,helper,constraints = _space_selector(rig,control,property_bone,key)
        before = checked_matrix(plain_matrix(world_matrix(context,rig,bone_name)))
        if expected_current_world is not None and matrix_error(before,checked_matrix(expected_current_world)) > tolerance:
            raise Refused('BASELINE_CONFLICT','Current control world matrix differs from the expected baseline')
        channels, old_value = capture_channels(control), selector[key]
        if old_value not in (0,1):
            raise Refused('INVALID_CURRENT_SPACE','Current selector must also be at a supported 0/1 endpoint')
        selector[key] = type(old_value)(value)
        evaluated = _refresh(context,rig)
        influences = [evaluated.pose.bones[helper.name].constraints[c.name].influence for c in constraints]
        expected = [1.0-value,float(value)]
        if any(abs(a-b)>1e-6 for a,b in zip(influences,expected)):
            raise Refused('NON_ONEHOT_SPACE','Constraint order/driver outputs do not match the declared 0/1 spaces')
        result = match_world(context,rig,bone_name,plain_matrix(before),tolerance=tolerance)
        if not result['ok']:
            raise Refused(result['error']['code'],result['error']['message'])
        result.update(operation='space_switch',property={'bone':property_bone,'key':key,'before':old_value,'after':selector[key]},
                      space_parent=helper.name,space_influences=influences)
        return result
    except Exception as error:
        rollback = {'mutation_started':channels is not None}
        if channels is not None:
            rollback.update(_rollback(context,rig,control,channels,selector,key,old_value,
                                      before_world=before,tolerance=tolerance))
        return {'ok':False,'operation':'space_switch','bone':bone_name,
                'error':{'code':error.code if isinstance(error,Refused) else 'NATIVE_ERROR','message':str(error)},
                'channels_and_property_restored':channels is None or rollback['stored_channels_restored'],
                'rollback':rollback}
