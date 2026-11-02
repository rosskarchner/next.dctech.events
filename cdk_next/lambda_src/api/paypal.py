"""
PayPal Payments Standard plumbing shared by everything we sell: weekly
sponsorships (sponsorships.py) and event highlights (highlights.py).

Only the PayPal-generic parts live here: the Buy Now form, IPN verification
and parsing, the per-message idempotency record, the receiver/amount checks,
and the plain-text mailer. What a payment *means* — which booking it pays
for and what state that moves it to — stays with each product.

Shared by the API Lambda and the MCP server, so it is copied into build/mcp/
beside sponsorships.py.
"""

import os
import time
from decimal import Decimal, InvalidOperation
from html import escape as html_escape
from urllib.parse import parse_qsl
from urllib.request import Request, urlopen

from botocore.exceptions import ClientError

import db

PAYPAL_RECEIVER_EMAIL = os.environ.get('PAYPAL_RECEIVER_EMAIL', 'ross@karchner.com')
PAYPAL_SANDBOX = os.environ.get('PAYPAL_SANDBOX', '') == '1'
BASE_URL = os.environ.get('BASE_URL', 'https://dctech.events')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'ross@karchner.com')
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'outbound@dctech.events')
REPLY_TO_EMAIL = os.environ.get('REPLY_TO_EMAIL', 'ross@karchner.com')

# The IPN fields worth keeping on a PAYMENT# record.
_PAYMENT_FIELDS = ('txn_id', 'parent_txn_id', 'payment_status', 'mc_gross',
                   'mc_currency', 'receiver_email', 'payer_email', 'custom',
                   'pending_reason', 'reason_code')


def iso_now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


# ─── Checkout ──────────────────────────────────────────────────────

def paypal_form_url():
    host = 'www.sandbox.paypal.com' if PAYPAL_SANDBOX else 'www.paypal.com'
    return f'https://{host}/cgi-bin/webscr'


def buy_now_fields(*, item_name, item_number, amount, custom, invoice,
                   return_url, cancel_url, notify_url=None):
    """Fields for a PayPal Payments Standard "Buy Now" (_xclick) form."""
    fields = {
        'cmd': '_xclick',
        'business': PAYPAL_RECEIVER_EMAIL,
        'item_name': item_name,
        'item_number': item_number,
        'amount': amount,
        'currency_code': 'USD',
        'no_shipping': '1',
        'custom': custom,
        'invoice': invoice,
        'return': return_url,
        'cancel_return': cancel_url,
    }
    if notify_url:
        fields['notify_url'] = notify_url
    return fields


# ─── IPN ───────────────────────────────────────────────────────────

def ipn_verify_url():
    host = 'ipnpb.sandbox.paypal.com' if PAYPAL_SANDBOX else 'ipnpb.paypal.com'
    return f'https://{host}/cgi-bin/webscr'


def verify_with_paypal(raw_body):
    """Echo the message back to PayPal; only 'VERIFIED' means it came from them."""
    req = Request(
        ipn_verify_url(),
        data=b'cmd=_notify-validate&' + raw_body.encode('latin-1', 'replace'),
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'User-Agent': 'dctech-events-ipn/1.0'},
    )
    with urlopen(req, timeout=15) as resp:
        return resp.read().decode('utf-8', 'replace').strip() == 'VERIFIED'


def parse_ipn(raw_body):
    return dict(parse_qsl(raw_body, keep_blank_values=True, encoding='latin-1'))


def custom_of(raw_body):
    """The `custom` field of an unverified IPN body — for routing only."""
    return parse_ipn(raw_body).get('custom', '')


def record_payment(pk, params):
    """Store an IPN once under `pk`. Returns False if this exact message was seen."""
    txn_id = params.get('txn_id', '')
    status = params.get('payment_status', '')
    item = {
        'PK': pk, 'SK': f'PAYMENT#{txn_id}#{status}',
        'received_at': iso_now(),
        **{k: params.get(k, '') for k in _PAYMENT_FIELDS},
    }
    try:
        db._get_table().put_item(Item=item,
                                 ConditionExpression='attribute_not_exists(PK)')
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False
        raise
    return True


def receiver_ok(params):
    """True if the money went to our PayPal account."""
    receiver = (params.get('receiver_email') or params.get('business') or '').lower()
    return receiver == PAYPAL_RECEIVER_EMAIL.lower()


def amount_ok(params, expected):
    """True if the IPN is in USD for exactly `expected` (a Decimal)."""
    try:
        gross = Decimal(params.get('mc_gross', ''))
    except InvalidOperation:
        return False
    return params.get('mc_currency') == 'USD' and gross == expected


# ─── Email ─────────────────────────────────────────────────────────

def send(to, subject, text, reply_to=None):
    """Plain-text mail. Never raises: mail trouble must not fail a booking."""
    try:
        import boto3
        boto3.client('sesv2').send_email(
            FromEmailAddress=FROM_EMAIL,
            ReplyToAddresses=[reply_to or REPLY_TO_EMAIL],
            Destination={'ToAddresses': [to]},
            Content={'Simple': {
                'Subject': {'Data': subject[:200]},
                'Body': {
                    'Text': {'Data': text},
                    'Html': {'Data': '<pre style="font-family:inherit;'
                                     'white-space:pre-wrap">'
                                     f'{html_escape(text)}</pre>'},
                },
            }},
        )
    except Exception as exc:
        print(f'Email to {to} failed: {exc}')
