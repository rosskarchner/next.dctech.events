"""Magic-link tokens for account-free actions (event submission, and
subscriber newsletter preferences).

Someone proves control of an email address by clicking a signed link
instead of holding a Cognito account. The token is a KMS HMAC over
``purpose:email:timestamp``, exactly like the newsletter's own confirmation
links — which means it is stateless: nothing to store, nothing to expire on
a schedule, and no session table to keep clean. The timestamp inside the
signed message is what bounds its lifetime.

`purpose` (default `'submit'`) scopes what a token is good for two ways:
the message itself (so a submission-purpose token for a given email+
timestamp can never be replayed as, say, a preferences-purpose one) *and*
which KMS key signs it — each purpose gets its own key (see
_key_id_for), matching this codebase's standing rule that the submission
key and the newsletter's confirmation key are deliberately never shared,
so each purpose's key can be rotated independently. 'prefs' is shared
across NextApiStack (which verifies it) and NextNewsletterStack (which
also generates it, embedding a link in every send) — the one purpose here
that legitimately spans two stacks, wired via a CDK cross-stack
reference the same way both stacks already share the DynamoDB table.

The signature covers the timestamp, so a client cannot extend its own token by
editing the query string; tampering with either field invalidates the MAC.
"""
import os
import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from time import time

import boto3

SUBMIT_KEY_ID = os.environ.get('SUBMIT_KEY_ID', '')
PREFS_KEY_ID = os.environ.get('PREFS_KEY_ID', '')
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


# Purpose-specific TTLs: a submission/correction link is meant to be used
# within minutes, in the same sitting it was requested — a "manage my
# preferences" link sits in an inbox and is plausibly reopened weeks later.
_PURPOSE_TTL_SECONDS = {
    'prefs': 30 * 24 * 60 * 60,
}

_PURPOSE_NOUN = {
    'submit': 'submission link',
    'prefs': 'preferences link',
}

_PURPOSE_KEY_ENV_NAME = {
    'prefs': 'PREFS_KEY_ID',
}


def _key_id_for(purpose):
    """Each purpose signs with its own KMS key — never SUBMIT_KEY_ID as a
    default-for-everything, so a purpose introduced later doesn't silently
    inherit a key it was never granted access to."""
    if purpose == 'submit':
        return SUBMIT_KEY_ID
    return PREFS_KEY_ID if purpose == 'prefs' else ''


def ttl_seconds_for(purpose='submit'):
    """The default TTL for a purpose, unless a verify_token caller
    overrides it directly via ttl_seconds. Used by callers (e.g. the
    "link works for the next N hours" copy in the request-link email) that
    need to describe a token's lifetime without duplicating the mapping."""
    return _PURPOSE_TTL_SECONDS.get(purpose, TOKEN_TTL_SECONDS)


def _message(email, timestamp, purpose='submit'):
    return f"{purpose}:{normalize_email(email)}:{timestamp}".encode()


def generate_token(email, timestamp=None, purpose='submit'):
    """Return (timestamp, signature) for an email address.

    purpose picks both the signed message and the KMS key (_key_id_for) —
    a token issued for one purpose can never verify as another, and each
    purpose's key can be rotated independently of the others.
    """
    key_id = _key_id_for(purpose)
    if not key_id:
        env_name = _PURPOSE_KEY_ENV_NAME.get(purpose, 'SUBMIT_KEY_ID')
        raise ValueError(f'Missing {env_name} configuration')

    timestamp = int(timestamp if timestamp is not None else time())
    response = kms.generate_mac(
        Message=_message(email, timestamp, purpose),
        KeyId=key_id,
        MacAlgorithm='HMAC_SHA_512',
    )
    return timestamp, _b64(response['Mac'])


def verify_token(email, timestamp, signature, purpose='submit', ttl_seconds=None):
    """Validate a magic-link token. Returns (ok, reason)."""
    noun = _PURPOSE_NOUN.get(purpose, 'submission link')
    key_id = _key_id_for(purpose)
    if not key_id:
        return False, 'Server is not configured for magic-link submission'
    if not is_valid_email(email):
        return False, f'Invalid {noun}'

    try:
        timestamp = int(timestamp)
    except (TypeError, ValueError):
        return False, f'Invalid {noun}'

    if ttl_seconds is None:
        ttl_seconds = _PURPOSE_TTL_SECONDS.get(purpose, TOKEN_TTL_SECONDS)

    age = time() - timestamp
    # Reject far-future timestamps too: a clock-skewed or hand-crafted value
    # should not buy a token that outlives the TTL.
    if age < -300:
        return False, f'Invalid {noun}'
    if age > ttl_seconds:
        return False, f'This {noun} has expired. Please request a new one.'

    try:
        valid = kms.verify_mac(
            Message=_message(email, timestamp, purpose),
            KeyId=key_id,
            MacAlgorithm='HMAC_SHA_512',
            Mac=_unb64(signature or ''),
        )['MacValid']
    except Exception as exc:  # KMSInvalidMacException lands here too
        print(f'Magic-link verification failed: {exc}')
        return False, f'Invalid {noun}'

    if not valid:
        return False, f'Invalid {noun}'
    return True, None


def build_link(email, timestamp, signature, path='/edit/submit-event.html'):
    """The URL emailed to a submitter.

    `path` may already carry its own query string (e.g. a correction link's
    `?guid=...`), so the token params are appended with `&` in that case
    rather than always starting a fresh `?`.
    """
    separator = '&' if '?' in path else '?'
    return (
        f"{BASE_URL}{path}"
        f"{separator}e={_b64(normalize_email(email).encode())}"
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
