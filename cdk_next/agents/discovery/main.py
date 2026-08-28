"""Weekly event discovery agent."""
import argparse
import json
import os
import re
import uuid
from datetime import date, timedelta

import digest
from mcp_sigv4 import mcp_transport
from prompt import DISCOVERY_PROMPT
from sources import SOURCES

MODEL = os.environ.get('DISCOVERY_MODEL', 'us.anthropic.claude-sonnet-5')
REGION = os.environ.get('AWS_REGION', 'us-east-1')
MAX_TOKENS = int(os.environ.get('DISCOVERY_MAX_TOKENS', '16000'))

# A normal run makes ~30 tool calls. This is a backstop, not a tuning knob —
# on 2026-08-24 a strands_tools browser concurrency bug (overlapping
# `_async_navigate` tasks) threw mid-run, the agent kept retrying the tool,
# and each retry resent the whole (growing) transcript: one session alone
# reached 65 tool calls and 824 calls that day burned 102M input tokens.
MAX_TOOL_CALLS = int(os.environ.get('DISCOVERY_MAX_TOOL_CALLS', '40'))

_WRITE_TOOLS = {'propose_group', 'propose_event', 'set_overlay',
                'resolve_qa_review', 'trigger_rebuild', 'add_single_event',
                'update_single_event', 'delete_single_event', 'add_group',
                'set_group_active', 'add_category', 'add_recurring_event',
                'update_recurring_event', 'delete_recurring_event',
                'approve_submission', 'reject_submission', 'trust_submitter',
                'untrust_submitter', 'revert_qa_run'}

_DISCOVERY_TOOLS = {'list_groups', 'list_categories', 'get_events',
                    'verify_ical_feed', 'list_discovery_proposals',
                    'propose_group', 'propose_event'}

_DRY_RUN_NOTE = """

# DRY RUN

The tools that write are not available to you on this run. Do not try to call
them. Do exactly the same research and judgement, and report in the same JSON
shape what you *would* have proposed.
"""


def _tool_name(tool):
    for attr in ('tool_name', 'name'):
        value = getattr(tool, attr, None)
        if isinstance(value, str):
            return value
    return (getattr(tool, 'tool_spec', None) or {}).get('name', '')


def _select(tools, allowed, dry_run):
    chosen = [t for t in tools if _tool_name(t) in allowed]
    if dry_run:
        chosen = [t for t in chosen if _tool_name(t) not in _WRITE_TOOLS]
    return chosen


def _extract_json(text):
    text = str(text or '')
    for candidate in reversed(re.findall(r'```(?:json)?\s*(.*?)```', text, re.S)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    for match in reversed(list(re.finditer(r'\{.*\}', text, re.S))):
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
    print(f'could not parse agent output as JSON: {text[:400]}')
    return {}


def _block_text(block):
    if isinstance(block, dict):
        return block.get('text')
    return getattr(block, 'text', None)


def _tool_result(result, expect_list=False):
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
            values.append(block)

    if expect_list:
        rows = []
        for value in values:
            rows.extend(value) if isinstance(value, list) else rows.append(value)
        return rows
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def _load_search_key():
    arn = os.environ.get('SEARCH_SECRET_ARN')
    if not arn or os.environ.get('TAVILY_API_KEY'):
        return
    import boto3

    value = boto3.client('secretsmanager').get_secret_value(SecretId=arn)
    os.environ['TAVILY_API_KEY'] = value['SecretString'].strip()


def _build_agent(system_prompt, tools, dry_run):
    from strands import Agent
    from strands.hooks.events import BeforeToolCallEvent
    from strands.models import BedrockModel
    from strands_tools.browser import AgentCoreBrowser
    from strands_tools.tavily import tavily_search, tavily_extract

    class _ToolCallBudget:
        """Cancels tool calls once a run has made too many of them."""

        def __init__(self, max_calls):
            self.max_calls = max_calls
            self.count = 0

        def __call__(self, event: BeforeToolCallEvent):
            self.count += 1
            if self.count > self.max_calls:
                event.cancel_tool = (
                    f'Tool call budget ({self.max_calls}) exceeded for this run. '
                    'Stop calling tools and report your findings so far as final JSON.'
                )

    tools = list(tools) + [AgentCoreBrowser(region=REGION).browser,
                           tavily_search, tavily_extract]
    return Agent(
        model=BedrockModel(model_id=MODEL, region_name=REGION,
                           max_tokens=MAX_TOKENS),
        system_prompt=system_prompt + (_DRY_RUN_NOTE if dry_run else ''),
        tools=tools,
        hooks=[_ToolCallBudget(MAX_TOOL_CALLS)],
    )


def _proposals_this_run(mcp, since):
    rows = _tool_result(mcp.call_tool_sync(
        'discovery-audit', 'list_discovery_proposals', {}), expect_list=True)
    return [r for r in rows
            if (r.get('created_at') or '').startswith(since.isoformat())]


def run_discovery(dry_run=False, run_id=None, horizon_days=90):
    from strands.tools.mcp import MCPClient

    _load_search_key()

    run_id = run_id or f"disc-{date.today().isoformat()}-{uuid.uuid4().hex[:8]}"
    today = date.today()
    results = {'run_id': run_id, 'dry_run': dry_run}

    with MCPClient(mcp_transport()) as mcp:
        tools = mcp.list_tools_sync()
        agent = _build_agent(DISCOVERY_PROMPT,
                             _select(tools, _DISCOVERY_TOOLS, dry_run), dry_run)

        source_list = '\n'.join(
            f"- {s['url']}  [{s['kind']}] {s['note']}" for s in SOURCES)

        out = agent(
            f"Run id: {run_id}. Today is {today.isoformat()}.\n\n"
            f"Consider events between now and "
            f"{(today + timedelta(days=horizon_days)).isoformat()}.\n\n"
            "Start by calling list_groups() and list_discovery_proposals() so "
            "you know what is already covered and what has already been "
            "rejected. Then load the calendar corpus with "
            f"get_events(date_from='{today.isoformat()}').\n\n"
            f"Scan these sources:\n{source_list}\n\n"
            "Then run open web searches for anything the sources missed."
        )
        results.update(_extract_json(out))

        if not dry_run:
            results['proposed'] = _proposals_this_run(mcp, since=today)

    if not dry_run:
        digest.send(run_id, results)
    else:
        print('\n--- digest (not sent) ---\n' + digest.render(run_id, results)[1])
    return results


try:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()

    @app.entrypoint
    def invoke(payload):
        task_id = app.add_async_task('discovery')
        try:
            return run_discovery(dry_run=bool(payload.get('dry_run')),
                                 run_id=payload.get('run_id'))
        finally:
            app.complete_async_task(task_id)

except ImportError:
    app = None


def should_serve(args, app_available):
    return app_available and not args.local


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    if not should_serve(args, app is not None):
        print(json.dumps(run_discovery(dry_run=args.dry_run), indent=2))
    else:
        app.run()
