# Parallel Python CDK Stack for next.dctech.events

> **Status note (2026-08-06).** This is the original planning document, kept as
> a record of the design and the reasoning behind it. It describes the world as
> it was *before* the build, so parts of it are deliberately out of date — most
> of all, the stack it plans is now live and serves production `dctech.events`.
>
> One point matters enough to correct inline, because the plan states it as a
> standing constraint: **calgen is no longer an external, pip-installed package
> with an upstream.** It lives in this repo at `packages/calgen`, which is its
> only maintained copy; the standalone `github.com/rosskarchner/calgen` repo is
> archived and read-only. Where this plan says calgen "must not be forked,"
> read that as its actual intent — there is one shared calgen implementation
> and it should stay that way — not as a rule against editing `packages/calgen`,
> which is now the correct place to fix calgen bugs. See `README.md` for the
> current layout.

## Context

dctech.events currently runs on a TypeScript CDK app (`infrastructure/`) with a hybrid,
partially-migrated data model: the static site is built by `calgen` (an external, pip-installed
Python SSG) from git-committed YAML (`_groups/`, `_single_events/`, `_recurring_events/`,
`_categories/`, `_overlay/`) and published to **GitHub Pages** (not S3/CloudFront, despite what
the docs say); meanwhile a newer DynamoDB table (`dctech-events`) holds user-submitted
drafts/events/groups/categories from the Cognito-authed `/edit/` admin UI, with approvals
currently written back into the repo as git-committed YAML (`backend/github_commit.py`) so calgen
picks them up on the next build. iCal aggregation, QA review, and "find new events" discovery all
either don't exist as live services or exist only as a weekly GitHub Actions LLM workflow
(`calendar-qc`) operating on local files.

The user sketched a target architecture where **DynamoDB is the real hub**: a Submission UI talks
to it through an API/MCP layer, four independent agents (iCal Aggregator, QA Agent, Discovery
Agent — an event-finding web/search-engine agent, not a forum integration — and Newsletter Sender)
read/write it directly, and a Static Site Generator reads from it to produce the public
S3+CloudFront site. The goal of this work is to stand up that architecture as a **new, fully
parallel Python CDK app deployed to `next.dctech.events`**, proving the design out without touching
or risking production `dctech.events` in any way. calgen's own rendering logic must not be forked —
only the data source feeding it changes.

Three scope decisions were made with the user before designing this:
- **Isolation**: share the existing production Cognito User Pool (new app client only, new
  callback URLs) so logins carry over, but give the new stack its own **isolated DynamoDB table**
  seeded from a one-time export/migration of current data.
- **Newsletter Sender**: port the actual mechanics of the live `~/projects/dctech-newsletter`
  Chalice app (double opt-in, KMS-signed confirmation links, SES contact list/topic, weekly send,
  bounce handling) — not the unfinished/never-deployed calgen-hub rewrite — into fresh, isolated
  SES/KMS/Secrets resources, with one deliberate modernization: the weekly sender reads events
  directly from the new DynamoDB table instead of HTTP-scraping the live site's prerendered
  `newsletter.html`.
- **QA Agent / Discovery Agent**: infrastructure scaffold only — Lambdas, MCP-backed tool surface,
  EventBridge schedules, and IAM roles (including provisioned-but-unused Bedrock permissions), with
  stub handler bodies. The actual LLM orchestration/prompts are an explicit follow-up, not part of
  this build.

## Key ground truth (verified by reading the actual code, not docs)

- Production hosting is **GitHub Pages**, not S3/CloudFront — DNS points at GitHub Pages IPs.
  `infrastructure/lib/frontend-stack.ts` (S3+CloudFront+OAC+directory-index CloudFront Function) and
  `redirect-stack.ts` exist but are **not instantiated** in `infrastructure/bin/infrastructure.ts` —
  good starting templates for the new stack's hosting construct, otherwise dead code.
- `infrastructure/lib/chalice-api-stack.ts.old` already targeted `next.dctech.events` for an earlier
  abandoned Chalice/SAM approach — confirms the subdomain is free and intended for exactly this.
- The current DynamoDB table (`dctech-events`, provisioned in `infrastructure/lib/dynamodb-stack.ts`)
  is single-table: PK/SK strings, GSI1–GSI4, PAY_PER_REQUEST, streams `NEW_AND_OLD_IMAGES`, PITR,
  `RemovalPolicy.RETAIN`. `backend/db.py` is the authoritative key-schema reference (Draft/Group/
  Event/Category entities, exact PK/SK/GSI conventions) — reuse these conventions directly.
- calgen's iCal fetch logic (`calendars.py:fetch_ical_and_extract_events`) and its MCP server
  (`mcp_server.py`, FastMCP-based) are **entirely filesystem/git-based** — no DynamoDB code exists
  in calgen at all. Both are patterns to mirror (tool names/shapes, fetch/parse/dedup logic), not
  code to import into a DynamoDB-backed context without an adapter.
- The live newsletter app (`~/projects/dctech-newsletter/app.py`) does double-opt-in signup, KMS
  HMAC-signed (`HMAC_SHA_512`) confirmation links, CSRF via a Secrets Manager secret, SES contact
  list `newsletters`/topic `dctech` (created by `setup_ses.py`), and a weekly
  `cron(0 11 ? * MON *)` Lambda that HTTP-GETs `https://dctech.events/newsletter.html`/`.txt` and
  sends via an SES template — those two files are calgen-rendered (`packages/calgen/src/calgen/app.py`
  `/newsletter.html`/`/newsletter.txt` routes, `prepare_newsletter_titles`, and
  `pipeline.remove_duplicates` for the "also published by" duplicate-merge logic).
- `frontends/edit/js/config.js` **and** `frontends/edit/js/auth.js` both hardcode the production
  Cognito app client ID (`58j1h73i72v1kaim503bk2amgb`) and hostname-based branching that doesn't
  cleanly cover `next.dctech.events` — `auth.js`'s `host.includes('dctech.events')` substring check
  happens to route `next.dctech.events` into the `isDctechSite` branch already, but with the
  **wrong (production) client ID** hardcoded. Both files need a templated client ID / API base for
  the new stack.

## Architecture

New Python CDK app in a new top-level directory `cdk_next/` (sibling to `infrastructure/`,
`backend/`), fully decoupled from the TS app: all new resources use `Next*`/`dctech-events-next*`
naming, `CfnOutput`s carry no `exportName` (or a non-colliding prefix), and every reference to an
existing resource is by literal ID/ARN (`UserPool.from_user_pool_id`,
`HostedZone.from_hosted_zone_attributes`) — never CloudFormation cross-stack import/export. Region
is `us-east-1` throughout (required for CloudFront-attached ACM certs; matches the existing pool's
region and the TS app's `sharedConfig.region`).

```
cdk_next/
  app.py, cdk.json, requirements.txt, config.py
  stacks/
    dynamodb_stack.py        # NextDynamoDBStack — new isolated table
    cognito_client_stack.py  # NextCognitoClientStack — imports existing pool, adds new app client only
    hosting_stack.py         # NextHostingStack — ACM cert, S3, CloudFront, Route53 for next.dctech.events
    api_stack.py             # NextApiStack — Lambda (backend fork) + API Gateway + MCP endpoint
    ical_aggregator_stack.py # NextIcalAggregatorStack — Lambda + EventBridge schedule
    site_generator_stack.py  # NextSiteGeneratorStack — CodeBuild project + DynamoDB-Streams trigger
    newsletter_stack.py      # NextNewsletterStack — KMS key, secrets, SES resources, 3 Lambdas
    qa_agent_stack.py        # NextQaAgentStack — scaffold Lambda + schedule + IAM (stub body)
    discovery_agent_stack.py # NextDiscoveryAgentStack — scaffold Lambda + schedule + IAM (stub body)
  lambda_src/
    api/{handler.py, routes/{public,submit,admin}.py, db.py, auth.py, templates/}
    mcp/{server.py, handler.py}
    ical_aggregator/handler.py
    site_generator/{buildspec.yml, export_dynamo_to_calgen.py, trigger/handler.py}
    newsletter/{app.py, render.py, bounce_handler.py}
    qa_agent/handler.py
    discovery_agent/handler.py
  scripts/migrate_to_next_table.py
```

Dependency order: `NextDynamoDBStack` and `NextHostingStack` first (independent); then
`NextCognitoClientStack` (imports existing pool); then `NextApiStack` (needs table + client +
cert); then `NextIcalAggregatorStack`, `NextSiteGeneratorStack` (needs table streams + hosting
bucket/distribution), `NextNewsletterStack` (needs table); `NextQaAgentStack`/
`NextDiscoveryAgentStack` last.

### Data model (`NextDynamoDBStack`)

Table `dctech-events-next`, PK/SK strings, PAY_PER_REQUEST, streams `NEW_AND_OLD_IMAGES`, PITR,
TTL attribute `ttl`, **`RemovalPolicy.DESTROY`** initially (parallel/dev environment — flip to
`RETAIN` once real subscribers/users depend on it). Carries over `backend/db.py`'s exact entity
conventions:

| Entity | PK | SK | GSI1 | GSI2 | GSI3 | GSI4 |
|---|---|---|---|---|---|---|
| Draft | `DRAFT#{id}` | `META` | `STATUS#{status}` / `{created_at}` | — | `USER#{user_id}` / `{created_at}` | — |
| Group | `GROUP#{slug}` | `META` | `ACTIVE#{0\|1}` / `NAME#{name}` | `CATEGORY#{slug}` / `GROUP#{slug}` | — | — |
| Event | `EVENT#{guid}` | `META` | `DATE#{date}` / `TIME#{time}` | — | `CREATED#{YYYY-MM}` / `{createdAt}` | `EVT#ACTIVE` / `{date}#{time}` |
| Category | `CATEGORY#{slug}` | `META` | — | — | — | — |

Additions (additive only):
- Event `source` now includes `ical`/`recurring` alongside `manual`/`submitted` (matches calgen's
  `pipeline.py` source-stamping). iCal Aggregator writes `EVENT#{guid}` with `source='ical'`,
  `group_id`, `group`, `group_website`, `location_type`.
- `ICAL#{group_id}` / `META` — replaces calgen's filesystem `_cache/ical/{group_id}.json` + `.meta`;
  stores fetched events + ETag/Last-Modified/last_fetch, with a `ttl` for self-expiry.
- New **GSI5** (`GSI5PK`/`GSI5SK`) for QA/Discovery queuing: `review_status` field
  (`pending_qa | approved | flagged | pending_discovery_review | discovered`) on Event items;
  Discovery candidates get their own `CANDIDATE#{hash}` / `META` entity (never pollutes real
  `EVENT#` records until promoted).
- Overlay fields reuse `backend/db.py`'s existing `update_event(..., overrides=...)` /
  `overrides` field mechanism rather than inventing a parallel `OVERLAY#` entity.

`cdk_next/lambda_src/api/db.py` is `backend/db.py` copied nearly verbatim plus these additions.

### Migration/seeding (`cdk_next/scripts/migrate_to_next_table.py`, one-time, `--dry-run` default)

Consolidates three sources: git YAML (`_groups/`, `_categories/`, `_single_events/`,
`_recurring_events/`, `_overlay/` — authoritative for groups/categories/manual events today, loaded
via calgen's own `pipeline.get_groups()`/`get_categories()`/`load_single_events()`/
`load_recurring_events()`/`load_overlays()`), the current `dctech-events` table (draft/submission
state, scanned and parsed with `backend/db.py`'s existing `_*_item_to_dict` helpers), and the legacy
`DcTechEvents` table (cross-referenced/audited only, not imported — presumed superseded). YAML wins
reconciliation for groups/categories/events (guid computed via
`calgen.event_utils.calculate_event_hash`, same formula, so guids stay stable); drafts copy
verbatim since there's no YAML equivalent. A live iCal fetch during migration seeds
`ICAL#{group_id}/META` cache items so the new site has real content immediately. Prints a
reconciliation report (counts, diffs) for manual review — not meant to be rerun as an ongoing sync.

### Component design

- **Submission UI**: reuse `frontends/edit/` wholesale, served from the new S3 bucket at `/edit/`.
  Only change: `config.js`'s `EXECUTE_API_BASE` and `auth.js`'s hardcoded `userPoolClientId`
  (`'58j1h73i72v1kaim503bk2amgb'`, both `isStemSite`/else branches) must become deploy-time
  templated values (simple string substitution in the deploy step) pointing at the new stack's API
  Gateway URL and new Cognito app client ID — no other HTML/CSS/JS changes needed.
- **API / MCP**: one Lambda + API Gateway REST API forked from `backend/` (`handler.py`,
  `routes/{public,submit,admin}.py`, `auth.py`, `templates/`), with `github_commit.py`'s call site
  **removed** — approval just promotes `DRAFT#`→`EVENT#` in DynamoDB directly (no git-commit hop;
  the Static Site Generator now reads DynamoDB directly, eliminating the need for it). A second
  Lambda exposes calgen's MCP tool surface (`list_groups`, `add_group`, `set_group_active`,
  single/recurring-event CRUD, `list_categories`/`add_category`, overlay get/set via `db.py`'s
  existing `overrides` mechanism, `get_events`) re-implemented against DynamoDB instead of YAML
  files, using `FastMCP(...).streamable_http_app()` wrapped with Mangum, mounted at `/mcp` behind
  the same API Gateway with an AWS_IAM authorizer (trusted agents/Lambdas only, not end users).
  Drop `bootstrap_site`/`write_regions`/`refresh_and_run_pipeline` (not applicable); optionally add
  a `trigger_rebuild()` tool that calls `codebuild.start_build()`.
- **iCal Aggregator**: Lambda importing `calgen.calendars.fetch_ical_and_extract_events` /
  `fetch_json_ld_data` directly (no fork) via a `/tmp`-scratch adapter — seed the expected cache/meta
  files from DynamoDB at invocation start, let calgen's function do its normal file I/O, then persist
  results back to `ICAL#{group_id}/META` and `EVENT#{guid}` (`source='ical'`) items. Reads the group
  list from DynamoDB (`GROUP#` items), not `_groups/*.yaml`. EventBridge schedule every 4 hours
  (matches calgen's own internal throttle window). Watch Lambda's 15-minute cap with 150+ groups —
  start single-Lambda-does-all-groups, revisit with an SQS fan-out if timeouts occur.
- **QA Agent / Discovery Agent** (scaffold only): Lambda + EventBridge schedule + IAM role each
  (QA: daily, reads GSI5 `REVIEW#pending_qa`; Discovery: weekly, reads/writes `CANDIDATE#` items),
  with provisioned-but-unused `bedrock:InvokeModel*` (and, for Discovery, future
  web-search-integration) permissions. Handler bodies log what they *would* do and exit — no prompts,
  no LLM calls, no data mutation. Note `~/projects/calendar-agents`' Strands-based URL/event
  extraction agent as the likely building block for Discovery's eventual real logic.
- **Newsletter Sender**: fresh, isolated KMS key, Secrets Manager CSRF secret, SES contact list
  (`dctech-events-next-newsletter`, topic `dctech-next`), SES template, configuration set, and SNS
  bounce/complaint topic — provisioned via a `setup_ses_next.py` script mirroring
  `dctech-newsletter/setup_ses.py`'s `SESSetup` class (verify at implementation time whether
  aws-cdk-lib has grown native SESv2 contact-list L2 constructs; fall back to `AwsCustomResource` or
  a post-deploy script otherwise). Port `dctech-newsletter/app.py`'s exact signup/confirm/CSRF/
  KMS-signing/bounce-handling logic into this stack's Lambda-proxy router style (not Chalice).
  Deliberate modernization: `render.py` replaces the HTTP-scrape of `newsletter.html`/`.txt` with a
  direct DynamoDB query (`db.get_all_events()` via GSI4) rendered through calgen's own
  `templates/newsletter.html`/`.txt` and `pipeline.remove_duplicates` (imported, not reimplemented,
  for the "also published by" dedup logic) — flagged for explicit visual/diff verification since
  it's a new context-building implementation even though it reuses calgen's templates and dedup
  function. Same weekly `cron(0 11 ? * MON *)` schedule as production.
- **Static Site Generator**: **AWS CodeBuild**, not Lambda — calgen/Flask-Frozen needs a real
  writable directory tree and can produce thousands of files; CodeBuild's disk/time headroom beats
  fighting Lambda's `/tmp` and 15-minute limits, and natively supports git-clone + pip install + s3
  sync. Buildspec: (1) `pip install calgen ...`; (2) clone this repo for `templates/`/`static/`/
  `config.yaml` (not group/event data), then run a new `export_dynamo_to_calgen.py` that queries
  DynamoDB and writes `_groups/`, `_categories/`, `_single_events/`, `_recurring_events/`,
  `_overlay/` (overwriting what git provided for those specific dirs) plus reconstructs
  `_cache/ical/{group_id}.json` from `ICAL#{group_id}/META` items; (3) run `calgen pipeline` then
  `calgen build` — **deliberately skip `calgen refresh`**, since the iCal Aggregator Lambda now owns
  fetching and the export step already materialized the cache files it needs; (4) `aws s3 sync
  build/ s3://<bucket>/ --delete` + CloudFront invalidation. Trigger: **both** a DynamoDB-Streams-fed
  Lambda (batched/debounced ~60–120s, guards against overlapping builds) for near-real-time rebuilds
  after admin approvals, and a fixed 4-hour schedule as a safety net aligned with the Aggregator's
  cadence; also expose an on-demand `POST /admin/rebuild` route / MCP tool.
- **S3 + CloudFront**: `NextHostingStack` adapts the unused `frontend-stack.ts` pattern verbatim
  (OAC-scoped bucket policy, directory-index CloudFront Function, `HTTP2_AND_3`, IPv6,
  `TLS_V1_2_2021` minimum) — one bucket/distribution serving both the calgen-built site and
  `/edit/*`, matching production's single-origin model. No WAF, no `www` subdomain (not needed for
  a parallel/staging environment).

### DNS/certs

**New** ACM cert for `next.dctech.events` (not a reference to the TS-owned `*.dctech.events`
wildcard — decouples the two apps' lifecycles entirely; ACM certs are free), us-east-1, DNS-validated
against the **existing** hosted zone `Z078066931R85FQDWCM3P` via a read-only
`HostedZone.from_hosted_zone_attributes` lookup. The validation CNAME and the final A/AAAA alias
records for `next.dctech.events` are new, additively-scoped records — nothing existing in the zone
(prod's bare-domain/`www` A/AAAA records, `login.dctech.events`) is touched.

### CI/CD

New workflow `.github/workflows/deploy-next.yml`, `workflow_dispatch`-only initially (not on every
push, to avoid redeploying this in-development stack on unrelated commits). Jobs: `cdk deploy --all`
for infra; a one-time/idempotent SES setup script run; trigger-and-wait on the CodeBuild project for
site builds (GitHub Actions doesn't run calgen itself — CodeBuild owns that, per the design above);
a trivial `aws s3 sync` for the templated `frontends/edit/`. Reuse the **existing** GitHub OIDC
provider (import read-only by ARN — OIDC providers are account-level singletons), but provision a
**new**, narrowly-scoped `NextGithubActionsDeployRole` rather than broadening the existing
`GithubActionsDeployRole` (whose policy is scoped to the TS stacks' specific resource ARNs).

## Phasing (deploy and verify one stack at a time, not `cdk deploy --all`)

1. **Hosting only** — empty S3 + CloudFront + DNS + cert. Verify `next.dctech.events` resolves and
   serves a placeholder over valid TLS.
2. **DynamoDB + migration** — deploy table, run migration script `--dry-run` then for real. Verify
   item counts/GSI keys via `aws dynamodb query` against the migrated groups/categories/events/drafts.
3. **API + MCP + Cognito client** — verify `/health`, `/api/events`, admin login via the shared
   Cognito pool's hosted UI with the new app client, and basic MCP tool calls (`list_groups`,
   `get_events`).
4. **Static Site Generator** — manual CodeBuild run, verify the real calendar renders at
   `next.dctech.events` (homepage, month page, categories, `events.json`/`events.ics`).
5. **Submission UI** — deploy templated `frontends/edit/`, submit→approve→confirm the Streams
   trigger rebuilds the site with the new event.
6. **iCal Aggregator** — manual invoke for a couple of groups, verify `EVENT#` items with
   `source='ical'` appear and flow through to the next site build.
7. **Newsletter Sender** — sign up/confirm with a personal test address, verify the isolated SES
   contact list, manually invoke the weekly sender once, visually compare against production's
   newsletter email.
8. **QA/Discovery Agent scaffolds** — deploy last; verify each Lambda logs its stub "would do X"
   message on manual invoke and mutates nothing.

## Verification

- **calgen output parity**: diff `_data/all_events.json` (by guid) and the full `build/` directory
  between a git-YAML-sourced calgen run and the new stack's DynamoDB-sourced run, for the same
  underlying data — expect matching guid sets and no unexplained field/page differences (confirm
  `config.yaml`'s `base_url` is overridden to `https://next.dctech.events` for this stack's build,
  or every internal link will point at production).
- **Newsletter parity**: render via the new DynamoDB-sourced `render.py` and via calgen's own live
  `/newsletter.html`/`/newsletter.txt` for the same data; diff HTML and do a manual visual check
  (email client rendering isn't fully captured by a text diff).
- **Auth/isolation**: confirm the same Cognito login works on `next.dctech.events` (proving
  shared-Cognito) while a submission made there never appears in the production `dctech-events`
  table or live site (proving isolated data) — spot-check via `aws dynamodb get-item` on both tables.
- **No regression on production**: click through `https://dctech.events/` and `/edit/` after each
  phase, especially right after adding the new Cognito app client, to confirm the shared user pool
  is unaffected.

## Critical files

- `backend/db.py` — DynamoDB key-schema/CRUD conventions to mirror in `cdk_next/lambda_src/api/db.py`
- `packages/calgen/src/calgen/pipeline.py`, `calendars.py`, `mcp_server.py` — reuse patterns/functions, keep one shared implementation (path was `calgen/…` when this plan was written, before calgen moved into this repo)
- `packages/calgen/src/calgen/app.py` (`/newsletter.html`, `/newsletter.txt`, `prepare_newsletter_titles`) — newsletter render context to port into `render.py`
- `infrastructure/lib/dynamodb-stack.ts`, `cognito-stack.ts`, `lambda-api-stack.ts`, `frontend-stack.ts`, `config.ts` — resource shapes/conventions to mirror in Python
- `dctech-newsletter/app.py`, `setup_ses.py` — newsletter logic and SES resource setup to port
- `frontends/edit/js/config.js`, `js/auth.js` — the two files needing deploy-time templated Cognito client ID / API base
- `backend/github_commit.py` — the git-commit hop to remove in the new stack's approval flow
