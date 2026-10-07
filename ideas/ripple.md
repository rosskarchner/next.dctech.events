# Ripple: event-driven, change-only site rendering

Status: idea, verified against the code (2026-10-07). Nothing implemented.
Originally sketched 2026-10-02 from code-reading; a second pass checked each claim
against the repo and corrected several (see "What we verified"). Still no live AWS
checks and no measured build durations: items that need them are marked **[live]**.

## Motivation

Every relevant DynamoDB stream record starts a full CodeBuild run
(`dctech-events-next-site-generator`): full table scan, re-render of every OG card,
full Frozen-Flask build, `s3 sync --delete`, `create-invalidation /*`. A burst of 10
aggregator events means up to one full build per stream batch.

Goal: a new or updated event re-renders only what it touches (its page, its OG image,
the category/month/week/region pages it belongs to or used to belong to, the homepage
and feeds), like ripples settling after a stone drops. Bursts coalesce, so 10 events
cause one homepage render.

Incidental numbers (2026-10-02): CodeBuild ran 1,226 min in September (about 40
min/day, 100 min/month free), roughly $5.63 gross, currently netted to $0 by a credit.
The savings from this work are freshness and precision, not money.

## What we verified (2026-10-07)

Confirmed as the sketch said:

- Stream trigger starts a build per relevant batch: `site_generator/trigger/handler.py:36-61`;
  mapping is batch 1000 / 90s window / `retry_attempts=1`, `concurrent_build_limit=1`,
  30 min build timeout (`site_generator_stack.py:167-216`).
- Buildspec order: export, `calgen pipeline`, `calgen og-images`, `calgen build`,
  two `s3 sync --delete` passes, `create-invalidation /*` (`site_generator_stack.py:36-95`).
- Sidebar is not one global fragment (`get_sidebar_data`, `routes/common.py:288`);
  `base.html` does not load htmx (only `homepage.html:44` and `virtual_events.html:34`).
- Event slugs come from `calgen.event_utils.event_slug`; `newsletter/render.py` already
  runs calgen + `site/` + the Flask test client in Lambda.

Corrections and additions the sketch missed:

1. **The stream trigger is already a doorbell, and a lossy one.** It filters on the PK
   prefix only (`EVENT# GROUP# CATEGORY# RECURRING# ICAL# POST# UPDATE#`), never reads
   the images, and calls `StartBuild`. Identical re-puts therefore start builds. A
   second concurrent `StartBuild` raises `AccountLimitExceededException`; the handler
   re-raises, the mapping retries the batch once, and after that the work is dropped
   until the daily 09:00 UTC build. The 2026-09-28 Monday failure is this race; commit
   `cb7896a` only widened the state machine's retries.
2. **The stream carries both images** (`NEW_AND_OLD_IMAGES`, `dynamodb_stack.py:28`), so
   a doorbell can skip no-op puts and could classify changes without a full diff.
3. **`ARCHIVE#` writes do not trigger a build**, yet the export reads them
   (`_archive/`), which feed `/updates` and archived week pages. Those can go stale
   until the next build. Confirm what renders from `_archive/` before adding the prefix.
4. **Three stream mappings already exist** (build trigger, social publisher, social
   enqueue). The doorbell must replace the build trigger, not sit beside it. All
   current filters are allowlists, so a `RENDER#` item is ignored by them.
5. **The manifest cannot live in the site bucket.** Origin access control makes the
   whole bucket readable through CloudFront, so `_state/manifest.json` would be public,
   and the buildspec's non-HTML `s3 sync --delete` (`site_generator_stack.py:89`,
   excludes only `edit/*`) would delete it. Use a DynamoDB item (or items) or a private
   bucket/prefix.
6. **Frozen-Flask's relative URLs only rewrite `url_for` links.** Templates also
   hardcode absolute hrefs (`partials/sidebar.html:28,43`; `_category_url` and
   `_month_url`, `routes/common.py:195-207`), so the frozen output is a mix. A
   test-client renderer emits absolute `url_for` links. The parity harness is therefore
   required, not optional.
7. **Byte-diffs need normalization.** iCal `DTSTAMP` (`common.py:659`) and RSS
   `lastBuildDate` (`common.py:676`, `posts.py:86`) change on every run. Without
   normalizing them, "upload only pages whose bytes changed" uploads every feed.
8. **A timezone inconsistency matters for Lambda.** `get_events` uses `local_tz`
   (`common.py:90`) but `get_upcoming_weeks` uses `date.today()` (`common.py:437`),
   which is UTC on Lambda and CodeBuild. Between 8 PM and midnight Eastern the week
   set could start a day early, which only matters on Sunday evenings. It was the only
   `date.today()` in the render path; the other "today" call sites (`listings.py:72,86,144,190`,
   `pipeline.py:287,330`, `common.py:90,378`) already use `local_tz`. **Fixed 2026-10-07**
   (Phase 0f, `next_dctech_events-5dm.6`).
9. **OG cards are generated for every event in `all_events.json`, past ones included,
   with no cache** (`cli.py:120-236`), and always overwritten. A card-input hash cache
   is a standalone win. Pillow is only in the optional `[cards]` extra because no
   cross-compiled manylinux wheel was available under `uv`
   (`packages/calgen/pyproject.toml:30-40`); today only CodeBuild installs it. A Lambda
   renderer needs a layer or container image; the repo has no Docker usage yet.
10. **Caching is different from "HTML revalidates, the rest is fine".** HTML is uploaded
    with `max-age=0, must-revalidate` but the distribution uses `CACHING_OPTIMIZED` for
    everything (`hosting_stack.py:109`), so CloudFront probably clamps up to a 1s
    minimum TTL and revalidates against S3 on nearly every request **[live: confirm the
    policy's min TTL]**. `.json .ics .xml .png` are uploaded with `max-age=86400`, so
    those are the files that need targeted invalidation, and `sidebar-counts.json`
    needs an explicit short `Cache-Control` at upload (the sync default would give it a
    day). Also: a 403/404 from the origin is cached for 5 minutes as `/404.html`
    (`hosting_stack.py:153-166`), so a newly created page requested before it exists can
    stay 404 for 5 minutes. Write new pages before anything links to them, or
    invalidate them. Invalidation limits on the flat-rate plan are not documented in the
    repo **[live]**.
11. **`updates_publisher` reads the live `https://dctech.events/events.json`**
    (`updates_publisher/app.py:59,91-95`) on purpose: it is the authority for "what the
    site is showing", so a draft can never leak into a post and the roundup never
    announces an unpublished event. The Monday ordering (build, then publish, then build)
    depends on it (`orchestration_stack.py:13-15,33-37`). Keep that coupling unless a
    renderer can guarantee the same "published set" from its manifest.
12. **The export is a full-table Scan on every run** (`export_dynamo_to_calgen.py`;
    the `SK = META or OVERRIDE#` filter runs after the read, so every item is read).
    Measured: the table is 1,162 items / 1.5 MB, so a Scan is about 360 read units
    and the export step takes ~14s end to end. Cheap, as the sketch assumed.
13. **The renderer must never touch `edit/*`.** It is deployed separately
    (`cicd_stack.py:72-84`) and the buildspec's filter-order comment
    (`site_generator_stack.py:65-88`) records a production deletion of `edit/`.
14. **Output size is known now** (see "Measurements"): 1,145 objects, 24.9 MB, of which
    431 HTML pages (269 under `/events/`), 358 PNG (337 OG cards), 297 `.ics`, 30 `.xml`,
    4 `.json`. Not "thousands of files", as the buildspec comment says.

## Measurements (2026-10-07, Phase 0a)

From the last 100 builds, the trigger and build CloudWatch logs, an S3 listing and
`describe-table`. Times are for the `dctech-events-next-site-generator` project.

| What | Number |
|---|---|
| Whole build | median ~4 min (BUILD phase median 210s, p90 226s, max 304s); 30 min timeout |
| Where one build's time goes (build of 07:02) | pip install ~27s, export ~14s, pipeline ~3s, OG cards ~20s, `calgen build` (Frozen-Flask) **~172s**, sync + invalidate ~15s |
| Builds per day | ~8 (49 trigger-started in 7 days, plus the daily 09:00 UTC rule, plus Monday's two) |
| Cadence | a build at `:28` past every 4th hour, from the stream trigger: the iCal aggregator writes 1-20 relevant records per run, and every slot in the last week had at least one |
| Trigger collisions | 208 `AccountLimitExceeded` failures in 7 days, in 59 distinct minutes, all within a minute or two of a build starting |
| Output | 1,145 objects, 24.9 MB: 431 HTML (269 events), 358 PNG (337 OG), 297 `.ics`, 30 `.xml`, 4 `.json` |
| Table | 1,162 items, 1.5 MB; one export Scan is about 360 read units |

What this changes:

- **The Lambda 15-minute question is answered.** A full render, install included, is
  about 4 minutes; the render alone is ~3. A Lambda could run the *whole* build, not just
  dirty pages.
- **Frozen-Flask is the cost, and it is slow for what it does.** 431 HTML pages plus
  ~700 other files in 172s is about 0.15 s per file. Before building a diffing renderer,
  find out why. `get_sidebar_data` runs per request and walks the event list, so it is a
  suspect. If a profile finds a 5-10x win, a full rebuild becomes cheap enough that the
  doorbell/coalescing in Phase 0 delivers most of what the renderer was for. Tracked as
  a bead.
- **OG cards are ~8% of a build** (~20s), so the card hash cache (0g) saves at most that,
  and only after adding persisted cards, pruning and a sync step. Not worth it yet.
- **The aggregator causes nearly every build**, and it already skips unchanged writes
  (`ical_aggregator/handler.py:_persist_group`), so these are mostly real changes. One
  consequence worth knowing: because a build lands at :28 past every 4th hour, including
  00:28 Eastern, past events usually drop off just after midnight today *by accident*.
  Once no-op rings are ignored (0c) that accident goes away, which is why 0d exists.
- **The "wasted builds" were never the aggregator's no-op writes**, which are already
  suppressed. What was wasted is the 208 failed invocations a week, the follow-up that
  never ran, and the extra full build that the Monday refresh causes.

## Phase 0: cheap wins (no renderer)

Each item stands alone on the current CodeBuild pipeline. Together they fix the
failure modes above and give the measurements that decide whether a renderer is worth
building. Status 2026-10-07: 0a, 0f done; 0b-0e implemented and tested, **not yet
deployed**; 0g deferred.

- **0a. Measure. Done.** See "Measurements".
- **0b. Single-flight with a follow-up. Implemented.**
  - The trigger (`trigger/handler.py`) stamps `RENDER#site.dirtyAt` *first*, then tries
    `StartBuild`. A refusal is no longer raised; the stamp is the promise.
  - `trigger/followup.py`, fed by an EventBridge rule on CodeBuild state change (terminal
    states), starts exactly one more build if `dirtyAt` is later than when the finished
    build read the table.
  - "When the build read the table" is `RENDER#site.exportAt`, stamped by the build
    itself just before its Scan (`export_dynamo_to_calgen.py --stamp-export`). Comparing
    with the build's *start* time would have added a redundant 4-minute build after
    almost every aggregator run, because several stream shards invoke the trigger a few
    seconds apart and the later ones collide inside the ~30s before the export. If the
    stamp is stale or missing it falls back to the start time, which only errs towards
    one extra build and cannot loop.
  - A failed build does not retry itself (a persistently broken build would loop); it
    only follows up rings that arrived after it read the table.
  - IAM is scoped to the `RENDER#site` partition key with `dynamodb:LeadingKeys`. The
    stream event source now has a prefix filter, so the state item never invokes the
    trigger.
  - Not changed: the Monday state machine's `.sync` steps still retry through a busy
    slot themselves.
  - Stays true: tests have no CI gate (see Testing). Run
    `cd cdk_next/lambda_src/site_generator/trigger && python -m pytest`.
- **0c. Skip no-op stream records. Implemented**, with a caveat. The trigger compares old
  and new images and ignores a MODIFY whose only differences are in `NON_SEMANTIC_ATTRS`
  (`updated_at`, `ttl`, `meta`: what the iCal cache re-stamps on every put). Inserts,
  removes and records without both images always count. Because the aggregator already
  suppresses unchanged writes, expect this to remove little; every ring now logs what
  changed (`EVENT:MODIFY:overrides,...`), so the list can be tuned from real traffic.
- **0d. Midnight Eastern rebuild. Implemented.** An EventBridge rule at `cron(5 5 * * ? *)`
  (00:05 EST, 01:05 EDT, so always after Eastern midnight) rings the same trigger with
  `{"reason": "time_roll"}`, so it coalesces with a running build. A fixed UTC cron
  rather than EventBridge Scheduler, matching `updates_stack.py`'s existing choice.
  Costs one extra build a day.
- **0e. `ARCHIVE#` writes trigger a build. Implemented.** Only `updates_publisher` writes
  them, alongside `UPDATE#`, so this mostly closes a gap that was already covered.
- **0f. Fix the `date.today()` timezone inconsistency. Done** (one call site, with a
  regression test).
- **0g. OG card hash cache. Deferred.** Measured at ~20s of a ~260s build (see
  "Measurements"). It would need persisted cards (pull from S3 before generating),
  hash metadata in the PNGs and pruning of cards for aged-out events, all to save about
  8%. Revisit after the build profile.

Gate: after Phase 0, if freshness and build minutes are no longer a problem, stop. The
renderer is justified only by what is left, and the build profile (above) comes first.

## Core idea: stream is a doorbell, a diff is the truth

The stream consumer stops starting builds. It only signals "something changed". A
renderer re-runs the existing export + pipeline, diffs the result against the last
published snapshot, and derives the dirty pages from the diff.

The diff catches what stream-record parsing would miss: group category changes,
overlays and hiding, region rejection, dedup (`also_published_by`), recurring
expansion, and "used to belong to" category pages (the old version of each event is in
the previous snapshot).

```
DDB stream -> doorbell Lambda -> sets RENDER#state {dirty, lastRingAt}; starts "settle" run
                                      |
          Step Functions: wait until quiet 45s (cap 5 min), take lease
                                      v
          renderer (calgen + site/ + Pillow; create_app + Flask test client)
            1. export + pipeline            -> new event set
            2. diff vs stored manifest (guid -> content hash, page -> normalized output hash)
            3. dirty pages = union over the whole diff   (dedup happens here)
            4. render each dirty page once; skip OG if card hash unchanged
            5. upload only pages whose normalized bytes changed; delete orphans
            6. invalidate changed non-HTML paths; write new manifest
            7. if the doorbell rang during the run, loop
```

Step Functions `Wait` and `Choice` states are not used anywhere in the repo yet; the
Monday machine is a linear chain of Lambda and `.sync` CodeBuild steps
(`orchestration_stack.py:164-170`).

## State store

Not the site bucket (finding 5). Options:

- **DynamoDB items under `RENDER#`** (preferred): `RENDER#state` for the doorbell/lease
  (`leaseExpiresAt`, `ttl`, conditional update) and `RENDER#manifest#<n>` shards for the
  guid and page hashes. Matches the `WRITERATE#` / `SOCIALQUEUE#` conventions, and the
  400 KB item limit means the manifest must be sharded or stored compressed in S3 with a
  pointer item.
- **Private S3 bucket** for the manifest only; the lease stays in DynamoDB.

Whichever it is, `RENDER#` writes must stay out of every stream allowlist (finding 4).

## Batching (10 events -> one homepage render)

- Stream batch window (existing: 1000 records / 90s) collapses a burst.
- Quiet-period wait: no ring for 45s (cap 5 min) so a multi-batch aggregator run lands
  in one drain.
- Dirty pages are a set; the homepage appears once however many events touch it.
- The lease makes it single-flight. A ring during a render causes one follow-up pass.
  This also removes the Monday state machine's lost build-slot race (if CodeBuild is
  demoted, the Monday steps call the renderer instead of racing for a build slot).

## Ripple map

| Diff entry | Pages re-rendered |
|---|---|
| Event added / changed / removed | `/events/<slug>/`, `event.ics`, its OG card |
| Slug changed (title edit) | new page + card written, old ones deleted |
| Old and new categories | `/categories/<slug>/`, its month pages, `feed.ics`, `feed.xml`, category OG |
| Old and new dates | `/week/<id>/` and `/<y>/<m>/` for both, plus category-month pages |
| Region | `/locations/<r>/` and its `feed.ics` / `feed.xml` |
| Virtual flag | `/virtual/` |
| New in last 30 days | `/just-added/` |
| Any visible add/remove | home, `/events.json`, `/events.ics`, `/events-feed.xml`, `/sitemap.xml` |
| Group metadata | `/groups/` and pages of that group's events |
| `UPDATE#` / `POST#` | `/updates/...` and its feed |
| Category or month appears/disappears | all pages that render the sidebar link lists (rare) |

Also always-on for the home/total counts: homepage totals, `get_recently_added_count`,
`/categories/` per-category counts, `/feeds/` counts (single pages, cheap).

The full route list the renderer must cover (from `freeze.py`): `/`, `/virtual/`,
`/week/<id>/`, `/<y>/<m>/`, `/just-added/`, `/groups/`, `/events/<slug>/` and
`event.ics`, `/locations/` (only with a region plugin), `/locations/<slug>/` and its
feeds, `/categories/`, `/categories/<slug>/` and `<y>/<m>/`, `/categories.json`, category
feeds, `/feeds/`, `/sitemap.xml`, `/events.json`, `/events.ics`, `/events-feed.xml`,
`/updates/` and its pages and feed, `/404.html`, `/robots.txt`. Frozen-Flask also copies
`static/` wholesale, including `static/og/*.png`, and `calgen build` copies
`static/categories.json`.

## Sidebar counts (the one global dependency)

Finding: the sidebar is NOT one global fragment. `get_sidebar_data(active_category,
active_month)` (`packages/calgen/src/calgen/routes/common.py:288`) makes the two lists
depend on each other: category counts are constrained to the active month, month counts
to the active category, links carry the other axis's selection, and zero-count entries
are hidden (except the active one). Active highlighting uses `request.path`. Callers:
home, month, category, category-month, just-added, groups.

Options considered:

1. Drop the counts. Simplest; sidebar then depends only on which category/month combos
   exist.
2. HTMX fragment per (category, month) state. Works, but creates many fragments and a
   fan-out of the X row and M column of the matrix per event; HTMX is also only loaded on
   `homepage.html` and `virtual_events.html` today (unpkg htmx.org@2.0.4), not in
   `base.html`.
3. (Preferred) Server-render the link lists into each page (they change only when a
   category/month combo gains its first or loses its last event) and emit one
   `/data/sidebar-counts.json` with the category-by-month count matrix
   (`get_category_month_counts()`, `common.py:260-277`, already computes it). Fill the
   `<span class="count">` elements with about 20 lines of vanilla JS. Without JS the links
   still render (and stay crawlable), only the numbers are missing. An event add then
   rewrites one JSON file plus the pages it belongs to.

Upload `sidebar-counts.json` with `Cache-Control: max-age=60` explicitly (finding 10); a
count one minute stale is harmless. Links must stay root-relative (Frozen-Flask rewrites
to relative URLs, and nested paths like `/categories/x/2026/10/` would otherwise break);
see finding 6 for why the test client does not do this for you.

## Time-dependent output

About a third of the site depends on "today": `get_events` drops past events
(`common.py:90`), so every event-derived page changes at midnight Eastern even with no
data change; the 21-day homepage window and `filter_events_to_upcoming_days`
(`common.py:378`); the week set (`common.py:437`, UTC, see finding 8); `is_past` flags;
rolling 90-day recurring expansion; the `/just-added/` window; RSS `lastBuildDate`; iCal
`DTSTAMP`.

Add an EventBridge Scheduler job at 00:05 `America/New_York` that rings the doorbell with
`time_roll`. The diff then yields removals; window-dependent pages are always marked
dirty. This is the same job as Phase 0d, so 0d ships first on CodeBuild and the renderer
inherits it.

## Existing pieces to reuse

- `cdk_next/lambda_src/newsletter/render.py`: already runs calgen + `site/` + Flask
  test-client rendering in Lambda (`build_site()`, `render_for_filters`). It copies the
  site to `/tmp/calgen-site`, `chdir`s, runs the export script, `reset_config()`, then
  `calgen.pipeline.main()`. A renderer needs the same global-state handling.
- `cdk_next/build_lambdas.sh`: bundles zips with
  `uv pip install --python-platform x86_64-manylinux_2_17`; the calendar_qc asset uses
  aarch64 and is about 92 MB zipped. Pillow is not in the newsletter bundle.
- `calgen.event_utils.event_slug`: single source for per-event URLs.
- `packages/calgen/src/calgen/og_image.py`: Pillow-only; every `render_*_card` takes
  plain fields and returns an `Image`, which makes a card-input hash cheap to define.
- `social_queue_enqueue` / `social_queue_worker`: template for a filtered stream
  consumer, constant-PK GSI idiom and the "write-back must not retrigger the build" rule.
  It is not a single-flight template (the hourly schedule is its serializer).

## Rollout

0. **Phase 0** above (0a-0g). Independent of everything below.
1. Shadow mode: write the manifest and diff, log the predicted dirty set, no writes.
   Exit: over N days the predicted set is a superset of what actually changed.
2. Parity harness: render the full site through the renderer to a staging prefix and
   byte-compare with the CodeBuild output, after normalizing `DTSTAMP` / `lastBuildDate`
   and accounting for the relative-URL difference (findings 6, 7). Exit: no unexplained
   diffs.
3. Event pages + OG cards live (the isolated tier).
4. Aggregate pages live (with `sidebar-counts.json` + JS), then the midnight time roll.
5. Demote CodeBuild to a weekly reconcile and the manual full-rebuild paths
   (`trigger_rebuild` in `mcp/server.py:488-494`, `/api/admin/rebuild` in
   `api/routes/admin.py:247-257`, `deploy-next.yml` `run_site_build`). A
   renderer/template version hash in the manifest forces a full render on template or
   code change. Reconcile must be allowed to delete orphans without touching `edit/*`.

## Testing

No CI runs tests (`.github/workflows/` has only `deploy-next.yml` and
`dependency-audit.yml`; the `CLAUDE.md` "Build & Test" section is a placeholder). Lambda
tests live beside the code (`test_*.py`, run with `python -m pytest` from the function
directory) and use `monkeypatch` plus hand-rolled fakes; there is no moto. Lease and
conditional-write logic needs a `FakeTable` that models `ConditionExpression` (the
existing one in `api/test_rate_limit.py:18` does not), and Phase 0b should add a test gate
before it ships. calgen tests are in `packages/calgen/tests/`.

## Open questions

Need live AWS **[live]**:

- CloudFront `CACHING_OPTIMIZED` minimum TTL, and so whether HTML invalidation is
  redundant.
- Invalidation limits or charges under the flat-rate plan.

Need a decision:

- State store: DynamoDB-sharded manifest or private bucket (see "State store").
- Whether to keep the `updates_publisher` to `events.json` coupling (finding 11).
- Lambda zip vs container image for Pillow, and whether to cache OG cards in S3 or
  regenerate the changed ones only.
- Redirects for renamed/removed slugs: today they 404 via `sync --delete`; the renderer
  needs its own deletion path (renamed, hidden, merged, aged out). Redirects are extra
  work and not a regression to skip.
- Why `calgen build` takes ~172s for ~1,150 files, and what a profile shows. Do this
  before any renderer work.
- Whether `NON_SEMANTIC_ATTRS` (0c) needs more entries; read the trigger's logs after
  deploy.
- Whether a `.sync` Step Functions CodeBuild step can ever participate in the dirty/
  follow-up scheme, or the Monday machine keeps its retries until Phase 5.
