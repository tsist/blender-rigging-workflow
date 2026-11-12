# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent per-group sidebar labels, sections and ordering; no pose writes."""
import json
from . import face_expression_core as core

STATE_KEY = 'c94_expression_sidebar_v1'
SCHEMA = 'c94.expression-sidebar.v1'


def read(hub):
    group, _, _ = core._read(hub)
    names = [r['name'] for r in group['channels']]
    if STATE_KEY not in hub:
        return dict(schema=SCHEMA, columns=1,
                    rows=[dict(channel=n, label=n, section='表情', visible=True) for n in names])
    try:
        record = json.loads(hub[STATE_KEY])
        core._require(set(record) == {'schema', 'columns', 'rows'} and record['schema'] == SCHEMA
                      and type(record['columns']) is int and 1 <= record['columns'] <= 3,
                      '表情侧栏布局字段或版本无效')
        rows = record['rows']
        core._require(isinstance(rows, list) and len(rows) == len(names)
                      and all(isinstance(r, dict) and set(r) == {'channel','label','section','visible'}
                              and r['channel'] in names and type(r['visible']) is bool
                              and all(isinstance(r[k],str) and r[k].strip() and len(r[k].encode('utf8')) <= 192
                                      for k in ('label','section')) for r in rows)
                      and {r['channel'] for r in rows} == set(names), '表情侧栏行映射无效')
        return record
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError('表情侧栏布局损坏；保留原文检查，不自动重置') from exc


def install(hub):
    record = read(hub)
    if STATE_KEY not in hub:
        hub[STATE_KEY] = json.dumps(record,ensure_ascii=False,allow_nan=False)
    return record


def configure(hub, *, channel=None, label=None, section=None, visible=None, position=None, columns=None):
    record = read(hub)
    core._require(channel is None or channel in {r['channel'] for r in record['rows']}, '明确已有表情语义')
    core._require(channel is not None or all(v is None for v in (label,section,visible,position)), '行布局须指定表情')
    core._require(columns is None or type(columns) is int and 1 <= columns <= 3, '侧栏列数为1..3')
    core._require(all(v is None or isinstance(v,str) and v.strip() and len(v.encode('utf8')) <= 192
                      for v in (label,section)) and (visible is None or type(visible) is bool), '标签/分区/显隐无效')
    core._require(position is None or type(position) is int and 0 <= position < len(record['rows']), '行位置无效')
    core._require(any(v is not None for v in (label,section,visible,position,columns)), '明确布局更改项')
    if channel is not None:
        row = next(r for r in record['rows'] if r['channel'] == channel)
        for key,value in [('label',label),('section',section),('visible',visible)]:
            if value is not None:row[key] = value.strip() if isinstance(value,str) else value
        if position is not None:
            record['rows'].remove(row);record['rows'].insert(position,row)
    if columns is not None:record['columns'] = columns
    payload = json.dumps(record,ensure_ascii=False,allow_nan=False)
    old = hub.get(STATE_KEY)
    try:
        hub[STATE_KEY] = payload
        return read(hub)
    except Exception:
        if old is None:
            if STATE_KEY in hub:del hub[STATE_KEY]
        else:hub[STATE_KEY] = old
        raise
