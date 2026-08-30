# CodeQL dismissals — 2026-08-30 (IDE + Display panel round)

Seven alerts on `c254b76`. **One was a true positive and was fixed in code**;
six are false positives and are dismissed here. Each entry names **the guard,
the file and the line it sits on**, and states how the guarded value reaches
the flagged sink. If that sentence cannot be written, the alert is not
dismissed.

Both causes trace to this project's own changes, so both are owned here rather
than described as noise.

Dismissal is via the API so the reason lands next to the alert as well as here:

```
gh api -X PATCH /repos/OmniFoxX/VeridianAI/code-scanning/alerts/<n> \
  -f state=dismissed -f dismissed_reason="false positive" \
  -f dismissed_comment="<text below>"
```

Comments name FUNCTIONS as well as line numbers, because IDE work shifted line
numbers in both files and a reason that only cites a line stops being checkable
the moment anything above it moves.

---

## #194–#199 — `py/path-injection`, `sage_engine.py` (six sinks in one function)

Flagged lines at `c254b76`, all inside `save_to_downloads`:

| alert | line | sink |
| ----- | ---- | ---- |
| #194 | 2505 | `path.resolve().relative_to(...)` |
| #195 | 2513 | `path.exists()` |
| #196, #197 | 2518 | `_sh.copy2(path, ...)` |
| #198 | 2529 | `path.write_text(...)` |
| #199 | 2534 | `path.stat().st_size` |

**Why they appeared now.** They are not new code. Phase 2a pointed
`POST /api/downloads/save` at this executor — deliberately, to end a situation
where three writers had three different answers to "what may be saved". That
gave the analysis a clean data path from an HTTP request body to a file write,
and the six sinks lit up. **The data flow is real.** The question is only
whether the guards hold, and "I read it and it looked fine" is not an answer to
six HIGH path-injection alerts.

**Guard 1 — the sanitiser.** `sage_engine.py`, first line of
`save_to_downloads`:

```python
safe_name = re.sub(r'[^\w\-.]', '_', filename.strip())
```

An allowlist, not a denylist: word characters, hyphen and dot survive;
everything else becomes `_`. No `/`, no `\`, no `:`, no NUL, no wildcard can
reach the join. The only traversal-capable string that survives is `..`,
because dot is allowed — which is guard 2's job.

**Guard 2 — `check_save_filename`,** applied to the SANITISED name, not the
requested one (using a validation's verdict but not its result is the classic
version of this mistake). It refuses `.`, `..`, the empty string, reserved
Windows device names (`con`, `prn`, `aux`, `nul`, `com1`–`com9`, `lpt1`–`lpt9`)
including as a stem, and enforces `ALLOWED_SAVE_EXT`. It judges the name after
`rstrip(". ")` because Windows discards trailing dots and spaces when opening a
file, so `pwn.bat.` on disk opens as `pwn.bat`.

**Guard 3 — containment, and it runs BEFORE any use of the path:**

```python
path = _dl / safe_name
try:
    path.resolve().relative_to(_dl.resolve())
except ValueError:
    return {"success": False, "error": "Path escapes downloads directory"}
```

`_dl` comes from `downloads_dir_for(ns)`, and `ns` is namespace-validated by
`ns_guard.safe_ns` at the point the path is built (`user_data_dir`), so the
BASE is not attacker-influenced either.

**Evidence, not assertion.** `backend/test_downloads_traversal.py` attacks the
function with **42 payloads**: `../`, `..\`, `....//`, URL-encoded and
double-URL-encoded, `..;/`, absolute POSIX and Windows paths, UNC
(`\\server\share`), extended-length (`\\?\C:\`), embedded NUL, RTL override,
zero-width space, `~/` and `%APPDATA%` expansion attempts, every reserved device
name, trailing dot and space, double extensions, and a 300-character name.
Nothing escapes; a canary written outside the folder is untouched.

It was **controlled**, which is what makes it worth anything:

- with guard 1 disabled, guard 3 alone still contains every payload;
- with guard 3 disabled, guard 1 alone still contains every payload;
- with **both** disabled the test goes red and names the escapes.

So the layering is genuine defence in depth *and* the test can actually detect
a failure. The test runs inside a throwaway namespace (`ztraversalprobe`) and
removes it afterwards, so neither it nor a future control run can write into a
real downloads folder.

**Dismissal comment (all six):**

> False positive. `save_to_downloads` (sage_engine.py) applies three guards
> before any filesystem use: (1) `re.sub(r'[^\w\-.]', '_', ...)` — an allowlist
> that removes every path separator, drive colon, NUL and wildcard; (2)
> `check_save_filename`, applied to the SANITISED name, refusing `.`/`..`,
> reserved Windows device names and non-allowlisted extensions, judged after
> `rstrip(". ")`; (3) `path.resolve().relative_to(_dl.resolve())`, an explicit
> containment check that runs BEFORE exists/copy2/write_text/stat. The base dir
> is namespace-validated by `ns_guard.safe_ns`. Verified by
> `backend/test_downloads_traversal.py` — 42 traversal payloads, none escape,
> controlled by disabling each guard in turn (either alone still contains; only
> both disabled goes red). Full reasoning:
> docs/security/history/CODEQL_DISMISSALS_2026-08-30.md

**What would reopen this.** Any of the three guards being removed or reordered
after the join, or `_dl / filename` replacing `_dl / safe_name`. The data flow
does not go away, so the alerts will return if the guards do not hold —
which is the correct behaviour and the reason for dismissing rather than
suppressing inline.

---

## Fixed instead of dismissed

## #200 — `py/stack-trace-exposure`, `main.py` (`api_ide_run`)

**True positive.** Two different tracebacks can reach that endpoint's response
and they are not the same kind of thing:

- **the child's stderr** — a traceback of the code the *person wrote and pressed
  Run on*. Showing it is the entire point of an IDE. It arrives as ordinary
  output and was never the problem.
- **VeridianAI's own executor failing** — an internal detail. The `except`
  block interpolated `f"{type(e).__name__}: {e}"` straight into the response.

Fixed by routing the second through `_safe_detail` (`main.py:3050`), the helper
this file already uses for every other internal failure: the real exception is
logged server-side under a short correlation ref, and the client gets
`internal error (ref xxxxxxxx)`.

The tempting "fix" — suppressing tracebacks from that endpoint generally —
would have closed the alert and broken the feature. So
`backend/test_ide_run.py` section 7b asserts **both** directions: a
`ValueError` raised by the user's own code still reaches them with its
traceback body, and an internal `RuntimeError` carrying a fake secret path
leaks neither its message, nor the path, nor even its exception type, while
still telling the person something went wrong and giving them a ref to quote.

---

## Not an alert, found while answering "do the `.bak` files ship?"

`build_integrity.py` hashes by extension **allowlist** (`.py .js .html .css
.bat .ps1 .webmanifest .vbs`), so a file named `x.py.pre_fix.bak` is not in the
signed manifest. But `electron/package.json`'s `extraFiles` filter for
`../backend` excluded only `__pycache__`, `.pyc` and `.pyo`, and
`make_release.ps1` excluded `*.bak_*` — which matches `file.bak_20260101` and
**not** `file.bak`.

Net effect: every plain `.bak` safety copy left in a tree **shipped in both the
MSIX and the portable zip, covered by no integrity statement at all.** That is
the rule in `build_integrity.py`'s own `EXCLUDE_FILES` note — *"excluding from
extraFiles and excluding from the manifest are two halves of one decision"* —
running in the other direction.

Fixed: `!**/*.bak` added to all nine `extraFiles` filters, and `'*.bak'` added
to `make_release.ps1`'s `$excludeFiles` beside the `'*.bak_*'` that did not
cover it.
