"""Cognito PreSignUp trigger — reject automated sign-ups before an email is sent.

The value here is the timing: rejecting at PreSignUp means Cognito never
sends a verification email, so abusive registrations cost the domain's
sending reputation nothing. Pruning unconfirmed accounts afterwards does
not undo an email already delivered to someone who never asked for it.

Runs in audit mode unless ENFORCE=true: every decision is logged, but
nothing is blocked. Check the logs before enforcing.

Fails OPEN by design. A PreSignUp trigger that raises on an unexpected
error blocks every registration on the site, which is far worse than
letting spam through, so only a deliberate rejection propagates.
"""
import os

from signup_rules import evaluate

ENFORCE = os.environ.get('ENFORCE', 'false').lower() == 'true'


class SignupRejected(Exception):
    """Message reaches the end user, so keep it human and non-technical."""


def lambda_handler(event, context):
    try:
        trigger = event.get('triggerSource', '')
        # Admin-created and federated identities are not the attack surface.
        if trigger != 'PreSignUp_SignUp':
            return event

        email = (event.get('request', {})
                      .get('userAttributes', {})
                      .get('email', ''))
        reasons = evaluate(email)

        if not reasons:
            print(f'ALLOW {email}')
            return event

        verdict = 'REJECT' if ENFORCE else 'WOULD-REJECT (audit mode)'
        print(f'{verdict} {email} :: {"; ".join(reasons)}')

        if ENFORCE:
            raise SignupRejected(
                'This email address could not be accepted. If you believe '
                'this is a mistake, contact ross@karchner.com.'
            )
        return event

    except SignupRejected:
        raise
    except Exception as e:  # never block real users because of a bug here
        print(f'ERROR in pre-signup check, allowing sign-up: {e!r}')
        return event
