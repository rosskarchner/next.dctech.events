"""Starts a weekly run of the calendar QC agent on AgentCore Runtime.

EventBridge can't call InvokeAgentRuntime directly — it's a data-plane
operation — so this thin Lambda sits between the schedule and the runtime. It
also mints the run id, which is what makes the whole run revertible later
(revert_qa_run), and returns it so a manual invocation can be traced.

The agent itself runs for many minutes; this returns as soon as the runtime
accepts the invocation.
"""
import hashlib
import json
import os
import uuid
from datetime import date

import boto3

AGENT_RUNTIME_ARN = os.environ['AGENT_RUNTIME_ARN']

# InvokeAgentRuntime constrains runtimeSessionId to 33-256 characters.
SESSION_ID_MIN = 33
SESSION_ID_MAX = 256


def _session_id(run_id):
    """A session id derived from `run_id`, padded to the API's minimum.

    Run ids are 22 characters and deliberately stay that way: the weekly
    digest prints one for a human to paste into `revert_qa_run(...)`, so the
    padding belongs here rather than in the id itself.

    Padded with a hash of the run id, not a random value, because
    runtimeSessionId is an idempotency token — a Lambda retry has to land on
    the same session instead of opening a second conversation with the agent.
    """
    if len(run_id) >= SESSION_ID_MIN:
        return run_id[:SESSION_ID_MAX]
    filler = hashlib.sha256(run_id.encode()).hexdigest()
    return f'{run_id}-{filler}'[:SESSION_ID_MAX]


def lambda_handler(event, context):
    event = event or {}
    run_id = event.get('run_id') or (
        f"qc-{date.today().isoformat()}-{uuid.uuid4().hex[:8]}"
    )
    payload = {'run_id': run_id}
    # Manual invocations can preview a run without writing anything.
    if event.get('dry_run'):
        payload['dry_run'] = True
    if event.get('limit'):
        payload['limit'] = event['limit']

    client = boto3.client('bedrock-agentcore')
    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        # Each weekly pass is its own conversation — nothing carries over.
        runtimeSessionId=_session_id(run_id),
        payload=json.dumps(payload).encode(),
        qualifier='DEFAULT',
    )

    result = {
        'run_id': run_id,
        'dry_run': bool(payload.get('dry_run')),
        'status': response.get('statusCode'),
    }
    print(json.dumps(result))
    return result
