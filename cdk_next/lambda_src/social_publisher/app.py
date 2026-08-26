"""Cross-post new /updates posts to Mastodon and Bluesky.

Fed by the events table's DynamoDB stream rather than called from the
publishers, so it covers every kind of /updates post from one place: the Monday
`UPDATE#{publish_date}` week-ahead link post and the Wednesday roundup of the
same key shape, both written by the updates publisher, and the free-form
`POST#{slug}` announcements published through /edit. No writer has to know this
exists, and a social API outage can never block or duplicate the DynamoDB write
that produced the post.

A link post has no page under /updates/, so it syndicates its target — see
`_syndicated_url`.

Idempotency is a `SOCIAL#{target_pk}` record holding the ids of whatever has
already been posted. Streams deliver at least once and a failed batch is
retried, so without it a flaky network call would double-post. The record is
per-network: if Mastodon succeeds and Bluesky fails, the retry posts only the
Bluesky half. `SOCIAL#` is deliberately absent from the site build trigger's
RELEVANT_PREFIXES, so writing it back to the table does not kick off a
pointless rebuild.

Manual invoke, for backfilling a post or checking the copy:

    {"pk": "UPDATE#2026-W33"}                 # publish, honoring the record
    {"pk": "UPDATE#2026-W33", "force": true}  # publish again anyway
    {"pk": "UPDATE#2026-W33", "dry_run": true}
"""
import json
import os
import re
from datetime import date, datetime
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeDeserializer

import networks

TABLE_NAME = os.environ["DYNAMODB_TABLE_NAME"]
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://dctech.events").rstrip("/")
MASTODON_SECRET_NAME = os.environ["MASTODON_SECRET_NAME"]
BLUESKY_SECRET_NAME = os.environ["BLUESKY_SECRET_NAME"]
# Mastodon's status length is an instance setting; dmv.community uses the
# 500-character default. Overridable without a code change if that moves.
MASTODON_CHAR_LIMIT = int(os.environ.get("MASTODON_CHAR_LIMIT", "500"))

SYNDICATED_PREFIXES = ("UPDATE#", "POST#")

_dynamodb = boto3.resource("dynamodb")
_secrets = boto3.client("secretsmanager")
_deserializer = TypeDeserializer()
_secret_cache = {}


def _secret(name):
    """Cached across warm invocations — credentials change far more rarely
    than posts, and every fetch is a billed Secrets Manager API call."""
    if name not in _secret_cache:
        value = _secrets.get_secret_value(SecretId=name)["SecretString"]
        _secret_cache[name] = json.loads(value)
    return _secret_cache[name]


# ── describing a post ────────────────────────────────────────────────


def _plain(value):
    if isinstance(value, Decimal):
        return int(value)
    return value


def _week_summary(item):
    """Same blurb calgen's updates.summarize() puts on the index page."""
    # Roundups published from 2026-08-12 on carry their own: they list what was
    # *added* over a stated span, which cannot be re-derived from the events.
    explicit = str(item.get("summary") or "").strip()
    if explicit:
        return explicit

    events = item.get("events") or []
    titles = [e.get("title") for e in events if isinstance(e, dict) and e.get("title")]
    count = _plain(item.get("event_count")) or len(titles)
    if not titles:
        return "No events were listed for this week."

    shown = titles[:3]
    remaining = count - len(shown)
    blurb = ", ".join(shown)
    if remaining > 0:
        blurb += f", and {remaining} more"
    return f"{count} event{'s' if count != 1 else ''} this week: {blurb}."


def _strip_markdown(text):
    """Enough Markdown stripping for a one-line blurb.

    calgen renders the real thing with markdown + BeautifulSoup, but pulling
    both into this Lambda to un-format a single paragraph is not worth it.
    """
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)        # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)    # links -> label
    text = re.sub(r"[*_`#>]+", "", text)                     # inline marks
    return " ".join(text.split())


def _first_paragraph(body):
    for chunk in (body or "").split("\n\n"):
        text = _strip_markdown(chunk)
        if text:
            return text
    return ""


def _syndicated_url(item, permalink):
    """Which URL the social post carries.

    A link post has no page under /updates/ — its link *is* its content — so
    it syndicates its target. Every other post syndicates its own permalink.

    The fallback for a link post is its week page, not `permalink`: that path
    is never built for a link post, so handing it to Mastodon would be
    announcing a 404.
    """
    if str(item.get("post_kind") or "") != "link":
        return permalink

    link_url = str(item.get("link_url") or "").strip()
    if not link_url:
        week_id = str(item.get("week_id") or "").strip()
        if not week_id:
            return None
        link_url = f"/week/{week_id}/"
    if link_url.startswith(("http://", "https://")):
        return link_url
    return f"{SITE_BASE_URL}/{link_url.lstrip('/')}"


def _describe(item):
    """The post as {pk, title, summary, url}, or None if it is not postable."""
    pk = str(item.get("PK", ""))

    if pk.startswith("UPDATE#"):
        # The date the post is filed under — its own publication date, or for
        # posts written before 2026-08-12, the Monday of the week they cover.
        raw_start = item.get("published_on") or item.get("week_start")
        if not raw_start:
            return None
        week_start = date.fromisoformat(str(raw_start))
        formatted = week_start.strftime("%B %-d, %Y")
        # Unpadded on purpose: Flask's <int:> converter renders 8, not 08, so
        # a zero-padded path is one the site never actually serves.
        permalink = (f"{SITE_BASE_URL}/updates/{week_start.year}"
                     f"/{week_start.month}/{week_start.day}/")
        url = _syndicated_url(item, permalink)
        if not url:
            # A link post with nothing to point at. Nothing to announce, and
            # better to say so than to publish a dead link.
            return None
        return {
            "pk": pk,
            "title": str(item.get("title") or
                         f"DC Tech Events for the week of {formatted}"),
            "summary": _week_summary(item),
            "url": url,
        }

    if pk.startswith("POST#"):
        # Drafts are exported alongside published posts so /edit can list
        # them; announcing one would leak an unpublished URL.
        if str(item.get("status", "draft")).lower() != "published":
            return None
        slug = str(item.get("slug") or pk.split("#", 1)[1])
        return {
            "pk": pk,
            "title": str(item.get("title") or slug),
            "summary": str(item.get("summary") or "").strip()
                       or _first_paragraph(item.get("body")),
            "url": f"{SITE_BASE_URL}/updates/{slug}/",
        }

    return None


def _clip(text, limit):
    """Truncate on a word boundary with an ellipsis, never mid-word."""
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return (clipped or text[: limit - 1].rstrip()) + "…"


def compose(post, limit):
    """Headline, blurb, permalink — dropping the blurb before the link.

    The link is the point of the post, so it is the one part that is never
    sacrificed to the character limit.
    """
    url = post["url"]
    title = _clip(post["title"].strip(), max(limit - len(url) - 2, 1))
    head = f"{title}\n\n{url}"

    summary = (post.get("summary") or "").strip()
    room = limit - len(head) - 2
    # Below this a blurb is a stub rather than a sentence; the headline alone
    # reads better than "27 events this we…".
    if summary and room >= 40:
        return f"{title}\n\n{_clip(summary, room)}\n\n{url}"
    return head


# ── publishing ───────────────────────────────────────────────────────


def _record_key(pk):
    return {"PK": f"SOCIAL#{pk}", "SK": "META"}


def _load_record(table, pk):
    return table.get_item(Key=_record_key(pk)).get("Item") or {}


def _save_record(table, pk, url, updates):
    """Merge one network's result in, leaving the other network's alone."""
    names = {"#target": "target_url", "#at": "updated_at"}
    values = {
        ":target": url,
        ":at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    sets = ["#target = :target", "#at = :at"]
    for i, (field, value) in enumerate(sorted(updates.items())):
        names[f"#f{i}"] = field
        values[f":f{i}"] = value
        sets.append(f"#f{i} = :f{i}")

    table.update_item(
        Key=_record_key(pk),
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def publish(item, *, force=False, dry_run=False):
    post = _describe(item)
    if not post:
        pk = str(item.get("PK", ""))
        print(f"Nothing to announce for {pk} (draft or not an /updates post)")
        return {"pk": pk, "status": "skipped",
                "reason": "not a publishable /updates post"}

    texts = {
        "mastodon": compose(post, MASTODON_CHAR_LIMIT),
        "bluesky": compose(post, networks.BLUESKY_CHAR_LIMIT),
    }
    if dry_run:
        return {"pk": post["pk"], "status": "dry_run", "url": post["url"],
                "texts": texts}

    table = _dynamodb.Table(TABLE_NAME)
    record = {} if force else _load_record(table, post["pk"])

    # Mastodon honors Idempotency-Key for hours, so a stable key would turn a
    # deliberate `force` repost into a silent no-op that hands back the
    # original status. Vary the key exactly when a repost is what was asked for.
    idempotency_key = post["pk"]
    if force:
        idempotency_key += f"#{datetime.now().timestamp():.0f}"

    result = {"pk": post["pk"], "url": post["url"], "posted": {},
              "skipped": [], "errors": {}}

    for network, already in (("mastodon", "mastodon_id"),
                             ("bluesky", "bluesky_uri")):
        if record.get(already):
            result["skipped"].append(network)
            continue
        try:
            if network == "mastodon":
                posted = networks.mastodon_post(
                    _secret(MASTODON_SECRET_NAME), texts["mastodon"],
                    idempotency_key=idempotency_key,
                )
                fields = {"mastodon_id": posted["id"],
                          "mastodon_url": posted["url"]}
            else:
                posted = networks.bluesky_post(
                    _secret(BLUESKY_SECRET_NAME), texts["bluesky"],
                    link=post["url"], title=post["title"],
                    description=post.get("summary") or "",
                )
                fields = {"bluesky_uri": posted["uri"],
                          "bluesky_url": posted["url"]}
        except Exception as exc:  # noqa: BLE001 — one network must not sink the other
            print(f"ERROR posting {post['pk']} to {network}: {exc}")
            result["errors"][network] = str(exc)
            continue

        _save_record(table, post["pk"], post["url"], fields)
        result["posted"][network] = posted
        print(f"Posted {post['pk']} to {network}: {posted.get('url')}")

    result["status"] = "error" if result["errors"] else "ok"
    return result


# ── entry point ──────────────────────────────────────────────────────


def _items_from_stream(records):
    items = []
    for record in records:
        if record.get("eventName") not in ("INSERT", "MODIFY"):
            continue
        keys = record.get("dynamodb", {}).get("Keys", {})
        pk = keys.get("PK", {}).get("S", "")
        if not pk.startswith(SYNDICATED_PREFIXES):
            continue
        if keys.get("SK", {}).get("S") != "META":
            continue
        image = record.get("dynamodb", {}).get("NewImage")
        if not image:
            continue
        items.append({k: _deserializer.deserialize(v) for k, v in image.items()})
    return items


def lambda_handler(event, context):
    event = event or {}

    if "Records" in event:
        items = _items_from_stream(event["Records"])
        force = dry_run = False
    elif event.get("pk"):
        table = _dynamodb.Table(TABLE_NAME)
        item = table.get_item(Key={"PK": event["pk"], "SK": "META"}).get("Item")
        if not item:
            raise ValueError(f"No item {event['pk']} / META in {TABLE_NAME}")
        items = [item]
        force = bool(event.get("force"))
        dry_run = bool(event.get("dry_run"))
    else:
        raise ValueError('Expected a stream event or {"pk": "UPDATE#..."}')

    results = [publish(i, force=force, dry_run=dry_run) for i in items]

    # Raising hands the batch back for the event source's configured retry.
    # Safe because the SOCIAL# record already covers whatever succeeded, so a
    # retry only re-attempts the network that actually failed.
    failed = [r for r in results if r.get("status") == "error"]
    if failed:
        raise RuntimeError(f"Social publish failed for {len(failed)} post(s): "
                           f"{json.dumps(failed, default=str)}")

    return {"processed": len(results), "results": results}
