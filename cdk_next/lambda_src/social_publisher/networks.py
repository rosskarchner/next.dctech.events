"""Minimal Mastodon and Bluesky clients, stdlib only.

Neither API is big enough here to justify a dependency: Mastodon is one
authenticated POST, Bluesky is a session call followed by a createRecord.
Staying on urllib also keeps this Lambda's asset to a few kilobytes and out
of build_lambdas.sh's `uv pip install` section entirely.

Both `post()` functions return a dict carrying whatever is needed to delete
the post again — that is what makes test posts cheap to clean up, and it is
also what gets stored on the dedupe record.
"""
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

TIMEOUT = 20

# Bluesky's post record caps text at 300 graphemes. Mastodon's limit is
# instance-configurable (500 by default) and is passed in by the caller.
BLUESKY_CHAR_LIMIT = 300

BLUESKY_DEFAULT_PDS = "https://bsky.social"
BLUESKY_COLLECTION = "app.bsky.feed.post"

# Deliberately conservative: matches the bare URLs we compose ourselves rather
# than trying to be a general-purpose linkifier over user-authored prose.
_URL_RE = re.compile(r"https?://[^\s<>\"\]]+")


class NetworkError(RuntimeError):
    """A social API call failed. Message includes the response body."""


def _request(url, *, body=None, headers=None, method=None):
    request = urllib.request.Request(
        url, data=body, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # nosec B310
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise NetworkError(f"{method or 'POST'} {url} -> {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise NetworkError(f"{method or 'POST'} {url} failed: {exc.reason}") from exc
    return json.loads(raw) if raw.strip() else {}


def _json_body(payload):
    return json.dumps(payload).encode("utf-8")


def _now_iso():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ── Mastodon ─────────────────────────────────────────────────────────


def mastodon_post(secret, text, *, idempotency_key=None):
    """Publish a status. Returns {"id", "url"}."""
    instance = secret["instance_url"].rstrip("/")
    headers = {
        "Authorization": f"Bearer {secret['access_token']}",
        "Content-Type": "application/json",
    }
    # Mastodon dedupes on this header for a few hours, which makes a Lambda
    # retry that lost its dedupe record a no-op rather than a double post.
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    data = _request(
        f"{instance}/api/v1/statuses",
        body=_json_body({"status": text, "visibility": "public"}),
        headers=headers,
    )
    return {"id": str(data["id"]), "url": data.get("url", "")}


def mastodon_delete(secret, status_id):
    instance = secret["instance_url"].rstrip("/")
    _request(
        f"{instance}/api/v1/statuses/{status_id}",
        headers={"Authorization": f"Bearer {secret['access_token']}"},
        method="DELETE",
    )


# ── Bluesky (atproto XRPC) ───────────────────────────────────────────


def link_facets(text):
    """Rich-text facets marking every URL in `text` as a link.

    Bluesky does not linkify bare URLs on its own, and facet offsets are
    counted in UTF-8 *bytes*, not characters — a non-ASCII character earlier
    in the post (an em dash, a '…') shifts every later offset.
    """
    facets = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?)")
        start = len(text[: match.start()].encode("utf-8"))
        facets.append({
            "index": {
                "byteStart": start,
                "byteEnd": start + len(url.encode("utf-8")),
            },
            "features": [
                {"$type": "app.bsky.richtext.facet#link", "uri": url}
            ],
        })
    return facets


def _bluesky_session(secret):
    pds = (secret.get("pds_url") or BLUESKY_DEFAULT_PDS).rstrip("/")
    data = _request(
        f"{pds}/xrpc/com.atproto.server.createSession",
        body=_json_body({
            "identifier": secret["identifier"],
            "password": secret["app_password"],
        }),
        headers={"Content-Type": "application/json"},
    )
    return pds, data["accessJwt"], data["did"], data.get("handle") or data["did"]


def bluesky_post(secret, text, *, link=None, title=None, description=None):
    """Publish a post. Returns {"uri", "cid", "url"}."""
    pds, jwt, did, handle = _bluesky_session(secret)

    record = {
        "$type": BLUESKY_COLLECTION,
        "text": text,
        "createdAt": _now_iso(),
        "langs": ["en"],
    }
    facets = link_facets(text)
    if facets:
        record["facets"] = facets
    if link:
        # A link card, so the post reads as a shared article rather than a
        # bare URL. No thumb: fetching and uploading a blob is the only part
        # of this that would need real image handling, and the card renders
        # fine without one.
        record["embed"] = {
            "$type": "app.bsky.embed.external",
            "external": {
                "uri": link,
                "title": (title or link)[:300],
                "description": (description or "")[:1000],
            },
        }

    data = _request(
        f"{pds}/xrpc/com.atproto.repo.createRecord",
        body=_json_body({
            "repo": did,
            "collection": BLUESKY_COLLECTION,
            "record": record,
        }),
        headers={
            "Authorization": f"Bearer {jwt}",
            "Content-Type": "application/json",
        },
    )
    rkey = data["uri"].rsplit("/", 1)[-1]
    return {
        "uri": data["uri"],
        "cid": data.get("cid", ""),
        "url": f"https://bsky.app/profile/{handle}/post/{rkey}",
    }


def bluesky_delete(secret, at_uri):
    """Delete a post by its at:// URI."""
    pds, jwt, did, _ = _bluesky_session(secret)
    _request(
        f"{pds}/xrpc/com.atproto.repo.deleteRecord",
        body=_json_body({
            "repo": did,
            "collection": BLUESKY_COLLECTION,
            "rkey": at_uri.rsplit("/", 1)[-1],
        }),
        headers={
            "Authorization": f"Bearer {jwt}",
            "Content-Type": "application/json",
        },
    )
