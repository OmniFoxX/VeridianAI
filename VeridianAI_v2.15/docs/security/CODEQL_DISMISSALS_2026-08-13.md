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
