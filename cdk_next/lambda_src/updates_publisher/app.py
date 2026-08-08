"""Publish the weekly /updates post.

Runs Monday morning. Snapshots the current ISO week's events into a single
`UPDATE#{week_id}` DynamoDB item; the table's stream then triggers the usual
debounced site rebuild, which renders /updates/<y>/<m>/<d>/ within ~90s.

Why a snapshot instead of rendering the week live: calgen's pipeline and
`get_events()` both drop events dated before today, so a post rendered from
live data would empty out as its week receded. Freezing the listing at publish
time is what makes the archive permanent.

Event data comes from the site's own published events.json rather than from a
re-implementation of the pipeline — that file is already the exact set of
events the site is showing, so the post can never disagree with the calendar.
"""
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

TABLE_NAME = os.environ["DYNAMODB_TABLE_NAME"]
EVENTS_URL = os.environ.get("EVENTS_URL", "https://dctech.events/events.json")

# Fields worth freezing; anything else is presentation the templates re-derive.
SNAPSHOT_FIELDS = (
    "title", "date", "time", "url", "group", "group_website",
    "location", "categories", "is_recurring",
)

_dynamodb = boto3.resource("dynamodb")


def _week_bounds(today):
    """Monday and Sunday of the ISO week containing `today`."""
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def _week_id(day):
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _fetch_events():
    req = urllib.request.Request(
        EVENTS_URL, headers={"User-Agent": "dctech-events-updates-publisher"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
        return json.loads(resp.read().decode("utf-8"))


def _events_for_week(events, week_start, week_end):
    """Events overlapping the week, ordered by date then time.

    Mirrors calgen's filter_events_by_week: a multi-day event counts if its
    span intersects the week at all, not only if it starts inside it.
    """
    selected = []
    for event in events:
        try:
            start = datetime.strptime(event["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError, TypeError):
            continue
        end = start
        if event.get("end_date"):
            try:
                end = datetime.strptime(event["end_date"], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                pass
        if start <= week_end and end >= week_start:
            selected.append({k: event[k] for k in SNAPSHOT_FIELDS if event.get(k)})

    selected.sort(key=lambda e: (e.get("date", ""), e.get("time") or ""))
    return selected


def lambda_handler(event, context):
    event = event or {}
    today = datetime.now().date()
    if event.get("week_of"):
        today = datetime.strptime(event["week_of"], "%Y-%m-%d").date()

    week_start, week_end = _week_bounds(today)
    week_id = _week_id(week_start)

    all_events = _fetch_events()
    week_events = _events_for_week(all_events, week_start, week_end)

    item = {
        "PK": f"UPDATE#{week_id}",
        # SK=META keeps the item inside the site exporter's existing
        # `SK == META` scan, so no export-side filter has to change.
        "SK": "META",
        "week_id": week_id,
        "week_start": week_start.strftime("%Y-%m-%d"),
        "week_end": week_end.strftime("%Y-%m-%d"),
        "title": (
            "DC Tech Events for the week of "
            f"{week_start.strftime('%B %-d, %Y')}"
        ),
        "published_at": datetime.now(timezone.utc)
                                .isoformat(timespec="seconds")
                                .replace("+00:00", "Z"),
        "event_count": len(week_events),
        "events": week_events,
    }

    table = _dynamodb.Table(TABLE_NAME)
    try:
        # Never silently rewrite a published post: the archive is supposed to
        # be a record of what the week looked like on Monday. `force` exists
        # for manual correction of a bad run.
        kwargs = {} if event.get("force") else {
            "ConditionExpression": "attribute_not_exists(PK)"
        }
        table.put_item(Item=item, **kwargs)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            print(f"Post for {week_id} already published; leaving it alone.")
            return {"week_id": week_id, "status": "already_published"}
        raise

    print(f"Published {week_id} with {len(week_events)} events.")
    return {
        "week_id": week_id,
        "status": "published",
        "event_count": len(week_events),
    }
