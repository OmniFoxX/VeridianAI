"""Developer Mode — hide/show VeridianAI's background console windows (Windows).

A simple on/off so normal users get a clean desktop while devs can reveal all
the log terminals. State lives in sage_data/ui_prefs.json (cross-process: the
daemons read it at spawn time). Pure stdlib (ctypes); a no-op on non-Windows.

Two layers, so the toggle works live AND survives respawns:
  - set_consoles_visible(): ShowWindow() on the consoles that are open now.
  - console_creationflags(): the launchers spawn NEW consoles visible (dev on)
    or windowless (dev off), so restarts/respawns honor the setting too.
"""
from __future__ import annotations

import os

try:
    import ui_prefs as _prefs
except Exception:  # pragma: no cover
    _prefs = None

# PER PROFILE, WITH A MACHINE-LEVEL MIRROR (v2.16.2)
# ---------------------------------------------------
# Todd: "toggling it off in a Non-Owner account does NOT actually turn it off
# if the Owner account has developer mode toggled on... I would like it per
# profile please."
#
# The obstacle was real and is worth keeping written down, because the answer
# is not to ignore it. Console windows are ONE set of windows on ONE desktop,
# and console_creationflags() is read by tier_launcher inside a daemon where
# there is no signed-in user to ask. ui_prefs' note is right: a per-user answer
# THERE is not merely wrong, it is unanswerable.
#
# So there are two values, and they are different questions:
#
#   developer_mode_pref   PER PROFILE. What this person wants. Stored under
#                         their own profile directory, untouched by anyone
#                         else's choice.
#
#   developer_mode        MACHINE. What is currently APPLIED to the desktop --
#                         the mirror the daemons read at spawn time. Always
#                         answerable, because it is a fact about the machine
#                         right now rather than a question about a person.
#
# The mirror is re-pointed at whoever is signed in: the session gate applies
# that profile's preference when they arrive, and sign-out returns it to hidden
# so one person's terminals are not left up for the next. Nothing outside this
# module changes -- console_creationflags() and every daemon still read
# developer_mode and still get a straight answer with nobody signed in.
#
# WHAT THIS HONESTLY CANNOT DO. If two profiles are signed in at once (two
# windows onto localhost) they share one desktop, and the last to sign in or
# toggle wins. No arrangement of software gives two people different views of
# the same window, and pretending otherwise would put this back where it
# started: a switch showing a state it does not have. Sequential profile use --
# how this app is actually used -- is exactly per profile.
#
# Single-user installs are unaffected: with multiuser off there is no ns, every
# read and write goes to the machine value, and the behaviour is what it was.
_KEY = "developer_mode"            # machine mirror: what is applied right now
_PREF_KEY = "developer_mode_pref"  # per profile: what this person wants

# Console-window titles VeridianAI's launchers set (start.bat) plus substrings of
# the Python-spawned consoles' default (command-line) titles. Matched lowercased.
_TITLE_HINTS = (
    "oracleai", "ollama-oracle", "llama-sage", "llama-daemon",
    "sage-daemon", "overseer", "bitchat", "llama-server",
)


def is_enabled(ns=None) -> bool:
    """True = show terminals (developer). Default False = hidden (clean UI).

    With a profile: THAT profile's own preference. Deliberately does not fall
    back to the machine value -- inheriting whatever the owner last chose is
    the precise bug this is fixing, and ui_prefs' two-scope note refuses the
    same fallback for the same reason.

    Without one (a daemon, the login screen, a single-user install): the
    machine mirror, which is what is actually applied right now.
    """
    if _prefs is None:
        return False
    try:
        if ns:
            return bool(_prefs.get(_PREF_KEY, False, ns=ns))
        return bool(_prefs.get(_KEY, False))
    except Exception:
        return False


def set_enabled(enabled: bool, ns=None) -> bool:
    """Record the choice, and point the machine mirror at it.

    Both writes, always. The preference is whose choice it is; the mirror is
    what the daemons will read when they next spawn a console, and leaving it
    behind would mean a respawn during this session reverted to the previous
    profile's answer.
    """
    enabled = bool(enabled)
    if _prefs is not None:
        try:
            if ns:
                _prefs.set(_PREF_KEY, enabled, ns=ns)
            _prefs.set(_KEY, enabled)
        except Exception:
            pass
    return enabled


def apply_for(ns=None) -> dict:
    """Make the desktop match this profile, and re-point the mirror at them.

    Called when a session arrives and when one ends. Returns the same summary
    as set_consoles_visible, plus the state it settled on.
    """
    want = is_enabled(ns)
    if _prefs is not None:
        try:
            _prefs.set(_KEY, want)      # the mirror follows whoever is here
        except Exception:
            pass
    result = set_consoles_visible(want)
    result["enabled"] = want
    return result


def console_creationflags() -> int:
    """creationflags for spawning a child console: a visible NEW console when
    dev mode is on, an invisible (windowless) one when off. 0 off-Windows."""
    import subprocess
    if os.name != "nt":
        return 0
    if is_enabled():
        return getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _our_pids() -> set:
    """PIDs in our process tree (start.bat + tiers/daemons). Best-effort."""
    pids = {os.getpid()}
    try:
        import psutil
        root = psutil.Process(os.getpid())
        for _ in range(6):
            parent = root.parent()
            if not parent:
                break
            root = parent
        pids.add(root.pid)
        for child in root.children(recursive=True):
            pids.add(child.pid)
    except Exception:
        pass
    return pids


def set_consoles_visible(visible: bool) -> dict:
    """Hide (visible=False) / show (True) our console windows live. Best-effort;
    returns a small summary. No-op + supported:False off-Windows."""
    if os.name != "nt":
        return {"supported": False, "matched": 0, "visible": bool(visible)}
    import ctypes
    from ctypes import wintypes

    # Prototype every call so 64-bit window HANDLES aren't truncated to a 32-bit
    # int — the classic ctypes bug that makes GetClassNameW / ShowWindow silently
    # operate on a bad handle (match 0 windows => nothing hides).
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL

    SW_HIDE, SW_SHOW = 0, 5
    our_pids = _our_pids()
    matched = [0]

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        try:
            cbuf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cbuf, 256)
            if cbuf.value != "ConsoleWindowClass":
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            tbuf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, tbuf, 512)
            title = (tbuf.value or "").lower()
            # Only manage consoles we can POSITIVELY identify as ours by TITLE.
            # Matching by PID alone also caught deliberately-windowless consoles
            # (CREATE_NO_WINDOW procs like ComfyUI / the browser IPC) and revealed
            # them as blank terminals. A non-empty known-tier title avoids that.
            if title.strip() and any(h in title for h in _TITLE_HINTS):
                user32.ShowWindow(hwnd, SW_SHOW if visible else SW_HIDE)
                matched[0] += 1
        except Exception:
            pass
        return True

    cb = WNDENUMPROC(_cb)  # keep a ref so the callback isn't GC'd mid-enumerate
    try:
        user32.EnumWindows(cb, 0)
    except Exception:
        pass
    return {"supported": True, "matched": matched[0], "visible": bool(visible)}


def apply_saved_state() -> dict:
    """Apply the persisted dev-mode flag to the consoles that are open now.

    No ns: at startup nobody is signed in yet. On a single-user install that
    machine value IS the owner's choice and this is exactly right. With
    multiuser on, main.py starts from hidden instead and lets the session gate
    apply the arriving profile's preference -- otherwise the last person's
    terminals would be sitting on the login screen.
    """
    return set_consoles_visible(is_enabled())


def diagnose() -> dict:
    """List the terminal-ish top-level windows we can see (class / title / pid /
    visible), so we can tell WHY a console did or didn't hide — e.g. it's hosted
    by Windows Terminal (class is NOT 'ConsoleWindowClass') or its title was
    changed by the child program. Windows-only; read-only."""
    if os.name != "nt":
        return {"supported": False, "windows": []}
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL

    our = _our_pids()
    out = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        try:
            cbuf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cbuf, 256)
            cls = cbuf.value or ""
            tbuf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, tbuf, 512)
            title = tbuf.value or ""
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            low = cls.lower()
            termish = ("console" in low) or ("cascadia" in low) or ("terminal" in low)
            hinted = any(h in title.lower() for h in _TITLE_HINTS)
            if termish or hinted or (pid.value in our):
                out.append({
                    "class": cls, "title": title, "pid": int(pid.value),
                    "visible": bool(user32.IsWindowVisible(hwnd)),
                    "in_our_tree": pid.value in our,
                    "would_match": bool(title.strip() and hinted),
                })
        except Exception:
            pass
        return True

    cb = WNDENUMPROC(_cb)
    try:
        user32.EnumWindows(cb, 0)
    except Exception:
        pass
    return {"supported": True, "our_pids": sorted(our), "count": len(out), "windows": out}
