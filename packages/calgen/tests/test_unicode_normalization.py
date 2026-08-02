"""Tests for Unicode normalization in title handling."""
import pytest

from calgen.event_utils import _normalize_title
from calgen.pipeline import are_events_duplicates


# ---------------------------------------------------------------------------
# _normalize_title
# ---------------------------------------------------------------------------

def test_normalize_title_strips_zero_width_space():
    assert _normalize_title("COMMUNITY TUESDAYS \u200bAT REFRACTION") == "COMMUNITY TUESDAYS AT REFRACTION"


def test_normalize_title_strips_zero_width_non_joiner():
    assert _normalize_title("Hello\u200cWorld") == "HelloWorld"


def test_normalize_title_strips_zero_width_joiner():
    assert _normalize_title("Hello\u200dWorld") == "HelloWorld"


def test_normalize_title_strips_bom():
    assert _normalize_title("\ufeffTitle") == "Title"


def test_normalize_title_strips_soft_hyphen():
    assert _normalize_title("soft\u00adhyphen") == "softhyphen"


def test_normalize_title_collapses_whitespace():
    assert _normalize_title("  too   many   spaces  ") == "too many spaces"


def test_normalize_title_plain_string_unchanged():
    assert _normalize_title("Normal Title") == "Normal Title"


def test_normalize_title_empty_string():
    assert _normalize_title("") == ""


# ---------------------------------------------------------------------------
# are_events_duplicates – zero-width space
# ---------------------------------------------------------------------------

_BASE = {'date': '2026-07-25', 'time': '18:00', 'url': ''}


def test_duplicates_zero_width_space_in_one_title():
    e1 = dict(_BASE, title="COMMUNITY TUESDAYS \u200bAT REFRACTION")
    e2 = dict(_BASE, title="COMMUNITY TUESDAYS AT REFRACTION")
    assert are_events_duplicates(e1, e2)


def test_duplicates_case_insensitive():
    e1 = dict(_BASE, title="Community Tuesdays at Refraction")
    e2 = dict(_BASE, title="COMMUNITY TUESDAYS AT REFRACTION")
    assert are_events_duplicates(e1, e2)


def test_duplicates_case_and_zero_width_space():
    e1 = dict(_BASE, title="Community Tuesdays\u200b at Refraction")
    e2 = dict(_BASE, title="COMMUNITY TUESDAYS AT REFRACTION")
    # strip() + lower() on both sides handles this
    assert are_events_duplicates(e1, e2)


def test_not_duplicates_different_titles():
    e1 = dict(_BASE, title="Event A")
    e2 = dict(_BASE, title="Event B")
    assert not are_events_duplicates(e1, e2)


def test_not_duplicates_different_dates():
    e1 = dict(_BASE, title="Event A", date='2026-07-25')
    e2 = dict(_BASE, title="Event A", date='2026-07-26')
    assert not are_events_duplicates(e1, e2)


def test_not_duplicates_different_times():
    e1 = dict(_BASE, title="Event A", time='18:00')
    e2 = dict(_BASE, title="Event A", time='19:00')
    assert not are_events_duplicates(e1, e2)
