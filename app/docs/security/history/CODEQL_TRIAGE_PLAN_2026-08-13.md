# CodeQL v2.15 -- recon findings and execution plan

37 open alerts on push #65 (`57c3377`). Every flagged line has been read, along
with the guard each one depends on. Nothing has been dismissed yet.

**The alert line numbers map exactly onto the STAGING tree.** All 187 backend
files were hashed against the repo: the only differences are the five files
changed by today's SSRF fix. The round-2 complaint about repo line numbers not
lining up does not apply this round.

---

## Headline: the FP rate is high, but it is not 35/37

Roughly **9 are clean false positives** with a nameable guard, **13 need a
judgement call** I can only make by tracing one more caller, **2 are already
fixed** (the SSRF pair), and **~13 point at one real structural weakness** --
described immediately below.

There is also a set of **repo-hygiene findings that are not CodeQL alerts at
all**, one of which deletes two alerts by itself.

---

## The structural finding: a security invariant that is a comment

21 of the 37 are `py/path-injection`, and about half of those trace back to one
place.

`sage_engine.user_data_dir(ns)` builds `DATA_DIR / "users" / str(ns)` and
carries this:

```python
# SECURITY: callers pass `ns` only after _safe_ns() has constrained it to
# _NS_RE (^[A-Za-z0-9_-]{1,64}$); it cannot contain a slash, backslash, dot or
# colon, so no '../' or absolute-path traversal is possible ...
```

The comment is accurate. It is also the *entire* enforcement. `_safe_ns` lives
in `main.py:2573` and is applied per-route; `user_data_dir` validates nothing.

That is the same shape as this morning's relay SSRF -- a real check, one layer
away from the thing that depends on it -- and it has already drifted:

**1. `profile_keys._user_dir()` has an unsanitised fallback.**

```python
try:
    import sage_engine
    d = sage_engine.user_data_dir(ns)
    return Path(d) if d else None
except Exception:
    base = _data_dir()
    return (base / "users" / str(ns)) if base else None   # <-- no guard at all
```

Any failure of that import or call -- circular import at startup, a raised
exception inside `user_data_dir` -- and `ns` goes straight into a path with no
validation. Every `keywrap.py` alert flows through here. This is precisely the
case the earlier triage note warned about: *"keywrap was built as pure key
operations that trust the caller; the guard lives one layer up"* is a defensible
design **and** the argument that would let a real hole through, because it is
the argument required to dismiss six alerts at once.

**2. The downloads routes are inconsistent with each other.**

```python
main.py:4801  _ddir = _downloads_dir_for_ns(_safe_ns(_session_ns(request)))   # WRITE  -- guarded
main.py:4758  path  = _downloads_dir_for_ns(_session_ns(request)) / name      # READ   -- not guarded
main.py:4773  path  = _downloads_dir_for_ns(_session_ns(request)) / name      # DELETE -- not guarded
```

The write route validates the namespace; the read and delete routes do not. The
delete route ends in `path.unlink()`. `_session_ns` is server-derived, so this is
probably not exploitable today -- but "probably not exploitable because of what
a different function currently returns" is exactly the reasoning that failed on
the relay client.

**Proposed fix (one change, ~11 alerts):** enforce `_NS_RE` inside
`user_data_dir` itself -- or in a small `ns_guard` leaf module both it and
`main.py` import -- so the invariant is enforced where it is relied upon rather
than asserted in a comment. Delete the unsanitised fallback in `_user_dir`;
failing closed is correct for a key path. Add `_safe_ns` to the two downloads
routes for consistency. Same move as `net_guard` this morning: one
implementation, at the point of use.

---

## Group-by-group

### A. `py/path-injection` x21

| Alerts | Location | Read |
|---|---|---|
| #131-136 | `keywrap.py` 201,203,207x2,209,219 | **Needs the fix above.** Pure path-in module; taint is `ns` via `profile_keys`. |
| #137 | `profile_keys.py:161` | **Needs the fix above.** `p.parent.mkdir()`, same chain. |
| #130 | `atrest.py:315` | **Trace callers.** Generic `read_file_auto(path)`; verdict depends on who calls it. |
| #140,141,142 | `main.py` 4759, 4774, 4775 | **Add `_safe_ns`.** Read/delete routes; basename-only is applied, ns is not guarded. |
| #143,144 | `main.py` 4807, 4808 | **FP.** `_safe_ns` + `_within` + explicit `.`/`..` rejection, all on the lines above. |
| #138 | `main.py:2589` | **FP.** This is `_within()` itself -- the containment guard. It only tests a path, never opens one. Same trap as round 2: flagging the guard. |
| #139 | `main.py:3232` | **Verify.** `t` is checked against the profile list ~6 lines earlier, which is an allowlist. Confirm the check is authoritative, then dismiss. |
| #145,146 | `main.py` 6073, 6249 | **Trace.** `_bb_resolve_gate_path` opens a gate-test file; need to establish where `gate_test` originates. If model- or request-supplied, real. |
| #147-150 | `skill_store.py` 157x2, 164, 167 | **FP.** `_safe_hid` (line 91) rejects `/ \ .. NUL`, len>160, `.`/`..`, and is applied inside `_body_path`/`_env_path` with `ValueError` handled. This is the July fix working; CodeQL flags the *use* because a sanitised value still builds a path. |

### B. `py/stack-trace-exposure` x7 -- #153-159, all `main.py`

1361, 2040, 2852, 3913, 3920, 4226 return dicts that *may* carry exception text
from upstream -- the accepted-low-risk group. **2728 is different:** it appends
`str(e)` to an `errors` list and returns it to the client. `_safe_detail()`
already exists in this file for exactly that. Plan: route 2728 (and 3913/3920
if `handle_jsonrpc` puts raw exception text in its error responses) through
`_safe_detail`, dismiss the rest.

### C. `js/client-side-unvalidated-url-redirection` x4

- #126, #127 -- `frontend/js/chat.js` 769/784: `img.src = imgUrl` and
  `save.href = imgUrl`. July notes say the image URL is scheme-guarded.
  **Verify the guard's output is what reaches `.src`** -- that is the exact
  question the relay SSRF failed.
- #124, #125 -- `frontend/SAFE/js/chat.js` 707/721. **This file does not exist
  in any working tree.** See hygiene item 1: deleting it removes both alerts.

### D. `py/full-ssrf` x2 -- #128, #129 -- **FIXED**, pending sync and push.

### E. `py/clear-text-logging-sensitive-data` x1 -- #152

`test_export_containment.py:51` is a `print()` inside the test harness's `ok()`
helper. GitHub tags it `Test`. Confirm what reaches `detail` on the traced flow,
then dismiss.

### F. `py/bind-socket-all-network-interfaces` x1 -- #151

`argonet_lan.py:177` is the **Windows multicast fallback**: INADDR_ANY is
required for multicast reception there, and the comment says unicast is filtered
by `_is_lan_source()`. Confirm that filter actually runs on received packets,
then dismiss. Documented and intentional.

### G. `js/shell-command-injection-from-environment` x1 -- #123

`electron/main.js:604`. July dismissed **:318** on array-arg spawn +
`existsSync`. This is a different line. Read it on its own terms -- matching on
pattern instead of location is the documented round-2 mistake.

---

## Repo hygiene -- not CodeQL alerts, found while checking line alignment

The published tree carries 66 files that exist in no working tree.

1. **`frontend/SAFE/` -- 31 tracked files.** A complete stale snapshot of the
   frontend from 2026-07-09, including four `index.html.bak-*` files. Not in
   STAGING or the Store tree; nothing loads it. **Deleting it removes alerts
   #124 and #125 outright.** Highest-value single action in this document.
2. **`build.log`** -- tracked, 16 KB, contains
   `E:\MentiSphereSoftwareStaging\STAGING\WinStoreApp\VeridianAI_v2.14.1\build_manifest.json`.
   A staging path on a public repo. Untrack.
3. **`chat_memory.json` and `.oracle_pids.json`** -- tracked runtime artifacts.
   **Both are 2 bytes / empty -- no data was ever published.** Untrack anyway.
4. **`dist/builder-debug.yml`, `dist/builder-effective-config.yaml`** --
   `dist/` IS in `.gitignore`, but these were committed before that rule.
   `.gitignore` does not untrack what is already tracked. Untrack.
5. **`oracleai_config.json`** -- REMOVED (commit `d648536`). It hardcoded the
   developer's home directory and an OracleAI-era layout in three keys, which
   breaks both the no-user-specific-hardcoding rule and the single-source-of-
   truth rule. Note that removal does not erase it from history; the values are
   a local path and a username, not credentials.
6. **`VeridianAI.exe` and `resources/app.asar`** -- tracked, and both DIFFER
   from STAGING. `*.exe` is in `.gitignore` but these predate it. A published
   `app.asar` that disagrees with the tree is the portable-asar trap wearing a
   different hat.
7. `docs/README/` nests its own `.gitattributes`, `.gitignore` and
   `Attorney_Note.md` -- duplicates of the repo-root files.
8. `config.json` is published and differs from STAGING. Checked every
   credential-shaped key: all empty. Worth a decision on whether a runtime
   config should ship at all.

---

## Proposed order

**Phase 0 -- hygiene (fast, no code risk).** Untrack items 2-4 and 6, delete
`frontend/SAFE/`, commit the `oracleai_config.json` deletion. Removes 2 alerts
and stops publishing build artifacts.

**Phase 1 -- the ns guard (~11 alerts).** Enforce `_NS_RE` at the point of use,
drop the unsanitised fallback, add `_safe_ns` to the two downloads routes. One
structural change, tests around it.

**Phase 2 -- verify and dismiss (~9 alerts).** `skill_store` x4, `main.py` 2589,
4807, 4808, `argonet_lan`, `test_export_containment`. Each dismissal names the
guard and the line it sits on -- if that sentence cannot be written, it is not a
false positive yet.

**Phase 3 -- read and decide (~13 alerts).** `atrest.py:315` callers,
`main.py` 3232/6073/6249, `electron/main.js:604`, `chat.js` 769/784, and the
seven stack-trace returns.

**Phase 4 -- sync and confirm.** STAGING -> Store tree -> the G: clone, `cmp`
each, one commit, one push, then confirm which alerts actually closed.

## One workflow note

`gh` can dismiss alerts directly:

```
gh api -X PATCH /repos/OmniFoxX/VeridianAI/code-scanning/alerts/<n> \
  -f state=dismissed -f dismissed_reason="false positive" \
  -f dismissed_comment="<the guard, and the line it is on>"
```

Same result as ticking the box in the UI, but the reason lands **next to the
alert** as well as in this folder -- so the next person to look at alert #147
sees "guarded by `_safe_hid`, skill_store.py:91" without having to find this
document. That is a strict improvement on the current convention, not a
replacement for it.

## Unresolved: the inline-suppression canary

`skill_api` is absent from all 37, and it carried the July
`# codeql[py/full-ssrf]` marker. **This does not prove inline suppression
works** -- that code was also genuinely hardened with `_pinned_get` in the same
round, so either could explain the absence, and the API cannot distinguish
"dismissed", "fixed" and "suppressed" after the fact. A local CodeQL run would
settle it in one pass. Until then, assume UI/API dismissal is the only reliable
route.

# This document and all documentation has been generated by AI and Human edited.

- A Human (Todd [That's Me, the Human]) Architect/Director/Editor led AI coding
  team of multiple current leading online frontier models, and many local models
  using VeridianAI's multi-model slots with Toga (very large local model library).