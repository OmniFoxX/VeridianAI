#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_confined_runner.py -- what the IDE's Run button can and cannot reach.

THE WORD THIS IS NOT

Not a sandbox. On Windows, without shipping a container or a VM, there is no
true sandbox to be had, and claiming one is how somebody spends trust they
should have kept. This is a CONFINED RUNNER: it removes the capabilities that
turn a mistake into an incident, and it is honest about the ones it leaves.

The assertions below are in two halves on purpose. The first half is what it
STOPS. The second half is what it does NOT stop -- asserted just as firmly,
because a documented gap that someone can look up beats a guard that half works
and a comment that oversells it. If a future change closes one of those gaps,
that test SHOULD fail, and the fix is to move the assertion, not delete it.

THE BUG THIS FILE EXISTS TO KEEP FIXED

The scratch directory lives INSIDE the data folder on a real install --
user_data_dir puts a profile's files under DATA_DIR. The deny guard therefore
had to learn an exception for it, or the runner could not write its own temp
file. A dev VM where those two paths happened to diverge hid that completely.
So section 1 asserts the relationship rather than trusting the layout.
"""
import io
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import sage_engine as se

_fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n            -> " + str(detail)) if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def run(code, timeout=25):
    return se.execute_python_confined(code, timeout=timeout)


def refused(out):
    """The child raised, and the message is the confined runner's own."""
    return "Traceback" in out and ("confined runner" in out
                                   or "data folder" in out)


print("\n=== 1. The scratch dir and the deny roots must not fight ===")
_wd = os.path.realpath(str(se._confine_workdir(None)))
_roots = se._confine_deny_roots()
_inside = [r for r in _roots if _wd == r or _wd.startswith(r + os.sep)]
ok("there is at least one deny root", len(_roots) >= 1,
   "with none, the data-folder guard does nothing")
ok("the runner can write its own scratch file",
   run('open("probe.txt","w").write("x"); print(open("probe.txt").read())'
       ).strip() == "x",
   "if the scratch dir sits inside a deny root, the exception is missing")
if _inside:
    print("        (scratch dir IS inside a deny root here -- the exception "
          "is doing real work)")

print("\n=== 2. Ordinary work still works ===")
ok("arithmetic", run("print(2+2)").strip() == "4")
ok("the standard library", run(
    "import math, json, statistics\n"
    "print(json.dumps({'m': round(math.pi, 2), 'x': statistics.mean([1,2,3])}))"
   ).strip() == '{"m": 3.14, "x": 2}')
ok("files in its own directory", run(
    'open("d.csv","w").write("a,b\\n1,2\\n")\n'
    'import csv\n'
    'print(list(csv.reader(open("d.csv")))[1])').strip() == "['1', '2']")
ok("a traceback still reaches the person",
   "ZeroDivisionError" in run("print(1/0)"))
ok("stdout and stderr are both labelled",
   "[STDERR]" in run("import sys; print('out'); print('err', file=sys.stderr)"))

print("\n=== 3. What it STOPS ===")
ok("socket()", refused(run("import socket; socket.socket()")))
ok("socket.create_connection",
   refused(run('import socket; socket.create_connection(("1.1.1.1",80))')))
ok("urllib (rides on socket)",
   refused(run('import urllib.request as u; u.urlopen("http://example.com")')))
ok("DNS lookup",
   refused(run('import socket; socket.getaddrinfo("example.com", 80)')))
ok("subprocess.run", refused(run('import subprocess; subprocess.run(["echo"])')))
ok("subprocess.Popen", refused(run('import subprocess; subprocess.Popen(["echo"])')))
ok("os.system", refused(run('import os; os.system("echo x")')))
ok("os.popen", refused(run('import os; os.popen("echo x")')))

_deny = _roots[0] if _roots else None
if _deny:
    ok("opening a file in the data folder",
       refused(run('print(open(r"%s").read())' % os.path.join(_deny, ".atrest_key"))))
    ok("LISTING the data folder",
       refused(run('import os; print(os.listdir(r"%s"))' % _deny)),
       "opening was blocked but listing was not -- knowing a signing key "
       "exists and what it is called is worth something on its own")
    ok("globbing it via pathlib",
       refused(run('from pathlib import Path; print(list(Path(r"%s").iterdir()))'
                   % _deny)),
       "iterdir goes through os.scandir")

print("\n=== 4. What it does NOT stop -- asserted, not assumed ===")
# A file this test wrote itself, outside the data folder, with a string that
# can only have come from reading it. Asserting against sage_engine.py's own
# first 200 characters looked equivalent and was not: it broke the day the
# module's opening docstring grew, and reported "the confinement changed" when
# nothing about the confinement had.
_tmp = tempfile.NamedTemporaryFile(
    mode="w", suffix=".txt", delete=False, encoding="utf-8")
_tmp.write("ordinary-file-canary-8f21")
_tmp.close()
_own = _tmp.name.replace("\\", "\\\\")
try:
    ok("reading an ordinary file by absolute path is ALLOWED",
       "ordinary-file-canary-8f21"
       in run('print(open(r"%s").read())' % _own),
       "a general allow-list over open() breaks the import machinery; this is "
       "a documented gap, not an oversight. If this ever starts failing, the "
       "confinement grew -- move this assertion, do not delete it")
finally:
    try:
        os.unlink(_tmp.name)
    except OSError:
        pass
ok("the environment is minimal, not scrubbed to nothing",
   "PATH" in run("import os; print(sorted(os.environ))"),
   "PATH is needed for the interpreter to work at all")

print("\n=== 5. It is still bounded in time ===")
t0 = time.time()
out = run("import time\nwhile True:\n    time.sleep(0.1)", timeout=2)
ok("a runaway is stopped", "[TIMEOUT]" in out, out[:80])
ok("...promptly", time.time() - t0 < 20)
ok("an over-long timeout is refused, not clamped",
   "[EXECUTION ERROR]" in run("print(1)", timeout=99999))

print("\n=== 6. The confined path is SEPARATE from the [CODE:] path ===")
# This check used to end in "or True", which made it a label rather than a
# test. What it is actually for: the [CODE:] path must still be able to do
# the thing the confined path refuses.
_unconf = se.execute_python("import socket; s = socket.socket(); "
                            "print('made', s.fileno() >= 0); s.close()")
ok("execute_python still exists and is unconfined",
   "made True" in _unconf and "confined runner" not in _unconf,
   _unconf[:160])
ok("the two are different functions",
   se.execute_python is not se.execute_python_confined,
   "[CODE:] must keep working exactly as it does today")
_src = open(os.path.join(_HERE, "sage_engine.py"), encoding="utf-8").read()
ok("the confined runner does not hand over BASE_DIR / DOWNLOADS_DIR",
   "DOWNLOADS_DIR = r" not in _src.split("_CONFINE_PREAMBLE")[1].split(
       'def _confine_workdir')[0],
   "signposting the app tree to a confined run works against the point")
ok("the code never calls it a sandbox in user-visible text",
   "not a sandbox" in _src.lower() and "NOT a sandbox" in _src,
   "the whole point is that the word is not used for this")

print("\n=== 7. The preamble is a raw triple-quoted string ===")
# Both of these are regressions that have already happened once each.
# Spelled with chr() on purpose: writing the quotes literally in this file
# is the same trap the first check is here to catch.
_q3d = chr(34) * 3
_q3s = chr(39) * 3
ok("no nested triple quote inside the preamble",
   _q3d not in se._CONFINE_PREAMBLE and _q3s not in se._CONFINE_PREAMBLE,
   "a docstring written inside the preamble closes the enclosing raw "
   "string early and turns the rest of the module into a SyntaxError -- "
   "use a # comment in there instead")
ok("the guards are callable OBJECTS, not plain functions",
   "class _Blocked" in se._CONFINE_PREAMBLE
   and "class _Guarded" in se._CONFINE_PREAMBLE
   and "class _NoSocket" in se._CONFINE_PREAMBLE,
   "a plain function assigned to os.listdir becomes a descriptor and binds "
   "self, which broke Path('.').iterdir() in the runner's own directory; "
   "socket.socket must stay a class because ssl.py subclasses it")

print("\n=== 8. Abandoned run scripts do not pile up ===")
# Each run unlinks its own temp script in a finally block, and that unlink is
# deliberately swallowed on failure -- tidying up must not turn a good run into
# an error. The consequence is silent, unbounded growth in the person's data
# folder anywhere the delete does not succeed: a locked file, an antivirus
# holding it open, a mount that refuses unlink. Found by counting: 122 of them
# in one tree.
_wd = se._confine_workdir(None)
_old = os.path.join(str(_wd), "tmp_stale_probe_%d.py" % os.getpid())
io.open(_old, "w", encoding="utf-8").write("# abandoned\n")
os.utime(_old, (time.time() - 999999, time.time() - 999999))
_fresh = os.path.join(str(_wd), "tmp_fresh_probe_%d.py" % os.getpid())
io.open(_fresh, "w", encoding="utf-8").write("# just written\n")

# Can this environment delete at all? If not, the sweep cannot possibly work
# and reporting that as a failure would be blaming the code for the mount --
# which is exactly the confusion this project keeps having to unpick. Said out
# loud rather than passed silently: a skip nobody can see is worse than a red
# line, because it looks like coverage.
_probe = os.path.join(str(_wd), "tmp_unlink_probe_%d.py" % os.getpid())
io.open(_probe, "w", encoding="utf-8").write("# probe\n")
try:
    os.unlink(_probe)
    _can_delete = True
except OSError as _e:
    _can_delete = False
    _why_no_delete = "%s: %s" % (type(_e).__name__, _e)

se.execute_python_confined("print('sweep')", timeout=30)

if _can_delete:
    ok("an abandoned script is swept", not os.path.exists(_old),
       "otherwise the data folder grows forever on any machine that cannot "
       "unlink, and nobody finds out until it is measured in gigabytes")
else:
    ok("an abandoned script is swept (SKIPPED: this data folder refuses "
       "unlink -- %s)" % _why_no_delete, True,
       "the sweep is the mitigation for exactly this condition and cannot "
       "run under it; the check is real wherever deletion works")
ok("a FRESH one is left alone", os.path.exists(_fresh),
   "the age cut is derived from CODE_EXEC_TIMEOUT_MAX, so it can never "
   "reach a run that is still going")
ok("the cutoff is beyond the longest legal run",
   "CODE_EXEC_TIMEOUT_MAX + 300" in se._sweep_confine_workdir.__doc__
   or "CODE_EXEC_TIMEOUT_MAX + 300" in io.open(
       os.path.join(_HERE, "sage_engine.py"), encoding="utf-8").read(),
   "a hand-picked number here would eventually delete somebody's live run")
ok("the sweep never raises, whatever it finds",
   se._sweep_confine_workdir("/definitely/not/a/directory") is None)
for _p in (_old, _fresh):
    try:
        os.unlink(_p)
    except OSError:
        pass

print("")
if _fails:
    print("%d CHECK(S) FAILED" % len(_fails))
    for f in _fails:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED - confined runner")
sys.exit(0)
