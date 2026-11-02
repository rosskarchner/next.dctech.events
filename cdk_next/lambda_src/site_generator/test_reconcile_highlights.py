"""reconcile_highlights: change-only writes, the 24-hour grace, a single
lost-event notification, and never failing the build.

Run: python -m pytest test_reconcile_highlights.py
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    'reconcile_highlights', os.path.join(HERE, 'reconcile_highlights.py'))
rec = importlib.util.module_from_spec(_spec)
sys.modules['reconcile_highlights'] = rec
_spec.loader.exec_module(rec)

NOW = 1_800_000_000
TODAY = '2026-11-02'


class Table:
    def __init__(self, items=None):
        self.items = {(i['PK'], i['SK']): dict(i) for i in (items or [])}
        self.updates = []

    def get_item(self, Key):
        item = self.items.get((Key['PK'], Key['SK']))
        return {'Item': dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues=None):
        self.updates.append(Key['SK'])
        item = self.items.setdefault((Key['PK'], Key['SK']), dict(Key))
        names, values = ExpressionAttributeNames, ExpressionAttributeValues or {}
        for clause in UpdateExpression.split('REMOVE'):
            clause = clause.strip()
            if clause.startswith('SET '):
                for part in clause[4:].split(','):
                    n, v = (x.strip() for x in part.split('='))
                    item[names[n]] = values[v]
            elif clause:
                for n in clause.split(','):
                    item.pop(names[n.strip()], None)


class Ses:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_email(self, **kw):
        if self.fail:
            raise RuntimeError('ses down')
        self.sent.append(kw['Destination']['ToAddresses'][0])


def _data(tmp_path, matches, date='2026-11-10'):
    (tmp_path / 'highlight_matches.json').write_text(json.dumps(matches))
    (tmp_path / 'highlights.json').write_text(json.dumps(
        [{'target_key': k, 'date': date} for k in matches]))
    return str(tmp_path)


META = {'PK': 'HIGHLIGHT#g-a', 'SK': 'META', 'status': 'active', 'kind': 'paid',
        'title': 'BSides', 'date': '2026-11-10', 'contact_email': 'buyer@x.example',
        'target_key': 'g-a'}


def test_a_new_match_is_stored(tmp_path):
    table = Table([META])
    rec.run(table, Ses(), _data(tmp_path, {'g-a': 'guid1'}), NOW, TODAY)
    assert table.items[('HIGHLIGHT#g-a', 'RESOLVED')]['resolved_guid'] == 'guid1'


def test_an_unchanged_match_writes_nothing(tmp_path):
    table = Table([META, {'PK': 'HIGHLIGHT#g-a', 'SK': 'RESOLVED',
                          'resolved_guid': 'guid1'}])
    rec.run(table, Ses(), _data(tmp_path, {'g-a': 'guid1'}), NOW, TODAY)
    assert table.updates == []


def test_missing_then_grace_then_one_notification(tmp_path):
    table, ses = Table([META]), Ses()
    data = _data(tmp_path, {'g-a': None})
    rec.run(table, ses, data, NOW, TODAY)
    assert table.items[('HIGHLIGHT#g-a', 'RESOLVED')]['missing_since'] == NOW
    rec.run(table, ses, data, NOW + 3600, TODAY)
    assert ses.sent == []  # still inside the grace period
    rec.run(table, ses, data, NOW + rec.GRACE_SECONDS, TODAY)
    assert sorted(ses.sent) == sorted([rec.ADMIN_EMAIL, 'buyer@x.example'])
    rec.run(table, ses, data, NOW + 2 * rec.GRACE_SECONDS, TODAY)
    assert len(ses.sent) == 2  # once only


def test_editor_pick_lost_emails_only_ross(tmp_path):
    table, ses = Table([{**META, 'kind': 'editor'},
                        {'PK': 'HIGHLIGHT#g-a', 'SK': 'RESOLVED', 'missing_since': 1}]), Ses()
    rec.run(table, ses, _data(tmp_path, {'g-a': None}), NOW, TODAY)
    assert ses.sent == [rec.ADMIN_EMAIL]


def test_coming_back_clears_missing(tmp_path):
    table = Table([META, {'PK': 'HIGHLIGHT#g-a', 'SK': 'RESOLVED',
                          'resolved_guid': 'guid1', 'missing_since': NOW - 10}])
    rec.run(table, Ses(), _data(tmp_path, {'g-a': 'guid1'}), NOW, TODAY)
    assert 'missing_since' not in table.items[('HIGHLIGHT#g-a', 'RESOLVED')]


def test_a_past_event_is_not_chased(tmp_path):
    table = Table([META])
    rec.run(table, Ses(), _data(tmp_path, {'g-a': None}, date='2026-11-01'), NOW, TODAY)
    assert table.updates == []


def test_revoked_since_export_is_not_notified(tmp_path):
    table, ses = Table([{**META, 'status': 'revoked'},
                        {'PK': 'HIGHLIGHT#g-a', 'SK': 'RESOLVED', 'missing_since': 1}]), Ses()
    rec.run(table, ses, _data(tmp_path, {'g-a': None}), NOW, TODAY)
    assert ses.sent == []


def test_only_resolved_rows_are_written(tmp_path):
    table = Table([META])
    data = _data(tmp_path, {'g-a': None})
    rec.run(table, Ses(), data, NOW, TODAY)
    rec.run(table, Ses(), data, NOW + rec.GRACE_SECONDS, TODAY)
    assert set(table.updates) == {'RESOLVED'}


def test_mail_failure_does_not_raise(tmp_path):
    table = Table([META, {'PK': 'HIGHLIGHT#g-a', 'SK': 'RESOLVED', 'missing_since': 1}])
    rec.run(table, Ses(fail=True), _data(tmp_path, {'g-a': None}), NOW, TODAY)


def test_main_never_fails(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['x', '--table', 't'])
    monkeypatch.setattr(rec, 'run', lambda *a, **k: 1 / 0)
    assert rec.main() == 0


def test_no_matches_file_is_a_no_op(tmp_path):
    assert rec.run(Table(), Ses(), str(tmp_path), NOW, TODAY) == []
