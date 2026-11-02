"""The highlights export: active rows only, the RESOLVED guid joined in, and
nothing private (buyer email, payment ids) ever written for calgen.

Run: python -m pytest test_export_highlights.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    'export_dynamo_to_calgen', os.path.join(HERE, 'export_dynamo_to_calgen.py'))
export = importlib.util.module_from_spec(_spec)
sys.modules['export_dynamo_to_calgen'] = export
_spec.loader.exec_module(export)


def _meta(key, status='active', **extra):
    return {'PK': f'HIGHLIGHT#{key}', 'SK': 'META', 'target_key': key,
            'kind': 'paid', 'status': status, 'title': key, 'date': '2026-11-20',
            'url': 'https://e.example', 'guid': key[2:],
            'contact_email': 'buyer@example.com', 'txn_id': 'T1',
            'payer_email': 'payer@example.com', **extra}


def test_only_active_rows_are_exported():
    rows = export._highlights([
        _meta('g-a'), _meta('g-b', status='held'), _meta('g-c', status='revoked'),
        {'PK': 'HIGHLIGHT#g-a', 'SK': 'PAYMENT#T1#Completed'}])
    assert [r['target_key'] for r in rows] == ['g-a']


def test_resolved_guid_is_joined():
    rows = export._highlights([
        _meta('g-a'),
        {'PK': 'HIGHLIGHT#g-a', 'SK': 'RESOLVED', 'resolved_guid': 'zzz'}])
    assert rows[0]['resolved_guid'] == 'zzz'


def test_private_fields_never_leave_dynamodb():
    row = export._highlights([_meta('g-a')])[0]
    for private in ('contact_email', 'txn_id', 'payer_email', 'status'):
        assert private not in row
    assert set(row) == set(export.HIGHLIGHT_FIELDS) | {'resolved_guid'}
