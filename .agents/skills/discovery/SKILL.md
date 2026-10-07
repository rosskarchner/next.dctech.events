---
name: discovery
description: Proactively scan DC-area tech event listing sites for groups and one-off events not yet in dctech.events, and queue them via propose_group/propose_event for human review. Use when asked to run discovery, find new DC tech groups, scout for events to add, or grow the calendar — distinct from calendar-qc, which only proposes something it stumbles on while triaging the existing queue.
---

# Discovery

This ports the retired `NextDiscoveryAgentStack` agent's logic (deleted
2026-09-01, recovered from git history at `69f82193^:cdk_next/agents/discovery/`)
into an interactive skill. That agent ran on its own weekly schedule,
independent of the calendar QC agent, and its whole job was to go looking
for what's missing — not to react to what turns up in the review queue.
`calendar-qc`'s "Discovery" section only proposes something noticed in
passing while triaging; this skill is the active version: it scans a fixed
set of listing sites every run.

You cannot publish anything directly. Every find goes through
`propose_group` or `propose_event`, which lands in the same review queue a
human (or the `approve_submission`/`reject_submission` tools) works through.
That's on purpose — discovery finds candidates, it doesn't vet them.

Tools used: `list_groups`, `list_discovery_proposals`, `get_events`,
`verify_ical_feed`, `propose_group`, `propose_event`, plus WebFetch/WebSearch
(and, if available, a browser MCP tool like `mcp__playwright__*` for the
JS-heavy Eventbrite listing — see below), and a web-search tool (e.g.
Tavily) when one is connected, for resolving ambiguous listings (vague
acronyms, generic titles) that a fetched page alone doesn't explain. All
dctech.events tools are namespaced `mcp__dctech-events__*`.

## Before any web work

Run these three first, every time, so you don't re-propose something that's
already tracked:

1. `list_groups()` — groups already on the site.
2. `list_discovery_proposals()` — candidates already queued, approved, *or
   rejected*. Don't re-propose a rejected one just because it turned up
   again this week; a human already said no.
3. `get_events(date_from=<today>)` — one-off events already active.

## Scope

**Geography:** DC metro — Washington DC; Northern Virginia; the Maryland
suburbs (same list as `calendar-qc`'s out-of-area rule). A purely virtual
event or group is in scope only if the *organizing group* is DC-area — a
national group's webinar doesn't count just because anyone can join.

**Subject:** technology — software, data, security, hardware,
design-for-tech, civic tech, and startup/founder events adjacent to those
communities. Not every professional meetup in the DC area is a tech event;
skip ones that are DC-based but not tech-subject (a general small-business
networking group, a real-estate meetup).

## Groups over events

**A recurring meetup is worth more than a single event listing.** When you
find one, spend the effort to locate its iCal feed (check the group's own
site and its Meetup/Luma page) and call `verify_ical_feed` before
`propose_group` — a feed that doesn't validate isn't worth queuing. Only
fall back to `propose_event` for something genuinely one-off (a summit, a
single workshop with no recurring organizer to follow up on).

## Sources to scan

Two fixed sources, every run:

- **`eventbrite.com/d/dc--washington/technology/`** — heavy client-side
  rendering; WebFetch often comes back empty or partial. If a browser tool
  is available (e.g. `mcp__playwright__*`), use it to render the listing;
  otherwise treat this source as failed for the run rather than guessing
  from a thin fetch. High noise either way — filter hard, most listings
  here are generic professional networking, not tech-subject.
- **`meetup.com/find/?keywords=technology&location=us--dc--Washington`** —
  prefer proposing the *group* behind a listing here, not the individual
  event, per "groups over events" above.

`technical.ly/dc/events/` was a third source in the original agent's list
but is gone as of this skill's first real run (2026-09-27): both WebFetch
and Tavily's extractor returned a stale 2013 archive page, not a live
listing — the site no longer serves a working DC events calendar at that
URL. Don't re-add it on a guess; if it's ever worth scanning again, confirm
it renders a real, current listing first.

If you find a promising group or event mentioned somewhere else entirely
(a link from one of these pages, a mention on a page you're already
reading), it's fair game too — the two sources are the floor, not a
whitelist.

## Judgement

Skipping is always valid. A missed group costs nothing until next week's
run finds it again or someone submits it directly; a wrongly-queued
proposal costs a human's review time and, if approved by mistake, puts an
out-of-scope group on the site. When a group's geography or tech-relevance
is genuinely unclear, skip it and note it — don't propose on a guess.

## Report

At the end, report as a JSON object:

```json
{
  "groups": [{"name": "...", "website": "...", "ical": "...", "reason": "..."}],
  "events": [{"title": "...", "date": "...", "url": "...", "reason": "..."}],
  "skipped": [{"name": "...", "reason": "..."}],
  "sources_scanned": ["..."],
  "sources_failed": ["..."]
}
```
