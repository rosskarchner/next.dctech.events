"""Tests for event_link: which URL an iCal event links to on the site."""
import icalendar
import pytest

from calgen.calendars import event_link


def _vevent(**props):
    event = icalendar.Event()
    for key, value in props.items():
        event.add(key, value)
    return event


def test_an_https_url_property_is_used():
    assert event_link(_vevent(url='https://example.com/e')) == 'https://example.com/e'


@pytest.mark.parametrize('url', [
    'javascript:alert(document.cookie)',
    ' JavaScript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'vbscript:msgbox(1)',
])
def test_a_non_http_url_property_is_ignored(url):
    group = {'website': 'https://group.example'}
    assert event_link(_vevent(url=url), group) == 'https://group.example'


def test_falls_back_to_a_link_in_the_description():
    event = _vevent(url='javascript:alert(1)', description='RSVP at https://rsvp.example/x')
    assert event_link(event) == 'https://rsvp.example/x'


def test_no_url_and_no_group_is_empty():
    assert event_link(_vevent()) == ''
