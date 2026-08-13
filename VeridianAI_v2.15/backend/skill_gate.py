"""
OracleAI / Aether -- capability GATE + ingestion policy (Layer 3).

Decides what a shared skill is ALLOWED to do on THIS machine. PURE policy: it
classifies a skill's declared capabilities, checks that any hooks it references
already exist locally, confirms the body does no more than it declared, and
returns a verdict (block / needs_approval / eligible). It executes nothing and
grants no authority -- promotion stays a human decision and this gate is the
enforcement point at that moment.

v1 stance ("don't trust, verify", no foreign code): shared skills are
DECLARATIVE -- data + a manifest that binds to locally-present hooks (mirroring
the plugin model: prepend_system / append_footer text injection, network.outbound
permission). Capabilities that would run shipped/foreign code are HARD-BLOCKED
until a sandbox layer exists.
"""
import json

SAFE = "safe"
GATED = "gated"
BLOCKED = "blocked"
UNKNOWN = "unknown"

# declarative hooks this engine understands today (mirror plugin_manager)
BUILTIN_HOOKS = {"prepend_system", "append_footer"}

CAPABILITY_CLASS = {
    "prompt.augment":      SAFE,
    "memory.read":         SAFE,
    "catalog.read":        SAFE,
    "hook.append_footer":  SAFE,    # text appended AFTER generation -> low risk
    "hook.prepend_system": GATED,   # influences the system prompt -> human review
    "network.outbound":    GATED,
    "browse":              GATED,
    "web_search":          GATED,
    "fs.read":             GATED,
    "fs.write":            GATED,
    "memory.write":        GATED,
    "code.exec":           BLOCKED,  # would run shipped/foreign code -> not in v1
    "shell":               BLOCKED,
    "process.spawn":       BLOCKED,
}


def classify(cap):
    return CAPABILITY_CLASS.get(cap, UNKNOWN)


def default_policy(available_hooks=None, allowed_caps=None,
                   require_trusted_author=True, block_unknown=True):
    return {
        "available_hooks": set(BUILTIN_HOOKS if available_hooks is None else available_hooks),
        "allowed_caps": set(allowed_caps or []),   # gated caps the user opted into
        "require_trusted_author": bool(require_trusted_author),
        "block_unknown": bool(block_unknown),
    }


def _body_hooks(body):
    """Hooks the skill body actually uses (so we can confirm it declared them)."""
    try:
        d = json.loads(body.decode("utf-8")) if isinstance(body, (bytes, bytearray)) else body
        if isinstance(d, dict) and isinstance(d.get("hooks"), dict):
            return set(str(k) for k in d["hooks"].keys())
    except Exception:
        pass
    return set()


def evaluate(envelope, policy, trusted=False, body=None):
    """Classify a skill against the policy. Returns a verdict dict. Pure: no I/O,
    no execution."""
    payload = envelope.get("payload", {}) if isinstance(envelope, dict) else {}
    requested = list(payload.get("capabilities", []) or [])
    req_set = set(requested)

    ok_caps, needs_approval, blocked, unknown, missing_hooks = [], [], [], [], []
    for cap in requested:
        if cap.startswith("hook."):
            name = cap[len("hook."):]
            if name not in policy["available_hooks"]:
                missing_hooks.append(name)
                continue
            cls = CAPABILITY_CLASS.get(cap, GATED)  # present-but-unlisted hook -> gated
        else:
            cls = classify(cap)
        if cls == BLOCKED:
            blocked.append(cap)
        elif cls == UNKNOWN:
            unknown.append(cap)
            if not policy["block_unknown"]:
                needs_approval.append(cap)
        elif cls == GATED:
            (ok_caps if cap in policy["allowed_caps"] else needs_approval).append(cap)
        else:
            ok_caps.append(cap)

    # the body must not DO more than it declared: every hook present in the body
    # must be covered by a declared hook.<name> capability (which is signed).
    undeclared = sorted(h for h in _body_hooks(body) if ("hook." + h) not in req_set)

    author_ok = (not policy["require_trusted_author"]) or bool(trusted)

    reasons = []
    if blocked:
        reasons.append("blocked capabilities: " + ", ".join(sorted(blocked)))
    if missing_hooks:
        reasons.append("missing local hooks: " + ", ".join(sorted(set(missing_hooks))))
    if undeclared:
        reasons.append("undeclared hooks in body: " + ", ".join(undeclared))
    if unknown and policy["block_unknown"]:
        reasons.append("unknown capabilities: " + ", ".join(sorted(unknown)))
    if not author_ok:
        reasons.append("author not in trusted set")

    hard_block = (bool(blocked) or bool(missing_hooks) or bool(undeclared)
                  or (bool(unknown) and policy["block_unknown"]) or (not author_ok))
    rec = "block" if hard_block else ("needs_approval" if needs_approval else "eligible")

    return {
        "recommendation": rec,
        "eligible": rec == "eligible",
        "ok_caps": sorted(ok_caps),
        "needs_approval": sorted(set(needs_approval)),
        "blocked": sorted(blocked),
        "unknown": sorted(unknown),
        "missing_hooks": sorted(set(missing_hooks)),
        "undeclared_hooks": undeclared,
        "author_trusted": bool(trusted),
        "reasons": reasons,
    }


def gate_for_promotion(envelope, policy, trusted=False, body=None):
    """Enforcement point: returns (allowed, verdict). allowed is True ONLY for an
    'eligible' verdict -- call this immediately before store.promote()."""
    v = evaluate(envelope, policy, trusted=trusted, body=body)
    return (v["recommendation"] == "eligible", v)
