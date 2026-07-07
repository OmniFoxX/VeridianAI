#!/usr/bin/env python3
"""
rotate_api_key.py -- generate a fresh OracleAI default API key
================================================================

v2.3.1 (2026-06-06): tokens now stored as prefix + SHA-256 hash.
Raw token is shown once and never written to disk.

v2.3.0 (2026-05-31): initial release.

USAGE
-----
    py rotate_api_key.py

WHAT IT DOES
------------
1. Loads (or creates) the keystore at backend/.api_keystore.json
2. Removes the existing 'default' token entry (if any)
3. Issues a fresh 256-bit random token with ["*"] scope and
   label='default (rotated)'
4. Persists the keystore atomically (prefix + hash only -- raw
   token is NEVER written to disk)
5. Prints the new token ONCE (this is the only time it is shown)

WHAT IT DOES NOT DO
-------------------
- Touch additional tokens with OTHER labels. If you have a scoped
  Continue.dev token (e.g. label='continue-dev', scopes=['mcp:*']),
  that token survives the rotation unchanged.
- Restart the FastAPI server. After rotation, the running server
  must be restarted (or the keystore reloaded) for it to see the
  new token. The simplest path: close OracleAI, run this script,
  restart OracleAI, copy the new token into your client config.
"""

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Make sure we can import auth.py from backend/ regardless of where
# this script is invoked from.
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPT_DIR / "backend"

if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

try:
    from auth import rotate_default_token, KEYSTORE_PATH
except ImportError as exc:
    print()
    print("  ERROR: could not import auth.py from backend/")
    print(f"  Detail: {exc}")
    print()
    print("  Make sure you are running this script from the OracleAI")
    print("  project root (e.g.  py rotate_api_key.py  from E:\\OracleAI_v2.3)")
    print()
    sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    bar = "=" * 72

    print()
    print(bar)
    print("  OracleAI -- API KEY ROTATION")
    print(bar)
    print()
    print("  Rotating default token ...")
    print()

    try:
        new_token = rotate_default_token()
    except Exception as exc:
        print(f"  ERROR during rotation: {exc}")
        print()
        sys.exit(1)

    print("  Done. New token (shown ONCE -- copy it now):")
    print()
    print(f"      {new_token}")
    print()
    print("  Keystore updated at:")
    print(f"      {KEYSTORE_PATH}")
    print()
    print("  The raw token is NOT stored in the keystore.")
    print("  Only a prefix + SHA-256 hash are persisted.")
    print()
    print("  Next steps:")
    print("    1. Close OracleAI (the FastAPI server must restart to")
    print("       pick up the new token)")
    print("    2. Restart OracleAI")
    print("    3. Paste the token above into your client config:")
    print()
    print("       Continue.dev  (~\\.continue\\config.yaml):")
    print("         requestOptions:")
    print("           headers:")
    print("             Authorization: Bearer <token>")
    print()
    print("       VS Code mcp.json:")
    print('         "headers": { "Authorization": "Bearer <token>" }')
    print()
    print("  Non-default tokens (e.g. continue-dev scoped token)")
    print("  were NOT rotated and remain valid.")
    print(bar)
    print()


if __name__ == "__main__":
    main()