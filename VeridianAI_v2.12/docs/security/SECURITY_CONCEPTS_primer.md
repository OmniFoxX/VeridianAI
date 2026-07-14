# Security Concepts — Plain-Language Primer

**Project:** VeridianAI v2.12
**Written:** 2026-07-13
**Companion to:** `SECURITY_REMEDIATION_semgrep_2026-07-13.md` (same folder)

A reference explaining, in plain language, the four behavior changes from the
2026-07-13 security remediation — what each concept means, how the code behaved
**before** vs. **now**, and **why** the change supports security, HIPAA, and
provenance. Kept here so the reasoning is available next time something similar
comes up.

---

## Vocabulary quick-reference

| Term | Plain meaning |
|------|---------------|
| **bind** | Which "doors" of the computer a program listens at for incoming connections. |
| **loopback / `127.0.0.1` / localhost** | A special address meaning "this same computer only." Traffic to it never leaves the machine. |
| **`0.0.0.0`** | "Listen on *every* door, including Wi-Fi/LAN" — other devices can reach it. |
| **port** | The numbered door (e.g. `8080`). |
| **plaintext** | Unencrypted; anyone who can see the network traffic can read it. |
| **shell** | The command interpreter (`cmd.exe` / `bash`). Understands special symbols like `&&`, `\|`, `>`, `%VAR%`. |
| **argv (argument list)** | A command expressed as separate items: `["python", "main.py", "--port", "8188"]`. |
| **serialize / deserialize (load)** | Turn a live object into bytes to save (serialize); turn bytes back into a live object (deserialize/load). |
| **pickle** | Python's built-in serialize format. Can store *code*, not just data — which is why loading an untrusted one is dangerous. |
| **TLS** | The encryption behind `https://` and `wss://`. |
| **`ws://` vs `wss://`** | WebSocket (a live two-way browser↔server channel), plaintext vs. encrypted. The extra `s` = secure. |

---

## 1. Loopback vs. network binding — the BLE daemon

**Concept:** a server chooses which network "doors" to listen on. `127.0.0.1`
(loopback) = same machine only. `0.0.0.0` = every interface, reachable by other
devices.

**Before:** the daemon opened its WebSocket at `0.0.0.0:8080`, so any device on
the same network could connect — over `ws://` (plaintext, unencrypted).

**Now:** it opens at `127.0.0.1:8080`. Only programs on the same PC can connect.

```
Before:  Phone at 192.168.1.50 could open ws://192.168.1.20:8080/ws and reach
         the daemon on your PC. So could anything else on that Wi-Fi.
Now:     That phone connection is refused. Only the app on the same PC connects.
```

**Why:** HIPAA transmission security says "don't send sensitive data across a
network in the clear." Loopback traffic never touches a network, so it sidesteps
that concern — the safe default. A plaintext channel on `0.0.0.0` is exactly what
that rule warns against. This channel is only a bridge between the daemon and the
app on the same machine, so loopback is correct and nothing user-visible changes.
To expose it deliberately: set `BITCHAT_WS_HOST=0.0.0.0` **and** put TLS in front.

---

## 2. Running commands without a shell — the ComfyUI launcher

**Concept:** the **shell** interprets special symbols (`&&` = "then also run
this", `|` = pipe, `%VAR%` = insert a variable's value). Running a command
`shell=True` hands the whole string to the shell to interpret; `shell=False`
launches the program directly, treating those symbols as plain text.

**Before:** `subprocess.Popen(command, shell=True)` — the launch string went
through `cmd.exe`. Shell features worked, but so would any command hidden in the
string.

**Now:** `shell=False` — the program launches directly; shell symbols are inert.

```
Config:  python main.py --listen
   Before & Now:   Identical. Launches normally.   ← the 99% case

Config:  python main.py && cleanup.exe
   Before:  cmd.exe runs BOTH (that is what && means).
   Now:     "&&" and "cleanup.exe" are passed to python as text; nothing extra runs.
```

If you ever need real shell features, put the lines in a `.bat` file and point
the config at it (the launcher runs a file path safely).

**Why:** `shell=True` enables **command injection** — if any part of the string
can be influenced by input you don't fully control, an attacker could smuggle in
extra commands. Here it was owner-only config (low real risk), but reviewers and
HIPAA audits flag `shell=True` on sight. Removing it means there is *no path*
where a string quietly becomes an unintended command.

---

## 3. Safe model loading — `torch.load(weights_only=True)`

**Concept:** loading a file (deserializing) with **pickle** can, by design,
execute code stored inside that file — not just read data. So loading an
untrusted model file can run code on your machine.

**Before:** `torch.load(path)` reconstructed *any* object, including ones that
run code. A booby-trapped file could execute an attacker's code the instant it
was loaded.

**Now:** `torch.load(path, weights_only=True)` allows only plain data (tensors,
numbers, lists, dicts) and refuses anything that would run code.

```
Loading your own trained model  →  works exactly the same (all plain data).

Loading a booby-trapped .pt file:
   Before:  could silently run the attacker's code.
   Now:     raises "Weights only load failed..." and runs nothing.
```

**Edge case:** if a *legitimate* file stores an unusual object type, the strict
loader may refuse it too. The fix is **not** to disable the safety — it is to
vouch for that specific type: `torch.serialization.add_safe_globals([TheType])`.

**Why:** integrity and provenance — being certain that loading a model can only
ever load numbers. It matters most because Aether can pull model files from
*other nodes*: once a file crosses a trust boundary, "loading runs code" is a real
risk. (PyTorch is moving to make `weights_only=True` the default for this reason.)

---

## 4. Encrypted WebSockets — `wss://` on HTTPS pages

**Concept:** `ws://` is a live browser↔server channel, plaintext. `wss://` is the
same thing encrypted with TLS (like `https://`).

**Before:** the browser always built the URL as `ws://...`, even on a secure
`https://` page — a secure-looking page with an unencrypted data channel
underneath.

**Now:** the code matches the channel to the page:

```js
const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
```

Read as: "if the page is HTTPS, use encrypted `wss:`, else `ws:`." (The `? :` is
JavaScript's shorthand for if/else — like Python's `a if condition else b`.)

```
http://localhost:8000       →  ws://localhost:8000/ws/chat    (local; no change)
https://myclinic.example.com →  wss://myclinic.example.com/ws/chat  (now encrypted)
     (Before, it wrongly tried ws:// — plaintext under a secure page.)
```

**Why:** same transmission-security principle as #1. The channel now *inherits*
the page's security automatically, so a secure page can't accidentally send
messages in the clear.

---

## Five rules of thumb (carry these forward)

1. **Default to loopback.** Listen on `127.0.0.1` unless you have a deliberate
   reason to expose something to the network — and when you do, encrypt it.
2. **Don't hand strings to a shell.** Prefer `shell=False` and pass arguments as
   a list; it removes the whole command-injection category.
3. **"Loading a file" can mean "running code."** For formats like pickle, use the
   safe-loading option, and be extra careful with anything that crossed a trust
   boundary.
4. **Match encryption to context.** If data travels over a network, encrypt it
   (`wss` / `https` / TLS).
5. **Never build a file path or URL directly from input you don't control**
   without validating or containing it first — that one sentence covers the whole
   path-traversal / SSRF family.
