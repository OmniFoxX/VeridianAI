# test_imperium.py
# Gate suite for IMPERIUM (imperium.py). Run:  python test_imperium.py
# Follows the house pattern (test_customs.py / test_request_scheduler.py):
# plain asserts, no pytest dependency, exit 1 on first failure.

import json
import time

import imperium
from imperium import (Enforcer, Observer, SPECIFICATIONS, Invariant,
                      IMPERIUM_VERSION, PARENT_RELEASE, _extract_flags)

PASS = 0


def ok(cond, label):
    global PASS
    assert cond, f"FAIL: {label}"
    PASS += 1
    print(f"  ok {PASS:2d} - {label}")


def fresh(enforce=False):
    return Enforcer(SPECIFICATIONS,
                    {"flags": {}, "reward": 0, "imperium_enabled": True},
                    enforce=enforce)


class FakeLogger:
    def __init__(self):
        self.entries = []

    def log(self, content, temperature=0.7, token_prob=None,
            metadata=None, role="assistant"):
        self.entries.append({"content": content, "role": role,
                             "metadata": metadata})
        return "fakehash"


print("== header constants ==")
ok(IMPERIUM_VERSION == "2.0", "IMPERIUM_VERSION present and pinned")
ok(PARENT_RELEASE.startswith("2.12"), "PARENT_RELEASE tracks shipping release")

print("== layer 1: invariants ==")
ok(SPECIFICATIONS["NO_SANDBOX_BYPASS"].check({"flags": {}}),
   "empty flags satisfy NO_SANDBOX_BYPASS")
ok(not SPECIFICATIONS["NO_SANDBOX_BYPASS"].check(
    {"flags": {"--no-sandbox": True}}), "--no-sandbox flag violates")
ok(not SPECIFICATIONS["NO_SANDBOX_BYPASS"].check(
    {"flags": {"allow_no_sandbox": 1}}), "no_sandbox variant violates")
ok(SPECIFICATIONS["NO_SANDBOX_BYPASS"].check(
    {"flags": {"--no-sandbox": False}}), "falsy sandbox flag passes")
ok(not SPECIFICATIONS["POSITIVE_REWARD"].check({"reward": -1}),
   "negative reward violates")
ok(not SPECIFICATIONS["GATE_INTEGRITY"].check({"imperium_enabled": False}),
   "agent-side self-disable violates GATE_INTEGRITY")

print("== layer 2: gate, enforce mode ==")
e = fresh(enforce=True)
ok(e.gate_transition({"set": {"reward": 12}}), "compliant transition accepted")
ok(e.snapshot()["reward"] == 12, "state committed")
ok(not e.gate_transition({"set": {"flags": {"--no-sandbox": True}}}),
   "sandbox bypass BLOCKED in enforce mode")
ok(e.snapshot()["flags"] == {}, "violating state NOT committed")
ok(not e.gate_transition({"set": {"reward": -5}}), "negative reward blocked")
ok(not e.gate_transition("not a dict"), "malformed action rejected")
ok(not e.gate_transition({"bogus_op": {}}), "unknown op rejected")

print("== layer 2: gate, observe-only mode ==")
o = fresh(enforce=False)
ok(o.gate_transition({"set": {"flags": {"--no-sandbox": True}}}),
   "observe-only: violating transition passes through")
ok(o.snapshot()["flags"].get("--no-sandbox") is True,
   "observe-only: shadow state mirrors reality")
viol = [x for x in o.log_chain if x["type"] == "violation"]
ok(len(viol) == 1 and viol[0]["data"]["enforced"] is False,
   "violation logged with enforced=False")

print("== v2 fix: timestamps are clocks, not thread ids ==")
d = viol[0]["data"]
ok(isinstance(d["monotonic"], float) and "wall" in d,
   "monotonic + wall timestamps present")
ok(abs(d["monotonic"] - time.monotonic()) < 60,
   "monotonic value is from time.monotonic(), not get_ident()")

print("== v2 fix: set vs merge are genuinely different ==")
m = fresh()
m.gate_transition({"set": {"nested": {"a": 1, "b": 2}}})
m.gate_transition({"merge": {"nested": {"b": 99, "c": 3}}})
ok(m.snapshot()["nested"] == {"a": 1, "b": 99, "c": 3},
   "merge deep-merges nested dicts")
m.gate_transition({"set": {"nested": {"only": True}}})
ok(m.snapshot()["nested"] == {"only": True},
   "set replaces the key wholesale")
m.gate_transition({"del": ["nested"]})
ok("nested" not in m.snapshot(), "del removes keys")

print("== hash chain: tamper evidence ==")
ok(m.verify_chain(), "untouched chain verifies")
m.log_chain[1]["data"] = {"forged": True}
ok(not m.verify_chain(), "tampered entry breaks verification")

print("== layer 3: sliding 5s window ==")
alerts = []
w = fresh(enforce=True)
obs = Observer(w, alerts.append, window_seconds=5.0, threshold=3,
               poll_timeout=0.05)
obs.start()
for _ in range(2):
    w.gate_transition({"set": {"reward": -1}})
time.sleep(0.3)
ok(alerts == [], "2 violations inside window: below threshold, no alert")
w.gate_transition({"set": {"reward": -1}})
time.sleep(0.3)
ok(len(alerts) == 1, "3rd violation within 5s window fires exactly one alert")
ok("window" in alerts[0], "alert message reports the window")
w.gate_transition({"set": {"reward": -1}})
time.sleep(0.3)
ok(len(alerts) == 1, "alert cooldown: no re-fire inside same window")
obs.stop()
obs.join(timeout=2)
ok(not obs.is_alive(), "observer thread stops cleanly")

print("== layer 3: spread-out violations do not alert ==")
alerts2 = []
w2 = fresh(enforce=True)
obs2 = Observer(w2, alerts2.append, window_seconds=0.4, threshold=3,
                poll_timeout=0.05)
obs2.start()
for _ in range(3):
    w2.gate_transition({"set": {"reward": -1}})
    time.sleep(0.25)  # each violation ages out before the 3rd lands
time.sleep(0.2)
ok(alerts2 == [], "3 violations spread beyond the window: no alert")
obs2.stop(); obs2.join(timeout=2)

print("== Toga bridge: buffer then flush ==")
b = fresh()
b.gate_transition({"set": {"reward": 1}})
fl = FakeLogger()
b.attach_memory_logger(fl)
ok(len(fl.entries) >= 2, "buffered entries (init+transition) flushed on attach")
ok(all(x["role"] == "imperium" for x in fl.entries),
   "chain witness uses role='imperium' (passes MemoryLogger guard)")
ok(all(x["content"].startswith("imperium:") for x in fl.entries),
   "witness content carries type + entry hash")
n = len(fl.entries)
b.gate_transition({"set": {"reward": 2}})
ok(len(fl.entries) == n + 1, "post-attach entries mirror live")

print("== chokepoint adapter: flag extraction ==")
ok(_extract_flags({"cmd": "chromium --no-sandbox --headless"}) != {},
   "string payload sandbox token detected")
ok(_extract_flags({"opts": {"no_sandbox": True}}) != {},
   "nested dict key detected")
ok(_extract_flags({"opts": {"no_sandbox": False}}) == {},
   "falsy flag ignored")
ok(_extract_flags({"text": "hello world", "n": 3}) == {},
   "clean payload extracts nothing")

print("== chokepoint adapter: observe_dispatch never breaks dispatch ==")
imperium._config_getter = lambda k, d: {"imperium_enabled": True,
                                        "imperium_enforce": False}.get(k, d)
imperium._data_dir = None
imperium._enforcer = None
imperium._observer = None
ok(imperium.observe_dispatch("browser", {"cmd": "curl example.com"},
                             "agentic") is True,
   "clean dispatch passes")
ok(imperium.observe_dispatch("powershell", {"cmd": "run --no-sandbox"},
                             "agentic") is True,
   "flagged dispatch still passes in observe-only mode")
enf = imperium.get_enforcer()
ok(any(x["type"] == "violation" for x in enf.log_chain),
   "flagged dispatch recorded as violation in the chain")
ok(enf.snapshot()["flags"] == {}, "flag-reset keeps shadow state clean")
imperium.shutdown()
imperium._config_getter = None

print(f"\nALL {PASS} CHECKS PASSED - IMPERIUM v{IMPERIUM_VERSION} "
      f"(parent {PARENT_RELEASE})")
