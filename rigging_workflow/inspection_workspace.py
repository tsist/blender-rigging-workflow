"""Owned native multi-view inspection. No cameras, geometry edits or scoring.

Workspace metadata is persistent by name so a saved/reopened inspection can be
left safely without retaining stale Python/RNA pointers. Object isolation lives
in a dedicated view layer; the user's original layer is not repurposed.
"""
import json
import math

import bpy
from mathutils import Quaternion, Vector

from . import positioning

OWNER = 'c94_inspection_workspace_owner'
SESSION = 'c94_inspection_workspace_session'
LAYER = 'c94_inspection_view_layer'
CONTROLS = 'c94_inspection_control_column'

PRESETS = [
    ('ALL', '全身', '检查整个人物'),
    ('BODY', '身体', '身体主骨与关节'),
    ('FACE', '头脸', '完整面部参考'),
    ('EYES', '眼周', '眼睑和眉毛'),
    ('NOSE', '鼻子', '鼻梁、鼻尖与鼻底'),
    ('MOUTH', '嘴唇', '嘴唇轮廓'),
    ('CHIN', '下巴', '下巴前缘与内部下颚根'),
    ('HAND_L', '左手', '左手与掌骨'),
    ('HAND_R', '右手', '右手与掌骨'),
    ('FINGER_L', '左手指', '左手全部指节，可选点继续放大'),
    ('FINGER_R', '右手指', '右手全部指节，可选点继续放大'),
    ('SHOULDER', '肩部', '肩与大臂根'),
    ('ELBOW', '肘部', '左右肘关节'),
    ('WRIST', '腕部', '左右腕关节'),
    ('HIP', '髋部', '左右髋关节'),
    ('KNEE', '膝部', '左右膝关节'),
    ('ANKLE', '踝部', '左右踝关节'),
    ('SELECTED', '选中位置', '聚焦当前标记或参考骨端点'),
]
_CODES = {p[0] for p in PRESETS}


def _state(context):
    state = positioning.read_state(context.scene)
    if not state:
        raise ValueError('当前场景没有角色定位状态')
    rig = context.scene.objects.get(state.get('fitted_reference', ''))
    target = context.scene.objects.get(state.get('target_mesh', ''))
    if (not rig or rig.type != 'ARMATURE'
            or rig.get(positioning.OWNER_KEY) != state['owner']):
        raise ValueError('当前角色的拟合参考骨缺失或身份不匹配')
    if not target or target.type != 'MESH':
        raise ValueError('当前角色的检查模型缺失')
    return state, rig, target


def _session(context):
    workspace = context.window.workspace if context.window else context.workspace
    raw = workspace.get(SESSION)
    if not raw:
        raise ValueError('请先进入定位检查工作区')
    session = json.loads(raw)
    state, rig, target = _state(context)
    if workspace.get(OWNER) != state['owner'] or not session.get('active'):
        raise ValueError('当前并非此角色的定位检查工作区')
    return session, state, rig, target


def _store(workspace, session):
    workspace[SESSION] = json.dumps(session, ensure_ascii=False, allow_nan=False)


def is_active(context):
    try:
        session, *_ = _session(context)
        return not session.get('initializing', False)
    except (ValueError, ReferenceError, TypeError):
        return False


def status(context):
    """Small UI-facing initialization status, including asynchronous errors."""
    state = positioning.read_state(context.scene)
    if not state:
        return {'active': False, 'pending': False}
    workspace = next((w for w in bpy.data.workspaces if w.get(OWNER) == state['owner']), None)
    session = json.loads(workspace.get(SESSION, '{}')) if workspace else {}
    return {'active': is_active(context), 'pending': bool(session.get('initializing')),
            'error': session.get('initialization_error'),
            'workspace': workspace.name if workspace else None}


def _view_areas(window):
    areas = [a for a in window.screen.areas if a.type == 'VIEW_3D']
    if window.workspace.get(CONTROLS) and len(areas) > 3:
        control = max(areas, key=lambda a: (a.x, -a.y))
        areas.remove(control)
    return sorted(areas, key=lambda a: (a.x, -a.y))


def _ensure_views(context):
    window = context.window
    if window is None or bpy.app.background:
        raise ValueError('多视图检查需要原生Blender窗口')
    areas = _view_areas(window)
    if not areas:
        area = max(window.screen.areas, key=lambda a: a.width * a.height)
        area.type = 'VIEW_3D'
        areas = [area]
    # Only the copied inspection screen is split. Original areas are untouched.
    while len(areas) < 3:
        area = max(areas, key=lambda a: a.width * a.height)
        region = next(r for r in area.regions if r.type == 'WINDOW')
        old_count = len(areas)
        with bpy.context.temp_override(window=window, area=area, region=region):
            result = bpy.ops.screen.area_split(
                direction='VERTICAL' if old_count == 1 else 'HORIZONTAL', factor=.5)
        areas = _view_areas(window)
        if 'FINISHED' not in result or len(areas) <= old_count:
            raise RuntimeError('检查视窗拆分失败，原工作区仍保留')
    # Give controls their own right column instead of covering the oblique view.
    if not window.workspace.get(CONTROLS):
        control = next((a for a in window.screen.areas if a.type == 'PROPERTIES'), None)
        if control is None:
            side = max(areas, key=lambda a: (a.x, a.y))
            region = next(r for r in side.regions if r.type == 'WINDOW')
            before = {a.as_pointer() for a in window.screen.areas}
            with bpy.context.temp_override(window=window, area=side, region=region):
                bpy.ops.screen.area_split(direction='VERTICAL', factor=.65)
            control = next((a for a in window.screen.areas if a.as_pointer() not in before), None)
            if control is None:
                raise RuntimeError('无法建立独立检查控制列')
        control.type = 'VIEW_3D'
        window.workspace[CONTROLS] = True
        areas = _view_areas(window)
    control = max((a for a in window.screen.areas if a.type == 'VIEW_3D'),
                  key=lambda a: (a.x, -a.y))
    controls_space = control.spaces.active
    controls_space.show_region_toolbar = False
    controls_space.show_region_ui = True
    controls_space.show_gizmo = False
    controls_space.overlay.show_overlays = False
    # Blender 5.2 Region.active_panel_category is read-only; the native
    # '角色绑定' sidebar tab remains available in this dedicated control column.
    # Existing additional views in a user layout are harmless and share framing.
    for i, area in enumerate(areas):
        space = area.spaces.active
        space.show_region_toolbar = False
        space.show_region_ui = False
        space.shading.type = 'SOLID'
        space.shading.light = 'STUDIO'
        space.shading.color_type = 'SINGLE'
        space.shading.single_color = (.62, .66, .71)
        space.shading.show_shadows = True
        space.shading.show_cavity = True
        space.shading.cavity_type = 'WORLD'
        space.overlay.show_floor = False
        space.overlay.show_axis_x = False
        space.overlay.show_axis_y = False
        space.overlay.show_relationship_lines = False
        space.overlay.show_extras = True
        space.overlay.show_bones = True
        space.overlay.show_text = True
    return areas


def _bone_snapshot(rig):
    return {
        'rig': rig.name,
        'bone_hide': {b.name: bool(b.hide) for b in rig.data.bones},
        'pose_hide': {b.name: bool(b.hide) for b in rig.pose.bones},
        'collections': {c.name: bool(c.is_visible) for c in rig.data.collections_all},
        'show_in_front': bool(rig.show_in_front),
        'show_names': bool(rig.data.show_names),
    }


def _restore_bones(scene, snapshot):
    rig = scene.objects.get(snapshot.get('rig', ''))
    if rig is None or rig.type != 'ARMATURE':
        return
    for name, value in snapshot['bone_hide'].items():
        if name in rig.data.bones:
            rig.data.bones[name].hide = value
    for name, value in snapshot['pose_hide'].items():
        if name in rig.pose.bones:
            rig.pose.bones[name].hide = value
    for name, value in snapshot['collections'].items():
        if name in rig.data.collections_all:
            rig.data.collections_all[name].is_visible = value
    rig.show_in_front = snapshot['show_in_front']
    rig.data.show_names = snapshot['show_names']


def enter(context):
    state, rig, target = _state(context)
    pending = status(context)
    if pending['pending']:
        return {'workspace': pending['workspace'], 'pending': True,
                'message': '正在建立多视图检查工作区'}
    if is_active(context):
        return focus(context, json.loads(context.window.workspace[SESSION]).get('preset', 'FACE'))
    window = context.window
    if window is None or bpy.app.background:
        raise ValueError('请在Blender原生窗口中进入检查工作区')
    original = window.workspace
    session = {
        'version': 1, 'active': True, 'original_workspace': original.name,
        'original_view_layer': window.view_layer.name,
        'original_active': context.view_layer.objects.active.name
        if context.view_layer.objects.active else None,
        'original_selection': [o.name for o in context.selected_objects],
        'original_mode': context.object.mode if context.object else 'OBJECT',
        'bones': _bone_snapshot(rig), 'preset': 'FACE', 'isolate': False,
        'marker_front': {m['object']: bool(context.scene.objects[m['object']].show_in_front)
                         for m in state['markers'] if m['object'] in context.scene.objects},
    }
    if context.object and context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    workspace = next((w for w in bpy.data.workspaces if w.get(OWNER) == state['owner']), None)
    if workspace is None:
        before = {w.as_pointer() for w in bpy.data.workspaces}
        bpy.ops.workspace.duplicate()
        workspace = next((w for w in bpy.data.workspaces if w.as_pointer() not in before), None)
        if workspace is None:
            raise RuntimeError('无法建立独立定位检查工作区')
        workspace.name = '定位检查·' + state['owner'][:6]
        workspace[OWNER] = state['owner']
    session['initializing'] = True
    _store(workspace, session)
    window.workspace = workspace
    # Blender commits workspace switching in its event loop, not within the
    # current operator. Never split a screen using the stale previous context.
    window_pointer, workspace_name = window.as_pointer(), workspace.name
    attempts = [0]

    def complete():
        attempts[0] += 1
        window = next((w for w in bpy.context.window_manager.windows
                       if w.as_pointer() == window_pointer), None)
        workspace = bpy.data.workspaces.get(workspace_name)
        if window is None or workspace is None:
            return None
        if window.workspace != workspace:
            if attempts[0] < 20:
                return .1
            pending = json.loads(workspace[SESSION])
            pending.update(active=False, initializing=False,
                           initialization_error='工作区切换未完成，请重新进入')
            _store(workspace, pending)
            return None
        with bpy.context.temp_override(window=window):
            current = bpy.context
            try:
                _finish_enter(current)
            except Exception as exc:
                pending = json.loads(workspace[SESSION])
                pending['initialization_error'] = str(exc)
                pending['initializing'] = False
                _store(workspace, pending)
                try:
                    leave(current)
                finally:
                    print('C94 inspection initialization:', repr(exc))
        return None

    bpy.app.timers.register(complete, first_interval=.05)
    return {'workspace': workspace.name, 'pending': True,
            'message': '正在建立多视图检查工作区'}


def _finish_enter(context):
    session, state, rig, target = _session(context)
    workspace, window = context.window.workspace, context.window
    # A private layer lets us hide source atlas/old candidates without changing
    # the original layer's object visibility and selection.
    layer = context.scene.view_layers.get(workspace.get(LAYER, ''))
    if layer is None:
        before = set(context.scene.view_layers.keys())
        bpy.ops.scene.view_layer_add(type='COPY')
        layer = next(l for l in context.scene.view_layers if l.name not in before)
        layer.name = '定位检查·' + state['owner'][:6]
        workspace[LAYER] = layer.name
    window.view_layer = layer
    _ensure_views(context)
    allowed = {rig.name, target.name, *[m['object'] for m in state['markers']]}
    for obj in layer.objects:
        if (obj.get(positioning.OWNER_KEY) == state['owner']
                and obj.get('c94_inspection_helper')):
            continue
        obj.hide_set(obj.name not in allowed, view_layer=layer)
    rig.hide_set(False, view_layer=layer)
    target.hide_set(False, view_layer=layer)
    for obj in layer.objects:
        obj.select_set(obj == rig, view_layer=layer)
    layer.objects.active = rig
    rig.data.show_names = False
    settings = getattr(context.scene, 'c94_inspection', None)
    preset = getattr(settings, 'preset', 'FACE')
    if preset not in _CODES or preset == 'SELECTED':
        preset = 'FACE'
    session['isolate'] = bool(getattr(settings, 'isolate', True))
    session['initializing'] = False
    _store(workspace, session)
    set_xray(context, bool(getattr(settings, 'xray', True)))
    return focus(context, preset)


def leave(context):
    session, state, rig, target = _session(context)
    workspace = context.window.workspace
    window = context.window
    if context.object and context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    _restore_bones(context.scene, session['bones'])
    for name, front in session.get('marker_front', {}).items():
        obj = context.scene.objects.get(name)
        if obj and obj.get(positioning.OWNER_KEY) == state['owner']:
            obj.show_in_front = front
    original = bpy.data.workspaces.get(session['original_workspace'])
    original_layer = context.scene.view_layers.get(session['original_view_layer'])
    if original is None or original_layer is None:
        raise ValueError('原工作区或视图层已被移除，无法自动返回')
    session['active'] = False
    _store(workspace, session)
    window.workspace = original
    window.view_layer = original_layer
    for obj in original_layer.objects:
        obj.select_set(obj.name in session['original_selection'], view_layer=original_layer)
    active = original_layer.objects.get(session.get('original_active') or '')
    original_layer.objects.active = active
    restored_mode = 'OBJECT'
    if active and session['original_mode'] != 'OBJECT':
        with bpy.context.temp_override(window=window, view_layer=original_layer,
                                       active_object=active, object=active):
            bpy.ops.object.mode_set(mode=session['original_mode'])
        restored_mode = active.mode
    return {'workspace': original.name, 'view_layer': original_layer.name,
            'mode': restored_mode, 'restored': True}


def _matches(marker, preset):
    endpoints = marker.get('endpoints', ())
    names = [item[0] for item in endpoints]
    if preset == 'ALL':
        return True
    if preset == 'BODY':
        return marker['domain'] == 'body'
    if preset == 'FACE':
        return marker['domain'] == 'face'
    if preset in {'EYES', 'NOSE', 'MOUTH', 'CHIN'}:
        words = {'EYES': ('眼睑', '眉毛'), 'NOSE': ('鼻部',),
                 'MOUTH': ('嘴唇',), 'CHIN': ('下颚', '下巴')}[preset]
        return any(word in name for name in names for word in words)
    if preset.startswith(('HAND_', 'FINGER_')):
        side = '.' + preset[-1]
        return marker['domain'] == 'hands' and any(
            name.endswith(side) and (preset.startswith('HAND_') or '指' in name)
            for name in names)
    expected = {
        'SHOULDER': (('形变-肩膀', None), ('大臂FK', 'H')),
        'ELBOW': (('大臂FK', 'T'), ('小臂FK', 'H')),
        'WRIST': (('小臂FK', 'T'), ('形变-手掌', 'H')),
        'HIP': (('大腿FK', 'H'),),
        'KNEE': (('大腿FK', 'T'), ('小腿FK', 'H')),
        'ANKLE': (('小腿FK', 'T'), ('形变-脚掌', 'H')),
    }.get(preset, ())
    return any(name.startswith(prefix + '.') and (end is None or endpoint == end)
               for name, endpoint in endpoints for prefix, end in expected)


def _selected_points(context, state, rig):
    points, names, marker_keys = [], set(), set()
    if context.object == rig and rig.mode == 'EDIT':
        for bone in rig.data.edit_bones:
            if bone.select_head:
                points.append(rig.matrix_world @ bone.head)
                names.add(bone.name)
            if bone.select_tail:
                points.append(rig.matrix_world @ bone.tail)
                names.add(bone.name)
    elif context.object == rig and rig.mode == 'POSE':
        for bone in rig.pose.bones:
            if bone.select:
                points.extend((rig.matrix_world @ bone.head, rig.matrix_world @ bone.tail))
                names.add(bone.name)
    for marker in state['markers']:
        obj = context.scene.objects.get(marker['object'])
        if obj and obj.select_get(view_layer=context.window.view_layer):
            points.append(obj.matrix_world.translation.copy())
            names.update(ep[0] for ep in marker['endpoints'])
            marker_keys.add(marker['key'])
    if not points:
        raise ValueError('请先选中此角色的标记或参考骨端点')
    marker_keys.update(m['key'] for m in state['markers']
                       if any(ep[0] in names for ep in m['endpoints']))
    return points, names, marker_keys


def _frame_data(context, state, rig, target, preset):
    if preset == 'SELECTED':
        return _selected_points(context, state, rig)
    markers = [m for m in state['markers'] if _matches(m, preset)]
    names = {ep[0] for m in markers for ep in m['endpoints']}
    if preset == 'ALL':
        points = [target.matrix_world @ Vector(p) for p in target.bound_box]
    else:
        points = []
        for marker in markers:
            for name, end in marker['endpoints']:
                if rig.mode == 'EDIT' and name in rig.data.edit_bones:
                    b = rig.data.edit_bones[name]
                    points.append(rig.matrix_world @ (b.head if end == 'H' else b.tail))
                elif name in rig.data.bones:
                    b = rig.data.bones[name]
                    points.append(rig.matrix_world @ (b.head_local if end == 'H' else b.tail_local))
        if not points:
            raise ValueError('当前参考层没有该部位的有效定位点')
    return points, names, {m['key'] for m in markers}


def _apply_isolation(context, state, rig, session, names, marker_keys):
    isolated = session.get('isolate', False)
    baseline = session['bones']
    for bone in rig.data.bones:
        default = baseline['bone_hide'].get(bone.name, bool(bone.hide))
        bone.hide = bone.name not in names if isolated else default
    for bone in rig.pose.bones:
        default = baseline['pose_hide'].get(bone.name, bool(bone.hide))
        bone.hide = bone.name not in names if isolated else default
    if rig.mode == 'EDIT':
        for bone in rig.data.edit_bones:
            bone.hide = (bone.name not in names if isolated
                         else baseline['bone_hide'].get(bone.name, False))
    for collection in rig.data.collections_all:
        collection.is_visible = True if isolated else baseline['collections'].get(collection.name, True)
    for marker in state['markers']:
        obj = context.scene.objects.get(marker['object'])
        if obj:
            obj.hide_set(isolated and marker['key'] not in marker_keys, view_layer=context.window.view_layer)


def focus(context, preset='FACE'):
    session, state, rig, target = _session(context)
    if preset not in _CODES:
        raise ValueError('未知检查部位')
    if session['bones']['rig'] != rig.name:
        _restore_bones(context.scene, session['bones'])
        session['bones'] = _bone_snapshot(rig)
    points, names, marker_keys = _frame_data(context, state, rig, target, preset)
    low = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    high = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    center = (low + high) * .5
    height = max((target.matrix_world @ Vector(p)).z for p in target.bound_box) - min(
        (target.matrix_world @ Vector(p)).z for p in target.bound_box)
    span = max(max(high - low), height * .025, .001)
    front = Quaternion((1, 0, 0), math.pi / 2)
    rotations = [front, Quaternion((0, 0, 1), math.pi / 2) @ front,
                 Quaternion((0, 0, 1), .65) @ Quaternion((1, 0, 0), -.12) @ front]
    areas = _ensure_views(context)
    for i, area in enumerate(areas):
        space = area.spaces.active
        view = space.region_3d
        view.view_perspective = 'ORTHO'
        view.view_rotation = rotations[min(i, 2)]
        view.view_location = center
        # Account for a narrow viewport/sidebar, so the whole preset fits.
        region = next(r for r in area.regions if r.type == 'WINDOW')
        aspect = max(region.width / max(region.height, 1), .25)
        view.view_distance = span * 1.65 * max(1.0, 1.0 / aspect)
        space.clip_start = max(height * 1e-5, .00001)
        space.clip_end = max(height * 20, 10)
        area.tag_redraw()
    session['preset'] = preset
    session['focus'] = {'center': list(center), 'span': span,
                        'bones': sorted(names), 'markers': sorted(marker_keys)}
    _apply_isolation(context, state, rig, session, names, marker_keys)
    _store(context.window.workspace, session)
    return {'workspace': context.window.workspace.name, 'views': len(areas), 'preset': preset,
            'center': list(center), 'span': span, 'isolated': session['isolate']}


def set_isolate(context, enabled):
    session, state, rig, target = _session(context)
    session['isolate'] = bool(enabled)
    _store(context.window.workspace, session)
    return focus(context, session.get('preset', 'FACE'))


def set_xray(context, enabled):
    session, state, rig, target = _session(context)
    rig.show_in_front = bool(enabled)
    for marker in state['markers']:
        obj = context.scene.objects.get(marker['object'])
        if obj:
            obj.show_in_front = bool(enabled)
    session['xray'] = bool(enabled)
    _store(context.window.workspace, session)
    for area in _view_areas(context.window):
        area.tag_redraw()
    return {'xray': bool(enabled)}
