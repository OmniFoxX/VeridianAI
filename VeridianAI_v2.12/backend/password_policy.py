"""Password policy for VeridianAI accounts (NIST SP 800-63B Rev.4 aligned).

WHAT THIS ENFORCES (the "memorized secret" rules, 2026 edition):
  * length >= 16 (config knob `password_min_length`, default 16) -- length is
    the primary strength control;
  * length <= 256 code points -- generous ceiling ("64+" per spec) that still
    bounds scrypt cost so a megabyte "password" can't be used as a CPU DoS;
  * full Unicode + spaces accepted; length counts CODE POINTS, so emoji and
    CJK passphrases are first-class citizens;
  * NO composition rules -- we never demand upper/lower/digit/symbol. NIST
    dropped these because they push people toward "Password1!" patterns;
  * NO periodic rotation -- callers only force a change on evidence of
    compromise (the login-time recheck in main.py handles legacy weak
    passwords via must_change);
  * reject list -- a local, offline blocklist of common/breached passwords
    (backend/password_blocklist.txt, optionally extended by a user-supplied
    sage_data/password_blocklist.txt), plus case-insensitive SUBSTRING checks
    for the app name and the username, plus algorithmic junk detection
    (single repeated char, short repeated patterns, alphabet/digit/keyboard
    walks) that catches the "aaaaaaaaaaaaaaaa" family no finite list can.

WHAT THIS DOES NOT DO:
  * no network calls, ever (breach checks are against the LOCAL list only --
    consistent with VeridianAI's local-first design);
  * no strength *enforcement* beyond the rules above: `estimate_strength` is
    advisory, for the non-blocking UI meter (WCAG 3.3.8: no cognitive
    gotchas, the meter never rejects).

stdlib-only so users.py can import it as early as it imports everything else.
"""
import os
import re

APP_NAME = "VeridianAI"
# Substring-reject terms. Deliberately NOT including short/common words like
# "toga" or "sage" -- they appear inside innocent passphrases ("photographer",
# "message"). Only distinctive product terms belong here.
_APP_TERMS = ("veridianai", "veridian", "oracleai")

DEFAULT_MIN_LEN = 16
MAX_LEN = 256  # "64+" per policy; bounded so scrypt input stays cheap

_BLOCKLIST_NAME = "password_blocklist.txt"
_blocklist_cache = None

# Sequences for "keyboard walk" detection: a password that is entirely a slice
# of one of these (or its reverse) is machine-guessable regardless of length.
_SEQUENCES = (
    "abcdefghijklmnopqrstuvwxyz" * 2,
    "01234567890123456789",
    "qwertyuiopasdfghjklzxcvbnm" * 2,
    "qazwsxedcrfvtgbyhnujmikolp" * 2,
    "1qaz2wsx3edc4rfv5tgb6yhn7ujm8ik9ol0p" * 2,
)


def min_length():
    """Config-driven minimum (single source of truth: config.json), default 16."""
    try:
        import config
        v = int(config.get("password_min_length", DEFAULT_MIN_LEN))
        return max(8, v)  # floor: never let a config typo turn the policy off
    except Exception:
        return DEFAULT_MIN_LEN


def _blocklist_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    paths = [os.path.join(here, _BLOCKLIST_NAME)]
    # Optional user/site extension OUTSIDE the project (survives upgrades,
    # lets a deployment drop in a bigger breached-password corpus).
    try:
        from config import DATA_DIR
        paths.append(os.path.join(str(DATA_DIR), _BLOCKLIST_NAME))
    except Exception:
        pass
    return paths


def _load_blocklist():
    global _blocklist_cache
    if _blocklist_cache is not None:
        return _blocklist_cache
    entries = set()
    for p in _blocklist_paths():
        try:
            if not os.path.exists(p):
                continue
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip().lower()
                    if line and not line.startswith("#"):
                        entries.add(line)
        except Exception:
            continue
    _blocklist_cache = entries
    return entries


def reload_blocklist():
    """Drop the cache (e.g. after the user edits the sage_data extension file)."""
    global _blocklist_cache
    _blocklist_cache = None


def _is_repeated_pattern(s):
    """True if s is one character repeated, or a short pattern repeated to fill
    the whole string ('abcabcabcabcabcabc', 'passwordpassword')."""
    n = len(s)
    if n == 0:
        return False
    if len(set(s)) == 1:
        return True
    for plen in range(1, n // 2 + 1):
        if n % plen == 0 and plen <= max(8, n // 2):
            if s == s[:plen] * (n // plen):
                return True
    return False


def _is_sequence_walk(s):
    """True if s (lowercased) is entirely a slice of a known sequence, forward
    or reversed -- alphabet runs, digit runs, keyboard rows/walks."""
    if len(s) < 6:
        return False
    for seq in _SEQUENCES:
        if s in seq or s in seq[::-1]:
            return True
    return False


def _normalize(password):
    """Lowercase + strip separators, for blocklist/pattern comparison only.
    (The HASHED password is never normalized -- what you type is what you get.)"""
    return re.sub(r"[\s\-_.]+", "", (password or "").lower())


def validate(password, username=None):
    """Validate a CANDIDATE password (create/change time -- never at verify).

    Returns {"ok": bool, "errors": [str, ...], "strength": {...}}.
    Errors are user-facing sentences; empty list means the password passes.
    """
    pw = password or ""
    errors = []
    mn = min_length()
    n = len(pw)  # code points, so Unicode passphrases count fairly

    if n < mn:
        errors.append("Password must be at least %d characters (spaces are "
                      "welcome -- a long passphrase beats a short scramble)." % mn)
    if n > MAX_LEN:
        errors.append("Password must be at most %d characters." % MAX_LEN)

    low = pw.lower()
    norm = _normalize(pw)

    if n >= mn:  # only pile on quality errors once length is plausible
        # 1) exact blocklist hit (raw lowercase OR separator-stripped form)
        bl = _load_blocklist()
        if low.strip() in bl or norm in bl:
            errors.append("That password is on the common-password reject "
                          "list. Please pick something less guessable.")
        # 2) app-name substring (case-insensitive, per policy)
        elif any(t in low for t in _APP_TERMS):
            errors.append("Password must not contain the app name.")
        # 3) username substring (case-insensitive; >=4 chars to avoid false
        #    positives on tiny usernames, but exact match rejects regardless)
        u = (username or "").strip().lower()
        if u:
            if norm == _normalize(u):
                errors.append("Password must not be your username.")
            elif len(u) >= 4 and u in low:
                errors.append("Password must not contain your username.")
        # 4) algorithmic junk: repeats and walks
        if _is_repeated_pattern(norm) or _is_repeated_pattern(low.strip()):
            errors.append("Password is a repeated pattern -- easy for a "
                          "computer to guess despite its length.")
        elif _is_sequence_walk(norm):
            errors.append("Password is a keyboard or alphabet sequence -- "
                          "easy for a computer to guess despite its length.")

    return {"ok": not errors, "errors": errors,
            "strength": estimate_strength(pw)}


def estimate_strength(password):
    """ADVISORY strength estimate for the non-blocking UI meter.

    Deliberately simple and explainable: length dominates (matching the
    policy's philosophy), variety adds a little, blocklist/pattern hits floor
    it. Returns {"score": 0..4, "label": str}. Never used to reject.
    """
    pw = password or ""
    n = len(pw)
    if n == 0:
        return {"score": 0, "label": "empty"}
    norm = _normalize(pw)
    low = pw.lower()
    if (low.strip() in _load_blocklist() or norm in _load_blocklist()
            or _is_repeated_pattern(norm) or _is_sequence_walk(norm)):
        return {"score": 0, "label": "very weak (common pattern)"}
    # length backbone
    if n < 12:
        score = 0
    elif n < 16:
        score = 1
    elif n < 20:
        score = 2
    elif n < 28:
        score = 3
    else:
        score = 4
    # small variety bonus (never required, occasionally helpful)
    classes = sum((any(c.islower() for c in pw), any(c.isupper() for c in pw),
                   any(c.isdigit() for c in pw),
                   any(not c.isalnum() for c in pw)))
    if classes >= 3 and score < 4:
        score += 1
    labels = ["very weak", "weak", "okay", "good", "excellent"]
    return {"score": score, "label": labels[score]}
