"""Discovery Agent — infrastructure scaffold only (stub body).

An event-finding web/search-engine agent (not a forum integration). The real
LLM orchestration is an explicit follow-up — likely built on
~/projects/calendar-agents' Strands-based URL/event extraction agent. This
handler logs what it *would* do and exits without mutating anything. Its IAM
role carries provisioned-but-unused Bedrock (and future web-search) perms.
"""
import json

import db


def lambda_handler(event, context):
    candidates = db.get_candidates()
    plan = {
        'agent': 'discovery',
        'status': 'scaffold — no action taken',
        'would_do': [
            'search the web for DC-area tech events not already in the table',
            'extract structured event data with Bedrock',
            'write CANDIDATE#{hash} items (review_status=pending_discovery_review)',
            'never touch real EVENT# records until a human promotes a candidate',
        ],
        'pending_candidates': len(candidates),
    }
    print(json.dumps(plan))
    return plan
