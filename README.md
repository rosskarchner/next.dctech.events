# next.dctech.events

Parallel Python CDK stack for [dctech.events](https://dctech.events), live at
**https://next.dctech.events**. DynamoDB is the hub: the submission UI, four
agents (iCal aggregator, QA, discovery, newsletter sender), and the static
site generator all read/write the `dctech-events-next` table. See
`next-architecture-plan.md` for the full design and the scope decisions
behind it.

## Layout

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
./build_lambdas.sh          # requires a calgen checkout at ~/projects/calgen
npx aws-cdk@latest deploy --all --require-approval never
.venv/bin/python scripts/setup_ses_next.py --feedback-topic-arn <NextNewsletterFeedbackTopicArn>
../scripts/deploy_edit_ui.sh
aws codebuild start-build --project-name dctech-events-next-site-generator
```

## Shared vs. isolated

Shared with production (account-level singletons, referenced read-only or
additively): Cognito user pool `us-east-1_8Ay4dTt8j` (+ hosted UI
`login.dctech.events`; this stack has its own app client), Route53 zone
`dctech.events`, SES domain identity, GitHub OIDC provider, and — because SES
allows exactly one contact list per account — the `newsletters` contact list
(this stack isolates via its own topic `dctech-next`, template, and
configuration set).

Isolated: DynamoDB table `dctech-events-next`, S3/CloudFront, ACM cert, API
Gateways, KMS HMAC key, CSRF secret, SNS feedback topic, CodeBuild project,
all Lambdas/schedules/roles.
