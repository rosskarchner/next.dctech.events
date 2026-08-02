"""Delete unconfirmed Cognito accounts older than a week.

Ported from the old TypeScript app's cleanup_unconfirmed_users Lambda,
which ran weekly until dctech-events-api was deleted on 2026-08-02. The
user pool is shared and long-lived, so without this, abandoned signups
accumulate forever.
"""
import os
from datetime import datetime, timedelta, timezone

import boto3

cognito = boto3.client('cognito-idp')

USER_POOL_ID = os.environ['COGNITO_USER_POOL_ID']
MAX_AGE_DAYS = int(os.environ.get('MAX_AGE_DAYS', '7'))
# Guard against a bug or an API change wiping the pool: a normal week sees
# a handful of abandoned signups, so anything wholesale is a red flag.
MAX_DELETIONS = int(os.environ.get('MAX_DELETIONS', '50'))


def lambda_handler(event, context):
    dry_run = bool((event or {}).get('dry_run'))
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    candidates = []
    for page in cognito.get_paginator('list_users').paginate(UserPoolId=USER_POOL_ID):
        for user in page.get('Users', []):
            if user.get('UserStatus') != 'UNCONFIRMED':
                continue
            created = user.get('UserCreateDate')
            if created and created.astimezone(timezone.utc) < cutoff:
                candidates.append((user['Username'], created))

    if len(candidates) > MAX_DELETIONS:
        msg = (f'Refusing to delete {len(candidates)} unconfirmed users '
               f'(limit {MAX_DELETIONS}) — investigate before re-running.')
        print(msg)
        return {'deleted': 0, 'candidates': len(candidates), 'aborted': msg}

    deleted, errors = 0, []
    for username, created in candidates:
        if dry_run:
            print(f'[dry-run] would delete {username} (created {created})')
            deleted += 1
            continue
        try:
            cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=username)
            print(f'Deleted unconfirmed user {username} (created {created})')
            deleted += 1
        except Exception as e:
            print(f'ERROR deleting {username}: {e}')
            errors.append(f'{username}: {e}')

    result = {'deleted': deleted, 'candidates': len(candidates),
              'errors': errors[:10], 'dry_run': dry_run}
    print(result)
    return result
