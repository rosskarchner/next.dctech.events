"""Tests for get_user_from_event's two claim sources.

Most routes sit behind API Gateway's Cognito authorizer, which pre-validates
the JWT and hands claims to the Lambda directly — that path is trusted
without re-verification. A few routes (/api/submissions, /api/corrections)
are deliberately unauthenticated at the gateway so a magic-link-only request
can reach the Lambda, which means a signed-in user's token never reaches
requestContext.authorizer.claims either — get_user_from_event falls back to
verifying the Authorization header itself (next_dctech_events-1k7).

These tests generate a real RSA keypair and real signed tokens rather than
mocking away jwt.decode, so the actual signature/expiry/audience/issuer
checks run for real — only the network fetch of the JWKS is faked, the same
"fake the I/O boundary, keep the logic real" style test_overlay.py's `store`
fixture uses for DynamoDB.

Run: python -m pytest test_auth.py
"""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import auth

ISSUER = 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL'
CLIENT_ID = 'test-client-id'
KID = 'test-kid'


@pytest.fixture
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture
def signing_env(monkeypatch, keypair):
    """Point auth's JWKS lookup at our local test keypair, and its
    issuer/audience checks at matching test values."""
    private_key, public_key = keypair
    monkeypatch.setattr(auth, 'COGNITO_CLIENT_ID', CLIENT_ID)
    monkeypatch.setattr(auth, '_ISSUER', ISSUER)

    class _FakeSigningKey:
        key = public_key

    class _FakeJwksClient:
        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey()

    monkeypatch.setattr(auth, '_jwks_client', _FakeJwksClient())
    return private_key


def _token(private_key, **overrides):
    now = int(time.time())
    payload = {
        'sub': 'user-123', 'email': 'test@example.com',
        'cognito:groups': ['admins'], 'token_use': 'id',
        'aud': CLIENT_ID, 'iss': ISSUER,
        'iat': now, 'exp': now + 3600,
    }
    payload.update(overrides)
    return jwt.encode(payload, private_key, algorithm='RS256',
                      headers={'kid': KID})


def _event(token=None, header_name='Authorization'):
    headers = {header_name: f'Bearer {token}'} if token else {}
    return {'requestContext': {}, 'headers': headers}


# ── The gateway-authorizer path (unchanged, still trusted as-is) ────


def test_gateway_authorizer_claims_are_used_without_reverification():
    event = {'requestContext': {'authorizer': {'claims': {
        'sub': 'u-1', 'email': 'admin@example.com',
    }}}}
    claims, err = auth.get_user_from_event(event)
    assert err is None
    assert claims['email'] == 'admin@example.com'


def test_no_claims_and_no_bearer_token_is_unauthorized():
    claims, err = auth.get_user_from_event({'requestContext': {}, 'headers': {}})
    assert claims is None
    assert err['statusCode'] == 401


# ── The Bearer-token fallback ────────────────────────────────────────


def test_a_valid_bearer_id_token_is_accepted(signing_env):
    token = _token(signing_env)
    claims, err = auth.get_user_from_event(_event(token))
    assert err is None
    assert claims['sub'] == 'user-123'
    assert claims['email'] == 'test@example.com'
    assert claims['cognito:groups'] == ['admins']


def test_lowercase_header_name_is_accepted(signing_env):
    token = _token(signing_env)
    claims, err = auth.get_user_from_event(_event(token, header_name='authorization'))
    assert err is None
    assert claims['sub'] == 'user-123'


def test_an_access_token_is_refused(signing_env):
    # Access tokens carry no email/groups claims worth trusting here, and
    # DctechAuth.authorizedFetch never sends one anyway.
    token = _token(signing_env, token_use='access')
    claims, err = auth.get_user_from_event(_event(token))
    assert claims is None
    assert err['statusCode'] == 401


def test_an_expired_token_is_refused(signing_env):
    token = _token(signing_env, iat=int(time.time()) - 7200,
                   exp=int(time.time()) - 3600)
    claims, err = auth.get_user_from_event(_event(token))
    assert claims is None
    assert err['statusCode'] == 401


def test_a_token_for_the_wrong_audience_is_refused(signing_env):
    token = _token(signing_env, aud='someone-elses-client-id')
    claims, err = auth.get_user_from_event(_event(token))
    assert claims is None
    assert err['statusCode'] == 401


def test_a_token_from_the_wrong_issuer_is_refused(signing_env):
    token = _token(signing_env, iss='https://cognito-idp.us-east-1.amazonaws.com/us-east-1_IMPOSTER')
    claims, err = auth.get_user_from_event(_event(token))
    assert claims is None
    assert err['statusCode'] == 401


def test_a_tampered_signature_is_refused(signing_env):
    token = _token(signing_env)
    tampered = token[:-4] + ('AAAA' if not token.endswith('AAAA') else 'BBBB')
    claims, err = auth.get_user_from_event(_event(tampered))
    assert claims is None
    assert err['statusCode'] == 401


def test_a_token_signed_by_the_wrong_key_is_refused(signing_env):
    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _token(other_private_key)
    claims, err = auth.get_user_from_event(_event(token))
    assert claims is None
    assert err['statusCode'] == 401


def test_a_non_bearer_authorization_header_is_ignored(signing_env):
    event = {'requestContext': {}, 'headers': {'Authorization': 'Basic abc123'}}
    claims, err = auth.get_user_from_event(event)
    assert claims is None
    assert err['statusCode'] == 401


def test_email_falls_back_to_cognito_username_when_absent(signing_env):
    token = _token(signing_env, email=None)
    del_email_token = jwt.decode(token, options={'verify_signature': False})
    # Rebuild without the email claim entirely (can't just set it to None —
    # jwt.encode would still emit the key with a null value).
    del_email_token.pop('email', None)
    del_email_token['cognito:username'] = 'fallback-username'
    token = jwt.encode(del_email_token, signing_env, algorithm='RS256',
                       headers={'kid': KID})
    claims, err = auth.get_user_from_event(_event(token))
    assert err is None
    assert claims['email'] == 'fallback-username'


def test_gateway_authorizer_claims_win_over_a_bearer_token_when_both_present(signing_env):
    # A route with the gateway authorizer attached never needs the fallback;
    # if it were consulted anyway, a request carrying both should still
    # trust the pre-validated claims, not silently swap identities.
    token = _token(signing_env)
    event = _event(token)
    event['requestContext'] = {'authorizer': {'claims': {
        'sub': 'gateway-user', 'email': 'gateway@example.com',
    }}}
    claims, err = auth.get_user_from_event(event)
    assert err is None
    assert claims['sub'] == 'gateway-user'
