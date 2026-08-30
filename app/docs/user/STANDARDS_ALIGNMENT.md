# Standards alignment — HIPAA and WCAG 2.2 AA

**Status: aim and evidence, not a compliance claim.**

VeridianAI is designed _toward_ these standards and this file records, honestly,
where it stands. Nothing here asserts compliance. Neither standard is
self-certified in any meaningful sense — HIPAA compliance is a property of an
organisation and its practices, not of a piece of software, and a WCAG
conformance claim requires evaluation against the full success criteria,
including things only a human with assistive technology can judge.

**Have this reviewed by someone qualified before making any external claim.**
I am not a compliance authority and this document is engineering evidence, not
a legal opinion.

STAGING root: not packaged, not hashed.

---

## Why this is a reasonable aim at all

The architecture happens to line up with a lot of what both standards ask for,
because it was built for other reasons:

- **Local by default.** No account, no telemetry, no outbound connection unless
  the user enables one. Data that never leaves the machine has a much smaller
  surface to protect.
- **Encrypted at rest.** Fernet on conversations, memory, procedures, evidence.
- **Tamper-evident.** The memory chain is hash-chained, which is the _shape_ of
  an audit control even where it is not being used as one.
- **Per-profile isolation.** `sage_data/users/<ns>/` with `_safe_ns` containment.
- **Owner gating.** System-level actions are refused to non-owners with a
  uniform 404 cloak rather than a revealing 403.
- **A destruction control.** ZDR burn, in the main interface, one action.

---

## HIPAA — where the gap actually is

### The strong parts

| Safeguard (general terms)                   | Where it lives                                                              |
| ------------------------------------------- | --------------------------------------------------------------------------- |
| Access control / unique user identification | per-profile namespaces, scrypt accounts, session gate                       |
| Transmission security                       | loopback only by default; wss for the UI socket; TLS validation on outbound |
| Encryption at rest                          | Fernet across all stored user content                                       |
| Integrity                                   | hash-chained memory log, signed build manifest                              |
| Disposal                                    | ZDR burn, per-profile scoped                                                |
| Automatic logoff                            | auth cookie is session-scoped; window close clears it                       |

### The gap that was here: **attribution on the API surface** -- CLOSED in v2.14

`auth._keystore_dir()` resolves to `DATA_DIR`, and until v2.14 there was one
`.api_keystore.json` whose entries carried **labels, not owners**:

```json
{ "tokens": [{ "label": "default (rotated)", "hash": "...", "scopes": ["*"] }] }
```

So UI actions were attributable -- a cookie session names the user -- and API
actions were not. Anything arriving with a bearer token was "someone holding
the default key". Worse than the audit problem, it was an **access-control**
problem: `_session_ns()` found no cookie on an API request, returned `None`,
and every token therefore resolved to the **owner's** namespace regardless of
whose key it was.

### What was built

**1. Tokens are bound to a profile.** Entries gain `owner_ns`, deliberately
matching the convention `_session_ns` already uses -- `None` means the owner or
a single-user install, a string means that profile and nothing else -- so a
token principal drops into the existing namespace plumbing rather than
introducing a second notion of identity.

**2. `_verify_token` returns a principal, not a scope list.** It answers _who_
as well as _what may they do_. It had exactly one caller, which is why this was
tractable.

**3. Containment is enforced in one place.** `require_scope` publishes the
principal on `request.state`; `_session_ns` and `_is_owner` read it. Both live
in `main.py` and are consulted by every data endpoint, so the rule is enforced
at a chokepoint rather than at ~40 call sites -- which is the difference
between a rule you can verify and a rule you hope holds.

**4. Fail closed on unknowns.** A token that predates ownership and has not yet
been migrated is `bound: False`, and `_is_owner` refuses it. An unidentified
holder must not open owner-gated surfaces.

**5. Migration is loud.** An upgrading install has one token belonging to
nobody. Leaving it unbound would either break the user's working editor
integration or silently admit it as the owner. It becomes the owner's -- who
created it and has been using it -- and this is **announced on the console and
stamped on the entry** (`migrated_from_unowned`), so the keystore carries its
own history and the change is visible to an audit rather than inferred.

**6. Rotation is now personal.** It had to be owner-gated while one shared key
served everyone: rotating it broke every integration on the machine for every
profile, turning a personal security action into a system-wide outage. Bound
tokens make it individual -- a user who suspects their own key leaked replaces
it without permission and without disturbing anyone.

**7. Deleting an account revokes its tokens.** Sessions were already destroyed;
the bearer tokens were not, so a deleted account was deleted in the UI only and
its holder could keep reaching the API as a profile that no longer existed.

**8. Actions are attributed into the hash chain.** A new `audit` role -- a
first-class role, not metadata inside a `system` entry, so "everything that was
done, and by whom" is a one-field filter rather than a grep. Recorded for key
rotation, data export, ZDR burn, and account creation/deletion, carrying the
acting profile and either the token label+prefix or the session username. The
burn entry is written **before** the wipe, since the wipe destroys the chain it
would otherwise land in. Audit writes are best-effort and never fail a user's
action: the chain is tamper-**evident**, and a gap in it reads as a gap, which
is the honest failure mode.

Covered by `backend/test_api_ownership.py` -- **39 checks**, including
containment between profiles, fail-closed on unbound tokens, migration
idempotence, rotation isolation, revoke-on-delete, that `list_tokens` never
returns a hash or a raw token, and that a single-user install behaves exactly
as before.

**Single-user installs are unaffected.** `_is_owner` short-circuits to `True`
when `multiuser_enabled` is `False`, before any of this is consulted.

### CLOSED (2026-08-13) -- containment now reaches the token surface

The correction below stood for a few hours. It is resolved; both halves are
kept because the shape of the mistake is worth keeping.

**What the tokens gate** -- three endpoints, and only these:

| endpoint                    | scope        |
| --------------------------- | ------------ |
| `POST /v1/chat/completions` | `chat:write` |
| `GET /v1/models`            | `chat:read`  |
| `POST /mcp/v1/jsonrpc`      | `mcp:*`      |

**Two faults, not one.** The chat path was _broken_ under multi-user, not
merely unscoped: `_ws_bridge`'s mock websocket lacked `.cookies` and `.close`,
so `ws_chat`'s guard raised `AttributeError` straight out of the handler. It
failed CLOSED -- nothing leaked, the endpoint simply stopped working. The MCP
path had the real hole: it never asked whose token it was, so any valid token
read and wrote the owner's archives, downloads and procedural memory.

**Now:**

- the bridge carries an explicit pre-authenticated identity, documented as the
  auth bypass it is
- `/v1/chat/completions` resolves the namespace from the verified token and
  audits the turn
- MCP threads the namespace to the five tools that touch per-profile data;
  the other eleven are stateless and deliberately get nothing
- `search_all_archives` and `save_to_downloads` gained the `ns` parameter their
  siblings already had; `ProceduralMemory` is per-namespace
- the browser profile is bound at the MCP dispatch boundary -- it is chosen by
  a ContextVar `ws_chat` set and MCP never did, so every MCP browse previously
  ran in the OWNER's profile with the owner's cookies
- `access_policy.mcp_allowed`, default **True**, fail-open: same abilities for
  every profile, owner can revoke

**The hash chain stays shared, deliberately.** Per-profile _data_; one
tamper-evident _audit log_. Splitting the chain per user would turn "has
anything been tampered with?" into N questions and hide each profile's activity
from the owner -- backwards for an audit control. Entries are attributed
instead: `owner_ns` is stamped into every procedural witness.

**Verification -- 86 checks across three layers:**

| file                      | checks | proves                                                                                 |
| ------------------------- | ------ | -------------------------------------------------------------------------------------- |
| `test_api_ownership.py`   | 39     | the keystore binds tokens to profiles                                                  |
| `test_mcp_containment.py` | 28     | a namespace reaches the storage layer                                                  |
| `test_api_http.py`        | 19     | **over real HTTP**: the dependency publishes the principal where the endpoint reads it |

That third file exists because of what went wrong here. The first two would
have passed happily while the endpoints had no `request` parameter at all --
they test the plumbing, and the fault was that nothing flowed through it. A
chokepoint only contains what actually reaches it.

**Still manual:** two live profiles chatting through the real inference
pipeline on a running install. The HTTP tests mount the real auth dependency
and the real namespace logic on a minimal app, not `main.app` with its tiers.

**Known limitation, unchanged:** a non-owner can export their data in
plaintext but not encrypted, because the Fernet key is app-wide. Per-user
encryption keys are a stated goal and a larger change than this work.

---

## WCAG 2.2 AA — measured, not assumed

### Fixed and verified this cycle

| Item                                                                | Before                                                              | After                                             |
| ------------------------------------------------------------------- | ------------------------------------------------------------------- | ------------------------------------------------- |
| `--text-muted` on `--surface-2/-3` (parchment)                      | 4.40 / 3.88                                                         | **#30281e**, >=4.72 on all five surfaces          |
| `--error` (parchment)                                               | 4.43 / 3.91                                                         | **#550c0c**, >=4.73                               |
| `--teal` on `--surface-3` -- control borders **and the focus ring** | **2.70** (fails 1.4.11)                                             | **#0c4942**, >=3.33                               |
| `.status-text` -- the app's error channel                           | 11px at ~1.7:1                                                      | `--text-muted`, 12px, `.is-error`, `role="alert"` |
| Dialog focus handling                                               | focus never moved in; never restored; Tab escaped behind the dialog | shared `modal-a11y.js`                            |
| **`--text-faint` -- all 32 usages**                                 | 1.45-1.85:1 parchment, 2.13-2.39:1 dark                             | **retired**; every usage moved to `--text-muted`  |
| `.input-disclaimer` `opacity: 0.85`                                 | dragged even `--text-muted` to 3.76:1                               | removed                                           |
| `.scoreboard-clear`                                                 | 9px                                                                 | 11px                                              |
| Toggle knob, OFF state                                              | 2.13:1 dark / 1.45:1 parchment against its own track                | `--text-muted`, 5.44 / 4.72                       |

### Corrections to what this document previously claimed

Two statements in the last revision were wrong, and both were wrong in the
direction of making things look better than they were.

**1. "The auditor does not walk the surface ladder. It checks text against
`--bg` and `--surface` but not `--surface-2/-3`."**

It checked neither. Rules 1.4.3 and 1.4.11 read the `style=` attribute out of
the static DOM and only fired on elements carrying **both** `color` and
`background-color` inline. A stylesheet-driven application presents none, so
both rules returned **PASS having measured nothing** -- and `color:
var(--text-faint)` would not have parsed as a colour even if one had been
found. 1.4.11 additionally compared everything "against white as a baseline",
which inverts on the dark theme.

A vacuous pass is worse than a failure: it is a failure that reports as
compliance. Every clean contrast result this tool has ever produced for
VeridianAI should be treated as unmeasured, not as passing.

**2. "`--text-faint` is 1.45-1.85:1 on every light surface."**

Also on every _dark_ surface, at 2.13-2.39:1. The dark theme was never clean;
it was never checked.

### What the auditor does now

- `core/browser.py` -- **the browser was being closed before any rule ran.**
  `browse_target` closed it in a `finally` and then returned the page, so every
  caller got a dead object. It never surfaced because not one of the 56 rules
  used `page`. It is now an async context manager and the page stays live.
- `core/contrast.py` -- computed-style probing: resolves CSS variables, walks
  the ancestor chain compositing translucent backgrounds, folds inherited
  `opacity` into the text colour, and reports text over images as REVIEW rather
  than guessing.
- **1.4.3** measures real text against its real composited backdrop, dedupes by
  colour pair, and states disabled-control exemptions instead of hiding them.
- **1.4.11** uses the actual adjacent colour, and **focuses each control to
  measure the focus indicator that really appears** -- including box-shadow
  rings. A control with no focus indicator at all is now reported.
- `--theme` flag: a theme is a different set of colours, so it is a different
  audit run.
- `check_tokens.py` -- **no browser needed.** Every text token against every
  surface token, both themes, in under a second. Thresholds follow each token's
  _actual_ declarations in the stylesheet, not its name: `color` usage is judged
  at 4.5:1 and reported as a failure, border/outline usage at 3:1 and reported
  as review, because 1.4.11 exempts decorative dividers and a token cannot say
  which it is drawing.
- 26 regression tests (`test_contrast_rules.py`) covering the colour maths, the
  surface ladder, large-text thresholds, opacity, exemptions and the
  dead-page-is-ERROR case.

**Not yet run against a live page.** Chromium could not be downloaded in the
environment these changes were made in, so the browser round-trip is unproven
and is on the release checklist. The probe JS is asserted to parse as a single
function, and the rule logic is tested against stubbed probe output.

### Known outstanding -- found by the repaired checker

`check_tokens.py` now reports **32 failing text pairings across six tokens**,
none of which the old tooling could see. These are palette decisions, not
mechanical fixes, so they are listed rather than changed:

| Token        | Theme     | Range                           | Note                                                                                              |
| ------------ | --------- | ------------------------------- | ------------------------------------------------------------------------------------------------- |
| `--warning`  | parchment | **1.42-1.82**                   | a warning that cannot be read is the worst case here                                              |
| `--success`  | parchment | 2.59-3.32                       | used for the "Detected" hardware badge                                                            |
| `--teal`     | parchment | 3.34-4.28                       | passes as a _border_ (3:1); fails where it is text                                                |
| `--teal-dim` | both      | 3.22-4.22                       | text usage                                                                                        |
| `--gold-dim` | parchment | 3.41-4.38                       | text usage                                                                                        |
| `--gold`     | parchment | 4.32 / 3.84 on `--surface-2/-3` | **the ladder gap, recurring** -- the earlier fix cleared `--bg` and `--surface` and stopped there |

`--gold` is the instructive one: it was deliberately tuned for AA in v2.12.7,
and it still failed two surfaces down, because the check that blessed it only
looked at two of five surfaces. That is the argument for the matrix in one line.

Also outstanding:

- **`--border` and `--border-hi` sit at 1.16-2.07:1** on every surface, both
  themes. Reported as REVIEW, not FAIL: 1.4.11 exempts decorative dividers, and
  these tokens draw both panel edges and control edges. The control usages need
  a decision.
- **Smallest font sizes are ~11px.** AA sets no minimum, but at that size 4.5:1
  is uncomfortable for many readers, including this project's author. Worth
  targeting AAA (7:1) for body text.
- **No screen-reader pass.** NVDA/JAWS testing has not been done. Several AA
  criteria cannot be settled without it.

## A framing correction (2026-08-14)

Earlier revisions of this document justified deferrals with "single-user
installs -- the overwhelming majority". **That assumption was wrong and it was
shaping the recommendations.**

- Commercial / institutional licences are the intended revenue path. Those
  installs are multi-profile by definition.
- Solo users have a real reason to turn multi-user on: password-protected,
  isolated profiles per project.
- A licence buyer evaluating this will look at exactly the multi-user path, and
  a feature that only works properly for one person is not something anyone
  pays for.

So multi-user is not the edge case. Anywhere this document treats it as one,
read it as an error rather than a judgement. The v2.14.2 containment work and
the per-user encryption map (`PLAN_per_user_encryption.md`) are both graded on
that basis now.

---

## Recommended order

1. ~~**Fix the auditor**~~ -- **done.** Computed styles, live page, surface
   ladder, focus indicators, and a browserless token matrix that can gate a
   build. This mattered more than any single token, and finding six more
   failing tokens within a minute of it working is the evidence.
2. ~~**Resolve `--text-faint`**~~ -- **done.** All 32 usages proved to be
   informational; none needed the exemption, so the token was retired and
   aliased to `--text-muted` rather than deleted, so any theme still referencing
   it inherits a passing colour.
3. **Decide the six remaining text tokens**, starting with `--warning` on
   parchment at 1.42:1.
4. **Screen-reader pass** with NVDA.
5. ~~**Per-user API tokens**~~ -- **done in v2.14.** Tokens bound to profiles,
   containment at the chokepoint, per-user rotation, revoke-on-delete, and
   actions attributed into the hash chain under an `audit` role.

Items 1-4 are accessibility work with immediate user benefit regardless of any
standard. Item 5 was compliance groundwork; it also happens to make separate
profiles genuinely useful to a solo user, since isolation between projects is
worth having whether or not anyone else touches the machine.

---

## A note on exemptions

The working rule this cycle, and the right one:

> An exemption should not be used where a pass is achievable. Where a pass is
> genuinely not realistic, an exemption is fine **with a written justification**
> for why it applies.

Applied to `--text-faint`, it left nothing exempt. Two usages looked incidental
and neither survived scrutiny: `.hw-badge.unavailable` renders "Not found",
which is a detection result and not text inside an inactive control; and the
toggle knob is the visual indicator of the control's state, which 1.4.11
covers explicitly. The knob was the closer call and the more consequential one
-- in the OFF state it was reading as an empty track rather than an off one.

# This document and all documentation has been generated by AI and Human edited.

- A Human (Todd [That's Me, the Human]) Architect/Director/Editor led AI coding
  team of multiple current leading online frontier models, and many local models
  using VeridianAI's multi-model slots with Toga (very large local model library).
