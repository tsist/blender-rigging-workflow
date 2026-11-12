# SPDX-License-Identifier: GPL-3.0-or-later
"""Build an explicitly run, file-contained daily panel from shared source.

The shared daily closure includes installed face modes, mouth/graph sliders,
layout and the basic character panel. This does not install an add-on, enable
autoexec, or embed authoring board creation, capture or real-time physics.
"""
import ast
import hashlib
import json
import pprint
from pathlib import Path

import bpy

MODULES = ('mode_semantics', 'control_panel', 'pose_switching', 'pose_switching_leg',
           'mode_animation', 'face_modes', 'face_expression_core', 'face_graph_core',
           'face_expression_layout', 'face_mouth_core', 'face_daily', 'face_daily_ui', 'workflow_navigation')
SCHEMA = 'c94.portable-daily.v2'
NAMESPACE = '_c94_portable_daily_v2'
STATE_KEY = 'c94_portable_daily_v2'
TEXT_KEY = 'c94_portable_daily_text_v2'
TEXT_NAME = 'C94日常控制_手动运行.py'
_EXTERNAL = {'bpy', 'mathutils', 'json', 'math', 'uuid', 'contextlib', 're', 'struct',
             'textwrap', 'hashlib', 'sys'}

# The bootstrap is packaging/registration glue, not a second implementation of
# character controls. Each business module below is the exact shared source.
_BOOTSTRAP = r'''
def _c94_load_daily(_payload):
    import hashlib as _hashlib
    import json as _json
    import sys as _sys
    import types as _types
    import bpy as _bpy

    _names = ('mode_semantics', 'control_panel', 'pose_switching', 'pose_switching_leg',
              'mode_animation', 'face_modes', 'face_expression_core', 'face_graph_core',
              'face_expression_layout', 'face_mouth_core', 'face_daily', 'face_daily_ui', 'workflow_navigation')
    _namespace = '_c94_portable_daily_v2'
    if set(_payload) != {'schema', 'modules', 'source_notice'} or _payload['schema'] != 'c94.portable-daily.v2':
        raise RuntimeError('便携日常面板记录版本无效；未注册')
    if tuple(_payload['modules']) != _names:
        raise RuntimeError('便携日常模块白名单无效；未注册')
    for _name in _names:
        _row = _payload['modules'][_name]
        if set(_row) != {'source', 'sha256'} or _hashlib.sha256(_row['source'].encode('utf8')).hexdigest() != _row['sha256']:
            raise RuntimeError('便携模块内容校验失败：' + _name)
    _digest = _hashlib.sha256(_json.dumps(_payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf8')).hexdigest()
    _old = _sys.modules.get(_namespace)
    if _old is not None:
        if getattr(_old, '_payload_sha256', None) == _digest and getattr(_old, '_registered', False):
            if (all(_cls.is_registered for _cls in _old.control_panel._REGISTERED)
                    and all(_cls.is_registered for _cls in _old.face_daily_ui._REGISTERED)
                    and all(_cls.is_registered for _cls in _old.workflow_navigation._REGISTERED)):
                return _old
        raise RuntimeError('另一份或未完整卸载的便携面板已加载；请保留工程并在新窗口运行，不自动热替换')
    # The Blender registry is global even though Python modules are namespaced.
    # Check every operator/Panel before creating any runtime modules.
    _operator_ids = ('control_collection', 'control_pose', 'switch_arm',
        'add_arm_switch_actions', 'switch_leg', 'add_leg_switch_actions',
        'mode_animation', 'add_mode_animation_actions', 'control_layout',
        'add_native_property_controls', 'native_property_info',
        'face_daily_bind', 'face_daily_mode', 'face_daily_restore',
        'face_daily_visibility', 'face_daily_axis', 'face_daily_axes',
        'face_daily_slider', 'face_daily_reset', 'face_daily_record',
        'face_daily_migrate', 'face_daily_layout', 'workspace_activate')
    for _operator in _operator_ids:
        try:
            getattr(_bpy.ops.c94_workflow, _operator).get_rna_type()
        except (AttributeError, KeyError, RuntimeError):
            continue
        raise RuntimeError('制作插件或其它面板已占用 c94_workflow.' + _operator + '；请在未加载制作插件的新窗口运行，不自动卸载')
    if _bpy.types.Panel.bl_rna_get_subclass_py('C94CTRL_PT_character', None) is not None:
        raise RuntimeError('角色控制Panel已注册；不覆盖其它运行时')
    if _bpy.types.Panel.bl_rna_get_subclass_py('C94DAILY_PT_face', None) is not None:
        raise RuntimeError('日常面部Panel已注册；不覆盖其它运行时')
    if hasattr(_bpy.types.Scene, 'c94_face_daily_options_v1'):
        raise RuntimeError('日常面部Scene选项已注册；不覆盖其它运行时')
    if hasattr(_bpy.types.Scene, 'c94_workspace_options_v1'):
        raise RuntimeError('角色工作台已注册；不覆盖其它运行时')
    _package = _types.ModuleType(_namespace)
    _package.__path__ = []
    _package.__package__ = _namespace
    _package._payload_sha256 = _digest
    _package._registered = False
    _owned = {_namespace: _package}
    _sys.modules[_namespace] = _package
    try:
        for _name in _names:
            _qualified = _namespace + '.' + _name
            _module = _types.ModuleType(_qualified)
            _module.__package__ = _namespace
            _module.__file__ = '<C94 portable>/' + _name + '.py'
            _owned[_qualified] = _module
            _sys.modules[_qualified] = _module
            setattr(_package, _name, _module)
        for _name in _names:
            _module = getattr(_package, _name)
            exec(compile(_payload['modules'][_name]['source'], _module.__file__, 'exec'), _module.__dict__)
        _package.workflow_navigation.register(stage_ids={'AUTO', 'CONTROL', 'FACE'})
        _package.workflow_navigation.route_panels([_package.control_panel, _package.face_daily_ui])
        _package.control_panel.register()
        _package.face_daily_ui.register()
        _package._registered = True
    except Exception:
        _daily = getattr(_package, 'face_daily_ui', None)
        if _daily is not None and hasattr(_daily, 'unregister'):
            _daily.unregister()
        _panel = getattr(_package, 'control_panel', None)
        if _panel is not None and hasattr(_panel, 'unregister'):
            _panel.unregister()
        _navigation = getattr(_package, 'workflow_navigation', None)
        if _navigation is not None and hasattr(_navigation, 'unregister'):
            _navigation.unregister()
        for _qualified, _module in reversed(tuple(_owned.items())):
            if _sys.modules.get(_qualified) is _module:
                del _sys.modules[_qualified]
        raise
    def _unregister():
        _package.face_daily_ui.unregister()
        _package.control_panel.unregister()
        _package.workflow_navigation.unregister()
        _package._registered = False
        for _qualified, _module in reversed(tuple(_owned.items())):
            if _sys.modules.get(_qualified) is _module:
                del _sys.modules[_qualified]
        return {'unregistered': True, 'scene_data_changed': False}
    _package.unregister = _unregister
    _package.source_manifest = {n: _payload['modules'][n]['sha256'] for n in _names}
    return _package
'''


def _digest(value):
    return hashlib.sha256(value.encode('utf8')).hexdigest()


def build_payload():
    """Read the exact shared modules at build time; runtime has no disk imports."""
    modules = {}
    for name in MODULES:
        source = Path(__file__).with_name(name + '.py').read_bytes().decode('utf8')
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                if any(alias.name.split('.')[0] not in _EXTERNAL for alias in node.names):
                    raise ValueError('日常模块增加了未审阅外部依赖：' + name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    dependencies = [node.module.split('.')[0]] if node.module else [a.name for a in node.names]
                    if node.level != 1 or any(dep not in MODULES for dep in dependencies):
                        raise ValueError('日常模块增加了白名单外依赖：' + name)
                elif node.module.split('.')[0] not in _EXTERNAL:
                    raise ValueError('日常模块增加了未审阅外部依赖：' + name)
        modules[name] = {'source': source, 'sha256': _digest(source)}
    return {'schema': SCHEMA, 'modules': modules,
            'source_notice': 'Blender AI project shared daily controls, including the installed face daily closure. Original source headers retained; authoring board creation, capture and real-time physics are not embedded.'}


def build_text():
    payload = build_payload()
    # Retain insertion order: the fixed module order is part of the contract.
    header = ('# SPDX-License-Identifier: GPL-3.0-or-later\n'
              '# 自包含日常控制：仅在信任此文件后，于Text Editor手动Run Script。\n'
              '# 不自动运行、不安装插件、不改变全局autoexec；制作插件冲突时请用新窗口。\n'
              '# 卸载本运行时：import sys; sys.modules["' + NAMESPACE + '"].unregister()\n')
    text = header + _BOOTSTRAP + '\n_C94_PAYLOAD = ' + pprint.pformat(payload, width=100, sort_dicts=False) + '\n'
    text += '_c94_portable_runtime = _c94_load_daily(_C94_PAYLOAD)\n'
    text += 'print("C94随文件日常面板已加载：3D视图 N → 角色工作台 → 角色控制/面部与表情。")\n'
    manifest = {'schema': SCHEMA, 'namespace': NAMESPACE,
                'modules': {name: payload['modules'][name]['sha256'] for name in MODULES},
                'payload_sha256': _digest(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))),
                'text_sha256': _digest(text), 'manual_run_only': True}
    return text, manifest


def embed(rig, *, text_name=TEXT_NAME):
    """Attach a new manual Text to one explicit generated rig, without saving."""
    from . import control_panel
    control_panel._writable(rig)
    text, manifest = build_text()
    old = rig.get(TEXT_KEY)
    if old is not None:
        if isinstance(old, bpy.types.Text) and _digest(old.as_string()) == manifest['text_sha256']:
            return dict(manifest, text=old.name, reused=True)
        raise ValueError('角色已有不同便携Text；保留原文，不自动覆盖或升级')
    if not isinstance(text_name, str) or not text_name.strip() or text_name in bpy.data.texts:
        raise ValueError('明确唯一的新Text名称；已有同名文本不覆盖')
    block = bpy.data.texts.new(text_name)
    previous = rig.get(STATE_KEY)
    try:
        block.from_string(text)
        block.use_module = False
        if _digest(block.as_string()) != manifest['text_sha256']:
            raise RuntimeError('便携Text写后内容不一致')
        rig[TEXT_KEY] = block
        rig[STATE_KEY] = json.dumps(dict(manifest, text=block.name), ensure_ascii=False)
        return dict(manifest, text=block.name, reused=False)
    except Exception:
        if rig.get(TEXT_KEY) == block:
            del rig[TEXT_KEY]
        if previous is None:
            if STATE_KEY in rig:
                del rig[STATE_KEY]
        else:
            rig[STATE_KEY] = previous
        bpy.data.texts.remove(block)
        raise
