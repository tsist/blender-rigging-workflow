"""Synchronous session exclusion; fresh pending state, no scene snapshots."""

class WorkspaceError(RuntimeError):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        super().__init__(code + ': ' + str(detail))


class Workspace:
    def __init__(self, names):
        if not isinstance(names, (tuple, list)) or not names or any(type(n) is not str or not n for n in names) or len(set(names)) != len(names):
            raise WorkspaceError('WORKSPACE_COMPONENT_NAMES', names)
        self._getters = dict.fromkeys(names)

    def status(self):
        result = {}
        for name, getter in self._getters.items():
            if getter is None:
                result[name] = {'initialized': False, 'pending': False}
                continue
            try:
                pending = getter()
            except Exception as error:
                raise WorkspaceError('WORKSPACE_STATE_UNAVAILABLE', name) from error
            if type(pending) is not bool:
                raise WorkspaceError('WORKSPACE_PENDING_NOT_BOOL', name)
            result[name] = {'initialized': True, 'pending': pending}
        return result

    def assert_idle(self):
        pending = [name for name, row in self.status().items() if row['pending']]
        if pending:
            raise WorkspaceError('WORKSPACE_RESTORE_FIRST', pending)
        return True

    def bind(self, name, pending_getter):
        if name not in self._getters or not callable(pending_getter):
            raise WorkspaceError('WORKSPACE_COMPONENT_BINDING', name)
        self.assert_idle()
        if type(pending_getter()) is not bool:
            raise WorkspaceError('WORKSPACE_PENDING_NOT_BOOL', name)
        self._getters[name] = pending_getter

    def guard_for(self, name):
        if name not in self._getters:
            raise WorkspaceError('WORKSPACE_UNKNOWN_COMPONENT', name)

        def guard(*, operation=None, owner=None):
            # The closure, supplied directly to one trusted module, identifies
            # the caller. Facial APIs also send their private transaction ID.
            # An owner must retain access to its own Restore/Undo path.
            others = [key for key, row in self.status().items() if key != name and row['pending']]
            if others:
                raise WorkspaceError('WORKSPACE_OTHER_COMPONENT_PENDING', others)
            return True
        return guard
