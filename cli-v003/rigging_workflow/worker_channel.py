# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded anonymous-stdin handoff for this product's owned child processes.

This is a product API capability, not isolation from arbitrary caller Python.
Keys exist only in parent/child memory and the inherited anonymous pipe.
"""
import hashlib
import hmac
import json
import os
import queue
import struct
import sys
import threading

SCHEMA = 'rigging.skeleton-worker-channel.v1'
MAX_PACKET = 2048
_used_nonces = set()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _need(condition):
    if not condition:
        raise ValueError('后台许可通道认证失败')


def _message(proof, parent_pid, child_pid):
    return {'schema': SCHEMA, 'proof': proof, 'parent_pid': parent_pid,
            'child_pid': child_pid}


def packet(key, proof, child_pid):
    """Private transport bytes; callers must never persist or log the result."""
    message = _message(proof, os.getpid(), child_pid)
    body = {'schema': SCHEMA, 'parent_pid': os.getpid(), 'child_pid': child_pid,
            'nonce': proof['nonce'], 'key': key.hex(),
            'mac': hmac.new(key, _canonical(message), hashlib.sha256).hexdigest()}
    raw = _canonical(body)
    _need(0 < len(raw) <= MAX_PACKET)
    return struct.pack('!I', len(raw)) + raw


def send(process, raw, *, timeout=5.):
    """Bound the write too: a failed constructor has no Job watchdog yet."""
    _need(process.stdin is not None and len(raw) <= MAX_PACKET + 4)
    done = queue.Queue(maxsize=1)

    def write():
        try:
            offset = 0
            while offset < len(raw):
                count = os.write(process.stdin.fileno(), raw[offset:])
                _need(count > 0)
                offset += count
            process.stdin.close()
            done.put(True)
        except Exception:
            # Never propagate data-bearing IO exceptions or packet contents.
            done.put(False)

    threading.Thread(target=write, daemon=True, name='rig-worker-handoff').start()
    try:
        _need(done.get(timeout=timeout) is True)
    except queue.Empty:
        raise ValueError('后台许可通道交接超时') from None


def _read_exact(stream, count):
    output = bytearray()
    while len(output) < count:
        chunk = stream.read(count - len(output))
        _need(isinstance(chunk, bytes) and bool(chunk))
        output.extend(chunk)
    return bytes(output)


def receive(stream=None, *, timeout=20.):
    """Single bounded frame; EOF, a console, and an idle pipe fail closed."""
    stream = getattr(sys.stdin, 'buffer', None) if stream is None else stream
    _need(stream is not None and not getattr(stream, 'isatty', lambda: False)())
    done = queue.Queue(maxsize=1)

    def read():
        try:
            size = struct.unpack('!I', _read_exact(stream, 4))[0]
            _need(0 < size <= MAX_PACKET)
            raw = _read_exact(stream, size)
            value = json.loads(raw.decode('utf-8'))
            done.put(value)
        except Exception:
            done.put(None)

    threading.Thread(target=read, daemon=True, name='rig-worker-receive').start()
    try:
        value = done.get(timeout=timeout)
    except queue.Empty:
        raise ValueError('后台许可通道读取超时') from None
    _need(isinstance(value, dict))
    return value


def authenticate(proof, stream=None):
    """Only the inherited channel supplies a key; disk proof is insufficient."""
    value = receive(stream)
    _need(set(value) == {'schema', 'parent_pid', 'child_pid', 'nonce', 'key', 'mac'})
    _need(value['schema'] == SCHEMA and type(value['parent_pid']) is int
          and type(value['child_pid']) is int
          and value['parent_pid'] == os.getppid() and value['child_pid'] == os.getpid()
          and proof.get('parent_pid') == value['parent_pid']
          and value['nonce'] == proof.get('nonce') and value['nonce'] not in _used_nonces)
    _need(isinstance(value['key'], str) and len(value['key']) == 64
          and isinstance(value['mac'], str) and len(value['mac']) == 64)
    try:
        key = bytes.fromhex(value['key'])
    except ValueError:
        raise ValueError('后台许可通道认证失败') from None
    expected = hmac.new(key, _canonical(_message(proof, value['parent_pid'], value['child_pid'])),
                        hashlib.sha256).hexdigest()
    _need(hmac.compare_digest(expected, value['mac']))
    _used_nonces.add(value['nonce'])
    return {'channel_schema': SCHEMA, 'parent_pid': value['parent_pid'],
            'child_pid': value['child_pid'], 'nonce': value['nonce']}
