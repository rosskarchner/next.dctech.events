# Highlighted events

## Context

Ross wants a way to give individual events extra visibility, and a second revenue line next to the $50 weekly sponsorship. A **highlighted** event appears in a box at the top of the homepage, category pages, and month/week pages, and in the Monday newsletter, for the 21 days before it happens. Ross can highlight any event for free ("Editor's pick"), or anyone can pay $10 ("Paid"). Every highlight carries a label linking to an explainer page, so readers can tell paid placement from editorial picks.

Decisions made while planning (Q&A):
- Paid highlights go **live right away** on capture. There's no approval queue and no per-purchase email to Ross. Refunds stay manual in PayPal.
- **No cap.** Every active highlight shows, sorted by date.
- **Recurring series:** $10 buys **one occurrence**.
- **Entry point:** a "Highlight this event: $10" link on each eligible event page.
- **Late purchases** are allowed at the full $10. Checkout states how many days and newsletters are left.
- **Labels:** "Paid" for paid highlights and "Editor's pick" for admin ones. Both link to the explainer page. Buyers are never named; checkout collects only a contact email.
- **Box scope** matches the page: a category page shows only highlights in that category, and a month or week page only highlights in that range. If nothing matches, the page has no box.
- A highlighted event **stays in its normal day list too**, with the same small label.
- **In-person events only.** Virtual events get no highlight link.
- **Management:** MCP tools, plus an admin page at `/edit/highlights.html`, plus a toggle in the existing `/edit` events list.
- **Reschedules:** the highlight follows the event. If the event disappears, the build emails Ross and the buyer once.

### Chosen visual design (from the mockup round)

- **Box:** a single bordered card titled "Highlighted", with a "what's this?" link to `/highlights/` in the corner. Each event is **one line**: `11/4 8am  BSides NOVA [Editor's pick]`, giving the short date, the short time, the title, and the label pill. Titles link to the **event's own external URL** (`event.url`), never our `/events/` copy. Lines wrap naturally on phones. Location and group are left out.
- **No highlights means no box.** When a page (or a subscriber's newsletter) has no matching active highlight, the box is omitted entirely: no empty card, no heading, no placeholder or "highlight your event" prompt. The partial is wrapped in `{% if highlights %}`, and calgen tests assert it is absent from the homepage, category, month, and week pages and from both newsletter formats when there are none.
- **Inline (day list):** a label pill by the title, styled like `.recurring-badge`, **plus a colored left accent border** on the card.
- **Admin label text:** "Editor's pick".
- **Newsletter:** the same single lines right after the sponsor block: "Highlighted (what's this?)" followed by `date time title [label]`, with titles linking to the external event URL.
  - The plain-text version puts the external URL under each line and ends with "About highlights: https://dctech.events/highlights/".
- **Event page:** a modest "⭐ Highlight this event: $10" line in `.event-actions`, under Add to calendar / Suggest a correction.
- **Checkout:** one step.
  - An event summary card.
  - "Shows from <start> to <event date>: N days on the site, in M Monday newsletters (to subscribers whose preferences match). Labeled "Paid". Live as soon as PayPal confirms. One occurrence only."
  - An email field, a "Pay $10 with PayPal" button, and a link to "How highlighting works".

## Design

### Data model (single table, mirroring `SPONSOR#`)

- **`HIGHLIGHT#<target_key>` / `META`**: one row per highlighted occurrence. The key is the lock, the same way `SPONSOR#<week>` locks a week.
  - `target_key` is `g-<guid>` for stored events (iCal, submitted, or manual) and `r-<recurring_slug>-<YYYY-MM-DD>` for a recurring occurrence. Recurring guids are recomputed on every build, so the series slug and date are the stable identity, the same reasoning `pipeline.py` uses for per-date overrides.
  - Fields:
    - `booking_id`, `kind` (`paid` or `editor`), `status`, `contact_email`, `price_usd`, `txn_id`, `payer_email`, `hold_expires_at`, `created_at`, `updated_at`, `revoked_by`.
    - A snapshot taken at purchase: `title`, `date`, `url`, `group`, `guid`, `recurring_slug`.
  - Statuses: `held`, `payment_pending`, `active`, `revoked`, `refunded`, `cancelled`. A free-target condition like `_WEEK_FREE_CONDITION` lets a new buyer take a target whose row is revoked, refunded, cancelled, or an expired hold. Editor picks are written straight to `active`.
- **`HIGHLIGHT#<target_key>` / `PAYMENT#<txn>#<status>`**: an IPN record, as for sponsorships.
- **`HIGHLIGHT#<target_key>` / `RESOLVED`**: build-maintained matching state: `resolved_guid`, `last_seen_at`, `missing_since`, `lost_notified_at`. It lives on its own SK so the site trigger can ignore it (see below).

### Payment: reuse the sponsorship PayPal path

- Pull the PayPal-generic parts of `cdk_next/lambda_src/api/sponsorships.py` into a new `cdk_next/lambda_src/api/paypal.py`:
  - `paypal_form_url`
  - field building
  - `verify_with_paypal`
  - custom-field parsing
  - `_record_payment`
  - the receiver, amount, and currency checks
  - `_send`

  Sponsorships keep their behavior; their existing tests must still pass unchanged.
- New `cdk_next/lambda_src/api/highlights.py`, shared by the API and the MCP server like sponsorships, holds the rules:
  - `quote`, `create_hold` (30 min, conditional put, `replace_booking_id`), `handle_ipn`
  - `add_editor_pick`, `revoke`, `list_highlights`, `set_eligibility`
  - emails: a receipt to the buyer when payment completes (when it starts showing, and a link to the event); an approved/refunded notice is not needed.
- **One IPN endpoint.** The highlight `custom` value is `highlight|<target_key>|<booking_id>`. The `ipn` route in `routes/sponsorships.py` sends anything starting with `highlight|` to `highlights.handle_ipn`. Payment states:
  - Completed: `active`
  - Pending: `payment_pending`
  - Refunded or Reversed: `refunded`
  - Failed or Denied: `cancelled`
- **Price** is `HIGHLIGHT_PRICE_USD="10"` in `cdk_next/config.py` and in `common_env` (`api_stack.py`). The matching `highlight_price_usd: "10"` goes in `site/config.yaml`, with a parity test like `test_sponsorships.py:590`.
- **Rate limits** use `db.check_and_record_write` with new actions `highlight` and `highlight_hold`, mirroring `routes/sponsorships.py:79-83`.

### Checkout and API

- **Event page link.** `site/templates/event_page.html` gets a "Highlight this event: $10" link in `.event-actions` to `/edit/highlight.html?guid=<guid>`. It shows only when the event is in person, upcoming, and not already highlighted. A highlighted event's page shows its label instead.
- **`GET /api/highlights/quote?guid=`** (public):
  - Looks the event up in the latest built `events.json`, read straight from the site bucket with S3 GetObject (the API role gets read on that one key). That avoids CloudFront caching and needs no copy of calgen in the API.
  - Returns:
    - the event summary
    - the `target_key` (recurring events → series slug + date)
    - eligibility (in person, upcoming, not already active or held)
    - the start date (`max(today, date − 21)`)
    - the days left and the count of Monday newsletters left
- **`POST /api/highlights`** (public, rate-limited) takes `{guid, contact_email, replace_booking_id?}` and returns the PayPal form fields. API Gateway wiring follows the sponsorship public resources in `api_stack.py:295-303`.
- **`frontends/edit/highlight.html` + `js/highlight.js`** are modeled on `sponsor.html`/`sponsor.js`: show the quote, take an email, keep the hold in sessionStorage, submit to PayPal, and handle `?status=returned|cancelled`.
- **Admin routes**, under the Cognito-authorized `/api/admin` proxy:
  - `GET /api/admin/highlights`
  - `POST /api/admin/highlights` (editor pick by guid)
  - `POST /api/admin/highlights/{key}/revoke`

### Admin tools

- **MCP tools** in `cdk_next/lambda_src/mcp/server.py`, next to the sponsorship tools (`:1117-1225`), using the same `_call` error wrapping. Leave them out of `SCHEDULED_AGENT_TOOLS`.
  - `list_highlights(status?)`
  - `highlight_event(guid, note?)`, which creates an editor pick
  - `revoke_highlight(target_key, reason?)`
- **`frontends/edit/highlights.html` + `js/highlights.js`:** an admin list (event, kind, status, window, buyer email) with Revoke and "Add editor's pick". Linked from `frontends/edit/index.html` like sponsorships.
- **"Highlight" toggle** in `frontends/edit/events.html` for stored events. Recurring occurrences go through MCP or the admin page by guid.

### Build: export, matching, rendering

- **Trigger.** Add `HIGHLIGHT#` to `RELEVANT_PREFIXES` (`lambda_src/site_generator/trigger/handler.py:42-47`) and to the stream filter (`site_generator_stack.py:46-48`). Filter it to `SK=META` so reconcile writes to `RESOLVED` never retrigger a build. Skip `held` META images so abandoned checkouts don't trigger builds.
- **Export.** `export_dynamo_to_calgen.py` writes `_data/highlights.json`: active rows with `kind`, the target fields, the snapshot, and `resolved_guid`.
- **Matching.** New `packages/calgen/src/calgen/highlights.py`, called from `pipeline.main()` after `remove_duplicates`. For each highlight it finds the event:
  1. Recurring target: `recurring_slug` + date.
  2. Otherwise `guid`, then `resolved_guid`, then follow `duplicate_of`/`also_published_by` to the surviving parent.
  3. Fallbacks for reschedules or iCal guid churn (the guid hashes date, time, title, and url): the only upcoming event with the same group and url, then the only one with the same group and normalized title.

  On a match it sets `event['highlight'] = {'kind': ..., 'key': ...}`. It writes `_data/highlight_matches.json` (key → guid or null) for reconciling.
- **Window helper.** `select_highlights(events, today)` in `routes/common.py` returns the in-person events with `highlight` whose start date is within 21 days (reuse `UPCOMING_WINDOW_DAYS`) and not past, sorted by date and time.
- **Rendering.**
  - New partial `site/templates/partials/highlight_box.html`: one line per event (short date, short time, title linked to `event.url`, and the label pill), plus a "what's this?" link to `/highlights/`.
  - Each route passes `highlights=select_highlights(<the page's already-filtered events>)`:
    - homepage (`listings.py:82`)
    - month (`:148`) and week (`:117`), only current or future pages
    - category (`categories.py:32`) and category-month (`:50`)
  - Templates include the partial above the day list: `homepage.html`, `month_page.html`, `week_page.html`, `category_page.html`, `category_month_page.html`.
  - `partials/events_by_day.html` shows the inline label pill next to `.recurring-badge` and adds a modifier class to the `.event` card for the left accent border.
  - **Class names must avoid ad-blocker selectors**, the same lesson as `.masthead-note` (`main.css:173-178`). Use neutral names like `.pick-box` and `.pick-label`, never `sponsor`, `promoted`, `paid`, or `ad`. Check them against EasyList before settling, and add a test like `test_header_slot_avoids_easylist_class`.
- **Explainer page.** New route `/highlights/` (`routes/highlights.py`, `site/templates/highlights.html`, registered in `app.py` and frozen in `freeze.py`). It covers what a highlight is, Paid vs Editor's pick, the $10 price, the 21 days, where it shows, in-person only, one occurrence per purchase, live without review, how reschedules and cancellations are handled, and that refunds are manual. It also lists current highlights.
- **Newsletter.** `routes/newsletter.py` `_newsletter_context` adds `highlights=select_highlights(filtered_events)`. That list already respects the subscriber's category and region preferences and excludes virtual events. `newsletter.html` and `.txt` render the box after the sponsor block. Label links use `base_url` + `/highlights/`.

### Reconcile and lost-event emails

- New `cdk_next/lambda_src/site_generator/reconcile_highlights.py` runs in CodeBuild after `calgen pipeline`. It reads `highlight_matches.json` and updates `RESOLVED` only when something changes.
- An unmatched highlight whose snapshot date hasn't passed gets `missing_since`. Once it has been missing for 24 hours, the script sends a one-time SES email to Ross (`ADMIN_EMAIL`) and to the buyer (for paid highlights) and sets `lost_notified_at`. The 24-hour grace period covers one-off feed failures.
- The CodeBuild role needs `dynamodb:UpdateItem` on the table and `ses:SendEmail`; copy the identity ARN pattern from `api_stack.py:154-160`. The step must never fail the build.

## Critical files

- **New:**
  - `cdk_next/lambda_src/api/paypal.py`, `highlights.py`, `routes/highlights.py`
  - `cdk_next/lambda_src/site_generator/reconcile_highlights.py`
  - `packages/calgen/src/calgen/highlights.py`, `routes/highlights.py`
  - `site/templates/highlights.html`, `partials/highlight_box.html`
  - `frontends/edit/highlight.html`, `highlights.html` + their JS
- **Modified:**
  - `api/sponsorships.py` (extract the PayPal code), `api/handler.py` (routing)
  - `stacks/api_stack.py`, `stacks/site_generator_stack.py`, `config.py`
  - `mcp/server.py`
  - `export_dynamo_to_calgen.py`, trigger `handler.py`
  - calgen `pipeline.py`, `routes/common.py`, `listings.py`, `categories.py`, `newsletter.py`, `app.py`, `freeze.py`
  - the five index templates, `event_page.html`, `events_by_day.html`, `newsletter.html`/`.txt`, `main.css`, `site/config.yaml`
  - `frontends/edit/events.html`, `index.html`
- `build_lambdas.sh`: copy `highlights.py` and `paypal.py` into `build/mcp/` like `sponsorships.py` (`:33`).

## Implementation order

Track the work as beads: one epic plus one issue per step.

1. Extract `paypal.py` from sponsorships. Sponsorship tests stay green.
2. `highlights.py` rules, the API routes, and the MCP tools, with unit tests on the `FakeTable` from `test_sponsorships.py`.
3. Export, calgen matching, `select_highlights`, the box and label rendering, the explainer page, and the newsletter, with calgen tests.
4. Checkout page, admin page, events-list toggle, and the event page link.
5. Reconcile step, CodeBuild IAM, and the trigger filter.

## Verification

- **Unit tests:**
  - `cd cdk_next/lambda_src/api && python -m pytest test_sponsorships.py test_sponsorship_routes.py test_highlights.py test_highlight_routes.py`. Cover holds and takeovers, a recurring target key, every IPN status, the price-parity check, and rate limits.
  - `cd cdk_next/lambda_src/mcp && python -m pytest`. Check the tools are wired and absent from `/mcp-agent`.
  - `cd packages/calgen && python -m pytest`. Cover:
    - matching: by guid, a reschedule via url, a recurring occurrence, and a duplicate merged into its parent
    - the window: outside 21 days, a past event, a virtual event
    - box scoping per page type, and a newsletter filtered by preferences
    - the inline label, and that EasyList class names are absent
- **Local build:** export, then `calgen pipeline`, then `calgen build` with a hand-written `_data/highlights.json`. Check visually with Playwright: the homepage, a category page, a month page, the week page, an event page, `/highlights/`, and the rendered newsletter.
- **After deploy:**
  1. Add an editor's pick via MCP `highlight_event`. After the triggered build, confirm the box and label on the live pages, then revoke it and confirm it's gone.
  2. Ross makes a real $10 purchase (`PAYPAL_SANDBOX` is global, so test live and refund afterward). Confirm IPN → `active`, the receipt email, and that it appears after the build.
  3. Force a "lost" case on a test single event (delete it) and confirm the one-time email after 24 hours, or run the reconcile script with a shortened grace.
- Run `cdk diff` for `NextApiStack` and `NextSiteGeneratorStack` before deploying, and deploy with `--exclusively`.
