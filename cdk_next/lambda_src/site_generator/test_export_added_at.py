"""The added_at export, and the key format three components have to agree on.

/just-added/ works by joining the site's events against `createdAt` on the
table's EVENT# rows. The join has a fallback on (date, title) for submitted
events, which used to be the only thing that worked for them: their guid
differed between the table and the site (next_dctech_events-p8o). The exporter
now writes the table's guid and calgen honours it, so the guid half of the join
hits for submitted events too and this fallback is belt-and-braces rather than
load-bearing. It is still worth keeping and still worth testing — anything
hand-authored in `_single_events/` has no guid — and it only works if this
exporter, calgen's reader, and updates_publisher all build the key identically.
A mismatch does not raise, it just silently produces an empty page.

Run: python -m pytest test_export_added_at.py
"""
import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


export = _load('export_dynamo_to_calgen',
               os.path.join(HERE, 'export_dynamo_to_calgen.py'))


class TestTitleKey:
    def test_shape(self):
        assert export._title_key('2026-09-01', 'Open Hack') == '2026-09-01|open hack'

    def test_case_and_internal_whitespace_are_normalized(self):
        assert (export._title_key('2026-09-01', '  Open   HACK ')
                == export._title_key('2026-09-01', 'Open Hack'))

    def test_none_title_does_not_raise(self):
        assert export._title_key('2026-09-01', None) == '2026-09-01|'

    def test_matches_calgens_reader(self):
        """The two halves of the join must agree, or /just-added/ empties out."""
        calgen_added_at = os.path.join(
            HERE, '..', '..', '..', 'packages', 'calgen', 'src', 'calgen', 'added_at.py')
        if not os.path.exists(calgen_added_at):
            pytest.skip('calgen source not available from here')
        reader = _load('calgen_added_at_probe', calgen_added_at)
        for date_str, title in [
            ('2026-09-01', 'Open Hack'),
            ('2026-11-14', '  District   Arcade '),
            ('2026-01-05', 'Ünicode Ĝathering'),
            ('2026-01-05', None),
            ('2026-01-05', ''),
        ]:
            assert export._title_key(date_str, title) == reader._title_key(date_str, title)

    def test_matches_updates_publisher(self):
        publisher = os.path.join(HERE, '..', 'updates_publisher', 'app.py')
        if not os.path.exists(publisher):
            pytest.skip('updates_publisher source not available from here')
        os.environ.setdefault('DYNAMODB_TABLE_NAME', 'test-table')
        pub = _load('updates_publisher_probe', publisher)
        for date_str, title in [('2026-09-01', 'Open Hack'),
                                ('2026-11-14', '  District   Arcade ')]:
            assert export._title_key(date_str, title) == pub._title_key(date_str, title)


class TestStampExport:
    """The build records when it reads the table so trigger/followup.py can tell
    which rings the build covered. The key must match handler.py's state item."""

    def test_writes_exportat_to_the_render_state_item(self):
        calls = []

        class FakeTable:
            def update_item(self, **kw):
                calls.append(kw)

        export._stamp_export(FakeTable())

        (call,) = calls
        assert call['Key'] == {'PK': 'RENDER#site', 'SK': 'STATE'}
        assert call['UpdateExpression'] == 'SET exportAt = :t'
        assert isinstance(call['ExpressionAttributeValues'][':t'], int)

    def test_a_failed_stamp_does_not_fail_the_build(self, capsys):
        class DeniedTable:
            def update_item(self, **kw):
                raise RuntimeError('AccessDeniedException')

        export._stamp_export(DeniedTable())  # must not raise

        assert 'could not stamp' in capsys.readouterr().err

    def test_key_matches_the_trigger_and_followup(self):
        sys.path.insert(0, os.path.join(HERE, 'trigger'))
        os.environ.setdefault('CODEBUILD_PROJECT_NAME', 'x')
        os.environ.setdefault('TABLE_NAME', 'y')
        os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
        import followup
        import handler
        assert handler.STATE_KEY == followup.STATE_KEY == {
            'PK': 'RENDER#site', 'SK': 'STATE'}
