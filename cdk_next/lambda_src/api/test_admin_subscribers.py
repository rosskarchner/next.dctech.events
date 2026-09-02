"""Tests for GET /api/admin/subscribers' category/region preferences addition.

The endpoint itself (auth, SES contact listing) had no prior test coverage;
this focuses on the one thing this change actually touches — that each
subscriber row is enriched with their preferences, and a subscriber with
none set shows as unfiltered ("All"), not an error or an omitted field.

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_admin_subscribers.py
"""
import json
import os

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import handler  # noqa: E402
import routes.admin as admin  # noqa: E402

ADMIN = {"sub": "u-1", "email": "admin@example.com", "cognito:groups": ["admins"]}


def request(path, claims=None):
    return {
        "path": path, "httpMethod": "GET", "body": None,
        "queryStringParameters": None,
        "requestContext": {"authorizer": {"claims": dict(claims)}} if claims else {},
        "headers": {},
    }


def call(path, **kw):
    response = handler.lambda_handler(request(path, **kw), None)
    try:
        payload = json.loads(response["body"]) if response.get("body") else {}
    except (ValueError, TypeError):
        payload = {"error": response.get("body")}
    return response["statusCode"], payload


class FakeSesV2:
    def __init__(self, contacts):
        self._contacts = contacts

    def list_contacts(self, **kwargs):
        return {'Contacts': self._contacts}


def _contact(email, timestamp=None):
    return {'EmailAddress': email, 'LastUpdatedTimestamp': timestamp, 'UnsubscribeAll': False}


@pytest.fixture
def store(monkeypatch):
    # admin.py does `from db import get_subscriber_preferences`, so it
    # holds its own reference — patching db.get_subscriber_preferences
    # alone wouldn't reach get_subscribers_json's calls to it.
    monkeypatch.setattr(admin, "get_subscriber_preferences", lambda email: {
        "a@example.com": {"categories": ["ai"], "regions": ["dc"]},
    }.get(email))


def test_a_subscriber_with_preferences_shows_them(store, monkeypatch):
    monkeypatch.setattr(
        "boto3.client", lambda name, **kw: FakeSesV2([_contact("a@example.com")]))
    status, payload = call("/api/admin/subscribers", claims=ADMIN)
    assert status == 200
    assert payload["subscribers"][0]["categories"] == ["ai"]
    assert payload["subscribers"][0]["regions"] == ["dc"]


def test_a_subscriber_with_no_preferences_shows_empty_not_missing(store, monkeypatch):
    monkeypatch.setattr(
        "boto3.client", lambda name, **kw: FakeSesV2([_contact("nofilter@example.com")]))
    status, payload = call("/api/admin/subscribers", claims=ADMIN)
    assert status == 200
    assert payload["subscribers"][0]["categories"] == []
    assert payload["subscribers"][0]["regions"] == []


def test_requires_admin(monkeypatch):
    monkeypatch.setattr("boto3.client", lambda name, **kw: FakeSesV2([]))
    status, _ = call("/api/admin/subscribers", claims={
        "sub": "u-2", "email": "nobody@example.com", "cognito:groups": []})
    assert status == 403
