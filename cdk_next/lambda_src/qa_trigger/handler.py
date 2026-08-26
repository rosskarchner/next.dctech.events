"""Starts a weekly run of the calendar QC agent on AgentCore Runtime.

EventBridge can't call InvokeAgentRuntime directly — it's a data-plane
operation — so this thin Lambda sits between the schedule and the runtime. It
also mints the run id, which is what makes the whole run revertible later
(revert_qa_run), and returns it so a manual invocation can be traced.

`InvokeAgentRuntime` is a *blocking* data-plane call: it does not return until
the agent's entrypoint does, which for a QC pass is many minutes. This Lambda
therefore starts the pass and deliberately abandons the response, with a read
timeout short enough to return cleanly rather than being killed at its own
timeout. The agent keeps running server-side after the caller disconnects —
that is what `add_async_task` in the agent's entrypoint is for, and the runs on
2026-08-08 and 2026-08-24 both wrote their overlays and sent their digests
while this Lambda had already been killed at 60s.

Before that read timeout was set, every invocation ended in `Status: timeout`
and Lambda's async retry policy started the agent up to three times over. The
`runtimeSessionId` idempotency below is what kept those from becoming three
independent conversations, but two of them still burned model calls.

A `task_token` is forwarded into the agent payload rather than released here,
for the same reason: the Monday state machine waits on the pass finishing, and
only the agent knows when that is.
"""
import hashlib
import json
import os
import uuid
from datetime import date

import boto3
from botocore.config import Config
from botocore.exceptions import ConnectionError as BotoConnectionError
from botocore.exceptions import ReadTimeoutError

AGENT_RUNTIME_ARN = os.environ['AGENT_RUNTIME_ARN']

# Long enough for the runtime to accept the invocation, far short of the pass
# itself. Retries are off because a retry here would start a second pass, not
# recover the first.
_START_CONFIG = Config(read_timeout=10, connect_timeout=5,
                       retries={'max_attempts': 0})

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
    # Passed straight through when a Step Functions execution is waiting on
    # the pass. The agent releases it on completion — this Lambda cannot,
    # since it returns long before the agent is done. Deliberately not logged:
    # a token is a capability to complete someone else's execution.
    if event.get('task_token'):
        payload['task_token'] = event['task_token']

    client = boto3.client('bedrock-agentcore', config=_START_CONFIG)
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            # Each weekly pass is its own conversation — nothing carries over.
            runtimeSessionId=_session_id(run_id),
            payload=json.dumps(payload).encode(),
            qualifier='DEFAULT',
        )
        status = response.get('statusCode')
    except (ReadTimeoutError, BotoConnectionError):
        # The expected path, not an error: the runtime took the invocation and
        # is now working. Waiting for it is what this function must not do.
        status = 'started'

    result = {
        'run_id': run_id,
        'dry_run': bool(payload.get('dry_run')),
        'status': status,
        'awaited': bool(payload.get('task_token')),
    }
    print(json.dumps(result))
    return result
