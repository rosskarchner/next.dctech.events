"""Guards HTTP fetches of externally-supplied URLs (group iCal feeds, scraped
Meetup event pages) against SSRF.

A malicious or compromised group feed can point its URL — or a redirect
target behind it — at the Lambda's own metadata endpoint or internal
network. Checking the scheme (see db.py's is_safe_url) isn't enough: it says
nothing about where the hostname actually resolves.

safe_get() closes the DNS-rebinding gap a plain check-then-fetch approach
would leave open (attacker's DNS answers "public" for the pre-flight lookup,
then "private" microseconds later for the real connection): the validating
wrapper installed by _guard_dns_resolution() stays active through the whole
request, not just through this module's own up-front lookup, so whatever
resolution urllib3 performs to actually open the connection is checked in
place too — a DNS answer that changes between the two is still caught,
because both calls go through the same guarded getaddrinfo.
"""
import contextlib
import ipaddress
import socket
from urllib.parse import urlparse

import requests

MAX_REDIRECTS = 5


class UnsafeURLError(ValueError):
    """Raised when a URL's scheme or resolved address isn't safe to fetch."""


def _is_unsafe_ip(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # can't parse it, don't trust it
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


@contextlib.contextmanager
def _guard_dns_resolution():
    """Validate every socket.getaddrinfo() resolution for the life of this
    block, raising UnsafeURLError the instant one resolves to a private,
    loopback, link-local, multicast, reserved, or unspecified address.

    Patched at the socket module level (not just for one hostname) so a
    redirect to a different host is checked exactly the same way, and
    because the validation happens inside the same call urllib3 uses to
    open the connection, there's no window between "checked" and "used"
    for DNS to answer differently.
    """
    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        results = real_getaddrinfo(host, *args, **kwargs)
        for family, socktype, proto, canonname, sockaddr in results:
            if _is_unsafe_ip(sockaddr[0]):
                raise UnsafeURLError(
                    f'{host!r} resolves to a private/internal address ({sockaddr[0]})')
        return results

    socket.getaddrinfo = guarded_getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo


def safe_get(url, **kwargs):
    """requests.get(), but refuses to connect to a private/internal address —
    including one reached only via a redirect.

    Follows redirects manually (capped at MAX_REDIRECTS) rather than passing
    allow_redirects=True, so each hop's URL gets its own scheme + DNS check
    before requests is allowed to connect to it.
    """
    kwargs.setdefault('allow_redirects', False)
    next_url = url
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(next_url)
        if parsed.scheme not in ('http', 'https'):
            raise UnsafeURLError(f'unsupported scheme: {parsed.scheme!r}')
        if not parsed.hostname:
            raise UnsafeURLError('URL has no host')

        with _guard_dns_resolution():
            # Resolve (and validate) up front so a bad address is caught
            # before requests/urllib3 ever opens a socket, not just when it
            # happens to. The guard stays active through the actual request
            # below too, so urllib3's own connection-time resolution is
            # checked again in place — a DNS answer that changes between
            # this lookup and that one is still caught, because it's this
            # same guarded getaddrinfo that validates it.
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            socket.getaddrinfo(parsed.hostname, port)
            response = requests.get(next_url, **kwargs)

        if response.status_code in (301, 302, 303, 307, 308) and 'Location' in response.headers:
            next_url = requests.compat.urljoin(next_url, response.headers['Location'])
            continue
        return response
    raise UnsafeURLError(f'too many redirects fetching {url}')
