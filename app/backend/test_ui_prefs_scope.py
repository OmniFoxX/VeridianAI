#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ui_prefs_scope.py -- one person's settings are not everybody's.

Todd found this from the outside: he turned browser cookie persistence on,
switched profiles, and it was still on for the next person. Every UI preference
lived in one file for the whole install, so a choice was not merely shared --
it could not come back, because there was only ever one value to come back to.

The behaviour asserted here is the one he described:

    A signs in and sets a preference. A signs out. B signs in and sees the
    DEFAULT, not A's choice. B sets their own. A signs back in and finds their
    own setting exactly as they left it.

Plus the half that makes it safe: Developer Mode is read by tier_launcher in a
daemon process where there is no signed-in user, so it must stay machine-wide.
A per-user answer there is not wrong, it is unanswerable.

    python test_ui_prefs_scope.py
"""
import os
import shutil
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="vai_uiprefs_")
os.environ["VERIDIAN_DATA_DIR"] = _TMP

from pathlib import Path                                     # noqa: E402
import ui_prefs                                              # noqa: E402

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


K = "browser_persist_cookies"

print("=== 1. The sequence Todd described ===")
ui_prefs.set(K, True, ns="alice")
ok("alice's choice is stored", ui_prefs.get(K, False, ns="alice") is True)
ok("BOB SEES THE DEFAULT, NOT ALICE'S", ui_prefs.get(K, False, ns="bob") is False)
ui_prefs.set(K, False, ns="bob")
ok("bob's own choice is stored", ui_prefs.get(K, True, ns="bob") is False)
ok("alice is unchanged by bob", ui_prefs.get(K, False, ns="alice") is True)
ok("and the owner is unaffected by either",
   ui_prefs.get(K, False) is False)

print("\n=== 2. A profile does not inherit the owner's choice ===")
# Falling back to the shared file would be the leak wearing a helpful hat: a
# new profile would silently start with whatever the owner had chosen.
ui_prefs.set(K, True)                       # owner turns it on
ok("owner has it on", ui_prefs.get(K, False) is True)
ok("a brand-new profile still gets the default",
   ui_prefs.get(K, False, ns="carol") is False)

print("\n=== 3. Files land where they belong ===")
ok("owner prefs stay in the shared file",
   (Path(_TMP) / "ui_prefs.json").exists())
ok("alice's live under her own profile",
   (Path(_TMP) / "users" / "alice" / "ui_prefs.json").exists())
ok("bob's are a different file",
   (Path(_TMP) / "users" / "bob" / "ui_prefs.json").exists())
_shared = (Path(_TMP) / "ui_prefs.json").read_text(encoding="utf-8")
ok("the shared file does not carry a profile's value",
   "alice" not in _shared and "bob" not in _shared, _shared)

print("\n=== 4. Machine keys stay machine-wide ===")
ui_prefs.set("developer_mode", True, ns="alice")   # ns offered, and ignored
ok("declared as a machine key", "developer_mode" in ui_prefs.MACHINE_KEYS)
ok("it landed in the shared file, not alice's",
   ui_prefs.get("developer_mode", False) is True)
ok("bob sees it too -- it is about the machine",
   ui_prefs.get("developer_mode", False, ns="bob") is True)
ok("alice has no private copy of it",
   "developer_mode" not in (Path(_TMP) / "users" / "alice" /
                            "ui_prefs.json").read_text(encoding="utf-8"))
ok("a daemon with no user still gets the real answer",
   ui_prefs.get("developer_mode", False) is True)

print("\n=== 5. devmode.py keeps working untouched ===")
import devmode                                               # noqa: E402
ok("devmode reads the machine value", devmode.is_enabled() is True)
devmode.set_enabled(False)
ok("and writes it back", ui_prefs.get("developer_mode", True) is False)
ok("still not in alice's file",
   ui_prefs.get("developer_mode", None, ns="alice") is False)

print("\n=== 6. Nothing raises on a missing or broken store ===")
ok("unknown key returns the default",
   ui_prefs.get("no_such_pref", "fallback", ns="nobody") == "fallback")
(Path(_TMP) / "users" / "dave").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "users" / "dave" / "ui_prefs.json").write_text("{ broken",
                                                             encoding="utf-8")
ok("a corrupt profile store reads as empty, not as a crash",
   ui_prefs.get(K, "safe", ns="dave") == "safe")
ok("and can still be written over",
   ui_prefs.set(K, True, ns="dave").get(K) is True)

shutil.rmtree(_TMP, ignore_errors=True)

_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
