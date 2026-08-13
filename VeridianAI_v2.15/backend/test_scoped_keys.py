# -*- coding: utf-8 -*-
"""v2.14.3 -- scoped API keys, over real HTTP.

The scope machinery has existed since v2.2 and nothing ever minted anything
but ["*"]. These tests cover the part that makes it usable AND the part that
makes it safe: a profile can issue and revoke its OWN keys and nobody else's.

The prefix used to identify a key is not a secret -- it is shown in the list --
so the isolation has to come from the namespace check, not from the identifier
being hard to guess. That is what the cross-profile tests below actually prove.

    python test_scoped_keys.py
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
except Exception as e:                                   # pragma: no cover
    print("SKIP: fastapi/httpx unavailable (%s)" % e)
    sys.exit(0)

_tmp = tempfile.mkdtemp(prefix="veridian_scoped_")
os.environ["VERIDIAN_DATA_DIR"] = _tmp
import auth                                              # noqa: E402
auth.KEYSTORE_PATH = Path(_tmp) / ".api_keystore.json"
auth._save_keystore(auth._empty_keystore())

print("== presets are a closed set, and unknown does NOT mean full ==")
ok("four presets", sorted(auth.SCOPE_PRESETS) == ["chat", "chat_read", "full", "mcp"],
   sorted(auth.SCOPE_PRESETS))
ok("chat_read is read-only", auth.scopes_for_preset("chat_read") == ["chat:read"])
ok("unknown preset -> None, never ['*']", auth.scopes_for_preset("nonsense") is None)
ok("empty preset -> None", auth.scopes_for_preset("") is None)
ok("non-string -> None", auth.scopes_for_preset(None) is None)

print("\n== scopes are actually enforced on the wire ==")
app = FastAPI()
app.state.keystore = auth._load_keystore()


@app.post("/read", dependencies=[Depends(auth.require_scope("chat:read"))])
async def r(): return {"ok": True}


@app.post("/write", dependencies=[Depends(auth.require_scope("chat:write"))])
async def w(): return {"ok": True}


@app.post("/tools", dependencies=[Depends(auth.require_scope("mcp:*"))])
async def t(): return {"ok": True}


c = TestClient(app)
KEYS = {p: auth.issue_token(None, "k-" + p, auth.scopes_for_preset(p))
        for p in ("full", "chat", "chat_read", "mcp")}
app.state.keystore = auth._load_keystore()


def code(tok, path):
    return c.post(path, headers={"Authorization": "Bearer " + tok}, json={}).status_code


EXPECT = {
    "full":      {"/read": 200, "/write": 200, "/tools": 200},
    "chat":      {"/read": 200, "/write": 200, "/tools": 403},
    "chat_read": {"/read": 200, "/write": 403, "/tools": 403},
    "mcp":       {"/read": 403, "/write": 403, "/tools": 200},
}
for preset, want in EXPECT.items():
    got = {p: code(KEYS[preset], p) for p in want}
    ok("%-9s -> %s" % (preset, want), got == want, got)

print("\n== the read-only key genuinely cannot write ==")
ok("chat_read is refused chat:write", code(KEYS["chat_read"], "/write") == 403)
ok("mcp key cannot reach chat at all",
   code(KEYS["mcp"], "/read") == 403 and code(KEYS["mcp"], "/write") == 403)

print("\n== listing exposes metadata only ==")
meta = auth.list_tokens(None)
ok("all four listed", len(meta) == 4, len(meta))
ok("no hash exposed", all("hash" not in m for m in meta))
ok("no raw token exposed", all("token" not in m for m in meta))
ok("preset name surfaced for the UI",
   sorted(m["preset"] for m in meta) == ["chat", "chat_read", "full", "mcp"],
   [m.get("preset") for m in meta])

print("\n== revoke one key, by prefix, without touching the others ==")
target = next(m for m in meta if m["preset"] == "chat_read")
ok("revoke returns True", auth.revoke_token(target["prefix"], None) is True)
app.state.keystore = auth._load_keystore()
ok("that key is dead", code(KEYS["chat_read"], "/read") == 401,
   code(KEYS["chat_read"], "/read"))
ok("the other three still work",
   all(code(KEYS[p], "/read" if p != "mcp" else "/tools") == 200
       for p in ("full", "chat", "mcp")))
ok("revoking an unknown prefix is False", auth.revoke_token("ora_zzzz", None) is False)
ok("empty prefix is False", auth.revoke_token("", None) is False)

print("\n== CROSS-PROFILE: the prefix is public, so the ns check must do the work ==")
alice_tok = auth.issue_token("alice", "alice key", ["*"])
bob_tok = auth.issue_token("bob", "bob key", ["*"])
app.state.keystore = auth._load_keystore()
alice_meta = auth.list_tokens("alice")[0]
bob_meta = auth.list_tokens("bob")[0]

ok("bob CANNOT revoke alice's key even knowing its prefix",
   auth.revoke_token(alice_meta["prefix"], "bob") is False)
ok("a non-owner CANNOT revoke an owner key by prefix",
   auth.revoke_token(meta[0]["prefix"], "alice") is False)
ok("...and alice's key still works afterwards",
   (auth._verify_token(alice_tok, auth._load_keystore()) or {}).get("owner_ns") == "alice")
ok("alice CAN revoke her own", auth.revoke_token(alice_meta["prefix"], "alice") is True)
ok("bob is unaffected",
   (auth._verify_token(bob_tok, auth._load_keystore()) or {}).get("owner_ns") == "bob")

print("\n== listing is scoped too ==")
ok("alice sees only her own", all(m["owner_ns"] == "alice" for m in auth.list_tokens("alice")))
ok("bob sees only his own", all(m["owner_ns"] == "bob" for m in auth.list_tokens("bob")))
ok("owner listing excludes both",
   all(m["owner_ns"] is None for m in auth.list_tokens(None)))

bad = [n for n, cnd in _results if not cnd]
print("\n%d/%d passed." % (len(_results) - len(bad), len(_results)))
if bad:
    print("FAILED:")
    for n in bad:
        print("  - " + n)
    sys.exit(1)
