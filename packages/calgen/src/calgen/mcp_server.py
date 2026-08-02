#!/usr/bin/env python3
"""
calgen MCP server.

Exposes groups, single events, recurring events, categories, and overlays as
MCP tools so agents can commit results directly instead of hand-writing YAML
(and re-deriving calgen's validation rules — valid category slugs, the
overlay protected-fields list, guid hashing, etc. — in every prompt).

One server instance serves one site directory, set via `calgen mcp SITE_DIR`
(which chdir's into SITE_DIR and sets CALGEN_SITE_DIR before this module's
tools run).
"""
import json
import os
import re

import requests
import yaml

from calgen import bootstrap, pipeline
from calgen.calendars import (
    USER_AGENT,
    fetch_json_ld_data,
    refresh_calendars,
)
from calgen.event_utils import calculate_event_hash

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("calgen")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _read_yaml(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_yaml(path: str, data: dict, comments: list[str] | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = "".join(f"# {c}\n" for c in (comments or []))
    text += yaml.dump(data, default_flow_style=False, sort_keys=False)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _valid_category_slugs() -> set[str]:
    return set(pipeline.get_categories().keys())


def _check_categories(categories: list[str]) -> None:
    unknown = sorted(set(categories) - _valid_category_slugs())
    if unknown:
        raise ValueError(
            f"Unknown category slug(s): {unknown}. "
            f"Valid slugs: {sorted(_valid_category_slugs())}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Groups
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_groups() -> list[dict]:
    """List all meetup groups: slug, name, website, ical feed, categories, active."""
    return [
        {
            "slug": g["id"],
            "name": g.get("name", ""),
            "website": g.get("website", ""),
            "ical": g.get("ical", ""),
            "categories": g.get("categories", []),
            "active": g.get("active", True),
        }
        for g in pipeline.get_groups()
    ]


@mcp.tool()
def verify_ical_feed(url: str) -> dict:
    """
    GET an iCal feed URL and check it returns calendar data.

    Always uses GET (some providers, e.g. Meetup.com, 404 on HEAD but work on
    GET). Returns {ok, status_code, reason}.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
    except Exception as e:
        return {"ok": False, "status_code": None, "reason": str(e)}
    ok = resp.status_code == 200 and "VCALENDAR" in resp.text
    if ok:
        reason = "ok"
    elif resp.status_code != 200:
        reason = f"HTTP {resp.status_code}"
    else:
        reason = "response body does not contain VCALENDAR"
    return {"ok": ok, "status_code": resp.status_code, "reason": reason}


@mcp.tool()
def add_group(name: str, website: str, ical: str, categories: list[str], active: bool = True) -> dict:
    """
    Add a new meetup group (_groups/<slug>.yaml). `categories` must be valid
    category slugs — see list_categories.
    """
    _check_categories(categories)
    slug = _slugify(name)
    path = os.path.join(pipeline.GROUPS_DIR, f"{slug}.yaml")
    if os.path.exists(path):
        raise ValueError(f"Group already exists: {slug}.yaml")
    _write_yaml(path, {
        "name": name,
        "active": active,
        "website": website,
        "ical": ical,
        "categories": categories,
    })
    return {"slug": slug, "path": path}


@mcp.tool()
def set_group_active(slug: str, active: bool, reason: str | None = None) -> dict:
    """Enable/disable a group (e.g. disable a group with a permanently broken feed)."""
    path = os.path.join(pipeline.GROUPS_DIR, f"{slug}.yaml")
    data = _read_yaml(path)
    if not data:
        raise ValueError(f"No such group: {slug}")
    data["active"] = active
    comments = [reason] if reason else []
    _write_yaml(path, data, comments)
    return {"slug": slug, "active": active}


# ─────────────────────────────────────────────────────────────────────────────
# Single events
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_single_events() -> list[dict]:
    """List all manually-added single events (one-off conferences/events)."""
    return [
        {
            "file_id": e["id"],
            "guid": e["guid"],
            "title": e.get("title", ""),
            "date": e.get("date", ""),
            "end_date": e.get("end_date"),
            "url": e.get("url", ""),
            "location": e.get("location", ""),
            "cost": e.get("cost"),
            "categories": e.get("categories", []),
        }
        for e in pipeline.load_single_events()
    ]


@mcp.tool()
def add_single_event(
    title: str, date: str, url: str, location: str,
    end_date: str | None = None, cost: str | None = None,
    categories: list[str] | None = None,
) -> dict:
    """
    Add a one-off event (_single_events/YYYY-MM-DD-slug.yaml). date/end_date
    are 'YYYY-MM-DD'. Returns the file_id and the guid the pipeline will assign
    to this event (so you can attach an overlay to it later).
    """
    categories = categories or []
    _check_categories(categories)
    file_id = f"{date}-{_slugify(title)}"
    path = os.path.join(pipeline.SINGLE_EVENTS_DIR, f"{file_id}.yaml")
    if os.path.exists(path):
        raise ValueError(f"Event already exists: {file_id}.yaml")
    data = {"title": title, "date": date, "url": url, "location": location}
    if end_date:
        data["end_date"] = end_date
    if cost:
        data["cost"] = cost
    data["categories"] = categories
    _write_yaml(path, data)
    guid = calculate_event_hash(date, "", title, url)
    return {"file_id": file_id, "path": path, "guid": guid}


@mcp.tool()
def update_single_event(file_id: str, fields: dict) -> dict:
    """
    Update fields on an existing single event. If `date` changes, the file is
    renamed to match (YYYY-MM-DD-slug.yaml). Returns the (possibly new) file_id.
    """
    if "categories" in fields:
        _check_categories(fields["categories"])
    path = os.path.join(pipeline.SINGLE_EVENTS_DIR, f"{file_id}.yaml")
    data = _read_yaml(path)
    if not data:
        raise ValueError(f"No such event: {file_id}")
    data.update(fields)

    new_date = data.get("date", "")
    new_file_id = f"{new_date}-{_slugify(data.get('title', file_id))}"
    new_path = os.path.join(pipeline.SINGLE_EVENTS_DIR, f"{new_file_id}.yaml")
    _write_yaml(new_path, data)
    if new_path != path and os.path.exists(path):
        os.remove(path)
    return {"file_id": new_file_id, "path": new_path}


@mcp.tool()
def delete_single_event(file_id: str) -> dict:
    """Delete a single event (e.g. a past event whose URL now 404s)."""
    path = os.path.join(pipeline.SINGLE_EVENTS_DIR, f"{file_id}.yaml")
    if not os.path.exists(path):
        raise ValueError(f"No such event: {file_id}")
    os.remove(path)
    return {"deleted": file_id}


@mcp.tool()
def fetch_event_page(url: str) -> dict:
    """
    Fetch a Meetup event page's JSON-LD metadata. Returns
    {title, location, is_virtual, cancelled}. Non-Meetup URLs return all-null
    fields — use WebFetch for those.
    """
    return fetch_json_ld_data(url)


# ─────────────────────────────────────────────────────────────────────────────
# Recurring events
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_recurring_events() -> list[dict]:
    """List recurring events (_recurring_events/*.yaml) with their RRULEs."""
    return [
        {
            "file_id": e["id"],
            "title": e.get("title", ""),
            "date": e.get("date", ""),
            "time": e.get("time"),
            "url": e.get("url", ""),
            "location": e.get("location", ""),
            "cost": e.get("cost"),
            "categories": e.get("categories", []),
            "rrule": e.get("rrule", ""),
            "description": e.get("description"),
        }
        for e in pipeline.load_recurring_events()
    ]


@mcp.tool()
def add_recurring_event(
    title: str, date: str, rrule: str, url: str, location: str,
    time: str | None = None, cost: str | None = None,
    categories: list[str] | None = None, description: str | None = None,
) -> dict:
    """
    Add a recurring event (_recurring_events/slug.yaml).

    `date` ('YYYY-MM-DD') is the first/anchor occurrence. `rrule` is an
    RFC-5545 RRULE string, e.g. 'FREQ=WEEKLY;BYDAY=TU' or
    'FREQ=MONTHLY;BYDAY=3TH' (3rd Thursday monthly). Occurrences are expanded
    up to 90 days out.
    """
    categories = categories or []
    _check_categories(categories)
    if not rrule.strip():
        raise ValueError("rrule must be a non-empty RFC-5545 RRULE string")
    file_id = _slugify(title)
    path = os.path.join(pipeline.RECURRING_EVENTS_DIR, f"{file_id}.yaml")
    if os.path.exists(path):
        raise ValueError(f"Recurring event already exists: {file_id}.yaml")
    data = {"title": title, "date": date}
    if time:
        data["time"] = time
    data["url"] = url
    data["location"] = location
    if cost:
        data["cost"] = cost
    if description:
        data["description"] = description
    data["categories"] = categories
    data["rrule"] = rrule
    _write_yaml(path, data)
    return {"file_id": file_id, "path": path}


@mcp.tool()
def update_recurring_event(file_id: str, fields: dict) -> dict:
    """Update fields (including `rrule`) on an existing recurring event."""
    if "categories" in fields:
        _check_categories(fields["categories"])
    path = os.path.join(pipeline.RECURRING_EVENTS_DIR, f"{file_id}.yaml")
    data = _read_yaml(path)
    if not data:
        raise ValueError(f"No such recurring event: {file_id}")
    data.update(fields)
    _write_yaml(path, data)
    return {"file_id": file_id, "path": path}


@mcp.tool()
def delete_recurring_event(file_id: str) -> dict:
    """Delete a recurring event (e.g. a series that has ended)."""
    path = os.path.join(pipeline.RECURRING_EVENTS_DIR, f"{file_id}.yaml")
    if not os.path.exists(path):
        raise ValueError(f"No such recurring event: {file_id}")
    os.remove(path)
    return {"deleted": file_id}


# ─────────────────────────────────────────────────────────────────────────────
# One-time site setup
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def bootstrap_site(
    city: str, domain: str, timezone: str, table_name: str,
    max_distance_miles: int = 40,
) -> dict:
    """
    Write config.yaml and static/robots.txt for a new site (one-time setup).

    `city` is e.g. 'Richmond, VA'. `domain` is the site's hostname, e.g.
    'richmond.localtech.events'. `timezone` is an IANA tz name, e.g.
    'US/Eastern'. `table_name` is the DynamoDB table name, e.g.
    'RichmondTechEvents'.
    """
    config_path = bootstrap.write_config(
        '.', city=city, domain=domain, timezone=timezone,
        table_name=table_name, max_distance_miles=max_distance_miles,
    )
    robots_path = bootstrap.write_robots('.', domain=domain)
    from calgen.site_config import reset_config
    reset_config()
    return {"config": config_path, "robots_txt": robots_path}


@mcp.tool()
def write_regions(
    city: str, home_state: str, default_slug: str,
    regions: list[dict], city_to_region: dict[str, str],
) -> dict:
    """
    Write regions.py — the metro-area region plugin (one-time setup).

    `city` is e.g. 'Richmond, VA', used in comments/messages. `home_state` is
    the two-letter state code; events outside it are rejected. `regions` is a
    list of {'slug': ..., 'name': ...} dicts describing the metro's
    sub-regions (counties/neighborhoods) — `default_slug` must be one of
    these slugs and is used for in-state locations not found in
    `city_to_region`. `city_to_region` maps lowercased suburb/city names (as
    returned by extract_location_info) to a region slug.
    """
    path = bootstrap.write_regions_file(
        '.', city=city, home_state=home_state, default_slug=default_slug,
        regions=regions, city_to_region=city_to_region,
    )
    return {"path": path, "regions": [r["slug"] for r in regions]}


# ─────────────────────────────────────────────────────────────────────────────
# Categories
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_categories() -> list[dict]:
    """List valid category slugs with their display name and description."""
    return [
        {"slug": slug, "name": cat.get("name", slug), "description": cat.get("description", "")}
        for slug, cat in pipeline.get_categories().items()
    ]


@mcp.tool()
def add_category(slug: str, name: str, description: str) -> dict:
    """Add a new category definition (_categories/<slug>.yaml). Setup-time only."""
    path = os.path.join(pipeline.CATEGORIES_DIR, f"{slug}.yaml")
    if os.path.exists(path):
        raise ValueError(f"Category already exists: {slug}.yaml")
    _write_yaml(path, {"name": name, "description": description})
    return {"slug": slug, "path": path}


# ─────────────────────────────────────────────────────────────────────────────
# Overlays + QA cache
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_overlays() -> dict[str, dict]:
    """Return all overlay files as {guid: overlay_fields}."""
    return pipeline.load_overlays()


@mcp.tool()
def get_overlay(guid: str) -> dict:
    """Return the overlay fields for one event guid (empty dict if none)."""
    return pipeline.load_overlays().get(guid, {})


@mcp.tool()
def set_overlay(guid: str, fields: dict, comment: str) -> dict:
    """
    Merge `fields` into _overlay/<guid>.yaml, preserving any existing overlay
    fields. `comment` is recorded as a leading YAML comment explaining why.

    Supported fields: duplicate_of, hidden, location, title, categories.
    Protected fields (set by the pipeline from the source feed, not
    overridable) are rejected: group, group_id, group_website, date,
    end_date, guid, source.
    """
    invalid = set(fields) & pipeline._OVERLAY_PROTECTED_FIELDS
    if invalid:
        raise ValueError(f"Cannot set protected overlay field(s): {sorted(invalid)}")
    path = os.path.join(pipeline.OVERLAY_DIR, f"{guid}.yaml")
    existing = _read_yaml(path)
    existing.update(fields)
    _write_yaml(path, existing, [comment])
    return {"guid": guid, "overlay": existing}


@mcp.tool()
def mark_reviewed(guids: list[str]) -> dict:
    """Append guids to _qa_cache.json (dedup'd) so future QA runs skip them."""
    cache_path = "_qa_cache.json"
    existing = set()
    if os.path.exists(cache_path):
        existing = set(json.load(open(cache_path)))
    existing |= set(guids)
    with open(cache_path, "w") as f:
        json.dump(sorted(existing), f, indent=2)
    return {"cache_size": len(existing)}


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline + events
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def refresh_and_run_pipeline() -> dict:
    """
    Run `calgen refresh` then `calgen pipeline`. Returns per-group event
    counts (from the iCal cache) and the total event count written to
    _data/all_events.json.

    NOTE: a group with 0 events is not necessarily broken — calgen only loads
    events within a 90-day window, so an active group with nothing scheduled
    soon will show 0 here. Use verify_ical_feed to check if a feed is actually
    broken.
    """
    refresh_calendars()

    groups_status = []
    for group in pipeline.get_groups():
        if not group.get("active", True):
            groups_status.append({"slug": group["id"], "active": False, "event_count": None})
            continue
        cache_file = os.path.join(pipeline.ICAL_CACHE_DIR, f"{group['id']}.json")
        count = None
        if os.path.exists(cache_file):
            try:
                count = len(json.load(open(cache_file)))
            except Exception:
                count = None
        groups_status.append({"slug": group["id"], "active": True, "event_count": count})

    pipeline.main()
    data_file = os.path.join(pipeline.DATA_DIR, "all_events.json")
    total = 0
    if os.path.exists(data_file):
        total = len(json.load(open(data_file)))

    return {"groups": groups_status, "total_events": total}


# Fields returned by get_events() — excludes description/group_website/time
# fields, which are large and unused by agents, to keep responses well under
# the MCP message size limit even for sites with thousands of events.
_EVENT_FIELDS = [
    "guid", "title", "date", "url", "location", "location_type",
    "group", "group_id", "source", "categories",
]


@mcp.tool()
def get_events(
    source: str | None = None, date_from: str | None = None,
    exclude_reviewed: bool = False, exclude_resolved_overlays: bool = False,
    limit: int | None = 500,
) -> list[dict]:
    """
    Read _data/all_events.json (overlay-applied), optionally filtered by:
      source                    'ical' | 'manual' | 'recurring'
      date_from                 'YYYY-MM-DD', inclusive
      exclude_reviewed          drop guids already in _qa_cache.json
      exclude_resolved_overlays drop guids with an overlay setting hidden/duplicate_of
      limit                     cap on returned events (default 500, None = no cap)

    Returns a compact projection of each event (guid, title, date, url,
    location, location_type, group, group_id, source, categories) — not the
    full record from all_events.json.

    Run refresh_and_run_pipeline first if this file is stale.
    """
    data_file = os.path.join(pipeline.DATA_DIR, "all_events.json")
    if not os.path.exists(data_file):
        return []
    events = json.load(open(data_file))
    if source:
        events = [e for e in events if e.get("source") == source]
    if date_from:
        events = [e for e in events if e.get("date", "") >= date_from]
    if exclude_reviewed:
        cache_path = "_qa_cache.json"
        reviewed = set(json.load(open(cache_path))) if os.path.exists(cache_path) else set()
        events = [e for e in events if e.get("guid") not in reviewed]
    if exclude_resolved_overlays:
        overlays = pipeline.load_overlays()
        events = [
            e for e in events
            if not (overlays.get(e.get("guid"), {}).get("hidden")
                    or overlays.get(e.get("guid"), {}).get("duplicate_of"))
        ]
    events = [{k: e.get(k) for k in _EVENT_FIELDS} for e in events]
    if limit is not None:
        events = events[:limit]
    return events
