"""Multi-factor auth for VeridianAI accounts: TOTP + recovery codes + FIDO2.

LOCAL-ONLY BY DESIGN. Every verification happens on this machine against
locally stored material; nothing here ever touches the network:
  * TOTP  -- RFC 6238, pure stdlib (hmac/sha1). Works with any authenticator
             app; the shared secret never leaves this box except as the
             otpauth:// URI the user enrolls with.
  * Recovery codes -- 10 single-use codes, shown ONCE at enrollment, stored
             only as SHA-256 digests (high-entropy random input, so a fast
             hash is the correct choice -- stretching is for low-entropy
             human passwords).
  * FIDO2 -- YubiKeys & friends via python-fido2 (Yubico's own library),
             implementation path (b): the BACKEND talks to the authenticator
             (CTAP2), the browser API is never involved, so Electron's
             file:// secure-context limitation is bypassed entirely.
             Verification is against OUR stored public keys (fido2.server),
             no Yubico cloud, no attestation-CA fetches.

  Windows note: since Win10 1903 the OS blocks raw HID access to FIDO
  devices for non-admin processes; the sanctioned path is the platform
  WebAuthn API (webauthn.dll). python-fido2's WindowsClient wraps it and
  pops the native Windows Security touch/PIN dialog -- so we use it when
  available and fall back to raw CTAP-over-HID (Linux/macOS, or elevated
  Windows). Same storage, same verification, either way.

STORAGE: sage_data/.mfa.json (OUTSIDE the project, like .users.json),
Fernet-encrypted at rest via atrest.py when available, 0600 either way.
The TOTP secret is necessarily reversible (that's how TOTP works); the
at-rest encryption plus sage_data separation is the mitigation.

python-fido2 is OPTIONAL: everything else works without it, and the FIDO2
API surface reports cleanly when it's absent.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import threading
import time

_STORE_NAME = ".mfa.json"
_LOCK = threading.Lock()

RP_ID = "localhost"
RP_NAME = "VeridianAI"
ORIGIN = "https://localhost"  # satisfies fido2's origin check; local-only

TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1          # +/- one step of clock skew
RECOVERY_COUNT = 10
_PENDING_TTL = 300       # seconds a login's second-factor challenge stays valid

# Unambiguous alphabet for recovery codes (no 0/O/1/l/I).
_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


# --- store ---------------------------------------------------------------------

def _store_path():
    try:
        from config import DATA_DIR, BACKEND_DIR
        from secret_locator import resolve_secret_file
        return str(resolve_secret_file(_STORE_NAME, DATA_DIR, BACKEND_DIR,
                                       announce=False))
    except Exception:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), _STORE_NAME)


def _load():
    p = _store_path()
    if not os.path.exists(p):
        return {"users": {}}
    try:
        with open(p, "rb") as f:
            blob = f.read()
        try:
            import atrest
            store = atrest.load_json_auto(blob)
        except ImportError:
            store = json.loads(blob.decode("utf-8"))
        if not isinstance(store, dict) or "users" not in store:
            return {"users": {}}
        return store
    except Exception:
        return {"users": {}}


def _save(store):
    p = _store_path()
    try:
        import atrest
        blob = atrest.dump_json_encrypted(store)
    except Exception:
        blob = json.dumps(store, indent=2).encode("utf-8")
    tmp = p + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def _user(store, username):
    return store["users"].setdefault((username or "").strip().lower(), {})


# --- TOTP (RFC 6238, stdlib) ---------------------------------------------------

def _b32_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _hotp(secret_b32, counter):
    pad = "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def _totp_match(secret_b32, code, last_counter):
    """Return the matched counter (int) or None. Rejects any counter <=
    last_counter -- a TOTP code is single-use here (replay protection)."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return None
    now = int(time.time()) // TOTP_STEP
    for delta in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        c = now + delta
        if c <= int(last_counter or 0):
            continue
        if hmac.compare_digest(_hotp(secret_b32, c), code):
            return c
    return None


def totp_begin(username):
    """Start TOTP enrollment: mint a secret, store it as PENDING (not yet
    trusted), hand back the secret + otpauth URI for the authenticator app."""
    secret = _b32_secret()
    with _LOCK:
        store = _load()
        _user(store, username)["pending_totp"] = {"secret": secret,
                                                  "created": int(time.time())}
        _save(store)
    label = "%s:%s" % (RP_NAME, username)
    uri = ("otpauth://totp/%s?secret=%s&issuer=%s&digits=%d&period=%d"
           % (label.replace(" ", "%20"), secret, RP_NAME, TOTP_DIGITS, TOTP_STEP))
    return {"secret": secret, "otpauth": uri}


def totp_confirm(username, code):
    """Finish enrollment: the user proves the app has the secret by echoing a
    valid code. Only then does pending become enabled. Returns recovery codes
    if this enrollment just minted them (first MFA method for the account)."""
    with _LOCK:
        store = _load()
        u = _user(store, username)
        pend = u.get("pending_totp")
        if not pend:
            return {"success": False, "error": "no enrollment in progress"}
        c = _totp_match(pend["secret"], code, 0)
        if c is None:
            return {"success": False, "error": "that code didn't match -- check "
                    "the app and try the next code"}
        u["totp"] = {"secret": pend["secret"], "enabled": True,
                     "last_counter": c, "created": int(time.time())}
        u.pop("pending_totp", None)
        codes = _ensure_recovery(u)
        _save(store)
    return {"success": True, "recovery_codes": codes}


def totp_disable(username):
    with _LOCK:
        store = _load()
        u = _user(store, username)
        u.pop("totp", None)
        u.pop("pending_totp", None)
        _maybe_clear_recovery(u)
        _save(store)
    return {"success": True}


def verify_totp(username, code):
    with _LOCK:
        store = _load()
        u = _user(store, username)
        t = u.get("totp")
        if not (t and t.get("enabled")):
            return False
        c = _totp_match(t["secret"], code, t.get("last_counter", 0))
        if c is None:
            return False
        t["last_counter"] = c   # burn it: same code can't be replayed
        _save(store)
        return True


# --- recovery codes ------------------------------------------------------------

def _new_code():
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))
    return raw[:5] + "-" + raw[5:]


def _hash_code(code):
    """Normalize forgivingly: users paste codes from notes that carry the
    display numbering ('3. abcde-fghij', '10) ...'). Strip a leading
    number+punctuation prefix FIRST (safe: real codes never contain . ) or :),
    then case/separator-fold. A stressed 2am user should never be locked out
    by formatting."""
    s = (code or "").strip()
    s = re.sub(r"^\s*\d{1,2}\s*[.):]\s*", "", s)
    norm = s.lower().replace("-", "").replace(" ", "")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _ensure_recovery(u):
    """Mint recovery codes if the account has none. Returns the PLAINTEXT list
    (caller shows it once) or None if codes already existed."""
    if u.get("recovery"):
        return None
    codes = [_new_code() for _ in range(RECOVERY_COUNT)]
    u["recovery"] = [_hash_code(c) for c in codes]
    return codes


def _maybe_clear_recovery(u):
    """Recovery codes exist to escape MFA lockout; with no MFA methods left
    they are meaningless, so drop them (a fresh enrollment mints fresh ones)."""
    if not (u.get("totp", {}).get("enabled") or u.get("fido2")):
        u.pop("recovery", None)


def verify_recovery(username, code):
    """Constant-time check against the stored digests; a hit CONSUMES the code."""
    h = _hash_code(code)
    with _LOCK:
        store = _load()
        u = _user(store, username)
        hit = None
        for stored in u.get("recovery", []):
            if hmac.compare_digest(stored, h):   # scan all: constant-ish time
                hit = stored
        if hit is None:
            return False
        u["recovery"] = [x for x in u["recovery"] if x != hit]
        _save(store)
        return True


def regenerate_recovery(username):
    with _LOCK:
        store = _load()
        u = _user(store, username)
        if not (u.get("totp", {}).get("enabled") or u.get("fido2")):
            return {"success": False, "error": "enable an MFA method first"}
        codes = [_new_code() for _ in range(RECOVERY_COUNT)]
        u["recovery"] = [_hash_code(c) for c in codes]
        _save(store)
    return {"success": True, "recovery_codes": codes}


# --- FIDO2 / passkeys (python-fido2, backend-owned CTAP path) ------------------

def fido2_available():
    try:
        import fido2  # noqa: F401
        return True
    except Exception:
        return False


def _fido2_server():
    from fido2.server import Fido2Server
    from fido2.webauthn import PublicKeyCredentialRpEntity
    # verify_origin: we ARE both ends of this conversation (backend-owned
    # client on the same box), so the browser-style origin check is moot.
    return Fido2Server(PublicKeyCredentialRpEntity(id=RP_ID, name=RP_NAME),
                       verify_origin=lambda origin: True)


def _get_client(pin=None):
    """Best client for this platform. Windows non-admin -> WindowsClient
    (native security dialog handles touch/PIN). Otherwise raw CTAP over HID.

    VERSION NOTE (the 2026-07-19 'Add key fails instantly' bug): python-fido2
    2.x moved WindowsClient to fido2.client.windows and reworked client
    construction around ClientDataCollector. The old 1.x-style import failed
    silently, we fell through to raw HID, and Windows blocks raw HID for
    FIDO devices in non-admin processes -> instant 'no key found'. Both
    import paths and both constructor shapes are handled below."""
    # 2.x collector (origin verification); absent on 1.x
    cdc = None
    try:
        from fido2.client import DefaultClientDataCollector
        cdc = DefaultClientDataCollector(ORIGIN)
    except ImportError:
        pass
    # WindowsClient: fido2 2.x home first, then the 1.x home
    WindowsClient = None
    try:
        from fido2.client.windows import WindowsClient  # fido2 >= 2.0
    except Exception:
        try:
            from fido2.client import WindowsClient      # fido2 1.x
        except Exception:
            WindowsClient = None
    if WindowsClient is not None:
        try:
            if WindowsClient.is_available():
                try:
                    return WindowsClient(cdc)           # 2.x: (client_data_collector)
                except TypeError:
                    return WindowsClient(ORIGIN)        # 1.x: (origin)
        except Exception:
            pass  # fall through to raw CTAP (e.g. elevated process)
    from fido2.client import Fido2Client, UserInteraction
    from fido2.hid import CtapHidDevice
    dev = next(CtapHidDevice.list_devices(), None)
    if dev is None:
        raise RuntimeError("No FIDO2 security key found. Plug the key in and "
                           "try again.")

    class _UI(UserInteraction):
        def prompt_up(self):
            pass  # frontend already shows "touch your key"

        def request_pin(self, permissions, rp_id):
            if pin:
                return pin
            raise RuntimeError("This security key requires its PIN. Enter the "
                               "PIN and try again.")

        def request_uv(self, permissions, rp_id):
            return True

    try:
        return Fido2Client(dev, cdc, user_interaction=_UI())   # 2.x
    except TypeError:
        return Fido2Client(dev, ORIGIN, user_interaction=_UI())  # 1.x


def _user_credentials(u):
    from fido2.webauthn import AttestedCredentialData
    creds = []
    for c in u.get("fido2", []):
        try:
            creds.append(AttestedCredentialData(base64.b64decode(c["data"])))
        except Exception:
            continue
    return creds


def fido2_register(username, label=None, pin=None):
    """BLOCKING (waits for the touch) -- callers run this in a worker thread.
    Registers a new credential and stores its public key locally."""
    if not fido2_available():
        return {"success": False, "error": "python-fido2 is not installed "
                "(pip install fido2)"}
    from fido2.webauthn import PublicKeyCredentialUserEntity
    with _LOCK:
        store = _load()
        existing = _user_credentials(_user(store, username))
    server = _fido2_server()
    user_entity = PublicKeyCredentialUserEntity(
        id=hashlib.sha256(("veridian:" + username.lower()).encode()).digest()[:16],
        name=username, display_name=username)
    options, state = server.register_begin(
        user_entity, existing, user_verification="discouraged")
    # fido2 2.x: CredentialCreationOptions dataclass (.public_key);
    # fido2 1.x: mapping (["publicKey"]).
    pk_options = getattr(options, "public_key", None)
    if pk_options is None:
        pk_options = options["publicKey"]
    try:
        client = _get_client(pin)
        result = client.make_credential(pk_options)
    except Exception as e:
        return {"success": False, "error": _friendly_fido_error(e)}
    try:
        # fido2 2.x: register_complete(state, RegistrationResponse)
        auth_data = server.register_complete(state, result)
    except (TypeError, AttributeError):
        # fido2 1.x: register_complete(state, client_data, attestation_object)
        auth_data = server.register_complete(
            state, result.client_data, result.attestation_object)
    cred = auth_data.credential_data
    entry = {"data": base64.b64encode(bytes(cred)).decode("ascii"),
             "id": base64.urlsafe_b64encode(cred.credential_id).decode("ascii"),
             "label": (label or "").strip() or "Security key",
             "sign_count": 0, "created": int(time.time())}
    with _LOCK:
        store = _load()
        u = _user(store, username)
        u.setdefault("fido2", []).append(entry)
        codes = _ensure_recovery(u)
        _save(store)
    return {"success": True, "label": entry["label"], "id": entry["id"],
            "recovery_codes": codes}


def fido2_authenticate(username, pin=None):
    """BLOCKING (waits for the touch) -- callers run this in a worker thread.
    Local assertion against OUR stored public keys; no network, no cloud."""
    if not fido2_available():
        return {"success": False, "error": "python-fido2 is not installed"}
    with _LOCK:
        store = _load()
        u = _user(store, username)
        creds = _user_credentials(u)
    if not creds:
        return {"success": False, "error": "no security key is enrolled for "
                "this account"}
    server = _fido2_server()
    options, state = server.authenticate_begin(
        creds, user_verification="discouraged")
    pk_options = getattr(options, "public_key", None)
    if pk_options is None:
        pk_options = options["publicKey"]
    try:
        client = _get_client(pin)
        selection = client.get_assertion(pk_options)
        result = selection.get_response(0)
    except Exception as e:
        return {"success": False, "error": _friendly_fido_error(e)}
    try:
        try:
            # fido2 2.x: authenticate_complete(state, creds, AuthenticationResponse)
            server.authenticate_complete(state, creds, result)
        except (TypeError, AttributeError):
            # fido2 1.x: exploded-arguments form
            server.authenticate_complete(state, creds, result.credential_id,
                                         result.client_data,
                                         result.authenticator_data,
                                         result.signature)
    except Exception as e:
        return {"success": False, "error": "assertion did not verify: %s" % e}
    # bump the matching credential's sign counter (clone detection signal)
    try:
        raw_id = getattr(result, "raw_id", None) or getattr(result, "credential_id", b"")
        cid = base64.urlsafe_b64encode(raw_id).decode("ascii")
        with _LOCK:
            store = _load()
            for c in _user(store, username).get("fido2", []):
                if c.get("id") == cid:
                    c["sign_count"] = int(c.get("sign_count", 0)) + 1
            _save(store)
    except Exception:
        pass
    return {"success": True}


def fido2_remove(username, cred_id):
    with _LOCK:
        store = _load()
        u = _user(store, username)
        before = len(u.get("fido2", []))
        u["fido2"] = [c for c in u.get("fido2", []) if c.get("id") != cred_id]
        removed = len(u["fido2"]) < before
        _maybe_clear_recovery(u)
        _save(store)
    return {"success": removed,
            "error": None if removed else "no such credential"}


def _friendly_fido_error(e):
    s = str(e) or e.__class__.__name__
    low = s.lower()
    if "pin" in low:
        return "The security key wants its PIN: " + s
    if "timeout" in low or "timed out" in low:
        return "Timed out waiting for a touch. Try again and tap the key."
    if "cancel" in low or "denied" in low:
        return "The request was cancelled."
    return "Security key error: " + s


# --- status / admin ------------------------------------------------------------

def status(username):
    with _LOCK:
        store = _load()
        u = _user(store, username)
        return {
            "totp_enabled": bool(u.get("totp", {}).get("enabled")),
            "fido2_keys": [{"id": c.get("id"), "label": c.get("label"),
                            "created": c.get("created")}
                           for c in u.get("fido2", [])],
            "fido2_available": fido2_available(),
            "recovery_remaining": len(u.get("recovery", [])),
        }


def enabled_methods(username):
    s = status(username)
    m = []
    if s["totp_enabled"]:
        m.append("totp")
    if s["fido2_keys"]:
        m.append("fido2")
    if m and s["recovery_remaining"] > 0:
        m.append("recovery")
    return m


def reset_user(username):
    """Owner-driven full MFA reset (lost key / lost phone). Clears every
    method AND the recovery codes; the account falls back to password-only
    until the user re-enrolls."""
    with _LOCK:
        store = _load()
        key = (username or "").strip().lower()
        existed = key in store["users"]
        store["users"].pop(key, None)
        _save(store)
    return {"success": True, "had_mfa": existed}


# --- pending second-factor challenges (login flow) -----------------------------
# Password verified -> mint a short-lived one-use token the frontend echoes
# back with the second factor. In-memory on purpose (same stance as sessions).

_PENDING = {}
_PLOCK = threading.Lock()


def begin_challenge(user, must_change=False, ttl=None):
    """`user` is the verified dict from users.verify_user ({username, ns,
    is_owner}); it rides the challenge so the session can be minted later
    without re-touching the password. `ttl` preserves any access-controls
    session cap across the second-factor hop."""
    token = secrets.token_urlsafe(32)
    with _PLOCK:
        now = time.time()
        for t in [t for t, r in _PENDING.items() if r["expires"] < now]:
            _PENDING.pop(t, None)
        _PENDING[token] = {"username": user.get("username"), "user": dict(user),
                           "must_change": bool(must_change), "ttl": ttl,
                           "expires": now + _PENDING_TTL}
    return token


def peek_challenge(token):
    with _PLOCK:
        r = _PENDING.get(token or "")
        if not r or r["expires"] < time.time():
            _PENDING.pop(token, None)
            return None
        return dict(r)


def consume_challenge(token):
    """One-shot: returns the record and burns the token (call ONLY on a
    successful second-factor verification)."""
    with _PLOCK:
        r = _PENDING.pop(token or "", None)
        if not r or r["expires"] < time.time():
            return None
        return r
