"""Weekly calendar quality-control agent.

Runs on Bedrock AgentCore Runtime. Its tools are the site's own MCP server —
the same one the /edit UI and a human operator use — so a fix applied here is
byte-for-byte a fix applied there, and the two paths cannot drift apart.

Two passes over each week's new events, in order:

1. **Triage** — duplicates and out-of-area listings, the two problems that
   warrant removing something from the site. Reluctant by design.
2. **Polish** — titles, locations and categories that do not match the event's
   own page, on whatever pass 1 did not remove. A correction leaves the event
   on the calendar, so the bar is lower.

They are separate agent calls with separate prompts because those two bars are
not the same instinct, and one prompt cannot hold both without blunting one.

The polish pass is a revival. It was built once on a *cheaper* model with only
the browser to read pages, and dropped after producing zero overlays across
three dry runs and one live run — the judgement it needed sat on the wrong side
of the cost/quality line. It now runs on the same model as triage and reads
pages with `tavily_extract`, so it compares against the canonical description
instead of inferring from a title. If it still produces nothing, cut it again;
`--dry-run` is how to tell.

Every write is stamped with a run id, so a whole run can be undone in one call
(`revert_qa_run`) if it gets something wrong. The digest email carries that id.
"""
import argparse
import json
import os
import re
import uuid
from datetime import date

import digest
from mcp_sigv4 import mcp_transport
from prompt import POLISH_PROMPT, TRIAGE_PROMPT

# Hiding an event is the one thing this agent does that a reader would notice
# if it got it wrong, so it runs on the stronger model. Overridable so the tier
# can be changed from the stack without a code deploy.
TRIAGE_MODEL = os.environ.get('QC_TRIAGE_MODEL', 'us.anthropic.claude-sonnet-5')
REGION = os.environ.get('AWS_REGION', 'us-east-1')

# Bedrock's per-response default is small enough that a pass over ~20 events
# is cut off mid-tool-call — the run then dies with MaxTokensReachedException
# after doing most of the work. Set it explicitly and generously.
MAX_TOKENS = int(os.environ.get('QC_MAX_TOKENS', '16000'))

# Tools that change something. Withheld entirely in dry-run mode — the agent
# can then physically not write, which is a stronger guarantee than asking it
# not to.
_WRITE_TOOLS = {'set_overlay', 'resolve_qa_review', 'trigger_rebuild',
                'add_single_event', 'update_single_event', 'delete_single_event',
                'add_group', 'set_group_active', 'add_category',
                'add_recurring_event', 'update_recurring_event',
                'delete_recurring_event', 'submit_event', 'approve_submission',
                'reject_submission', 'trust_submitter', 'untrust_submitter',
                'revert_qa_run'}

# The QC pass needs these and nothing else; the MCP server also carries group,
# submission, and category admin tools that are none of this agent's business.
_TRIAGE_TOOLS = {'list_pending_qa', 'get_events', 'get_event', 'get_overlay',
                 'set_overlay', 'resolve_qa_review'}

# The polish pass writes corrections, never removals. Deliberately without
# `list_pending_qa` and `resolve_qa_review` (triage owns the queue), without
# `get_events` (it judges each entry against its own page, not against the
# corpus), and *with* `list_categories`, because a category slug that does not
# exist renders as nothing at all.
_POLISH_TOOLS = {'get_event', 'get_overlay', 'set_overlay', 'list_categories'}

# Written by the polish pass. Kept next to the tool set so the digest and the
# prompt cannot drift apart on what this agent is allowed to correct.
_POLISH_FIELDS = ('title', 'location', 'categories')

_DRY_RUN_NOTE = """

# DRY RUN

The tools that write are not available to you on this run. Do not try to call
them. Work through exactly the same judgement, and report in the same JSON
shape what you *would* have changed.
"""


def _load_search_key():
    """Put the Tavily key where strands_tools.tavily looks for it.

    Same secret the discovery agent uses, referenced by name rather than
    duplicated: it is one Tavily account, and a second key would be a second
    thing to rotate. The `discovery/` in the path is historical.
    """
    arn = os.environ.get('SEARCH_SECRET_ARN')
    if not arn or os.environ.get('TAVILY_API_KEY'):
        return
    import boto3

    value = boto3.client('secretsmanager').get_secret_value(SecretId=arn)
    os.environ['TAVILY_API_KEY'] = value['SecretString'].strip()


def _tool_name(tool):
    """MCP tool objects expose their name differently across strands versions."""
    for attr in ('tool_name', 'name'):
        value = getattr(tool, attr, None)
        if isinstance(value, str):
            return value
    spec = getattr(tool, 'tool_spec', None) or {}
    return spec.get('name', '')


def _select(tools, allowed, dry_run):
    chosen = [t for t in tools if _tool_name(t) in allowed]
    if dry_run:
        chosen = [t for t in chosen if _tool_name(t) not in _WRITE_TOOLS]
    return chosen


def _extract_json(text):
    """Pull the result object out of an agent's final message.

    Models wrap JSON in prose or fences often enough that insisting on a clean
    parse would fail runs that actually did the work. A digest that misses a
    section is recoverable; discarding a completed pass is not.
    """
    text = str(text or '')
    fenced = re.findall(r'```(?:json)?\s*(.*?)```', text, re.S)
    for candidate in reversed(fenced):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    # Fall back to the last balanced-looking object in the message.
    for match in reversed(list(re.finditer(r'\{.*\}', text, re.S))):
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
    print(f'could not parse agent output as JSON: {text[:400]}')
    return {}


def digest_from_overlays(rows, model_results=None):
    """Build the digest's change sections from the overlays actually written.

    The model also reports what it did, but that report is a narrative and can
    be wrong in the direction that matters. An agent has been observed here
    finishing its writes and then failing to emit parseable JSON, which would
    have left real production changes out of the digest entirely.
    `list_qa_run` is the authoritative record, so every section describing an
    applied change comes from there.

    Only `flagged` and `fetch_failures` still come from the model — neither
    leaves an overlay behind, so there is nothing authoritative to read.
    """
    results = {'duplicates': [], 'hidden': [], 'polished': [], 'other': []}

    for row in rows or []:
        applied = row.get('applied') or {}
        guid, title = row.get('guid'), row.get('title', '')
        reason = row.get('comment', '')

        if 'duplicate_of' in applied:
            results['duplicates'].append(
                {'guid': guid, 'title': title,
                 'canonical': applied['duplicate_of'], 'reason': reason})
        if applied.get('hidden'):
            results['hidden'].append(
                {'guid': guid, 'title': title, 'reason': reason})

        corrected = {k: applied[k] for k in _POLISH_FIELDS if k in applied}
        if corrected:
            results['polished'].append(
                {'guid': guid, 'title': title, 'fields': corrected,
                 'reason': reason})

        # Anything else this run wrote. Nothing should land here today, so if
        # it does, the digest says so rather than quietly dropping a live
        # change nobody asked for.
        unexpected = sorted(
            set(applied) - {'duplicate_of', 'hidden'} - set(_POLISH_FIELDS))
        if unexpected:
            results['other'].append(
                {'guid': guid, 'title': title,
                 'fields': {k: applied[k] for k in unexpected},
                 'reason': reason})

    for key in ('flagged', 'fetch_failures'):
        value = (model_results or {}).get(key)
        if value:
            results[key] = value
    return results


def _merge_claims(*reports):
    """Combine what each pass said about things that leave no overlay behind.

    `flagged` and `fetch_failures` are the only sections not re-derived from
    `list_qa_run`, and both passes can contribute to them, so they concatenate
    rather than overwrite. `reviewed` is the triage count — the queue is what
    was reviewed, and polish sees a subset of it.
    """
    merged = {}
    for report in reports:
        for key in ('flagged', 'fetch_failures'):
            rows = (report or {}).get(key) or []
            if rows:
                merged.setdefault(key, []).extend(rows)
    return merged


def _removed_guids(rows, claims):
    """Events the triage pass took off the site, so polish can skip them.

    Prefers `list_qa_run` rows, which are what was actually written. Falls
    back to the model's own report, which is all a dry run has.
    """
    if rows is not None:
        return {row.get('guid') for row in rows
                if 'duplicate_of' in (row.get('applied') or {})
                or (row.get('applied') or {}).get('hidden')}
    claimed = ((claims or {}).get('duplicates') or []) + \
              ((claims or {}).get('hidden') or [])
    return {row.get('guid') for row in claimed if row.get('guid')}


def _build_agent(model_id, system_prompt, tools, dry_run, with_browser=False,
                 with_search=False):
    from strands import Agent
    from strands.models import BedrockModel

    if with_browser:
        from strands_tools.browser import AgentCoreBrowser
        tools = list(tools) + [AgentCoreBrowser(region=REGION).browser]
    if with_search:
        # Both are wanted: extract is the cheap first try on an event page,
        # search is the only way to resolve a venue the page merely names.
        from strands_tools.tavily import tavily_extract, tavily_search
        tools = list(tools) + [tavily_extract, tavily_search]

    return Agent(
        model=BedrockModel(model_id=model_id, region_name=REGION,
                           max_tokens=MAX_TOKENS),
        system_prompt=system_prompt + (_DRY_RUN_NOTE if dry_run else ''),
        tools=tools,
    )


def run_qc(dry_run=False, limit=None, run_id=None, own_rebuild=True):
    """One full QC pass. Returns the results dict the digest renders.

    `own_rebuild=False` withholds the rebuild this function would otherwise
    kick off at the end. Set it when a caller is sequencing the build itself —
    the Monday state machine runs a `codebuild:startBuild.sync` step straight
    after this one, and two concurrent builds of the same project would have
    two `s3 sync --delete` runs racing over the same bucket.
    """
    from strands.tools.mcp import MCPClient

    run_id = run_id or f"qc-{date.today().isoformat()}-{uuid.uuid4().hex[:8]}"
    today = date.today().isoformat()
    results = {'run_id': run_id, 'dry_run': dry_run}

    with MCPClient(mcp_transport()) as mcp:
        tools = mcp.list_tools_sync()

        # call_tool_sync(tool_use_id, name, arguments) — the leading id is a
        # correlation handle, not the tool name.
        pending = _tool_result(mcp.call_tool_sync(
            f'{run_id}-queue', 'list_pending_qa', {'limit': limit or 200}),
            expect_list=True)
        print(f'run {run_id}: {len(pending)} events pending QC (dry_run={dry_run})')
        if not pending:
            return {**results, 'reviewed': 0}

        guids = [e['guid'] for e in pending]

        def audit(tag):
            """What this run has actually written so far, per the table."""
            if dry_run:
                return None
            return _tool_result(mcp.call_tool_sync(
                f'{run_id}-{tag}', 'list_qa_run', {'run_id': run_id}),
                expect_list=True)

        # ── pass 1: what should come off the site ────────────────────
        triage = _build_agent(TRIAGE_MODEL, TRIAGE_PROMPT,
                              _select(tools, _TRIAGE_TOOLS, dry_run), dry_run,
                              with_browser=True, with_search=True)
        triage_out = triage(
            f"Run id: {run_id}\n"
            f"Review these {len(guids)} queued events for duplicates and "
            f"out-of-area listings: {', '.join(guids)}\n"
            f"Load the comparison corpus with get_events(date_from='{today}') "
            f"— most duplicate pairs have their canonical half already approved "
            f"and outside the queue."
        )
        triage_claims = _extract_json(triage_out)
        reviewed = triage_claims.get('reviewed', len(guids))

        # ── pass 2: what should be corrected ─────────────────────────
        # Only on what survived pass 1. Polishing the title of an event that
        # has just been hidden is work nobody will ever see.
        removed = _removed_guids(audit('triage-audit'), triage_claims)
        survivors = [g for g in guids if g not in removed]
        polish_claims = {}
        if survivors:
            print(f'run {run_id}: polishing {len(survivors)} of {len(guids)} '
                  f'({len(removed)} removed by triage)')
            polish = _build_agent(TRIAGE_MODEL, POLISH_PROMPT,
                                  _select(tools, _POLISH_TOOLS, dry_run),
                                  dry_run, with_browser=True, with_search=True)
            polish_out = polish(
                f"Run id: {run_id}\n"
                f"Check these {len(survivors)} events against their own event "
                f"pages and correct what is wrong: {', '.join(survivors)}"
            )
            polish_claims = _extract_json(polish_out)

        claims = {**triage_claims, **_merge_claims(triage_claims, polish_claims)}
        if dry_run:
            results.update(claims)
            results['polished'] = polish_claims.get('polished') or []
        else:
            # Re-derive every applied-change section from what was actually
            # written, not from what the models said they wrote. The audit runs
            # after both passes so it sees corrections as well as removals.
            results = {**digest_from_overlays(audit('audit'), claims),
                       'run_id': run_id, 'dry_run': False}

        results['reviewed'] = reviewed

        changed = sum(len(results.get(key) or [])
                      for key, _, _ in digest._SECTIONS
                      if key != 'fetch_failures')
        if changed and not dry_run and own_rebuild:
            mcp.call_tool_sync(f'{run_id}-rebuild', 'trigger_rebuild', {})

    if not dry_run:
        digest.send(run_id, results, reviewed=results.get('reviewed', 0))
    else:
        subject, text, _ = digest.render(run_id, results,
                                         reviewed=results.get('reviewed', 0))
        print(f'\n--- digest (not sent) ---\n{subject}\n\n{text}')

    return results


def _block_text(block):
    """Text out of one content block, which may be a dict or an object."""
    if isinstance(block, dict):
        return block.get('text')
    return getattr(block, 'text', None)


def _tool_result(result, expect_list=False):
    """Unwrap an MCP tool result into plain Python.

    Two traps here, both of which produce a plausible-looking number rather
    than an error:

    strands' MCPToolResult is a *dict* subclass ({content, status,
    toolUseId, isError}), not an object with attributes — reading `.content`
    off it returns the whole envelope, whose len() is 4.

    FastMCP serialises a list return value as *one content block per element*,
    so a 199-event response arrives as 199 blocks. Reading only the first
    block yields a single event dict, and len() of that is its field count.

    `expect_list` says which shape the tool returns, because a one-element
    list and a scalar are indistinguishable on the wire — without it, a queue
    holding exactly one event would parse as a bare dict and re-create the
    same class of bug at N=1.
    """
    if isinstance(result, dict) and 'status' in result and 'content' in result:
        if result.get('status') == 'error' or result.get('isError'):
            detail = ' '.join(str(_block_text(b) or '') for b in result['content'])
            raise RuntimeError(f'MCP tool call failed: {detail.strip()[:500]}')
        result = result['content']
    elif not isinstance(result, (list, str)) and hasattr(result, 'content'):
        result = result.content

    if isinstance(result, str):
        result = [{'text': result}]
    if not isinstance(result, list):
        return result

    values = []
    for block in result:
        text = _block_text(block)
        if text:
            values.append(json.loads(text))
        elif isinstance(block, dict) and 'text' not in block:
            values.append(block)  # already-decoded row

    if expect_list:
        rows = []
        for value in values:
            rows.extend(value) if isinstance(value, list) else rows.append(value)
        return rows
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def _token_summary(results):
    """The compact shape handed back to Step Functions.

    Counts, not content. The full digest is emailed and the overlays are on
    the events themselves; SendTaskSuccess caps output at 256KB, and a state
    machine has no use for reasons and titles anyway.
    """
    return {
        'run_id': results.get('run_id'),
        'dry_run': bool(results.get('dry_run')),
        'reviewed': results.get('reviewed', 0),
        'duplicates': len(results.get('duplicates') or []),
        'hidden': len(results.get('hidden') or []),
        'flagged': len(results.get('flagged') or []),
    }


def report_to_step_functions(token, *, results=None, error=None, run_id=None):
    """Release a Step Functions task token, if this run was started by one.

    Never raises. A failure to report is worth logging loudly, but the pass
    itself has already written its overlays and sent its digest — letting a
    SendTaskSuccess error propagate would turn a completed run into a failed
    one, and the execution has a timeout that covers being left waiting.
    """
    if not token:
        return
    try:
        import boto3
        sfn = boto3.client('stepfunctions', region_name=REGION)
        if error is not None:
            sfn.send_task_failure(
                taskToken=token,
                error=type(error).__name__[:256],
                cause=f'run {run_id}: {error}'[:32768],
            )
            print(f'reported QC failure to Step Functions: {error}')
        else:
            sfn.send_task_success(
                taskToken=token,
                output=json.dumps(_token_summary(results or {})),
            )
            print('reported QC completion to Step Functions')
    except Exception as exc:  # noqa: BLE001
        print(f'ERROR could not release Step Functions task token: {exc}')


try:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()

    @app.entrypoint
    def invoke(payload):
        """AgentCore entrypoint.

        Registered as an async task for its whole duration: a QC pass runs for
        many minutes, and without this the runtime's health ping treats the
        silence as a hung agent and kills it around the 15-minute mark.

        A `task_token` in the payload means a Step Functions execution is
        waiting on this pass — see `report_to_step_functions`. Its presence
        also means the caller owns the site rebuild, so this run does not
        start one.
        """
        task_id = app.add_async_task('calendar_qc')
        token = payload.get('task_token')
        try:
            results = run_qc(dry_run=bool(payload.get('dry_run')),
                             limit=payload.get('limit'),
                             run_id=payload.get('run_id'),
                             own_rebuild=not token)
        except Exception as exc:  # noqa: BLE001 — the waiter has to be told
            # Without this the execution sits on the token until its hour-long
            # timeout, turning a crash into a silent stall.
            report_to_step_functions(token, error=exc,
                                     run_id=payload.get('run_id'))
            raise
        finally:
            app.complete_async_task(task_id)

        report_to_step_functions(token, results=results)
        return results

except ImportError:  # local dry runs don't need the runtime SDK
    app = None


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local', action='store_true',
                        help='run a single pass in-process instead of serving')
    parser.add_argument('--dry-run', action='store_true',
                        help='withhold every write tool and print the digest')
    parser.add_argument('--limit', type=int, default=None,
                        help='cap the number of queued events reviewed')
    return parser


def should_serve(args, app_available):
    """Whether to start the HTTP server rather than run one pass.

    AgentCore's entryPoint is ["main.py"], so it executes this file with no
    arguments — serving therefore has to be the no-argument default.
    """
    return app_available and not args.local


if __name__ == '__main__':
    args = build_arg_parser().parse_args()

    if not should_serve(args, app is not None):
        print(json.dumps(run_qc(dry_run=args.dry_run, limit=args.limit),
                         indent=2))
    else:
        # Serving has to be the no-argument default. AgentCore's entryPoint is
        # ["main.py"], so it executes this file as a script with no arguments
        # and then polls /ping. When this block ran a QC pass instead, nothing
        # ever listened: every invocation died with "Runtime initialization
        # time exceeded" while a pass ran anyway, off the CLI defaults rather
        # than the request payload — so run_id and dry_run were silently
        # dropped and every run wrote. Keep the bare `python main.py` path
        # serving; use --local for a one-shot run on a workstation.
        app.run()
