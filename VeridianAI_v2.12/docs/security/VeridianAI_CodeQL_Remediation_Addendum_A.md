# Security Remediation Report — Addendum A

## Information-Exposure Sweep · exception text in responses

| Field | Detail |
|---|---|
| **Parent document** | CodeQL Static-Analysis Findings #71–#87 |
| **Project** | VeridianAI — backend (v2.13; folder rename from v2.12 pending) |
| **File changed** | `backend/sage_engine.py` |
| **Class addressed** | Information exposure through an exception (CWE-209) — client-facing |
| **Date** | 20 July 2026 |
| **Result** | Client-facing class fully closed · CodeQL re-scan clean |

---

## 1. Purpose

Finding #71 was a single CodeQL-flagged exception-exposure in an archive route, fixed at its source. This addendum records the proactive sweep that followed: a pass across `sage_engine.py` to find and close every remaining client-facing return that hands raw exception text to the caller — the same vulnerability class, whether or not CodeQL had flagged it. Five client-facing spots were remediated; a further fifteen exception-bearing strings were reviewed and confirmed safe to leave in place.

## 2. Remediated (client-facing)

Each of these returned raw exception detail to an API caller. All now log the detail server-side and return a generic, user-safe message.

| Function | Endpoint / caller | Was | Now returns to client |
|---|---|---|---|
| `archive_conversation` | `POST /api/archives/save` | `str(e)` | "Could not save the archive." |
| `save_to_downloads` | `[SAVE_FILE]` handler | `str(e)` | "Could not save the file." |
| `set_tavily_key` | Settings — API key save | `str(e)` | "Could not save the API key." |
| `delete_tavily_key` | Settings — API key remove | `str(e)` | "Could not remove the API key." |
| `daemon_status` | `GET /api/daemon/status` | `str(e)[:120]` | "daemon communication error" |

The pattern applied throughout — full detail retained server-side for debugging, a generic and accessible message (WCAG 2.1 SC 3.3.1) to the client, no internal paths or implementation detail disclosed:

```python
except Exception as e:
    print(f"[TAG] <operation> failed: {e}")   # server-side only
    return {"success": False, "error": "<generic, user-safe message>"}
```

## 3. Reviewed & cleared (safe to leave)

Fifteen further strings embed exception text but are **not** client-facing REST error objects. Each was traced to its destination and confirmed safe; redacting them would reduce functionality for no security gain.

- **Tool results consumed by the model** — Tavily search, weather lookup, code execution, file verify, browser fetch/search, and the MCP tool-dispatch handlers (`browse` / `web_search` / `verify_file`). These return into the agentic loop so the model can reason about or retry a failure, and are rendered only in the requester's own chat. The model needs the detail; they are never emitted as a REST error object.
- **Warnings about the user's own uploaded file** — text / PDF / DOCX / spreadsheet / JSON extraction and HEIC image conversion, returned to the uploader as a `(text, warning)` pair. Operational detail about the caller's own content, not PHI or credentials.

Two reassurances: none of these can echo a secret (for example, the Tavily API key travels in the request body, out of the exception's reach), and CodeQL flagged none of them — consistent with this reading.

## 4. Optional hardening (not required)

In a multi-user deployment, a raw `{e}` in a model-facing tool error could include a server filesystem path — operational metadata, not sensitive data. If the app is ever exposed to semi-trusted non-owner users, swapping `{e}` for `{type(e).__name__}` on those tool-result strings keeps the error class useful to the model while dropping raw paths. Not required for the single-user / owner threat model.

## 5. Verification & status

- `py_compile` passed after every change.
- `grep` confirms zero client-facing "error"/"reason" fields interpolate exception text.
- CodeQL re-scan on push: no new information-exposure alerts.

**Status:** the client-facing information-exposure class is now fully closed — the original #71 plus four siblings and `daemon_status` — and the remaining exception-bearing strings are a distinct, model-facing category that is safe as-is.

---

*Prepared with Claude (Cowork) · Addendum A to CodeQL Remediation Report #71–#87 · 20 July 2026*
