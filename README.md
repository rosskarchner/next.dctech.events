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
