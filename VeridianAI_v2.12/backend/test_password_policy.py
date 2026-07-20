"""Gate tests for password_policy.py + the users.py enforcement hook.

Run directly:  python backend/test_password_policy.py
Style matches test_expression_engine.py: plain asserts, loud counter, exit 1
on any failure. Hermetic: config + user store are stubbed to a temp dir so
nothing touches real sage_data.
"""
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# --- hermetic stubs BEFORE importing the modules under test --------------------
_TMP = tempfile.mkdtemp(prefix="vai_pw_test_")
_cfg = types.ModuleType("config")
_cfg.DATA_DIR = _TMP
_cfg.BACKEND_DIR = HERE
_cfg.get = lambda key, default=None: default
sys.modules["config"] = _cfg

import password_policy  # noqa: E402
import users  # noqa: E402
users._store_path = lambda: os.path.join(_TMP, ".users.json")

CHECKS = 0
FAILS = 0


def check(cond, label):
    global CHECKS, FAILS
    CHECKS += 1
    if cond:
        print("  ok  %s" % label)
    else:
        FAILS += 1
        print("FAIL  %s" % label)


def ok(pw, username=None):
    return password_policy.validate(pw, username=username)["ok"]


print("== length (NIST: length is the control; 16 min, 256 max) ==")
check(not ok("1"), "'1' rejected")
check(not ok("a"), "'a' rejected")
check(not ok("fifteen chars!!"), "15 chars rejected")
check(ok("gentle otter promenade"), "22-char passphrase accepted")
check(not ok("x" * 257), "257 chars rejected (DoS bound)")
check(ok("this is exactly!" ), "16 chars accepted")

print("== unicode + spaces are first-class ==")
check(ok("correct staple horse purple nine"), "spaces accepted")
check(ok("こんにちは世界の素晴"
         "らしい時間だね"), "16 CJK code points accepted")
check(not ok("\U0001F511" * 8), "8 emoji = 8 code points -> too short")
check(not ok("\U0001F511" * 16), "16 IDENTICAL emoji -> repeated-pattern reject")
_emoji16 = ("\U0001F511\U0001F98A\U0001F332\U0001F30A\U0001F3D4\U0001F98B"
            "\U0001F344\U0001F41D\U0001F335\U0001F42C\U0001F341\U0001F9ED"
            "\U0001F4D0\U0001F52D\U0001F9F2\U0001F9C2")
check(ok(_emoji16), "16 DISTINCT emoji accepted (code-point counting)")

print("== NO composition rules ==")
check(ok("purelylowercaselettersonly"), "all-lowercase accepted")
check(ok("2846150937261048275610"), "all-digits accepted (not sequential)")

print("== reject list: blocklist file ==")
check(not ok("passwordpassword"), "blocklist: passwordpassword")
check(not ok("PasswordPassword"), "blocklist is case-insensitive")
check(not ok("correcthorsebatterystaple"), "blocklist: xkcd classic")
check(not ok("correct horse battery staple"), "blocklist: normalized (spaces stripped)")
check(not ok("correct-horse-battery-staple"), "blocklist: normalized (dashes stripped)")
check(not ok("thequickbrownfoxjumpsoverthelazydog"), "blocklist: pangram")

print("== reject list: app name substring (case-insensitive) ==")
check(not ok("my VeridianAI password 42"), "contains VeridianAI")
check(not ok("ilove-veridian-forever"), "contains veridian")
check(not ok("ORACLEAI is my favourite app"), "contains ORACLEAI")
check(ok("a viridian green meadow at dusk"), "'viridian' (different word) accepted")

print("== reject list: username substring ==")
check(not ok("silverfox is my username ok", username="silverfox"), "contains username")
check(not ok("SILVERFOX4816 forever and ever", username="silverfox4816"), "case-insensitive")
check(ok("no trace of that name here", username="silverfox"), "clean of username -> ok")
check(ok("although albert said no", username="al"), "2-char username not substring-matched")

print("== algorithmic junk ==")
check(not ok("aaaaaaaaaaaaaaaa"), "single repeated char")
check(not ok("abcabcabcabcabcabc"), "short repeated pattern")
check(not ok("abcdefghijklmnopqrst"), "alphabet walk")
check(not ok("4321432143214321"), "repeated digit pattern")
check(not ok("qwertyuiopasdfghjklz"), "keyboard walk")
check(not ok("6543210987654321"[::-1]), "digit run")
check(ok("ababab plus actual words here"), "partial repeat inside a real phrase ok")

print("== strength meter (advisory only) ==")
s = password_policy.estimate_strength
check(s("")["score"] == 0, "empty -> 0")
check(s("passwordpassword")["score"] == 0, "blocklisted -> floored to 0")
check(s("short pass")["score"] <= 1, "short -> weak")
check(s("gentle otter promenade")["score"] >= 2, "22 chars -> okay+")
check(s("gentle otter promenades over the calm river")["score"] == 4, "43 chars -> excellent")
check("label" in s("anything at all here"), "label present")

print("== users.py enforcement hook ==")
r = users.create_user("todd_test", "1")
check(not r["success"], "create_user rejects '1'")
check("policy_errors" in r, "policy errors surfaced")
r = users.create_user("todd_test", "gentle otter promenade at dawn")
check(r["success"], "create_user accepts strong passphrase")
r = users.set_password("todd_test", "a")
check(not r["success"], "set_password rejects 'a'")
r = users.set_password("todd_test", "todd_test has a nice password", )
check(not r["success"], "set_password rejects password containing username")
r = users.set_password("todd_test", "quiet violet lantern by the sea")
check(r["success"], "set_password accepts strong passphrase")
r = users.create_user("legacy_user", "1", enforce_policy=False)
check(r["success"], "enforce_policy=False bypass (migration/tests only)")
v = users.verify_user("legacy_user", "1")
check(v["success"], "legacy weak password still VERIFIES (login-time recheck "
      "handles the upgrade, not a lockout)")

print("\n%d checks, %d failures" % (CHECKS, FAILS))
sys.exit(1 if FAILS else 0)
