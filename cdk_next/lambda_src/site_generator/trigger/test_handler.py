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

    monkeypatch.setattr(handler.codebuild, "list_builds_for_project",
                        lambda **kw: {"ids": []})
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


def test_no_build_started_while_one_is_in_progress(monkeypatch):
    monkeypatch.setattr(handler.codebuild, "list_builds_for_project",
                        lambda **kw: {"ids": ["b-0"]})
    monkeypatch.setattr(handler.codebuild, "batch_get_builds",
                        lambda **kw: {"builds": [{"buildStatus": "IN_PROGRESS"}]})
    monkeypatch.setattr(handler.codebuild, "start_build",
                        lambda **kw: pytest.fail("should not start a build"))

    result = handler.lambda_handler({"Records": [_record("POST#x")]}, None)
    assert result["started"] is False
    assert result["reason"] == "build in progress"
