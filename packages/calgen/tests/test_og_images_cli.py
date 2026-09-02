"""`calgen og-images` — generates social share cards (events, category pages,
week pages, /updates/ posts) run between `calgen pipeline` and `calgen
build` in the site-generator buildspec.

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


def test_passes_the_events_group_through_to_the_card(tmp_path, monkeypatch):
    events = [{
        'title': 'Intro to Rust', 'date': '2026-09-01', 'time': '18:00',
        'guid': 'eee555', 'group': 'DC Rust',
    }]
    _write_site(tmp_path, events)

    calls = []
    import calgen.cli as cli_module

    def fake_render(*args, **kwargs):
        calls.append(kwargs)
        return Image.new('RGB', (1200, 630))

    # og-images imports render_event_card into its own function body at call
    # time (`from calgen.og_image import render_event_card`), so patching
    # the source module — not calgen.cli — is what actually takes effect.
    monkeypatch.setattr('calgen.og_image.render_event_card', fake_render)

    result = CliRunner().invoke(cli_module.cli, ['og-images', '--site-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert calls and calls[0]['group_name'] == 'DC Rust'


def test_generates_a_card_for_every_category(tmp_path):
    events = [{
        'title': 'AI Meetup', 'date': '2026-09-01', 'time': '18:00', 'guid': 'fff666',
        'categories': ['ai'],
    }]
    _write_site(tmp_path, events, categories={'ai': {'name': 'Artificial Intelligence'}})

    result = CliRunner().invoke(cli, ['og-images', '--site-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output

    png_path = tmp_path / 'static' / 'og' / 'category-ai.png'
    assert png_path.exists()
    with Image.open(png_path) as img:
        assert img.size == (1200, 630)


def test_generates_at_least_one_week_card(tmp_path):
    # get_all_week_ids() is relative to "today", so this can't assert an
    # exact week id — just that the upcoming-weeks half of it (always
    # non-empty) produced real files.
    _write_site(tmp_path, [])

    result = CliRunner().invoke(cli, ['og-images', '--site-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output

    week_cards = list((tmp_path / 'static' / 'og').glob('week-*.png'))
    assert week_cards
    with Image.open(week_cards[0]) as img:
        assert img.size == (1200, 630)


def test_generates_a_card_for_a_free_form_post(tmp_path):
    _write_site(tmp_path, [])
    (tmp_path / '_posts').mkdir()
    (tmp_path / '_posts' / 'welcome.yaml').write_text(
        "slug: welcome\n"
        "title: Welcome to DC Tech Events\n"
        "published_on: 2026-08-01\n"
        "status: published\n"
        "body: Hello there.\n"
    )

    result = CliRunner().invoke(cli, ['og-images', '--site-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output

    png_path = tmp_path / 'static' / 'og' / 'post-welcome.png'
    assert png_path.exists()
    with Image.open(png_path) as img:
        assert img.size == (1200, 630)


def test_generates_a_card_for_a_weekly_roundup_post(tmp_path):
    _write_site(tmp_path, [])
    (tmp_path / '_updates').mkdir()
    (tmp_path / '_updates' / '2026-09-15.yaml').write_text(
        "week_id: 2026-W38\n"
        "published_on: 2026-09-15\n"
        "title: This week in DC tech\n"
        "events: []\n"
    )

    result = CliRunner().invoke(cli, ['og-images', '--site-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output

    png_path = tmp_path / 'static' / 'og' / 'post-2026-09-15.png'
    assert png_path.exists()
    with Image.open(png_path) as img:
        assert img.size == (1200, 630)


def test_a_link_post_does_not_get_its_own_card(tmp_path):
    # A link post's page IS the week page it points at (no page of its own),
    # so it must not produce a post-*.png that nothing ever links to.
    _write_site(tmp_path, [])
    (tmp_path / '_updates').mkdir()
    (tmp_path / '_updates' / '2026-09-15.yaml').write_text(
        "week_id: 2026-W38\n"
        "published_on: 2026-09-15\n"
        "title: The week ahead\n"
        "post_kind: link\n"
    )

    result = CliRunner().invoke(cli, ['og-images', '--site-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / 'static' / 'og' / 'post-2026-09-15.png').exists()


def test_missing_all_events_json_errors_instead_of_crashing(tmp_path):
    (tmp_path / 'config.yaml').write_text("site_name: Test Events\n")
    result = CliRunner().invoke(cli, ['og-images', '--site-dir', str(tmp_path)])
    assert result.exit_code != 0
    assert 'run `calgen pipeline` first' in result.output
