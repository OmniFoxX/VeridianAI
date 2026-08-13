# -*- coding: utf-8 -*-
"""v2.14 per-profile API token ownership.

Covers the HIPAA attribution gap documented in STANDARDS_ALIGNMENT.md: API
requests used to arrive as "someone holding the default key", resolving to the
owner's namespace with nothing in the record distinguishing one caller from
another.

The tests that matter most are the CONTAINMENT ones -- a token must reach its
own profile and no other -- and the MIGRATION one, because an upgrade that
silently promotes an unowned token to owner is the failure this release exists
to prevent.

    python test_api_ownership.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


# Point the keystore at a scratch file BEFORE auth resolves anything.
_tmp = tempfile.mkdtemp(prefix="veridian_authtest_")
os.environ["VERIDIAN_DATA_DIR"] = _tmp

import auth  # noqa: E402

auth.KEYSTORE_PATH = Path(_tmp) / ".api_keystore.json"


def reset(entries=None):
    """Write a keystore directly, bypassing the API under test."""
    store = auth._empty_keystore()
    if entries:
        store["tokens"] = entries
    auth._save_keystore(store)
    return store


print("== binding ==")

reset()
tok_owner = auth.issue_token(None, "owner key")
tok_alice = auth.issue_token("alice", "alice key")
tok_bob = auth.issue_token("bob", "bob key")

store = auth._load_keystore()
p_owner = auth._verify_token(tok_owner, store)
p_alice = auth._verify_token(tok_alice, store)
p_bob = auth._verify_token(tok_bob, store)

ok("a verified token yields a principal, not a bare scope list",
   isinstance(p_owner, dict) and "owner_ns" in p_owner, p_owner)
ok("the owner's token has owner_ns None", p_owner["owner_ns"] is None)
ok("alice's token names alice", p_alice["owner_ns"] == "alice")
ok("bob's token names bob", p_bob["owner_ns"] == "bob")
ok("issued tokens are marked bound",
   all(p["bound"] for p in (p_owner, p_alice, p_bob)))
ok("scopes still travel with the principal", p_alice["scopes"] == ["*"])
ok("an unknown token verifies as nothing",
   auth._verify_token("ora_not-a-real-token", store) is None)
ok("a raw token is never written to disk",
   tok_alice not in auth.KEYSTORE_PATH.read_text(encoding="utf-8"))

print("\n== containment (the access-control half of the gap) ==")

ok("alice's token does not resolve to the owner",
   p_alice["owner_ns"] is not None)
ok("alice's token does not resolve to bob",
   p_alice["owner_ns"] != p_bob["owner_ns"])

# Mirrors main._is_owner's decision for an API request.
def is_owner_for(principal):
    if principal is None:
        return None
    if not principal.get("bound"):
        return False
    return principal.get("owner_ns") is None

ok("owner token passes the owner gate", is_owner_for(p_owner) is True)
ok("alice's token FAILS the owner gate", is_owner_for(p_alice) is False)
ok("bob's token FAILS the owner gate", is_owner_for(p_bob) is False)

print("\n== migration of a pre-v2.14 keystore ==")

legacy = auth._make_token_entry(auth._new_token(), ["*"], "default")
del legacy[auth.OWNER_NS_KEY]          # exactly what a 2.13 keystore holds
reset([legacy])

store = auth._load_keystore()
ok("an unbound entry is detectable before migration",
   auth.OWNER_NS_KEY not in store["tokens"][0])

# An unbound token must NOT pass as owner while it is still unbound -- that is
# the fail-open this release closes.
unbound_principal = auth._principal(store["tokens"][0])
ok("an UNBOUND token is not bound", unbound_principal["bound"] is False)
ok("an UNBOUND token is refused the owner gate (fail closed)",
   is_owner_for(unbound_principal) is False)

n = auth.migrate_ownership(store)
ok("migration binds the legacy entry", n == 1)
ok("it becomes the OWNER's, not nobody's",
   store["tokens"][0][auth.OWNER_NS_KEY] is None)
ok("and it is stamped, so the keystore carries its own history",
   bool(store["tokens"][0].get("migrated_from_unowned")))
ok("after migration it is bound",
   auth._principal(store["tokens"][0])["bound"] is True)
ok("migration is idempotent", auth.migrate_ownership(store) == 0)

print("\n== per-profile rotation ==")

reset()
t_owner = auth.issue_token(None, "owner key")
t_alice = auth.issue_token("alice", "alice key")
t_bob = auth.issue_token("bob", "bob key")

new_alice = auth.rotate_token_for("alice")
store = auth._load_keystore()

ok("alice's old token stops working",
   auth._verify_token(t_alice, store) is None)
ok("alice's new token works",
   (auth._verify_token(new_alice, store) or {}).get("owner_ns") == "alice")
ok("bob is UNAFFECTED by alice's rotation",
   (auth._verify_token(t_bob, store) or {}).get("owner_ns") == "bob")
ok("the owner is UNAFFECTED by alice's rotation",
   auth._verify_token(t_owner, store) is not None)

new_owner = auth.rotate_token_for(None)
store = auth._load_keystore()
ok("rotating the owner revokes the owner's old token",
   auth._verify_token(t_owner, store) is None)
ok("and still leaves bob alone",
   auth._verify_token(t_bob, store) is not None)
ok("rotate_default_token() still works for the standalone script",
   auth.rotate_default_token().startswith(auth.TOKEN_PREFIX))

print("\n== revocation on account deletion ==")

reset()
auth.issue_token(None, "owner key")
t_alice = auth.issue_token("alice", "alice key")
t_alice2 = auth.issue_token("alice", "alice second key")
t_bob = auth.issue_token("bob", "bob key")

removed = auth.revoke_tokens_for("alice")
store = auth._load_keystore()
ok("both of alice's tokens are revoked", removed == 2, removed)
ok("alice's first token is dead", auth._verify_token(t_alice, store) is None)
ok("alice's second token is dead", auth._verify_token(t_alice2, store) is None)
ok("bob survives alice's deletion", auth._verify_token(t_bob, store) is not None)

try:
    auth.revoke_tokens_for(None)
    ok("revoking the owner by namespace is refused", False, "it was allowed")
except ValueError:
    ok("revoking the owner by namespace is refused", True)

print("\n== listing never leaks material ==")

meta = auth.list_tokens("bob")
ok("list_tokens filters to one profile",
   len(meta) == 1 and meta[0]["owner_ns"] == "bob", meta)
ok("no hash is exposed", all("hash" not in m for m in auth.list_tokens(all_profiles=True)))
ok("no raw token is exposed",
   all("token" not in m for m in auth.list_tokens(all_profiles=True)))

print("\n== single-user installs are unchanged ==")

reset()
t = auth.issue_token(None, "default (auto-generated on first boot)")
store = auth._load_keystore()
pr = auth._verify_token(t, store)
ok("the solo user's token is the owner's", pr["owner_ns"] is None)
ok("and passes the owner gate", is_owner_for(pr) is True)
ok("a fresh keystore's first-boot token is bound, not legacy",
   pr["bound"] is True)

bad = [n for n, c in _results if not c]
print("\n%d/%d passed." % (len(_results) - len(bad), len(_results)))
if bad:
    print("FAILED:")
    for n in bad:
        print("  - " + n)
    sys.exit(1)
