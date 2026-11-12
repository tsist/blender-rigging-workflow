# SPDX-License-Identifier: GPL-3.0-or-later
"""Coherent native face samples across explicit independent Mesh sources.

One sample name covers every member, including unchanged members. At least one
member must move. Candidate submission is all-or-none; original data is never
written. Save/load/unregister release RAM-only submission authority.
"""
import json
import uuid

import bpy
from bpy.app.handlers import persistent

from . import face_capture as capture

_SESSIONS = {}
_require = capture._require


class Session:
    def __init__(self, members, rig):
        self.token = uuid.uuid4().hex
        self.members = tuple(members)
        self.rig = rig
        self._source_names = tuple(m.source.name for m in members)
        self._samples = {}
        self._done = False
        self.report = {}

    def _check(self):
        _require(not self._done and _SESSIONS.get(self.token) is self,
                 '整组捕获会话已结束；保存/换文件/卸载后请重新开始')
        for member in self.members:
            member._check()
            _require(member._neutral_mode == self.members[0]._neutral_mode
                     and member._neutral == self.members[0]._neutral
                     and list(member._samples) == list(self._samples),
                     '组内中立或采样列表不一致，请结束并重新开始')
        _require(tuple(m.source.name for m in self.members) == self._source_names,
                 '源对象已改名，请结束并重新开始以保留准确来源')


def begin(sources, rig):
    """Start an ordered, explicit group at one recorded neutral and frame."""
    _require(isinstance(sources, (list, tuple)) and bool(sources), '请明确提供源网格列表')
    _require(all(isinstance(o, bpy.types.Object) and o.type == 'MESH' for o in sources),
             '组内每项必须是实际Mesh对象')
    _require(len({o.as_pointer() for o in sources}) == len(sources)
             and len({o.data.as_pointer() for o in sources}) == len(sources),
             '源对象或Mesh重复，不能建立同步组')
    members = []
    try:
        for source in sources:
            members.append(capture.begin(source, rig))
        session = Session(members, rig)
        register()
        _SESSIONS[session.token] = session
        session._check()
        session.report = {'schema': 'c94.face-capture-group.v1', 'group_id': session.token,
                          'sources': [m.source.name for m in members], 'rig': rig.name,
                          'neutral_mode': members[0]._neutral_mode, 'samples': [],
                          'unchanged_member_policy': 'retain_same_name_zero_delta_key',
                          'keys_default_zero': True}
        return session
    except Exception:
        if 'session' in locals():
            _SESSIONS.pop(session.token, None)
        for member in members:
            capture.cancel(member)
        raise


def get_session(token):
    _require(isinstance(token, str) and token in _SESSIONS,
             '没有活动整组捕获；保存/换文件/卸载后请重新开始')
    return _SESSIONS[token]


def sample(session, name):
    """Read all members before committing any RAM sample; no source writes."""
    _require(isinstance(session, Session), '需要整组begin返回的Session')
    session._check()
    pending, displacements = [], []
    for member in session.members:
        name, points, delta = capture._sample_payload(member, name)
        pending.append(points)
        displacements.append({'source': member.source.name,
                              'max_displacement_meters': delta, 'unchanged': delta == 0})
    _require(any(row['max_displacement_meters'] > 0 for row in displacements),
             '整组实际RAW网格均未改变，不记录空表情')
    session._check()
    # All validation and native evaluation have succeeded before this commit.
    try:
        for member, points in zip(session.members, pending):
            member._samples[name] = points
            member.report['sample_count'] = len(member._samples)
        session._samples[name] = displacements
    except Exception:
        for member in session.members:
            member._samples.pop(name, None)
            member.report['sample_count'] = len(member._samples)
        session._samples.pop(name, None)
        raise
    return {'name': name, 'sample_count': len(session._samples), 'members': displacements}


def _label(member, token):
    prefix = member.source.name
    suffix = '__表情组_' + token[:8]
    while len((prefix + suffix).encode('utf-8')) > 63:
        prefix = prefix[:-1]
    return prefix + suffix


def create_candidates(session, *, names=None):
    """Submit all independent candidates, or delete only our partial output.

    Submission attempts end the whole group even on failure. Retry requires a
    fresh group; no successful child is silently reused after another fails.
    """
    _require(isinstance(session, Session), '需要整组begin返回的Session')
    candidates, owned = [], []
    try:
        session._check()
        _require(bool(session._samples), '整组至少需要一个实际表情采样')
        labels = names if names is not None else [_label(m, session.token) for m in session.members]
        _require(isinstance(labels, (list, tuple)) and len(labels) == len(session.members),
                 '候选名称数量必须与源对象一致')
        _require(all(isinstance(n, str) and n.strip() and len(n.encode('utf-8')) <= 63
                     and n not in bpy.data.objects for n in labels)
                 and len(set(labels)) == len(labels), '候选名称无效、重复或已存在')
        report = dict(session.report, samples=list(session._samples),
                      sample_members=dict(session._samples))
        for member, label in zip(session.members, labels):
            obj = capture.create_candidate(member, name=label)
            candidates.append(obj)
            owned.append((obj, obj.data, label))
        # Child submission ends each RAM session, so verify frozen facts here.
        for member in session.members:
            _require(capture._source_state(member.source) == member._source
                     and capture._rig_state(session.rig, member._neutral) == member._rig,
                     '整组提交后源或非面部状态不一致')
        report['candidates'] = [o.name for o in candidates]
        report['members'] = [dict(m.report) for m in session.members]
        payload = json.dumps(report, ensure_ascii=False, allow_nan=False)
        for obj in candidates:
            obj['c94_face_capture_group_v1'] = payload
        session.report = report
        return candidates
    except Exception as exc:
        cleanup_errors = []
        for obj, mesh, label in reversed(owned):
            try:
                capture._discard(obj, mesh=mesh)
            except Exception as cleanup:
                cleanup_errors.append(label + ': ' + str(cleanup))
        if cleanup_errors:
            raise RuntimeError(str(exc) + '；部分候选清理失败：' + '; '.join(cleanup_errors)) from exc
        raise
    finally:
        cancel(session)


def cancel(session):
    _require(isinstance(session, Session), '需要整组begin返回的Session')
    _SESSIONS.pop(session.token, None)
    for member in session.members:
        capture.cancel(member)
    session._done = True
    session._samples.clear()
    return {'cancelled': True, 'files_or_candidates_deleted': False}


@persistent
def _clear_sessions(_dummy=None):
    for session in tuple(_SESSIONS.values()):
        cancel(session)


def register():
    capture.register()
    for handlers in (bpy.app.handlers.save_pre, bpy.app.handlers.load_pre):
        if _clear_sessions not in handlers:
            handlers.append(_clear_sessions)


def unregister():
    _clear_sessions()
    for handlers in (bpy.app.handlers.save_pre, bpy.app.handlers.load_pre):
        if _clear_sessions in handlers:
            handlers.remove(_clear_sessions)
