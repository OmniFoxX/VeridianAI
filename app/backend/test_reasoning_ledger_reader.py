#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_reasoning_ledger_reader.py -- the thinking log can actually be read.

WHAT WAS WRONG

reasoning_ledger.py was correct, tested, encrypted, per-user, and unreachable.
It was written on every reasoning turn and there was NO way to read it back:
no endpoint, no button, nothing but a Python session against a Fernet file.

And the audit half was worse. A reachability scan of main.py found exactly one
dead function in the whole module:

    UNCALLED  line 573  verify_reasoning_provenance

So "reasoning is captured and auditable" was true of the code and false of the
app. Section 2 asserts reachability rather than existence, because existence is
what passed last time.

SIXTH INSTANCE OF THE SAME SHAPE THIS RELEASE

  * the reasoning hook covered streaming; agentic turns went around it
  * the at-rest call-site guard could not see a module that made no atrest calls
  * the thinking budget reached the respawn path, not the boot spawner
  * procedural memory took an owner_ns and was constructed once, globally
  * data_export.py: 72 passing tests and no endpoint, for its whole life
  * this

THE CRY-WOLF BUG, FOUND WHILE WIRING IT UP

verify_reasoning_provenance walked the most recent 2000 chain entries and
reported anything it did not find as "it differs from what was recorded at the
time". The chain is GLOBAL and append-only across every profile and every kind
of entry, so an honest witness from 3000 entries ago reads as TAMPERING.

_reasoning_hash's own docstring names the stakes: "a provenance check that
cries wolf gets switched off, which is worse than not having one." Section 3
pins the fix -- "not in the window I searched" is now its own answer.

AND TRUNCATION IS NOT TAMPERING

record() stores at most MAX_TRACE_CHARS of text but hashes the ORIGINAL, so a
truncated entry's stored text deliberately does not hash to its own sha256.
Re-hashing it and calling the mismatch tampering would flag every long trace on
a healthy install. Section 3 pins that too.

HOW SECTION 3 TESTS REAL BEHAVIOUR WITHOUT BOOTING THE APP

Importing main.py starts plugins, the memory logger, the voice stack and more,
which no other main.py test does. So the verifier functions are lifted out of
the SHIPPED source by AST and exec'd against a stub logger. It is the real
code -- not a copy that can drift -- with only its dependency replaced.

    python test_reasoning_ledger_reader.py
"""
import ast
import hashlib
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


MAIN = io.open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
DEX = io.open(os.path.join(_HERE, "data_export.py"), encoding="utf-8").read()
_TREE = ast.parse(MAIN)

_FE = os.path.join(os.path.dirname(_HERE), "frontend")
JS = io.open(os.path.join(_FE, "js", "reasoning-log.js"),
             encoding="utf-8").read()
HTML = io.open(os.path.join(_FE, "index.html"), encoding="utf-8").read()


def _routes():
    """path -> (methods, function node), read off the decorators."""
    out = {}
    for n in ast.walk(_TREE):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in n.decorator_list:
            if not isinstance(d, ast.Call):
                continue
            f = d.func
            if not (isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Name) and f.value.id == "app"):
                continue
            if d.args and isinstance(d.args[0], ast.Constant):
                out.setdefault(d.args[0].value, []).append((f.attr, n))
    return out


ROUTES = _routes()


def _src(node):
    return ast.get_source_segment(MAIN, node) or ""


# =============================================================================
print("=== 1. The endpoints exist, and are gated like the rest ===")
# =============================================================================
for _path, _method in (("/api/reasoning-ledger", "get"),
                       ("/api/reasoning-ledger/entry", "get"),
                       ("/api/reasoning-ledger/clear", "post")):
    _hit = [(m, n) for (m, n) in ROUTES.get(_path, []) if m == _method]
    ok("%s %s is registered" % (_method.upper(), _path), bool(_hit))
    if not _hit:
        continue
    _body = _src(_hit[0][1])
    ok("...it is localhost-only", "_is_local_client(request)" in _body,
       "every other data endpoint refuses a non-local caller; one that does "
       "not is the hole")
    ok("...it cloaks rather than 403s", "_cloak_not_found()" in _body,
       "matching the export surface: a 403 confirms the endpoint exists")
    ok("...it scopes to the caller's namespace",
       "_safe_ns(_session_ns(request))" in _body,
       "without this every profile reads the OWNER's thinking log")

_list = [n for (m, n) in ROUTES.get("/api/reasoning-ledger", []) if m == "get"]
if _list:
    _b = _src(_list[0])
    # ON THE AST, BY DICT KEY. The first version of this check was
    # '"trace" not in body', which FAILED on correct code -- the endpoint reads
    # _e.get("trace") to cut the preview from it. Matching source text again
    # instead of the property, for the sixth time this release. The real rule
    # is about the shape of the row that goes OUT.
    _keys = set()
    for _n in ast.walk(_list[0]):
        if isinstance(_n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_row" for t in _n.targets):
            if isinstance(_n.value, ast.Dict):
                _keys = {k.value for k in _n.value.keys
                         if isinstance(k, ast.Constant)}
    ok("the LIST endpoint's rows carry a preview",
       "preview" in _keys, sorted(_keys))
    ok("...and never the whole trace", "trace" not in _keys,
       "a trace runs to 200,000 chars; fifty of them to draw a list is why "
       "this is a summary endpoint -- found keys %s" % sorted(_keys))
    ok("...and does not leak the on-disk path",
       '_st.pop("path", None)' in _b,
       "stats() carries the profile directory; counts are safe, the path "
       "names the profile")
    ok("...verification is opt-in", "verify: bool = False" in _b,
       "each verified entry walks the chain; fifty walks per render is not a "
       "default anyone asked for")

_ent = [n for (m, n) in ROUTES.get("/api/reasoning-ledger/entry", []) if m == "get"]
if _ent:
    _b = _src(_ent[0])
    ok("a single entry is addressed by HASH, not index",
       "sha" in _b and "len(_sha) != 64" in _b,
       "the ledger prunes oldest-first, so entry #7 is a different entry "
       "after a prune -- an index would silently show the wrong trace")
    ok("...and the hash is validated before use",
       '"0123456789abcdef"' in _b)

_vm = [n for (m, n) in ROUTES.get("/api/reasoning/verify-message", [])
       if m == "post"]
ok("POST /api/reasoning/verify-message is registered", bool(_vm))
if _vm:
    _b = _src(_vm[0])
    ok("...it is localhost-only", "_is_local_client(request)" in _b)
    ok("...and it calls the chat-message verifier",
       "verify_reasoning_provenance(" in _b)
    # DELIBERATELY not namespace-scoped, and that needs saying rather than
    # looking like an oversight next to three endpoints that are.
    ok("...it takes no namespace, because the chain is global",
       "_session_ns" not in _b,
       "the hash chain is the one global store in this app; a witness lookup "
       "is a yes/no about a hash the caller already holds")
    ok("...and it stores nothing", "record(" not in _b and "_save" not in _b)

_clr = [n for (m, n) in ROUTES.get("/api/reasoning-ledger/clear", []) if m == "post"]
if _clr:
    _b = _src(_clr[0])
    ok("clearing the log is audited",
       '_audit_api_action(request, "reasoning_ledger.clear"' in _b,
       "deleting an audit trail is itself worth a record")
    ok("...and the audit entry carries counts, not traces",
       '"entries"' in _b and '"trace"' not in _b)


# =============================================================================
print("\n=== 2. Nothing in the reasoning surface is dead code ===")
# =============================================================================
# THE ASSERTION THAT WOULD HAVE CAUGHT THIS. Asserting the verifier EXISTED
# passed for its entire unreachable life.
_called = set()
for _n in ast.walk(_TREE):
    if isinstance(_n, ast.Call):
        _f = _n.func
        if isinstance(_f, ast.Name):
            _called.add(_f.id)
        elif isinstance(_f, ast.Attribute):
            _called.add(_f.attr)
    # A function passed as a value (thread target, callback) is used too.
    elif isinstance(_n, ast.Name) and isinstance(_n.ctx, ast.Load):
        _called.add(_n.id)

_dead = []
for _n in _TREE.body:
    if not isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    if _n.name in _called:
        continue
    if any("app." in (ast.unparse(d)) for d in _n.decorator_list):
        continue
    _dead.append((_n.lineno, _n.name))

ok("no top-level function in main.py is unreachable", not _dead, _dead)
ok("verify_reasoning_provenance specifically has a caller",
   "verify_reasoning_provenance" in _called,
   "it was the ONLY dead function in this module -- the audit half of a "
   "feature whose whole point is auditability")
ok("verify_reasoning_ledger_entry has one too",
   "verify_reasoning_ledger_entry" in _called)

# One chain walk, for the same reason there is one hash.
_walkers = [n.name for n in _TREE.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and 'reasoning:' in _src(n) and 'get_recent' in _src(n)]
ok("exactly one function walks the chain for a reasoning witness",
   _walkers == ["_chain_has_reasoning"], _walkers)


# =============================================================================
print("\n=== 3. The verifiers, run for real ===")
# =============================================================================
# Lifted from the shipped source so this cannot test a stale copy.
_want = {"_reasoning_hash", "_chain_has_reasoning",
         "verify_reasoning_provenance", "verify_reasoning_ledger_entry"}
_ns = {}
_pieces = []
for _n in _TREE.body:
    if isinstance(_n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_CHAIN_SEARCH_WINDOW"
            for t in _n.targets):
        _pieces.append(_src(_n))
    if isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
            and _n.name in _want:
        _pieces.append(_src(_n))
ok("all four verifier pieces were found in main.py",
   len(_pieces) == len(_want) + 1, len(_pieces))


class _Logger(object):
    """Just enough memory_logger to answer the two questions the walk asks."""

    def __init__(self, contents, total=None):
        self._c = list(contents)
        self._total = len(self._c) if total is None else total

    def get_recent(self, n=10, **kw):
        return [{"content": c} for c in self._c[:n]]

    def count_entries(self):
        return self._total


exec(compile("\n\n".join(_pieces), "<main.py extract>", "exec"), _ns)
_ns["memory_logger"] = None

TRACE = ("First I thought the answer was 7. That only holds if the input is "
         "sorted, so discard it. The answer is 4.")
SHA = hashlib.sha256(TRACE.encode("utf-8")).hexdigest()

ok("the extracted hash matches hashlib", _ns["_reasoning_hash"](TRACE) == SHA)

# -- the walk ---------------------------------------------------------------
_ns["memory_logger"] = None
_found, _note = _ns["_chain_has_reasoning"](SHA)
ok("no logger -> unverifiable, not a failure", _found is None, _note)

_ns["memory_logger"] = _Logger(["reasoning:" + SHA, "other"])
_found, _note = _ns["_chain_has_reasoning"](SHA)
ok("witness present -> True", _found is True, _note)

# The whole chain was searched and it is not there. THAT is a red flag.
_ns["memory_logger"] = _Logger(["nope", "also nope"])
_found, _note = _ns["_chain_has_reasoning"](SHA)
ok("witness genuinely absent -> False", _found is False, _note)

# THE CRY-WOLF CASE: the chain is bigger than the window that was searched.
_ns["memory_logger"] = _Logger(["nope"] * 10, total=9999)
_found, _note = _ns["_chain_has_reasoning"](SHA)
ok("older than the search window -> None, NOT False", _found is None,
   "reported %r: %s" % (_found, _note))
ok("...and it says so in words", "Inconclusive" in _note, _note)

# -- chat-message shape -----------------------------------------------------
_ns["memory_logger"] = _Logger(["reasoning:" + SHA])
_v = _ns["verify_reasoning_provenance"](
    {"reasoning": TRACE, "reasoning_chain_hash": "abc"})
ok("a witnessed chat message verifies", _v["hash_matches"] is True, _v)
_v = _ns["verify_reasoning_provenance"]({"reasoning": TRACE})
ok("an unwitnessed one is unverifiable, not tampered",
   _v["witnessed"] is False and _v["hash_matches"] is None, _v)

# -- ledger shape -----------------------------------------------------------
_full = {"trace": TRACE, "sha256": SHA, "truncated": False,
         "meta": {"chain_hash": "abc"}}
_v = _ns["verify_reasoning_ledger_entry"](_full)
ok("an intact ledger entry verifies both ways",
   _v["text_intact"] is True and _v["hash_matches"] is True, _v)

_tampered = dict(_full, trace=TRACE.replace("4", "9"))
_v = _ns["verify_reasoning_ledger_entry"](_tampered)
ok("editing the ledger file is caught by the text check",
   _v["text_intact"] is False, _v)
ok("...and the message says the entry was altered",
   "altered" in _v["message"], _v["message"])

# THE TRUNCATION CASE. Stored text is a prefix; sha256 is of the original.
_trunc = {"trace": TRACE[:40], "sha256": SHA, "truncated": True,
          "meta": {"chain_hash": "abc"}}
_v = _ns["verify_reasoning_ledger_entry"](_trunc)
ok("a TRUNCATED entry is not reported as altered",
   _v["text_intact"] is None,
   "record() hashes the original and stores a prefix, so re-hashing the "
   "prefix mismatches on healthy data -- that is not tampering")
ok("...its recorded hash is still checked against the chain",
   _v["hash_matches"] is True, _v)

_v = _ns["verify_reasoning_ledger_entry"](
    {"trace": TRACE, "sha256": SHA, "meta": {}})
ok("a ledger entry with no witness is unverifiable, not tampered",
   _v["witnessed"] is False and _v["hash_matches"] is None, _v)


# =============================================================================
print("\n=== 4. It can be taken OUT, through the existing export ===")
# =============================================================================
ok("the ledger is an export section",
   '"reasoning"' in DEX and ".reasoning_ledger.dat" in DEX)
ok("...resolved beside chat memory, like the evidence ledger",
   "mem.parent / \".reasoning_ledger.dat\"" in DEX,
   "rebuilding the path here would strand the section if conversations move")
ok("...and it is labelled as working, not as sources",
   "Model reasoning traces" in DEX,
   "calling it evidence is the confusion the separate ledger exists to avoid")
# Containment: a profile's sections must all live under its own root, and the
# reasoning section arrives via mem.parent, which is exactly that root.
ok("the profile containment filter still guards every section",
   "_within(profile_root, v[1])" in DEX)


# =============================================================================
print("\n=== 5. The viewer treats a trace as untrusted text ===")
# =============================================================================
ok("the module exists and opens from the toolbar",
   "window.openReasoningLog" in JS)
ok("the toolbar button is wired", 'onclick="openReasoningLog()"' in HTML)
ok("...and the script is actually loaded",
   "js/reasoning-log.js" in HTML,
   "a module nothing loads is how we got here in the first place")
ok("the script loads after modal-a11y",
   HTML.index("js/modal-a11y.js") < HTML.index("js/reasoning-log.js"),
   "focus containment is set up by that module")

# The one rule that matters in this file.
ok("the full trace is inserted with textContent",
   "pre.textContent" in JS and "pre.innerHTML" not in JS)
ok("the preview is too",
   "prev.textContent" in JS and "prev.innerHTML" not in JS)
_assigns = [l.strip() for l in JS.splitlines()
            if ".innerHTML" in l and "=" in l]
_bad = [l for l in _assigns
        if not (l.startswith("box.innerHTML") or l.startswith("box.innerHTML ")
                or 'innerHTML = ""' in l)]
ok("no innerHTML assignment carries model output", not _bad, _bad)

ok("pruning is surfaced rather than hidden", "pruned" in JS,
   "the ledger drops oldest-first and counts it; a reader that hides the "
   "count throws away the reason it is counted")
ok("unverifiable is a THIRD state, not a failure",
   "unverifiable" in JS and "NO MATCH" in JS,
   "painting inconclusive as red teaches the reader to ignore red")
ok("clearing warns that replies are untouched",
   "NOT touched" in JS)
ok("...and points at export first", "Export" in JS)


# =============================================================================
print("\n=== 6. The in-chat panel can ask the chain ===")
# =============================================================================
CHAT = io.open(os.path.join(_FE, "js", "chat.js"), encoding="utf-8").read()
ok("attachReasoningPanel accepts the chain hash",
   "function attachReasoningPanel(target, reasoning, chainHash)" in CHAT)
ok("...and calls the verifier endpoint",
   "/api/reasoning/verify-message" in CHAT)
ok("BOTH call sites pass the hash through",
   CHAT.count("attachReasoningPanel(") == 3
   and "attachReasoningPanel(target, meta.reasoning, meta.reasoning_chain_hash)" in CHAT
   and "attachReasoningPanel(wrap, msg.reasoning, msg.reasoning_chain_hash)" in CHAT,
   "%d occurrences (1 definition + 2 calls expected)"
   % CHAT.count("attachReasoningPanel("))
ok("the button only appears when there IS a hash to check",
   "if (chainHash) {" in CHAT,
   "a live turn has no witness yet; a button that always answered 'cannot "
   "verify' would teach people the check is broken")
ok("inconclusive is not painted as failure",
   "hash_matches === false" in CHAT and "hash_matches === true" in CHAT,
   "an else-branch on a boolean would collapse three states into two")
ok("the trace is still inserted with textContent",
   "body.textContent = reasoning" in CHAT)
ok("chat.js's cache-bust was bumped so the change actually loads",
   'chat.js?v=2.15.2a' in HTML,
   "same ?v= with new contents is a stale cached module -- which looks "
   "exactly like the feature not working")


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
