"""QA Agent — infrastructure scaffold only (stub body).

The real LLM orchestration/prompts are an explicit follow-up. This handler
logs what it *would* do and exits without mutating anything. Its IAM role
carries provisioned-but-unused Bedrock permissions for that follow-up.
"""
import json

import db


def lambda_handler(event, context):
    pending = db.get_events_by_review_status('pending_qa', limit=50)
    plan = {
        'agent': 'qa',
        'status': 'scaffold — no action taken',
        'would_do': [
            'read events with review_status=pending_qa from GSI5',
            'invoke Bedrock to check each event for spam/miscategorization/duplicates',
            'set review_status=approved or flagged via db.set_event_review_status',
            'write overlay fixes via the MCP set_overlay tool',
        ],
        'pending_qa_count': len(pending),
    }
    print(json.dumps(plan))
    return plan
