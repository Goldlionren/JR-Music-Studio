"""Shared strict JSON Schema validation for the Master/Critic boundary."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
try:
    from jsonschema import Draft202012Validator
except ModuleNotFoundError:
    # Same project-local dependency directory used by the design tools.
    sys.path.insert(0, str(ROOT / '.dev-deps'))
    from jsonschema import Draft202012Validator

from .store import StoreError


def validate(family, name, value, version=1):
    if type(version) is not int or version not in (1, 2):
        raise StoreError('UNSUPPORTED_CONTRACT_VERSION')
    schema = json.loads((ROOT / 'schemas' / family / str(version) / (name + '.schema.json')).read_text(encoding='utf-8'))
    if next(Draft202012Validator(schema).iter_errors(value), None) is not None:
        raise StoreError('INVALID_' + name.upper().replace('-', '_'))
    return value
