"""DynamoDB-Streams-fed CodeBuild trigger for the static site generator.

Batched by the event source mapping (90s window), then started — and *retried*
if CodeBuild refuses because a build is already running.

It used to skip on a running build, on the grounds that "the scheduled build
will catch up". That safety net is only daily, so a change landing during a
build could sit unpublished for up to 24 hours — which is what happened when an
event was hidden by hand and CodeBuild had to be force-invoked
(next_dctech_events-lux). Overlay writes are especially exposed, because they
arrive as EVENT# stream records in the same burst as the aggregator's own churn.

Serialising still matters: every build ends in `s3 sync --delete` over the whole
site bucket, so two overlapping builds can have the older one deleting what the
newer just wrote. The project therefore carries concurrent_build_limit=1.

Note what that limit actually does, because it is not what it sounds like:
StartBuild *fails* with AccountLimitExceededException. It does not queue. So
this function raises on that error, which hands the batch back to the event
source mapping to retry (retry_attempts=1) — a build takes a couple of minutes
and the batching window is 90s, so the retry usually lands. That is strictly
better than the old skip, which dropped the work with nothing to re-drive it,
but it is not a guarantee: if both attempts collide, the daily build is still
the backstop.
"""
import os

import boto3

PROJECT_NAME = os.environ['CODEBUILD_PROJECT_NAME']

# Only content that feeds the static site should trigger a rebuild.
# POST# (free-form /updates posts) and UPDATE# (weekly roundups) both render
# into /updates, so publishing either has to rebuild the site — without them
# a new post sits invisible until the daily safety-net build.
RELEVANT_PREFIXES = ('EVENT#', 'GROUP#', 'CATEGORY#', 'RECURRING#', 'ICAL#',
                     'POST#', 'UPDATE#')

codebuild = boto3.client('codebuild')


def lambda_handler(event, context):
    records = event.get('Records', [])
    relevant = 0
    for record in records:
        keys = record.get('dynamodb', {}).get('Keys', {})
        pk = keys.get('PK', {}).get('S', '')
        if pk.startswith(RELEVANT_PREFIXES):
            relevant += 1

    if not relevant:
        print(f'No site-relevant changes in {len(records)} records; skipping')
        return {'started': False, 'reason': 'no relevant changes'}

    try:
        build = codebuild.start_build(projectName=PROJECT_NAME)
    except codebuild.exceptions.AccountLimitExceededException:
        # concurrent_build_limit=1 refuses rather than queueing. Raise so the
        # event source mapping re-drives this batch instead of dropping it.
        print('A build is already running; failing so the batch is retried')
        raise

    build_id = build['build']['id']
    print(f'Started build {build_id} ({relevant} relevant changes)')
    return {'started': True, 'build_id': build_id}
