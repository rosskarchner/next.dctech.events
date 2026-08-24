"""Starts a weekly run of the discovery agent on AgentCore Runtime."""
import hashlib
import json
import os
import uuid
from datetime import date

import boto3
from botocore.config import Config
from botocore.exceptions import ReadTimeoutError

AGENT_RUNTIME_ARN = os.environ['AGENT_RUNTIME_ARN']
SESSION_ID_MIN = 33
SESSION_ID_MAX = 256


def _session_id(run_id):
    """A session id derived from `run_id`, padded to the API's minimum."""
    if len(run_id) >= SESSION_ID_MIN:
        return run_id[:SESSION_ID_MAX]
    filler = hashlib.sha256(run_id.encode()).hexdigest()
    return f'{run_id}-{filler}'[:SESSION_ID_MAX]


def lambda_handler(event, context):
    event = event or {}
    run_id = event.get('run_id') or (
        f"disc-{date.today().isoformat()}-{(context.aws_request_id if context else uuid.uuid4().hex)[:8]}"
    )
    payload = {'run_id': run_id}
    if event.get('dry_run'):
        payload['dry_run'] = True

    client = boto3.client(
        'bedrock-agentcore',
        config=Config(connect_timeout=5, read_timeout=5, retries={'max_attempts': 0}),
    )
    status = None
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=_session_id(run_id),
            payload=json.dumps(payload).encode(),
            qualifier='DEFAULT',
        )
        status = response.get('statusCode')
    except ReadTimeoutError:
        # Runtime invocation can run longer than the trigger's practical budget.
        # A read timeout here means the call was sent but no immediate response
        # frame arrived; report accepted-for-processing and rely on runtime logs.
        status = 202

    result = {
        'run_id': run_id,
        'dry_run': bool(payload.get('dry_run')),
        'status': status,
    }
    print(json.dumps(result))
    return result
