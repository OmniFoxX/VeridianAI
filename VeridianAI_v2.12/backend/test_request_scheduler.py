"""test_request_scheduler.py -- gates the aging-fair scheduler (v2.12.2).

Covers BOTH layers:
  * RequestScheduler (sync reference): scoring, ceiling math, admission,
    urgent bucket, starvation simulation vs strict precedence.
  * AsyncAgingGate (the LIVE gate in model_manager.generate): fairness,
    urgent jump, admission shed, cancellation safety, no-deadlock handoff.

Run:  python test_request_scheduler.py   (pure stdlib, no model needed)
"""
import asyncio
import time

import request_scheduler as rs

PASS = 0
FAIL = 0

def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")

# ---------------------------------------------------------------- sync layer

def test_scoring_and_ceiling():
    r = rs.Request(tier="remote", arrival_time=0.0)
    h = rs.Request(tier="hyperlocal", arrival_time=40.0)
    # ceiling: (3-1)/0.05 = 40s -- at t=40 an aged remote TIES a fresh local,
    # and FIFO tie-break (earlier seq) sends the remote first.
    check("ceiling math", abs(rs.max_wait_ceiling(0.05) - 40.0) < 1e-9)
    s_r = rs.priority_score(r, 40.0, 0.05)
    s_h = rs.priority_score(h, 40.0, 0.05)
    check("aged remote ties fresh local at ceiling", abs(s_r - s_h) < 1e-9)
    check("clock-skew guard", rs.priority_score(rs.Request(tier="remote", arrival_time=100.0), 50.0, 0.05) == 1.0)

def test_starvation_sim():
    """Sustained over-capacity: hyperlocal arrives every tick, service is one
    request per tick. Strict precedence never serves remote; aging does,
    within the ceiling."""
    # -- aging scheduler --
    sched = rs.RequestScheduler(aging_rate=0.05, queue_limit=10_000)
    sched.submit("remote", now=0.0, ident="R")
    remote_served_at = None
    for tick in range(200):
        sched.submit("hyperlocal", now=float(tick), ident=f"H{tick}")
        served = sched.pick_next(now=float(tick))
        if served and served.ident == "R":
            remote_served_at = tick
            break
    check("aging serves remote", remote_served_at is not None)
    check("within ceiling", remote_served_at is not None
          and remote_served_at <= rs.max_wait_ceiling(0.05) + 1)
    # -- strict precedence control (what the old _PriorityGate would do) --
    strict_q = [("remote", 0.0)]
    remote_served_strict = None
    for tick in range(200):
        strict_q.append(("hyperlocal", float(tick)))
        strict_q.sort(key=lambda x: (0 if x[0] == "hyperlocal" else 1, x[1]))
        served = strict_q.pop(0)
        if served[0] == "remote":
            remote_served_strict = tick
            break
    check("strict precedence starves remote (the disease)", remote_served_strict is None)

def test_admission_and_urgent_bucket():
    sched = rs.RequestScheduler(aging_rate=0.05, queue_limit=3, urgent_per_hour=1)
    for i in range(3):
        check(f"admit {i}", sched.submit("remote", now=0.0)["accepted"])
    r = sched.submit("remote", now=0.0)
    check("shed at limit", not r["accepted"])
    r = sched.submit("hyperlocal", now=0.0)
    check("downshift suggested for local tiers", not r["accepted"] and r["suggest_downshift"])
    r = sched.submit("hyperlocal", now=0.0, urgent=True, profile="todd")
    check("urgent token bypasses admission", r["accepted"] and r["urgent"])
    r = sched.submit("hyperlocal", now=1.0, urgent=True, profile="todd", force=True)
    check("second urgent within the hour demotes to normal", r["accepted"] and not r["urgent"])
    nxt = sched.pick_next(now=2.0)
    check("urgent served first", nxt is not None and nxt.urgent)

# --------------------------------------------------------------- async gate

async def _worker(gate, tier, urgent, hold, served, name):
    await gate.acquire(tier, urgent=urgent, ident=name)
    try:
        served.append((name, time.monotonic()))
        await asyncio.sleep(hold)
    finally:
        gate.release()

async def test_gate_fairness():
    """The starvation scenario, live: a remote enqueues once, then hyperlocal
    requests keep ARRIVING (staggered, like real traffic; service 30ms,
    aging_rate 40 -> ceiling (3-1)/40 = 50ms). Strict precedence would serve
    every H first, forever. The aged remote must beat the LATE-arriving
    locals -- that is exactly the ceiling's guarantee. Same-instant arrivals
    correctly keep their base-weight order (aging accrues to them too)."""
    gate = rs.AsyncAgingGate(aging_rate=40.0, queue_limit=100)
    served = []
    tasks = [asyncio.create_task(_worker(gate, "hyperlocal", False, 0.03, served, "H0"))]
    await asyncio.sleep(0.005)      # H0 is now HOLDING the gate
    tasks.append(asyncio.create_task(_worker(gate, "local_network", False, 0.03, served, "L")))
    tasks.append(asyncio.create_task(_worker(gate, "remote", False, 0.03, served, "R")))
    for i in range(1, 9):           # sustained local pressure AFTER R queued
        await asyncio.sleep(0.02)
        tasks.append(asyncio.create_task(_worker(gate, "hyperlocal", False, 0.03, served, f"H{i}")))
    await asyncio.gather(*tasks)
    order = [n for n, _ in served]
    check("all served (no starvation)", set(order) == {f"H{i}" for i in range(9)} | {"L", "R"})
    late_locals = [order.index(f"H{i}") for i in range(5, 9)]
    check("aged remote beats late-arriving locals", order.index("R") < min(late_locals))
    check("local_network aged up before remote", order.index("L") < order.index("R"))

async def test_gate_urgent_and_admission():
    gate = rs.AsyncAgingGate(aging_rate=0.05, queue_limit=2)
    served = []
    t0 = asyncio.create_task(_worker(gate, "hyperlocal", False, 0.05, served, "H0"))
    await asyncio.sleep(0.005)
    t1 = asyncio.create_task(_worker(gate, "hyperlocal", False, 0.02, served, "H1"))
    t2 = asyncio.create_task(_worker(gate, "hyperlocal", False, 0.02, served, "H2"))
    await asyncio.sleep(0.005)     # queue depth now 2 == limit
    adm = gate.should_admit("hyperlocal")
    check("gate sheds at queue_limit", not adm["admit"] and adm["suggest_downshift"])
    check("urgent admitted past full queue", gate.should_admit("hyperlocal", urgent=True)["admit"])
    tu = asyncio.create_task(_worker(gate, "remote", True, 0.02, served, "U"))
    await asyncio.gather(t0, t1, t2, tu)
    order = [n for n, _ in served]
    check("urgent served immediately after holder", order[1] == "U")

async def test_gate_cancellation():
    """A waiter that disconnects while queued must not wedge the gate."""
    gate = rs.AsyncAgingGate(aging_rate=0.05, queue_limit=10)
    served = []
    t0 = asyncio.create_task(_worker(gate, "hyperlocal", False, 0.03, served, "H0"))
    await asyncio.sleep(0.005)
    dead = asyncio.create_task(_worker(gate, "hyperlocal", False, 0.03, served, "DEAD"))
    t2 = asyncio.create_task(_worker(gate, "hyperlocal", False, 0.03, served, "H2"))
    await asyncio.sleep(0.005)
    dead.cancel()
    try:
        await dead
    except asyncio.CancelledError:
        pass
    await asyncio.gather(t0, t2)
    order = [n for n, _ in served]
    check("cancelled waiter skipped", "DEAD" not in order and order == ["H0", "H2"])
    # gate must be fully idle again: a fresh acquire returns immediately
    await asyncio.wait_for(gate.acquire("hyperlocal"), timeout=0.5)
    gate.release()
    check("gate idle after drain (no deadlock)", gate.depth() == 0)

def main():
    test_scoring_and_ceiling()
    test_starvation_sim()
    test_admission_and_urgent_bucket()
    asyncio.run(test_gate_fairness())
    asyncio.run(test_gate_urgent_and_admission())
    asyncio.run(test_gate_cancellation())
    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
