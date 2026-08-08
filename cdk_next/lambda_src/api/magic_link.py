"""Magic-link tokens for account-free event submission.

A submitter proves control of an email address by clicking a signed link
instead of holding a Cognito account. The token is a KMS HMAC over
``email:timestamp``, exactly like the newsletter's confirmation links — which
means it is stateless: nothing to store, nothing to expire on a schedule, and
no session table to keep clean. The timestamp inside the signed message is
what bounds its lifetime.

The signature covers the timestamp, so a client cannot extend its own token by
editing the query string; tampering with either field invalidates the MAC.
"""
import os
import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from time import time

import boto3

SUBMIT_KEY_ID = os.environ.get('SUBMIT_KEY_ID', '')
BASE_URL = os.environ.get('BASE_URL', 'https://dctech.events')

# How long a clicked link stays usable. Long enough to fill in a form in
# another sitting, short enough that a forwarded or leaked link goes stale.
TOKEN_TTL_SECONDS = int(os.environ.get('MAGIC_LINK_TTL_SECONDS', 24 * 60 * 60))

# Deliberately permissive: this validates shape, not deliverability. SES is
# the real arbiter of whether an address exists.
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

kms = boto3.client('kms')


def normalize_email(email):
    return str(email or '').strip().lower()


def is_valid_email(email):
    email = normalize_email(email)
    return bool(email) and len(email) <= 254 and bool(EMAIL_RE.match(email))


def _b64(raw):
    return urlsafe_b64encode(raw).decode('utf-8').rstrip('=')


def _unb64(value):
    padded = value + '=' * (-len(value) % 4)
    return urlsafe_b64decode(padded.encode())


def _message(email, timestamp):
    return f"submit:{normalize_email(email)}:{timestamp}".encode()


def generate_token(email, timestamp=None):
    """Return (timestamp, signature) for an email address."""
    if not SUBMIT_KEY_ID:
        raise ValueError('Missing SUBMIT_KEY_ID configuration')

    timestamp = int(timestamp if timestamp is not None else time())
    response = kms.generate_mac(
        Message=_message(email, timestamp),
        KeyId=SUBMIT_KEY_ID,
        MacAlgorithm='HMAC_SHA_512',
    )
    return timestamp, _b64(response['Mac'])


def verify_token(email, timestamp, signature):
    """Validate a magic-link token. Returns (ok, reason)."""
    if not SUBMIT_KEY_ID:
        return False, 'Server is not configured for magic-link submission'
    if not is_valid_email(email):
        return False, 'Invalid submission link'

    try:
        timestamp = int(timestamp)
    except (TypeError, ValueError):
        return False, 'Invalid submission link'

    age = time() - timestamp
    # Reject far-future timestamps too: a clock-skewed or hand-crafted value
    # should not buy a token that outlives the TTL.
    if age < -300:
        return False, 'Invalid submission link'
    if age > TOKEN_TTL_SECONDS:
        return False, 'This submission link has expired. Please request a new one.'

    try:
        valid = kms.verify_mac(
            Message=_message(email, timestamp),
            KeyId=SUBMIT_KEY_ID,
            MacAlgorithm='HMAC_SHA_512',
            Mac=_unb64(signature or ''),
        )['MacValid']
    except Exception as exc:  # KMSInvalidMacException lands here too
        print(f'Magic-link verification failed: {exc}')
        return False, 'Invalid submission link'

    if not valid:
        return False, 'Invalid submission link'
    return True, None


def build_link(email, timestamp, signature, path='/edit/submit-event.html'):
    """The URL emailed to a submitter."""
    return (
        f"{BASE_URL}{path}"
        f"?e={_b64(normalize_email(email).encode())}"
        f"&t={timestamp}"
        f"&s={signature}"
    )


def decode_email_param(value):
    """Decode the base64url email from a magic link, or '' if malformed."""
    try:
        return normalize_email(_unb64(value or '').decode('utf-8'))
    except Exception:
        return ''


def token_from_request(data):
    """Pull magic-link fields out of a submission payload.

    Returns (email, timestamp, signature) with empty strings when absent, so
    callers can distinguish "no magic link offered" from "bad magic link".
    """
    return (
        decode_email_param(data.get('mlt_e')) or normalize_email(data.get('mlt_email')),
        data.get('mlt_t') or data.get('mlt_ts') or '',
        data.get('mlt_s') or data.get('mlt_sig') or '',
    )
