#!/usr/bin/env python3
"""Unit tests for comfyui_launcher.py -- the OWNED-process ComfyUI autostart.

Covers the safety-critical behaviors: OFF by default, never double-launch,
resolve precedence (explicit wins), owned-only reaping, and graceful no-ops.
Pure-stdlib; monkeypatches the module's own functions so no real process,
socket, PowerShell, or filesystem probe is touched. Run: python3 test_comfyui_launcher.py
"""
import comfyui_launcher as L


class Cfg(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


class FakeProc:
    def __init__(self, pid=4321):
        self.pid = pid
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self._alive = False

    def terminate(self):
        self._alive = False


_ORIG = dict(
    is_running=L.is_running, resolve_command=L.resolve_command, _spawn=L._spawn,
    _find=L._find_start_menu_comfy, _desk=L._known_desktop_paths, _port=L._known_portable,
)


def _reset():
    L._proc = None
    L._owned = False
    L._started_cmd = None
    L._atexit_armed = True  # don't actually register atexit during tests
    L.is_running = _ORIG["is_running"]
    L.resolve_command = _ORIG["resolve_command"]
    L._spawn = _ORIG["_spawn"]
    L._find_start_menu_comfy = _ORIG["_find"]
    L._known_desktop_paths = _ORIG["_desk"]
    L._known_portable = _ORIG["_port"]


def test_disabled_never_launches():
    _reset()
    calls = []
    L._spawn = lambda cmd: (calls.append(cmd), FakeProc())[1]
    r = L.start(Cfg(comfyui_autostart_enabled=False))
    assert r["launched"] is False and "disabled" in r["reason"], r
    assert calls == [], "must not spawn when autostart is disabled"
    assert L.owns_process() is False


def test_already_running_skips_spawn():
    _reset()
    L.is_running = lambda *a, **k: True
    calls = []
    L._spawn = lambda cmd: (calls.append(cmd), FakeProc())[1]
    r = L.start(Cfg(comfyui_autostart_enabled=True))
    assert r["launched"] is False and "already running" in r["reason"], r
    assert calls == [], "must not spawn a second ComfyUI"
    assert r.get("owned") is False, "must not claim a process we did not start"


def test_resolve_precedence_explicit_wins():
    _reset()
    # explicit cmd should win and be stripped, without consulting detectors
    L._find_start_menu_comfy = lambda: "SHOULD_NOT_BE_USED"
    got = L.resolve_command(Cfg(comfyui_launch_cmd="  C:/my/ComfyUI.exe  "))
    assert got == "C:/my/ComfyUI.exe", got


def test_resolve_none_when_nothing_found():
    _reset()
    L._find_start_menu_comfy = lambda: None
    L._known_desktop_paths = lambda: None
    L._known_portable = lambda: None
    assert L.resolve_command(Cfg()) is None


def test_resolve_falls_through_to_detectors():
    _reset()
    L._find_start_menu_comfy = lambda: None
    L._known_desktop_paths = lambda: r"C:\Program Files\ComfyUI\ComfyUI.exe"
    L._known_portable = lambda: None
    assert L.resolve_command(Cfg()) == r"C:\Program Files\ComfyUI\ComfyUI.exe"


def test_launch_then_owned_stop_reaps():
    _reset()
    L.is_running = lambda *a, **k: False
    L.resolve_command = lambda cfg: "C:/my/ComfyUI.exe"
    fake = FakeProc(pid=9999)
    L._spawn = lambda cmd: fake
    r = L.start(Cfg(comfyui_autostart_enabled=True))
    assert r["launched"] is True and r["owned"] is True and r["pid"] == 9999, r
    assert L.owns_process() is True
    s = L.stop()  # on this POSIX test host stop() uses proc.terminate()
    assert s["stopped"] is True, s
    assert L.owns_process() is False, "ownership must clear after stop"
    assert fake._alive is False, "stop() must terminate the owned process"


def test_start_no_command_is_safe():
    _reset()
    L.is_running = lambda *a, **k: False
    L.resolve_command = lambda cfg: None
    r = L.start(Cfg(comfyui_autostart_enabled=True))
    assert r["launched"] is False and "not found" in r["reason"], r
    assert L.owns_process() is False


def test_stop_with_nothing_owned_is_noop():
    _reset()
    s = L.stop()
    assert s["stopped"] is False, s


def test_is_running_false_on_dead_port():
    _reset()
    # nothing listens on port 1; connect refused -> False, fast
    assert L.is_running("http://127.0.0.1:1", timeout=0.5) is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("  PASS", t.__name__)
        passed += 1
    print("\n%d/%d comfyui_launcher tests passed" % (passed, len(tests)))
