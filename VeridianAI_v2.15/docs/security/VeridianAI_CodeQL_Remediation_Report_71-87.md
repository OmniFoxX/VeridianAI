# Security Remediation Report — GitHub CodeQL Findings #71–#87

| Field | Detail |
|---|---|
| **Project** | VeridianAI v2.12 — backend |
| **Files changed** | `backend/sage_engine.py`, `backend/main.py` |
| **Scanner** | GitHub CodeQL (Python security queries) |
| **Rules involved** | `py/path-injection`, `py/clear-text-logging-sensitive-data`, `py/stack-trace-exposure` |
| **Compliance context** | HIPAA Security Rule (§164.312); WCAG 2.1 |
| **Remediation date** | 20 July 2026 |
| **Final scan result** | **0 open · 87 closed** |

---

## 1. Executive Summary

On 20 July 2026, GitHub's CodeQL scanner flagged five new alerts (#71–#75) in the VeridianAI v2.12 backend following an overnight update. All five were remediated. Because three of them belonged to CodeQL's path-traversal class — which is only cleared when the fix matches a sanitizer shape the analyzer explicitly models — remediation proceeded through three refinements, during which CodeQL surfaced and then closed a cascade of related path-traversal alerts (through #87). A latent namespace-validation gap was also discovered and closed along the way.

The final CodeQL scan reports **0 open and 87 closed.** Reduced to first principles, the original five alerts stemmed from two root causes: untrusted input reaching a filesystem operation without a validation boundary the analyzer recognizes, and sensitive or internal information crossing a trust boundary outward — to API clients and to logs. Every fix was made to respect and advance the project's HIPAA and WCAG goals.

## 2. Findings & Disposition

| Alert(s) | Rule / Severity | Location | Disposition |
|---|---|---|---|
| #71 | Information exposure through an exception — Medium | `main.py:2607` | Fixed — generic error + server-side log |
| #72 | Clear-text logging of sensitive information — High | `main.py:3963` | Fixed — encrypted audit event |
| #73–#75 | Uncontrolled data used in path expression — High | `sage_engine.py` (archive fns) | Fixed — realpath + startswith barrier |
| #75–#87 | Uncontrolled data used in path expression (interim) — High | `sage_engine.py` | Transient during remediation; all closed |
| Latent | Namespace not constrained before path build | `main.py` archive routes | Hardened — `_safe_ns()` applied |

> **Note:** alerts #75–#87 were transient. They appeared and closed within the remediation process as each path-injection fix was refined toward CodeQL's recognized sanitizer shape; none represent code that ever shipped as an open vulnerability.

## 3. Root-Cause Analysis

Rather than treat the alerts as five (then eight) separate defects, remediation targeted two boundaries:

- **Boundary A — input into a filesystem sink.** Archive filenames (and, secondarily, the per-user namespace) flowed from HTTP requests into path construction. The fix installs a normalize-and-verify gate that confines every resolved path to the namespace's archive directory.
- **Boundary B — sensitive/internal data leaving outward.** Raw exception text reached API clients, and account identifiers were written to a clear-text log. The fix installs a redaction/generic-error layer and routes the security event to a tamper-evident, encrypted audit log.

## 4. Remediation Detail

### 4.1 Path traversal (#73–#75, and the #75–#87 cascade)

The first line of defense already existed — filenames pass through `_safe_archive_name()`, which reduces any input to a bare basename and requires an `archive_*.json` allowlist:

```python
def _safe_archive_name(filename):
    name = Path((filename or "").strip()).name   # strips any directory part
    if not (name.startswith("archive_") and name.endswith(".json")):
        return None
    return name
```

CodeQL nonetheless flagged the sinks, because it does not model that guard as sufficient. Reaching zero required matching a sanitizer shape the analyzer recognizes. Three passes were needed:

1. **Containment helper.** An `_archive_path()` helper checked `root in target.parents`. CodeQL does not model parent-containment as a sanitizer and cannot carry "already validated" across a function-return boundary, so it flagged the helper plus every caller.
2. **Inline resolve check.** An inline check of `str(target.resolve()).startswith(...)`. Two problems: the sinks still consumed the raw `Path` `target` (graph-distinct from the checked string), and pathlib `.resolve()` on tainted input is itself a flagged path expression — which is why the count rose to eight.
3. **Shipped fix.** `os.path.realpath` normalization plus a `startswith` check on that exact string, consumed directly at every sink. This matches CodeQL's documented safe pattern and cleared all alerts.

The shipped barrier, identical across `set_archive_title`, `load_archive`, and `delete_archive`:

```python
trusted_root = os.path.realpath(str(_archive_folder(ns)))
target = os.path.realpath(os.path.join(trusted_root, name))
if not target.startswith(trusted_root + os.sep):
    return {"success": False, "error": "Archive not found"}
if not os.path.exists(target):            # existence check is guarded too
    return {"success": False, "error": "Archive not found"}
```

Every sink — `os.path.exists()`, `open(target, 'rb')`, `os.remove()` — consumes the same checked string. The trailing `os.sep` defeats the `.../archives_evil` sibling-prefix trap, and because `realpath` resolves symlinks before the check, a symlinked archive pointing outside the folder is now blocked as well — a hardening the earlier unresolved path would have missed.

### 4.2 Information exposure through an exception (#71)

`main.py:2607` — sink is the `/api/archives/title` route return; the leak originated in `sage_engine.set_archive_title`'s `except` (same pattern in `load_archive`). The handler returned `{"error": str(e)}`, shipping raw exception text to the client. The fix logs detail server-side and returns a generic message:

```python
except Exception as e:
    print(f"[ARCHIVE] set_archive_title failed for {name!r}: {e}")  # server-side only
    return {"success": False, "error": "Could not update the archive title."}
```

- **HIPAA:** minimum-necessary — no internal system detail disclosed.
- **WCAG 2.1 (3.3.1 Error Identification):** users receive a clear, human-readable message instead of a raw exception string.

### 4.3 Clear-text logging of sensitive information (#72)

`main.py:3963` — an MFA-reset event was logged via `print(...)` with account names in clear-text stdout. An admin resetting a user's MFA is exactly what an audit trail should capture; the problem is the clear-text `print` sink. The event is routed, as metadata only, to the tamper-evident hash-chain audit log (the same sink `customs_daemon` uses), encrypted at rest, with the fallback on the `veridian` logger rather than `print`. No secret is ever read or logged — `reset_user()` returns only `{success, had_mfa}`.

- **HIPAA:** §164.312(b) Audit Controls — the event is preserved for accountability while identifiers are encrypted at rest and removed from clear-text stdout.

### 4.4 Namespace validation hardening (latent gap)

While tracing the path flow, the archive routes were found to pass `_session_ns(request)` **without** `_safe_ns()` — unlike the upload, download, and settings routes, which already wrap it. `_NS_RE` (`^[A-Za-z0-9_-]{1,64}$`) rejects `/`, `\`, `.` and `:`, so it blocks `../` and absolute/drive paths. All five archive routes were wrapped, and the invariant documented at the `user_data_dir` chokepoint.

## 5. HIPAA & WCAG Mapping

| Remediation | HIPAA Security Rule | WCAG 2.1 |
|---|---|---|
| Archive path-traversal barrier | §164.312(a)(1) Access Control — confines access to a namespace's own PHI archives | — |
| Generic client error messages | Minimum-necessary — no internal detail disclosed | SC 3.3.1 Error Identification |
| MFA-reset audit event | §164.312(b) Audit Controls — retained, encrypted, tamper-evident | — |
| Namespace validation | §164.312(a)(1) Access Control | — |

## 6. Verification

- `python -m py_compile` on both files — pass.
- Confirmed all three archive functions route through the barrier, the interim helper was removed, and no raw tainted-path sinks remain.
- A functional traversal test confirmed the barrier blocks `../` traversal, the sibling-prefix trap, and absolute paths, while allowing legitimate archive names.
- An offline Fernet round-trip confirmed the audit-event detail decrypts back to the original metadata and that account names do not appear in the log line.
- **Authoritative result:** the GitHub CodeQL workflow was re-run on the pushed commit and reports **0 open / 87 closed.**

## 7. CodeQL Sanitizer Lessons (for future path fixes)

- **Recognized:** `os.path.realpath` / `os.path.abspath` normalization followed by a `startswith` (or `commonpath`) check against a trusted prefix, where the checked value and the sink value are the **same** dataflow node.
- **Not recognized:** parent-containment (`x in target.parents`); custom validation buried in a helper whose return value is consumed elsewhere; or a check on a value derived from the sink value (`str(x)` checked while the `Path` `x` is used).
- **Guard every sink,** including `.exists()` — CodeQL treats existence disclosure on a tainted path as its own vulnerability class.
- **Prefer `os.path` string operations** over pathlib `.resolve()` when you need the analyzer to see the barrier, since `.resolve()` on tainted input is itself flagged.

## 8. Residual & Recommended Follow-ups

- **Four more `error: str(e)` returns** in `sage_engine.py` — same information-exposure pattern, then unflagged by CodeQL. *(Closed in Addendum A.)*
- **Whole audit-log encryption at rest** while preserving the hash-chain tamper-evidence guarantee. *(Delivered in Addendum B.)*
- **Optional consolidation:** the three archive functions share an identical inline barrier; once no further CodeQL churn is expected, this may be centralized to reduce duplication.

---

*Prepared with Claude (Cowork) · VeridianAI v2.12 · 20 July 2026*
