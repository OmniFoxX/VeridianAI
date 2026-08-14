# CodeQL dismissals -- 2026-08-13

Eight alerts verified as false positives. Each entry names **the guard, the file
and the line it sits on**, and states how the guarded value reaches the flagged
sink. If that sentence cannot be written, the alert is not dismissed.

Two alerts that looked like dismissal candidates were **fixed instead** -- see
the bottom of this file.

Dismissal is via the API so the reason lands next to the alert as well as here:

```
gh api -X PATCH /repos/OmniFoxX/VeridianAI/code-scanning/alerts/<n> \
  -f state=dismissed -f dismissed_reason="false positive" \
  -f dismissed_comment="<text below>"
```

Comments name FUNCTIONS as well as line numbers, because the v2.15 changes shift
line numbers in `main.py` and a reason that only cites a line stops being
checkable the moment anything above it moves.

---

## #147, #148, #149, #150 -- `py/path-injection`, `skill_store.py` 157 x2, 164, 167

**Guard:** `SkillStore._safe_hid`, `skill_store.py:91`.

It rejects `/`, `\`, `..`, NUL, `.`, `..` and anything over 160 characters,
raising `ValueError`. It is applied INSIDE `_body_path` (line 100) and
`_env_path` (line 103) -- the only two functions that build a path from a
content id -- so there is no route to the filesystem that skips it. Both
callers catch the `ValueError` and return None.

Why CodeQL still flags it: sanitising a value and then building a path from it
leaves the path tainted in the dataflow model. That is the documented round-2
lesson, not a new finding.

> Guarded by `SkillStore._safe_hid` (skill_store.py:91), which rejects `/ \ .. NUL`
> and len>160 with ValueError. Applied inside `_body_path` (:100) and `_env_path`
> (:103), the only path-constructing functions here, so no sink is reachable
> without it; both callers catch the ValueError. The id is a content hash, never
> a path. CodeQL flags the USE because a sanitised value still builds a path.

## #138 -- `py/path-injection`, `main.py:2589`

**This alert is on the containment guard itself.**

`_within(child, parent)` resolves both paths and returns a boolean. It never
opens, reads, writes or deletes anything -- it is the primitive the burn,
settings, upload, image-write and account-wipe paths call BEFORE touching the
filesystem. Flagging it is the same pattern as round 2, where making a guard
explicit added an alert rather than removing one.

> `_within()` is the containment CHECK, not a filesystem operation. It calls
> `Path(...).resolve()` on both arguments and returns a bool; nothing is opened,
> written or removed. It is the guard that the burn / settings / upload /
> image-write / account-wipe paths call before acting. Dismissing the guard does
> not weaken the sinks it protects -- those are separately guarded.

## #143, #144 -- `py/path-injection`, `main.py` 4807, 4808

**Function:** `api_save_to_downloads` (`POST /api/downloads/save`).
**Guards, all above the flagged lines:** `_safe_ns` on the namespace,
`re.sub(r'[^\w\-.]', '_', filename)` on the name, an explicit rejection of
`""`, `"."` and `".."`, and `_within(path, _ddir)` on the assembled path.

The `.` character is deliberately permitted (filenames have extensions), which
is exactly why the explicit `..` rejection and the `_within` check are both
there. This is the route that was already correct.

> Guarded on the four lines directly above: `_safe_ns` on the namespace,
> `re.sub(r'[^\w\-.]','_')` on the filename, an explicit reject of ""/"."/"..",
> and `_within(path, _ddir)` confirming the assembled path resolves inside the
> caller's own downloads directory before the write. Function:
> `api_save_to_downloads`.

## #151 -- `py/bind-socket-all-network-interfaces`, `argonet_lan.py:177`

**Guard:** `ArgoNetLAN._is_lan_source`, `argonet_lan.py:196`, **called at line
314** in the receive loop: `if not self._is_lan_source(addr[0]):` -- before any
parsing of the datagram.

The flagged bind is the **Windows-only fallback**. The preferred path (line 171)
binds the multicast GROUP address, where unicast to the port is kernel-refused.
Windows returns `WSAEADDRNOTAVAIL` for a group bind and requires `INADDR_ANY`
to receive multicast at all, so the fallback is not a choice. Datagrams from
any non-private source are dropped on arrival.

> Windows-only fallback: a group-address bind (:171) is preferred and used on
> Linux/macOS, where unicast to the port is kernel-refused. Windows cannot bind a
> group address (WSAEADDRNOTAVAIL) and needs INADDR_ANY for multicast reception.
> The exposure is closed in software by `_is_lan_source` (:196), called at :314
> in the receive loop, which drops any datagram whose source is not RFC1918,
> link-local or loopback BEFORE parsing.

---

## Fixed rather than dismissed

**#152 -- `py/clear-text-logging-sensitive-data`, `test_export_containment.py:51`.**
The flagged value was a loop variable named `secret` holding canary markers --
`b"OWNER CHAIN ENTRY"`, `b"private procedure"`, `b"OWNER LAB RESULT"` -- planted
in the owner's data so the test can assert they are ABSENT from another
profile's export. Nothing sensitive was ever logged. But CodeQL was pointing at
a genuinely misleading name, so the variable is now `marker`. The alert goes
with the rename, and the next reader is not left deciding whether a file called
`test_export_containment` prints secrets.

**#128, #129 -- `py/full-ssrf`, `relay_client.py` 22 and 32.** Fixed this
morning; see `CODEQL_TRIAGE_2026-08-13.md`.

---

# Round 2 -- after the `ed2aeb4` re-scan

The push closed the two Critical SSRFs, the build-battle read-and-execute chain,
the two `frontend/SAFE/` alerts, `#152`, `#153` and `#156`. **37 -> 22.** These
19 are the remainder that are genuinely guarded but that CodeQL cannot see
through. Same rule as round 1: the guard and its line are named, or it is not
dismissed.

## `#131`-`#136` -- `keywrap.py` 201, 203, 207 x2, 209, 219

**Guard:** `ns_guard.safe_ns`, enforced in `sage_engine.user_data_dir` and in
`profile_keys._user_dir`.

The alerts did not close, and that is expected: `safe_ns` matches a regex and
returns the ORIGINAL string, which CodeQL does not model as a barrier. But the
reasoning is materially stronger than it would have been yesterday. Before
v2.15 the rule lived in a comment claiming callers had applied `main.py`'s
`_safe_ns`, and `profile_keys._user_dir` had an `except` branch that bypassed
every caller. Now the rule is enforced at the point the path is built, that
fallback is gone, and `test_ns_guard` has a regression test proving it fails
closed.

`\A[A-Za-z0-9_-]{1,64}\Z` -- and note `\A...\Z`, not `^...$`: Python's `$` also
matches before a trailing newline, so the original pattern accepted `"alice\n"`,
a different directory from `"alice"`.

## `#137` -- `profile_keys.py:176`

Same guard, and this is the function that was actually broken. It validates
first now, so both the sage_engine branch and the fallback build from a checked
value.

## `#130` -- `atrest.py:315`

`read_file_auto`'s path is server-built at every one of its three callers: the
downloads route (basename + `_safe_ns`), a walk over export roots derived from
an enforced namespace, and a module constant. No caller passes a client-supplied
path.

**Hardening note, not this alert:** `data_export._files_under` walks with
`rglob` + `is_file()`, and `is_file()` FOLLOWS symlinks. A symlink planted
inside a profile directory would be followed into whatever it points at and
packed into that profile's export. Planting one needs OS access rather than the
app, so it is not reachable through this flow -- but it is the same shape as the
export-containment leak already fixed once.

## `#139` -- `main.py:3283`

Three layers above the line: `_safe_ns` on the target, `_is_owner` (only the
owner may import into another profile), and membership of `users.list_users()`,
which is an allowlist.

## `#140`, `#141`, `#142` -- the downloads read and delete routes

`_safe_ns` on the namespace and `Path(filename).name` for the file. As of v2.15
all five downloads routes apply the namespace guard; before it, only the save
route did, and the delete route ended in `path.unlink()`.

## `#154`, `#159` -- stack-trace false positives

`#154`: the ComfyUI status dict carries no exception text; the config read above
it swallows failures with `except Exception: pass` and no returned field derives
from an exception. `#159`: `sage_engine.process_upload` returns a fixed decoder
message on failure, not exception text.

## `#160`, `#161` -- the two that replaced `#153`

Worth recording plainly: **fixing `#153` produced two alerts where there was
one**, because one return statement became two. This is the round-2 pattern
where making a guard explicit adds alerts rather than removing them.

`#160` is the OWNER branch of `/api/hardware`, which deliberately keeps the full
probe text -- that text is what identified the missing MSVC runtime and the
Vulkan questions, so it is scoped to the owner rather than deleted. `#161` is
`_scrub_probe_errors` itself: the line CodeQL flags is the sanitiser, which
replaces every `error` string with a fixed placeholder, recursing through dicts
and lists.

## `#126`, `#127` -- `frontend/js/chat.js` 769, 784

**Guard:** the scheme allowlist at `chat.js:757`, which rejects anything that is
not `data:image/`, `blob:`, `http(s):` or same-origin.

These were re-verified rather than re-dismissed on July's reasoning, because
July's note ("CodeQL can't see the regex barrier") is the same shape of argument
that failed on the relay client. The test applied: **the value that was checked
is the value that is used.** `imgUrl` is tested at 757 and reaches `img.src` and
`save.href` unchanged, with no re-derivation in between. That is what the relay
path failed and this one passes.

Checking it did surface a real asymmetry, fixed rather than dismissed: one of
the two `appendImageResult` call sites allowlisted `result.mimetype` and the
other interpolated it raw, relying on the scheme check as its only backstop.
Both now apply the same rule.

## `#123` -- `electron/main.js:604`

`spawn()` with an ARRAY of arguments and **no `shell: true`**, so nothing parses
a command line. `_spawnCmd` is a joined path (Store) or the literal `'cmd.exe'`,
never env-derived. The only variable element of argv is re-checked against
`VALID_MODES` at the spawn, failing closed to `'vulkan'`. The tainted value
CodeQL tracks, `_dataDir` from `VERIDIAN_DATA_DIR`, reaches `env` only -- never
argv.

Note this alert was previously dismissed as `#90` at the `VeridianAI_v2.12`
path. It re-raised because the folder was renamed, not because anything changed.

## Left OPEN deliberately

`#155`, `#157`, `#158` are not dismissed. Fixes are staged and should close them
on the next scan:

- `#155` -- burn's three per-file appends emitted `f"{path}: {exception}"`. Now
  `_burn_err`: basename plus exception TYPE to the caller (a burn report still
  has to say what survived), full path and message to the server log.
- `#157`/`#158` -- the first pass sanitised `handle_jsonrpc`'s own `except`,
  which was **not where the text came from**. `call_tool` returned a full
  traceback -- absolute source paths, line numbers, frame context -- to a
  token-authenticated MCP caller. Now logged with a ref; the caller keeps the
  tool name and exception type.
