"""Small SSRF-hardening helpers.

Two jobs:

1. ``safe_urlopen`` -- ``urllib.request.urlopen`` with an http(s)-only scheme guard.
   urllib silently supports file://, ftp://, data: and more; a *dynamic* URL that an
   attacker could ever steer to file:// would read local files (semgrep
   dynamic-urllib-use-detected). Every urllib fetch in the backend routes through
   here so the scheme is always checked. Transparent for normal http/https URLs
   (ComfyUI localhost API, model downloads, Aether node URLs).

2. ``resolve_validated`` / ``pinned_base`` -- the SSRF check plus the address PIN used
   by every outbound call whose URL comes from a request body (skill_api browse/fetch,
   both the direct-peer path and the relay path). Validating a hostname and then
   letting the HTTP client re-resolve it leaves a DNS-rebinding TOCTOU: the name can
   answer with a public address for the check and an internal one for the request.
   These helpers hand back the exact address that was validated, so the caller talks
   to that and nothing else.

   This lives in net_guard rather than in skill_api so there is ONE implementation of
   the policy. It raises a plain ``UrlNotAllowed`` (a ValueError), never an
   HTTPException -- net_guard must stay importable by non-FastAPI callers.
"""
import ipaddress
import socket
import urllib.request
from urllib.parse import urlparse

_ALLOWED_SCHEMES = ("http", "https")


class UrlNotAllowed(ValueError):
    """Raised when a caller-supplied URL fails the SSRF check."""


def safe_urlopen(target, **kwargs):
    """Drop-in replacement for ``urllib.request.urlopen`` that rejects any non-
    http(s) scheme before opening. ``target`` may be a URL string or a
    ``urllib.request.Request`` (both forms are used across the backend)."""
    if isinstance(target, urllib.request.Request):
        url = target.full_url
    else:
        url = str(target)
    scheme = urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError("refusing to fetch non-http(s) URL (scheme=%r)" % scheme)
    return urllib.request.urlopen(target, **kwargs)


def resolve_validated(url):
    """Parse + validate a URL and return (scheme, host, port, ip, netloc).

    Validates EVERY address ``host`` resolves to (getaddrinfo, not just
    gethostbyname's first record -- closes the multi-A-record bypass) and returns the
    exact IP so the caller can PIN it: the connection then uses the same address that
    was validated, which closes the DNS-rebinding TOCTOU. Raises ``UrlNotAllowed`` on
    a non-http(s) scheme, a missing host, a resolution failure, or any private,
    loopback, link-local, reserved, multicast or unspecified address."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UrlNotAllowed("url must be http or https")
    host = parsed.hostname
    if not host:
        raise UrlNotAllowed("url missing host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise UrlNotAllowed("could not resolve host")
    ip_pin = None
    for info in infos:
        ip_s = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_s)
        except ValueError:
            continue
        # is_unspecified blocks 0.0.0.0 / ::; is_multicast blocks 224.0.0.0/4.
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise UrlNotAllowed("target address not allowed")
        if ip_pin is None:
            ip_pin = ip_s
    if ip_pin is None:
        raise UrlNotAllowed("could not resolve host")
    return parsed.scheme, host, port, ip_pin, parsed.netloc


def pinned_base(url):
    """Validate ``url`` and return ``(base_url, host_header, sni_hostname)``.

    ``base_url`` addresses the VALIDATED IP literal, so the caller's HTTP client never
    re-resolves the name. ``host_header`` is the original netloc, so virtual hosting
    and the peer's own routing still work. ``sni_hostname`` is the hostname for https
    (None for http) so TLS SNI and certificate verification run against the name
    rather than the IP. Raises ``UrlNotAllowed``, same as ``resolve_validated``."""
    scheme, host, port, ip, netloc = resolve_validated(url)
    ip_host = ("[%s]" % ip) if ":" in ip else ip   # bracket IPv6 literals
    return ("%s://%s:%d" % (scheme, ip_host, port), netloc,
            host if scheme == "https" else None)
