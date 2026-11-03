"""Pure-host forward binding for Blender 5.2.1 build 9e2066aef7ef.

Pinned source: blender/blender commit 9e2066aef7ef7e20c142ad7bd3303138a4304c93,
intern/debug/deg_debug_stats_gnuplot.cc, gnuplotify_name(). Each underscore is
preceded by THREE literal backslashes (ASCII 92,92,92); all other characters
are unchanged. This is not general CSV/gnuplot escaping. No reverse unescape,
code execution, file access, ID inference, row merging or timing summation.
"""
from __future__ import annotations

import copy
import json
import math
import re

MAX_ROWS = 10000
MAX_BINDINGS = 10000
MAX_LABEL_CHARS = 4096
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_IDENTITY_BYTES = 8 * 1024 * 1024
_LABEL = re.compile(r'\[([A-Z]{2})\] (.+)\Z')
_INSERT = chr(92) * 3


class LabelBindingError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise LabelBindingError(message)


def _text(value, label, maximum=MAX_LABEL_CHARS):
    _require(type(value) is str and 0 < len(value) <= maximum,
             label + ' must be a bounded nonempty string')
    _require(not any(char in value for char in '\x00\r\n'), label + ' contains an unsupported control character')
    try:
        return len(value.encode('utf-8', errors='strict'))
    except UnicodeError as exc:
        raise LabelBindingError(label + ' contains invalid Unicode') from exc


def _label(value):
    _text(value, 'label')
    match = _LABEL.fullmatch(value)
    _require(match is not None and bool(match.group(2).strip()), 'Expected [XX] nonempty-name label')
    return match


def encode_label(label):
    """Forward-encode the complete native label, never decode observed rows.

    Existing backslashes and quotes remain literal data. Newline/NUL labels
    are refused because native export cannot safely delimit them as one row.
    This function does not make native quote-containing CSV valid.
    """
    _label(label)
    encoded = label.replace('_', _INSERT + '_')
    _text(encoded, 'encoded label')
    return encoded


def _identity(value, depth=0):
    _require(type(value) is dict and {'type', 'name', 'library'} <= set(value)
             and set(value) <= {'type', 'name', 'library', 'owner'}, 'Invalid native identity fields')
    _text(value['type'], 'identity.type')
    _text(value['name'], 'identity.name')
    if value['library'] is not None:
        _text(value['library'], 'identity.library', 32768)
    if 'owner' in value:
        _require(depth < 1, 'Nested identity owner depth exceeds the supported Key-owner domain')
        _identity(value['owner'], depth + 1)
    # Copy only after the bounded, known identity structure is checked.
    return copy.deepcopy(value)


def bind_rows(rows, bindings):
    """Preserve each parsed observation with all matching identity candidates.

    Inputs are parsed ID-cost rows and native unencoded label bindings. Zero,
    one or multiple matching binding records yield unmatched/unique/ambiguous.
    Duplicate bindings remain candidates; duplicate rows are never combined.
    A unique binding is unique within this supplied list, not globally proven.
    Quote-containing labels are refused: native export does not escape them,
    and valid CSV parsing can itself fold doubled native quotes ambiguously.
    """
    _require(type(rows) is list and len(rows) <= MAX_ROWS, 'Expected at most 10000 rows')
    _require(type(bindings) is list and len(bindings) <= MAX_BINDINGS, 'Expected at most 10000 bindings')
    table = {}
    input_bytes = 0
    for binding in bindings:
        _require(type(binding) is dict and set(binding) == {'label', 'identity'}, 'Binding fields must be label/identity')
        match = _label(binding['label'])
        _require('"' not in binding['label'],
                 'Quote-containing native labels lack a lossless native CSV transport contract')
        identity = _identity(binding['identity'])
        _require(match.group(2) == identity['name'], 'Native label name differs from its explicit identity')
        encoded = encode_label(binding['label'])
        size = len(json.dumps(identity, ensure_ascii=False, allow_nan=False).encode('utf-8'))
        input_bytes += len(binding['label'].encode('utf-8')) + size
        _require(input_bytes <= MAX_TEXT_BYTES, 'Binding/row input exceeds the 8 MiB text budget')
        table.setdefault(encoded, []).append((identity, size))
    result = []
    output_identity_bytes = 0
    for position, row in enumerate(rows):
        _require(type(row) is dict and {'row_index', 'raw_label', 'seconds'} <= set(row)
                 and set(row) <= {'row_index', 'raw_label', 'seconds', 'id_code', 'name', 'precision_note'},
                 'Unexpected parsed row fields')
        _require(type(row['row_index']) is int and 0 <= row['row_index'] < MAX_ROWS,
                 'row_index must be a bounded native integer')
        match = _label(row['raw_label'])
        _require('"' not in row['raw_label'],
                 'Quote-containing parsed labels cannot be certified as lossless native labels')
        value = row['seconds']
        _require(type(value) in (int, float), 'seconds must be a finite nonnegative native number')
        try:
            good = math.isfinite(value) and value >= 0
        except OverflowError:
            good = False
        _require(good, 'seconds must be finite and nonnegative')
        if 'id_code' in row:
            _require(type(row['id_code']) is str and row['id_code'] == match.group(1), 'Parsed id_code differs from raw_label')
        if 'name' in row:
            _require(type(row['name']) is str and row['name'] == match.group(2), 'Parsed name differs from raw_label')
        if 'precision_note' in row:
            _text(row['precision_note'], 'precision_note')
        input_bytes += len(json.dumps(row, ensure_ascii=False, allow_nan=False).encode('utf-8'))
        _require(input_bytes <= MAX_TEXT_BYTES, 'Binding/row input exceeds the 8 MiB text budget')
        candidates = table.get(row['raw_label'], [])
        output_identity_bytes += sum(size for identity, size in candidates)
        _require(output_identity_bytes <= MAX_OUTPUT_IDENTITY_BYTES,
                 'Repeated identity candidates exceed the 8 MiB output budget')
        result.append({**copy.deepcopy(row), 'input_position': position,
                       'binding_status': 'unique' if len(candidates) == 1 else 'ambiguous' if candidates else 'unmatched',
                       'candidate_identities': [copy.deepcopy(identity) for identity, size in candidates]})
    return result
