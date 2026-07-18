"""
test_customs.py -- regression gate for customs_daemon.py (v2.13)
================================================================

Run:  python test_customs.py        (no pytest dependency; same style as
                                     test_expression_engine.py)

Covers, per the BUILD SPEC:
  * Tier 1 strict validation (pass + parity: valid args byte-identical)
  * Tier 2 per-tool repair -- REGRESSION ANCHOR: the actual pipe-in-URL
    failure class from live logs ([BROWSE: url|...])
  * Tier 3 bounce with specific correction + 2-retry cap
  * Tier 4 hard reject (visible, never silent)
  * FAIL CLOSED: forced internal exception -> reject, not pass-through
  * Disabled flag -> exact pass-through, zero behavior change
  * Unknown tool -> generic floor (log + validate shape), never crash
  * Hash-chain audit: events append to the EXISTING chain and verify
"""

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import customs_daemon as cd  # noqa: E402

PASS = 0
FAIL = 0
_tmp = Path(tempfile.mkdtemp(prefix="customs_test_"))


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f"FAIL  {label}  {extra}")


def enable(flag=True):
    cd.set_runtime(lambda k, d=None: flag if k == "customs_enabled" else d,
                   _tmp)
    cd._ledger.clear()


print("== disabled: exact pass-through ==")
enable(False)
r = cd.inspect("browse", {"url": "total garbage | not a url"}, "test")
check("disabled verdict", r.verdict == "disabled")
check("disabled allows", r.allowed)
t = cd.inspect_tag("browse", "total garbage | not a url", "test")
check("disabled tag content untouched",
      t.allowed and t.content == "total garbage | not a url")

print("== tier 1: pass + parity ==")
enable(True)
raw = {"url": "https://example.com/page", "max_chars": 0}
r = cd.inspect("browse", dict(raw), "test")
check("valid browse passes", r.verdict == "pass")
check("PARITY: args byte-identical (no default injection)",
      r.args == raw, str(r.args))
r = cd.inspect("search", {"query": "wa state llc filing", "num_results": 5},
               "test")
check("valid search passes", r.verdict == "pass")

print("== tier 2: pipe-in-URL regression anchor ==")
enable(True)
# The actual failure class: model crams url + extra into one field with '|'.
r = cd.inspect("browse", {"url": "https://dor.wa.gov/tax-basics|3000"},
               "test")
check("url|<int> repaired", r.verdict == "repaired", r.verdict)
check("url split on last pipe",
      r.args.get("url") == "https://dor.wa.gov/tax-basics")
check("int tail became max_chars", r.args.get("max_chars") == 3000)
# body with pipes INSIDE it: split must be on the LAST pipe.
r = cd.inspect("browse",
               {"url": "https://x.com/a%7Cb|2000"}, "test")
check("escaped-ish pipe body, last-pipe split",
      r.verdict == "repaired" and r.args.get("max_chars") == 2000)
# ambiguous text tail: NEVER guess -> Tier 3 bounce, specific message.
r = cd.inspect("browse",
               {"url": "https://x.com/page|please summarize this"}, "test")
check("url|text bounces (no guessing)", r.verdict == "bounce", r.verdict)
check("bounce message is specific",
      "concatenated" in r.correction and "'|'" in r.correction,
      r.correction)

print("== tier 2: other browse repairs ==")
enable(True)
r = cd.inspect("browse", {"url": '"https://x.com/page"'}, "test")
check("wrapping quotes stripped",
      r.verdict == "repaired" and r.args["url"] == "https://x.com/page")
r = cd.inspect("browse",
               {"url": "[WA DOR](https://dor.wa.gov/file)"}, "test")
check("markdown link unwrapped",
      r.verdict == "repaired" and r.args["url"] == "https://dor.wa.gov/file")
r = cd.inspect("browse", {"url": "dor.wa.gov/taxes"}, "test")
check("bare domain gets https://",
      r.verdict == "repaired" and r.args["url"] == "https://dor.wa.gov/taxes")

print("== tier 2: code fence strip ==")
enable(True)
r = cd.inspect("code", {"code": "```python\nprint('hi')\n```"}, "test")
check("markdown fence stripped",
      r.verdict == "repaired" and r.args["code"] == "print('hi')")
r = cd.inspect("code", {"code": "print('clean')"}, "test")
check("clean code passes untouched",
      r.verdict == "pass" and r.args["code"] == "print('clean')")

print("== tier 3/4: bounce cap -> hard reject ==")
enable(True)
bad = {"url": "https://x.com/page|do the thing"}
r1 = cd.inspect("browse", dict(bad), "agentic")
r2 = cd.inspect("browse", dict(bad), "agentic")
r3 = cd.inspect("browse", dict(bad), "agentic")
check("bounce 1", r1.verdict == "bounce" and "1/2" in r1.correction)
check("bounce 2", r2.verdict == "bounce" and "2/2" in r2.correction)
check("3rd attempt hard-rejects", r3.verdict == "reject", r3.verdict)
check("reject is VISIBLE", "[CUSTOMS REJECT]" in r3.correction)
check("different origin has its own ledger",
      cd.inspect("browse", dict(bad), "mcp").verdict == "bounce")

print("== fail closed: chaos ==")
enable(True)


class _Bomb(cd.ToolValidator):
    tool_name = "browse"
    schema = None

    def validate(self, raw_call):
        raise RuntimeError("chaos monkey")


_orig = cd.registry._validators["browse"]
cd.registry.register(_Bomb())
r = cd.inspect("browse", {"url": "https://example.com"}, "test")
check("internal exception -> REJECT (never pass-through)",
      r.verdict == "reject", r.verdict)
check("fail-closed error type", r.error_type == "customs_internal_error")
cd.registry.register(_orig)
# inspect_tag fail-closed too
_orig_map = cd._TAG_TO_ARGS["weather"]
cd._TAG_TO_ARGS["weather"] = lambda c: (_ for _ in ()).throw(
    RuntimeError("chaos"))
t = cd.inspect_tag("weather", "Seattle", "test")
check("inspect_tag fail-closed", not t.allowed and t.verdict == "reject")
cd._TAG_TO_ARGS["weather"] = _orig_map

print("== unknown tool: generic floor ==")
enable(True)
r = cd.inspect("brand_new_tool_2027", {"anything": "goes"}, "test")
check("unknown tool passes generic floor", r.verdict == "pass")
r = cd.inspect("brand_new_tool_2027", "not a dict", "test")
check("unknown tool bad shape bounces", r.verdict == "bounce")
r = cd.inspect("brand_new_tool_2027", {"f": "nul\x00byte"}, "test")
check("NUL bytes bounce", r.verdict == "bounce")

print("== tag adapter: shape preservation ==")
enable(True)
t = cd.inspect_tag("save_file", ("notes.md", "hello|world|pipes ok"), "test")
check("save_file tuple passes, shape kept",
      t.allowed and t.content == ("notes.md", "hello|world|pipes ok"))
t = cd.inspect_tag("remember", "llc_filing|WA DOR portal, annual report",
                   "test")
check("remember key|desc passes, string kept",
      t.allowed and isinstance(t.content, str))
t = cd.inspect_tag("browse", '"https://x.com/page"', "test")
check("repaired browse tag content is clean string",
      t.allowed and t.content == "https://x.com/page", repr(t.content))
t = cd.inspect_tag("search", "", "test")
check("empty search bounces with message",
      not t.allowed and t.message != "")
t = cd.inspect_tag("prioritise",
                   "search news for AI | weather Seattle", "test")
check("prioritise pipe list passes", t.allowed)

print("== CRAIID-adjacent identity: memory tools pass unchanged ==")
enable(True)
for tool, content in (("search_memory", "fernet key rotation"),
                      ("recall", "backup procedure"),
                      ("remember", "vlts_atrest|encrypted chunks in sage_data"),
                      ("remember_fail", "bad_mount|stale bash snapshot")):
    t = cd.inspect_tag(tool, content, "agentic")
    check(f"{tool} identity round-trip",
          t.allowed and t.content == content, f"{t.verdict} {t.message}")

print("== redaction: no payload in audit chain ==")
enable(True)
secret = "PHI-SSN-536-22-1111-do-not-log"
cd.inspect("code", {"code": f"x = '{secret}'"}, "test")
cd.inspect("search", {"query": secret}, "test")
chain = _tmp / "handoff_audit.log"
blob = chain.read_text(encoding="utf-8") if chain.exists() else ""
check("audit chain exists", chain.exists())
check("sensitive content NEVER in chain", secret not in blob)
# detail is a JSON string embedded in the chain record, so quotes are
# escaped: \"fields\": [\"code\"]
check("field names ARE in chain",
      '\\"fields\\": [\\"code\\"' in blob, blob[:200])

print("== ledger: TTL + size cap ==")
enable(True)
for i in range(cd._LEDGER_MAX + 500):
    cd._bounce_count("test", "browse", f"digest-{i}")
check("ledger size-capped",
      len(cd._ledger) <= cd._LEDGER_MAX, str(len(cd._ledger)))
cd._ledger.clear()
cd._ledger[("old", "browse", "x")] = (1, time.time() - 3600)
cd._bounce_count("test", "browse", "fresh")
check("TTL prunes stale entries",
      ("old", "browse", "x") not in cd._ledger)

print("== hash chain: verify + tamper detection ==")
try:
    from handoff_guard import HandoffGuard
    g = HandoffGuard(_tmp)
    ok, broken = g.verify_audit()
    check("chain verifies clean", ok and broken is None, str(broken))
    lines = chain.read_text(encoding="utf-8").splitlines()
    if lines:
        rec = json.loads(lines[0])
        rec["detail"] = "tampered"
        lines[0] = json.dumps(rec)
        chain.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok2, broken2 = g.verify_audit()
        check("tampering detected", not ok2 and broken2 == 1,
              f"ok={ok2} line={broken2}")
except Exception as e:
    check("hash chain integration", False, f"{type(e).__name__}: {e}")

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
shutil.rmtree(_tmp, ignore_errors=True)
sys.exit(1 if FAIL else 0)
