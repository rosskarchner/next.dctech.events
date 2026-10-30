"""Drain the new-event social queue, one item per hour.

Runs on an hourly EventBridge schedule limited to roughly 6am-midnight
Eastern (cdk_next/stacks/updates_stack.py) — a fixed UTC cron, so the window
drifts by an hour across the DST transition; that's accepted, not a bug.

Each invocation looks at the oldest CANDIDATE_LIMIT pending SOCIALQUEUE#{guid}
items (a GSI1 query on the constant partition SOCIALQUEUE#PENDING — see
lambda_src/social_queue_enqueue/handler.py for how items get there), resolves
any that are not worth announcing, and posts exactly one of the rest to
Mastodon and Bluesky. Items are resolved without posting when:

* the EVENT# item it points at has been deleted since it was queued, or
* the event's date (and time, if it has one) is already in the past by the
  time it comes up — never announce something over, or
* it is a virtual event (the enqueuer already drops these; this catches ones
  queued before that, or reclassified since).

Which one gets posted is not plain FIFO. A feed that lands dozens of events in
one sync would otherwise hold the queue for days, so the worker prefers an
event whose group is not among the last RECENT_GROUP_WINDOW groups posted
(tracked on a SOCIALQUEUE#STATE item), and among those the soonest event
first. If every candidate is from a recently-posted group it just takes the
soonest one, so the queue always drains.

Posting reuses the same Mastodon/Bluesky HTTP clients and Secrets Manager
credentials as the /updates cross-poster (lambda_src/social_publisher/), and
the same per-network progress idea as its SOCIAL# dedupe record — except here
the queue item *is* the progress record, since there is exactly one of it per
event.

Each network drains the queue independently: every run, each network picks
its own next item from the pending ones it has not yet posted (or given up
on), so an outage on one network never slows the other below one post per
hour. A network keeps retrying the item it already started on — tracked as
{network}_attempts — and gives up on it ({network}_failed) after
MAX_ATTEMPTS hourly tries rather than blocking its own queue forever. An item
leaves the pending index once every network has either posted it or given
up: status=posted if all posted, failed if all gave up, partial otherwise.

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

# One post attempt per network per hour, so a permanently-broken item (e.g.
# a network outage lasting a day) does not wedge that network's queue forever.
MAX_ATTEMPTS = 8

# (network, the field whose presence means "already posted there").
NETWORKS = (("mastodon", "mastodon_id"), ("bluesky", "bluesky_uri"))

# How many of the oldest pending items to consider per run, and how many of
# the most recently posted groups to steer away from.
CANDIDATE_LIMIT = 100
RECENT_GROUP_WINDOW = 3
STATE_PK = "SOCIALQUEUE#STATE"

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


def _pending(table):
    resp = table.query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq("SOCIALQUEUE#PENDING"),
        ScanIndexForward=True,
        Limit=CANDIDATE_LIMIT,
    )
    return resp.get("Items", [])


def _recent_groups(table):
    item = table.get_item(Key={"PK": STATE_PK, "SK": "META"}).get("Item") or {}
    return list(item.get("recent_groups") or [])


def _remember_group(table, group):
    """Record a posted group, keeping only the newest RECENT_GROUP_WINDOW."""
    if not group:
        return
    recent = [g for g in _recent_groups(table) if g != group] + [group]
    _save_progress(table, STATE_PK, {"recent_groups": recent[-RECENT_GROUP_WINDOW:]})


def _event_sort_key(event):
    return (str(event.get("date") or ""), str(event.get("time") or ""))


def _choose(candidates, recent):
    """candidates: [(queue_item, event)] in enqueue order. Soonest event from a
    group not posted recently; else the soonest overall. Ties keep the
    enqueue order (sorted() is stable)."""
    # Events with no group (manual/submitted) are never "recent".
    fresh = [c for c in candidates
             if not c[1].get("group") or c[1]["group"] not in recent]
    pool = fresh or candidates
    return sorted(pool, key=lambda c: _event_sort_key(c[1]))[0]


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


def _bump_attempts(table, queue_pk, network):
    field = f"{network}_attempts"
    resp = table.update_item(
        Key={"PK": queue_pk, "SK": "META"},
        UpdateExpression="SET #a = if_not_exists(#a, :zero) + :one",
        ExpressionAttributeNames={"#a": field},
        ExpressionAttributeValues={":zero": 0, ":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"][field])


def _needs(queue_item, network, done_field):
    return not queue_item.get(done_field) and not queue_item.get(f"{network}_failed")


def _settled(queue_item):
    """None while any network still owes this item a post, else its status."""
    if any(_needs(queue_item, n, f) for n, f in NETWORKS):
        return None
    posted = [n for n, f in NETWORKS if queue_item.get(f)]
    if len(posted) == len(NETWORKS):
        return "posted"
    return "partial" if posted else "failed"


def _choose_for(network, candidates, recent):
    """Finish the item this network is already retrying before starting a
    new one, so its MAX_ATTEMPTS budget is spent on one item at a time."""
    retrying = [c for c in candidates if c[0].get(f"{network}_attempts")]
    if retrying:
        return sorted(retrying, key=lambda c: _event_sort_key(c[1]))[0]
    return _choose(candidates, recent)


def _post(network, queue_item, event, url):
    queue_pk = queue_item["PK"]
    if network == "mastodon":
        posted = networks.mastodon_post(
            _secret(MASTODON_SECRET_NAME),
            compose_event_post(event, url, MASTODON_CHAR_LIMIT),
            idempotency_key=queue_pk,
        )
        return posted, {"mastodon_id": posted["id"],
                        "mastodon_url": posted["url"]}
    posted = networks.bluesky_post(
        _secret(BLUESKY_SECRET_NAME),
        compose_event_post(event, url, networks.BLUESKY_CHAR_LIMIT),
        link=url, title=str(event.get("title") or ""),
        description=_format_when(event),
    )
    return posted, {"bluesky_uri": posted["uri"], "bluesky_url": posted["url"]}


def _process_one(table):
    pending = _pending(table)
    if not pending:
        return {"status": "empty"}

    viable = []
    first_skip = None
    for queue_item in pending:
        queue_pk = queue_item["PK"]
        guid = str(queue_item.get("event_guid") or queue_pk.split("#", 1)[1])
        event = _load_event(table, guid)
        if not event:
            reason = "event no longer exists"
        elif event.get("location_type") == "virtual":
            reason = "virtual event"
        elif _is_past(event):
            reason = "event date has passed"
        else:
            viable.append((queue_item, event))
            continue
        _resolve(table, queue_pk, "skipped")
        first_skip = first_skip or {"status": "skipped", "pk": queue_pk,
                                    "reason": reason}

    if not viable:
        return first_skip

    recent = _recent_groups(table)
    results = {}
    touched = {}
    for network, done_field in NETWORKS:
        candidates = [c for c in viable if _needs(c[0], network, done_field)]
        if not candidates:
            continue
        queue_item, event = _choose_for(network, candidates, recent)
        queue_pk = queue_item["PK"]
        touched[queue_pk] = (queue_item, event)
        url = f"{SITE_BASE_URL}/events/{event_utils.event_slug(event)}/"
        try:
            posted, fields = _post(network, queue_item, event, url)
        except Exception as exc:  # noqa: BLE001 — one network must not sink the other
            print(f"ERROR posting {queue_pk} to {network}: {exc}")
            attempts = _bump_attempts(table, queue_pk, network)
            queue_item[f"{network}_attempts"] = attempts
            result = {"status": "retry", "pk": queue_pk, "error": str(exc),
                      "attempts": attempts}
            if attempts >= MAX_ATTEMPTS:
                _save_progress(table, queue_pk, {f"{network}_failed": True})
                queue_item[f"{network}_failed"] = True
                result["status"] = "failed"
            results[network] = result
            continue

        _save_progress(table, queue_pk, fields)
        queue_item.update(fields)
        print(f"Posted {queue_pk} to {network}: {posted.get('url')}")
        results[network] = {"status": "posted", "pk": queue_pk, "url": url}

    if not results:
        return first_skip or {"status": "empty"}

    posted_groups = []
    for queue_pk, (queue_item, event) in touched.items():
        status = _settled(queue_item)
        if status:
            _resolve(table, queue_pk, status)
        group = str(event.get("group") or "")
        if (any(r["status"] == "posted" and r["pk"] == queue_pk
                for r in results.values())
                and group not in posted_groups):
            posted_groups.append(group)
    for group in posted_groups:
        _remember_group(table, group)

    statuses = {r["status"] for r in results.values()}
    overall = statuses.pop() if len(statuses) == 1 else "partial"
    return {"status": overall, "networks": results}


def lambda_handler(event, context):
    table = _dynamodb.Table(TABLE_NAME)
    result = _process_one(table)
    print(json.dumps(result, default=str))
    return result
