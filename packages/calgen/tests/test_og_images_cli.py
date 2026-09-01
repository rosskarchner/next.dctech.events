"""`calgen og-images` — generates static/og/{slug}.png for every event in
_data/all_events.json, run between `calgen pipeline` and `calgen build` in
the site-generator buildspec.

Run: python -m pytest test_og_images_cli.py
"""
import json
import os

from click.testing import CliRunner
from PIL import Image

from calgen.cli import cli
from calgen.event_utils import event_slug


def _write_site(tmp_path, events, categories=None):
    (tmp_path / '_data').mkdir()
    (tmp_path / '_data' / 'all_events.json').write_text(json.dumps(events))
    (tmp_path / '_categories').mkdir()
    for slug, cat in (categories or {}).items():
        (tmp_path / '_categories' / f'{slug}.yaml').write_text(f"name: {cat['name']}\n")
    (tmp_path / 'config.yaml').write_text("site_name: Test Events\n")


def test_generates_one_png_per_event(tmp_path):
    events = [
        {'title': 'Event One', 'date': '2026-09-01', 'time': '18:00', 'guid': 'aaa111'},
        {'title': 'Event Two', 'date': '2026-09-02', 'time': '09:00', 'guid': 'bbb222'},
    ]
    _write_site(tmp_path, events)

    result = CliRunner().invoke(cli, ['og-images', '--site-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output

    out_dir = tmp_path / 'static' / 'og'
    for event in events:
        png_path = out_dir / f'{event_slug(event)}.png'
        assert png_path.exists()
        with Image.open(png_path) as img:
            assert img.size == (1200, 630)


def test_uses_the_first_categorys_display_name(tmp_path):
    events = [{
        'title': 'AI Meetup', 'date': '2026-09-01', 'time': '18:00', 'guid': 'ccc333',
        'categories': ['ai', 'data'],
    }]
    _write_site(tmp_path, events, categories={
        'ai': {'name': 'Artificial Intelligence'},
        'data': {'name': 'Data'},
    })

    result = CliRunner().invoke(cli, ['og-images', '--site-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    # No direct way to read the drawn text back out of the PNG (no OCR here)
    # — this just confirms the run didn't error resolving a real category
    # slug to a display name, which is the part with a failure mode (a typo
    # in the categories dict key, a missing category file).
    assert (tmp_path / 'static' / 'og' / f"{event_slug(events[0])}.png").exists()


def test_an_event_with_no_categories_does_not_error(tmp_path):
    events = [{'title': 'No Category Event', 'date': '2026-09-01', 'time': '18:00', 'guid': 'ddd444'}]
    _write_site(tmp_path, events)

    result = CliRunner().invoke(cli, ['og-images', '--site-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / 'static' / 'og' / f"{event_slug(events[0])}.png").exists()


def test_missing_all_events_json_errors_instead_of_crashing(tmp_path):
    (tmp_path / 'config.yaml').write_text("site_name: Test Events\n")
    result = CliRunner().invoke(cli, ['og-images', '--site-dir', str(tmp_path)])
    assert result.exit_code != 0
    assert 'run `calgen pipeline` first' in result.output
