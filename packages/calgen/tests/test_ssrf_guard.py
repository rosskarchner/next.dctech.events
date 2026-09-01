"""safe_get() must refuse to connect to a private/internal address, whether
that's the URL's own host or something it redirects to, and must catch it
even when a DNS answer only turns unsafe on the connection lookup (not the
lookup an earlier, separate check would have made) — see ssrf_guard.py's
module docstring for why the check is folded into the resolution itself
rather than done as a step beforehand.

Run: python -m pytest test_ssrf_guard.py
"""
import socket

import pytest

from calgen.ssrf_guard import UnsafeURLError, safe_get


class _FakeResponse:
    def __init__(self, status_code=200, location=None):
        self.status_code = status_code
        self.headers = {'Location': location} if location else {}


def _fake_getaddrinfo(answers):
    """answers: {hostname: ip_str}"""
    def getaddrinfo(host, *args, **kwargs):
        ip = answers.get(host)
        if ip is None:
            raise socket.gaierror(f'no fake answer configured for {host}')
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]
    return getaddrinfo


def test_public_address_is_allowed(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', _fake_getaddrinfo({'example.com': '93.184.216.34'}))
    monkeypatch.setattr('calgen.ssrf_guard.requests.get', lambda *a, **k: _FakeResponse())

    response = safe_get('https://example.com/feed.ics')

    assert response.status_code == 200


@pytest.mark.parametrize('ip', [
    '169.254.169.254',  # cloud metadata endpoint
    '127.0.0.1',
    '10.0.0.5',
    '172.16.0.5',
    '192.168.1.1',
    '::1',
    '0.0.0.0',
])
def test_private_or_internal_address_is_rejected(monkeypatch, ip):
    monkeypatch.setattr(socket, 'getaddrinfo', _fake_getaddrinfo({'evil.example': ip}))
    monkeypatch.setattr('calgen.ssrf_guard.requests.get', lambda *a, **k: pytest.fail('should never connect'))

    with pytest.raises(UnsafeURLError):
        safe_get('https://evil.example/feed.ics')


def test_non_http_scheme_is_rejected(monkeypatch):
    monkeypatch.setattr('calgen.ssrf_guard.requests.get', lambda *a, **k: pytest.fail('should never connect'))

    with pytest.raises(UnsafeURLError):
        safe_get('file:///etc/passwd')


def test_redirect_to_a_private_address_is_rejected(monkeypatch):
    answers = {'public.example': '93.184.216.34', 'internal.example': '10.0.0.5'}
    monkeypatch.setattr(socket, 'getaddrinfo', _fake_getaddrinfo(answers))

    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url == 'https://public.example/feed.ics':
            return _FakeResponse(status_code=302, location='https://internal.example/steal')
        return _FakeResponse()

    monkeypatch.setattr('calgen.ssrf_guard.requests.get', fake_get)

    with pytest.raises(UnsafeURLError):
        safe_get('https://public.example/feed.ics')

    # the redirect target must never have been reached
    assert calls == ['https://public.example/feed.ics']


def test_redirect_to_a_public_address_is_followed(monkeypatch):
    answers = {'public.example': '93.184.216.34', 'also-public.example': '93.184.216.35'}
    monkeypatch.setattr(socket, 'getaddrinfo', _fake_getaddrinfo(answers))

    def fake_get(url, **kwargs):
        if url == 'https://public.example/feed.ics':
            return _FakeResponse(status_code=302, location='https://also-public.example/feed.ics')
        return _FakeResponse(status_code=200)

    monkeypatch.setattr('calgen.ssrf_guard.requests.get', fake_get)

    response = safe_get('https://public.example/feed.ics')

    assert response.status_code == 200


def test_the_guard_stays_active_through_the_actual_connect(monkeypatch):
    """The up-front lookup isn't the whole defense — the guard it runs under
    must still be installed when requests/urllib3 does its own connection-time
    resolution, or a DNS answer that changes between the two (rebinding)
    would sail through on the second, unchecked call. Simulated here by
    making requests.get itself perform a getaddrinfo lookup, as urllib3
    would, and confirming it's caught even though the up-front check for a
    *different* host already passed.
    """
    answers = {'rebind.example': '169.254.169.254'}
    monkeypatch.setattr(socket, 'getaddrinfo', _fake_getaddrinfo(answers))

    def fake_get(url, **kwargs):
        # what urllib3 would do to actually open the connection
        socket.getaddrinfo('rebind.example', 443)
        return _FakeResponse()

    monkeypatch.setattr('calgen.ssrf_guard.requests.get', fake_get)

    with pytest.raises(UnsafeURLError):
        safe_get('https://rebind.example/feed.ics')


def test_too_many_redirects_gives_up(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', _fake_getaddrinfo({'loop.example': '93.184.216.34'}))
    monkeypatch.setattr(
        'calgen.ssrf_guard.requests.get',
        lambda *a, **k: _FakeResponse(status_code=302, location='https://loop.example/next'),
    )

    with pytest.raises(UnsafeURLError):
        safe_get('https://loop.example/start')


def test_getaddrinfo_is_restored_after_a_successful_call(monkeypatch):
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, 'getaddrinfo', _fake_getaddrinfo({'example.com': '93.184.216.34'}))
    monkeypatch.setattr('calgen.ssrf_guard.requests.get', lambda *a, **k: _FakeResponse())

    safe_get('https://example.com/feed.ics')

    assert socket.getaddrinfo is not real  # still monkeypatched by the test itself
    monkeypatch.undo()
    assert socket.getaddrinfo is real


def test_getaddrinfo_is_restored_after_a_rejected_call(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', _fake_getaddrinfo({'evil.example': '127.0.0.1'}))

    with pytest.raises(UnsafeURLError):
        safe_get('https://evil.example/feed.ics')

    # the guard's own patch must have unwound even though the call raised,
    # leaving the test's patch (not some guard-installed wrapper) in place
    assert socket.getaddrinfo.__name__ == _fake_getaddrinfo({}).__name__
