---
name: calendar-qc
description: Run a calendar QC pass over dctech.events' pending event review queue — triage duplicates and out-of-area listings, polish titles/locations/categories, and file discovery proposals for groups or events found along the way. Use when asked to run QA, review the QA queue, triage the calendar, do a QC pass, or catch up pending events, or when list_pending_qa has a backlog someone wants worked through by hand.
---

# Calendar QC

This is the same judgement the nightly Scheduled Task applies against the
bearer-token `/mcp-agent` route (see `NEXT_MCP_AGENT_MIGRATION.md`), run
instead from an interactive session against the full `dctech-events` MCP
server. That means every tool below is available, including `list_qa_run`
and `revert_qa_run` — the automated route can't reach either — so unlike the
nightly run, a mistake here can be inspected and undone. Still write overlays
deliberately: the rules exist because they were tuned against months of real
DC-area feeds, not because a strong model needs guardrails.

Tools used: `list_groups`, `list_categories`, `get_events`, `get_event`,
`get_overlay`, `set_overlay`, `list_pending_qa`, `resolve_qa_review`,
`list_qa_run`, `revert_qa_run`, `verify_ical_feed`, `propose_event`,
`propose_group`, `list_discovery_proposals`, plus WebFetch/WebSearch for
reading event pages. All are namespaced `mcp__dctech-events__*`; if they
aren't available, check `.mcp.json` and `aws sts get-caller-identity` per
the project's AWS guidance.

## Run id

Use `qc-<today's date, YYYY-MM-DD>` (or append `-2`, `-3`, ... if you run a
second pass the same day) for every `set_overlay` call in this session. It's
what `list_qa_run`/`revert_qa_run` key on if the run needs undoing later.

## Scope and batch size

Only `source: "ical"` events dated today or later. Call
`list_pending_qa(limit=15)` for the queue — not the default (200). If it
returns 15, treat that as "more may be waiting": finish this batch, report
that the queue isn't empty, and re-run for another batch rather than raising
the limit in one call. A queue that's grown far past 15 without being worked
is itself worth a line in the report — it usually means a feed dumped a lot
of events at once.

## Reading event pages

Try WebFetch first. Meetup, Luma and Eventbrite are aggressive about
blocking automated readers, so when WebFetch comes back empty, blocked, or
obviously partial, use WebSearch for the specific fact you need (a venue's
real address, which city a place is in) rather than retrying the fetch. If
you still can't confirm something, don't guess — treat it as a fetch failure
and move on; a guess dressed as a correction is worse than the feed's own
value.

## Pass 1: Triage (duplicates and out-of-area) — be reluctant

This pass *removes* an event from the site (`hidden: true` or
`duplicate_of`), so the bar for acting is high. Skipping is always a valid
answer: a false positive costs a reader the listing they came for, while a
false negative just leaves a duplicate up for another pass.

**Overlay fields for this pass, and only these:** `duplicate_of: <guid>`,
`hidden: true`.

**Duplicates.** Look for pairs that are almost certainly the same
real-world event listed by two different groups: the same `url`, the same
or nearly identical `title` on the same `date`, or one event's description
referencing the other group's event (`get_event` to read descriptions). The
other half of a pair is usually *not* in the queue — compare against the
whole corpus from `get_events`, not just the queued events. Decide which is
canonical, in order, stopping at the first rule that settles it:

1. The organising group's own listing wins over an aggregator/umbrella
   calendar re-listing someone else's event.
2. A listing that points at the other ("RSVP on Luma") is still canonical —
   don't flip the decision because it routes RSVPs elsewhere.
3. Otherwise prefer the more complete `location`, then the URL on the
   group's own domain.

Put the overlay on whichever half is the duplicate — that may be an event
approved on an earlier pass, not the one in the current queue.

```
set_overlay(duplicate_guid, {"duplicate_of": canonical_guid},
            "{GroupA} re-post of \"{title}\"; canonical is the {GroupB} entry",
            "qc-<date>")
```

**Out-of-area.** DC metro = Washington DC; Northern Virginia (Arlington,
Alexandria, Fairfax, Reston, Tysons, McLean, Vienna, Herndon, Ashburn,
Sterling, Loudoun/Prince William counties); Maryland suburbs (Montgomery
County, Prince George's County, Bethesda, Silver Spring, Rockville, College
Park, Greenbelt). Flag events clearly located in a distant city; confirm via
the event page if ambiguous before acting. Never judge from the group name
alone. Virtual/hybrid events (`location_type` virtual/hybrid) are never
out of area.

```
set_overlay(guid, {"hidden": true},
            "{Group} event — located in {City}, not DC metro", "qc-<date>")
```

For each event in the queue: apply overlays where warranted, then
`resolve_qa_review(guid, "approved")` once you've made a decision either way,
or `"flagged"` if something's wrong in a way you can't fix yourself (a
broken feed, a duplicate you can't resolve, an unresolvable location).

## Pass 2: Polish (title/location/categories) — lower bar, act on clear improvements

Run this only on events that survived pass 1 (not hidden/marked duplicate).
Every field here is a correction, not a removal — the event stays either
way, so act on a clear improvement rather than holding out for certainty.
It's still not license to rewrite: the test is whether the page contradicts
the entry, not whether you'd have phrased it differently.

**Overlay fields for this pass, and only these:** `title`, `location`,
`categories`. You cannot hide an event or take one out of the review queue
in this pass — pass 1 already handled both.

**Titles.** Fix only boilerplate ("Monthly Meetup", "Event"), unfilled
templates ("{{topic}}", "TBD title"), or a title that's mostly the group
name repeated when the page has a real subject. Leave alone: emoji,
capitalization, a group-name prefix, non-English titles, anything already
specific.

**Locations.** Fix empty/filler ("TBA", "See event page", "Online" on an
in-person event), a bare room with no building/city, or a venue name you can
resolve to a real address (WebSearch). Write `Venue, City, ST`. Never invent
a street address you didn't read, never move an event between cities (pass
1's job, already done) — except correcting an impossible pair like
"Arlington, DC" → "Arlington, VA", which is a label fix, not a move, and is
squarely your job when you're certain. Leave virtual/hybrid alone unless the
location field is empty.

**Categories.** Call `list_categories()` first; assign only from that list —
go by each slug's `description`, not its plain-English reading. Add
categories the page clearly supports, remove ones it contradicts. Two or
three well-chosen slugs beat six speculative ones. If existing categories
are already reasonable, write nothing — `categories` replaces the list
wholesale, so keep every slug you're not removing.

If you couldn't read the page at all, change nothing for that event and
note it as a fetch failure in your report.

One `set_overlay` call per event, carrying every field you're changing.

**Comments matter.** Say what was wrong, and where the new value came from
— kept-from-feed (just trimmed noise), read-off-the-page, or
resolved-by-search. These carry very different risk and a reviewer skimming
your report can't tell them apart without your saying so explicitly.

## Discovery: candidate groups and events

If you come across a DC-area tech group or event that isn't in the system
while doing the above (e.g. mentioned on a page you're reading), queue it
with `propose_group`/`propose_event` rather than publishing it yourself.
Use `verify_ical_feed` to sanity-check a feed URL before `propose_group`.
Check `list_discovery_proposals` first so you don't queue the same
candidate twice.

## If something needs undoing

`list_qa_run(run_id)` shows every overlay written under a given run id;
`revert_qa_run(run_id)` removes them all. Useful if a batch turns out to
have gotten a rule wrong partway through — revert the run and re-do it
rather than hand-correcting individual overlays.

## Report

At the end, report as a JSON object:

```json
{
  "run_id": "qc-<date>",
  "hidden": [{"guid": "...", "title": "...", "reason": "..."}],
  "duplicates": [{"guid": "...", "title": "...", "canonical": "...", "reason": "..."}],
  "polished": [{"guid": "...", "title": "...", "fields": {...}, "reason": "..."}],
  "flagged": [{"guid": "...", "title": "...", "reason": "..."}],
  "fetch_failures": [{"guid": "...", "title": "...", "url": "...", "reason": "..."}],
  "proposed_groups": [...],
  "proposed_events": [...],
  "reviewed": <count>,
  "queue_remaining": <true if list_pending_qa returned the full 15, i.e. more may be waiting>
}
```
