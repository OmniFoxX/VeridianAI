"""
customs_daemon.py -- Universal Tool-Call Sanitizer ("Customs")  v2.13
=====================================================================

Border inspection for EVERY tool call: each call, from every origin
(agentic elif chain, PRIORITISE sub-dispatcher, text->image intercept,
Build Battle gate, MCP clients, IPC bridge, HTTP/node image endpoints),
passes through Customs between "model emits tool call" and "tool
executor runs it".

NOT related to the max_tokens=-1 sanitizer (inference param handling).
Grep marker for logs: [CUSTOMS].

Design (see BUILD SPEC, v2.13):
  Tier 1  strict pydantic schema validation
  Tier 2  per-tool heuristic repair (known patterns ONLY; wrong-but-valid
          is worse than invalid-and-rejected -- never guess)
  Tier 3  bounce-back-to-model: a short, specific correction message is
          returned as the tool result so the model fixes its own mistake.
          Capped at MAX_BOUNCES (2) per (origin, tool, payload-digest).
  Tier 4  hard reject: visible failure, never a silent no-op.

HIPAA / privacy hard constraints honored here:
  * NO plaintext payload logging -- audit records carry tool name, origin,
    verdict, error type, field NAMES, and a sha256[:12] payload digest.
    The only content previews are structural: URL scheme+host, filename
    basename. Fields flagged sensitive are NEVER previewed.
  * FAIL CLOSED -- if Customs itself throws, the call is REJECTED, not
    passed through unvalidated.
  * Audit events append to the EXISTING hash-chain audit log
    (handoff_guard.HandoffGuard.audit in sage_data), never a parallel log.

Registry pattern: adding a new tool = subclass ToolValidator + register.
Unknown tools NEVER crash and are NEVER silently passed: they fall back
to GenericSchemaValidator (log + permissive pydantic floor).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Type
from urllib.parse import urlsplit

try:
    from pydantic import BaseModel, Field, ValidationError
except Exception as _pyd_err:  # pragma: no cover -- fastapi guarantees pydantic
    BaseModel = None  # type: ignore
    ValidationError = Exception  # type: ignore
    print(f"[CUSTOMS] pydantic unavailable ({_pyd_err}); "
          f"Customs will fail closed if enabled.")

__version__ = "2.13.0"

# Tier-3 bounce cap per (origin, tool, payload digest). Mirrors the
# overseer_daemon MAX_RESTART_ATTEMPTS counting pattern (count -> escalate)
# rather than inventing a new mechanism; the ledger below IS that counter.
MAX_BOUNCES = 2
_LEDGER_TTL_SEC = 1800.0  # forget a payload's bounce history after 30 min
_LEDGER_MAX = 4096        # hard size cap: evict oldest when exceeded, so a
                          # flood of unique bad payloads can't grow the dict
                          # unbounded for the life of the process

# ---------------------------------------------------------------------------
# Runtime wiring (config + hash-chain audit). main.py calls set_runtime().
# Falls back to config_store when running standalone (mcp_server stdio).
# ---------------------------------------------------------------------------
_config_getter: Optional[Callable[[str, Any], Any]] = None
_data_dir: Optional[Path] = None
_guard = None            # handoff_guard.HandoffGuard -- lazy
_guard_failed = False
_lock = threading.Lock()
_ledger: Dict[Tuple[str, str, str], Tuple[int, float]] = {}
_warned_disabled = False


def set_runtime(config_getter: Callable[[str, Any], Any],
                data_dir: str | Path) -> None:
    """Wire Customs to the live config and sage_data dir. Call once at boot.

    config_getter(key, default) must behave like dict.get on the flat
    effective config (main.py's `config`).
    """
    global _config_getter, _data_dir, _guard, _guard_failed
    _config_getter = config_getter
    _data_dir = Path(data_dir)
    _guard = None          # re-resolve against the new dir on next audit
    _guard_failed = False


def is_enabled() -> bool:
    """CUSTOMS_ENABLED knob. Default OFF until CRAIID regression passes."""
    try:
        if _config_getter is not None:
            return bool(_config_getter("customs_enabled", False))
        # Standalone fallback (stdio MCP server): read config_store directly.
        import config_store
        cfg = config_store.OracleConfig.load()
        return bool(getattr(cfg.sage, "customs_enabled", False))
    except Exception:
        return False  # unknown state -> treat as disabled (no behavior change)


def _audit(event_detail: Dict[str, Any]) -> None:
    """Append a Customs event to the EXISTING hash-chain audit log."""
    global _guard, _guard_failed
    if _guard_failed:
        return
    try:
        if _guard is None:
            if _data_dir is None:
                _guard_failed = True
                print("[CUSTOMS] audit disabled: no data_dir wired "
                      "(set_runtime not called)")
                return
            from handoff_guard import HandoffGuard
            _guard = HandoffGuard(_data_dir)
        _guard.audit("customs", json.dumps(event_detail, sort_keys=True,
                                           default=str))
    except Exception as e:
        _guard_failed = True
        print(f"[CUSTOMS] audit chain unavailable ({e}); "
              f"continuing with stdout logging only.")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class ValidationResult:
    verdict: str                 # disabled|pass|repaired|bounce|reject
    args: Dict[str, Any] = field(default_factory=dict)
    correction: str = ""         # Tier-3 message / Tier-4 reason
    error_type: str = ""
    repaired_fields: tuple = ()

    @property
    def allowed(self) -> bool:
        return self.verdict in ("disabled", "pass", "repaired")


# ---------------------------------------------------------------------------
# Schemas (pydantic). Field metadata: json_schema_extra={"sensitive": True}
# marks fields whose content must never appear in logs or previews.
# ---------------------------------------------------------------------------
if BaseModel is not None:

    class BrowseArgs(BaseModel):
        url: str = Field(min_length=1, max_length=4096)
        # ge=0: MCP's _tool_browse uses 0 as "tool-defined default" --
        # rejecting 0 here would break existing valid MCP calls.
        max_chars: int = Field(default=0, ge=0, le=500_000)

    class SearchArgs(BaseModel):
        query: str = Field(min_length=1, max_length=500,
                           json_schema_extra={"sensitive": True})
        num_results: int = Field(default=5, ge=1, le=25)

    class WeatherArgs(BaseModel):
        location: str = Field(min_length=1, max_length=120)

    class CodeArgs(BaseModel):
        code: str = Field(min_length=1,
                          json_schema_extra={"sensitive": True})
        timeout: int = Field(default=56000, ge=1, le=86400)

    class SaveFileArgs(BaseModel):
        filename: str = Field(min_length=1, max_length=255)
        content: str = Field(json_schema_extra={"sensitive": True})

    class GenerateImageArgs(BaseModel):
        prompt: str = Field(min_length=1, max_length=4000,
                            json_schema_extra={"sensitive": True})

    class VerifyFileArgs(BaseModel):
        path: str = Field(min_length=1, max_length=1024)

    class RememberArgs(BaseModel):
        key: str = Field(min_length=1, max_length=160)
        description: str = Field(default="",
                                 json_schema_extra={"sensitive": True})

    class RememberFailArgs(BaseModel):
        key: str = Field(min_length=1, max_length=160)
        reason: str = Field(default="",
                            json_schema_extra={"sensitive": True})

    class QueryOnlyArgs(BaseModel):
        query: str = Field(min_length=1, max_length=500,
                           json_schema_extra={"sensitive": True})

    class ExprArgs(BaseModel):
        expr: str = Field(min_length=1, max_length=10_000,
                          json_schema_extra={"sensitive": True})

    class PrioritiseArgs(BaseModel):
        subtasks: list = Field(min_length=1, max_length=12)

    class GenericArgs(BaseModel):
        """Permissive floor for unknown tools: any dict passes shape check;
        NUL bytes and non-dict payloads do not."""
        model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Validator base + registry
# ---------------------------------------------------------------------------
class ToolValidator(ABC):
    tool_name: str = ""
    schema: Optional[Type] = None

    def validate(self, raw_call: Dict[str, Any]) -> ValidationResult:
        """Tier 1: strict schema validation."""
        if self.schema is None or BaseModel is None:
            return ValidationResult("reject", correction=(
                f"{self.tool_name}: no schema available "
                f"(pydantic missing) -- failing closed."),
                error_type="no_schema")
        try:
            self.schema(**raw_call)
            # PARITY GUARANTEE (CRAIID identity property): valid calls pass
            # through with their ORIGINAL args, byte-identical -- never the
            # pydantic-coerced/default-injected model dump. Only Tier-2
            # repaired calls carry rewritten args.
            return ValidationResult("pass", args=dict(raw_call))
        except ValidationError as e:
            return ValidationResult(
                "bounce", args=dict(raw_call),
                correction=self.correction_message(raw_call, e),
                error_type="schema_validation",
            )
        except TypeError as e:
            return ValidationResult(
                "bounce", args=dict(raw_call) if isinstance(raw_call, dict)
                else {},
                correction=f"{self.tool_name}: arguments must be a JSON "
                           f"object of named fields ({e}).",
                error_type="bad_payload_shape",
            )

    def attempt_repair(self, raw_call: Dict[str, Any],
                       error: Exception | None) -> Optional[Dict[str, Any]]:
        """Tier 2: tool-specific KNOWN patterns only. Return a candidate
        args dict (re-validated by the pipeline) or None to fall through
        to Tier 3. Never generic string mangling."""
        return None

    def correction_message(self, raw_call: Dict[str, Any],
                           error: Exception) -> str:
        """Tier 3: short, specific, actionable resubmission instruction."""
        missing, bad = [], []
        if isinstance(error, ValidationError):
            for err in error.errors():
                loc = ".".join(str(p) for p in err.get("loc", ()))
                if err.get("type") == "missing":
                    missing.append(loc)
                else:
                    bad.append(f"{loc} ({err.get('type')})")
        parts = [f"Your {self.tool_name} call failed validation."]
        if missing:
            parts.append(f"Missing required field(s): {', '.join(missing)}.")
        if bad:
            parts.append(f"Invalid field(s): {', '.join(bad)}.")
        parts.append("Resubmit the call with corrected fields.")
        return " ".join(parts)

    # -- redaction helpers ---------------------------------------------
    def sensitive_fields(self) -> set:
        out = set()
        if self.schema is not None and BaseModel is not None:
            for name, f in self.schema.model_fields.items():
                extra = getattr(f, "json_schema_extra", None) or {}
                if isinstance(extra, dict) and extra.get("sensitive"):
                    out.add(name)
        return out

    def safe_preview(self, args: Dict[str, Any]) -> str:
        """Structural, non-sensitive preview only. Default: nothing."""
        return ""


class CustomsRegistry:
    def __init__(self):
        self._validators: Dict[str, ToolValidator] = {}
        self._generic = GenericSchemaValidator()

    def register(self, validator: ToolValidator) -> None:
        self._validators[validator.tool_name] = validator

    def get(self, tool_name: str) -> ToolValidator:
        v = self._validators.get(tool_name)
        if v is not None:
            return v
        # NEVER crash on unknown tool, NEVER silently pass it either:
        print(f"[CUSTOMS] unknown tool {tool_name!r} -> generic "
              f"pydantic-only validation (floor).")
        return self._generic

    def known(self) -> list:
        return sorted(self._validators.keys())


class GenericSchemaValidator(ToolValidator):
    """Registry fallback: shape floor for tools with no dedicated
    validator. Rejects non-dict payloads and NUL bytes; passes the rest
    (logged) so unknown tools keep working from day one."""
    tool_name = "_generic"
    schema = None

    def validate(self, raw_call: Dict[str, Any]) -> ValidationResult:
        if not isinstance(raw_call, dict):
            return ValidationResult(
                "bounce", args={},
                correction="Tool arguments must be a JSON object of named "
                           "fields, not a bare string or list.",
                error_type="bad_payload_shape")
        for k, v in raw_call.items():
            if isinstance(v, str) and "\x00" in v:
                return ValidationResult(
                    "bounce", args=dict(raw_call),
                    correction=f"Field {k!r} contains NUL bytes; resubmit "
                               f"without control characters.",
                    error_type="nul_bytes")
        return ValidationResult("pass", args=dict(raw_call))


# ---------------------------------------------------------------------------
# Per-tool validators
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"^https?://\S+$", re.I)
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^\s)]+)\)", re.I)


class BrowserToolValidator(ToolValidator):
    """browse: knows the pipe-got-crammed-into-URL failure class.

    Repair rules (KNOWN patterns only; anything else -> Tier 3):
      * markdown-link form  [text](https://x)  -> extract the URL
      * wrapping quotes / angle brackets / backticks / trailing ).,;] noise
      * bare domain without scheme -> https:// prefix
      * url|<int>            -> the int is max_chars (split on LAST pipe)
      * url|<anything else>  -> AMBIGUOUS: do NOT guess. Tier 3 with the
        spec's correction message (url and body concatenated with '|').
    """
    tool_name = "browse"
    schema = BrowseArgs if BaseModel is not None else None

    def validate(self, raw_call):
        res = super().validate(raw_call)
        if res.verdict == "pass":
            url = res.args.get("url", "")
            if "|" in url or not _URL_RE.match(url):
                # schema-shaped but semantically broken -> force repair path
                return ValidationResult(
                    "bounce", args=dict(res.args),
                    correction=self._pipe_msg if "|" in url else (
                        "browse: 'url' must be a single absolute http(s) "
                        "URL with no spaces. Resubmit with a clean URL."),
                    error_type="bad_url")
        return res

    _pipe_msg = ("Your browse call had url and extra content concatenated "
                 "with '|'. Resubmit with 'url' as its own field containing "
                 "ONLY the URL.")

    def attempt_repair(self, raw_call, error):
        url = raw_call.get("url")
        if not isinstance(url, str):
            return None
        u = url.strip()
        m = _MD_LINK_RE.search(u)
        if m:
            u = m.group(1)
        u = u.strip("`'\"<> ").rstrip(").,;]")
        if "|" in u:
            # Split on the LAST unescaped pipe -- bodies can contain pipes,
            # URLs rarely do after the scheme.
            head, _, tail = u.rpartition("|")
            head, tail = head.strip().rstrip(").,;]"), tail.strip()
            if tail.isdigit():                       # url|max_chars
                cand = {"url": head, "max_chars": int(tail)}
                return cand if _URL_RE.match(head) else None
            return None                              # ambiguous -> Tier 3
        if u and " " not in u and not u.lower().startswith(("http://",
                                                            "https://")):
            if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", u, re.I):
                u = "https://" + u
        if u == url or not _URL_RE.match(u):
            return None      # nothing changed, or still broken -> Tier 3
        cand = dict(raw_call)
        cand["url"] = u
        return cand

    def safe_preview(self, args):
        try:
            p = urlsplit(args.get("url", ""))
            return f"{p.scheme}://{p.netloc}"
        except Exception:
            return ""


class WebSearchBrowserValidator(ToolValidator):
    tool_name = "web_search_browser"
    schema = QueryOnlyArgs if BaseModel is not None else None

    def attempt_repair(self, raw_call, error):
        q = raw_call.get("query")
        if isinstance(q, str):
            q2 = " ".join(q.strip().strip("`'\"").split())
            if q2 and q2 != q and len(q2) <= 500:
                cand = dict(raw_call)
                cand["query"] = q2
                return cand
        return None


class SearchValidator(WebSearchBrowserValidator):
    tool_name = "search"
    schema = SearchArgs if BaseModel is not None else None


class SearchGeneralValidator(SearchValidator):
    tool_name = "search_general"


class SearchMemoryValidator(WebSearchBrowserValidator):
    tool_name = "search_memory"


class RecallValidator(WebSearchBrowserValidator):
    tool_name = "recall"


class WeatherValidator(ToolValidator):
    tool_name = "weather"
    schema = WeatherArgs if BaseModel is not None else None

    def attempt_repair(self, raw_call, error):
        loc = raw_call.get("location")
        if isinstance(loc, str):
            l2 = " ".join(loc.strip().strip("`'\"").split())[:120]
            if l2 and l2 != loc:
                return {"location": l2}
        return None


class CodeValidator(ToolValidator):
    """code: strips markdown fences -- the one deterministic, known-safe
    repair. Anything else about broken code is the model's to fix."""
    tool_name = "code"
    schema = CodeArgs if BaseModel is not None else None
    _FENCE_RE = re.compile(r"^\s*```[\w+-]*\s*\n(.*?)\n?\s*```\s*$",
                           re.DOTALL)

    def validate(self, raw_call):
        res = super().validate(raw_call)
        if res.verdict == "pass":
            code = res.args.get("code", "")
            if isinstance(code, str) and self._FENCE_RE.match(code):
                # schema-valid but fence-wrapped: force the repair path
                return ValidationResult(
                    "bounce", args=dict(res.args),
                    correction="code: submit raw Python, not a markdown "
                               "```-fenced block.",
                    error_type="markdown_fence")
        return res

    def attempt_repair(self, raw_call, error):
        code = raw_call.get("code")
        if isinstance(code, str):
            m = self._FENCE_RE.match(code)
            if m:
                cand = dict(raw_call)
                cand["code"] = m.group(1)
                return cand
        return None


class SaveFileValidator(ToolValidator):
    tool_name = "save_file"
    schema = SaveFileArgs if BaseModel is not None else None

    def validate(self, raw_call):
        res = super().validate(raw_call)
        if res.verdict == "pass":
            fname = res.args.get("filename", "")
            if "\x00" in fname or fname in (".", ".."):
                return ValidationResult(
                    "bounce", args=dict(res.args),
                    correction="save_file: invalid filename. Resubmit with "
                               "a plain filename like 'report.md'.",
                    error_type="bad_filename")
        return res

    def attempt_repair(self, raw_call, error):
        fname = raw_call.get("filename")
        if isinstance(fname, str):
            f2 = fname.strip().strip("`'\"")
            if f2 and f2 != fname:
                cand = dict(raw_call)
                cand["filename"] = f2
                return cand
        return None

    def safe_preview(self, args):
        fname = str(args.get("filename", ""))
        return fname.replace("\\", "/").rsplit("/", 1)[-1][:80]

    def correction_message(self, raw_call, error):
        return ("Your save_file call was malformed. Use "
                "[SAVE_FILE: filename.ext|<file content>] -- filename, then "
                "a single '|', then the full content.")


class GenerateImageValidator(ToolValidator):
    tool_name = "generate_image"
    schema = GenerateImageArgs if BaseModel is not None else None

    def attempt_repair(self, raw_call, error):
        p = raw_call.get("prompt")
        if isinstance(p, str):
            p2 = p.strip().strip("`'\"")
            if p2 and p2 != p and len(p2) <= 4000:
                return {"prompt": p2}
        return None


class VerifyFileValidator(ToolValidator):
    tool_name = "verify_file"
    schema = VerifyFileArgs if BaseModel is not None else None

    def safe_preview(self, args):
        p = str(args.get("path", ""))
        return p.replace("\\", "/").rsplit("/", 1)[-1][:80]


class RememberValidator(ToolValidator):
    tool_name = "remember"
    schema = RememberArgs if BaseModel is not None else None

    def correction_message(self, raw_call, error):
        return ("Your remember call was malformed. Use "
                "[REMEMBER: short_key|description of the procedure].")


class RememberFailValidator(ToolValidator):
    tool_name = "remember_fail"
    schema = RememberFailArgs if BaseModel is not None else None

    def correction_message(self, raw_call, error):
        return ("Your remember_fail call was malformed. Use "
                "[REMEMBER_FAIL: short_key|why it failed].")


class ExprValidator(ToolValidator):
    tool_name = "parse_expr"
    schema = ExprArgs if BaseModel is not None else None


class LintExprValidator(ExprValidator):
    tool_name = "lint_expr"


class PrioritiseValidator(ToolValidator):
    tool_name = "prioritise"
    schema = PrioritiseArgs if BaseModel is not None else None

    def attempt_repair(self, raw_call, error):
        st = raw_call.get("subtasks")
        if isinstance(st, str) and st.strip():
            parts = [s.strip() for s in st.split("|") if s.strip()]
            if 1 <= len(parts) <= 12:
                return {"subtasks": parts}
        return None


# ---------------------------------------------------------------------------
# Registry instance + registration
# ---------------------------------------------------------------------------
registry = CustomsRegistry()
for _v in (BrowserToolValidator(), SearchValidator(),
           SearchGeneralValidator(), SearchMemoryValidator(),
           RecallValidator(), WebSearchBrowserValidator(),
           WeatherValidator(), CodeValidator(), SaveFileValidator(),
           GenerateImageValidator(), VerifyFileValidator(),
           RememberValidator(), RememberFailValidator(),
           ExprValidator(), LintExprValidator(), PrioritiseValidator()):
    registry.register(_v)


# ---------------------------------------------------------------------------
# Bounce ledger (Tier-3 retry cap)
# ---------------------------------------------------------------------------
def _digest(args: Any) -> str:
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        blob = repr(args)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def _bounce_count(origin: str, tool: str, dig: str) -> int:
    now = time.time()
    with _lock:
        # TTL prune
        for k in [k for k, (_, ts) in _ledger.items()
                  if now - ts > _LEDGER_TTL_SEC]:
            _ledger.pop(k, None)
        # size cap: evict oldest-touched entries first
        if len(_ledger) >= _LEDGER_MAX:
            for k in sorted(_ledger, key=lambda k: _ledger[k][1])[
                    :len(_ledger) - _LEDGER_MAX + 1]:
                _ledger.pop(k, None)
        cnt, _ = _ledger.get((origin, tool, dig), (0, now))
        _ledger[(origin, tool, dig)] = (cnt + 1, now)
        return cnt + 1


# ---------------------------------------------------------------------------
# The chokepoint
# ---------------------------------------------------------------------------
def inspect(tool_name: str, raw_args: Any, origin: str,
            intent: str | None = None) -> ValidationResult:
    """Run one tool call through Customs. FAIL CLOSED: any internal error
    rejects the call rather than passing it through unvalidated.

    intent (v2.13.2): optional requested/opportunistic label from
    intent_scope classification -- recorded in the hash-chain event so
    scope-creep is auditable alongside validation verdicts."""
    global _warned_disabled
    if not is_enabled():
        if not _warned_disabled:
            _warned_disabled = True
            print("[CUSTOMS] disabled (customs_enabled=false) -- "
                  "tool calls pass through unvalidated.")
        return ValidationResult("disabled",
                                args=raw_args if isinstance(raw_args, dict)
                                else {"_raw": raw_args})
    try:
        v = registry.get(tool_name)
        # Pass the payload through UNWRAPPED: validators must see the real
        # shape so a non-dict payload bounces as bad_payload_shape instead
        # of being laundered into {"_raw": ...} and passing the floor.
        raw = raw_args

        # Tier 1
        res = v.validate(raw)
        if res.verdict == "pass":
            _log_event(v, tool_name, origin, res, intent)
            return res

        # Tier 2 -- known per-tool patterns only, then re-validate strictly
        try:
            cand = v.attempt_repair(raw, None)
        except Exception as rep_err:
            print(f"[CUSTOMS] {tool_name} repair heuristic raised "
                  f"{type(rep_err).__name__}: {rep_err} -- falling to "
                  f"Tier 3 (never guess).")
            cand = None
        if cand is not None:
            res2 = v.validate(cand)
            if res2.verdict == "pass":
                out = ValidationResult(
                    "repaired", args=res2.args,
                    repaired_fields=tuple(
                        k for k in cand
                        if cand.get(k) != raw.get(k)))
                _log_event(v, tool_name, origin, out, intent)
                return out
            # repair didn't produce a schema-clean call: do NOT guess more

        # Tier 3 -- bounce back to the originating model (capped)
        dig = _digest(raw)
        n = _bounce_count(origin, tool_name, dig)
        if n <= MAX_BOUNCES:
            out = ValidationResult(
                "bounce", args=raw,
                correction=(res.correction or v.correction_message(
                    raw, ValueError("validation failed")))
                + f" (attempt {n}/{MAX_BOUNCES})",
                error_type=res.error_type or "schema_validation")
            _log_event(v, tool_name, origin, out, intent)
            return out

        # Tier 4 -- hard reject. Visible garbage, never a silent no-op.
        out = ValidationResult(
            "reject", args=raw,
            correction=(f"[CUSTOMS REJECT] {tool_name} call failed "
                        f"validation after {MAX_BOUNCES} correction "
                        f"attempts and was not executed. Last error: "
                        f"{res.error_type or 'schema_validation'}."),
            error_type=res.error_type or "schema_validation")
        _log_event(v, tool_name, origin, out, intent)
        return out

    except Exception as e:
        # FAIL CLOSED. A daemon whose crash mode is "let everything
        # through" is worse than no daemon at all.
        msg = (f"[CUSTOMS REJECT] internal Customs error while validating "
               f"{tool_name}: {type(e).__name__}. The call was NOT "
               f"executed (fail-closed).")
        print(f"[CUSTOMS] INTERNAL ERROR ({tool_name}/{origin}): "
              f"{type(e).__name__}: {e} -> rejecting call (fail-closed)")
        try:
            _audit({"tool": tool_name, "origin": origin,
                    "verdict": "reject",
                    "error_type": "customs_internal_error",
                    "exc": type(e).__name__})
        except Exception:
            pass
        return ValidationResult("reject", correction=msg,
                                error_type="customs_internal_error")


def _log_event(v: ToolValidator, tool: str, origin: str,
               res: ValidationResult,
               intent: str | None = None) -> None:
    """Hash-chain + stdout. Field NAMES, digest, verdict -- no payload."""
    try:
        args = res.args if isinstance(res.args, dict) else {}
        detail = {
            "tool": tool, "origin": origin, "verdict": res.verdict,
            "fields": sorted(args.keys()),
            "digest": _digest(args)[:12],
        }
        if intent:
            detail["intent"] = intent
        if res.error_type:
            detail["error_type"] = res.error_type
        if res.repaired_fields:
            detail["repaired"] = sorted(res.repaired_fields)
        try:
            pv = v.safe_preview(args)
            if pv:
                detail["preview"] = pv[:80]
        except Exception:
            pass
        _audit(detail)  # pass events are chained too (spec 5d: one trail)
        if res.verdict in ("repaired", "bounce", "reject"):
            print(f"[CUSTOMS] {res.verdict.upper()} {tool} "
                  f"(origin={origin}, "
                  f"err={res.error_type or '-'}, "
                  f"fields={','.join(detail['fields'])})")
    except Exception as e:
        print(f"[CUSTOMS] logging error (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Tag adapter -- maps the agentic parser's (action_type, content) pairs to
# structured args and back, preserving the content SHAPE main.py expects.
# ---------------------------------------------------------------------------
_TAG_TO_ARGS: Dict[str, Callable[[Any], Dict[str, Any]]] = {
    "browse":             lambda c: {"url": c if isinstance(c, str) else str(c)},
    "search":             lambda c: {"query": str(c)},
    "search_general":     lambda c: {"query": str(c)},
    "web_search_browser": lambda c: {"query": str(c)},
    "search_memory":      lambda c: {"query": str(c)},
    "recall":             lambda c: {"query": str(c)},
    "weather":            lambda c: {"location": str(c)},
    "code":               lambda c: {"code": str(c)},
    "generate_image":     lambda c: {"prompt": str(c)},
    "verify_file":        lambda c: {"path": str(c)},
    "lint_expr":          lambda c: {"expr": str(c)},
    "parse_expr":         lambda c: {"expr": str(c)},
    "prioritise":         lambda c: {"subtasks": str(c)},
    "save_file":          lambda c: (
        {"filename": c[0], "content": c[1]} if isinstance(c, tuple)
        else {"filename": "", "content": str(c)}),
    "remember":           lambda c: dict(zip(
        ("key", "description"),
        (str(c).split("|", 1) + [""])[:2])),
    "remember_fail":      lambda c: dict(zip(
        ("key", "reason"),
        (str(c).split("|", 1) + [""])[:2])),
}

_ARGS_TO_TAG: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "browse":             lambda a: a["url"],
    "search":             lambda a: a["query"],
    "search_general":     lambda a: a["query"],
    "web_search_browser": lambda a: a["query"],
    "search_memory":      lambda a: a["query"],
    "recall":             lambda a: a["query"],
    "weather":            lambda a: a["location"],
    "code":               lambda a: a["code"],
    "generate_image":     lambda a: a["prompt"],
    "verify_file":        lambda a: a["path"],
    "lint_expr":          lambda a: a["expr"],
    "parse_expr":         lambda a: a["expr"],
    "prioritise":         lambda a: (
        a["subtasks"] if isinstance(a["subtasks"], str)
        else " | ".join(a["subtasks"])),
    "save_file":          lambda a: (a["filename"], a["content"]),
    "remember":           lambda a: f"{a['key']}|{a.get('description', '')}",
    "remember_fail":      lambda a: f"{a['key']}|{a.get('reason', '')}",
}


@dataclass
class TagResult:
    allowed: bool
    content: Any          # (possibly repaired) content, original shape
    verdict: str
    message: str = ""     # correction / rejection text for tool_results_acc


def inspect_tag(action_type: str, content: Any, origin: str,
                intent: str | None = None) -> TagResult:
    """Customs gate for parse_agent_actions output. Shape-preserving:
    callers keep using `content` exactly as before. FAIL CLOSED."""
    try:
        if not is_enabled():
            return TagResult(True, content, "disabled")
        mapper = _TAG_TO_ARGS.get(action_type)
        if mapper is None:
            # unknown tag type: generic floor (log + pass) -- parity with
            # registry fallback behavior.
            res = inspect(action_type, {"_raw": content}, origin,
                          intent=intent)
            return TagResult(res.allowed, content, res.verdict,
                             res.correction)
        res = inspect(action_type, mapper(content), origin,
                      intent=intent)
        if not res.allowed:
            return TagResult(False, content, res.verdict, res.correction)
        if res.verdict == "repaired":
            try:
                content = _ARGS_TO_TAG[action_type](res.args)
            except Exception:
                # repaired args unmappable -> keep original (validated
                # semantics unclear) and bounce instead of guessing.
                return TagResult(False, content, "bounce",
                                 f"{action_type}: repair could not be "
                                 f"applied; resubmit the call.")
        return TagResult(True, content, res.verdict)
    except Exception as e:
        print(f"[CUSTOMS] inspect_tag internal error "
              f"({action_type}/{origin}): {type(e).__name__}: {e} "
              f"-> rejecting (fail-closed)")
        return TagResult(False, content, "reject",
                         f"[CUSTOMS REJECT] internal error validating "
                         f"{action_type}; call not executed.")
