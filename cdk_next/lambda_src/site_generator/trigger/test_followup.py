"""Tests for the site-build follow-up (starts one more build if the site went
dirty during the one that just finished).

Run: CODEBUILD_PROJECT_NAME=x TABLE_NAME=y python -m pytest test_followup.py
"""
import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("CODEBUILD_PROJECT_NAME", "test-project")
os.environ.setdefault("TABLE_NAME", "test-table")

import followup  # noqa: E402

START_MS = 1_790_000_000_000


def _event(status="SUCCEEDED", build_id="test-project:abc"):
    return {"detail": {"build-id": build_id, "build-status": status}}


@pytest.fixture
def world(monkeypatch):
    """A finished build that started at START_MS, with the state item's two
    stamps (dirtyAt from the trigger, exportAt from the build) to compare."""
    state = {"dirty": 0, "export": 0, "started": [], "refuse": False}

    def get_item(**kw):
        item = {}
        if state["dirty"]:
            item["dirtyAt"] = {"N": str(state["dirty"])}
        if state["export"]:
            item["exportAt"] = {"N": str(state["export"])}
        return {"Item": item} if item else {}

    monkeypatch.setattr(followup.dynamodb, "get_item", get_item)
    monkeypatch.setattr(followup.codebuild, "batch_get_builds", lambda **kw: {
        "builds": [{"startTime": datetime.fromtimestamp(START_MS / 1000, timezone.utc)}]})

    def start(**kw):
        if state["refuse"]:
            raise followup.codebuild.exceptions.AccountLimitExceededException(
                {"Error": {"Code": "AccountLimitExceededException", "Message": "busy"}},
                "StartBuild")
        state["started"].append(kw)
        return {"build": {"id": "follow-1"}}

    monkeypatch.setattr(followup.codebuild, "start_build", start)
    return state


def test_a_ring_after_the_build_started_gets_a_followup(world):
    world["dirty"] = START_MS + 1

    result = followup.lambda_handler(_event(), None)

    assert result == {"started": True, "build_id": "follow-1"}


def test_a_ring_before_the_build_started_needs_nothing(world):
    world["dirty"] = START_MS - 1

    assert followup.lambda_handler(_event(), None)["started"] is False
    assert world["started"] == []


def test_a_ring_exactly_at_build_start_is_covered(world):
    """handler.py stamps dirtyAt and then calls StartBuild, so the build it
    starts is never later than its own stamp's millisecond. Equal is clean, or
    every build would trigger a follow-up of itself."""
    world["dirty"] = START_MS

    assert followup.lambda_handler(_event(), None)["started"] is False


def test_never_rung_means_nothing_to_do(world):
    world["dirty"] = 0

    assert followup.lambda_handler(_event(), None)["started"] is False


@pytest.mark.parametrize("status", ["FAILED", "STOPPED", "TIMED_OUT", "FAULT"])
def test_a_failed_build_still_gets_a_followup_for_rings_after_it_started(world, status):
    world["dirty"] = START_MS + 5

    assert followup.lambda_handler(_event(status), None)["started"] is True


@pytest.mark.parametrize("status", ["FAILED", "STOPPED", "TIMED_OUT", "FAULT"])
def test_a_failed_build_does_not_retry_itself(world, status):
    """Rings from before the failed build are not re-driven, or a persistently
    broken build would loop forever. They wait for the next ring or the daily
    build."""
    world["dirty"] = START_MS - 1000

    assert followup.lambda_handler(_event(status), None)["started"] is False


def test_a_busy_project_is_not_an_error(world):
    """A manual rebuild or the daily rule took the slot. Its own completion
    event re-runs this check, so the ring survives."""
    world["dirty"] = START_MS + 1
    world["refuse"] = True

    result = followup.lambda_handler(_event(), None)

    assert result["started"] is False
    assert "re-check" in result["reason"]


def test_an_event_without_a_build_id_is_ignored(world):
    assert followup.lambda_handler({"detail": {}}, None)["started"] is False


def test_an_unknown_build_is_ignored(world, monkeypatch):
    monkeypatch.setattr(followup.codebuild, "batch_get_builds",
                        lambda **kw: {"builds": []})

    assert followup.lambda_handler(_event(), None)["started"] is False


# --- the export stamp ----------------------------------------------------

def test_a_ring_between_build_start_and_its_export_is_covered(world):
    """pip install comes first, so the table is read ~30s after the build
    starts. A second shard's invocation a couple of seconds after the first
    collides and rings in that gap; the build still reads it. Comparing with
    the start time instead would add a redundant 4-minute build after almost
    every aggregator run."""
    world["export"] = START_MS + 30_000
    world["dirty"] = START_MS + 2_000

    assert followup.lambda_handler(_event(), None)["started"] is False


def test_a_ring_after_the_export_read_needs_a_followup(world):
    world["export"] = START_MS + 30_000
    world["dirty"] = START_MS + 31_000

    assert followup.lambda_handler(_event(), None)["started"] is True


def test_a_stale_export_stamp_falls_back_to_the_start_time(world):
    """exportAt older than the build means this build never stamped (the
    update failed, or the item predates the feature). Trusting it would see
    every later ring as uncovered and rebuild forever."""
    world["export"] = START_MS - 600_000
    world["dirty"] = START_MS - 1

    assert followup.lambda_handler(_event(), None)["started"] is False


def test_a_stale_export_stamp_still_follows_up_a_ring_after_the_start(world):
    world["export"] = START_MS - 600_000
    world["dirty"] = START_MS + 1

    assert followup.lambda_handler(_event(), None)["started"] is True
