"""The added_at export, and the key format three components have to agree on.

/just-added/ works by joining the site's events against `createdAt` on the
table's EVENT# rows. The join has a fallback on (date, title) for submitted
events, whose guid differs between the table and the site
(next_dctech_events-p8o). That fallback only works if this exporter, calgen's
reader, and updates_publisher all build the key identically — a mismatch does
not raise, it just silently produces an empty page.

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
