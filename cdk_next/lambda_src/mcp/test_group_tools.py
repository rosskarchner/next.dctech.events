"""Tests for the MCP group tools.

Run: python -m pytest test_group_tools.py
"""
import os
import sys

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

import server  # noqa: E402

GROUP = {"id": "station-dc", "name": "STATION DC", "active": True,
         "website": "https://stationdc.com", "ical": "https://luma.com/ics",
         "categories": ["startups"]}


@pytest.fixture
def puts(monkeypatch):
    written = []
    monkeypatch.setattr(server.db, "get_group",
                        lambda slug: dict(GROUP) if slug == "station-dc" else None)
    monkeypatch.setattr(server.db, "put_group",
                        lambda slug, data: written.append((slug, data)))
    return written


def test_set_skip_phrases_keeps_other_fields(puts):
    out = server.set_group_skip_phrases("station-dc", [" Members Only ", ""])

    assert out == {"slug": "station-dc", "skip_phrases": ["Members Only"]}
    ((slug, data),) = puts
    assert slug == "station-dc"
    assert data["skip_phrases"] == ["Members Only"]
    assert data["ical"] == GROUP["ical"] and data["categories"] == ["startups"]
    assert "id" not in data


def test_set_skip_phrases_rejects_non_strings(puts):
    with pytest.raises(ValueError):
        server.set_group_skip_phrases("station-dc", [1])
    assert puts == []


def test_set_skip_phrases_unknown_group(puts):
    with pytest.raises(Exception):
        server.set_group_skip_phrases("nope", ["x"])


@pytest.fixture
def valid_slugs(monkeypatch):
    monkeypatch.setattr(server.db, "valid_category_slugs",
                        lambda: {"startups", "social"})


def test_set_category_rules_cleans_and_keeps_other_fields(puts, valid_slugs):
    out = server.set_group_category_rules(
        "station-dc", [{"phrase": " Mixer ", "categories": ["social"]}])

    assert out["category_rules"] == [{"phrase": "Mixer", "categories": ["social"]}]
    ((_, data),) = puts
    assert data["category_rules"] == out["category_rules"]
    assert data["categories"] == ["startups"]


@pytest.mark.parametrize("rule", [
    {"phrase": "", "categories": ["social"]},
    {"phrase": "x", "categories": []},
    {"phrase": "x", "categories": ["tech-culture"]},
    "x",
])
def test_set_category_rules_rejects_bad_rules(puts, valid_slugs, rule):
    with pytest.raises(ValueError):
        server.set_group_category_rules("station-dc", [rule])
    assert puts == []


def test_set_group_categories_validates(puts, valid_slugs):
    with pytest.raises(ValueError):
        server.set_group_categories("station-dc", ["nope"])
    server.set_group_categories("station-dc", ["social"])
    assert puts[0][1]["categories"] == ["social"]
