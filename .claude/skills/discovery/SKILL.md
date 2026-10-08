---
name: discovery
description: Proactively search for DC-area tech groups and one-off events not yet in dctech.events, by scanning the Eventbrite and Meetup listings and then sweeping every category (and a rotating set of specific technologies) through web search, and queue what turns up via propose_group/propose_event for human review. Use when asked to run discovery, find new DC tech groups, scout for events to add, or grow the calendar — distinct from calendar-qc, which only proposes something it stumbles on while triaging the existing queue.
---

# Discovery

This ports the retired `NextDiscoveryAgentStack` agent's logic (deleted
2026-09-01, recovered from git history at `69f82193^:cdk_next/agents/discovery/`)
into an interactive skill. That agent ran on its own weekly schedule,
independent of the calendar QC agent, and its whole job was to go looking
for what's missing — not to react to what turns up in the review queue.
`calendar-qc`'s "Discovery" section only proposes something noticed in
passing while triaging; this skill is the active version.

A run has two phases: **fixed listing sources** (Eventbrite, Meetup), which
surface what the platforms themselves rank highest, and a **category sweep**
(web search per category and per specific technology), which finds the
smaller groups those listings bury — a Salesforce admins group, a Unity users
group, an OpenSearch meetup. A run of the first phase on 2026-10-07 turned up
four groups and four events worth proposing out of roughly 40 candidates; the
rest was noise, dormant groups, or national series, which is the shape to expect.

You cannot publish anything directly. Every find goes through
`propose_group` or `propose_event`, which lands in the same review queue a
human (or the `approve_submission`/`reject_submission` tools) works through.
That's on purpose — discovery finds candidates, it doesn't vet them.

Tools used: `list_groups`, `list_discovery_proposals`, `list_categories`,
`get_events`, `verify_ical_feed`, `propose_group`, `propose_event`, plus
WebSearch and WebFetch, a browser MCP tool (`mcp__playwright__*`) for the
JS-heavy listing pages, and a second search tool (e.g. Tavily) when one is
connected. `probe_feeds.py`, next to this file, checks whether a feed is
alive. All dctech.events tools are namespaced `mcp__dctech-events__*`; if
they are missing the MCP server failed to connect (an expired SSO session is
the usual cause: `aws sso login`, then `/mcp` to reconnect) — stop and say so,
because nothing can be deduped or queued without them.

## Before any web work

Run these four first, every time, so you don't re-propose something that's
already tracked:

1. `list_groups()` — groups already on the site. Note each group's Meetup
   slug or Luma calendar id (from `website`/`ical`); that is what search
   results get matched against.
2. `list_discovery_proposals()` — candidates already queued, approved, *or
   rejected*. Don't re-propose a rejected one just because it turned up
   again this week; a human already said no.
3. `get_events(date_from=<today>)` — one-off events already active. This is a
   long list; filtering with `source="manual"` shows the one-offs, but an event
   can also already be present through a group's feed (FedTalks was, via
   DefenseScoop), so check a candidate's title against the full list before
   proposing it.
4. `list_categories()` — the topic list for the sweep. Read it every run so
   the sweep follows the real taxonomy instead of a copy that went stale.

## Scope

**Geography:** DC metro — Washington DC; Northern Virginia; the Maryland
suburbs (same list as `calendar-qc`'s out-of-area rule). A purely virtual
event or group is in scope only if the *organizing group* is DC-area — a
national group's webinar doesn't count just because anyone can join.
Telltale signs of one that doesn't: the same events listed under several
city-named groups (one AI performance-engineering series appeared under
"Washington DC", "Wash DC" and "Arlington VA" at once), titles that name a
national audience ("NA Huddle", "Global Virtual Job & Networking Event"), and
an organizer that is a vendor or training company running webinars.

**Subject:** technology — software, data, security, hardware,
design-for-tech, civic tech, and startup/founder events adjacent to those
communities. Not every professional meetup in the DC area is a tech event;
skip ones that are DC-based but not tech-subject (a general small-business
networking group, a real-estate meetup, an enterprise asset-management user
conference).

## Groups over events

**A recurring meetup is worth more than a single event listing.** When you
find one, spend the effort to locate its iCal feed (check the group's own
site and its Meetup/Luma page) and probe it before `propose_group`. Only
fall back to `propose_event` for something genuinely one-off (a summit, a
single workshop with no recurring organizer to follow up on) or for an
organizer who runs events but has no feed.

Feed URLs: Meetup is `https://www.meetup.com/<slug>/events/ical/`. A Luma
calendar's feed is `https://api.lu.ma/ics/get?entity=calendar&id=cal-XXXX`,
where the `cal-…` id is usually visible in the calendar page's source or its
subscribe link. Our existing Luma groups use `api.lu.ma` or `api2.luma.com`; use
whichever host serves a valid feed, and probe it before trusting it.

## Phase 1 — fixed listing sources

Two sources, every run:

- **`eventbrite.com/d/dc--washington/technology/`** — heavy client-side
  rendering; WebFetch comes back empty or partial. Use the browser tool:
  navigate, scroll to the bottom a few times so more cards load, then collect
  the `a[href*="/e/"]` links that match `tickets-<digits>` with their card
  text (title, date, venue, organizer). About 20 listings come back. High
  noise — most are generic professional networking, not tech-subject. If no
  browser tool is available, treat this source as failed for the run rather
  than guessing from a thin fetch.
- **`meetup.com/find/?keywords=technology&location=us--dc--Washington`** —
  prefer proposing the *group* behind a listing, not the individual event.
  The default view is events (about 27 groups); add `&source=GROUPS` for the
  groups view, which returns about 90 and is the one worth scanning. Scroll,
  then collect each group card's slug and description. Roughly half of the
  groups are already tracked.

`technical.ly/dc/events/` was a third source in the original agent's list
but is gone as of this skill's first real run (2026-09-27): both WebFetch
and Tavily's extractor returned a stale 2013 archive page, not a live
listing — the site no longer serves a working DC events calendar at that
URL. Don't re-add it on a guess; if it's ever worth scanning again, confirm
it renders a real, current listing first.

## Phase 2 — category sweep

The fixed sources only show what a platform ranks highest. This phase asks a
search engine about each topic directly.

**Which engine.** Use WebSearch (it is US-only and returns titles and URLs, so
follow up with WebFetch or the browser to read a page) and, when one is
connected, Tavily's search as a second opinion. Don't drive google.com's results
page through the browser: it serves CAPTCHAs and is against its terms. The
`allowed_domains` filter is what keeps results on the platforms that carry iCal
feeds.

**Topics.** Take them from two lists.

1. *Categories* from `list_categories()`, as search phrases using the category
   name and what its description says: Software Development, Microsoft
   Technologies, Gaming & Gamedev, Cybersecurity, Legal Tech, Government
   Technology, Open Source Community, Cloud Computing, Hardware & Electronics,
   Startups & Entrepreneurship, Blockchain & Crypto, AI & Machine Learning,
   Data & Analytics, Mobile Development, UX & Design, Apple Technologies,
   Technology & Media. The meta categories (Conferences, Social & Networking,
   Training & Workshops, Career & Professional Development, Vendor Conferences,
   Tech Culture, Coworking) aren't topics; for those search "tech conference",
   "tech happy hour", "coding workshop" and so on once each, with the region.
2. *Specific technologies*, which is where the small groups live. Every run
   takes one fifth of this list: number the items from 0 and use the ones whose
   position, modulo 5, equals the ISO week modulo 5 (`date +%V` gives the week),
   so five runs cover all of it: Python; Rust; Go; Java; JavaScript, TypeScript, React;
   Ruby on Rails; PHP, Drupal, WordPress; .NET and C#; Elixir and Erlang; Kotlin
   and Swift; Kubernetes and CNCF; Docker and DevOps; Terraform and
   infrastructure as code; AWS; Azure and Power Platform; Google Cloud and GDG;
   Salesforce; ServiceNow; Red Hat and OpenShift; Linux and BSD; PostgreSQL and
   databases; Snowflake, Databricks, Spark; Kafka and streaming; Elastic and
   OpenSearch; GIS and OpenStreetMap; robotics and ROS; drones; 3D printing,
   Arduino, Raspberry Pi; AR/VR, Unity, Unreal; quantum computing; health IT and
   FHIR; fintech and payments; OWASP, pentesting, DEF CON, ISSA; GRC, CMMC,
   compliance; identity, IAM, zero trust; accessibility; product management;
   QA and testing; agile; data visualization and BI; MLOps and LLMs.

**Queries.** One query style per run, chosen by `ISO week % 3`, so a run stays
around 25 searches and every style gets its turn over three weeks:

| `week % 3` | Style | Query | `allowed_domains` |
|---|---|---|---|
| 0 | Meetup | `{topic} meetup Washington DC` | `meetup.com` |
| 1 | Luma | `{topic} events Washington DC` | `lu.ma`, `luma.com` |
| 2 | Open web | `{topic} user group OR meetup Northern Virginia OR Maryland OR "Washington DC"` | none |

Rotate the place within a style too: DC proper, Northern Virginia (Arlington,
Reston, Tysons, Alexandria), and the Maryland suburbs (Bethesda, Rockville,
Silver Spring) turn up different groups. If a category's first query is all
already-tracked groups, that category is saturated for now; don't spend a
second query on it. If it returns an unfamiliar organizer, a second query
naming them is worth it.

**Budget.** Stop when the week's slice is done, around 25 searches, plus the
follow-up reads for real candidates. More is not better: the review queue is
read by a person, and 8 well-checked proposals is a good run. If the sweep turns
up more than about 15, propose the 15 you are most sure of and list the rest as
skipped with a reason.

## Triage: from a search hit to a proposal

Work every hit through these in order and stop at the first that rejects it.
Each step is cheap; the later ones cost a fetch.

1. **Already tracked?** Match its Meetup slug or Luma id against `list_groups`,
   and its title against the active events. Skip it.
2. **Already decided?** Check `list_discovery_proposals`. Skip anything
   rejected.
3. **In scope?** DC-area organizer, tech subject (see Scope). A national series,
   a vendor webinar mill, or a general networking group stops here.
4. **Find the feed**, then run
   `python3 .claude/skills/discovery/probe_feeds.py <slug-or-feed-url> ...`
   (several at once is fine). It reports upcoming events per feed and flags the
   patterns below. Then `verify_ical_feed` confirms the feed parses; `propose_group`
   re-verifies it.
   - **`up=0`** — nothing scheduled. The feed is valid and empty. Don't propose;
     put it on the watchlist (see Report). It will turn up again when it has
     events.
   - **HTTP 403** — a private or invite-only group. Its feed can't be
     aggregated. Skip it.
   - **Identical titles and dates under several slugs** — one organizer
     cross-listing. Skip all of them.
5. **Read the page, not a summary.** For a one-off event, take the date and
   venue from the page's own structured data: in the browser, read the
   `application/ld+json` block (`startDate`, `location`), or fetch the page.
   WebFetch summarises with a small model and has reported the wrong year
   ("October 22, 2020" for an event on 2026-10-22). For a group, whether its
   sessions are in person comes from its *future* events: a group page lists
   past events too, and one in-person session from June was once mistaken for
   a live in-person schedule. Count only events dated today or later.
6. **Virtual groups are fine, and are handled for you.** The aggregator reads
   each event page's JSON-LD (`eventAttendanceMode`) and flags online sessions
   `virtual`, which keeps them off the week pages, the newsletter, `/just-added/`,
   the roundup and the social queue; they appear on `/virtual/` only. So an
   online-only group with a DC-area organizer is in scope, as long as you say
   it is online-only in the evidence.
7. **Propose**, with `evidence` that states what a reviewer would otherwise have
   to go and check: how many events are scheduled and when the next is, in
   person or online and where, the category you chose and why, and anything you
   are unsure of ("generic event titles", "vendor-style summit"). Put the least
   certain proposals' doubts in plain words; that is what the reviewer reads.

## Judgement

Skipping is always valid. A missed group costs nothing until next week's
run finds it again or someone submits it directly; a wrongly-queued
proposal costs a human's review time and, if approved by mistake, puts an
out-of-scope group on the site. When a group's geography or tech-relevance
is genuinely unclear, skip it and note it — don't propose on a guess.

Three failure modes to watch for in your own work, all seen in practice:
treating a dormant group as a find (check `up=`), calling a group in-person from
a past event (check the dates), and trusting a page summary's date (read the
structured data).

## Report

At the end, report as a JSON object:

```json
{
  "groups": [{"name": "...", "website": "...", "ical": "...", "reason": "...", "draft_id": "..."}],
  "events": [{"title": "...", "date": "...", "url": "...", "reason": "...", "draft_id": "..."}],
  "skipped": [{"name": "...", "reason": "..."}],
  "watchlist": [{"name": "...", "website": "...", "why": "valid feed, nothing scheduled"}],
  "sources_scanned": ["..."],
  "sources_failed": ["..."],
  "sweep": {"week": 41, "query_style": "web", "technology_slice": 1, "searches_run": 25, "categories_saturated": ["..."]}
}
```

`watchlist` is the groups that were in scope and had a valid feed but nothing
scheduled; they're the first thing to re-check next run. `sweep` records which
slice of the rotation ran, so the next run can tell what has and hasn't been
covered. After the report, say plainly which proposals you are least sure of.
