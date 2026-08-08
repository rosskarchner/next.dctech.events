"""DynamoDB-Streams-fed CodeBuild trigger for the static site generator.

Batched by the event source mapping (60s window); guards against overlapping
builds by skipping when a build is already running — the 4-hour scheduled
build and the on-demand /admin/rebuild route cover anything missed.
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

    in_progress = codebuild.list_builds_for_project(
        projectName=PROJECT_NAME, sortOrder='DESCENDING')['ids'][:5]
    if in_progress:
        statuses = codebuild.batch_get_builds(ids=in_progress)['builds']
        if any(b['buildStatus'] == 'IN_PROGRESS' for b in statuses):
            print('Build already in progress; skipping (scheduled build will catch up)')
            return {'started': False, 'reason': 'build in progress'}

    build = codebuild.start_build(projectName=PROJECT_NAME)
    build_id = build['build']['id']
    print(f'Started build {build_id} ({relevant} relevant changes)')
    return {'started': True, 'build_id': build_id}
