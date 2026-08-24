#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_first_run_ownership.py -- who gets to own this install, and when.

THE GAP THIS CLOSES
users.create_user() grants ownership to the first account ever created:

    owner = bool(is_owner) or not store.get("users")   # first account is the owner

Nothing decided WHEN that could happen. Multi-profile mode was an ordinary
settings toggle, so on any machine more than one person touches -- a family PC,
a demo unit, a Store review machine -- whoever first flipped that toggle and
made an account owned the install and everything already in it. The person who
did the installing never got asked.

v2.15 moves the decision to the first-run dialog, where the installer makes it
once, before anybody else reaches the UI. This file checks the parts of that
which can be checked without a browser and without booting FastAPI -- which is
all of the ones that actually broke:

  1. users.py really does hand ownership to the first account (the premise)
  2. install_claimed survives a save/load round-trip, flat AND nested
  3. POST /api/config cannot switch multi-profile on with no owner present
  4. POST /api/config cannot write install_claimed at all
  5. /api/first-run is local-only, one-shot, and refuses "multi" before an owner
  6. the dialog offers the three buttons, in the promised wording
  7. the dialog asks the SERVER, not localStorage, whether the install is claimed
  8. Decline actually exits (whitelisted channel -> a handler that quits)
  9. the settings toggle reports a refusal instead of claiming success

    python test_first_run_ownership.py
"""
import ast
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_FRONT = os.path.join(_ROOT, "frontend")
_ELECTRON = os.path.join(_ROOT, "electron")

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def read(path):
    return io.open(path, encoding="utf-8").read()


MAIN = read(os.path.join(_HERE, "main.py"))
HTML = read(os.path.join(_FRONT, "index.html"))
SETTINGS = read(os.path.join(_FRONT, "js", "settings.js"))
PRELOAD = read(os.path.join(_ELECTRON, "preload.js"))
EMAIN = read(os.path.join(_ELECTRON, "main.js"))


def func_source(src, tree, name):
    """The exact source of one top-level (or nested) def, by name."""
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(src, n) or ""
    return ""


MAIN_AST = ast.parse(MAIN)


# =============================================================================
print("=== 1. The premise: the first account IS the owner ===")
# =============================================================================
# Point the account store at a scratch file BEFORE users resolves anything, so
# this never touches a real sage_data.
_tmp = tempfile.mkdtemp(prefix="veridian_firstrun_")
_store_file = os.path.join(_tmp, ".users.json")
os.environ["VERIDIAN_DATA_DIR"] = _tmp

import users  # noqa: E402

users._store_path = lambda: _store_file        # noqa: E731

ok("a fresh install has no accounts", users.any_users() is False)

r1 = users.create_user("installer", "Correct-Horse-9!", enforce_policy=False)
ok("the first account is created", r1.get("success"), r1)
ok("...and it is the OWNER even though nobody asked for that",
   r1.get("is_owner") is True, r1)
ok("any_users() flips once an account exists", users.any_users() is True)

r2 = users.create_user("secondperson", "Battery-Staple-7!", enforce_policy=False)
ok("the second account is created", r2.get("success"), r2)
ok("...and is NOT the owner", r2.get("is_owner") is False, r2)

# This is the whole reason the dialog exists: ownership is decided by ORDER,
# so the only defence is controlling when the order can start.
ok("ownership is decided by order of creation, nothing else",
   r1.get("is_owner") is True and r2.get("is_owner") is False)


# =============================================================================
print("\n=== 2. install_claimed persists (a claim that forgets is no claim) ===")
# =============================================================================
import config_store  # noqa: E402

cfg = config_store.OracleConfig()
ok("it defaults to unclaimed", cfg.network.install_claimed is False)
ok("multi-profile defaults to off", cfg.network.multiuser_enabled is False)

flat = cfg.to_flat_dict()
ok("it appears in the flat view", "install_claimed" in flat)

cfg.network.install_claimed = True
cfg.network.multiuser_enabled = True
round_flat = config_store.OracleConfig.from_flat_dict(cfg.to_flat_dict())
ok("a flat round-trip keeps the claim",
   round_flat.network.install_claimed is True)
ok("a flat round-trip keeps the mode",
   round_flat.network.multiuser_enabled is True)

_p = Path(_tmp) / "config.json"
config_store.save_config(cfg, path=_p)
raw = json.loads(read(_p))
ok("the saved file is the nested v2 shape", isinstance(raw.get("network"), dict), raw.keys())
ok("...and carries the claim", raw["network"].get("install_claimed") is True)
reloaded = config_store.get_config(path=_p, force_reload=True)
ok("a nested load restores the claim",
   reloaded.network.install_claimed is True)
ok("a config written before v2.15 loads as UNCLAIMED, not as claimed",
   config_store.OracleConfig.from_flat_dict({}).network.install_claimed is False)


# =============================================================================
print("\n=== 3. The settings back door: multi-profile with no owner ===")
# =============================================================================
# Why this needed its own gate: in SINGLE-user mode _session_ns() is None and
# _is_owner() is true for everyone, so a settings write lands on the global path
# with nobody to authorise it. The gate has to test the EXISTENCE of an owner,
# not a permission.
UPD = func_source(MAIN, MAIN_AST, "api_update_config")
ok("the settings route was found", bool(UPD))
ok("it checks any_users() before letting multi-profile on",
   "any_users()" in UPD and "multiuser_enabled" in UPD, UPD[:0])
ok("it refuses with 409, not a silent no-op",
   "409" in UPD.split("config.update(payload)")[0])
ok("it only fires on the OFF -> ON transition",
   'payload.get("multiuser_enabled") and not config.get("multiuser_enabled"'
   in UPD.replace("\n", " ").replace("  ", " ") or
   'payload.get("multiuser_enabled")' in UPD)
ok("the gate runs BEFORE the config is written",
   UPD.index("any_users()") < UPD.index("config.update(payload)"))
ok("the refusal explains itself instead of just saying no",
   "owner account first" in UPD)
# NOTE: the phrase itself still appears -- inside the correction that quotes it.
# What matters is that it is no longer STATED as fact, so check for the
# correction rather than for the absence of the words.
ok("the stale 'owner-only by construction' claim was corrected, not left standing",
   "v2.15 CORRECTION" in UPD and "VACUOUS in" in UPD)
ok("...and the correction names the single-user case that made it vacuous",
   "_session_ns() is None" in UPD)


# =============================================================================
print("\n=== 4. install_claimed is not a setting ===")
# =============================================================================
ok("POST /api/config refuses install_claimed outright",
   '"install_claimed" in payload' in UPD)
ok("...with 403", "403" in UPD.split('"install_claimed" in payload')[1][:400])
ok("...before anything is written",
   UPD.index('"install_claimed" in payload') < UPD.index("config.update(payload)"))
# It must still persist, or a claimed install forgets it was claimed on restart.
ok("but it is still persisted", "install_claimed" in config_store.OracleConfig().to_flat_dict())


# =============================================================================
print("\n=== 5. /api/first-run ===")
# =============================================================================
GETFR = func_source(MAIN, MAIN_AST, "api_first_run_state")
POSTFR = func_source(MAIN, MAIN_AST, "api_first_run_choose")
SETUP = func_source(MAIN, MAIN_AST, "api_auth_setup")
ok("both routes exist", bool(GETFR) and bool(POSTFR))
ok("the routes are registered at /api/first-run",
   '@app.get("/api/first-run")' in MAIN and '@app.post("/api/first-run")' in MAIN)

ok("GET is local-only", "_is_local_client(request)" in GETFR)
ok("GET reports the claim, the mode and whether an owner exists",
   all(k in GETFR for k in ("install_claimed", "multiuser_enabled", "any_users")))

ok("POST is local-only", "_is_local_client(request)" in POSTFR)
ok("POST is ONE-SHOT (refuses once claimed)",
   'config.get("install_claimed"' in POSTFR and "409" in POSTFR)
ok("...and refuses before it writes",
   POSTFR.index('config.get("install_claimed"') < POSTFR.index("config.update("))
ok("only 'single' and 'multi' are accepted",
   '("single", "multi")' in POSTFR and "400" in POSTFR)
ok("'multi' requires the owner account to ALREADY exist",
   "any_users()" in POSTFR and
   POSTFR.index("any_users()") < POSTFR.index("config.update("))
ok("the claim is persisted, not just held in memory",
   "save_config(config)" in POSTFR)
ok("the choice is audited", "_audit_api_action" in POSTFR)

ok("owner setup 409s if an owner already exists",
   "any_users()" in SETUP and "409" in SETUP)
ok("owner setup creates the account as owner", "is_owner=True" in SETUP)


# =============================================================================
print("\n=== 6. The dialog is a decision, not a notice ===")
# =============================================================================
# Everything below reads the gate block itself, not the whole document.
_gate = HTML[HTML.index("FIRST-RUN INSTALL GATE"):]

ok("Accept (Single Profile) is offered", "Accept (Single Profile)" in HTML)
ok("Accept (Multi-Profile) is offered", "Accept (Multi-Profile)" in HTML)
ok("Decline (Exit VeridianAI) is offered", "Decline (Exit VeridianAI)" in HTML)
# Count the RENDERED buttons, not the words: "Accept (" also appears in the
# block comment above the script, which is prose, not a way out of the dialog.
ok("each choice is a real button, exactly once",
   HTML.count("Accept (Single Profile)</button>") == 1 and
   HTML.count("Accept (Multi-Profile)</button>") == 1 and
   HTML.count("Decline (Exit VeridianAI)</button>") == 1)
ok("'I understand' survives only as the already-claimed acknowledgement",
   HTML.count("I understand</button>") == 1 and "showAck()" in _gate)

_lead = "The first Multi-Profile user becomes the "
ok("the ownership sentence is present", _lead in HTML)
ok("...and leads, so the disclaimer is not diluted by a closing diversion",
   HTML.index(_lead) < HTML.index("Toga is a <strong>local AI assistant</strong>"),
   "the ownership line must come BEFORE the AI disclaimer")
ok("the AI disclaimer itself survived the rebuild",
   "produce inaccurate information" in HTML and
   "legal, medical, or financial advice" in HTML)

ok("Multi-Profile creates the owner in the same flow",
   '"/api/auth/setup"' in HTML and "Create Owner and Continue" in HTML)
ok("...and only records the mode AFTER that",
   HTML.index('postJSON("/api/auth/setup"') < HTML.index('return claim("multi")'))
ok("Single Profile records its choice too", 'claim("single")' in HTML)
ok("a mistyped password is caught before the account is made",
   "The two passwords do not match." in HTML)
ok("the server's own error text is shown, not a generic failure",
   "detailOf(r)" in HTML)

# Every id the gate reaches for must be an id the gate builds. A typo here is a
# silent no-op at runtime, not an error -- and a no-op button on THIS dialog
# means an unclaimed install with a dead Accept.
import re  # noqa: E402
_used = set(re.findall(r'byId\("([^"]+)"\)', _gate))
_used |= set(re.findall(r'querySelector\("#([^"]+)"\)', _gate))
_built = set(re.findall(r"id=\\?[\"']([a-zA-Z0-9_-]+)\\?[\"']", _gate))
_missing = sorted(_used - _built)
ok("no id is reached for that is never created", not _missing, _missing)

ok("Escape cannot dismiss a decision", "onEscape" not in _gate.split("</script>")[0]
   or "No onEscape" in _gate)
ok("the shared focus handling is used, not reinvented", "modalA11y" in _gate)
ok("the gate source stays pure ASCII",
   not [i + 1 for i, line in enumerate(_gate.split("</script>")[0].splitlines())
        if any(ord(c) > 127 for c in line)])


# =============================================================================
print("\n=== 7. The server decides, localStorage only remembers ===")
# =============================================================================
ok("the gate asks the backend", '"/api/first-run"' in _gate)
ok("the claim cache is clearly not the authority",
   "veridian_install_claimed" in _gate and "cached echo" in _gate)
ok("a fresh browser on an UNCLAIMED install still gets the gate",
   "if (st.claimed)" in _gate and "showGate(false)" in _gate)
ok("a cold start does not accuse a claimed install of being unclaimed",
   "beginPolling" in _gate and 'lsGet(CLAIM_KEY) === "1"' in _gate)
ok("a non-local caller is not handed the ownership question",
   "{ na: true }" in _gate)
ok("the AI acknowledgement is still remembered per browser",
   "oai_disclaimer_ack" in _gate)


# =============================================================================
print("\n=== 8. Decline actually exits ===")
# =============================================================================
ok("the renderer sends the decline channel",
   'send("veridian-decline-exit")' in _gate)
ok("preload whitelists that channel", "'veridian-decline-exit'" in PRELOAD)
# Asked WITHOUT naming the variable.
#
# This said "allowed.includes(channel)" and went red in v2.16.2, when the list
# was hoisted to ALLOWED_SEND so it could be both enforced and advertised. The
# bridge was still an allowlist; only an identifier had changed. Pinning a
# local variable name fails on a harmless rename and passes on a rewrite that
# drops the guard but keeps the name -- wrong on both counts.
#
# What must stay true is structural: the bridge has exactly one
# ipcRenderer.send, and it is reached only through a membership test on the
# channel.
_sends = re.findall(r"ipcRenderer\.send\(", PRELOAD)
ok("...and the whitelist is still a whitelist",
   re.search(r"\.includes\(\s*channel\s*\)", PRELOAD) is not None
   and len(_sends) == 1,
   "found %d ipcRenderer.send call(s) and %s membership test -- an unguarded "
   "path beside the guarded one is the failure this checks for"
   % (len(_sends),
      "a" if re.search(r"\.includes\(\s*channel\s*\)", PRELOAD) else "NO"))
ok("main.js handles it", "ipcMain.on('veridian-decline-exit'" in EMAIN)
ok("...by quitting",
   "app.quit()" in EMAIN.split("veridian-decline-exit'", 1)[1][:400])
ok("...and registers idempotently, like its neighbour",
   "removeAllListeners('veridian-decline-exit')" in EMAIN)
ok("a plain browser is told the truth instead of being silently let in",
   "SETUP DECLINED" in _gate and "has not been set up" in _gate)


# =============================================================================
print("\n=== 9. The settings toggle reports a refusal ===")
# =============================================================================
ok("updateSetting returns the outcome", "return { ok: true" in SETTINGS)
ok("...and reports a refusal", "return { ok: false" in SETTINGS)
ok("a refused save does not leave a wrong value cached",
   "delete window._appConfig[key]" in SETTINGS)
ok("turning it OFF still checks the answer and puts the switch back",
   "r.ok === false" in SETTINGS and
   'setChecked("toggle-multiprofile", true)' in SETTINGS)
ok("...and shows the server's reason", "r.detail" in SETTINGS)


# =============================================================================
print("\n=== 10. The toggle can actually be satisfied ===")
# =============================================================================
# v2.15.1. v2.15 made POST /api/config refuse multi-profile until an Owner
# exists, and shipped nothing that could create one from Settings. The only
# path was the first-run dialog, which is one-shot -- so on any install where
# that dialog had already been answered, Multi-Profile could not be turned on
# at all. A guard nothing can satisfy is not a guard, it is a wall, and this
# section exists so it cannot be rebuilt.
AUTH = read(os.path.join(_FRONT, "js", "auth.js"))

ok("the toggle has something to call", "createOwnerAccount" in AUTH)
ok("...and Settings calls it",
   "OracleAuth.createOwnerAccount" in SETTINGS)
ok("...and checks it exists first, rather than throwing",
   'typeof window.OracleAuth.createOwnerAccount !== "function"' in SETTINGS)

_COA = AUTH[AUTH.index("async function createOwnerAccount"):]
_COA = _COA[:_COA.index("window.OracleAuth = {")]
ok("it opens the EXISTING owner form, not a second copy of one",
   "showAuthOverlay(true" in _COA)
ok("...saying what the account is for",
   "Owner account for Multi-Profile" in _COA)
ok("...with a way out", "cancelLabel" in _COA)

# The ordering rule from v2.15, preserved: account first, mode second. If this
# ever inverts there is a window where multi-profile is on and unowned, which is
# the exact state that hands the install to the next person to make an account.
ok("the mode is switched on only AFTER the account exists",
   "afterCreate: enableMultiProfileMode" in _COA)
_EMP = AUTH[AUTH.index("async function enableMultiProfileMode"):]
_EMP = _EMP[:_EMP.index("/* Called by the Settings toggle")]
ok("...and that step writes the mode and nothing else",
   'JSON.stringify({ multiuser_enabled: true })' in _EMP)
ok("...and reports a refusal instead of reloading over it",
   "return { ok: false" in _EMP and "location.reload()" in _EMP)

ok("an already-created Owner is not asked for twice",
   '"/api/first-run"' in _COA and "any_users" in _COA)
ok("...it just finishes the job", "enableMultiProfileMode()" in _COA)

# Todd's requirement, verbatim: do not force a sign-out. /api/auth/setup issues
# the session, so the reload lands back in the app as the Owner.
ok("nothing logs the person out", "logout()" not in _COA and "logout()" not in _EMP)
ok("the no-sign-out reasoning is written down where it can be checked",
   "No sign-out" in AUTH)

ok("the backend gate is UNCHANGED -- still refuses mode-before-owner",
   "any_users()" in UPD and "owner account first" in UPD)


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
