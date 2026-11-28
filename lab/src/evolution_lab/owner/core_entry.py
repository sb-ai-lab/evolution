"""Durable worker evidence at the Core campaign admission boundary.

The Owner registers the receipt in its inventory before launching this worker.
The worker must publish it before calling Core. Absence for that protocol means
Core was never called; helper/process launch alone says nothing about admission.
"""
from pathlib import Path

from .receipts import ReceiptError, publish_json, read_json

NAME = 'core-entry.json'
SCHEMA = 'lab-campaign-core-entry/1'


def publish_core_entry(config):
    publish_json(Path(config['diagnostic_path']).with_name(NAME), {
        'schema': SCHEMA, 'operation_id': config['operation_id'],
        'scope_id': config['scope_id'], 'owner_generation': config['owner_generation'],
    })


def read_core_entry(path, *, operation_id, scope_id, owner_generation):
    """False only for absence. Invalid or mismatched evidence fails closed."""
    try:
        value = read_json(path)
    except FileNotFoundError:
        return False
    if value != {'schema': SCHEMA, 'operation_id': operation_id,
                 'scope_id': scope_id, 'owner_generation': owner_generation}:
        raise ReceiptError('Core entry identity mismatch')
    return True
