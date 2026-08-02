"""Heuristics for rejecting automated Cognito sign-ups.

Kept separate from the Lambda handler so the rules can be exercised against
a real export of the user pool without invoking anything.

Tuned against the 2026-08-02 spam wave, where 51 of 66 accounts were
unconfirmed bot registrations. The dominant signature was Gmail
dot-injection — l.i.z.a.g.ey.e.r.ma.n@gmail.com and friends — which works
because Gmail ignores dots, so one mailbox yields unlimited distinct
addresses.
"""
import re

# Gmail (and googlemail) ignore dots and anything after a '+'.
_GMAIL_DOMAINS = {'gmail.com', 'googlemail.com'}

# A real address rarely has more than two dots in the local part
# ('mary.jane.watson' has two). Three or more is the injection signature.
MAX_LOCAL_DOTS = 3

_DISPOSABLE_DOMAINS = {
    'mailinator.com', 'guerrillamail.com', '10minutemail.com', 'yopmail.com',
    'tempmail.com', 'temp-mail.org', 'throwawaymail.com', 'sharklasers.com',
    'trashmail.com', 'getnada.com', 'dispostable.com', 'maildrop.cc',
}


def split_email(email):
    email = (email or '').strip().lower()
    if email.count('@') != 1:
        return None, None
    local, domain = email.split('@')
    return local, domain


def canonical_email(email):
    """Collapse provider-specific aliasing to the mailbox it really targets."""
    local, domain = split_email(email)
    if not local or not domain:
        return email
    local = local.split('+', 1)[0]
    if domain in _GMAIL_DOMAINS:
        local = local.replace('.', '')
        domain = 'gmail.com'
    return f'{local}@{domain}'


def evaluate(email):
    """Return a list of reasons this sign-up looks automated. Empty == fine."""
    reasons = []
    local, domain = split_email(email)

    if not local or not domain:
        return ['malformed email address']

    if domain in _DISPOSABLE_DOMAINS:
        reasons.append(f'disposable email domain ({domain})')

    if domain in _GMAIL_DOMAINS:
        dots = local.count('.')
        if dots >= MAX_LOCAL_DOTS:
            reasons.append(
                f'gmail dot-injection ({dots} dots in local part; '
                f'canonical form {canonical_email(email)})')

    # Consecutive dots are invalid per RFC and only show up in generated
    # addresses.
    if re.search(r'\.\.', local):
        reasons.append('consecutive dots in local part')

    return reasons
