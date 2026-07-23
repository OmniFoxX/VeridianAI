# Security Remediation Report — `browser_tool.py` Web-Browsing Engine

| Field | Detail |
|---|---|
| **Project** | VeridianAI v2.12 — backend |
| **Component** | `backend/browser_tool.py` (Playwright web-browsing engine driven by Toga) |
| **File(s) changed** | `backend/browser_tool.py` |
| **Audit trigger** | Manual audit prompted by recent industry reporting on indirect prompt-injection via web content |
| **Environment** | Windows 11 · Python 3.14 · Playwright 1.58.0 · playwright-stealth |
| **Compliance context** | HIPAA Security Rule (§164.312 — transmission security, integrity); WCAG 2.2 AA (this engine is the platform's core accessibility feature) |
| **Remediation date** | 22 July 2026 |
| **Status** | **All four findings closed · verified in-code · `--no-sandbox` banner confirmed gone in live run** |

---

## 1. Executive Summary

`browser_tool.py` is not an optional convenience in VeridianAI — it is the mechanism by which users who cannot browse the web independently do so at all, via the assistant (Toga). That makes its security boundary unusually load-bearing: whatever the browser returns flows directly into Toga's context window, and whatever the browser exposes on the network is exposed on behalf of a user who cannot see the warning signs themselves.

A manual audit on 22 July 2026 identified four issues — two critical, two significant — spanning the two trust boundaries that matter for a browsing agent: **untrusted content coming in** from the open web, and **unsafe defaults going out** in how the browser is launched and how it talks to the network. All four are now remediated in a single rewrite that preserves every accessibility and human-mimicry capability and every existing method signature (full backward compatibility with `sage_engine.py`).

Reduced to first principles, the four findings stem from three root assumptions the original code inherited without questioning: that page content is *data* rather than *executable input*; that a browser-automation library launches securely *by default*; and that transport-layer failures would be *visible*. Each of those assumptions was false, and each is now corrected explicitly at the boundary rather than left implicit.

The headline fix — the one worth broad visibility — is **CRITICAL #2**: the `--no-sandbox` flag that had been silently disabling Chromium's process isolation was traced to a Playwright-level default (`chromium_sandbox=False`), not to VeridianAI code, and is now closed for every browser channel.

## 2. Findings & Disposition

| # | Severity | Finding | Root boundary | Disposition |
|---|---|---|---|---|
| 1 | **Critical** | No sanitization on the browser-content return boundary — square-bracket tool-call patterns on a malicious page flowed into Toga's context and were executed | Untrusted content inbound | **Fixed** — single-choke-point sanitizer neutralizes all tag shapes |
| 2 | **Critical** | `--no-sandbox` silently disabling Chromium process isolation on every channel | Unsafe launch default | **Fixed at root cause** — `chromium_sandbox=True`; Brave via `executable_path` |
| 3 | **Significant** | `ignore_https_errors=True` bypassing TLS validation on every page | Unsafe transport default | **Fixed** — default `False`, explicit opt-in + warning |
| 4 | **Significant** | Dead SearXNG default (`searx.be`) — `[WEB_SEARCH:]` silently failed | Availability / correctness | **Fixed** — DuckDuckGo default; SearXNG kept as opt-in |

**Net:** 1 file changed. Four issues fixed for real (no suppressions), plus two defense-in-depth hardenings surfaced during the work (reCAPTCHA token injection; `check_page_health()` title/URL sanitization). File passes `py_compile`; sanitizer passes an 8-payload injection self-test against the live parser's own regexes.

## 3. Root-Cause Analysis

Rather than treat the findings as four unrelated defects, remediation targeted the underlying assumptions:

- **Boundary A — untrusted web content into the tool parser (Finding 1).** `sage_engine.py` parses ~24 square-bracket tool tags (`[BROWSE:…]`, `[REMEMBER:…]`, `[SAVE_FILE:…]`, `[GENERATE_IMAGE]`, …) out of model-visible text, *plus* an orphan/partial detector that reacts to an unclosed `[WORD` with no closing bracket. Page content was returned raw, so any of those shapes on a hostile page became a live, indirect tool call. The fix installs a normalize-at-the-boundary gate so page-derived text is inert to the parser while remaining fully readable to the user.
- **Boundary B — the browser's own defaults (Findings 2 & 3).** A browsing agent is only as safe as the process it launches and the transport it trusts. Two defaults undermined both: Playwright's `chromium_sandbox` defaults to `False`/`None`, so it appended `--no-sandbox` itself; and `ignore_https_errors=True` made a man-in-the-middle invisible. Both are now set explicitly to the safe value, with a logged, opt-in escape hatch for the rare host that genuinely needs otherwise.
- **Boundary C — silent failure masquerading as capability (Finding 4).** The default search engine pointed at a public SearXNG instance that no longer serves the JSON API, so web search failed quietly. Replaced with a live-navigated DuckDuckGo default that actually works.

## 4. Remediation Detail

### 4.1 CRITICAL #1 — Content-return sanitization

Every page-derived value now passes through one private helper before crossing back to the engine. It swaps tag-like ASCII brackets for Unicode mathematical white square brackets (`U+27E6 ⟦` / `U+27E7 ⟧`): human-readable (important — this text is read aloud to blind and low-vision users) but completely inert to a parser that only ever matches ASCII `[`/`]`.

```python
_TAGISH_TOKEN_RE = re.compile(r"\[\s*[A-Za-z_][^\[\]]*\]")   # [WORD], [WORD:...], [edit]
_TAGISH_OPEN_RE  = re.compile(r"\[(?=\s*[A-Za-z_])")          # bare, unclosed "[WORD"

def _sanitize_browser_content(text):
    # Pass 1: closed tag-like tokens -> swap both brackets
    text = _TAGISH_TOKEN_RE.sub(lambda m: "⟦" + m.group(0)[1:-1] + "⟧", text)
    # Pass 2: bare "[WORD" openings -> neutralize the opener so the
    #         orphan/partial-tag detector cannot pair it with a later "]"
    text = _TAGISH_OPEN_RE.sub("⟦", text)
    return text
```

Two passes are required because the engine's *orphan/partial* detector acts on an unclosed `[WORD` even with no closing bracket — a single closed-token pass would miss that vector. Applied to `extract_text()`, `page_content()`, `extract_links()` (per link), and `check_page_health()` (title + URL). Numeric `[1]` citation markers and `[...]` ellipses are deliberately preserved — they never match a tool tag (tags require a leading letter/underscore) and read more naturally left alone. Our own trusted, tool-originated tags (e.g. the `[REMEMBER:…]` we emit after a CAPTCHA solve) are *not* passed through the sanitizer — only untrusted, page-derived content is.

### 4.2 CRITICAL #2 — `--no-sandbox` (the headline fix)

**Symptom:** Chromium showed the "unsupported command-line flag: --no-sandbox — stability and security will suffer" banner on every run, regardless of which Chromium-based browser served the session.

**Root cause:** This was *not* VeridianAI code adding the flag. Playwright's `chromium_sandbox` launch option **defaults to `False`/`None`**, and `launch_persistent_context()` therefore appends `--no-sandbox` *itself* on every attempt — bundled Chromium, Chrome, Edge, or Brave alike. Because it is a Playwright-level default rather than a Windows or per-browser quirk, it appeared identically across channels. (Confirmed against Playwright 1.58.0 / Windows 11 / Python 3.14.)

**Fix:** force the sandbox on in the launch kwargs, and never manage `--no-sandbox` in `args` at all:

```python
allow_no_sandbox = os.getenv("VERIDIAN_ALLOW_NO_SANDBOX") == "1"
chromium_sandbox = not allow_no_sandbox            # True in normal operation

base_kwargs = dict(
    user_data_dir=str(self.profile_dir),           # persistent profile intact
    ignore_https_errors=self.ignore_https_errors,  # see 4.3
    ignore_default_args=["--enable-automation"],
    chromium_sandbox=chromium_sandbox,             # THE FIX: keeps process isolation
    args=[...],                                     # no --no-sandbox here, ever
)
```

The persistent profile and `launch_persistent_context()` are untouched. The `VERIDIAN_ALLOW_NO_SANDBOX=1` environment variable is the *only* way to disable the sandbox, for genuinely locked-down hosts, and it emits a loud warning when used. Each launch attempt now logs the channel and the sandbox state (`process sandbox ON`) so an operator can confirm from the console.

**Channel selection & Brave.** Brave is not one of Playwright's official `channel` values (only `chrome`/`msedge` and their beta/dev/canary variants are), so `channel="brave"` would error. A new `_detect_brave_path()` helper locates the Brave binary across Windows/macOS/Linux standard install locations and launches it via `executable_path`. The default preference order is **Brave (when present) → Chrome → Edge → bundled Chromium**, so Brave — the recommended browser — is used when installed, while a machine with none of them still works. Every step keeps the sandbox on; the fallback chain is about browser *identity*, not safety.

> **Operational note:** if the sandbox ever fails to initialize *with* `chromium_sandbox=True`, the usual Windows cause is running the application **elevated (as Administrator)** — Chromium will not sandbox under an elevated token. The correct response is to run VeridianAI unelevated, not to disable the sandbox.

### 4.3 SIGNIFICANT #3 — TLS validation

`ignore_https_errors` moved from a hardcoded `True` to a constructor parameter defaulting to **`False`**. TLS certificates are now validated on every page. When the flag is explicitly enabled for a known-bad-cert host, a warning is logged so the reduced protection is never silent. This matters doubly alongside Finding 1: without cert validation, a man-in-the-middle could forge the very page whose content we now sanitize.

### 4.4 SIGNIFICANT #4 — Search default

`search()` now defaults to `engine="duckduckgo"`, driven through the real (stealth, human-timed) browser via `goto()` to `https://duckduckgo.com/?q=…`, extracting results from the rendered DOM with resilient multi-selector strategies and a fallback to the no-JS `html.duckduckgo.com` endpoint. The dead `searx.be` default is gone; SearXNG remains fully supported as an explicit opt-in that requires a `base_url`. The method signature is unchanged, so `sage_engine.py`'s `browser.search(query, max_results=…)` binds exactly as before. All result fields (title/url/snippet) pass through the Finding 1 sanitizer.

### 4.5 Defense-in-depth surfaced during the work

- **reCAPTCHA token injection.** The solved token was previously interpolated into a JavaScript string via f-string. It is now passed as a bound `page.evaluate()` argument, so a hostile token value cannot break out of the quotes.
- **`check_page_health()`** now sanitizes the page-controlled `title` and `status` (URL) fields it returns.

## 5. Preserved Capabilities (Absolute Constraints)

Every core accessibility / human-mimicry capability was verified intact: playwright-stealth (`Stealth().apply_stealth_async`), human timing (`_human_delay`, `_random_mouse_move`), per-domain rate limiting, exponential backoff, the full CAPTCHA detect/solve/fallback pipeline, per-user namespace isolation (`ns`, `_resolve_profile_dir`), cookie opt-in behavior, `signup_auto_detect()` / `_detect_form_fields()`, the silent-fail IPC bridge on port 9999, all interaction primitives, and the `get_browser()` per-namespace singleton. **All existing method signatures are unchanged.**

## 6. Verification

- **Static:** `python -m py_compile backend/browser_tool.py` — clean.
- **Injection self-test:** the sanitizer was run against 8 payloads (closed `[BROWSE:…]`, multi-line `[REMEMBER:…]`, `[GENERATE_IMAGE]`, an **unclosed** `[SAVE_FILE:…` with no closing bracket, an **end-of-string partial** `[REMEMBER`, a whole-file `[SAVE_FILE:…]`, a nested tag, and wiki-style `[edit]`) checked against the engine's *actual* parser regexes (`sage_engine.py:2267–2304`) plus its orphan/partial detectors. **8/8 neutralized, 0 leaks;** numeric `[1]` and `[...]` confirmed preserved.
- **Channel logic:** the launch-attempt builder was unit-checked across all four scenarios (default with/without Brave, explicit `channel="brave"` present/absent, explicit `channel="chrome"`); confirmed no launch override ever carries both `channel` and `executable_path`.
- **Live:** the `--no-sandbox` banner is **confirmed gone** in a real run on the target Windows 11 machine.

## 7. Residual Notes

- **DuckDuckGo selector drift.** DDG periodically renames its SERP containers; the extractor uses multiple selector strategies plus the stable HTML-endpoint fallback, but a live `[WEB_SEARCH:]` smoke test is worth repeating after major DDG changes.
- **`VERIDIAN_ALLOW_NO_SANDBOX=1`** should remain unset in all normal and distributed deployments. It exists only for locked-down hosts that cannot sandbox, and it self-documents via a startup warning.

---

*Remediation by Claude, in-codebase context. No findings were suppressed; all four were fixed at the boundary. Consistent with the project's HIPAA and WCAG 2.2 AA goals and its distribution-readiness posture (no user-specific hardcoding; cross-platform Brave detection).*
