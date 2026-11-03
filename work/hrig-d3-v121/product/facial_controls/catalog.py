"""Frozen source-derived controls; labels/presets remain subject to visual review."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

PATH = Path(__file__).with_name('catalog.json')


def load():
    return json.loads(PATH.read_text(encoding='utf-8'))


def file_sha256():
    return hashlib.sha256(PATH.read_bytes()).hexdigest()


def index(catalog=None):
    data = load() if catalog is None else catalog
    return {row['id']: deepcopy(row) for row in data['controls']}
