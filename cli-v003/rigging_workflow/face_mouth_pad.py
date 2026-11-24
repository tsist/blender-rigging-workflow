# SPDX-License-Identifier: GPL-3.0-or-later
"""Authoring 2D mouth-pad creation and fitting over the shared daily core.

Daily read/axis/slider/record are re-exported from face_mouth_core. Creation
and graphics stay here so the portable Text never embeds board construction.
"""
import json
import math
import uuid

import bpy
from mathutils import Vector

from . import face_expression_core as controls, face_mouth_core as core
from . import face_graph_board as graph, face_modes as fm, mode_animation as anim

STATE_KEY = core.STATE_KEY
SCHEMA = core.SCHEMA
_require = core._require
_path = core._path
_read = core._read
inspect = core.inspect
set_axes = core.set_axes
set_axis = core.set_axis
set_values = core.set_values
set_layout = core.set_layout
switch = core.switch
record_current = core.record_current
_no_target_animation = core._no_target_animation
_input_rows = core._input_rows
_row = core._row
_refresh_restored = core._refresh_restored
_SLIDER_COLUMNS = core._SLIDER_COLUMNS
_SLIDER_ROW = core._SLIDER_ROW


def _driver(hub, prop, rig, expression, variables):
    curve = hub.driver_add(controls._path(prop))
    curve.keyframe_points.clear()
    for modifier in list(curve.modifiers):
        curve.modifiers.remove(modifier)
    driver = curve.driver
    driver.type = 'SCRIPTED'
    driver.use_self = False
    driver.expression = expression
    for variable in list(driver.variables):
        driver.variables.remove(variable)
    for name, path in variables:
        variable = driver.variables.new()
        variable.name = name
        variable.type = 'SINGLE_PROP'
        target = variable.targets[0]
        target.id_type = 'OBJECT'
        target.id = rig
        target.data_path = path


def _limit(pb, travel, *, pad):
    pb.lock_location = (not pad, False, True)
    pb.lock_rotation = (True, True, True)
    pb.lock_scale = (True, True, True)
    constraint = pb.constraints.new('LIMIT_LOCATION')
    constraint.owner_space = 'LOCAL'
    for axis in 'xyz':
        setattr(constraint, 'use_min_' + axis, True)
        setattr(constraint, 'use_max_' + axis, True)
    constraint.min_x = -travel if pad else 0
    constraint.max_x = travel if pad else 0
    constraint.min_y = 0
    constraint.max_y = travel


def _graphics(rig, record, display, objects, token, font):
    travel = record['travel']
    root, anchor, pad = record['root'], record['anchor'], record['pad']
    prefix = '口型_' + token + '_'
    maximum = travel + .035
    if record['sliders']:
        maximum = max(maximum, travel + .075 + (min(_SLIDER_COLUMNS,len(record['sliders'])) - 1) * .09 + .08)
    rows = max(1, math.ceil(len(record['sliders']) / _SLIDER_COLUMNS))
    verts, edges = graph._rect(-travel - .035, -.035-(rows-1)*_SLIDER_ROW, maximum, travel + .065)
    frame = graph._shape(prefix + '框', verts, edges, rig, root, display, objects)
    frame['c94_face_frame_v1'] = True
    rig.pose.bones[root].custom_shape['c94_face_frame_v1'] = True
    verts, edges = [], []
    for x in (-travel, 0, travel):
        index = len(verts); verts += [(x, 0, 0), (x, travel, 0)]; edges.append((index, index + 1))
    for y in (0, travel / 2, travel):
        index = len(verts); verts += [(-travel, y, 0), (travel, y, 0)]; edges.append((index, index + 1))
    graph._shape(prefix + '网格', verts, edges, rig, anchor, display, objects, color=(.35, .45, .6, 1))
    circle = [(.006 * math.cos(i * math.tau / 32), .006 * math.sin(i * math.tau / 32), .001) for i in range(32)]
    graph._shape(prefix + '二维手柄', circle, [(i, (i + 1) % 32) for i in range(32)], rig, pad, display, objects,
                 color=(.25, .9, .65, 1))
    for index, row in enumerate([dict(name='Basis', x=0, y=0), *record['grid']]):
        label = graph._shape(prefix + '格点%02d' % index, None, None, rig, anchor, display, objects,
                             font=font, text=row['name'].replace('元音-','').replace('Basis','中立'), color=(.9, .9, .95, 1))
        label.location = (row['x'] * travel - .012, row['y'] * travel / 2 + .007, .002)
        label.data.size = .012
    for index, row in enumerate(record['sliders']):
        verts, edges = graph._rect(-.004, 0, .004, travel)
        graph._shape(prefix + '轨道%02d' % index, verts, edges, rig, row['anchor'], display, objects)
        verts, edges = graph._rect(-.01, -.005, .01, .005)
        graph._shape(prefix + '滑杆%02d' % index, verts, edges, rig, row['bone'], display, objects,
                     color=(.25, .8, .65, 1))
        label = graph._shape(prefix + '标签%02d' % index, None, None, rig, row['anchor'], display, objects,
                             font=font, text=row['name'])
        label.location = (-.025, -.02, .002)


def create(hub, grid, *, travel=.06, font=None, preserve_disjoint_nla=False):
    group, rig, members = controls._read(hub)
    controls._baseline(rig, group['neutral_fingerprint'])
    _require(STATE_KEY not in hub and graph.STATE_KEY not in hub and 'board_display' not in hub,
             'hub已有图形板/口型板或未知显示引用，不能接管')
    _require(rig.data.users == 1 and rig.data.is_editable and not rig.data.library and not rig.data.override_library,
             '同rig的Armature Data须本地单用户')
    _require(type(travel) in (int, float) and math.isfinite(travel) and travel > 0, '行程必须有限且为正')
    _require('表情面板' in rig.data.bones and fm.SPACES['panel'] in rig.data.collections_all, '缺少原表情面板父根/集合')
    travel = float(travel)
    grid_rows = core._grid(group['channels'], grid)
    core._grid_basis(group, grid_rows, members)
    grid_names = {r['name'] for r in grid_rows}
    remaining = [r for r in group['channels'] if r['name'] not in grid_names]
    # Refuse before creating any ID/bone/collection: this shared core edits and
    # records exactly 8 grid cells + 15 sliders, so a 14/16-slider board must
    # never be reported as successfully created.
    core._require_standard_slider_count(len(remaining))
    token = uuid.uuid4().hex[:8]
    record = dict(schema=SCHEMA, root='口型板根_' + token, anchor='口型板布局_' + token,
                  pad='口型板控件_' + token, collection='角色口型板_' + token, travel=travel, grid=grid_rows,
                  sliders=[dict(name=r['name'], property=r['property'], bone='口型滑杆_%02d_' % i + token,
                                anchor='口型布局_%02d_' % i + token)
                           for i, r in enumerate(remaining)])
    names = [record['root'], record['anchor'], record['pad']] + [n for r in record['sliders'] for n in (r['anchor'], r['bone'])]
    _require(all(n not in rig.data.bones for n in names) and record['collection'] not in rig.data.collections_all
             and record['collection'] not in bpy.data.collections, '新板身份冲突')
    old_values = {r['property']: hub[r['property']] for r in group['channels']}
    _require(all(v == 0 for v in old_values.values()), '建立口型板前所有参数须为0')
    _require(type(preserve_disjoint_nla) is bool, '明确只读保留NLA选项')
    future_paths = {
        'pose.bones[' + json.dumps(n, ensure_ascii=False) + '].' + field
        for n in names for field in (*fm._FIELDS, 'rotation_mode')
    }
    binding_h = anim._binding(hub)
    binding_r = anim._binding(rig, read_only_nla_paths=future_paths if preserve_disjoint_nla else None)
    _no_target_animation(hub, {core._path(p) for p in old_values})
    before, face_before = anim._snapshot(rig), fm._snapshot(rig)
    hub_bag = anim._bag(binding_h['action'], binding_h['slot']) if binding_h['action'] else None
    hub_paths = {(f.data_path, f.array_index) for f in hub_bag.fcurves} if hub_bag else set()
    hub_paths.update((field, index) for field in anim._OBJECT_CHANNELS for index in range(len(getattr(hub, field))))
    hub_inputs = {p: anim._animated_value(hub, *p) for p in hub_paths}
    mirror = rig.data.use_mirror_x
    objects, added = [], []
    display = bone_collection = None
    old_ids = {kind: set(getattr(bpy.data, kind)) for kind in ('meshes', 'curves', 'objects')}
    try:
        bone_collection = rig.data.collections.new(record['collection'], parent=rig.data.collections_all[fm.SPACES['panel']])
        rig.data.use_mirror_x = False
        with graph._edit(rig) as bones:
            origin = bones['表情面板'].head.copy() + Vector((.27, 0, -.36))
            def bone(name, parent, position):
                b = bones.new(name); b.head = position; b.tail = position + Vector((0, 0, .015))
                b.parent = bones[parent]; b.use_connect = False; b.use_deform = False
                bone_collection.assign(b)
                return b
            bone(record['root'], '表情面板', origin)
            bone(record['anchor'], record['root'], origin)
            bone(record['pad'], record['anchor'], origin)
            for index, row in enumerate(record['sliders']):
                position = origin + Vector((travel + .075 + (index % _SLIDER_COLUMNS) * .09, 0,
                                            -(index // _SLIDER_COLUMNS) * _SLIDER_ROW))
                bone(row['anchor'], record['root'], position)
                bone(row['bone'], row['anchor'], position)
        rig.data.use_mirror_x = mirror
        for name in names:
            b = rig.data.bones[name]
            for collection in list(b.collections):
                if collection != bone_collection:
                    collection.unassign(b)
        _limit(rig.pose.bones[record['pad']], travel, pad=True)
        rig.data.bones[record['anchor']].hide_select = True
        for row in record['sliders']:
            _limit(rig.pose.bones[row['bone']], travel, pad=False)
            rig.data.bones[row['anchor']].hide_select = True
        for row in grid_rows:
            added.append(core._path(row['property']))
            _driver(hub, row['property'], rig, core._expression(row, travel),
                    [('x', _path(record['pad']) + '[0]'), ('y', _path(record['pad']) + '[1]')])
        for row in record['sliders']:
            added.append(core._path(row['property']))
            _driver(hub, row['property'], rig, core._slider_expression(travel), [('v', _path(row['bone']) + '[1]')])
        display = bpy.data.collections.new(record['collection'])
        bpy.context.scene.collection.children.link(display)
        _graphics(rig, record, display, objects, token, font)
        hub['board_display'] = display
        hub[STATE_KEY] = json.dumps(record, ensure_ascii=False, allow_nan=False)
        controls._update(hub, rig)
        core._read(hub)
        anim._check_inputs(rig, before, set())
        _require(all(anim._animated_value(hub, *p) == value for p, value in hub_inputs.items()),
                 '创建覆盖了非目标hub输入')
        _require(fm._snapshot(rig) == face_before and all(abs(hub[p] - v) < 1e-7 for p, v in old_values.items()),
                 '创建改变了原面部输入或参数')
        return core.inspect(hub)
    except Exception as error:
        recovery_errors = []
        def attempt(label, operation):
            try: operation()
            except Exception as recovery: recovery_errors.append(label + ': ' + str(recovery))
        for path in added:
            attempt('remove hub driver ' + path, lambda path=path: hub.driver_remove(path))
        attempt('restore hub binding', lambda: anim._restore_binding(hub, binding_h))
        attempt('restore rig binding', lambda: anim._restore_binding(rig, binding_r))
        attempt('flush restored binding', lambda: controls._update(hub, rig))
        for key in (STATE_KEY, 'board_display'):
            def clear(key=key):
                if key in hub: del hub[key]
            attempt('clear owned metadata ' + key, clear)
        for prop, value in old_values.items():
            attempt('restore parameter ' + prop, lambda prop=prop, value=value: hub.__setitem__(prop, value))
        for obj in reversed(objects):
            attempt('remove display object', lambda obj=obj: graph._destroy([obj]))
        if display is not None:
            attempt('remove display collection', lambda: bpy.data.collections.remove(display))
        def remove_bones():
            with graph._edit(rig) as bones:
                for name in reversed(names):
                    if name in bones:
                        attempt('remove owned bone ' + name, lambda name=name: bones.remove(bones[name]))
        attempt('remove owned bones', remove_bones)
        if bone_collection is not None:
            attempt('remove bone collection', lambda: rig.data.collections.remove(bone_collection))
        prefix = '口型_' + token + '_'
        for kind in ('objects', 'meshes', 'curves'):
            for item in set(getattr(bpy.data, kind)) - old_ids[kind]:
                if item.name.startswith(prefix) and (kind == 'objects' or item.users == 0):
                    attempt('remove owned orphan ' + kind,
                            lambda kind=kind, item=item: getattr(bpy.data, kind).remove(item, do_unlink=True)
                            if kind == 'objects' else getattr(bpy.data, kind).remove(item))
        attempt('restore rig inputs', lambda: _restore_snapshot(rig, before))
        for (path, index), value in hub_inputs.items():
            attempt('restore hub input ' + path,
                    lambda path=path, index=index, value=value: anim._restore_value(hub, path, index, value))
        attempt('refresh restored inputs without TIME', lambda: _refresh_restored(hub, rig))
        def check_recovered():
            _require(anim._snapshot(rig) == before and fm._snapshot(rig) == face_before,
                     '创建失败后的rig输入恢复不一致')
            _require(all(anim._animated_value(hub, *p) == v for p, v in hub_inputs.items())
                     and all(hub[p] == v for p, v in old_values.items()), '创建失败后的hub输入恢复不一致')
            _require(not any(n in rig.data.bones for n in names) and STATE_KEY not in hub and 'board_display' not in hub,
                     '创建失败后仍有本次骨/记录残留')
        attempt('verify restored inputs and ownership', check_recovered)
        if recovery_errors:
            raise RuntimeError(str(error) + '; 创建恢复未完整核实：' + '; '.join(recovery_errors)) from error
        raise
    finally:
        rig.data.use_mirror_x = mirror


def _restore_snapshot(rig, before):
    for n,v in before['bones'].items():
        pb=rig.pose.bones[n]
        for f in anim.pose._CHANNELS:setattr(pb,f,v[f])
        pb.rotation_mode=v['rotation_mode']
    for (n,k),v in before['modes'].items():rig.pose.bones[n][k]=v
    for f,v in before['object'].items():setattr(rig,f,v)
    for (p,i),v in before['animated'].items():anim._restore_value(rig,p,i,v)


def fit_frame(hub, *, padding=.025):
    """Fit the owned rectangular frame to edited anchors and native labels."""
    group, record, rig, _ = core._read(hub)
    _require(type(padding) in (int,float) and math.isfinite(padding) and padding > 0, '板框边距须有限且为正')
    display = hub['board_display']
    frames = [o for o in display.objects if o.get('c94_face_frame_v1') and o.parent == rig
              and o.parent_type == 'BONE' and o.parent_bone == record['root'] and o.type == 'CURVE']
    widget = rig.pose.bones[record['root']].custom_shape
    _require(len(frames) == 1 and widget is not None and widget.get('c94_face_frame_v1')
             and widget.type == 'MESH' and len(widget.data.vertices) == 4 and len(widget.data.edges) == 4,
             '当前板框未声明为本工具的矩形；保留原形状，不猜测改写')
    frame = frames[0]
    _require(widget.data.users == 1 and frame.data.users == 1 and len(frame.data.splines) == 4
             and all(s.type == 'POLY' and len(s.points) == 2 for s in frame.data.splines), '板框几何已共享或手改，保留原形状')
    core._update(hub,rig)
    inverse = (rig.matrix_world @ rig.pose.bones[record['root']].matrix).inverted()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []
    for obj in display.objects:
        if obj != frame and obj.type in {'CURVE','FONT'}:
            points.extend(inverse @ point for point in graph._display_points(obj, depsgraph))
    _require(points and all(math.isfinite(v) for p in points for v in p), '无法取得实际控件/标签边界')
    bounds = (min(p.x for p in points)-padding, min(p.y for p in points)-padding,
              max(p.x for p in points)+padding, max(p.y for p in points)+padding)
    verts,edges = graph._rect(*bounds)
    before_vertices = [tuple(v.co) for v in widget.data.vertices]
    before_splines = [[tuple(p.co) for p in s.points] for s in frame.data.splines]
    strengths = {r['property']:hub[r['property']] for r in group['channels']}
    try:
        for vertex,co in zip(widget.data.vertices,verts):vertex.co = co
        widget.data.update()
        for spline,(a,b) in zip(frame.data.splines,edges):
            spline.points[0].co=(*verts[a],1);spline.points[1].co=(*verts[b],1)
        core._update(hub,rig)
        _require(all(hub[p]==value for p,value in strengths.items()), '适配板框改变了表情输入')
        return dict(bounds=list(bounds),inputs_unchanged=True)
    except Exception:
        for vertex,co in zip(widget.data.vertices,before_vertices):vertex.co=co
        widget.data.update()
        for spline,coords in zip(frame.data.splines,before_splines):
            for point,co in zip(spline.points,coords):point.co=co
        core._update(hub,rig);raise
