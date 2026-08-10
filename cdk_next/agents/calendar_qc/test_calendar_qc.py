"""Tests for the QC agent's plumbing — the parts that don't need a model.

The judgement lives in the prompts and can only be evaluated against real
feeds. What's testable here is everything around it: that dry-run really
cannot write, that a chatty model's output still parses, and that the digest
always tells you how to undo the run.

Run: python -m pytest test_calendar_qc.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import digest  # noqa: E402
import main  # noqa: E402


class FakeTool:
    def __init__(self, name):
        self.tool_name = name


# ── Tool gating ────────────────────────────────────────────────────

ALL_TOOLS = [FakeTool(n) for n in (
    'list_pending_qa', 'get_events', 'get_event', 'get_overlay',
    'set_overlay', 'resolve_qa_review', 'trigger_rebuild',
    'delete_single_event', 'approve_submission', 'revert_qa_run',
)]


def _names(tools):
    return {main._tool_name(t) for t in tools}


def test_triage_gets_only_the_tools_it_needs():
    selected = _names(main._select(ALL_TOOLS, main._TRIAGE_TOOLS, dry_run=False))
    assert selected == main._TRIAGE_TOOLS


def test_moderation_and_deletion_tools_are_never_exposed():
    # The MCP server also carries group/submission admin; a calendar QC agent
    # has no business approving submissions or deleting events.
    selected = _names(main._select(ALL_TOOLS, main._TRIAGE_TOOLS, dry_run=False))
    assert 'approve_submission' not in selected
    assert 'delete_single_event' not in selected


def test_dry_run_withholds_every_write_tool():
    selected = _names(main._select(ALL_TOOLS, main._TRIAGE_TOOLS, dry_run=True))
    assert selected == {'list_pending_qa', 'get_events', 'get_event', 'get_overlay'}
    assert not selected & main._WRITE_TOOLS


def test_dry_run_never_exposes_revert():
    # Undoing a previous run mid-dry-run would be a spectacular own goal.
    assert 'revert_qa_run' not in _names(
        main._select(ALL_TOOLS, main._TRIAGE_TOOLS | {'revert_qa_run'}, dry_run=True))


def test_tool_name_falls_back_to_the_spec():
    class SpecOnly:
        tool_spec = {'name': 'get_events'}

    assert main._tool_name(SpecOnly()) == 'get_events'


# ── Parsing model output ───────────────────────────────────────────

def test_extract_json_reads_a_fenced_block():
    text = 'Here is what I found:\n```json\n{"hidden": [{"guid": "g1"}]}\n```\nDone.'
    assert main._extract_json(text) == {'hidden': [{'guid': 'g1'}]}


def test_extract_json_reads_a_bare_object_amid_prose():
    text = 'I reviewed everything. {"reviewed": 4} Let me know if you need more.'
    assert main._extract_json(text) == {'reviewed': 4}


def test_extract_json_prefers_the_last_block():
    # Models often restate an example from the prompt before the real answer.
    text = '```json\n{"reviewed": 0}\n```\nActually:\n```json\n{"reviewed": 7}\n```'
    assert main._extract_json(text) == {'reviewed': 7}


def test_extract_json_survives_unparseable_output():
    # A pass that did the work but rambled should not be thrown away.
    assert main._extract_json('I could not complete the task.') == {}


def test_extract_json_handles_empty_output():
    assert main._extract_json(None) == {}


# ── Digest ─────────────────────────────────────────────────────────

RESULTS = {
    'duplicates': [{'guid': 'g2', 'title': 'Same Talk', 'canonical': 'g1',
                    'reason': 're-post'}],
    'hidden': [{'guid': 'g3', 'title': 'Norfolk Luncheon',
                'reason': 'not DC metro'}],
}


def test_digest_always_says_how_to_undo_the_run():
    # This is the whole safety net now that there's no PR to close.
    subject, text, body_html = digest.render('qc-r1', RESULTS, reviewed=5)
    assert 'revert_qa_run("qc-r1")' in text
    assert 'revert_qa_run(&quot;qc-r1&quot;)' in body_html or 'qc-r1' in body_html


def test_digest_counts_fixes_in_the_subject():
    subject, _, _ = digest.render('qc-r1', RESULTS, reviewed=5)
    assert '2 fixes across 5 events' in subject


def test_digest_reports_a_clean_run_distinctly():
    subject, text, _ = digest.render('qc-r1', {}, reviewed=12)
    assert '12 events reviewed, no changes' in subject
    assert 'No quality issues found.' in text


def test_digest_omits_empty_sections():
    _, text, _ = digest.render('qc-r1', RESULTS, reviewed=5)
    assert 'Hidden (out of area)' in text
    assert 'Flagged for you' not in text


def test_digest_escapes_titles_in_html():
    results = {'hidden': [{'guid': 'g1', 'title': '<script>x</script>',
                           'reason': 'out of area'}]}
    _, _, body_html = digest.render('qc-r1', results, reviewed=1)
    assert '<script>' not in body_html
    assert '&lt;script&gt;' in body_html


def test_digest_send_never_raises(monkeypatch):
    # A mail failure must not turn a good QC run into a failed one.
    def boom(*a, **kw):
        raise RuntimeError('SES is down')

    monkeypatch.setattr(digest, 'render', lambda *a, **kw: ('s', 't', 'h'))
    monkeypatch.setitem(sys.modules, 'boto3', type('M', (), {'client': boom}))
    assert digest.send('qc-r1', RESULTS, reviewed=5) is False


# ── Digest built from ground truth ─────────────────────────────────

RUN_ROWS = [
    {'guid': 'g2', 'title': 'Same Talk', 'comment': 're-post',
     'applied': {'duplicate_of': 'g1'}, 'restores_to': {}, 'removes': ['duplicate_of']},
    {'guid': 'g3', 'title': 'Norfolk Luncheon', 'comment': 'not DC metro',
     'applied': {'hidden': True}, 'restores_to': {}, 'removes': ['hidden']},
]


def test_digest_reports_writes_the_model_never_mentioned():
    # A pass can finish its writes and then fail to emit parseable JSON. Those
    # changes are live; the digest must still show them.
    results = main.digest_from_overlays(RUN_ROWS, model_results={})
    assert [r['guid'] for r in results['duplicates']] == ['g2']
    assert results['duplicates'][0]['canonical'] == 'g1'
    assert [r['guid'] for r in results['hidden']] == ['g3']


def test_digest_surfaces_overlay_fields_this_agent_should_not_write():
    # Only duplicate_of and hidden are in scope. Anything else appearing under
    # this run's id is live and unexpected, so it must not be dropped silently.
    rows = [{'guid': 'g9', 'title': 'Talk', 'comment': 'why',
             'applied': {'title': 'Rewritten', 'categories': ['ai']},
             'restores_to': {}, 'removes': []}]
    other = main.digest_from_overlays(rows)['other']
    assert other[0]['fields'] == {'categories': ['ai'], 'title': 'Rewritten'}


def test_digest_does_not_flag_in_scope_fields_as_unexpected():
    assert main.digest_from_overlays(RUN_ROWS)['other'] == []


def test_digest_keeps_model_reported_sections_with_no_overlay():
    # flagged / fetch_failures leave no trace to audit against.
    results = main.digest_from_overlays(
        [], {'flagged': [{'guid': 'g9', 'title': 'X', 'reason': 'unclear'}],
             'fetch_failures': [{'guid': 'g8', 'url': 'u', 'reason': 'timeout'}]})
    assert results['flagged'][0]['guid'] == 'g9'
    assert results['fetch_failures'][0]['guid'] == 'g8'


def test_digest_from_overlays_ignores_model_claims_about_writes():
    # A model claiming three hidden events when none were written must not
    # put them in the digest.
    results = main.digest_from_overlays(
        [], {'hidden': [{'guid': 'x'}, {'guid': 'y'}, {'guid': 'z'}]})
    assert results['hidden'] == []


# ── Tool-result unwrapping ─────────────────────────────────────────

def test_tool_result_unwraps_the_real_mcptoolresult_shape():
    # MCPToolResult is a dict subclass, not an object. Reading .content off it
    # returns the whole 3-key envelope, whose len() looks like a row count —
    # which is exactly how this went unnoticed the first time.
    result = {'content': [{'text': '[{"guid": "g1"}]'}],
              'status': 'success', 'toolUseId': 'x'}
    assert main._tool_result(result) == [{'guid': 'g1'}]


def test_tool_result_raises_on_an_error_status():
    # Must not degrade to "no events pending" — that reads as a quiet week.
    result = {'content': [{'text': 'ValidationException'}],
              'status': 'error', 'toolUseId': 'x'}
    with pytest.raises(RuntimeError, match='ValidationException'):
        main._tool_result(result)


def test_tool_result_raises_on_the_iserror_flag():
    result = {'content': [{'text': 'boom'}], 'status': 'success',
              'isError': True, 'toolUseId': 'x'}
    with pytest.raises(RuntimeError, match='boom'):
        main._tool_result(result)


def test_tool_result_reads_every_block_not_just_the_first():
    # FastMCP emits one block per list element. Reading only block 0 returned a
    # single event, whose 10 fields read as "10 events" against a real 199.
    result = {'content': [{'text': '{"guid": "g1"}'},
                          {'text': '{"guid": "g2"}'},
                          {'text': '{"guid": "g3"}'}],
              'status': 'success', 'toolUseId': 'x'}
    rows = main._tool_result(result, expect_list=True)
    assert [r['guid'] for r in rows] == ['g1', 'g2', 'g3']


def test_tool_result_keeps_a_one_element_list_a_list():
    # A queue of exactly one event must not collapse to a bare dict — len()
    # would then be its field count all over again.
    result = {'content': [{'text': '{"guid": "g1"}'}],
              'status': 'success', 'toolUseId': 'x'}
    assert main._tool_result(result, expect_list=True) == [{'guid': 'g1'}]


def test_tool_result_returns_a_scalar_tool_unwrapped():
    # set_overlay and friends return one object, not a list of one.
    result = {'content': [{'text': '{"guid": "g1", "review_status": "approved"}'}],
              'status': 'success', 'toolUseId': 'x'}
    assert main._tool_result(result) == {'guid': 'g1', 'review_status': 'approved'}


def test_tool_result_flattens_a_single_block_holding_a_whole_array():
    result = {'content': [{'text': '[{"guid": "g1"}, {"guid": "g2"}]'}],
              'status': 'success', 'toolUseId': 'x'}
    rows = main._tool_result(result, expect_list=True)
    assert [r['guid'] for r in rows] == ['g1', 'g2']


def test_tool_result_empty_queue_is_an_empty_list():
    result = {'content': [], 'status': 'success', 'toolUseId': 'x'}
    assert main._tool_result(result, expect_list=True) == []


def test_tool_result_unwraps_object_style_content_blocks():
    class Block:
        text = '[{"guid": "g1"}]'

    class Result:
        content = [Block()]

    assert main._tool_result(Result()) == [{'guid': 'g1'}]


def test_tool_result_passes_through_already_decoded_rows():
    assert main._tool_result([{'guid': 'g1'}], expect_list=True) == [{'guid': 'g1'}]


# ── AgentCore entrypoint mode ────────────────────────────────────────
# Regression guard: entryPoint is ["main.py"], so AgentCore runs this file
# with no arguments. It used to run a QC pass there instead of serving, so
# nothing ever answered /ping ("Runtime initialization time exceeded") and the
# pass ran off argparse defaults — dropping run_id and dry_run from the
# request payload, which made every invocation a write run.


def test_no_arguments_serves_rather_than_running_a_pass():
    args = main.build_arg_parser().parse_args([])
    assert args.local is False
    assert main.should_serve(args, app_available=True) is True


def test_local_flag_runs_a_single_pass():
    args = main.build_arg_parser().parse_args(['--local', '--dry-run'])
    assert main.should_serve(args, app_available=True) is False
    assert args.dry_run is True


def test_falls_back_to_a_single_pass_without_the_runtime_sdk():
    # Importing bedrock_agentcore fails on a workstation; `python main.py`
    # there must still do something useful rather than crash.
    args = main.build_arg_parser().parse_args([])
    assert main.should_serve(args, app_available=False) is False


def test_dry_run_defaults_off_so_serving_never_implies_a_write_mode():
    # run_qc's own default is the one that bit us; keep it visible.
    assert main.build_arg_parser().parse_args([]).dry_run is False
