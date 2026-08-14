# Build Battle — gate tests

A **gate test** makes a Build Battle decide on evidence instead of prose. Without
one, the Judge model reads two submissions and forms an opinion. With one, both
submissions are actually **executed** against your test first, and the Judge is
told the results are decisive.

---

## The terms, once

| Term | What it means here |
|---|---|
| **Submission / candidate** | The code each Builder model wrote. Extracted from its final message. |
| **Gate test** | A Python file *you* wrote that checks whether a submission is correct. |
| **Module** | A Python file you can `import`. `fizzbuzz.py` is the module `fizzbuzz`. |
| **Import** | Loading another file's code so you can call it. `import fizzbuzz`. |
| **Exit code** | The number a program returns when it ends. **0 means success**, anything else means failure. This is how the gate decides pass or fail. |
| **Sandbox** | A temporary throwaway folder the test runs in, so it cannot touch your real files. |

---

## The one rule that makes it work

**The name of your test file decides what the submission is called.**

VeridianAI takes your test filename, strips `test_`, and writes the submission
to disk under that name:

```
your test file:      test_fizzbuzz.py
strip "test_":            fizzbuzz
submission saved as:      fizzbuzz.py
```

So your test must contain **`import fizzbuzz`**. That is the whole trick, and it
is the part that is easy to get wrong.

Both files are written into the same temporary folder, then your test is run.

---

## Recipe

### 1. Write the test

Put it in the **`backend`** folder (or `backend/gates/`), named `test_<something>.py`.

`backend/test_fizzbuzz.py`:

```python
# The submission will be saved as fizzbuzz.py because this file is
# named test_fizzbuzz.py. So that is what we import.
import sys
import fizzbuzz

failures = []

def check(name, got, want):
    if got != want:
        failures.append(f"{name}: expected {want!r}, got {got!r}")

check("3 is Fizz",        fizzbuzz.fizzbuzz(3),  "Fizz")
check("5 is Buzz",        fizzbuzz.fizzbuzz(5),  "Buzz")
check("15 is FizzBuzz",   fizzbuzz.fizzbuzz(15), "FizzBuzz")
check("7 is just 7",      fizzbuzz.fizzbuzz(7),  "7")

for f in failures:
    print("FAIL:", f)
print(f"{4 - len(failures)}/4 passed")

# THIS LINE IS REQUIRED. 0 = the submission passed. Anything else = failed.
sys.exit(1 if failures else 0)
```

### 2. Ask for it in the battle

Put a line starting with `GATE:` anywhere in your Build Battle prompt. Give the
**file name only** — not a path:

```
GATE: test_fizzbuzz.py

Write a fizzbuzz(n) function. It returns "Fizz" for multiples of 3,
"Buzz" for multiples of 5, "FizzBuzz" for both, and the number as a
string otherwise.
```

The `GATE:` line is stripped out before the Builders see the prompt, so they are
not told what the test checks. They only see the challenge.

### 3. Read the result

```
### Gate Test Results

_Running test_fizzbuzz.py against each finalist in the sandbox (module fizzbuzz)._

- **Builder A (qwen2.5)** -- **PASS**: 4/4 passed
- **Builder B (llama3)**  -- **FAIL**: 15 is FizzBuzz: expected 'FizzBuzz', got 'Fizz'
```

A submission that fails the gate cannot win on style.

---

## Rules for the file name

The name must:

- be a **name, not a path** — `test_fizzbuzz.py`, never `C:\...\test_fizzbuzz.py`
  and never `subfolder/test_fizzbuzz.py`
- start with **`test_`**
- end with **`.py`**
- exist in `backend/` or `backend/gates/`

Anything else is refused and you get:

```
> _Gate test not found: <what you typed> -- skipping the execution gate._
```

The battle still runs; it just falls back to judging on the code alone.

**Why the restriction:** before v2.15 this accepted any path at all, including an
absolute one like `C:\Users\you\anything.py` — and whatever it named was read
**and executed**. Since the `GATE:` line is just chat text, anyone who could open
a chat window could run a file of their choosing as the application. Restricting
it to a name inside `backend/` closes that without changing how the feature is
used: naming your own test file was always the point.

---

## Common mistakes

| Symptom | Cause |
|---|---|
| "Gate test not found" | You gave a path, or the name does not start with `test_`, or the file is not in `backend/` |
| Every submission FAILS with `ModuleNotFoundError` | Your test imports the wrong name. `test_fizzbuzz.py` must `import fizzbuzz` |
| Everything PASSES even when it shouldn't | Your test never calls `sys.exit(1)` on failure. Without a non-zero exit code the gate reads success |
| "no code block could be extracted" | The Builder answered in prose without a code block. Not a gate problem |

---

## Notes

- The test runs as a **plain script**, not under pytest. Use `sys.exit()`, not
  pytest assertions, unless the tree you ship includes pytest.
- Default timeout is 60 seconds per submission.
- The test runs in a temporary folder. It cannot see your project files, so it
  must be self-contained apart from importing the submission.
- Everything the test prints comes back in the battle transcript (first 3000
  characters), so print something useful on failure.
- You can also set a permanent default gate in config as
  `build_battle_gate_test`; a `GATE:` line in the prompt overrides it.
