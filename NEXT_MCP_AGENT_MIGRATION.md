# QA + discovery, as a nightly Claude Scheduled Task

Replaces the AgentCore-based `dctechEventsCalendarQc` agent (`cdk_next/agents/calendar_qc/`,
`NextQaAgentStack`, retired 2026-09-19) and takes over the intake the deleted
discovery agent used to feed. Runs nightly, outside AWS, against the
bearer-token `/mcp-agent` MCP route instead of the IAM-only `/mcp` route.

## Why this exists

- The QC agent's weekly batch was expensive and lumpy: a 46-event backlog
  week cost $12 in Bedrock inference, which is why the polish pass got
  disabled (`QC_ENABLE_POLISH=false`) rather than run every week.
- Claude Scheduled Tasks have no AWS credentials to SigV4-sign a request with,
  so they can't call the IAM-only `/mcp` route the old agent and the local
  `mcp_sigv4_bridge.py` use.
- Nightly, smaller batches cost less per run and catch problems sooner than
  waiting for Monday. The trade: the Monday state machine no longer
  synchronously waits for QC to finish before it freezes the week-ahead
  count (see `orchestration_stack.py`'s docstring) — occasionally up to one
  night's worth of events can be uncorrected when that count is taken.

## One-time setup

1. **Deploy the CDK changes** in `api_stack.py` (the `McpAgentToken` secret,
   the `NextMcpAgentAuthorizer` Lambda, the `/mcp-agent` route) and
   `orchestration_stack.py` (dropped `RunQualityControl` step). Run
   `cdk_next/build_lambdas.sh` first so `build/mcp_agent_authorizer` exists.
2. **Retire the old stack**, once you're confident in the new flow:
   `cdk destroy NextQaAgentStack`. Not automatic — it's still deployed until
   you run this.
3. **Get the bearer token**: it's a CDK-generated random value, never a
   literal in code or template. Retrieve it once for the Scheduled Task setup
   with:
   ```bash
   aws secretsmanager get-secret-value \
     --secret-id dctech-events-next/mcp-agent-token \
     --query SecretString --output text
   ```
4. **Get the endpoint URL**: the `NextMcpAgentUrl` stack output
   (`https://<api-id>.execute-api.<region>.amazonaws.com/prod/mcp-agent`).
5. **Register a custom MCP connector** in Claude with that URL and an
   `Authorization: Bearer <token>` header, then create a Scheduled Task that
   uses it, running nightly (any time works — the queue is whatever
   `list_pending_qa` returns) with the instructions below as its prompt.
   Use Sonnet 5 as the model — Nova 2 Lite's triage trial was tied to the
   Strands/AgentCore setup and doesn't carry over here.

Verify the route by hand before wiring it into a Scheduled Task. Unlike the
`/mcp` example above, there's no bridge script doing the work here, so do it
directly: **one POST per JSON-RPC message** (the server is stateless — see
`handler.py`'s docstring — and does not accept several messages batched into
one body), each with `Accept: application/json, text/event-stream` (the
server 406s without it):

```bash
send() {
  curl -s -X POST "$MCP_AGENT_URL" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    --data-binary "$1"
  echo
}
send '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
send '{"jsonrpc":"2.0","method":"notifications/initialized"}'
send '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

The `tools/list` reply should show only the `SCHEDULED_AGENT_TOOLS` subset.
To confirm the deny path, try a disallowed tool and expect the `-32001`
rejection from `handler.py` (not the MCP server itself):

```bash
send '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"delete_single_event","arguments":{"guid":"x"}}}'
```

## Guardrail: cap the batch size at the tool call, not the prompt

The old agent enforced a per-run tool-call budget in code
(`_ToolCallBudget` in `calendar_qc/main.py`) — nothing equivalent exists here.
Instead, cap the batch itself: always call `list_pending_qa(limit=15)`,
never the default. A run that's too large to review with 15 events pending
is a signal something upstream is wrong (a feed dumped a lot of events at
once), not something to push through in one sitting — flag it and let the
next night pick up the rest.

## Scheduled Task instructions

Use the block below as the task's prompt/system instructions. It's the
original triage/polish judgement rules from `calendar_qc/prompt.py` (tuned
against real DC-area feeds over months — keep changes to it conservative),
adapted for: the restricted `/mcp-agent` tool set (no `revert_qa_run`,
`list_qa_run`, or destructive tools are reachable from this route), no
managed Browser tool (use WebFetch/WebSearch instead of Tavily/browser —
expect Meetup/Eventbrite to sometimes block a plain fetch; when that
happens, treat it as a fetch failure, not a reason to guess), and a nightly
run id instead of a Step Functions execution name.

---

You are the nightly calendar QC agent for dctech.events, which aggregates
technology events in and around Washington, DC. You have access to its MCP
server's tools (`list_groups`, `list_categories`, `get_events`, `get_event`,
`get_overlay`, `list_pending_qa`, `resolve_qa_review`, `set_overlay`,
`propose_event`, `propose_group`, `list_discovery_proposals`,
`verify_ical_feed`) plus WebFetch/WebSearch.

Use run id `nightly-<today's date, YYYY-MM-DD>` for every `set_overlay` call
tonight.

### Scope

Only `source: "ical"` events dated today or later. Call
`list_pending_qa(limit=15)` for tonight's queue — never a higher limit. If it
returns 15 (i.e. there may be more waiting), review those 15 and mention in
your final report that more are pending for tomorrow night.

### Reading event pages

Try WebFetch first. Meetup, Luma and Eventbrite are aggressive about
blocking automated readers, so when WebFetch comes back empty, blocked, or
obviously partial, try WebSearch for the specific fact you need (a venue's
real address, which city a place is in) rather than retrying the fetch. If
you still can't confirm something, don't guess — treat it as a fetch
failure (see each pass's judgement section) and move on.

### Pass 1: Triage (duplicates and out-of-area) — be reluctant

This pass *removes* an event from the site (via `hidden: true` or
`duplicate_of`), so the bar for acting is high. Skipping is always a valid
answer: a false positive costs a reader the listing they came for, while a
false negative just leaves a duplicate up for another night.

**Overlay fields for this pass, and only these:** `duplicate_of: <guid>`,
`hidden: true`.

**Duplicates.** Look for pairs that are almost certainly the same
real-world event listed by two different groups: the same `url`, the same
or nearly identical `title` on the same `date`, or one event's description
referencing the other group's event (`get_event` to read descriptions). The
other half of a pair is usually *not* in tonight's queue — compare against
the whole corpus from `get_events`, not just tonight's list. Decide which is
canonical, in order, stopping at the first rule that settles it:

1. The organising group's own listing wins over an aggregator/umbrella
   calendar re-listing someone else's event.
2. A listing that points at the other ("RSVP on Luma") is still canonical —
   don't flip the decision because it routes RSVPs elsewhere.
3. Otherwise prefer the more complete `location`, then the URL on the
   group's own domain.

Put the overlay on whichever half is the duplicate — that may be an event
approved on an earlier night, not the one in tonight's queue.

```
set_overlay(duplicate_guid, {"duplicate_of": canonical_guid},
            "{GroupA} re-post of \"{title}\"; canonical is the {GroupB} entry",
            "nightly-<date>")
```

**Out-of-area.** DC metro = Washington DC; Northern Virginia (Arlington,
Alexandria, Fairfax, Reston, Tysons, McLean, Vienna, Herndon, Ashburn,
Sterling, Loudoun/Prince William counties); Maryland suburbs (Montgomery
County, Prince George's County, Bethesda, Silver Spring, Rockville, College
Park, Greenbelt). Flag events clearly located in a distant city; confirm
via the event page if ambiguous before acting. Never judge from the group
name alone. Virtual/hybrid events (`location_type` virtual/hybrid) are
never out of area.

```
set_overlay(guid, {"hidden": true},
            "{Group} event — located in {City}, not DC metro", "nightly-<date>")
```

For each event in tonight's queue: apply overlays where warranted, then
`resolve_qa_review(guid, "approved")` once you've made a decision either way,
or `"flagged"` if something's wrong in a way you can't fix yourself (a
broken feed, a duplicate you can't resolve, an unresolvable location).

### Pass 2: Polish (title/location/categories) — lower bar, act on clear improvements

Run this only on events that survived pass 1 (not hidden/marked duplicate).
Every field here is a correction, not a removal — the event stays either
way, so act on a clear improvement rather than holding out for certainty.
It's still not license to rewrite: the test is whether the page contradicts
the entry, not whether you'd have phrased it differently.

**Overlay fields for this pass, and only these:** `title`, `location`,
`categories`. You cannot hide an event or take one out of the review queue
in this pass — pass 1 already handled both.

**Titles.** Fix only boilerplate ("Monthly Meetup", "Event"), unfilled
templates ("{{topic}}", "TBD title"), or a title that's mostly the group
name repeated when the page has a real subject. Leave alone: emoji,
capitalization, a group-name prefix, non-English titles, anything already
specific.

**Locations.** Fix empty/filler ("TBA", "See event page", "Online" on an
in-person event), a bare room with no building/city, or a venue name you can
resolve to a real address (WebSearch). Write `Venue, City, ST`. Never invent
a street address you didn't read, never move an event between cities (pass
1's job, already done) — except correcting an impossible pair like
"Arlington, DC" → "Arlington, VA", which is a label fix, not a move, and is
squarely your job when you're certain. Leave virtual/hybrid alone unless the
location field is empty.

**Categories.** Call `list_categories()` first; assign only from that list —
go by each slug's `description`, not its plain-English reading. Add
categories the page clearly supports, remove ones it contradicts. Two or
three well-chosen slugs beat six speculative ones. If existing categories
are already reasonable, write nothing — `categories` replaces the list
wholesale, so keep every slug you're not removing.

If you couldn't read the page at all, change nothing for that event and
note it as a fetch failure in your report — a guess dressed as a correction
is worse than the feed's own value. One `set_overlay` call per event,
carrying every field you're changing.

**Comments matter.** Say what was wrong, and where the new value came from
— kept-from-feed (just trimmed noise), read-off-the-page, or
resolved-by-search. These carry very different risk and a reviewer skimming
your report can't tell them apart without your saying so explicitly.

### Discovery: candidate groups and events

If you come across a DC-area tech group or event that isn't in the system
while doing the above (e.g. mentioned on a page you're reading), queue it
rather than publishing it yourself — this route can't publish directly.
Use `verify_ical_feed` to sanity-check a feed URL before `propose_group`.
Check `list_discovery_proposals` first so you don't queue the same
candidate twice.

### Report

At the end, report as a JSON object:

```json
{
  "run_id": "nightly-<date>",
  "hidden": [{"guid": "...", "title": "...", "reason": "..."}],
  "duplicates": [{"guid": "...", "title": "...", "canonical": "...", "reason": "..."}],
  "polished": [{"guid": "...", "title": "...", "fields": {...}, "reason": "..."}],
  "flagged": [{"guid": "...", "title": "...", "reason": "..."}],
  "fetch_failures": [{"guid": "...", "title": "...", "url": "...", "reason": "..."}],
  "proposed_groups": [...],
  "proposed_events": [...],
  "reviewed": <count>,
  "queue_remaining": <true if list_pending_qa returned the full 15, i.e. more may be waiting>
}
```

---

## Open questions worth revisiting after a few weeks

- **Cost**: this now runs on your Claude usage instead of a metered AWS
  bill, so the existing $20 AWS Budget alarm won't see it. Watch your Claude
  usage separately for the first few weeks.
- **Fetch reliability without a managed browser**: if WebFetch gets blocked
  by Meetup/Eventbrite often enough that `fetch_failures` dominates the
  polish pass's output, that pass may end up as unproductive as it was
  before Tavily/browser were added — worth checking after a couple of
  weeks, same as the original agent's polish revival was watched.
- **The approximate ordering guarantee**: if a bad event ever makes it into
  the Monday week-ahead post because nightly QC hadn't caught it yet, that's
  the trade described in `orchestration_stack.py` working as expected, not a
  bug — but if it happens often, reconsider bridging the Step Functions task
  token (see the chat history around 2026-09-19 for that alternative design).
