"""Starts one follow-up site build if the site went dirty during the last one.

Fed by an EventBridge rule on "CodeBuild Build State Change" for the site
generator project (terminal states only). handler.py stamps RENDER#site.dirtyAt
whenever something the site shows may have changed, and swallows the refusal
when a build is already running. This closes the loop: when a build finishes,
compare when that build read the table (exportAt, stamped by the build itself)
with dirtyAt. A later dirtyAt means a change landed after the build could have
read it, so build again.

Termination: the follow-up reads the table after dirtyAt, so on its own
completion dirtyAt < its read time and nothing further happens, unless a new
ring arrived.

A failed or stopped build still gets its follow-up if rings arrived after it
read the table (those changes were never going to be in it). It does not retry
itself: changes that rang *before* a failed build read stay unpublished until the
next ring or the daily build, which keeps a persistent build failure from looping.
"""
import os

import boto3

PROJECT_NAME = os.environ['CODEBUILD_PROJECT_NAME']
TABLE_NAME = os.environ['TABLE_NAME']

STATE_KEY = {'PK': 'RENDER#site', 'SK': 'STATE'}

codebuild = boto3.client('codebuild')
dynamodb = boto3.client('dynamodb')


def _state():
    """(dirtyAt, exportAt) in epoch ms, 0 for whichever was never written."""
    item = dynamodb.get_item(
        TableName=TABLE_NAME,
        Key={k: {'S': v} for k, v in STATE_KEY.items()},
        ConsistentRead=True,
    ).get('Item') or {}
    return (int(item['dirtyAt']['N']) if 'dirtyAt' in item else 0,
            int(item['exportAt']['N']) if 'exportAt' in item else 0)


def _started_ms(build_id):
    builds = codebuild.batch_get_builds(ids=[build_id]).get('builds', [])
    if not builds:
        return None
    return int(builds[0]['startTime'].timestamp() * 1000)


def lambda_handler(event, context):
    detail = event.get('detail', {})
    build_id = detail.get('build-id')
    status = detail.get('build-status')
    if not build_id:
        print(f'No build-id in event; ignoring: {event}')
        return {'started': False, 'reason': 'no build id'}

    started = _started_ms(build_id)
    if started is None:
        print(f'Build {build_id} not found; ignoring')
        return {'started': False, 'reason': 'build not found'}

    dirty, export_at = _state()
    # The build reads the table ~30s after it starts (pip install comes first),
    # so a ring in that gap is still covered. The build stamps exportAt itself
    # just before its Scan. If this build never did (the stamp failed, or the
    # item predates it), exportAt is older than the build and cannot be trusted;
    # fall back to the start time, which only errs towards one extra build.
    # Without the fallback a stale exportAt would re-trigger forever.
    read_at = export_at if export_at >= started else started
    if dirty <= read_at:
        print(f'Build {build_id} ({status}) covers every ring '
              f'(dirtyAt {dirty} <= read {read_at}); nothing to do')
        return {'started': False, 'reason': 'clean'}

    try:
        build = codebuild.start_build(projectName=PROJECT_NAME)
    except codebuild.exceptions.AccountLimitExceededException:
        # Another build (a manual rebuild, the daily rule) got the slot first.
        # Its own completion event runs this same check, so the ring is not lost.
        print(f'Build {build_id} ({status}) left the site dirty but another '
              'build is already running; its completion will re-check')
        return {'started': False, 'reason': 'build running; re-check on completion'}

    new_id = build['build']['id']
    print(f'Build {build_id} ({status}) left the site dirty '
          f'(dirtyAt {dirty} > read {read_at}); started follow-up {new_id}')
    return {'started': True, 'build_id': new_id}
