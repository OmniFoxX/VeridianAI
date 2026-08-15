# VeridianAI — security session, 2026-08-13

**Starting point:** 37 open CodeQL alerts on push `#65` (`57c3377`), 2 Critical.
**Ending point:** **0 open.** Test suite 56/56 green in both trees. Five commits pushed.

| | |
|---|---|
| Alerts fixed by code | **12** |
| Alerts dismissed, each naming its guard and line | **25** |
| Open now | **0** |
| Real defects found that **no scanner flagged** | **5** |
| Tests: before → after | 52/54 green → **56/56 green** |
| New test files | 4 (98 new assertions) |

---

## Commits

| SHA | What |
|---|---|
| `ed2aeb4` | 2 Critical SSRF, the read-and-execute chain, the namespace guard, repo hygiene |
| `4140b5e` | the last three stack-trace surfaces |
| `a916dec` | export symlink containment; tests that killed themselves printing output |
| `4152e2f` | **`VeridianAI_v2.15/` → `app/`** — 464 files, all as renames |
| `4361df2` | de-versioning + developer-specific paths out of CRAIID |

---

# Part 1 — What changed, and where

## 1. Two Critical SSRFs — the relay client

**`backend/net_guard.py`** now owns the outbound address policy: `resolve_validated()` and `pinned_base()`, moved out of `skill_api`, raising a plain `UrlNotAllowed` so non-FastAPI callers can import them.
**`backend/relay_client.py`** — `RelayClient`/`RelaySource` take optional `host_header` and `sni_hostname`; given them they dial the validated IP while `Host` and TLS SNI keep working. `request_id` and `peer_id` are percent-encoded.
**`backend/skill_api.py`** — browse/fetch call `_pinned_relay()`; `_pinned_get` shares the helper.

**What was actually wrong:** validation *did* run. It threw away the resolved IP and handed the hostname to httpx, which re-resolved it — the DNS-rebinding window that `_pinned_get` exists to close on the sibling path. The July verdict ("validated upstream") was true and insufficient.

## 2. A read-and-execute chain — worse than anything the scanner labelled

**`backend/main.py`** — `_bb_resolve_gate_path` + `_bb_run_gate`.

CodeQL called it `py/path-injection`. It was: `GATE: <path>` in an ordinary chat message on `/ws/chat` (which needs only *a* session, not the owner's) → the named file **read and executed as a Python subprocess** → stdout/stderr returned. It composed with `api_save_to_downloads`, whose filename scrub permits `.py`. Any profile → code execution as the backend → every other profile's keys. Around per-user encryption, not through it.

Now a **name, not a path**: no separators, no `..`, not absolute, `test_*.py` only, contained to the backend directory and confirmed with `_within`. Customs now inspects the gate test too — it only ever saw the candidate. **The feature is unchanged**; naming a project test file still works.

## 3. The namespace rule is enforced instead of asserted

**`backend/ns_guard.py`** (new) — `NS_RE`, `InvalidNamespace`, `safe_ns()`, `is_valid()`.
**`backend/sage_engine.py`** — `user_data_dir` enforces it before building a path.
**`backend/profile_keys.py`** — `_user_dir` validates first; its unvalidated `except` fallback is gone.
**`backend/main.py`** — `_safe_ns` now just translates to HTTP 400.

`user_data_dir` used to carry a *comment* saying callers had applied `_safe_ns`. It had already drifted twice: `profile_keys._user_dir` bypassed every caller whenever importing `sage_engine` raised, and only one of five downloads routes applied the guard — while the delete route ended in `path.unlink()`.

**`NS_RE` now uses `\A...\Z`, not `^...$`.** Python's `$` also matches before a trailing newline, so the old pattern **accepted `"alice\n"`** — a different directory from `"alice"`. That had been in the guard since it was written. The new test caught it on its first run.

## 4. Exception text leaving the process

**`backend/mcp_handlers.py`** — `call_tool` returned a **full traceback** (absolute source paths, line numbers, frame context) to a token-authenticated MCP caller. Now logged with a ref; the caller keeps the tool name and exception type.
**`backend/main.py`** — burn's per-file reports emitted `path: raw OSError`; now basename + exception type, full detail to the log. `/api/hardware` keeps its probe text **for the owner** and scrubs it for everyone else.
**`backend/build_integrity.py`** — type + correlation ref instead of the raw message.

Expression-engine errors (`ZeroDivisionError`, `ParseError`) are deliberately unchanged: they describe the user's own input, not the machine.

## 5. Export containment

**`backend/data_export.py`** — `_files_under` walked with `is_file()`, which **follows symlinks**. A link inside a profile directory would have been followed and packed into that profile's export. Symlinks are skipped, a symlinked root exports nothing, survivors are confirmed to resolve inside the profile.

## 6. Frontend

**`frontend/js/chat.js`** — both `appendImageResult` call sites now allowlist the mimetype. One did; the other interpolated it raw and leaned on the scheme check as its only backstop.

## 7. Repo hygiene

`frontend/SAFE/` — **31 tracked files**, a stale 2026-07-09 frontend snapshot with four `index.html.bak-*`, loaded by nothing — removed from tracking (two alerts went with it). `build.log` (which leaked a staging path) and `.oracle_pids.json` untracked; `.gitignore` updated so they cannot return. `chat_memory.json` was tracked but **2 bytes / empty** — no data was ever published.

## 8. The tree moved to a stable path

`VeridianAI_v2.15/` → **`app/`**, 464 files, all staged as renames so history follows.

CodeQL keys alerts on **file path**. The version-named folder meant every bump made every file new and dropped every dismissal. `#128`/`#129` — yesterday's Criticals — are byte-for-byte `#5`/`#6` from v2.12. Seven of the 37 were re-raises of work already done.

Done at zero open on purpose: the re-raise became a correctness check. **27 came back, 27 matched a prior verdict, 0 genuinely new.**

## 9. De-versioning, and developer-specific paths

`electron/package.json` is the one place carrying a version. Everything else derives or omits it.

The sweep found more than comments — five CRAIID files carried absolute paths to one machine's drive, and in **`audit_archives_personal_v2.py`** they were **live argparse defaults** for `--plm` and `--output`, not fallbacks. On anyone else's machine that script read and wrote paths that do not exist.

Corrected: `craiid/audit_archives_{deep,lite,personal_v2}.py`, `craiid/craiid_compression_{core_v3,validation_v4}.py`, `context_fatigue_detector.py` (its `--help` advertised a default it did not use), `coordinator_signal.py`, `ipc_monitor.py`, `migrate_v216_timestamps.py`, `ble_sniff_test.py`, `mcp_server.py`, `rotate_api_key.py`, `electron/package.json`, `start.bat`.

## New tests

| File | Assertions | Covers |
|---|---|---|
| `test_ns_guard.py` | 11 | traversal, absolute/device paths, NUL, the newline bypass, the removed fallback failing closed, and a property test that no namespace `users._ns_for` can mint violates the rule |
| `test_build_gate_containment.py` | 40 | the feature still resolves a real test file; absolute paths, traversal, non-`test_*` names all refused |
| `test_stack_trace_containment.py` | 27 | all four surfaces; functional coverage of the hardware scrubber |
| `test_export_symlink_containment.py` | 8 | links to an outside file, an outside directory, a broken link, a symlinked root |

Also **corrected**: `test_skill_api.test_browse_bad_peer_graceful` had been failing since the July hardening — it still asserted the old graceful contract for a loopback URL. And four test files printed non-ASCII with no stdout guard; two were dying with `UnicodeEncodeError` on a cp1252 console **before reporting a single assertion**, so they read as failing when nothing was wrong.

---

# Part 2 — TO-DO

## 🔴 1. The version is wrong — decide it and run the bump

**`electron/package.json` says `"version": "2.14.2"` in all three trees.** The folders say v2.15, commit `57c3377` says "Minor version bum from 2.14.2 to v2.15", and everything written today says v2.15.

That file **is** the version and becomes the MSIX package version. `_bump_version.py` *does* target it — the tool is correct, it simply was not run for this bump.

Nothing was changed here because the number is yours to choose (semver, and Store package versions must increase and have their own format rules).

```
py _bump_version.py            # from the app folder
```

Left alone for the same reason: the Store tree's `electron/package.json`, also at 2.14.2.

## 🟠 2. Rebuild — the published binaries predate everything above

`VeridianAI.exe` and `resources/app.asar` in the repo are from before today. **Only `app.asar` runs** — the portable-asar trap. Rebuild both trees and re-verify the payload before shipping.

## 🟠 3. Decide what happens to your personal running copy

`E:\VeridianAI_v2.14.3` is a whole version behind and has **none** of today's fixes — including the gate chain and the namespace guard. It was invaluable today as an unmodified control for "was this test already red?", so there is a real argument for keeping one behind deliberately. Your call; just make it deliberately.

## 🟡 4. Live checks I could not run

Everything below passes its tests; none has been exercised against a running app.

- **Build Battle** with a real `GATE: test_something.py` line — confirm a legitimate gate still resolves and runs.
- **`/api/hardware`** — confirm the panel renders for a **non-owner** profile (it should show the report with probe errors replaced) and full text for the owner.
- **An MCP tool error** — confirm the caller gets `[tool 'x' raised] TypeError (ref abc12345)` and the log holds the traceback.
- **A burn with a locked file** — confirm the report reads `chat.dat: PermissionError` and is still useful.
- **An export on a real profile** — confirm content is unchanged (the symlink change should be invisible).
- **`GATE:` with an absolute path** — confirm it is refused rather than silently doing nothing surprising.

## 🟡 5. Your call, no action needed

- **`GH001: Large files detected`** on every push — the bundled `.dll`/`.exe` payloads. Git LFS is the answer if it ever becomes a problem.
- **Oracle→Veridian naming** still in CRAIID prose (`craiid_compression_validation_v4.py` and others). Deliberately not touched — a blind rename sweep has eaten things in this project before.
- **Comments recording past removals** (`"v2.12.2: was hardcoded to E:\OracleAI_v2.3"`) keep their old paths. That is history and the path is the evidence; say the word if you would rather they go.
- **Two tests still cannot report on a cp1252 console** without the new guard — if you add test files that print symbols, copy the four-line `reconfigure(errors="replace")` block.

---

# Part 3 — For the next CodeQL round

**Get the alerts** (`gh` is installed and authed; `repo` scope is enough — the repo is public, `security_events` is not needed):

```
gh api "/repos/OmniFoxX/VeridianAI/code-scanning/alerts?state=open&per_page=100"
```

**If a batch of dismissals gets dropped**, `STAGING/tools_codeql/recarry_dismissals.py` matches each open alert to the prior one it *is* — same rule, same file, same line — and re-applies the original comment verbatim. **Always read the dry run first**; its "genuinely new" list is the whole point.

```
python recarry_dismissals.py            # dry run
python recarry_dismissals.py --apply
```

**Two traps that cost time today:** `dismissed_comment` caps at **280 characters** (HTTP 422 above it), and in PowerShell `[ordered]@{}` indexes by **position** for an integer key, so `$d[147]` silently returns `$null`.

**The rule that found the real bugs:** *"validated upstream" is not a dismissal on its own.* Ask whether the validation's **result** was used, or only its verdict. A check whose output is discarded before the call leaves a window after it. That single question found the relay pin, the namespace fallback, and the downloads split.

**And: never report a CodeQL fix as done on reasoning alone.** The re-scan disagreed with me twice in one day — once because I sanitised the wrong site (`handle_jsonrpc` instead of `call_tool`, which is the "match on location, not pattern" mistake, made while writing a warning about that mistake), and once because fixing one alert produced two. Both were caught only by pushing and looking.
