"""Small SSRF-hardening helper: urllib.request.urlopen with an http(s)-only scheme
guard.

urllib silently supports file://, ftp://, data: and more; a *dynamic* URL that an
attacker could ever steer to file:// would read local files (semgrep
dynamic-urllib-use-detected). Every urllib fetch in the backend routes through here
so the scheme is always checked. This is transparent for normal http/https URLs
(ComfyUI localhost API, model downloads, Aether node URLs) -- only non-http(s)
schemes are rejected.
"""
import urllib.request
from urllib.parse import urlparse

_ALLOWED_SCHEMES = ("http", "https")


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
