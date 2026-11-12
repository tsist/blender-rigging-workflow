# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent, display-only face inspection in a newly owned ViewLayer.

The user's layer is never isolated in place. Object references live on Scene,
not in RAM; no Mesh/Key pointers, input values or animation are saved/restored.
"""
import json
import math
import uuid

import bpy
from mathutils import Quaternion, Vector

from . import face_expression_controls as core, face_modes as fm
from . import face_graph_board as graph, face_mouth_pad as pad, control_panel

STATE_KEY = 'c94_face_focus_v1'
LAYERS_KEY = 'c94_face_focus_layers_v1'
SCHEMA = 'c94.face-focus.v1'
_OVERLAY = {'show_overlays': True, 'show_bones': True, 'show_extras': False,
            'show_relationship_lines': False, 'show_floor': False,
            'show_axis_x': False, 'show_axis_y': False, 'show_axis_z': False}
_SHADING = {'type': 'SOLID', 'color_type': 'OBJECT'}


def _require(value, message):
    if not value:
        raise ValueError(message)


def _store(scene, session):
    scene[STATE_KEY] = json.dumps(session, ensure_ascii=False, allow_nan=False)


def _pointer_key(session, role):
    return '_c94_ff_' + session['owner'] + '_' + role


def _pointer(scene, session, role):
    value = scene.get(_pointer_key(session, role))
    return value if isinstance(value, bpy.types.Object) else None


def _read(scene):
    raw = scene.get(STATE_KEY)
    if raw is None:
        return None
    try:
        record = json.loads(raw)
        _require(isinstance(record, dict) and record.get('schema') == SCHEMA
                 and type(record.get('active')) is bool
                 and isinstance(record.get('owner'), str) and len(record['owner']) == 32
                 and all(c in '0123456789abcdef' for c in record['owner']),
                 '面部检查恢复记录无效；保留记录检查')
        if record['active']:
            _require(isinstance(record.get('selection'), list)
                     and all(isinstance(n, str) for n in record['selection'])
                     and record['mode'] in {'OBJECT', 'POSE'}, '面部检查选择/模式记录无效')
        return record
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError('面部检查恢复记录损坏；保留记录检查') from exc


def _registry(scene):
    raw = scene.get(LAYERS_KEY, '{}')
    try:
        records = json.loads(raw)
        _require(isinstance(records, dict), '面部检查视图层所有权记录无效')
        return records
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError('面部检查视图层所有权记录损坏') from exc


def _layer(scene, session):
    name = session['focus_layer']
    marker = _registry(scene).get(name)
    _require(isinstance(marker, dict) and marker.get('owner') == session['owner']
             and marker.get('schema') == SCHEMA and marker.get('active') is True
             and name == '面部检查_' + session['owner'], '专用视图层所有权不匹配；不抢占同名层')
    layer = scene.view_layers.get(name)
    _require(layer is not None, '专用检查视图层已删除；保留恢复记录')
    return layer


def _view(context):
    _require(context.window is not None and context.window.scene == context.scene
             and context.area is not None and context.area.type == 'VIEW_3D'
             and context.mode in {'OBJECT', 'POSE'}, '请在原生3D视图的对象/姿态模式使用面部检查')
    space = context.area.spaces.active
    _require(space.type == 'VIEW_3D' and space.local_view is None
             and space.region_3d is not None and not space.region_quadviews,
             '请先离开局部视图/四视图，再进入面部检查')
    region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
    _require(region is not None, '找不到真实3D主区域')
    return context.window, context.area, region, space


def _original_view(context, session):
    # A missing/replaced screen must be resolved before changing any global hide.
    candidates = [w for w in context.window_manager.windows
                  if w.scene == context.scene and w.screen.name == session['screen']]
    _require(candidates, '原检查视窗未显示；请切回原Screen后再离开，恢复记录已保留')
    window = next((w for w in candidates if w == context.window), candidates[0])
    index = session['area_index']
    _require(type(index) is int and 0 <= index < len(window.screen.areas), '原3D区域索引已不存在')
    area = window.screen.areas[index]
    _require(area.type == 'VIEW_3D' and area.spaces.active.local_view is None
             and area.spaces.active.region_3d is not None and not area.spaces.active.region_quadviews,
             '原3D区域已改变或进入局部/四视图；请先恢复原区域')
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    _require(region is not None, '原3D主区域已不存在')
    return window, area, region, area.spaces.active


def _viewport_snapshot(space):
    view = space.region_3d
    return {'location': list(view.view_location), 'distance': float(view.view_distance),
            'rotation': list(view.view_rotation), 'perspective': view.view_perspective,
            'shading': {k: getattr(space.shading, k) for k in _SHADING},
            'overlay': {k: getattr(space.overlay, k) for k in _OVERLAY},
            'clip_start': float(space.clip_start), 'clip_end': float(space.clip_end)}


def _bone_identity(bone):
    # Rest identity protects same-named replacements without retaining data IDs.
    return {'parent': bone.parent.name if bone.parent else None,
            'matrix': [list(row) for row in bone.matrix_local], 'length': float(bone.length)}


def _bone_snapshot(rig):
    return {'identity': {b.name: _bone_identity(b) for b in rig.data.bones},
            'bone_hide': {b.name: bool(b.hide) for b in rig.data.bones},
            'pose_hide': {b.name: bool(b.hide) for b in rig.pose.bones},
            'pose_select': {b.name: bool(b.select) for b in rig.pose.bones},
            'active_bone': rig.data.bones.active.name if rig.data.bones.active else None,
            'collections': {c.name: bool(c.is_visible) for c in rig.data.collections_all},
            'collection_solo': {c.name: bool(c.is_solo) for c in rig.data.collections_all},
            'collection_parents': {c.name: c.parent.name if c.parent else None for c in rig.data.collections_all},
            'show_in_front': bool(rig.show_in_front), 'show_names': bool(rig.data.show_names)}


def _rig(scene, session):
    rig = _pointer(scene, session, 'rig')
    _require(rig is not None and rig.type == 'ARMATURE' and rig.name in scene.objects,
             '源骨架已删除或离开Scene；仍可用leave恢复原视图')
    _require(rig.data.users == 1 and rig.data.is_editable and not rig.data.library
             and not rig.data.override_library and rig.data.name == session['rig_data'],
             '源骨架数据已共享/替换；不能改变全局骨显隐')
    return rig


def _source(scene, session):
    rig = _rig(scene, session)
    hub = _pointer(scene, session, 'hub')
    _, current_rig, members = core._read(hub)
    _require(current_rig == rig, '检查源控制对象改绑其它骨架，拒绝切换检查空间')
    return hub, rig, members


def _plan(hub, rig, space, secondary):
    _require(space in {'KEY', 'BONE'} and type(secondary) is bool, '检查空间须为KEY/BONE，次要层须为布尔值')
    display = None
    if space == 'KEY':
        kind = core._control_type(hub)
        if kind == 'MOUTH':
            _, board, _, _ = pad._read(hub)
            names = {board['root'], board['anchor'], board['pad']}
            names.update(n for r in board['sliders'] for n in (r['anchor'], r['bone']))
        elif kind == 'GRAPH':
            _, board, _, _ = graph._read(hub)
            names = {board['root']}
            names.update(n for r in board['channels'] for n in (r['anchor'], r['bone']))
        else:
            raise ValueError('此hub尚无原生图形板；可使用BONE检查或先建立图形板')
        display = hub.get('board_display')
        _require(isinstance(display, bpy.types.Collection), '当前hub的图形显示集合缺失')
    else:
        groups = [rig.data.collections_all.get(fm.SPACES[k])
                  for k in (('basic', 'main', 'secondary') if secondary else ('basic', 'main'))]
        _require(all(c is not None and len(c.bones) for c in groups), '缺少完整物理面部控骨层')
        names = {b.name for c in groups for b in c.bones}
    _require(names and all(n in rig.pose.bones for n in names), '所选面部控骨缺失')
    curves = [o for o in display.all_objects if o.type in {'CURVE', 'FONT'}] if display else []
    return names, curves


def _global_visible(scene, objects):
    # Global flags cannot be overridden in just one ViewLayer; do not change them.
    for obj in objects:
        _require(obj.name in scene.objects and not obj.hide_viewport,
                 '检查对象被全局禁用或不在当前Scene：' + obj.name)
        def paths(collection, blocked=False):
            blocked = blocked or collection.hide_viewport
            found = obj.name in collection.objects and not blocked
            return found or any(paths(c, blocked) for c in collection.children)
        _require(paths(scene.collection), '检查对象的集合路径被全局禁用：' + obj.name)


def _display_static(rig):
    fm._static(rig, {'show_in_front', *(p.path_from_id('hide') for p in rig.pose.bones)})
    fm._static(rig.data, {'show_names', *(b.path_from_id('hide') for b in rig.data.bones),
                          *(c.path_from_id(f) for c in rig.data.collections_all for f in ('is_visible', 'is_solo'))})


def _apply_bones(rig, names):
    for bone in rig.data.bones:
        bone.hide = bone.name not in names
    for pb in rig.pose.bones:
        pb.hide = pb.name not in names
        pb.select = False
    collections = set()
    for name in names:
        for collection in rig.data.bones[name].collections:
            while collection:
                collections.add(collection.name)
                collection = collection.parent
    for collection in rig.data.collections_all:
        collection.is_solo = False
        collection.is_visible = collection.name in collections
    rig.show_in_front = True
    rig.data.show_names = False
    selectable = [n for n in sorted(names) if not rig.data.bones[n].hide_select]
    _require(selectable, '当前空间没有可选择的控骨')
    rig.pose.bones[selectable[0]].select = True
    rig.data.bones.active = rig.data.bones[selectable[0]]


def _isolate(layer, objects):
    def include(collection):
        collection.exclude = False
        collection.hide_viewport = False
        for child in collection.children:
            include(child)
    include(layer.layer_collection)
    layer.update()
    allowed = set(objects)
    for obj in layer.objects:
        obj.hide_set(obj not in allowed, view_layer=layer)
    layer.update()


def _frame(context, area, region, space, members, rig, names, curves, preset):
    depsgraph = context.evaluated_depsgraph_get()
    points = []
    for obj in [*members, *curves] if preset == 'KEY' else members:
        if obj.type in {'CURVE', 'FONT'}:
            points.extend(graph._display_points(obj, depsgraph))
        else:
            evaluated = obj.evaluated_get(depsgraph)
            points.extend(evaluated.matrix_world @ Vector(p) for p in evaluated.bound_box)
    if preset == 'KEY':
        evaluated = rig.evaluated_get(depsgraph)
        points.extend(evaluated.matrix_world @ p for n in names
                      for p in (evaluated.pose.bones[n].head, evaluated.pose.bones[n].tail))
    _require(points and all(math.isfinite(v) for p in points for v in p), '面部/板的世界包围盒不是有限数')
    low = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    high = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    span = max(max(high - low), .001)
    view = space.region_3d
    view.view_perspective = 'ORTHO'
    view.view_rotation = Quaternion((1, 0, 0), math.pi / 2)
    view.view_location = (low + high) * .5
    aspect = max(region.width / max(region.height, 1), .25)
    view.view_distance = span * 1.65 * max(1., 1. / aspect)
    space.clip_start, space.clip_end = max(span * 1e-5, .00001), max(span * 20, 10.)
    for k, v in _SHADING.items(): setattr(space.shading, k, v)
    for k, v in _OVERLAY.items(): setattr(space.overlay, k, v)
    area.tag_redraw()
    return {'center': list(view.view_location), 'span': span, 'bones': sorted(names)}


def _focus(context, session, space, secondary):
    window, area, region, viewport = _view(context)
    layer = _layer(context.scene, session)
    _require(window.view_layer == layer, '请先激活本次专用检查视图层')
    _require(window.screen.name == session['screen']
             and list(window.screen.areas).index(area) == session['area_index'], '请在原检查3D区域切换空间')
    hub, rig, members = _source(context.scene, session)
    _require(set(rig.data.bones.keys()) == set(session['bones']['identity'])
             and all(_bone_identity(b) == session['bones']['identity'][b.name] for b in rig.data.bones),
             '检查期间骨架结构已改变；请先leave恢复仍存在的显示字段')
    names, curves = _plan(hub, rig, space, secondary)
    _global_visible(context.scene, [rig, *members])
    _display_static(rig)
    _isolate(layer, [rig, *members, *curves])
    _apply_bones(rig, names)
    # Board curves have native hide drivers reading panel-collection visibility.
    # Let the owned display change evaluate before testing their global flags.
    layer.update()
    _global_visible(context.scene, curves)
    with context.temp_override(window=window, area=area, region=region, view_layer=layer):
        control_panel._pose(bpy.context, rig)
        frame = _frame(bpy.context, area, region, viewport, members, rig, names, curves, space)
    session.update(space=space, secondary=secondary, frame=frame, errors=[])
    _store(context.scene, session)
    return {'active': True, 'space': space, 'view_layer': layer.name, **frame}


def enter(context, hub, *, space='AUTO', secondary=False):
    window, area, region, viewport = _view(context)
    old = _read(context.scene)
    _require(old is None or not old['active'], '已有活动面部检查；请先leave，不覆盖恢复记录')
    _, rig, members = core._read(hub)
    core._local(rig, 'ARMATURE')
    _require(rig.data.users == 1 and rig.data.is_editable and not rig.data.library
             and not rig.data.override_library and not rig.hide_select,
             '检查要求可选、本地单用户Armature Data')
    _require(space in {'AUTO', 'KEY', 'BONE'}, '检查空间须为AUTO/KEY/BONE')
    target = fm._mode(rig) if space == 'AUTO' else space
    names, curves = _plan(hub, rig, target, secondary)
    _global_visible(context.scene, [rig, *members])
    _display_static(rig)
    owner = uuid.uuid4().hex
    original = window.view_layer
    selection = [o for o in original.objects if o.select_get(view_layer=original)]
    session = {'schema': SCHEMA, 'owner': owner, 'active': True,
               'original_layer': original.name, 'focus_layer': '面部检查_' + owner,
               'rig': rig.name, 'rig_data': rig.data.name, 'hub': hub.name,
               'screen': window.screen.name, 'area_index': list(window.screen.areas).index(area),
               'mode': context.mode, 'selection': [o.name for o in selection],
               'active_object': original.objects.active.name if original.objects.active else None,
               'bones': _bone_snapshot(rig), 'viewport': _viewport_snapshot(viewport),
               'space': target, 'secondary': secondary, 'errors': []}
    _require(session['focus_layer'] not in context.scene.view_layers, '新检查层名称已存在，不覆盖')
    registry = _registry(context.scene)
    # Persist every recovery pointer before the first view/mode mutation.
    context.scene[_pointer_key(session, 'rig')] = rig
    context.scene[_pointer_key(session, 'hub')] = hub
    if original.objects.active: context.scene[_pointer_key(session, 'active')] = original.objects.active
    for i, obj in enumerate(selection): context.scene[_pointer_key(session, 'sel_%04d' % i)] = obj
    _store(context.scene, session)
    try:
        if context.mode == 'POSE':
            with context.temp_override(window=window, area=area, region=region):
                bpy.ops.object.mode_set(mode='OBJECT')
        layer = context.scene.view_layers.new(session['focus_layer'])
        registry[layer.name] = {'schema': SCHEMA, 'owner': owner, 'active': True}
        context.scene[LAYERS_KEY] = json.dumps(registry, ensure_ascii=False)
        window.view_layer = layer
        with context.temp_override(window=window, area=area, region=region, view_layer=layer):
            return _focus(bpy.context, session, target, secondary)
    except Exception as error:
        # leave also handles a failure before the owned layer was constructed.
        try:
            leave(context)
        except Exception as recovery:
            session['errors'] = [str(error), '进入失败后的恢复未完成：' + str(recovery)]
            _store(context.scene, session)
            raise RuntimeError('; '.join(session['errors'])) from error
        raise


def set_space(context, *, space='KEY', secondary=False):
    session = _read(context.scene)
    _require(session is not None and session['active'], '没有活动面部检查')
    window, area, region, _ = _original_view(context, session)
    _require(context.window == window and context.area == area and window.scene == context.scene,
             '请在原检查Scene/Screen/3D区域显式重新激活检查层')
    layer = _layer(context.scene, session)
    before_layer = window.view_layer
    # Native Undo/Redo restores Scene state but need not restore Window's layer.
    # Only this explicit action may reactivate the validated owned ViewLayer.
    try:
        if window.view_layer != layer:
            window.view_layer = layer
        with context.temp_override(window=window, area=area, region=region, view_layer=layer):
            return _focus(bpy.context, session, space, secondary)
    except Exception as error:
        errors = [str(error)]
        try:
            if window.view_layer != before_layer:
                window.view_layer = before_layer
        except Exception as recovery:
            errors.append('恢复进入前Window视图层失败：' + str(recovery))
        session['errors'] = errors
        _store(context.scene, session)
        if len(errors) > 1:
            raise RuntimeError('; '.join(errors)) from error
        raise


def leave(context):
    session = _read(context.scene)
    _require(session is not None and session['active'], '没有活动面部检查')
    original = context.scene.view_layers.get(session['original_layer'])
    _require(original is not None, '原视图层已不存在；不改全局显隐，恢复记录已保留')
    window, area, region, viewport = _original_view(context, session)
    _require(window.scene == context.scene, '原窗口Scene已改变，恢复记录保留')
    _require(context.mode in {'OBJECT', 'POSE'}, '请先退出编辑/绘制模式再离开检查')
    # If the owned layer exists, verify its ownership before touching it. An
    # entry failure before layer creation has no isolation state to undo.
    if context.scene.view_layers.get(session['focus_layer']) is not None:
        _layer(context.scene, session)
    errors, missing = [], []
    def attempt(label, operation):
        try: operation()
        except Exception as error: errors.append(label + ': ' + str(error))
    with context.temp_override(window=window, area=area, region=region):
        if bpy.context.object and bpy.context.object.mode == 'POSE':
            attempt('退出本次姿态模式', lambda: bpy.ops.object.mode_set(mode='OBJECT'))
        attempt('恢复原视图层', lambda: setattr(window, 'view_layer', original))
    rig = _pointer(context.scene, session, 'rig')
    bones = session['bones']
    if rig is None or rig.type != 'ARMATURE' or rig.data.name != session['rig_data']:
        missing.append('源rig或Armature Data身份已改变；未恢复骨显示')
    elif rig.data.users != 1 or not rig.data.is_editable or rig.data.library or rig.data.override_library:
        errors.append('Armature Data已共享/只读；未恢复骨显示，避免影响其他角色')
    else:
        existing = {n for n, identity in bones['identity'].items()
                    if n in rig.data.bones and _bone_identity(rig.data.bones[n]) == identity}
        missing.extend('bone:' + n for n in bones['identity'] if n not in existing)
        for field, owner in [('bone_hide', rig.data.bones), ('pose_hide', rig.pose.bones), ('pose_select', rig.pose.bones)]:
            prop = 'select' if field == 'pose_select' else 'hide'
            for name, value in bones[field].items():
                if name in existing:
                    attempt(field + ':' + name, lambda name=name, value=value, owner=owner, prop=prop: setattr(owner[name], prop, value))
        for name, value in bones['collections'].items():
            collection = rig.data.collections_all.get(name)
            if collection is None or (collection.parent.name if collection.parent else None) != bones['collection_parents'][name]:
                missing.append('collection:' + name)
            else:
                attempt('collection solo:' + name, lambda collection=collection, name=name:
                        setattr(collection, 'is_solo', bones['collection_solo'][name]))
                attempt('collection:' + name, lambda collection=collection, value=value: setattr(collection, 'is_visible', value))
        attempt('show_in_front', lambda: setattr(rig, 'show_in_front', bones['show_in_front']))
        attempt('show_names', lambda: setattr(rig.data, 'show_names', bones['show_names']))
        active_bone = bones['active_bone']
        if active_bone is None or active_bone in existing:
            attempt('active bone', lambda: setattr(rig.data.bones, 'active', rig.data.bones.get(active_bone) if active_bone else None))
    saved = session['viewport']
    for field, key in [('view_location', 'location'), ('view_distance', 'distance'),
                       ('view_rotation', 'rotation'), ('view_perspective', 'perspective')]:
        attempt('viewport:' + field, lambda field=field, key=key: setattr(viewport.region_3d, field, saved[key]))
    for field in _SHADING:
        attempt('shading:' + field, lambda field=field: setattr(viewport.shading, field, saved['shading'][field]))
    for field in _OVERLAY:
        attempt('overlay:' + field, lambda field=field: setattr(viewport.overlay, field, saved['overlay'][field]))
    for field in ('clip_start', 'clip_end'):
        attempt(field, lambda field=field: setattr(viewport, field, saved[field]))
    if window.view_layer == original:
        selected = set()
        for i, name in enumerate(session['selection']):
            obj = _pointer(context.scene, session, 'sel_%04d' % i)
            if obj is not None and obj.name in original.objects: selected.add(obj)
            else: missing.append('selected object:' + name)
        for obj in original.objects:
            attempt('object selection:' + obj.name, lambda obj=obj: obj.select_set(obj in selected, view_layer=original))
        active = _pointer(context.scene, session, 'active')
        if active is None or active.name in original.objects:
            attempt('active object', lambda: setattr(original.objects, 'active', active))
        else:
            missing.append('active object:' + session['active_object'])
        if session['mode'] == 'POSE':
            if active is None or active.type != 'ARMATURE' or active.name not in original.objects:
                missing.append('原POSE活动骨架不可用')
            else:
                with context.temp_override(window=window, area=area, region=region, view_layer=original):
                    attempt('恢复原POSE模式', lambda: bpy.ops.object.mode_set(mode='POSE'))
    else:
        errors.append('原视图层未恢复；未尝试改变其它层选择')
    attempt('更新原视图层', original.update)
    area.tag_redraw()
    session['errors'], session['missing'] = errors, missing
    if errors:
        _store(context.scene, session)
        raise RuntimeError('面部检查恢复未完整完成，记录保留可重试：' + '; '.join(errors))
    session['active'] = False
    _store(context.scene, session)
    registry = _registry(context.scene)
    if session['focus_layer'] in registry:
        registry[session['focus_layer']]['active'] = False
        context.scene[LAYERS_KEY] = json.dumps(registry, ensure_ascii=False)
    for role in ['rig', 'hub', 'active', *('sel_%04d' % i for i in range(len(session['selection'])))]:
        key = _pointer_key(session, role)
        if key in context.scene: del context.scene[key]
    return {'active': False, 'restored': not missing, 'missing': missing,
            'view_layer': original.name, 'focus_layer_retained': session['focus_layer'], 'inputs_restored': False}


def status(context):
    try:
        session = _read(context.scene)
        if session is None or not session['active']:
            return {'active': False, 'layer_active': False, 'missing': session.get('missing', []) if session else []}
        layer = _layer(context.scene, session)
        rig = _pointer(context.scene, session, 'rig')
        return {'active': True, 'space': session['space'], 'secondary': session['secondary'],
                'layer_active': bool(context.window and context.window.scene == context.scene and context.window.view_layer == layer),
                'view_layer': layer.name, 'current_layer': context.window.view_layer.name if context.window else None,
                'rig': rig.name if rig else None, 'hub': session['hub'], 'errors': session.get('errors', [])}
    except (ValueError, KeyError, TypeError, ReferenceError) as error:
        return {'active': bool(context.scene.get(STATE_KEY)), 'layer_active': False, 'error': str(error)}
