# -*- coding: utf-8 -*-
"""v2.14.2 -- the DEPENDENCY-INJECTION link, over real HTTP.

WHAT THIS COVERS THAT THE OTHER TESTS DO NOT
--------------------------------------------
test_api_ownership.py proves the keystore binds tokens to profiles.
test_mcp_containment.py proves a namespace reaches the storage layer.

Neither proves the link between them: that auth.require_scope, running as a
FastAPI dependency on a real request, publishes the principal where the
endpoint can read it. That link is the whole mechanism -- and it is exactly
the kind of thing that looks obviously fine and silently is not, which is how
the endpoints ended up with no `request` parameter in the first place.

So this drives a real ASGI app through TestClient with real tokens.

WHAT IT STILL DOES NOT COVER
----------------------------
The full pipeline. These are the real auth dependency and the real
namespace-resolution logic, mounted on a minimal app -- not main.app with its
inference tiers. Two live profiles chatting through /v1/chat/completions on a
running install remains a manual check.

    python test_api_http.py          (needs fastapi + httpx, both in requirements)
"""
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


try:
    from fastapi import FastAPI, Depends, Request
    from starlette.testclient import TestClient
except Exception as e:                                  # pragma: no cover
    print("SKIP: fastapi/httpx not available here (%s)" % e)
    print("Both are in backend/requirements.txt -- run this on the install.")
    sys.exit(0)

_tmp = tempfile.mkdtemp(prefix="veridian_http_")
os.environ["VERIDIAN_DATA_DIR"] = _tmp

import auth                                              # noqa: E402
auth.KEYSTORE_PATH = Path(_tmp) / ".api_keystore.json"

store = auth._empty_keystore()
auth._save_keystore(store)
TOK_OWNER = auth.issue_token(None, "owner key")
TOK_ALICE = auth.issue_token("alice", "alice key")
TOK_BOB = auth.issue_token("bob", "bob key")

# A pre-v2.14 entry: no owner_ns key at all.
_st = auth._load_keystore()
_legacy_raw = auth._new_token()
_entry = auth._make_token_entry(_legacy_raw, ["*"], "legacy")
del _entry[auth.OWNER_NS_KEY]
_st["tokens"].append(_entry)
auth._save_keystore(_st)
TOK_LEGACY = _legacy_raw

app = FastAPI()
app.state.keystore = auth._load_keystore()


def _resolve_ns(request):
    """Byte-for-byte main._session_ns's API branch."""
    pr = getattr(request.state, "api_principal", None)
    if pr is not None:
        return pr.get("owner_ns") if pr.get("owner_ns") else None
    return None


def _is_owner(request):
    """Byte-for-byte main._is_owner's API branch."""
    pr = getattr(request.state, "api_principal", None)
    if pr is not None:
        if not pr.get("bound"):
            return False
        return pr.get("owner_ns") is None
    return False


@app.post("/probe", dependencies=[Depends(auth.require_scope("chat:write"))])
async def probe(request: Request):
    return {"ns": _resolve_ns(request), "is_owner": _is_owner(request)}


@app.post("/narrow", dependencies=[Depends(auth.require_scope("admin:*"))])
async def narrow(request: Request):
    return {"ok": True}


c = TestClient(app)


def call(tok, path="/probe"):
    h = {"Authorization": "Bearer " + tok} if tok else {}
    return c.post(path, headers=h, json={})


print("== the principal reaches the endpoint over real HTTP ==")
for tok, label, want_ns, want_owner in [
        (TOK_OWNER, "owner", None, True),
        (TOK_ALICE, "alice", "alice", False),
        (TOK_BOB, "bob", "bob", False)]:
    r = call(tok)
    body = r.json() if r.status_code == 200 else {}
    ok("%-6s token -> ns=%r, is_owner=%s" % (label, want_ns, want_owner),
       r.status_code == 200 and body.get("ns") == want_ns
       and body.get("is_owner") is want_owner, (r.status_code, body))

print("\n== containment between profiles ==")
ok("alice's token never resolves to bob",
   call(TOK_ALICE).json().get("ns") != call(TOK_BOB).json().get("ns"))
ok("neither non-owner resolves to the owner",
   call(TOK_ALICE).json().get("ns") is not None
   and call(TOK_BOB).json().get("ns") is not None)
ok("neither non-owner passes the owner gate",
   call(TOK_ALICE).json().get("is_owner") is False
   and call(TOK_BOB).json().get("is_owner") is False)

print("\n== an unbound legacy token fails CLOSED ==")
r = call(TOK_LEGACY)
ok("it authenticates (so an upgrade does not break the user)",
   r.status_code == 200, r.status_code)
ok("but is REFUSED owner privileges",
   r.json().get("is_owner") is False, r.json())

print("\n== rejection paths ==")
ok("no token -> 401", call(None).status_code == 401, call(None).status_code)
ok("garbage token -> 401",
   call("ora_nope").status_code == 401, call("ora_nope").status_code)
# NOTE: every token issue_token() mints carries scopes ["*"], which satisfies
# everything -- so alice's ordinary token reaches /narrow legitimately. That is
# consistent with the design: abilities are equal across profiles and gating
# happens in Access Controls (mcp_allowed, admin_grants, socials_allowed), not
# in token scopes. Scopes exist so an INTEGRATION can be least-privilege.
# Testing enforcement therefore needs a deliberately narrow token.
TOK_READONLY = auth.issue_token("alice", "read-only integration",
                                scopes=["chat:read"])
app.state.keystore = auth._load_keystore()
ok("wildcard token satisfies any scope (by design)",
   call(TOK_ALICE, "/narrow").status_code == 200,
   call(TOK_ALICE, "/narrow").status_code)
ok("a chat:read token is REFUSED chat:write -> 403",
   call(TOK_READONLY).status_code == 403, call(TOK_READONLY).status_code)
ok("...and refused admin:* too", call(TOK_READONLY, "/narrow").status_code == 403,
   call(TOK_READONLY, "/narrow").status_code)

print("\n== rotation and revocation take effect on the wire ==")
new_alice = auth.rotate_token_for("alice")
app.state.keystore = auth._load_keystore()
ok("alice's OLD token now 401s", call(TOK_ALICE).status_code == 401)
ok("alice's NEW token works, same namespace",
   call(new_alice).json().get("ns") == "alice", call(new_alice).json())
ok("bob is untouched by alice's rotation",
   call(TOK_BOB).json().get("ns") == "bob")
ok("the owner is untouched", call(TOK_OWNER).json().get("ns") is None)

auth.revoke_tokens_for("bob")
app.state.keystore = auth._load_keystore()
ok("bob's token dies when his profile is deleted",
   call(TOK_BOB).status_code == 401, call(TOK_BOB).status_code)
ok("alice still works after bob's deletion",
   call(new_alice).status_code == 200)

bad = [n for n, cnd in _results if not cnd]
print("\n%d/%d passed." % (len(_results) - len(bad), len(_results)))
if bad:
    print("FAILED:")
    for n in bad:
        print("  - " + n)
    sys.exit(1)
