"""Weekly calendar quality-control agent.

Runs on Bedrock AgentCore Runtime. Its tools are the site's own MCP server —
the same one the /edit UI and a human operator use — so a fix applied here is
byte-for-byte a fix applied there, and the two paths cannot drift apart.

One pass over each week's new events, looking for duplicates and out-of-area
listings — the two problems that warrant removing something from the site.

A second "polish" pass (venues, titles, categories on a cheaper model) was
built and dropped: across three dry runs and one live run it produced zero
overlays, while costing a model call, browser sessions, and about half the
runtime. The judgement it needed sat on the wrong side of the cost/quality
line. Its rules live in git history if it is ever worth revisiting on a
stronger model.

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
from prompt import TRIAGE_PROMPT

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
                'delete_recurring_event', 'approve_submission',
                'reject_submission', 'trust_submitter', 'untrust_submitter',
                'revert_qa_run'}

# The QC pass needs these and nothing else; the MCP server also carries group,
# submission, and category admin tools that are none of this agent's business.
_TRIAGE_TOOLS = {'list_pending_qa', 'get_events', 'get_event', 'get_overlay',
                 'set_overlay', 'resolve_qa_review'}

_DRY_RUN_NOTE = """

# DRY RUN

The tools that write are not available to you on this run. Do not try to call
them. Work through exactly the same judgement, and report in the same JSON
shape what you *would* have changed.
"""


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
    results = {'duplicates': [], 'hidden': [], 'other': []}

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

        # Anything else this run wrote. Nothing should land here today, so if
        # it does, the digest says so rather than quietly dropping a live
        # change nobody asked for.
        unexpected = sorted(set(applied) - {'duplicate_of', 'hidden'})
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


def _build_agent(model_id, system_prompt, tools, dry_run, with_browser=False):
    from strands import Agent
    from strands.models import BedrockModel

    if with_browser:
        from strands_tools.browser import AgentCoreBrowser
        tools = list(tools) + [AgentCoreBrowser(region=REGION).browser]

    return Agent(
        model=BedrockModel(model_id=model_id, region_name=REGION,
                           max_tokens=MAX_TOKENS),
        system_prompt=system_prompt + (_DRY_RUN_NOTE if dry_run else ''),
        tools=tools,
    )


def run_qc(dry_run=False, limit=None, run_id=None):
    """One full QC pass. Returns the results dict the digest renders."""
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

        triage = _build_agent(TRIAGE_MODEL, TRIAGE_PROMPT,
                              _select(tools, _TRIAGE_TOOLS, dry_run), dry_run,
                              with_browser=True)
        triage_out = triage(
            f"Run id: {run_id}\n"
            f"Review these {len(guids)} queued events for duplicates and "
            f"out-of-area listings: {', '.join(guids)}\n"
            f"Load the comparison corpus with get_events(date_from='{today}') "
            f"— most duplicate pairs have their canonical half already approved "
            f"and outside the queue."
        )
        results.update(_extract_json(triage_out))
        reviewed = results.get('reviewed', len(guids))

        if not dry_run:
            # Re-derive the change sections from what was actually written,
            # not from what the models said they wrote.
            written = _tool_result(mcp.call_tool_sync(
                f'{run_id}-audit', 'list_qa_run', {'run_id': run_id}),
                expect_list=True)
            results = {**digest_from_overlays(written, results),
                       'run_id': run_id, 'dry_run': False}

        results['reviewed'] = reviewed

        changed = sum(len(results.get(key) or [])
                      for key, _, _ in digest._SECTIONS
                      if key != 'fetch_failures')
        if changed and not dry_run:
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


try:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()

    @app.entrypoint
    def invoke(payload):
        """AgentCore entrypoint.

        Registered as an async task for its whole duration: a QC pass runs for
        many minutes, and without this the runtime's health ping treats the
        silence as a hung agent and kills it around the 15-minute mark.
        """
        task_id = app.add_async_task('calendar_qc')
        try:
            return run_qc(dry_run=bool(payload.get('dry_run')),
                          limit=payload.get('limit'),
                          run_id=payload.get('run_id'))
        finally:
            app.complete_async_task(task_id)

except ImportError:  # local dry runs don't need the runtime SDK
    app = None


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='withhold every write tool and print the digest')
    parser.add_argument('--limit', type=int, default=None,
                        help='cap the number of queued events reviewed')
    args = parser.parse_args()
    print(json.dumps(run_qc(dry_run=args.dry_run, limit=args.limit), indent=2))
