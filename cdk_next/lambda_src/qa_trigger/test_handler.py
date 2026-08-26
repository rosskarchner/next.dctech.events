"""Tests for the calendar QC agent trigger.

Run: AGENT_RUNTIME_ARN=arn:test python -m pytest test_handler.py
"""
import os
import uuid
from datetime import date

os.environ.setdefault("AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/t")

import handler  # noqa: E402


def _default_run_id():
    """The id lambda_handler mints when it is not given one."""
    return f"qc-{date.today().isoformat()}-{uuid.uuid4().hex[:8]}"


def test_the_minted_run_id_is_too_short_for_the_api_on_its_own():
    # The bug this guards: a 22-character run id was passed straight through
    # as runtimeSessionId and every scheduled run died on validation.
    assert len(_default_run_id()) < handler.SESSION_ID_MIN


def test_session_id_meets_the_api_minimum():
    session = handler._session_id(_default_run_id())
    assert handler.SESSION_ID_MIN <= len(session) <= handler.SESSION_ID_MAX


def test_session_id_keeps_the_run_id_as_its_prefix():
    run_id = _default_run_id()
    assert handler._session_id(run_id).startswith(run_id)


def test_session_id_is_deterministic():
    # runtimeSessionId is an idempotency token, so a Lambda retry of the same
    # run must reuse the session rather than open a second conversation.
    run_id = _default_run_id()
    assert handler._session_id(run_id) == handler._session_id(run_id)


def test_distinct_runs_get_distinct_sessions():
    assert handler._session_id(_default_run_id()) != \
        handler._session_id(_default_run_id())


def test_an_already_long_run_id_is_passed_through_unpadded():
    run_id = "qc-2026-08-10-" + "a" * 40
    assert handler._session_id(run_id) == run_id


def test_an_over_long_run_id_is_clipped_to_the_api_maximum():
    assert len(handler._session_id("qc-" + "a" * 400)) == handler.SESSION_ID_MAX


def test_session_id_matches_the_live_botocore_constraint():
    """Pin the constants to the service model rather than to a comment."""
    import botocore.session
    shape = (botocore.session.get_session()
             .get_service_model("bedrock-agentcore")
             .operation_model("InvokeAgentRuntime")
             .input_shape.members["runtimeSessionId"])
    assert shape.metadata["min"] == handler.SESSION_ID_MIN
    assert shape.metadata["max"] == handler.SESSION_ID_MAX


# ── Step Functions task token ────────────────────────────────────────
# Forwarded into the agent payload, never released here: this Lambda returns
# seconds after the runtime accepts the invocation, and the pass takes minutes.


class _FakeAgentCore:
    def __init__(self):
        self.calls = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return {'statusCode': 200}


def _invoke(event, monkeypatch):
    import json
    client = _FakeAgentCore()
    monkeypatch.setattr(handler.boto3, 'client', lambda *a, **k: client)
    result = handler.lambda_handler(event, None)
    payload = json.loads(client.calls[0]['payload'].decode())
    return result, payload


def test_task_token_is_forwarded_into_the_agent_payload(monkeypatch):
    _, payload = _invoke({'task_token': 'tok-abc'}, monkeypatch)
    assert payload['task_token'] == 'tok-abc'


def test_no_task_token_means_the_key_is_absent(monkeypatch):
    # The agent reads its presence as "a state machine owns the rebuild", so
    # an empty string must not look like a token.
    _, payload = _invoke({}, monkeypatch)
    assert 'task_token' not in payload
    _, payload = _invoke({'task_token': ''}, monkeypatch)
    assert 'task_token' not in payload


def test_the_token_is_never_logged(monkeypatch, capsys):
    # A token is a capability to complete someone else's execution.
    result, _ = _invoke({'task_token': 'tok-secret'}, monkeypatch)
    assert 'tok-secret' not in capsys.readouterr().out
    assert 'tok-secret' not in str(result)


def test_the_result_says_whether_something_is_waiting(monkeypatch):
    result, _ = _invoke({'task_token': 'tok-abc'}, monkeypatch)
    assert result['awaited'] is True
    result, _ = _invoke({}, monkeypatch)
    assert result['awaited'] is False


def test_the_state_machine_run_id_becomes_the_qc_run_id(monkeypatch):
    # The machine passes $$.Execution.Name, which is what makes a run
    # revertible from the digest.
    result, payload = _invoke(
        {'run_id': 'monday-2026-08-31', 'task_token': 'tok'}, monkeypatch)
    assert payload['run_id'] == result['run_id'] == 'monday-2026-08-31'
