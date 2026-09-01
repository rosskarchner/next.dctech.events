"""
Cognito JWT authorization.

Reads pre-validated claims from API Gateway Cognito Authorizer context.
API Gateway validates the JWT before invoking Lambda, so re-validation
here is unnecessary — for routes API Gateway has the authorizer attached to.

Some routes are deliberately unauthenticated at the gateway (/api/submissions,
/api/corrections — a magic-link-only request must reach the Lambda without a
Cognito token at all, and API Gateway's built-in Cognito authorizer has no
"optional" mode, only reject-or-pass). On those routes,
requestContext.authorizer.claims is never populated, even for a signed-in
user holding a perfectly valid token — so get_user_from_event falls back to
verifying the Authorization header's Bearer token itself, against the user
pool's JWKS, before giving up (next_dctech_events-1k7).
"""

import os

import jwt
from jwt import PyJWKClient

COGNITO_CLIENT_ID = os.environ.get('COGNITO_USER_POOL_CLIENT_ID', '')
COGNITO_USER_POOL_ID = os.environ.get('COGNITO_USER_POOL_ID', '')

# The pool id's own prefix names its region (e.g. 'us-east-1_8Ay4dTt8j') —
# deriving it this way keeps the JWKS/issuer URLs self-contained rather than
# trusting Lambda's ambient AWS_REGION to agree with where the pool lives.
_REGION = COGNITO_USER_POOL_ID.split('_')[0] if COGNITO_USER_POOL_ID else ''
_ISSUER = (
    f'https://cognito-idp.{_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}'
    if COGNITO_USER_POOL_ID else ''
)

# Constructed once and reused across warm invocations, so the JWKS is fetched
# at most once per container lifetime rather than once per request — the same
# reuse-across-invocations pattern db.py uses for its table handle and
# magic_link.py for its KMS client. PyJWKClient fetches lazily on first use,
# so building this unconditionally at import time costs nothing when unused.
_jwks_client = PyJWKClient(f'{_ISSUER}/.well-known/jwks.json') if _ISSUER else None


def _bearer_token(event):
    headers = event.get('headers') or {}
    auth = headers.get('Authorization') or headers.get('authorization') or ''
    if not auth.lower().startswith('bearer '):
        return None
    return auth[len('bearer '):].strip() or None


def _claims_from_bearer_token(event):
    """Manually verify a Bearer ID token against the Cognito JWKS.

    Returns a claims dict, or None if there is no token, it does not verify,
    or it is not an ID token (an access token carries no email/groups claims
    worth trusting here, and DctechAuth.authorizedFetch only ever sends the
    ID token anyway).
    """
    if not _jwks_client:
        return None
    token = _bearer_token(event)
    if not token:
        return None
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token, signing_key.key, algorithms=['RS256'],
            audience=COGNITO_CLIENT_ID, issuer=_ISSUER,
            options={'require': ['exp', 'iat', 'sub']},
        )
    except Exception:
        return None
    if claims.get('token_use') != 'id':
        return None
    return claims


def get_user_from_event(event):
    """
    Extract user claims, from wherever this request's route puts them.

    Most routes sit behind API Gateway's Cognito authorizer, which validates
    the JWT before Lambda is invoked and injects claims into
    event['requestContext']['authorizer']['claims'] — trusted without
    re-verification. A route with no gateway authorizer (see module
    docstring) has no such claims, so this falls back to verifying the
    Authorization header itself.

    Returns (claims_dict, error_response) tuple.
    """
    try:
        claims = (
            event.get('requestContext', {})
                 .get('authorizer', {})
                 .get('claims', {})
        )
        if not claims or not claims.get('sub'):
            claims = _claims_from_bearer_token(event) or {}

        if not claims or not claims.get('sub'):
            return None, {'statusCode': 401, 'body': 'Unauthorized'}

        # Ensure email is available in claims, falling back to cognito:username or sub
        if 'email' not in claims:
            claims['email'] = claims.get('cognito:username') or claims.get('sub')

        return claims, None
    except Exception:
        return None, {'statusCode': 401, 'body': 'Unauthorized'}


def require_admin(claims):
    """
    Check if user has admin group membership.

    Returns error response dict if not admin, None if authorized.
    """
    groups = claims.get('cognito:groups', [])
    if 'admins' not in groups:
        return {'statusCode': 403, 'body': 'Admin access required'}
    return None
