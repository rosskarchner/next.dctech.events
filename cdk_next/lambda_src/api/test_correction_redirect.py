"""Tests for the magic-link redirect_path allowlist.

/api/submit-link is unauthenticated and mails whatever link it builds, so a
client-supplied redirect_path must never be able to turn this into an open
redirect or inject content into an emailed link — see
routes/submit.py:_sanitize_redirect_path.

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_correction_redirect.py
"""
import os

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

from routes.submit import _sanitize_redirect_path  # noqa: E402


def test_no_redirect_path_falls_back_to_the_submission_form():
    assert _sanitize_redirect_path(None) == '/edit/submit-event.html'
    assert _sanitize_redirect_path('') == '/edit/submit-event.html'


def test_the_correction_form_is_allowed():
    assert _sanitize_redirect_path('/edit/correct-event.html') == \
        '/edit/correct-event.html'


def test_a_valid_guid_is_carried_through():
    assert _sanitize_redirect_path('/edit/correct-event.html?guid=abc123ef') == \
        '/edit/correct-event.html?guid=abc123ef'


def test_a_full_32_hex_guid_is_carried_through():
    guid = 'f820a76b93b0ef5c21448d988e152c4b'
    assert _sanitize_redirect_path(f'/edit/correct-event.html?guid={guid}') == \
        f'/edit/correct-event.html?guid={guid}'


def test_a_malformed_guid_is_dropped_not_rejected():
    # A bad param must not cost the whole request — fall back to the bare
    # correction form rather than erroring.
    assert _sanitize_redirect_path('/edit/correct-event.html?guid=<script>') == \
        '/edit/correct-event.html'


def test_an_absolute_url_falls_back_rather_than_being_echoed():
    assert _sanitize_redirect_path('https://evil.example/phish') == \
        '/edit/submit-event.html'


def test_a_scheme_relative_url_falls_back():
    assert _sanitize_redirect_path('//evil.example/phish') == \
        '/edit/submit-event.html'


def test_an_unrecognized_path_falls_back():
    assert _sanitize_redirect_path('/edit/some-other-page.html') == \
        '/edit/submit-event.html'


def test_extra_query_params_on_the_correction_path_do_not_leak_through():
    # Only guid is ever extracted; nothing else on the query string reaches
    # the built link.
    result = _sanitize_redirect_path(
        '/edit/correct-event.html?guid=abc123ef&redirect=https://evil.example')
    assert result == '/edit/correct-event.html?guid=abc123ef'
