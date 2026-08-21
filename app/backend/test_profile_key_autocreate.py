#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_profile_key_autocreate.py -- no profile lives on the system key forever.

THE QUESTION THAT FOUND THIS (Todd, 2026-08-21)

    "Does any non-owner profile ever resolve to the system key, or is it
     exclusively the owner's legacy compatibility path? If all but the owner
     resolve to the same, is the only profile with unique protection the
     owner?"

Traced, not guessed. atrest._fernet_for returns the profile's key IF one is
registered, and the SYSTEM key otherwise. A profile has none when it was
created before per-profile keys existed, or when key creation failed at
profile creation (that path is try/except and the profile is still made).

Live state of the install this was found on:

    sage_9083a3c5   own_key=True    migration_done=True
    toga_13e1f447   own_key=True    migration_done=False
    todd-1          own_key=False   migration_done=False   <-- system key

So: NOT "all but the owner share one key" -- but one real profile did, and
would have forever. unlock() returns None for a keyless profile,
key_migration.run() then refuses with "profile key is not unlocked", and
nothing else was ever going to create one. It logged in with a password every
day and stayed on the owner's key.

THE FIX, AND WHY IT IS AT LOGIN

Creating a key at WRITE time cannot work: there is no password there.
create_for_profile(ns, "") produces a keywrap openable with an empty string
AND unopenable with the user's real password -- worse than the gap, in two
directions. Login is the only moment a password exists, so that is where the
key is created.

MIXED-KEY STORES (Todd's follow-up) are handled by machinery that already
existed: _migrate_profile_key_once runs immediately after the key is attached
and re-encrypts that profile's existing files. It always refused for these
profiles because there was no key to convert TO. Creating one unblocks it.

    python test_profile_key_autocreate.py
"""
import io
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


import atrest                                                 # noqa: E402
import profile_keys as PK                                     # noqa: E402
import keywrap                                                # noqa: E402

MAIN = io.open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
AT = io.open(os.path.join(_HERE, "atrest.py"), encoding="utf-8").read()
PKS = io.open(os.path.join(_HERE, "profile_keys.py"), encoding="utf-8").read()


# =============================================================================
print("=== 1. The write fallback is no longer silent ===")
# =============================================================================
ok("encrypt_bytes warns on a system-key write for a namespace",
   "WRITE side" in AT or "the WRITE side" in AT)
ok("...only for a NAMESPACE, never the owner",
   "if ns and str(ns) not in _PROFILE_FERNETS" in AT,
   "for the owner the system key IS their key; warning there would teach "
   "people to ignore the real case")
ok("...once per namespace, not once per write",
   "_WRITE_FALLBACK_WARNED" in AT)
ok("the READ fallback stays silent",
   "_WRITE_FALLBACK_WARNED" not in AT.split("def decrypt_bytes")[1][:900],
   "reads falling back is what makes migration lossless and keeps readable "
   "exports working -- it must not become noise")
ok("the warning is inspectable, not just printed",
   "def write_fallback_warned" in AT)

atrest._WRITE_FALLBACK_WARNED.clear()
_blob = atrest.encrypt_bytes(b"x", ns="ghost_profile")
ok("a keyless namespace write is recorded",
   "ghost_profile" in atrest.write_fallback_warned())
_before = set(atrest.write_fallback_warned())
atrest.encrypt_bytes(b"y", ns="ghost_profile")
ok("...and not re-recorded on every write",
   atrest.write_fallback_warned() == _before)
atrest.encrypt_bytes(b"z", ns=None)
ok("the OWNER never triggers it", None not in atrest.write_fallback_warned()
   and "None" not in atrest.write_fallback_warned())
atrest._WRITE_FALLBACK_WARNED.clear()


# =============================================================================
print("\n=== 2. ensure_for_profile: create only when it is safe ===")
# =============================================================================
ok("ensure_for_profile exists", hasattr(PK, "ensure_for_profile"))
ok("...and is exported, not an accident of module scope",
   "ensure_for_profile" in PK.__all__ and "load_dek_or_none" in PK.__all__,
   PK.__all__)
ok("the owner gets None (system key by design)",
   PK.ensure_for_profile(None, "anything") is None)
ok("a keyless profile with NO password is not given a key",
   PK.ensure_for_profile("nobody_ns", "") is None,
   "a passwordless keywrap is openable by anyone or by nobody -- both worse "
   "than the gap")
ok("the empty-password hazard is documented, not just avoided",
   'create_for_profile(ns, "")' in PKS)
ok("recovery defaults ON, and the reason is given",
   "recovery: bool = True" in PKS and "the owner CAN read" in PKS,
   "an automatic upgrade must not silently remove an ability the install "
   "already had")
ok("a wrong password cannot orphan an existing key",
   "load_dek_or_none" in PKS and "orphan" in PKS,
   "creating a second key over a real one would strand the data the first "
   "one protects")


# =============================================================================
print("\n=== 3. End to end, on real files ===")
# =============================================================================
_TMP = tempfile.mkdtemp(prefix="vai_pk_")
_orig_data_dir = PK._data_dir
_orig_user_dir = PK._user_dir
try:
    from pathlib import Path as _P

    PK._data_dir = lambda: _P(_TMP)
    PK._user_dir = lambda ns: (_P(_TMP) / "users" / str(ns)) if ns else None

    NS = "legacy_user"
    _udir = _P(_TMP) / "users" / NS
    _udir.mkdir(parents=True, exist_ok=True)

    ok("the profile starts with NO key of its own", not PK.has_profile_key(NS))

    # Data written while keyless lands under the SYSTEM key -- the very thing
    # Todd asked about.
    SECRET = b"legacy-user-private-conversation-text"
    _f = _udir / "chat_memory.json"
    _f.write_bytes(atrest.encrypt_bytes(SECRET, ns=NS))
    ok("its data is readable with the SYSTEM key (the exposure)",
       atrest.decrypt_with_system_key(_f.read_bytes()) == SECRET)

    # Login: password in hand -> a key is created.
    _dek = PK.ensure_for_profile(NS, "correct horse battery staple")
    ok("logging in creates the missing key", bool(_dek))
    ok("...and the profile now HAS one", PK.has_profile_key(NS))
    ok("...openable with that password",
       PK.unlock(NS, "correct horse battery staple") == _dek)
    ok("...and NOT with a different one",
       PK.load_dek_or_none(NS, "wrong") in (None,))

    # Register it the way _hold_profile_key does, then re-encrypt.
    atrest.register_profile_key(NS, _dek)
    ok("the key is now registered for this process", atrest.has_profile_key(NS))

    _new = atrest.encrypt_bytes(SECRET, ns=NS)
    ok("NEW writes use the profile key",
       atrest.decrypt_with_profile_key(_new, NS) == SECRET)
    _sys_fails = False
    try:
        atrest.decrypt_with_system_key(_new)
    except Exception:
        _sys_fails = True
    ok("...and the system key can no longer open them", _sys_fails,
       "this is the separation the whole question was about")

    # The mixed-key store Todd flagged: the OLD file is still system-key.
    ok("the old file is still under the system key (the mixed store)",
       atrest.decrypt_with_system_key(_f.read_bytes()) == SECRET)
    # ...which is exactly what the migration pass is for.
    _converted = atrest.encrypt_bytes(
        atrest.decrypt_bytes(_f.read_bytes(), ns=NS), ns=NS)
    _f.write_bytes(_converted)
    ok("re-encrypting it moves it under the profile key",
       atrest.decrypt_with_profile_key(_f.read_bytes(), NS) == SECRET)
    _sys_fails2 = False
    try:
        atrest.decrypt_with_system_key(_f.read_bytes())
    except Exception:
        _sys_fails2 = True
    ok("...and it is no longer system-readable", _sys_fails2,
       "no mixed-key store left behind")

    atrest.forget_profile_key(NS)
finally:
    PK._data_dir = _orig_data_dir
    PK._user_dir = _orig_user_dir
    shutil.rmtree(_TMP, ignore_errors=True)


# =============================================================================
print("\n=== 4. Wired at BOTH login paths, before the migration pass ===")
# =============================================================================
ok("_attach_profile_key ensures rather than unlocks",
   "_pk.ensure_for_profile(ns, password)" in MAIN)
ok("the MFA challenge path ensures too",
   "_pk.ensure_for_profile(r[\"ns\"], _login_password)" in MAIN,
   "that is the MFA path's only sight of the password -- by /verify it is gone")
ok("no login path still calls the bare unlock()",
   "_pk.unlock(ns, password)" not in MAIN)

# Ordering is the whole point: create, THEN convert.
_a = MAIN.index("_attach_profile_key(tok, r.get(\"ns\"), _login_password)")
_m = MAIN.index("_mig = _migrate_profile_key_once(request, r.get(\"ns\"))")
ok("the key is attached BEFORE the migration pass runs", _a < _m,
   "migration refuses with 'profile key is not unlocked' if it goes first, "
   "which is exactly why these profiles never converted")
ok("the dependency is written down where it can be broken",
   "_migrate_profile_key_once runs immediately after" in MAIN)


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
