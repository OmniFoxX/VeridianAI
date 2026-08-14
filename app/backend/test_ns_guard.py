"""Self-running tests for ns_guard -- the namespace rule that bounds a profile's
data directory.

Run:  python test_ns_guard.py

Covers the rule itself, the two places v2.15 moved enforcement TO, and the one
property the whole change depends on: that every namespace users.py can mint
already satisfies the rule, so enforcing it cannot lock anybody out of their
own data.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ns_guard


# ---------------------------------------------------------------- the rule --

def test_valid_namespaces_pass_through_unchanged():
    for ns in ("alice", "bob_1a2b3c4d", "A-Z_az-09", "x", "u" * 64):
        assert ns_guard.safe_ns(ns) == ns, ns
        assert ns_guard.is_valid(ns), ns


def test_none_is_the_owner_and_passes_through():
    assert ns_guard.safe_ns(None) is None
    assert ns_guard.is_valid(None) is False   # None is not a namespace


def test_traversal_rejected():
    for ns in ("..", ".", "../evil", "a/b", "a\\b", "..\\..\\evil",
               "%2e%2e", "alice/../bob"):
        try:
            ns_guard.safe_ns(ns)
        except ns_guard.InvalidNamespace:
            continue
        raise AssertionError("accepted traversal: %r" % ns)


def test_absolute_and_device_paths_rejected():
    for ns in ("C:", "C:\\Windows", "/etc/passwd", "\\\\server\\share",
               "~", "$HOME", "CON", "a:b"):
        if ns == "CON":
            # A Windows reserved DEVICE NAME is alphanumeric, so NS_RE accepts
            # it. Recorded deliberately: it is not a traversal, and a directory
            # called CON is a Windows create-time failure, not a containment
            # break. Flagged here so nobody later reads its absence as an oversight.
            assert ns_guard.is_valid(ns)
            continue
        try:
            ns_guard.safe_ns(ns)
        except ns_guard.InvalidNamespace:
            continue
        raise AssertionError("accepted absolute/device path: %r" % ns)


def test_nul_empty_and_whitespace_rejected():
    """`"alice\n"` is the load-bearing case here. Python's `$` matches at the end
    of the string OR just before a trailing newline, so the original
    `^[A-Za-z0-9_-]{1,64}$` accepted it -- and "alice\n" is a DIFFERENT directory
    from "alice" on any POSIX filesystem, so one logical profile could resolve to
    two data directories. NS_RE uses \\A...\\Z for exactly this reason. Do not
    "simplify" it back to ^...$."""
    for ns in ("", " ", "a b", "a\x00b", "\x00", "\n", "alice\n", "alice\r\n",
               "alice\t", " alice", "alice "):
        try:
            ns_guard.safe_ns(ns)
        except ns_guard.InvalidNamespace:
            continue
        raise AssertionError("accepted: %r" % ns)


def test_length_boundary():
    assert ns_guard.safe_ns("u" * 64) == "u" * 64
    try:
        ns_guard.safe_ns("u" * 65)
    except ns_guard.InvalidNamespace:
        return
    raise AssertionError("accepted a 65-character namespace")


def test_never_scrubs_only_accepts_or_raises():
    """A scrubbing guard would silently redirect one profile's reads into
    another's directory. This one must return the input or refuse."""
    try:
        out = ns_guard.safe_ns("al/ice")
    except ns_guard.InvalidNamespace:
        return
    raise AssertionError("scrubbed to %r instead of raising" % (out,))


# ------------------------------------------- the places enforcement moved to --

def test_user_data_dir_enforces_the_rule():
    import sage_engine
    assert sage_engine.user_data_dir(None) is None
    try:
        sage_engine.user_data_dir("../../etc")
    except ns_guard.InvalidNamespace:
        return
    raise AssertionError("user_data_dir built a path from a traversal namespace")


def test_profile_keys_fails_closed_without_sage_engine():
    """REGRESSION: _user_dir used to fall back to `base / "users" / str(ns)`
    with no validation whenever importing sage_engine raised. Every keywrap
    path flows through here, so that branch is why six keywrap alerts were
    real. Simulate the failure and confirm it now refuses."""
    import profile_keys
    saved = sys.modules.get("sage_engine", "<absent>")
    sys.modules["sage_engine"] = None          # makes `import sage_engine` raise
    try:
        try:
            profile_keys._user_dir("../../evil")
        except ns_guard.InvalidNamespace:
            pass
        else:
            raise AssertionError("fallback built a path from an invalid namespace")
        # ...and a VALID namespace still resolves, so the resilience is intact.
        d = profile_keys._user_dir("alice")
        assert d is None or d.name == "alice", d
    finally:
        if saved == "<absent>":
            sys.modules.pop("sage_engine", None)
        else:
            sys.modules["sage_engine"] = saved


def test_keywrap_path_refuses_an_invalid_namespace():
    import profile_keys
    try:
        profile_keys.keywrap_path("../../etc/shadow")
    except ns_guard.InvalidNamespace:
        return
    raise AssertionError("keywrap_path accepted a traversal namespace")


# --------------------------------------------- the property it all rests on --

def test_every_namespace_users_can_mint_is_valid():
    """Enforcement is only safe if no real profile can violate the rule.
    users._ns_for scrubs to [a-zA-Z0-9_-], truncates to 40 and appends 8 hex
    digits -- so it cannot produce anything NS_RE rejects. If this ever fails,
    enforcing the rule would lock a real person out of their own data."""
    import users
    nasty = [
        "alice", "Bob Smith", "../../etc/passwd", "C:\\Windows\\System32",
        "user@example.com", "  spaced  ", "\u00e9l\u00e8ve", "\u4e2d\u6587",
        "a" * 200, "", None, "NUL\x00byte", "dots...dots", "semi;colon",
        "quote'\"quote", "%2e%2e%2f", "tab\there", "new\nline",
    ]
    for name in nasty:
        ns = users._ns_for(name)
        assert ns_guard.is_valid(ns), "minted an invalid namespace %r from %r" % (ns, name)


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    p = f = 0
    for fn in fns:
        try:
            fn(); p += 1; print("PASS", fn.__name__)
        except Exception:
            f += 1; print("FAIL", fn.__name__); traceback.print_exc()
    print("\n%d passed, %d failed" % (p, f)); sys.exit(1 if f else 0)
