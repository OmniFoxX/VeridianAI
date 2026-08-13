"""Offline MFA rescue hatch -- run ON the machine, from the project root:

    python tools/reset_mfa.py <username>

Clears every second factor (TOTP, security keys, recovery codes) for the
account so it can sign in with password alone and re-enroll. This is the
OWNER-lockout escape: physical access to the box + sage_data IS the proof of
ownership in a local-first install, which is exactly the trust anchor the rest
of VeridianAI already relies on (the Fernet keys live in the same place).

Does not touch the password. Prints what it did; refuses silently-fuzzy input.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "backend"))


def main():
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print(__doc__)
        return 2
    username = sys.argv[1].strip()
    try:
        import users
        import mfa
    except Exception as e:
        print("Could not import the backend (run from the project root): %s" % e)
        return 1
    if not users.user_exists(username):
        print("No account named %r exists." % username)
        return 1
    st = mfa.status(username)
    if not (st["totp_enabled"] or st["fido2_keys"]):
        print("Account %r has no MFA enrolled; nothing to reset." % username)
        return 0
    print("Account %r currently has: totp=%s, security_keys=%d, recovery=%d"
          % (username, st["totp_enabled"], len(st["fido2_keys"]),
             st["recovery_remaining"]))
    ans = input("Type the username again to confirm the reset: ").strip()
    if ans.lower() != username.lower():
        print("Confirmation did not match; aborted.")
        return 1
    mfa.reset_user(username)
    print("MFA cleared for %r. They can sign in with their password and "
          "re-enroll from the Security panel." % username)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
