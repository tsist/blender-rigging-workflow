"""Session workflow shared by real UI operators and CLI operator tests."""
from copy import deepcopy
import bpy
from binding import BoundControl


_PRIVATE_RESULT_FIELDS = frozenset(('snapshot_token', 'restore_token', 'recovery_token'))


class SessionError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _public_result(value):
    """Detach a result and keep private transaction handles out of UI data."""
    if type(value) is dict:
        return {key: _public_result(item) for key, item in value.items()
                if key not in _PRIVATE_RESULT_FIELDS}
    if type(value) is list:
        return [_public_result(item) for item in value]
    if type(value) is tuple:
        return tuple(_public_result(item) for item in value)
    return deepcopy(value)


def _is_scalar_or_array(value):
    if value is None or type(value) in (bool, int, float, str):
        return True
    return type(value) in (list, tuple) and all(
        item is None or type(item) in (bool, int, float, str) for item in value)


class ControlSession:
    def __init__(self, document, *, control_factory=BoundControl):
        self._control = control_factory(document)
        self._document = self._control.describe()
        self.phase = 'Ready'
        self._snapshot = None
        self._applied = None

    @property
    def has_pending_edit(self):
        return self._control.has_pending_edit

    @property
    def write_count(self):
        return self._control.write_count

    def available(self, action):
        if action == 'inspect':
            return not self.has_pending_edit and self.phase in ('Ready', 'Restored', 'Error')
        if action == 'preview':
            return not self.has_pending_edit and self.phase == 'Inspected'
        if action == 'retain':
            return self.has_pending_edit and self.phase == 'Preview'
        if action == 'restore':
            return (self.has_pending_edit and self._applied is not None and
                    self.phase in ('Preview', 'Applied', 'Error'))
        return False

    def _context(self):
        runtime = self._document['runtime']
        if bpy.context.scene.name != runtime['scene'] or bpy.context.view_layer.name != runtime['view_layer']:
            raise SessionError('SESSION_CONTEXT_CHANGED')

    def perform(self, action):
        if not self.available(action):
            raise SessionError('SESSION_TRANSITION_REJECTED')
        self._context()
        try:
            if action == 'inspect':
                result = self._control.inspect()
                self._snapshot = result['snapshot_token']
                self.phase = 'Inspected'
            elif action == 'preview':
                result = self._control.apply(self._snapshot)
                self._applied = deepcopy(result)
                self.phase = 'Preview'
            elif action == 'retain':
                result = {'retained': True, 'new_component_write': False, 'persistent_save': False}
                self.phase = 'Applied'
            else:
                result = self._control.restore(self._applied['restore_token'])
                self._snapshot = self._applied = None
                self.phase = 'Restored'
            return _public_result(result)
        except Exception:
            self.phase = 'Error'
            if action == 'preview' and self.has_pending_edit:
                recovery = getattr(self._control, 'recovery_token', None)
                if type(recovery) is str and recovery:
                    self._applied = {'restore_token': recovery}
            raise

    def _role(self):
        display = self._document.get('display')
        if type(display) is str and display:
            return display
        if type(display) is dict:
            for field in ('role', 'role_label', 'label'):
                label = display.get(field)
                if type(label) is str and label:
                    return label
        role_id = self._document.get('role_id', '')
        return role_id if type(role_id) is str else str(role_id)

    def _input_state(self):
        reader = getattr(self._control, 'input_state', None)
        if reader is not None:
            if not callable(reader):
                raise SessionError('INPUT_STATE_PROTOCOL')
            state = reader()
            if type(state) is not dict or set(state) != {'text', 'value'}:
                raise SessionError('INPUT_STATE_PROTOCOL')
            text, value = state['text'], state['value']
            if type(text) is not str or not _is_scalar_or_array(value):
                raise SessionError('INPUT_STATE_PROTOCOL')
            return text, deepcopy(value), None

        native = self._document['native']
        value = float(bpy.data.objects[native['rig']].pose.bones[native['bone']].location[0])
        return str(value), value, value

    def status(self):
        input_text, input_value, local_x = self._input_state()
        state = {
            'phase': self.phase,
            'role': self._role(),
            'binding': self._document['binding_id'],
            'character': self._document['character_id'],
            'pending': bool(self.has_pending_edit),
            'count': self.write_count,
            'input_text': input_text,
            'input_value': input_value,
        }
        if local_x is not None:
            state['local_x'] = float(local_x)
        return state
