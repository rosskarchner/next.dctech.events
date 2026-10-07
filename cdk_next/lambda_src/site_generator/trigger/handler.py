"""DynamoDB-Streams-fed CodeBuild trigger for the static site generator.

The stream is a doorbell: this function decides whether anything the site shows
could have changed, rings by stamping RENDER#site.dirtyAt, and tries to start a
build. It never raises on a busy project. `followup.py` owns the other half.

Why the dirty stamp exists. The project carries concurrent_build_limit=1,
because every build ends in `s3 sync --delete` over the whole bucket and two
overlapping builds can have the older one deleting what the newer just wrote.
That limit does not queue: a second StartBuild *fails* with
AccountLimitExceededException. This function used to re-raise so the event source
mapping would retry the batch once, and after that the work was dropped with only
the daily build as a backstop (next_dctech_events-lux). Measured over a week
(2026-09-30..10-07), that meant ~200 failed invocations clustered in the minute
after each aggregator-driven build began.

Now a refused start is fine, provided something remembers. dirtyAt does: when a
build finishes, followup.py starts exactly one more if dirtyAt is later than that
build's start time. A change that landed in the minute before a build's export
phase is picked up by that build anyway; one that landed after is re-driven.

Rings come from three places:
  * stream records (a real content change),
  * the nightly time-roll rule (no data changed, but "today" did),
  * nothing else; manual rebuilds call StartBuild directly and do not need this.
"""
import os
import time

import boto3

PROJECT_NAME = os.environ['CODEBUILD_PROJECT_NAME']
TABLE_NAME = os.environ.get('TABLE_NAME', '')

STATE_KEY = {'PK': 'RENDER#site', 'SK': 'STATE'}

# Only content that feeds the static site should trigger a rebuild.
# POST# (free-form /updates posts) and UPDATE# (weekly roundups) both render
# into /updates, so publishing either has to rebuild the site — without them
# a new post sits invisible until the daily safety-net build. ARCHIVE# holds the
# frozen past weeks that the export turns into _archive/ and calgen renders on
# archived week pages. RENDER# is deliberately absent: this function writes it.
RELEVANT_PREFIXES = ('EVENT#', 'GROUP#', 'CATEGORY#', 'RECURRING#', 'ICAL#',
                     'POST#', 'UPDATE#', 'ARCHIVE#')

# Attributes whose changes never alter a rendered page. They differ on an
# otherwise identical rewrite: the iCal cache stamps updated_at/ttl and its fetch
# meta (etag, last_fetch) on every put. Add to this list only after checking
# the site really does not read the attribute.
NON_SEMANTIC_ATTRS = frozenset({'updated_at', 'ttl', 'meta'})

codebuild = boto3.client('codebuild')
dynamodb = boto3.client('dynamodb')


def _changed_attrs(record):
    """Names of attributes that differ between old and new image, minus the
    non-semantic ones. None means "cannot tell, treat as a change" (insert,
    remove, or a stream view without both images)."""
    ddb = record.get('dynamodb', {})
    old, new = ddb.get('OldImage'), ddb.get('NewImage')
    if record.get('eventName') != 'MODIFY' or old is None or new is None:
        return None
    return sorted(
        k for k in set(old) | set(new)
        if k not in NON_SEMANTIC_ATTRS and old.get(k) != new.get(k)
    )


def _classify(records):
    """Return (relevant, noops, changes) for a batch of stream records.

    changes is a small list of "PREFIX:eventName:attrs" strings, logged so the
    NON_SEMANTIC_ATTRS list can be tuned from real traffic instead of guesses.
    """
    relevant = noops = 0
    changes = []
    for record in records:
        pk = record.get('dynamodb', {}).get('Keys', {}).get('PK', {}).get('S', '')
        if not pk.startswith(RELEVANT_PREFIXES):
            continue
        changed = _changed_attrs(record)
        label = f"{pk.split('#')[0]}:{record.get('eventName', '?')}"
        if changed is not None and not changed:
            noops += 1
            continue
        relevant += 1
        if len(changes) < 10:
            changes.append(label + (':' + ','.join(changed[:5]) if changed else ''))
    return relevant, noops, changes


def _ring(now_ms):
    if not TABLE_NAME:
        return
    dynamodb.update_item(
        TableName=TABLE_NAME,
        Key={k: {'S': v} for k, v in STATE_KEY.items()},
        UpdateExpression='SET dirtyAt = :now',
        ExpressionAttributeValues={':now': {'N': str(now_ms)}},
    )


def lambda_handler(event, context):
    if 'Records' in event:
        records = event['Records']
        relevant, noops, changes = _classify(records)
        if not relevant:
            print(f'No site-relevant changes in {len(records)} records '
                  f'({noops} no-op rewrites ignored); skipping')
            return {'started': False, 'reason': 'no relevant changes'}
        reason = f'{relevant} relevant changes ({noops} no-ops ignored): {changes}'
    else:
        # Scheduled ring (the nightly time roll): the table did not change, but
        # past events have to drop off the pages.
        reason = f"scheduled ring: {event.get('reason', 'time_roll')}"

    # Ring first, then try to build. If StartBuild is refused, the build that
    # is running was started before this stamp, so its completion will see the
    # site is dirty and start the follow-up.
    now_ms = int(time.time() * 1000)
    _ring(now_ms)

    try:
        build = codebuild.start_build(projectName=PROJECT_NAME)
    except codebuild.exceptions.AccountLimitExceededException:
        print(f'A build is already running; follow-up will cover it ({reason})')
        return {'started': False, 'reason': 'build running; follow-up pending'}

    build_id = build['build']['id']
    print(f'Started build {build_id} ({reason})')
    return {'started': True, 'build_id': build_id}
