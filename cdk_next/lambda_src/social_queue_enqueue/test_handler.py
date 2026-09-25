"""Tests for the new-event social queue enqueuer.

Run: python -m pytest test_handler.py
"""
import os

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import handler  # noqa: E402


def _record(pk, *, event_name="INSERT", sk="META", image=True, extra=None):
    record = {"eventName": event_name,
              "dynamodb": {"Keys": {"PK": {"S": pk}, "SK": {"S": sk}}}}
    if image:
        body = {"PK": {"S": pk}, "SK": {"S": sk}, "title": {"S": "T"},
                "createdAt": {"S": "2026-09-01T12:00:00Z"}}
        body.update(extra or {})
        record["dynamodb"]["NewImage"] = body
    return record


def test_only_new_events_are_selected():
    records = [
        _record("EVENT#abc", event_name="INSERT"),
        _record("EVENT#def", event_name="MODIFY"),  # iCal re-sync / correction overlay
        _record("UPDATE#2026-W33", event_name="INSERT"),
        _record("EVENT#ghi", event_name="REMOVE", image=False),
    ]
    got = [i["PK"] for i in handler._events_from_stream(records)]
    assert got == ["EVENT#abc"]


def test_ignores_non_meta_rows():
    assert handler._events_from_stream([_record("EVENT#abc", sk="OVERRIDE#2026-09-01")]) == []


def test_deserializes_the_new_image():
    item = handler._events_from_stream([_record("EVENT#abc")])[0]
    assert item["title"] == "T"
    assert item["createdAt"] == "2026-09-01T12:00:00Z"


class _FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None):
        key = (Item["PK"], Item["SK"])
        if ConditionExpression == "attribute_not_exists(PK)" and key in self.items:
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException",
                            "Message": "exists"}},
                "PutItem",
            )
        self.items[key] = Item


def test_enqueue_writes_a_pending_queue_item():
    table = _FakeTable()
    event = {"PK": "EVENT#abc123", "createdAt": "2026-09-01T12:00:00Z"}
    assert handler._enqueue(table, event) == "queued"
    item = table.items[("SOCIALQUEUE#abc123", "META")]
    assert item["GSI1PK"] == "SOCIALQUEUE#PENDING"
    assert item["GSI1SK"] == "2026-09-01T12:00:00Z"
    assert item["status"] == "pending"
    assert item["event_guid"] == "abc123"


def test_enqueue_is_idempotent_on_retry():
    table = _FakeTable()
    event = {"PK": "EVENT#abc123", "createdAt": "2026-09-01T12:00:00Z"}
    assert handler._enqueue(table, event) == "queued"
    assert handler._enqueue(table, event) == "already_queued"
    assert len(table.items) == 1
