"""DynamoDB-Streams-fed CodeBuild trigger for the static site generator.

Batched by the event source mapping (60s window), then started unconditionally.

It used to skip when a build was already running, on the grounds that "the
scheduled build will catch up". That safety net is only daily, so a change
landing during a build could sit unpublished for up to 24 hours — which is what
happened when an event was hidden by hand and CodeBuild had to be force-invoked
(next_dctech_events-lux). Overlay writes are especially exposed, because they
arrive as EVENT# stream records in the same burst as the aggregator's own churn.

The skip existed for a real reason: every build ends in `s3 sync --delete` over
the whole site bucket, and two overlapping builds can have the older one
deleting what the newer just wrote. That is now prevented a level down — the
project carries concurrent_build_limit=1, so CodeBuild queues a second build
instead of running it. Serialising there rather than here means a queued build
still happens, where a skipped one never did.
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

    build = codebuild.start_build(projectName=PROJECT_NAME)
    build_id = build['build']['id']
    print(f'Started build {build_id} ({relevant} relevant changes)')
    return {'started': True, 'build_id': build_id}
