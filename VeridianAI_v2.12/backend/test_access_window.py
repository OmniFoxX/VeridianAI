"""
test_access_window.py -- regression gate for access_policy time windows
=======================================================================

Run:  python test_access_window.py     (no pytest dependency; same style as
                                        test_customs.py / test_expression_engine.py)

Origin (v2.12.9, field report: Todd): an "11am-1am" window appeared to
reject a 2pm login. The overnight math below is provably correct -- the
real failure mode was an AM/PM slip in the native time picker producing
a DIFFERENT stored window whose 24-hour rejection message read like the
intended one. These checks pin the correct math forever, and pin the new
unambiguous 12-hour _fmt_window rendering that makes such slips visible.

Covers:
  * _parse_window: valid same-day + overnight, malformed, zero-length
  * _window_state: same-day and overnight membership + remaining minutes
  * THE FIELD CASE: 11:00-01:00 must admit 2:00 PM (overnight window)
  * the AM/PM-slip twin 11:00-13:00 must REJECT 2:00 PM (correct behavior)
  * _fmt_window: 12-hour AM/PM rendering, "(overnight)" tag, malformed fallback
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import access_policy as ap  # noqa: E402

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def mins(h, m=0):
    return h * 60 + m


print("_parse_window")
check("same-day parses", ap._parse_window("07:30-21:00") == (450, 1260))
check("overnight parses", ap._parse_window("11:00-01:00") == (660, 60))
check("zero-length rejected", ap._parse_window("09:00-09:00") is None)
check("malformed rejected", ap._parse_window("banana") is None)
check("bad hour rejected", ap._parse_window("25:00-01:00") is None)
check("bad minute rejected", ap._parse_window("11:61-01:00") is None)
check("empty rejected", ap._parse_window("") is None)

print("_window_state -- same-day 11:00-13:00 (the AM/PM-slip twin)")
w = ap._parse_window("11:00-13:00")
check("10:59 outside", ap._window_state(w, mins(10, 59))[0] is False)
check("11:00 inside (inclusive start)", ap._window_state(w, mins(11))[0] is True)
check("12:00 inside", ap._window_state(w, mins(12))[0] is True)
check("13:00 outside (exclusive end)", ap._window_state(w, mins(13))[0] is False)
check("14:00 OUTSIDE -- this is what a PM slip does to a 2pm login",
      ap._window_state(w, mins(14))[0] is False)
check("remaining at noon = 60", ap._window_state(w, mins(12))[1] == 60)

print("_window_state -- overnight 11:00-01:00 (THE FIELD CASE)")
w = ap._parse_window("11:00-01:00")
check("2:00 PM INSIDE (the reported symptom must never reproduce)",
      ap._window_state(w, mins(14))[0] is True)
check("11:00 inside (inclusive start)", ap._window_state(w, mins(11))[0] is True)
check("23:59 inside", ap._window_state(w, mins(23, 59))[0] is True)
check("00:30 inside (past midnight)", ap._window_state(w, mins(0, 30))[0] is True)
check("01:00 outside (exclusive end)", ap._window_state(w, mins(1))[0] is False)
check("10:00 outside", ap._window_state(w, mins(10))[0] is False)
check("remaining at 2pm = 11h", ap._window_state(w, mins(14))[1] == 11 * 60)
check("remaining at 00:30 = 30", ap._window_state(w, mins(0, 30))[1] == 30)

print("_window_state -- overnight 20:00-06:00 (docstring example)")
w = ap._parse_window("20:00-06:00")
check("21:00 inside", ap._window_state(w, mins(21))[0] is True)
check("03:00 inside", ap._window_state(w, mins(3))[0] is True)
check("12:00 outside", ap._window_state(w, mins(12))[0] is False)
check("remaining at 21:00 = 9h", ap._window_state(w, mins(21))[1] == 9 * 60)

print("_fmt_window -- unambiguous 12-hour rendering")
check("overnight tagged",
      ap._fmt_window("11:00-01:00") == "11:00 AM to 1:00 AM (overnight)",
      repr(ap._fmt_window("11:00-01:00")))
check("same-day PM shown",
      ap._fmt_window("11:00-13:00") == "11:00 AM to 1:00 PM",
      repr(ap._fmt_window("11:00-13:00")))
check("noon is 12:00 PM",
      ap._fmt_window("09:00-12:00") == "9:00 AM to 12:00 PM",
      repr(ap._fmt_window("09:00-12:00")))
check("midnight is 12:00 AM",
      ap._fmt_window("20:00-00:00") == "8:00 PM to 12:00 AM (overnight)",
      repr(ap._fmt_window("20:00-00:00")))
check("malformed falls back to old rendering",
      ap._fmt_window("junk-value") == "junk to value",
      repr(ap._fmt_window("junk-value")))

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
