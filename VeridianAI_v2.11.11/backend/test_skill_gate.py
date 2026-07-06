"""Self-running unit tests for skill_gate (Aether skill-share Layer 3).

Run:  python test_skill_gate.py     (no pytest needed; pure-stdlib module)
Covers: safe-only eligibility, gated opt-in flow, hard-blocked code.exec (even if
opted in), unknown-capability handling, missing-local-hook block, prepend_system
review gate, the declared-vs-actual hook cross-check, author-trust requirement,
and the promotion enforcement point.
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_gate as g


def env(caps):
    return {"payload": {"schema": "oracleai.skill/1", "capabilities": caps}}


def test_safe_only_eligible():
    p = g.default_policy(require_trusted_author=False)
    v = g.evaluate(env(["prompt.augment", "hook.append_footer"]), p)
    assert v["recommendation"] == "eligible", v


def test_gated_needs_then_eligible():
    p = g.default_policy(require_trusted_author=False)
    v = g.evaluate(env(["network.outbound"]), p)
    assert v["recommendation"] == "needs_approval" and "network.outbound" in v["needs_approval"]
    p2 = g.default_policy(require_trusted_author=False, allowed_caps=["network.outbound"])
    assert g.evaluate(env(["network.outbound"]), p2)["recommendation"] == "eligible"


def test_blocked_code_exec_wins_over_optin():
    p = g.default_policy(require_trusted_author=False, allowed_caps=["code.exec"])
    v = g.evaluate(env(["code.exec"]), p)
    assert v["recommendation"] == "block" and "code.exec" in v["blocked"]


def test_unknown_blocked_by_default():
    p = g.default_policy(require_trusted_author=False)
    v = g.evaluate(env(["frobnicate"]), p)
    assert v["recommendation"] == "block" and "frobnicate" in v["unknown"]
    p2 = g.default_policy(require_trusted_author=False, block_unknown=False)
    assert g.evaluate(env(["frobnicate"]), p2)["recommendation"] == "needs_approval"


def test_missing_hook_blocks():
    p = g.default_policy(require_trusted_author=False, available_hooks=["append_footer"])
    v = g.evaluate(env(["hook.prepend_system"]), p)
    assert v["recommendation"] == "block" and "prepend_system" in v["missing_hooks"]


def test_prepend_system_gated():
    p = g.default_policy(require_trusted_author=False)
    assert g.evaluate(env(["hook.prepend_system"]), p)["recommendation"] == "needs_approval"
    p2 = g.default_policy(require_trusted_author=False, allowed_caps=["hook.prepend_system"])
    assert g.evaluate(env(["hook.prepend_system"]), p2)["recommendation"] == "eligible"


def test_undeclared_body_hook_blocks():
    p = g.default_policy(require_trusted_author=False)
    body = json.dumps({"hooks": {"append_footer": "hi"}}).encode()
    v = g.evaluate(env(["prompt.augment"]), p, body=body)
    assert v["recommendation"] == "block" and "append_footer" in v["undeclared_hooks"]


def test_declared_body_hook_ok():
    p = g.default_policy(require_trusted_author=False)
    body = json.dumps({"hooks": {"append_footer": "hi"}}).encode()
    v = g.evaluate(env(["hook.append_footer"]), p, body=body)
    assert v["recommendation"] == "eligible", v


def test_author_trust_required():
    p = g.default_policy()  # require_trusted_author True
    v = g.evaluate(env(["prompt.augment"]), p, trusted=False)
    assert v["recommendation"] == "block" and any("trusted" in r for r in v["reasons"])
    assert g.evaluate(env(["prompt.augment"]), p, trusted=True)["recommendation"] == "eligible"


def test_gate_for_promotion_enforces():
    p = g.default_policy(require_trusted_author=False)
    allowed, _ = g.gate_for_promotion(env(["network.outbound"]), p)
    assert allowed is False
    p2 = g.default_policy(require_trusted_author=False, allowed_caps=["network.outbound"])
    allowed2, _ = g.gate_for_promotion(env(["network.outbound"]), p2)
    assert allowed2 is True


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    p = f = 0
    for fn in fns:
        try:
            fn(); p += 1; print("PASS", fn.__name__)
        except Exception:
            f += 1; print("FAIL", fn.__name__); traceback.print_exc()
    print("\n%d passed, %d failed" % (p, f))
    sys.exit(1 if f else 0)
