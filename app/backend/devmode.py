"""Developer Mode — show VeridianAI's background console windows (Windows).

ARM, RESTART, RUN. NOT A LIVE SWITCH. (v2.16.2)

This file used to claim "the toggle works live AND survives respawns". The
second half was true. The first was not, and had never been -- start.bat has
said "Restart-to-apply" on line 456 the whole time. Todd established the real
behaviour by testing it into the ground:

    "Actually quitting VeridianAI was always required, whether signing out
     took place or not."

WHY LIVE SHOWING CANNOT WORK, EVER

A console spawned with CREATE_NO_WINDOW has no window. Not a hidden one -- none
at all. ShowWindow cannot reveal what was never created, so turning Developer
Mode ON can only ever affect consoles spawned AFTER the decision. The visibility
choice is made once per process, at spawn, by console_creationflags().

That is why this is now modelled as ARMING rather than switching:

    1. You toggle it on and confirm. That instant stamps a deadline
       five minutes into the future, by the CLOCK.
    2. You fully quit VeridianAI and start it again before that deadline.
       tier_launcher reads the deadline as it starts and spawns the consoles
       visible.
    3. They stay for that whole session. Quitting ends Developer Mode.

WHY THE DEADLINE IS AN ABSOLUTE TIME

Todd, exactly: "it has to be by actual time, not a countdown... 5 minutes
counting down while the app is open is not the same as the 5 minutes
deterministically gone from 2:10 to 2:15." A countdown owned by a running
process is meaningless here, because the whole point is that the process stops.
So a wall-clock instant is stored and every reader compares against now.

WHY IT IS PER LAUNCH AND NOT PER PROFILE

The consoles are spawned before anyone has signed in -- tier_launcher runs from
start.bat and computes visibility at import, in its own process. There is
nobody to ask yet. Binding this to a profile would require revealing windows
after sign-in, which is the thing that cannot be done. So the arm belongs to
the machine and to one launch: whoever armed it, the next start inside the
window is a Developer Mode session, and it ends when that session ends.

NO STATE CAN GO STALE

The deadline is the only thing persisted, and it expires on its own. A crash,
a power cut, a killed process -- none of them can leave Developer Mode stuck on
for the next person, because five minutes later the stored instant is simply in
the past. A graceful quit clears it immediately.

LIVE HIDING, which is a different question, does work and is kept best-effort:
see set_consoles_visible.
"""
from __future__ import annotations

import os
import time

try:
    import ui_prefs as _prefs
except Exception:  # pragma: no cover
    _prefs = None

# THE PER-PROFILE ATTEMPT, AND WHY IT WAS WITHDRAWN (v2.16.2)
# ------------------------------------------------------------
# An earlier pass this same day stored a per-profile preference here, applied
# on sign-in from the session gate. It was wrong, and the way it was wrong is
# worth keeping so nobody rebuilds it.
#
# It assumed the consoles could be made to follow whoever signed in. They
# cannot. tier_launcher spawns them from start.bat, in its own process, BEFORE
# anybody has signed in -- and it spawns them with CREATE_NO_WINDOW when
# Developer Mode is off, which does not produce a hidden window but no window
# at all. Nothing can reveal one afterwards. So a preference belonging to a
# person could be stored, and read, and reported by the API, and still never
# change a single thing on screen.
#
# Todd's testing found exactly that, in the form the code guaranteed:
#
#     "If the person before exited VeridianAI through any means, and left Dev
#      mode on, the next person gets terminals upon starting VeridianAI, even
#      if they have dev mode off."
#
# The decision is made once per LAUNCH, so that is what it is modelled as now.
# What replaced the preference is the arm window described at the top of this
# file: whoever arms it, the next start inside the window is a Developer Mode
# session, and it ends when that session ends.
# THE ONLY PERSISTED FACT: the wall-clock instant the arm lapses.
#
# Epoch seconds, in the shared ui_prefs.json because every reader is a
# different process -- tier_launcher, overseer_daemon and tier_lifecycle each
# ask at spawn time, none of them has a signed-in user, and all of them need
# the same answer.
#
# Absent or in the past means OFF, which is why nothing here can get stuck on.
_UNTIL_KEY = "developer_mode_until"

# Who armed it, for the audit trail and for the Settings line. Never consulted
# for a decision -- the deadline decides.
_BY_KEY = "developer_mode_by"

# How long you have to quit and restart. Five minutes is Todd's number and the
# reasoning is sound: it has to cover a slow machine shutting down and coming
# back up, and it is short enough that walking away disarms it for you.
DEFAULT_ARM_SECONDS = 300

# Console-window titles VeridianAI's launchers set, plus substrings of the
# Python-spawned consoles' default (command-line) titles. Matched lowercased.
#
# v2.16.2: "veridianai" was MISSING. The 2026-08-14 rename sweep (Sage -> Toga,
# OracleAI -> VeridianAI) rewrote product names across the tree and did not
# reach this tuple, so the main "VeridianAI v2.16" console matched nothing and
# was never hidden. Todd found it by counting: "the live hide/show only
# affected the 3 llama terminals, not the other 5". Those three are the only
# entries here that survived the rename intact.
#
# The old name stays alongside the new one. Installs that predate the rename
# still have consoles titled OracleAI, and dropping it would un-hide them.
_TITLE_HINTS = (
    "veridianai", "oracleai", "ollama-oracle", "ollama", "llama-sage",
    "llama-daemon", "sage-daemon", "toga-daemon", "overseer", "bitchat",
    "llama-server", "tier_launcher", "sage_daemon",
)


def _now() -> float:
    return time.time()


def armed_until() -> float:
    """The stored deadline as epoch seconds, or 0.0 if there is none."""
    if _prefs is None:
        return 0.0
    try:
        return float(_prefs.get(_UNTIL_KEY, 0) or 0)
    except Exception:
        return 0.0


def is_enabled() -> bool:
    """Should a console spawned RIGHT NOW be visible?

    This is the question every launcher asks, and the only one they need. True
    while the arm is live; False the instant it lapses. tier_launcher,
    overseer_daemon and tier_lifecycle keep calling it exactly as before.
    """
    return armed_until() > _now()


def seconds_left() -> int:
    return max(0, int(armed_until() - _now()))


def arm(seconds: int = DEFAULT_ARM_SECONDS, by=None) -> dict:
    """Start the window. Returns the deadline so the UI can show a real time.

    Stamped from the clock at the moment of the call -- which is the moment the
    person clicked OK -- so it keeps running while VeridianAI is shut down.
    That is the whole point: the app is not running for most of this window.
    """
    until = _now() + max(1, int(seconds))
    if _prefs is not None:
        try:
            _prefs.set(_UNTIL_KEY, until)
            _prefs.set(_BY_KEY, str(by or ""))
        except Exception:
            pass
    return {"until": until, "seconds_left": seconds_left(),
            "armed": is_enabled()}


def disarm() -> dict:
    """Cancel the window. Consoles already open are not closed by this -- see
    set_consoles_visible, which is attempted separately and best-effort."""
    if _prefs is not None:
        try:
            _prefs.set(_UNTIL_KEY, 0)
            _prefs.set(_BY_KEY, "")
        except Exception:
            pass
    return {"until": 0, "seconds_left": 0, "armed": False}


def armed_by() -> str:
    if _prefs is None:
        return ""
    try:
        return str(_prefs.get(_BY_KEY, "") or "")
    except Exception:
        return ""


def status(session_active: bool = False) -> dict:
    """Everything Settings needs to describe the true state in one call.

    `active` and `armed` are DIFFERENT facts and the UI must not merge them:
    armed means "the next start will show terminals", active means "this
    session's terminals are already up". Showing one switch and calling it both
    is how this feature got its reputation.
    """
    _until = armed_until()
    return {
        "active": bool(session_active),
        "armed": _until > _now(),
        "until": _until or None,
        "seconds_left": seconds_left(),
        "by": armed_by(),
        "arm_seconds": DEFAULT_ARM_SECONDS,
    }


def set_enabled(enabled: bool, by=None) -> dict:
    """Arm or disarm. Kept under the old name so existing callers still read.

    There is no "enabled" to set any more, and that is the correction: this
    never was a switch that took effect where it was thrown. Turning it on
    starts the window; turning it off ends it.
    """
    return arm(by=by) if enabled else disarm()


def console_creationflags() -> int:
    """creationflags for spawning a child console: a visible NEW console when
    dev mode is on, an invisible (windowless) one when off. 0 off-Windows.

    Read once per spawning process, at spawn. This is the ONLY moment the
    choice can be made -- CREATE_NO_WINDOW does not produce a hidden window,
    it produces no window, and nothing can reveal it afterwards.
    """
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
            _titled = bool(title.strip()) and any(h in title for h in _TITLE_HINTS)
            # HIDING and SHOWING are not symmetrical, and treating them as one
            # rule is what limited this to three of the eight consoles.
            #
            # SHOW: title match only. Matching by PID would also "reveal"
            # deliberately-windowless processes (ComfyUI, the browser IPC) as
            # blank terminals -- the bug this rule was written for.
            #
            # HIDE: our own process tree counts too. Todd: "the live hide/show
            # only affected the 3 llama terminals, not the other 5" -- Ollama
            # and the three python.exe consoles carry their default
            # command-line titles and match no hint anybody could write down.
            # Hiding is safe where showing is not: hiding a window that is
            # already invisible, or that a process never had, does nothing at
            # all. There is no equivalent of an accidentally-revealed blank
            # terminal in this direction.
            if _titled or (not visible and pid.value in our_pids):
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


def begin_launch() -> dict:
    """Called once by the backend as it starts. Reports whether THIS launch is
    a Developer Mode session.

    IT DOES NOT CONSUME THE ARM, and that is deliberate. start.bat runs
    tier_launcher at line 470, in a separate process that reads the deadline as
    it imports; the backend comes up after the readiness probes. Clearing the
    arm here would be safe under that ordering and catastrophic under any other
    -- the consoles would spawn hidden for the one launch the person went to
    the trouble of arming. A startup that depends on winning a race it does not
    control is a bug waiting for a slower machine.

    So the arm is cleared when the session ENDS, which is also exactly what was
    asked for: "expires when they quit". If the app is killed instead, the
    deadline lapses on its own within five minutes.
    """
    return {"active": is_enabled()}


def end_launch(arm_placed_this_session: bool = False) -> dict:
    """Called as the backend shuts down. Ends the Developer Mode session.

    The guard is load-bearing. Somebody who arms Developer Mode and then quits
    -- which is the entire prescribed flow -- must NOT have that arm cleared on
    the way out, or the restart they were told to perform would come up with no
    terminals and the feature would look broken to the one person using it
    correctly. So: clear the window only when this session was ENDING one, not
    when it was STARTING one.
    """
    if arm_placed_this_session:
        return {"cleared": False, "reason": "an arm was placed this session"}
    disarm()
    return {"cleared": True}


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
