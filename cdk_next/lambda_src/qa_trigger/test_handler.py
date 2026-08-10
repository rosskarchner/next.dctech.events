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
