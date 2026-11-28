"""Closed, immutable suspension results; synthetic evidence, not ADR0019 capture."""
from dataclasses import FrozenInstanceError
import json

import pytest

from evolution_lab import owner


def result(*args, **kwargs):
    return owner.SuspensionEvidenceResult(*args, **kwargs)


def test_available_empty_and_unavailable_payloads_are_distinct():
    assert result('op1', 'available', [], None).to_payload() == {
        'schema': 'lab-suspension-evidence/1', 'operation_id': 'op1',
        'state': 'available', 'source': 'core_raw', 'reason': None, 'suspensions': []}
    assert result('op1', 'unavailable', None, 'campaign_missing').to_payload() == {
        'schema': 'lab-suspension-evidence/1', 'operation_id': 'op1',
        'state': 'unavailable', 'source': 'core_raw', 'reason': 'campaign_missing', 'suspensions': None}


@pytest.mark.parametrize('reason', ['not_campaign', 'core_raw_unsupported',
    'campaign_family_unsupported', 'campaign_missing', 'campaign_unreadable', 'unsupported_schema'])
def test_unavailable_reason_codes(reason):
    assert result('op1', 'unavailable', None, reason).to_payload()['reason'] == reason


@pytest.mark.parametrize('state,values,reason', [
    ('available', None, None), ('available', [], 'campaign_missing'),
    ('unavailable', [], 'campaign_missing'), ('unavailable', None, None),
    ('unavailable', None, 'results_deleted'), ('unavailable', None, 'invented'),
    ('unknown', [], None), ('available', {}, None), ('available', 'private', None)])
def test_result_refuses_inconsistent_state(state, values, reason):
    with pytest.raises(owner.OwnerError):
        result('op1', state, values, reason)


def test_result_defensively_freezes_nested_values_and_returns_fresh_json():
    entries = [{'seq': 7, 'reason': 'unknown_code', 'owner': 'target',
        'evidence_kind': 'stage_record', 'evidence': {'seq': 6, 'operation_id': None}, 'rows': None}]
    value = result('op1', 'available', entries, None)
    entries[0]['evidence']['seq'] = 999
    payload = value.to_payload()
    assert payload['suspensions'][0]['evidence']['seq'] == 6
    payload['suspensions'][0]['evidence']['seq'] = 888
    assert value.to_payload()['suspensions'][0]['evidence']['seq'] == 6
    assert json.loads(json.dumps(value.to_payload())) == value.to_payload()
    with pytest.raises(TypeError):
        value.suspensions[0]['evidence']['seq'] = 4
    with pytest.raises(FrozenInstanceError):
        value.reason = 'campaign_missing'


def test_result_refuses_unknown_nested_content():
    entries = [{'seq': 7, 'reason': 'unknown_code', 'owner': 'target',
        'evidence_kind': 'stage_record', 'evidence': {'seq': 6, 'operation_id': None, 'private': 'secret'},
        'rows': None}]
    with pytest.raises(owner.OwnerError):
        result('op1', 'available', entries, None)
