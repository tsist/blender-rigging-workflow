"""Small visible panel, calling the versioned profile's shared controller."""
import json
import os
from pathlib import Path
import tempfile
import bpy
from .controller import BoundControl, ControlSession


_session = None
_directory = None
_events = []
_audit_warning = None
_audit_detail = None
_PRIVATE_RESULT_FIELDS = frozenset(('snapshot_token', 'restore_token',
                                    'recovery_token'))


def session():
    return _session


def audit_warning():
    return _audit_warning


def _without_tokens(value):
    if type(value) is dict:
        return {key: _without_tokens(item) for key, item in value.items()
                if key not in _PRIVATE_RESULT_FIELDS}
    if type(value) is list:
        return [_without_tokens(item) for item in value]
    if type(value) is tuple:
        return tuple(_without_tokens(item) for item in value)
    return value


def write_json(path, value):
    """Atomically replace one file inside the session's owned audit directory."""
    target = Path(path)
    if (_directory is None or target.parent != _directory or
            target.name not in ('events.json', 'RESTORE.json')):
        raise RuntimeError('UI_AUDIT_PATH_REJECTED')

    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf8', dir=str(target.parent),
                prefix='.' + target.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def emit(action, result):
    event = _without_tokens({
        'action': action,
        **_session.status(),
        'frame': bpy.context.scene.frame_current_final,
        'entry': 'control_ui.ControlSession -> bound',
        'result': result,
    })
    events = _events + [event]
    write_json(_directory / 'events.json', events)
    _events.append(event)


def _remember_audit_failure(message, error):
    global _audit_warning, _audit_detail
    _audit_warning = message
    _audit_detail = f'{type(error).__name__}: {error}'


def _clear_audit_failure():
    global _audit_warning, _audit_detail
    _audit_warning = _audit_detail = None


def _error_receipt(error):
    code = str(getattr(error, 'code', type(error).__name__))
    phase = getattr(_session, 'phase', 'Unknown')
    detail = _without_tokens(getattr(error, 'detail', None))
    message = f'{code}; phase={phase}'
    if detail is not None:
        message += f'; detail={detail}'
    return code, detail, phase, message


def _redraw(context):
    for area in context.screen.areas if context.screen else []:
        area.tag_redraw()


class HRIG068ControlOperator:
    action = ''

    @classmethod
    def poll(cls, context):
        return _session is not None and _session.available(cls.action)

    def execute(self, context):
        try:
            result = _session.perform(self.action)
        except Exception as error:
            code, detail, phase, message = _error_receipt(error)
            try:
                emit('error', {'action': self.action, 'code': code,
                               'detail': detail, 'phase': phase})
            except Exception as audit_error:
                _remember_audit_failure('操作失败，记录写入失败', audit_error)
            else:
                _clear_audit_failure()
            self.report({'ERROR'}, message)
            _redraw(context)
            return {'CANCELLED'}

        audit_failed = False
        try:
            emit(self.action, result)
        except Exception as audit_error:
            audit_failed = True
            _remember_audit_failure('操作已完成，记录写入失败', audit_error)
        if self.action == 'restore':
            try:
                write_json(_directory / 'RESTORE.json', result)
            except Exception as audit_error:
                audit_failed = True
                _remember_audit_failure('操作已完成，记录写入失败', audit_error)
        if audit_failed:
            self.report({'WARNING'}, _audit_warning)
        else:
            _clear_audit_failure()
        _redraw(context)
        return {'FINISHED'}


class HRIG068_OT_inspect(HRIG068ControlOperator, bpy.types.Operator):
    bl_idname = 'hrig068.control_inspect'
    bl_label = '检查绑定'
    action = 'inspect'


class HRIG068_OT_preview(HRIG068ControlOperator, bpy.types.Operator):
    bl_idname = 'hrig068.control_preview'
    bl_label = '预览控制'
    action = 'preview'


class HRIG068_OT_retain(HRIG068ControlOperator, bpy.types.Operator):
    bl_idname = 'hrig068.control_retain'
    bl_label = '保留预览（内存）'
    action = 'retain'


class HRIG068_OT_restore(HRIG068ControlOperator, bpy.types.Operator):
    bl_idname = 'hrig068.control_restore'
    bl_label = '恢复原值'
    action = 'restore'


class HRIG068_PT_control(bpy.types.Panel):
    bl_idname = 'HRIG068_PT_control'
    bl_label = '角色控制'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'HRIG Binding'

    def draw(self, context):
        if _session is None:
            return
        state = _session.status()
        layout = self.layout
        layout.label(text={'anion': '爱音', 'snow': 'Snow'}.get(
            state['character'], state['character']))
        layout.label(text='Role: ' + str(state['role']))
        layout.label(text='State: ' + state['phase'])
        layout.label(text='Input: ' + state['input_text'])
        if 'local_x' in state:
            layout.label(text='Local X = ' + str(round(state['local_x'], 7)))
        for action in ('inspect', 'preview', 'retain', 'restore'):
            layout.operator('hrig068.control_' + action)
        if state['pending']:
            layout.label(text='Pending: 修改待保留或恢复')
        elif state['phase'] == 'Restored':
            layout.label(text='Restored: 原值已恢复')
        else:
            layout.label(text='Pending: 无')
        if _audit_warning is not None:
            layout.label(text=_audit_warning)


CLASSES = (HRIG068_OT_inspect, HRIG068_OT_preview, HRIG068_OT_retain,
           HRIG068_OT_restore, HRIG068_PT_control)


def _class_base(cls):
    return bpy.types.Operator if issubclass(cls, bpy.types.Operator) else bpy.types.Panel


def _class_rna_identifier(cls):
    idname = cls.bl_idname
    if '.' in idname:
        module, name = idname.split('.', 1)
        return module.upper() + '_OT_' + name
    return idname


def _registered_class(cls):
    return _class_base(cls).bl_rna_get_subclass_py(_class_rna_identifier(cls))


def _class_is_registered(cls):
    return _registered_class(cls) is cls


def _class_conflict(cls):
    registered = _registered_class(cls)
    if registered is not None and registered is not cls:
        return registered
    for base in (bpy.types.Operator, bpy.types.Panel):
        for candidate in base.__subclasses__():
            if candidate is cls or candidate.__name__ != cls.__name__:
                continue
            if _class_is_registered(candidate):
                return candidate
    return None


def _require_class_ownership():
    for cls in CLASSES:
        if _class_conflict(cls) is not None:
            raise RuntimeError('UI_CLASS_CONFLICT')


def install(document, directory, *, control_factory=BoundControl):
    global _session, _directory, _events, _audit_warning, _audit_detail
    if _session is not None:
        if bool(_session.has_pending_edit):
            raise RuntimeError('UI_SESSION_PENDING')
        if getattr(_session, 'phase', None) not in ('Ready', 'Restored', 'Error'):
            raise RuntimeError('UI_SESSION_REPLACE_DENIED')

    _require_class_ownership()
    control = ControlSession(document, control_factory=control_factory)
    target = Path(directory)
    target.mkdir(exist_ok=False)

    attempted = []
    try:
        for cls in CLASSES:
            if _class_is_registered(cls):
                continue
            bpy.utils.register_class(cls)
            attempted.append(cls)
        if not all(_class_is_registered(cls) for cls in CLASSES):
            raise RuntimeError('UI_CLASS_REGISTRATION_INCOMPLETE')
    except Exception:
        for cls in reversed(attempted):
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                pass
        try:
            target.rmdir()
        except OSError:
            pass
        raise

    _session, _directory, _events = control, target, []
    _clear_audit_failure()
    try:
        emit('ready', {'profile': control._control.describe()})
    except Exception as audit_error:
        _remember_audit_failure('记录写入失败', audit_error)
    return control


def uninstall():
    global _session, _directory, _events, _audit_warning, _audit_detail
    if _session is None:
        return False
    if bool(_session.has_pending_edit):
        raise RuntimeError('UI_SESSION_PENDING')
    _require_class_ownership()
    unregistered = []
    try:
        for cls in reversed(CLASSES):
            if _class_is_registered(cls):
                bpy.utils.unregister_class(cls)
                unregistered.append(cls)
    except Exception:
        for cls in reversed(unregistered):
            try:
                if not _class_is_registered(cls):
                    bpy.utils.register_class(cls)
            except Exception:
                pass
        raise
    _session = _directory = None
    _events = []
    _audit_warning = _audit_detail = None
    return True
