# -*- coding: utf-8 -*-
"""The namespace token rule, in one place.

A namespace (`ns`) is the per-profile directory segment under
`sage_data/users/<ns>`. users.py mints it in `_ns_for()` by scrubbing a
username to `[a-zA-Z0-9_-]`, truncating to 40 characters and appending 8 hex
digits -- so every real namespace already satisfies NS_RE by construction.
Nothing here narrows what a profile may be called; it asserts what is already
true, at the place that depends on it being true.

WHY THIS MODULE EXISTS
----------------------
The rule used to be enforced in main.py (`_safe_ns`, applied per route) and
merely ASSERTED, in a comment, at the place that actually builds the path
(`sage_engine.user_data_dir`). One layer of separation between a check and the
code depending on it -- and it had already drifted in two places by v2.15:

  - `profile_keys._user_dir()` fell back to `base / "users" / str(ns)` with no
    validation at all whenever importing sage_engine raised. Every keywrap
    path flowed through that function.
  - the downloads WRITE route applied `_safe_ns`; the READ and DELETE routes,
    forty lines above it, did not. The delete route ends in `path.unlink()`.

CodeQL raised 21 `py/path-injection` alerts across keywrap.py, profile_keys.py,
atrest.py and main.py, and was right to: the paths genuinely did depend on a
value whose only guarantee lived in another file. "Validated upstream" is not a
property you can rely on unless the validated value is the one that arrives.

So the rule moves to the point of use. It raises a plain ValueError subclass
rather than an HTTPException, so the modules that need it most -- keywrap,
profile_keys, sage_engine, the daemons -- can import it without dragging in
FastAPI; `main.py._safe_ns` translates it to HTTP 400 and the API contract is
unchanged.

HIPAA 164.312(a)(1) access control: this string IS a profile's data boundary.

Pure-ASCII source.
"""
import re

__all__ = ["NS_RE", "InvalidNamespace", "safe_ns", "is_valid"]

# [A-Za-z0-9_-], 1..64 characters. No slash, backslash, dot, colon or NUL, so a
# namespace can never contribute '../', a drive letter, a UNC prefix or an
# absolute path to a filesystem path built from it.
#
# \A and \Z, NOT ^ and $. In Python `$` matches at the end of the string OR
# immediately before a trailing newline, so the original `^[A-Za-z0-9_-]{1,64}$`
# ACCEPTED "alice\n" -- which is a different directory from "alice" on any
# POSIX filesystem. That is a containment bug, not a cosmetic one: the same
# logical profile would resolve to two different data directories depending on
# whether a newline survived whatever produced the string. Caught by
# test_ns_guard.test_nul_empty_and_whitespace_rejected; it had been in
# main.py._NS_RE since the guard was written. \Z anchors at the true end of
# string and has no newline exception.
NS_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


class InvalidNamespace(ValueError):
    """A namespace that does not satisfy NS_RE."""


def is_valid(ns) -> bool:
    """True if `ns` may be used to build a path.

    None is NOT valid here. Callers that treat None as "the owner / shared
    store" must say so explicitly rather than letting it pass a truthiness
    check -- confusing "no namespace" with "a valid namespace" is how
    owner-scoped and user-scoped paths get swapped.
    """
    return ns is not None and bool(NS_RE.match(str(ns)))


def safe_ns(ns):
    """Return `ns` unchanged if valid. None passes through (owner/single-user).

    Raises InvalidNamespace otherwise -- deliberately, rather than returning
    None or a scrubbed value. A caller handed None back would build an
    OWNER-scoped path for what was meant to be a user-scoped one: a containment
    failure that looks exactly like success. A scrubbed value would silently
    read or write somebody else's directory.
    """
    if ns is None:
        return None
    ns = str(ns)
    if not NS_RE.match(ns):
        raise InvalidNamespace("invalid namespace")
    return ns
