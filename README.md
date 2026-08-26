# dctech.events

Python CDK stack serving **https://dctech.events** (and `www`). Built as a
parallel stack at `next.dctech.events`, it took over the main domain at
cutover; the repo keeps its original name. DynamoDB is the hub: the
submission UI, four
agents (iCal aggregator, QA, discovery, newsletter sender), and the static
site generator all read/write the `dctech-events-next` table. See
`next-architecture-plan.md` for the full design and the scope decisions
behind it.

## Layout

This is a monorepo: calgen (the static site generator) lives here rather than
being installed from a separate repo, so a single checkout builds everything.

- `packages/calgen/` — the calgen SSG. **This is calgen's canonical home and
  the only maintained copy — there is no upstream.** The standalone
  `github.com/rosskarchner/calgen` repo is archived and read-only; nothing is
  synced from it, and it should not be treated as a source of truth. Fix
  calgen bugs here, directly. Lambda bundles and the CodeBuild wheel are built
  from this path. Its rendering logic is shared as-is by the site generator,
  the iCal aggregator, and the newsletter renderer — only the data source
  (DynamoDB instead of git YAML) differs, so change it in this one place
  rather than copying diverging variants into individual Lambdas.
- `cdk_next/` — Python CDK app (nine `Next*` stacks, fully decoupled from the
  production TypeScript CDK app; existing resources referenced by literal
  ID/ARN only)
- `cdk_next/lambda_src/` — Lambda sources: API (fork of `dctech.events/backend`
  minus the git-commit hop), MCP server (calgen's tool surface against
  DynamoDB), iCal aggregator (imports `calgen.calendars` unmodified via a
  /tmp adapter), newsletter (port of the live `dctech-newsletter` Chalice
  app), site-generator export/trigger, QA + discovery stubs
- `cdk_next/scripts/` — `migrate_to_next_table.py` (one-time seed, dry-run by
  default) and `setup_ses_next.py` (idempotent SES provisioning)
- `site/` — calgen site source (config/templates/static/regions.py); the
  `_groups/_categories/...` data dirs are generated from DynamoDB at build
  time by `export_dynamo_to_calgen.py`, never committed
- `frontends/edit/` — submission UI with `{{API_BASE}}`/`{{COGNITO_CLIENT_ID}}`
  placeholders, deployed by `scripts/deploy_edit_ui.sh`
- `.github/workflows/deploy-next.yml` — `workflow_dispatch`-only deploy via
  the `NextGithubActionsDeployRole` OIDC role

## Deploying

```sh
cd cdk_next
uv venv .venv && uv pip install -p .venv/bin/python aws-cdk-lib constructs boto3 pyyaml
./build_lambdas.sh          # builds calgen from packages/calgen
npx aws-cdk@latest deploy --all --require-approval never
.venv/bin/python scripts/setup_ses_next.py --feedback-topic-arn <NextNewsletterFeedbackTopicArn>
../scripts/deploy_edit_ui.sh
aws codebuild start-build --project-name dctech-events-next-site-generator
```

## Shared with the older stack

The previous TypeScript CDK app (in the `dctech.events` repo) still owns some
account-level resources this stack references read-only or additively:
Cognito user pool `us-east-1_8Ay4dTt8j` (+ hosted UI `login.dctech.events`;
this stack has its own app client), Route53 zone `dctech.events`, the SES
domain identity and its `newsletters` contact list — SES allows exactly one
per account — plus the GitHub OIDC provider. Newsletter subscribers live on
that list's long-standing `dctech` topic, so they carried over at cutover.

Owned by this stack: DynamoDB table `dctech-events-next`, S3/CloudFront, ACM
cert, API Gateways, KMS HMAC key, CSRF secret, SES template/configuration
set, SNS feedback topic, CodeBuild project, all Lambdas/schedules/roles.

The old site is still built and published to GitHub Pages by the other repo,
but nothing points at it: its weekly newsletter schedule
(`dctech-newsletter-dev-scheduled_newsletter-event`) was disabled at cutover
to avoid double-sending to the shared subscriber list.

## MCP server (managing groups and events)

Groups, events, categories, and overlays are managed through an MCP server
(`cdk_next/lambda_src/mcp/server.py`) deployed behind API Gateway. Prefer its
tools over hand-written `aws dynamodb` calls — they validate categories, verify
iCal feeds, and keep the GSI keys consistent.

The endpoint uses an AWS_IAM authorizer, so every request must be SigV4-signed.
MCP clients do not sign requests, so `scripts/mcp_sigv4_bridge.py` bridges
stdio to the signed endpoint using your ordinary AWS credentials. `.mcp.json`
wires it up, so a client started in this repo picks it up automatically.

Verify it by hand with:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 scripts/mcp_sigv4_bridge.py
```

Tools: `list_groups`, `add_group`, `set_group_active`, `verify_ical_feed`,
`list_single_events`, `add_single_event`, `update_single_event`,
`delete_single_event`, `list_recurring_events`, `add_recurring_event`,
`update_recurring_event`, `delete_recurring_event`, `list_categories`,
`add_category`, `get_overlay`, `set_overlay`, `get_events`, `trigger_rebuild`.

Submissions: `submit_event` queues an event for review the way the public
/submit form does; `list_pending_submissions`, `get_submission`,
`approve_submission` (optionally trusting the submitter), `reject_submission`,
and `list_trusted_submitters` / `trust_submitter` / `untrust_submitter` work
the queue. Use `submit_event` for anything found rather than vetted — a
scraped calendar, a third-party listing — and `add_single_event` only when you
mean to publish immediately.

## The /updates posts

`dctech-events-next-updates-publisher`
(`cdk_next/lambda_src/updates_publisher/`) runs twice a week, writing an
`UPDATE#{publish_date}` item each time. The table's stream rebuilds the site,
and the social publisher cross-posts it.

**Monday 10:30 UTC — the week ahead.** A *link post*: title, blurb, and a
pointer at `/week/<iso-week>/` for the week just starting. It has no page of
its own — it appears on /updates/ and in the feed, but the link goes straight
to the week. It stores no listing either, because the week page already merges
live events with its own `ARCHIVE#` capture, so the link keeps working after
the week is over. This is the post that ran on Mondays before 2026-08-13, now
pointing at the week page instead of duplicating it.

**Wednesday 11:00 UTC — what's new.** A *roundup* listing every event *added*
to the calendar in the previous seven days, at
`/updates/<y>/<m>/<d>/`. Anything still in the moderation queue is a `DRAFT#`
item that was never published, so it cannot appear. This one is a frozen
snapshot rather than a query: calgen drops events dated before today, so a
roundup rendered live would empty out as the events it announced happened.

### Week archives

Both runs also refresh the `ARCHIVE#<iso-week>` captures for the current and
coming week. These are the site's only memory of what the calendar showed once
a week is over — `get_events()` drops past events and organizers' feeds do too.

A capture **accumulates** rather than overwrites, split at today:

* **Before today**, the stored capture is kept verbatim. It is the only record
  there is, so events are not lost as they happen.
* **Today onward**, live `events.json` wins outright — it carries everything
  added since the last merge, and it reflects cancellations, which a plain
  union would resurrect.

The two halves are disjoint by date, so nothing needs de-duplicating. This also
makes re-running an old post safe: merging a finished week keeps its capture
intact instead of writing an empty list over it.

Coverage starts where archiving started — `2026-W34` and earlier were never
captured, and `/week/` 404s for them.

Preview or republish either by hand:

```bash
# Wednesday roundup
aws lambda invoke --function-name dctech-events-next-updates-publisher \
  --payload '{"published_on":"2026-08-19","dry_run":true}' /dev/stdout

# Monday link post
aws lambda invoke --function-name dctech-events-next-updates-publisher \
  --payload '{"mode":"week_ahead","published_on":"2026-08-31","dry_run":true}' \
  /dev/stdout
```

Drop `dry_run` to write it, and add `"force": true` to overwrite a post that
already exists. Both kinds key on the publication date, so Monday's and
Wednesday's posts never collide even though they share an ISO week. `force`
guards the post only — the archive merge is always applied, since it cannot
destroy anything.

Three post shapes render on /updates/, not two: posts published before
2026-08-12 are keyed by ISO week and list the events *happening* that week —
the original meaning of the post. calgen tells them apart by `post_kind`, and
only the paged kinds build a page (`packages/calgen/src/calgen/updates.py`,
`get_paged_update_posts`).

## Social cross-posting

Every new /updates post is announced on Mastodon
([@techevents@dmv.community](https://dmv.community/@techevents)) and Bluesky
([@dctechevents.bsky.social](https://bsky.app/profile/dctechevents.bsky.social))
by `dctech-events-next-social-publisher`
(`cdk_next/lambda_src/social_publisher/`). It reads the events table's
DynamoDB stream, filtered to `UPDATE#` and `POST#` keys, so it covers the
Monday link post, the Wednesday roundup, and free-form announcements written in
/edit without any publisher knowing it exists — and a social outage can never
block the write that created the post.

A link post syndicates its *target*, because it has no page of its own: the
Monday post goes out to Mastodon and Bluesky linking straight to
`/week/<iso-week>/`. Everything else links its own `/updates/` permalink.
Dedupe is still keyed on the post's `UPDATE#`/`POST#` key, not on the URL, so
re-pointing a link post cannot cause a repost.

Credentials live in `dctech-events-next/mastodon` and
`dctech-events-next/bluesky` (created by `NextSocialStack`, populated
out-of-band). Posting twice is prevented by a `SOCIAL#{PK}` record holding the
ids of what has already gone out, per network; streams deliver at least once
and failed batches retry, so it is load-bearing rather than belt-and-braces.

Backfill or preview a post by hand:

```bash
aws lambda invoke --function-name dctech-events-next-social-publisher \
  --cli-binary-format raw-in-base64-out \
  --payload '{"pk":"UPDATE#2026-W33","dry_run":true}' /dev/stdout
```

Drop `dry_run` to post; add `"force": true` to post again despite the
`SOCIAL#` record.
