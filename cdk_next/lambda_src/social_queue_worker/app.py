"""Drain the new-event social queue, one item per hour.

Runs on an hourly EventBridge schedule limited to roughly 6am-midnight
Eastern (cdk_next/stacks/updates_stack.py) — a fixed UTC cron, so the window
drifts by an hour across the DST transition; that's accepted, not a bug.

Each invocation grabs the single oldest pending SOCIALQUEUE#{guid} item (a
GSI1 query on the constant partition SOCIALQUEUE#PENDING, oldest
GSI1SK first — see lambda_src/social_queue_enqueue/handler.py for how items
get there) and either posts it to Mastodon and Bluesky, or resolves it
without posting:

* the EVENT# item it points at has been deleted since it was queued, or
* the event's date (and time, if it has one) is already in the past by the
  time it reaches the front of the queue — never announce something over.

Posting reuses the same Mastodon/Bluesky HTTP clients and Secrets Manager
credentials as the /updates cross-poster (lambda_src/social_publisher/), and
the same per-network progress idea as its SOCIAL# dedupe record — except here
the queue item *is* the progress record, since there is exactly one of it per
event. A network that already has an id/uri recorded is skipped on retry; a
failure on one network never blocks the other. A queue item that keeps
failing is abandoned (status=failed) after MAX_ATTEMPTS hourly tries rather
than permanently blocking the one-item-per-hour queue behind it.

Manual invoke, to drain the queue without waiting for the next hour:

    {}
"""
import json
import os
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key

import event_utils
import networks

TABLE_NAME = os.environ["DYNAMODB_TABLE_NAME"]
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://dctech.events").rstrip("/")
MASTODON_SECRET_NAME = os.environ["MASTODON_SECRET_NAME"]
BLUESKY_SECRET_NAME = os.environ["BLUESKY_SECRET_NAME"]
MASTODON_CHAR_LIMIT = int(os.environ.get("MASTODON_CHAR_LIMIT", "500"))

# One post attempt per hour, so a permanently-broken item (e.g. a network
# outage lasting a day) does not wedge every event behind it forever.
MAX_ATTEMPTS = 8

EASTERN = ZoneInfo("America/New_York")

_dynamodb = boto3.resource("dynamodb")
_secrets = boto3.client("secretsmanager")
_secret_cache = {}


def _secret(name):
    if name not in _secret_cache:
        value = _secrets.get_secret_value(SecretId=name)["SecretString"]
        _secret_cache[name] = json.loads(value)
    return _secret_cache[name]


# ── composing the post ──────────────────────────────────────────────


def _clip(text, limit):
    """Truncate on a word boundary with an ellipsis, never mid-word."""
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return (clipped or text[: limit - 1].rstrip()) + "…"


def _format_when(event):
    """'Thursday, October 1, 2026' or '... at 6:30 PM' if there's a real time."""
    date_str = str(event.get("date") or "")
    try:
        when = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return date_str
    formatted = when.strftime("%A, %B %-d, %Y")
    time_str = event.get("time")
    if event_utils.has_specific_time(time_str):
        try:
            t = datetime.strptime(time_str, "%H:%M").time()
        except ValueError:
            return formatted
        formatted += " at " + t.strftime("%-I:%M %p")
    return formatted


def compose_event_post(event, url, limit):
    """Title, then date/group detail, then the link — same "drop the detail
    before the link" shape as social_publisher.compose()."""
    title = str(event.get("title") or "Untitled event").strip()
    detail_lines = [_format_when(event)]
    group = str(event.get("group") or "").strip()
    if group:
        detail_lines.append(f"Hosted by {group}")
    detail = "\n".join(line for line in detail_lines if line)

    clipped_title = _clip(title, max(limit - len(url) - 2, 1))
    head = f"{clipped_title}\n\n{url}"
    room = limit - len(head) - 2
    if detail and room >= 40:
        return f"{clipped_title}\n\n{_clip(detail, room)}\n\n{url}"
    return head


# ── past-event check ────────────────────────────────────────────────


def _is_past(event, now=None):
    now = now or datetime.now(EASTERN)
    date_str = str(event.get("date") or "")
    try:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return True  # no usable date - nothing to safely announce

    time_str = event.get("time")
    if event_utils.has_specific_time(time_str):
        try:
            t = datetime.strptime(time_str, "%H:%M").time()
        except ValueError:
            t = dt_time.min
        start = datetime.combine(event_date, t, tzinfo=EASTERN)
        return start < now
    return event_date < now.date()


# ── the queue ────────────────────────────────────────────────────────


def _oldest_pending(table):
    resp = table.query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq("SOCIALQUEUE#PENDING"),
        ScanIndexForward=True,
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _load_event(table, guid):
    return table.get_item(Key={"PK": f"EVENT#{guid}", "SK": "META"}).get("Item")


def _resolve(table, queue_pk, status):
    """Mark a queue item done (posted/skipped/failed) and drop it out of the
    pending index so it is never picked up again."""
    table.update_item(
        Key={"PK": queue_pk, "SK": "META"},
        UpdateExpression="SET #status = :status REMOVE GSI1PK, GSI1SK",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": status},
    )


def _save_progress(table, queue_pk, fields):
    """Merge one network's result in, leaving the other network's alone —
    same shape as social_publisher._save_record."""
    names = {}
    values = {}
    sets = []
    for i, (field, value) in enumerate(sorted(fields.items())):
        names[f"#f{i}"] = field
        values[f":f{i}"] = value
        sets.append(f"#f{i} = :f{i}")
    table.update_item(
        Key={"PK": queue_pk, "SK": "META"},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _bump_attempts(table, queue_pk):
    resp = table.update_item(
        Key={"PK": queue_pk, "SK": "META"},
        UpdateExpression="SET attempts = if_not_exists(attempts, :zero) + :one",
        ExpressionAttributeValues={":zero": 0, ":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["attempts"])


def _process_one(table):
    queue_item = _oldest_pending(table)
    if not queue_item:
        return {"status": "empty"}

    queue_pk = queue_item["PK"]
    guid = str(queue_item.get("event_guid") or queue_pk.split("#", 1)[1])

    event = _load_event(table, guid)
    if not event:
        _resolve(table, queue_pk, "skipped")
        return {"status": "skipped", "pk": queue_pk,
                "reason": "event no longer exists"}

    if _is_past(event):
        _resolve(table, queue_pk, "skipped")
        return {"status": "skipped", "pk": queue_pk,
                "reason": "event date has passed"}

    url = f"{SITE_BASE_URL}/events/{event_utils.event_slug(event)}/"
    texts = {
        "mastodon": compose_event_post(event, url, MASTODON_CHAR_LIMIT),
        "bluesky": compose_event_post(event, url, networks.BLUESKY_CHAR_LIMIT),
    }

    errors = {}
    for network, already in (("mastodon", "mastodon_id"),
                             ("bluesky", "bluesky_uri")):
        if queue_item.get(already):
            continue
        try:
            if network == "mastodon":
                posted = networks.mastodon_post(
                    _secret(MASTODON_SECRET_NAME), texts["mastodon"],
                    idempotency_key=queue_pk,
                )
                fields = {"mastodon_id": posted["id"],
                          "mastodon_url": posted["url"]}
            else:
                posted = networks.bluesky_post(
                    _secret(BLUESKY_SECRET_NAME), texts["bluesky"],
                    link=url, title=str(event.get("title") or ""),
                    description=_format_when(event),
                )
                fields = {"bluesky_uri": posted["uri"],
                          "bluesky_url": posted["url"]}
        except Exception as exc:  # noqa: BLE001 — one network must not sink the other
            print(f"ERROR posting {queue_pk} to {network}: {exc}")
            errors[network] = str(exc)
            continue

        _save_progress(table, queue_pk, fields)
        print(f"Posted {queue_pk} to {network}: {posted.get('url')}")

    if errors:
        attempts = _bump_attempts(table, queue_pk)
        if attempts >= MAX_ATTEMPTS:
            _resolve(table, queue_pk, "failed")
            return {"status": "failed", "pk": queue_pk, "errors": errors,
                     "attempts": attempts}
        return {"status": "retry", "pk": queue_pk, "errors": errors,
                 "attempts": attempts}

    _resolve(table, queue_pk, "posted")
    return {"status": "posted", "pk": queue_pk, "url": url}


def lambda_handler(event, context):
    table = _dynamodb.Table(TABLE_NAME)
    result = _process_one(table)
    print(json.dumps(result, default=str))
    return result
