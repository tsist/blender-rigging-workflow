"""Pure-host ID-cost plan and native gnuplot-data parsing contracts.

No bpy, file access, native execution or gnuplot execution. Statistical rows are
the native export's observations, not a complete scene-cost inventory. A noop
update may retain previous statistics; missing IDs do not establish zero cost.
The fixed native version's top-32/nonzero selection is a worker-level coverage
contract; this parser preserves rows and never fills in censored IDs with zero.
"""
from __future__ import annotations

import csv
import math
import re

from .diagnostic_contract import validate_plan as validate_observation


SCHEMA = 'hrig.id-cost-plan.v1'
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_ROWS = 10000
MAX_LABEL_CHARS = 4096
PRECISION_NOTE = (
    'Native debug_stats_gnuplot prints seconds with six decimal places; '
    'a positive native cost can round to 0.000000. Values do not imply unlimited '
    'timing accuracy, exact zero cost or complete ID coverage.'
)
_OPEN = re.compile(r'[ \t]*\$data[ \t]+<<[ \t]+EOD[ \t]*\Z')
_LABEL = re.compile(r'\[([A-Z]{2})\] (.+)\Z')
_SECONDS = re.compile(r'(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z')
# csv.reader(strict=True) still accepts bare quotes inside unquoted fields.
# Require RFC-style quote placement first, then delegate escaping to csv.reader.
_FIELD = r'(?:"(?:[^"\r\n]|"")*"|[^",\r\n]*)'
_RECORD = re.compile(r'[ \t]*' + _FIELD + r',[ \t]*' + _FIELD + r'\Z')


class IDCostError(ValueError):
    def __init__(self, message, code='INVALID_ID_COST_STATS'):
        super().__init__(message)
        self.code = code


def _require(condition, message, code='INVALID_ID_COST_STATS'):
    if not condition:
        raise IDCostError(message, code)


def validate_plan(value):
    """Validate exact wrapper fields and return independently copied plain data.

    Callers consuming raw JSON must first reject duplicate keys and nonfinite
    literals with their strict JSON parser; a dict cannot retain duplicate keys.
    """
    code = 'INVALID_ID_COST_PLAN'
    _require(type(value) is dict and set(value) == {'schema', 'observation', 'profile'},
             'ID-cost plan fields must be exactly schema/observation/profile', code)
    _require(type(value['schema']) is str and value['schema'] == SCHEMA,
             'Expected schema ' + SCHEMA, code)
    _require(type(value['profile']) is bool, 'profile must be a JSON boolean', code)
    try:
        observation = validate_observation(value['observation'])
    except ValueError as exc:
        raise IDCostError(str(exc), code) from exc
    return {'schema': SCHEMA, 'observation': observation, 'profile': value['profile']}


def parse_stats(text):
    """Read only one $data << EOD block, never interpret following commands.

    Returns one row per nonblank CSV record, in native order. row_index is zero
    based and is not an ID identity. Duplicate raw labels remain separate rows.
    Only unsigned ordinary decimal seconds are accepted (no scientific notation).
    An empty, properly delimited block returns [], with no coverage assertion.
    """
    _require(type(text) is str, 'Native stats must be decoded text')
    _require(len(text) <= MAX_TEXT_BYTES, 'Stats exceed the 2 MiB UTF-8 text limit')
    try:
        size = len(text.encode('utf-8', errors='strict'))
    except UnicodeError as exc:
        raise IDCostError('Stats contain invalid Unicode') from exc
    _require(size <= MAX_TEXT_BYTES, 'Stats exceed the 2 MiB UTF-8 text limit')
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if _OPEN.fullmatch(line)]
    _require(len(starts) == 1, 'Exactly one standalone $data << EOD block is required')
    start = starts[0]
    end = next((index for index in range(start + 1, len(lines))
                if lines[index].strip(' \t') == 'EOD'), None)
    _require(end is not None, 'Native stats data block has no standalone EOD terminator')
    result = []
    for physical_line, line in enumerate(lines[start + 1:end], start=start + 2):
        if not line.strip(' \t'):
            continue
        _require(len(result) < MAX_ROWS, 'Stats contain more than 10000 data rows')
        _require(_RECORD.fullmatch(line) is not None,
                 f'Malformed two-field CSV record at line {physical_line}')
        try:
            rows = list(csv.reader([line], strict=True, skipinitialspace=True))
        except csv.Error as exc:
            raise IDCostError(f'Invalid CSV at line {physical_line}: {exc}') from exc
        _require(len(rows) == 1 and len(rows[0]) == 2,
                 f'CSV must have exactly label/seconds at line {physical_line}')
        label, raw_seconds = rows[0]
        _require(0 < len(label) <= MAX_LABEL_CHARS and not any(c in label for c in '\x00\r\n'),
                 f'Invalid or oversized ID label at line {physical_line}')
        match = _LABEL.fullmatch(label)
        _require(match is not None and bool(match.group(2).strip()),
                 f'ID label must have form [XX] Name at line {physical_line}')
        seconds_text = raw_seconds.strip(' \t')
        _require(_SECONDS.fullmatch(seconds_text) is not None,
                 f'Seconds must be an unsigned ordinary decimal at line {physical_line}')
        try:
            seconds = float(seconds_text)
        except (ValueError, OverflowError) as exc:
            raise IDCostError(f'Invalid seconds at line {physical_line}') from exc
        _require(math.isfinite(seconds) and seconds >= 0.0,
                 f'Seconds must be finite and nonnegative at line {physical_line}')
        _require(seconds != 0.0 or not any(c in '123456789' for c in seconds_text),
                 f'Positive seconds underflow float representation at line {physical_line}')
        result.append({'row_index': len(result), 'raw_label': label,
                       'id_code': match.group(1), 'name': match.group(2),
                       'seconds': seconds, 'precision_note': PRECISION_NOTE})
    return result
