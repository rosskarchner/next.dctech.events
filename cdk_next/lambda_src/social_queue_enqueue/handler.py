"""Queue newly-published events for the hourly social-media worker.

Fed by the events table's stream, filtered at the event source mapping to
INSERT records under EVENT# (see cdk_next/stacks/updates_stack.py). A
PutItem on an *existing* key streams as MODIFY, so that filter alone already
excludes the iCal aggregator's routine re-sync of an event it has seen
before, and correction overlays (which use UpdateItem) — no per-caller
changes to db.put_event() needed to tell "genuinely new" from "touched
again".

This Lambda only ever writes a SOCIALQUEUE#{guid} placeholder; it never talks
to Mastodon or Bluesky. See lambda_src/social_queue_worker/app.py for what
happens to a queued item — one is drained per hour, oldest first, via a GSI1
query on the constant partition SOCIALQUEUE#PENDING (the same "constant PK,
sortable SK" idiom GSI4PK='EVT#ACTIVE' already uses for listing events).

SOCIALQUEUE# is deliberately absent from the site build trigger's
RELEVANT_PREFIXES (lambda_src/site_generator/trigger/handler.py), so writing
it back does not kick off a rebuild.
"""
import os
import time

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

TABLE_NAME = os.environ["DYNAMODB_TABLE_NAME"]

_dynamodb = boto3.resource("dynamodb")
_deserializer = TypeDeserializer()


def _events_from_stream(records):
    items = []
    for record in records:
        if record.get("eventName") != "INSERT":
            continue
        keys = record.get("dynamodb", {}).get("Keys", {})
        pk = keys.get("PK", {}).get("S", "")
        if not pk.startswith("EVENT#"):
            continue
        if keys.get("SK", {}).get("S") != "META":
            continue
        image = record.get("dynamodb", {}).get("NewImage")
        if not image:
            continue
        items.append({k: _deserializer.deserialize(v) for k, v in image.items()})
    return items


def _enqueue(table, event):
    guid = str(event.get("PK", "")).split("#", 1)[1]
    created_at = str(event.get("createdAt") or
                      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        table.put_item(
            Item={
                "PK": f"SOCIALQUEUE#{guid}",
                "SK": "META",
                "GSI1PK": "SOCIALQUEUE#PENDING",
                "GSI1SK": created_at,
                "event_guid": guid,
                "status": "pending",
                "attempts": 0,
                "enqueued_at": created_at,
            },
            # A retried stream batch (ESM retry_attempts) must not reset an
            # in-progress or already-resolved queue item back to pending.
            ConditionExpression="attribute_not_exists(PK)",
        )
        return "queued"
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "already_queued"
        raise


def lambda_handler(event, context):
    records = (event or {}).get("Records", [])
    table = _dynamodb.Table(TABLE_NAME)
    results = [_enqueue(table, e) for e in _events_from_stream(records)]
    return {"processed": len(results), "queued": results.count("queued")}
