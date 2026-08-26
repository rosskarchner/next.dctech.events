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
    'set_overlay', 'resolve_qa_review', 'list_categories', 'trigger_rebuild',
    'add_category', 'delete_single_event', 'approve_submission',
    'revert_qa_run',
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


def test_corrections_get_their_own_section_not_the_unexpected_bucket():
    # title / location / categories are the polish pass's output, so they are
    # reported as corrections rather than as things nobody asked for.
    rows = [{'guid': 'g9', 'title': 'Talk', 'comment': 'page says otherwise',
             'applied': {'title': 'Rewritten', 'categories': ['ai']},
             'restores_to': {}, 'removes': []}]
    results = main.digest_from_overlays(rows)
    assert results['other'] == []
    assert results['polished'][0]['fields'] == \
        {'categories': ['ai'], 'title': 'Rewritten'}
    assert results['polished'][0]['reason'] == 'page says otherwise'


def test_a_removal_and_a_correction_on_one_event_are_reported_separately():
    rows = [{'guid': 'g9', 'title': 'Talk', 'comment': 'both',
             'applied': {'hidden': True, 'location': 'Boston, MA'},
             'restores_to': {}, 'removes': []}]
    results = main.digest_from_overlays(rows)
    assert [r['guid'] for r in results['hidden']] == ['g9']
    assert results['polished'][0]['fields'] == {'location': 'Boston, MA'}


def test_digest_still_surfaces_a_field_outside_both_passes():
    # The bucket exists so a field neither pass is supposed to write cannot
    # land on the live site unmentioned.
    rows = [{'guid': 'g9', 'title': 'Talk', 'comment': 'why',
             'applied': {'group_website': 'http://example.com'},
             'restores_to': {}, 'removes': []}]
    other = main.digest_from_overlays(rows)['other']
    assert other[0]['fields'] == {'group_website': 'http://example.com'}


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


# ── Step Functions task token ────────────────────────────────────────
# The Monday state machine waits on the QC pass finishing. The trigger Lambda
# cannot release the token — it returns seconds after the runtime accepts the
# invocation — so the agent releases it itself.


class FakeSfn:
    def __init__(self, fail_on=None):
        self.successes, self.failures = [], []
        self.fail_on = fail_on

    def send_task_success(self, **kwargs):
        if self.fail_on == 'success':
            raise RuntimeError('token expired')
        self.successes.append(kwargs)

    def send_task_failure(self, **kwargs):
        if self.fail_on == 'failure':
            raise RuntimeError('token expired')
        self.failures.append(kwargs)


@pytest.fixture
def fake_sfn(monkeypatch):
    client = FakeSfn()
    fake_boto3 = type('boto3', (), {'client': staticmethod(lambda *a, **k: client)})
    monkeypatch.setitem(sys.modules, 'boto3', fake_boto3)
    return client


RESULTS = {
    'run_id': 'qc-2026-08-31-abcd1234',
    'reviewed': 28,
    'duplicates': [{'guid': 'a', 'title': 'A', 'reason': 'dup'}],
    'hidden': [{'guid': 'b', 'title': 'B', 'reason': 'NYC'}],
    'flagged': [],
}


def test_no_token_means_no_call_at_all(fake_sfn):
    # A hand-run pass has no waiter; reporting to one would fail.
    main.report_to_step_functions(None, results=RESULTS)
    assert fake_sfn.successes == [] and fake_sfn.failures == []


def test_success_releases_the_token(fake_sfn):
    main.report_to_step_functions('tok-1', results=RESULTS)
    assert len(fake_sfn.successes) == 1
    assert fake_sfn.successes[0]['taskToken'] == 'tok-1'


def test_success_output_is_counts_not_content(fake_sfn):
    # SendTaskSuccess caps output at 256KB, and a state machine has no use for
    # titles and reasons — the digest carries those.
    import json
    main.report_to_step_functions('tok-1', results=RESULTS)
    out = json.loads(fake_sfn.successes[0]['output'])
    assert out == {'run_id': 'qc-2026-08-31-abcd1234', 'dry_run': False,
                   'reviewed': 28, 'duplicates': 1, 'hidden': 1, 'flagged': 0}


def test_summary_of_a_clean_run(fake_sfn):
    import json
    main.report_to_step_functions('tok-1', results={'run_id': 'r', 'reviewed': 0})
    out = json.loads(fake_sfn.successes[0]['output'])
    assert (out['reviewed'], out['duplicates'], out['hidden']) == (0, 0, 0)


def test_failure_releases_the_token_too(fake_sfn):
    # Without this the execution sits on the token for the full hour, turning
    # a crash into a silent stall.
    main.report_to_step_functions('tok-1', error=ValueError('feed exploded'),
                                  run_id='qc-1')
    assert len(fake_sfn.failures) == 1
    assert fake_sfn.failures[0]['error'] == 'ValueError'
    assert 'feed exploded' in fake_sfn.failures[0]['cause']
    assert 'qc-1' in fake_sfn.failures[0]['cause']


def test_a_reporting_error_never_sinks_a_completed_pass(monkeypatch):
    # The overlays are written and the digest is sent by this point. Raising
    # here would turn a good run into a failed one for nothing.
    client = FakeSfn(fail_on='success')
    monkeypatch.setitem(sys.modules, 'boto3',
                        type('boto3', (), {'client': staticmethod(lambda *a, **k: client)}))
    main.report_to_step_functions('tok-1', results=RESULTS)  # must not raise


def test_a_reporting_error_on_the_failure_path_is_also_swallowed(monkeypatch):
    client = FakeSfn(fail_on='failure')
    monkeypatch.setitem(sys.modules, 'boto3',
                        type('boto3', (), {'client': staticmethod(lambda *a, **k: client)}))
    main.report_to_step_functions('tok-1', error=RuntimeError('boom'))


def test_cause_and_error_are_clipped_to_the_api_limits(fake_sfn):
    main.report_to_step_functions('tok-1', error=ValueError('x' * 50000),
                                  run_id='qc-1')
    sent = fake_sfn.failures[0]
    assert len(sent['error']) <= 256
    assert len(sent['cause']) <= 32768


# ── two passes, one queue ────────────────────────────────────────────
# Polish runs on what triage left alone: correcting the title of an event that
# has just been hidden is work nobody will ever see.


def test_removed_guids_reads_the_written_rows_not_the_model():
    rows = [
        {'guid': 'g1', 'applied': {'duplicate_of': 'gX'}},
        {'guid': 'g2', 'applied': {'hidden': True}},
        {'guid': 'g3', 'applied': {'title': 'Corrected'}},
    ]
    # A model claiming g4 was hidden does not make it so.
    claims = {'hidden': [{'guid': 'g4'}]}
    assert main._removed_guids(rows, claims) == {'g1', 'g2'}


def test_a_corrected_event_is_not_treated_as_removed():
    rows = [{'guid': 'g3', 'applied': {'title': 'Corrected',
                                       'location': 'Arlington, VA'}}]
    assert main._removed_guids(rows, {}) == set()


def test_removed_guids_falls_back_to_model_claims_in_a_dry_run():
    # A dry run writes nothing, so list_qa_run has nothing to audit and the
    # model's own report is all there is.
    claims = {'duplicates': [{'guid': 'g1'}], 'hidden': [{'guid': 'g2'}]}
    assert main._removed_guids(None, claims) == {'g1', 'g2'}


def test_removed_guids_survives_a_claim_with_no_guid():
    assert main._removed_guids(None, {'hidden': [{'title': 'no guid'}]}) == set()


def test_removed_guids_on_an_empty_run():
    assert main._removed_guids([], {}) == set()


def test_both_passes_contribute_to_flagged_and_fetch_failures():
    # Neither section leaves an overlay behind, so they concatenate rather
    # than the second pass overwriting the first.
    triage = {'flagged': [{'guid': 'g1'}], 'fetch_failures': [{'guid': 'g2'}]}
    polish = {'flagged': [{'guid': 'g3'}], 'fetch_failures': [{'guid': 'g4'}]}
    merged = main._merge_claims(triage, polish)
    assert [r['guid'] for r in merged['flagged']] == ['g1', 'g3']
    assert [r['guid'] for r in merged['fetch_failures']] == ['g2', 'g4']


def test_merge_claims_omits_sections_neither_pass_reported():
    assert main._merge_claims({}, {}) == {}
    assert main._merge_claims(None, None) == {}


def test_merge_claims_does_not_carry_applied_change_sections():
    # duplicates / hidden / polished are re-derived from list_qa_run, never
    # taken from a model's narrative.
    merged = main._merge_claims({'duplicates': [{'guid': 'g1'}],
                                 'polished': [{'guid': 'g2'}]}, {})
    assert merged == {}


def test_polish_gets_no_tool_that_removes_or_resolves():
    chosen = {main._tool_name(t)
              for t in main._select(ALL_TOOLS, main._POLISH_TOOLS, dry_run=False)}
    assert 'resolve_qa_review' not in chosen
    assert 'list_pending_qa' not in chosen
    assert 'get_events' not in chosen


def test_polish_can_read_the_category_list():
    # A slug that does not exist renders as nothing, so the list is not
    # optional.
    chosen = {main._tool_name(t)
              for t in main._select(ALL_TOOLS, main._POLISH_TOOLS, dry_run=False)}
    assert 'list_categories' in chosen


def test_polish_dry_run_cannot_write_either():
    chosen = {main._tool_name(t)
              for t in main._select(ALL_TOOLS, main._POLISH_TOOLS, dry_run=True)}
    assert 'set_overlay' not in chosen
    assert 'list_categories' in chosen


def test_the_fields_the_digest_routes_match_the_fields_the_prompt_offers():
    # If these drift, a correction the agent is told to make lands in the
    # digest's "nobody asked for this" bucket.
    import prompt
    for field in main._POLISH_FIELDS:
        assert f'`{field}`' in prompt.POLISH_PROMPT


def test_the_polish_prompt_never_offers_a_removal_field():
    import prompt
    scope = prompt.POLISH_PROMPT.split('# Your task')[0]
    assert 'duplicate_of' not in scope
    assert '`hidden`' not in scope


def test_the_polish_prompt_demands_provenance_in_comments():
    # A trim invents nothing; a searched value is the riskiest edit there is.
    # A comment that does not distinguish them makes a reviewer re-fetch the
    # page to tell, which is the whole thing the comment exists to avoid.
    import prompt
    assert '# Comments' in prompt.POLISH_PROMPT
    for phrase in ('from the feed', 'read from', 'resolved by search'):
        assert phrase in prompt.POLISH_PROMPT
