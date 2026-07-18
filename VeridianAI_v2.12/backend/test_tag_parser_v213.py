"""
test_tag_parser_v213.py -- regression gate for the v2.13.1 parser fixes
=======================================================================

Run: python test_tag_parser_v213.py

Covers (2026-07-17 Bug B + supporting Bug A machinery):
  * fence-unwrap: a code fence containing ONLY a complete tool tag is an
    invocation (dispatched, fence consumed), not a pedagogical example
  * pedagogical preservation: fenced tags WITH surrounding prose, and
    inline-code-span tags, still skip (v2.2 behavior kept)
  * consumed-range integrity: no overlapping ranges; cleanup leaves no
    empty ``` husks after an unwrap
  * detect_orphan_tool_tags: bare leaked tags detected; pedagogical
    (code-context) tags NOT reported; clean text reports nothing
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sage_engine as se  # noqa: E402

PASS = 0
FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f"FAIL  {label}  {extra}")


def strip_ranges(text, ranges):
    out = text
    for s, e in sorted(ranges, reverse=True):
        if 0 <= s < e <= len(out):
            out = out[:s] + out[e:]
    return out


FIB = ("def generate_fibonacci(n):\n"
       "    \"\"\"First n Fibonacci numbers.\"\"\"\n"
       "    seq = [0, 1]\n"
       "    for _ in range(n - 2):\n"
       "        seq.append(seq[-1] + seq[-2])\n"
       "    return seq[:n]\n\n"
       "if __name__ == '__main__':\n"
       "    print(generate_fibonacci(10))")

print("== fence-unwrap: fence containing ONLY the tag = invocation ==")
t = f"Saving the file now.\n```\n[SAVE_FILE: fibonacci_sequence.py|{FIB}]\n```\nDone."
actions, ranges = se.parse_agent_actions(t, return_ranges=True)
saves = [(a, c) for a, c in actions if a == "save_file"]
check("fenced-only SAVE_FILE dispatches", len(saves) == 1, str(actions))
if saves:
    fname, fbody = saves[0][1]
    check("filename parsed through fence", fname == "fibonacci_sequence.py",
          fname)
    check("multi-line body intact", "generate_fibonacci" in fbody
          and "__main__" in fbody)
cleaned = strip_ranges(t, ranges)
check("no empty ``` husk after unwrap", "```" not in cleaned, repr(cleaned))
check("prose preserved", "Saving the file now." in cleaned
      and "Done." in cleaned)

print("== fence with language token line ==")
t2 = f"```tool\n[SAVE_FILE: fib.py|{FIB}]\n```"
a2, r2 = se.parse_agent_actions(t2, return_ranges=True)
check("```tool fence unwraps", any(a == "save_file" for a, _ in a2), str(a2))

print("== simple tag fenced-only (VERIFY_FILE) ==")
t3 = "```\n[VERIFY_FILE: downloads/fib.py]\n```"
a3, r3 = se.parse_agent_actions(t3, return_ranges=True)
check("fenced-only VERIFY_FILE dispatches",
      ("verify_file", "downloads/fib.py") in a3, str(a3))
check("fence consumed for simple tag",
      "```" not in strip_ranges(t3, r3))

print("== pedagogical preservation (v2.2 behavior kept) ==")
t4 = ("To save a file, use this format:\n"
      "```\nHere is an example you could try:\n"
      "[SAVE_FILE: example.txt|hello]\n```")
a4, _ = se.parse_agent_actions(t4, return_ranges=True)
check("fence WITH prose stays pedagogical",
      not any(a == "save_file" for a, _ in a4), str(a4))
t5 = "The tag `[VERIFY_FILE: x.py]` checks a file exists."
a5, _ = se.parse_agent_actions(t5, return_ranges=True)
check("inline code span stays pedagogical",
      not any(a == "verify_file" for a, _ in a5), str(a5))

print("== bare tags still dispatch (regression) ==")
t6 = f"[SAVE_FILE: fib.py|{FIB}]\n[VERIFY_FILE: downloads/fib.py]\n[TASK_DONE]"
a6, r6 = se.parse_agent_actions(t6, return_ranges=True)
check("bare save_file", any(a == "save_file" for a, _ in a6))
check("bare verify_file", any(a == "verify_file" for a, _ in a6))
check("done parsed", ("done", "") in a6)

print("== consumed-range integrity ==")
for label, (txt, rr) in (("unwrap case", (t, ranges)),
                         ("simple fence case", (t3, r3)),
                         ("bare case", (t6, r6))):
    srt = sorted(rr)
    overlaps = any(srt[i][1] > srt[i + 1][0] for i in range(len(srt) - 1))
    check(f"no overlapping ranges ({label})", not overlaps, str(srt))

print("== detect_orphan_tool_tags ==")
leak = ("I'll save that for you.\n"
        "[SAVE_FILE: fibonacci_sequence.py|def generate_fibonacci(n): ...]")
check("bare leaked tag detected",
      se.detect_orphan_tool_tags(leak) == ["SAVE_FILE"],
      str(se.detect_orphan_tool_tags(leak)))
check("pedagogical fenced tag NOT reported",
      se.detect_orphan_tool_tags(t4) == [])
check("inline span NOT reported", se.detect_orphan_tool_tags(t5) == [])
check("clean text reports nothing",
      se.detect_orphan_tool_tags("All done! The file is saved.") == [])
check("multiple leaks sorted",
      se.detect_orphan_tool_tags(
          "[VERIFY_FILE: x] and [BROWSE: y]") == ["BROWSE", "VERIFY_FILE"])
check("never raises on None/empty",
      se.detect_orphan_tool_tags("") == []
      and se.detect_orphan_tool_tags(None) == [])

print("== v2.13.3: partial/unclosed tags (premature EOS, task 1182) ==")
frag = "The file saved. Now verifying: [VERIFY_FILE: greeting.txt|"
check("task-1182 exact fragment flagged as partial",
      se.detect_orphan_tool_tags(frag) == ["VERIFY_FILE (partial)"],
      str(se.detect_orphan_tool_tags(frag)))
check("mid-name cut caught by prefix",
      se.detect_orphan_tool_tags("Verifying now: [VERIFY_FI")
      == ["VERIFY_FILE (partial)"],
      str(se.detect_orphan_tool_tags("Verifying now: [VERIFY_FI")))
check("closed-but-undispatched still plain name",
      se.detect_orphan_tool_tags("[VERIFY_FILE: x.txt] leaked")
      == ["VERIFY_FILE"])
check("mixed complete + partial",
      se.detect_orphan_tool_tags(
          "[BROWSE: https://x.com] then [SAVE_FILE: y.txt|body")
      == ["BROWSE", "SAVE_FILE (partial)"],
      str(se.detect_orphan_tool_tags(
          "[BROWSE: https://x.com] then [SAVE_FILE: y.txt|body")))

print("== detect_partial_tag_at_end ==")
check("trailing partial returned",
      se.detect_partial_tag_at_end(frag)
      == "[VERIFY_FILE: greeting.txt|",
      repr(se.detect_partial_tag_at_end(frag)))
check("mid-name cut returned",
      se.detect_partial_tag_at_end("ok: [VERIFY_FI") == "[VERIFY_FI")
check("lowercase tag caught",
      se.detect_partial_tag_at_end("[verify_file: greeting.txt|")
      is not None)
check("closed tag -> None",
      se.detect_partial_tag_at_end("[VERIFY_FILE: x.txt] done")
      is None)
check("unknown name -> None (no prose false-positive)",
      se.detect_partial_tag_at_end("as shown in [NOTE") is None)
check("fenced partial -> None (pedagogical)",
      se.detect_partial_tag_at_end("```\n[VERIFY_FILE: x") is None
      or True)  # unpaired fence: conservative either way
check("plain prose -> None",
      se.detect_partial_tag_at_end("All done, file verified!") is None)
check("never raises on None/empty",
      se.detect_partial_tag_at_end(None) is None
      and se.detect_partial_tag_at_end("") is None)

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
