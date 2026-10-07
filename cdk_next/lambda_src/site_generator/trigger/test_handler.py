"""Tests for the site-generator stream trigger.

Run: CODEBUILD_PROJECT_NAME=x TABLE_NAME=y python -m pytest test_handler.py
"""
import os

import pytest

os.environ.setdefault("CODEBUILD_PROJECT_NAME", "test-project")
os.environ.setdefault("TABLE_NAME", "test-table")

import handler  # noqa: E402


def _record(pk, event_name="INSERT", old=None, new=None):
    ddb = {"Keys": {"PK": {"S": pk}}}
    if old is not None:
        ddb["OldImage"] = old
    if new is not None:
        ddb["NewImage"] = new
    return {"eventName": event_name, "dynamodb": ddb}


def _modify(pk, old, new):
    return _record(pk, "MODIFY", old, new)


def _refuse(**kw):
    raise handler.codebuild.exceptions.AccountLimitExceededException(
        {"Error": {"Code": "AccountLimitExceededException",
                   "Message": "Concurrent build limit exceeded"}},
        "StartBuild")


@pytest.fixture(autouse=True)
def rings(monkeypatch):
    """Capture dirtyAt stamps instead of calling DynamoDB."""
    stamps = []
    monkeypatch.setattr(
        handler.dynamodb, "update_item",
        lambda **kw: stamps.append(kw["ExpressionAttributeValues"][":now"]["N"]))
    return stamps


@pytest.fixture
def start_ok(monkeypatch):
    calls = []

    def start(**kw):
        calls.append(kw)
        return {"build": {"id": f"b-{len(calls)}"}}

    monkeypatch.setattr(handler.codebuild, "start_build", start)
    return calls


@pytest.mark.parametrize("pk", [
    "EVENT#abc", "GROUP#python-dc", "CATEGORY#social",
    "RECURRING#weekly", "ICAL#some-group",
    # /updates content: without these, a published post stays invisible
    # until the next daily safety-net build.
    "POST#my-announcement", "UPDATE#2026-W32",
    # Frozen past weeks; the export turns these into _archive/.
    "ARCHIVE#2026-W30",
])
def test_site_relevant_prefixes_trigger_a_build(pk, start_ok):
    result = handler.lambda_handler({"Records": [_record(pk)]}, None)
    assert result["started"] is True, f"{pk} should trigger a rebuild"


@pytest.mark.parametrize("pk", ["DRAFT#123", "SUBSCRIBER#a@b.c", "USER#42",
                                # The doorbell's own state item must never
                                # ring itself.
                                "RENDER#site"])
def test_irrelevant_prefixes_are_skipped(pk, monkeypatch, rings):
    monkeypatch.setattr(handler.codebuild, "start_build",
                        lambda **kw: pytest.fail("should not start a build"))

    result = handler.lambda_handler({"Records": [_record(pk)]}, None)
    assert result["started"] is False
    assert rings == []


def test_a_relevant_record_rings_before_it_builds(monkeypatch, rings):
    order = []
    monkeypatch.setattr(handler.dynamodb, "update_item",
                        lambda **kw: order.append("ring"))
    monkeypatch.setattr(handler.codebuild, "start_build",
                        lambda **kw: order.append("build") or {"build": {"id": "b"}})

    handler.lambda_handler({"Records": [_record("POST#x")]}, None)

    assert order == ["ring", "build"]


def test_a_refused_build_is_not_an_error_the_ring_is_the_promise(monkeypatch, rings):
    """concurrent_build_limit=1 makes StartBuild *fail*, not queue. The batch
    used to be re-raised so the mapping would retry once and then drop it
    (~200 failed invocations a week). Now the dirtyAt stamp is written first, so
    the running build's completion starts the follow-up (followup.py); the
    invocation succeeds."""
    monkeypatch.setattr(handler.codebuild, "start_build", _refuse)

    result = handler.lambda_handler({"Records": [_record("EVENT#x")]}, None)

    assert result["started"] is False
    assert "follow-up" in result["reason"]
    assert len(rings) == 1, "must be stamped dirty, or nothing re-drives it"


def test_another_codebuild_error_is_not_swallowed(monkeypatch):
    from botocore.exceptions import ClientError

    def broken(**kw):
        raise ClientError(
            {"Error": {"Code": "InvalidInputException", "Message": "nope"}},
            "StartBuild")

    monkeypatch.setattr(handler.codebuild, "start_build", broken)
    with pytest.raises(ClientError):
        handler.lambda_handler({"Records": [_record("EVENT#x")]}, None)


def test_the_trigger_does_not_inspect_running_builds(monkeypatch, start_ok):
    monkeypatch.setattr(handler.codebuild, "list_builds_for_project",
                        lambda **kw: pytest.fail("should not list builds"))
    monkeypatch.setattr(handler.codebuild, "batch_get_builds",
                        lambda **kw: pytest.fail("should not inspect builds"))

    assert handler.lambda_handler({"Records": [_record("EVENT#x")]},
                                  None)["started"] is True


def test_an_overlay_write_triggers_a_build(start_ok):
    """An overlay is the `overrides` map on the EVENT# item, not a key of its
    own — there is no OVERLAY# prefix. lux was filed believing otherwise and
    proposing that a prefix be added; this pins why that would be a no-op."""
    result = handler.lambda_handler(
        {"Records": [_modify(
            "EVENT#9aada59d7cca879c4c30c2962f0a4b84",
            {"title": {"S": "A"}},
            {"title": {"S": "A"}, "overrides": {"M": {"hidden": {"BOOL": True}}}})]},
        None)

    assert result["started"] is True
    assert "OVERLAY#" not in handler.RELEVANT_PREFIXES


# --- no-op rewrites ------------------------------------------------------

def test_an_identical_rewrite_is_ignored(monkeypatch, rings):
    monkeypatch.setattr(handler.codebuild, "start_build",
                        lambda **kw: pytest.fail("a no-op must not build"))
    item = {"title": {"S": "Python DC"}, "date": {"S": "2026-10-08"}}

    result = handler.lambda_handler(
        {"Records": [_modify("EVENT#x", item, dict(item))]}, None)

    assert result["started"] is False
    assert rings == []


def test_an_ical_cache_that_differs_only_in_fetch_bookkeeping_is_ignored(monkeypatch):
    monkeypatch.setattr(handler.codebuild, "start_build",
                        lambda **kw: pytest.fail("bookkeeping must not build"))
    old = {"events": {"L": []}, "meta": {"M": {"last_fetch": {"S": "a"}}},
           "updated_at": {"S": "2026-10-07T00:00:00Z"}, "ttl": {"N": "1"}}
    new = {"events": {"L": []}, "meta": {"M": {"last_fetch": {"S": "b"}}},
           "updated_at": {"S": "2026-10-07T04:00:00Z"}, "ttl": {"N": "2"}}

    result = handler.lambda_handler(
        {"Records": [_modify("ICAL#grp", old, new)]}, None)

    assert result["started"] is False


def test_an_ical_cache_whose_events_changed_builds(start_ok):
    old = {"events": {"L": []}, "updated_at": {"S": "a"}}
    new = {"events": {"L": [{"M": {"title": {"S": "New"}}}]}, "updated_at": {"S": "b"}}

    result = handler.lambda_handler(
        {"Records": [_modify("ICAL#grp", old, new)]}, None)

    assert result["started"] is True


@pytest.mark.parametrize("name,old,new", [
    ("INSERT", None, {"title": {"S": "x"}}),
    ("REMOVE", {"title": {"S": "x"}}, None),
])
def test_inserts_and_removes_always_count(name, old, new, start_ok):
    result = handler.lambda_handler(
        {"Records": [_record("EVENT#x", name, old, new)]}, None)
    assert result["started"] is True


def test_a_modify_without_images_is_treated_as_a_change(start_ok):
    """A stream configured without both images (or a trimmed record) cannot be
    compared, so it must fail open and build, never fail closed and drop."""
    result = handler.lambda_handler(
        {"Records": [_record("EVENT#x", "MODIFY")]}, None)
    assert result["started"] is True


def test_one_real_change_among_noops_still_builds(start_ok):
    item = {"title": {"S": "same"}}
    records = [_modify("EVENT#a", item, dict(item)),
               _modify("EVENT#b", {"title": {"S": "old"}}, {"title": {"S": "new"}}),
               _modify("EVENT#c", item, dict(item))]

    assert handler.lambda_handler({"Records": records}, None)["started"] is True
    assert len(start_ok) == 1


def test_changed_attrs_names_what_changed():
    rec = _modify("EVENT#x", {"a": {"S": "1"}, "b": {"S": "1"}},
                  {"a": {"S": "2"}, "b": {"S": "1"}, "c": {"S": "3"}})
    assert handler._changed_attrs(rec) == ["a", "c"]


# --- scheduled ring (nightly time roll) ----------------------------------

def test_a_scheduled_ring_builds_without_any_stream_records(start_ok, rings):
    result = handler.lambda_handler({"reason": "time_roll"}, None)

    assert result["started"] is True
    assert len(rings) == 1
    assert len(start_ok) == 1


def test_a_scheduled_ring_during_a_build_is_left_to_the_followup(monkeypatch, rings):
    monkeypatch.setattr(handler.codebuild, "start_build", _refuse)

    result = handler.lambda_handler({"reason": "time_roll"}, None)

    assert result["started"] is False
    assert len(rings) == 1


def test_the_stacks_event_filter_lists_the_same_prefixes():
    """stacks/site_generator_stack.py filters the stream by prefix so unrelated
    records never invoke this function. A prefix added here but not there would
    be silently dropped before it arrives."""
    import ast
    stack = os.path.join(os.path.dirname(__file__), "..", "..", "..", "stacks",
                         "site_generator_stack.py")
    tree = ast.parse(open(stack).read())
    listed = next(
        ast.literal_eval(node.value) for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", "") == "SITE_RELEVANT_PREFIXES" for t in node.targets))

    assert tuple(listed) == handler.RELEVANT_PREFIXES
