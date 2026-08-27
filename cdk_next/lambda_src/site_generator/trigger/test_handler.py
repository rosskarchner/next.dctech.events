"""Tests for the site-generator stream trigger.

Run: CODEBUILD_PROJECT_NAME=x python -m pytest test_handler.py
"""
import os

import pytest

os.environ.setdefault("CODEBUILD_PROJECT_NAME", "test-project")

import handler  # noqa: E402


def _record(pk):
    return {"dynamodb": {"Keys": {"PK": {"S": pk}}}}


@pytest.mark.parametrize("pk", [
    "EVENT#abc", "GROUP#python-dc", "CATEGORY#social",
    "RECURRING#weekly", "ICAL#some-group",
    # /updates content: without these, a published post stays invisible
    # until the next daily safety-net build.
    "POST#my-announcement", "UPDATE#2026-W32",
])
def test_site_relevant_prefixes_trigger_a_build(pk, monkeypatch):
    started = {}

    monkeypatch.setattr(handler.codebuild, "start_build",
                        lambda **kw: started.setdefault(
                            "build", {"build": {"id": "b-1"}}))

    result = handler.lambda_handler({"Records": [_record(pk)]}, None)
    assert result["started"] is True, f"{pk} should trigger a rebuild"


@pytest.mark.parametrize("pk", ["DRAFT#123", "SUBSCRIBER#a@b.c", "USER#42"])
def test_irrelevant_prefixes_are_skipped(pk, monkeypatch):
    monkeypatch.setattr(handler.codebuild, "start_build",
                        lambda **kw: pytest.fail("should not start a build"))

    result = handler.lambda_handler({"Records": [_record(pk)]}, None)
    assert result["started"] is False


def test_a_build_is_attempted_even_while_one_is_running(monkeypatch):
    """The trigger used to skip here and say the scheduled build would catch
    up. That net is only daily, so a change landing during a build could sit
    unpublished for a day (next_dctech_events-lux)."""
    started = {}
    monkeypatch.setattr(handler.codebuild, "start_build",
                        lambda **kw: started.setdefault(
                            "build", {"build": {"id": "b-2"}}))

    result = handler.lambda_handler({"Records": [_record("POST#x")]}, None)

    assert result["started"] is True
    assert "build" in started


def test_a_refused_build_is_raised_so_the_batch_is_retried(monkeypatch):
    """concurrent_build_limit=1 makes StartBuild *fail*, not queue — which is
    not what the limit sounds like. Raising hands the batch back to the event
    source mapping, so the work is re-driven rather than dropped the way the
    old skip dropped it."""
    def refuse(**kw):
        raise handler.codebuild.exceptions.AccountLimitExceededException(
            {"Error": {"Code": "AccountLimitExceededException",
                       "Message": "Concurrent build limit exceeded"}},
            "StartBuild")

    monkeypatch.setattr(handler.codebuild, "start_build", refuse)

    with pytest.raises(Exception, match="Concurrent build limit"):
        handler.lambda_handler({"Records": [_record("EVENT#x")]}, None)


def test_another_codebuild_error_is_not_swallowed(monkeypatch):
    from botocore.exceptions import ClientError

    def broken(**kw):
        raise ClientError(
            {"Error": {"Code": "InvalidInputException", "Message": "nope"}},
            "StartBuild")

    monkeypatch.setattr(handler.codebuild, "start_build", broken)
    with pytest.raises(ClientError):
        handler.lambda_handler({"Records": [_record("EVENT#x")]}, None)


def test_the_trigger_no_longer_inspects_running_builds(monkeypatch):
    # Two fewer CodeBuild API calls per stream batch, and no way to regress
    # into skipping.
    monkeypatch.setattr(handler.codebuild, "list_builds_for_project",
                        lambda **kw: pytest.fail("should not list builds"))
    monkeypatch.setattr(handler.codebuild, "batch_get_builds",
                        lambda **kw: pytest.fail("should not inspect builds"))
    monkeypatch.setattr(handler.codebuild, "start_build",
                        lambda **kw: {"build": {"id": "b-3"}})

    assert handler.lambda_handler({"Records": [_record("EVENT#x")]},
                                  None)["started"] is True


def test_an_overlay_write_triggers_a_build(monkeypatch):
    """An overlay is the `overrides` map on the EVENT# item, not a key of its
    own — there is no OVERLAY# prefix. lux was filed believing otherwise and
    proposing that a prefix be added; this pins why that would be a no-op."""
    monkeypatch.setattr(handler.codebuild, "start_build",
                        lambda **kw: {"build": {"id": "b-4"}})

    result = handler.lambda_handler(
        {"Records": [_record("EVENT#9aada59d7cca879c4c30c2962f0a4b84")]}, None)

    assert result["started"] is True
    assert "OVERLAY#" not in handler.RELEVANT_PREFIXES
