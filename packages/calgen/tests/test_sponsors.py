"""Weekly sponsorships on the site: the header line, the week page, the
newsletter, and /sponsor/. `_data/sponsors.json` is written by the DynamoDB
export with approved sponsorships only, so these tests write it by hand.

Run: python -m pytest tests/test_sponsors.py
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from calgen.app import create_app, sponsor_for_week
from calgen.routes.common import get_week_identifier, local_tz

SITE_DIR = Path(__file__).resolve().parents[3] / 'site'

THIS_WEEK = get_week_identifier(datetime.now(local_tz).date())
NEXT_WEEK = get_week_identifier(datetime.now(local_tz).date() + timedelta(weeks=1))
LAST_WEEK = get_week_identifier(datetime.now(local_tz).date() - timedelta(weeks=1))

ACME = {'week_id': THIS_WEEK, 'name': 'Acme <Corp>',
        'message': 'Hiring Python devs in Arlington.',
        'url': 'https://acme.example/jobs'}
NEXT = {'week_id': NEXT_WEEK, 'name': 'Globex', 'message': 'Cloud stuff.',
        'url': 'https://globex.example'}
PAST = {'week_id': LAST_WEEK, 'name': 'Initech', 'message': 'TPS reports.',
        'url': 'https://initech.example'}


def _client(monkeypatch, tmp_path, sponsors):
    (tmp_path / '_data').mkdir()
    (tmp_path / '_data' / 'sponsors.json').write_text(json.dumps(sponsors))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('calgen.routes.listings.get_events', lambda *a, **k: [])
    monkeypatch.setattr('calgen.routes.listings.get_archived_week',
                        lambda *a, **k: None)
    app = create_app(site_dir=str(SITE_DIR))
    app.testing = True
    return app.test_client()


def _get(client, path):
    response = client.get(path)
    assert response.status_code == 200, path
    return response.get_data(as_text=True)


def test_sponsor_for_week_picks_by_id():
    assert sponsor_for_week(NEXT_WEEK, [ACME, NEXT]) is NEXT
    assert sponsor_for_week('1999-W01', [ACME, NEXT]) is None


def test_header_shows_this_weeks_sponsor_escaped(monkeypatch, tmp_path):
    html = _get(_client(monkeypatch, tmp_path, [ACME, NEXT]), '/sponsor/')
    header = html.split('</header>')[0]
    assert 'Acme &lt;Corp&gt;' in header
    assert 'Hiring Python devs in Arlington.' in header
    assert 'rel="sponsored noopener"' in header
    assert 'Globex' not in header


def test_header_invites_sponsors_when_week_is_open(monkeypatch, tmp_path):
    html = _get(_client(monkeypatch, tmp_path, [NEXT]), '/sponsor/')
    header = html.split('</header>')[0]
    assert 'href="/sponsor/"' in header
    assert 'Globex' not in header


def test_week_page_shows_its_own_sponsor(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, [ACME, NEXT])
    main = _get(client, f'/week/{NEXT_WEEK}/').split('</header>', 1)[1]
    assert 'This week is sponsored' in main
    assert 'Globex' in main


def test_sponsor_page_lists_upcoming_and_past(monkeypatch, tmp_path):
    html = _get(_client(monkeypatch, tmp_path, [PAST, ACME, NEXT]), '/sponsor/')
    main = html.split('</header>', 1)[1]
    upcoming, past = main.split('Thanks to our past sponsors')
    assert 'Globex' in upcoming and 'This week' in upcoming
    assert 'Initech' in past and 'Initech' not in upcoming
    assert '/edit/sponsor.html' in main


def test_newsletter_leads_with_sponsor(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, [ACME])
    html = _get(client, '/newsletter.html')
    assert html.index('This week\'s sponsor') < html.index('</h1>') + 400
    assert 'https://acme.example/jobs' in html
    text = _get(client, '/newsletter.txt')
    assert "This week's sponsor: Acme <Corp>" in text.splitlines()[3]


def test_no_sponsors_file_is_harmless(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    app = create_app(site_dir=str(SITE_DIR))
    app.testing = True
    html = _get(app.test_client(), '/sponsor/')
    assert 'Upcoming sponsors' not in html


# ─── House ad: the slot's filler in unsponsored weeks ───────────────

HOUSE = {'name': 'Sponsor DC Tech Events!',
         'message': 'Get your message out and help support this site.',
         'url': '/sponsor/'}


@pytest.fixture
def house_client(monkeypatch, tmp_path):
    def make(sponsors):
        client = _client(monkeypatch, tmp_path, sponsors)
        cfg = {'site_name': 'DC Tech Events', 'base_url': 'https://dctech.events',
               'sponsorship_house_ad': HOUSE}
        monkeypatch.setattr('calgen.app.get_config', lambda: cfg)
        monkeypatch.setattr('calgen.routes.newsletter.get_config', lambda: cfg)
        return client
    return make


def test_house_ad_fills_header_in_unsponsored_week(house_client):
    header = _get(house_client([NEXT]), '/sponsor/').split('</header>')[0]
    assert 'Sponsor DC Tech Events!' in header
    assert 'Get your message out and help support this site.' in header
    assert 'href="/sponsor/"' in header
    assert 'rel="sponsored' not in header


def test_paid_sponsor_beats_house_ad(house_client):
    header = _get(house_client([ACME]), '/sponsor/').split('</header>')[0]
    assert 'Acme &lt;Corp&gt;' in header
    assert 'Get your message out' not in header


def test_house_ad_leads_newsletter_with_absolute_link(house_client):
    client = house_client([])
    html = _get(client, '/newsletter.html')
    assert 'href="https://dctech.events/sponsor/"' in html
    assert 'Get your message out' in html
    text = _get(client, '/newsletter.txt')
    assert 'https://dctech.events/sponsor/' in text


def test_house_ad_stays_off_week_pages(house_client):
    main = _get(house_client([]), f'/week/{NEXT_WEEK}/').split('</header>', 1)[1]
    assert 'Get your message out' not in main
    assert 'sponsored by' not in main.lower()


def test_header_slot_avoids_easylist_class(house_client):
    # EasyList hides `.header-sponsor` outright (it blanked the slot in
    # Firefox), so the line uses a neutral class instead.
    header = _get(house_client([ACME]), '/sponsor/').split('</header>')[0]
    assert 'class="masthead-note"' in header
    assert 'header-sponsor' not in header
