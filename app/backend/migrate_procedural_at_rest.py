"""migrate_procedural_at_rest.py -- one-time: encrypt the EXISTING plaintext
procedural memory store using THIS install's own .atrest_key.

WHY THIS EXISTS

sage_data/procedural_memory/procedural.json was being written as plaintext JSON
while every neighbour around it (archives, chat_memory, prompt_cache, the vlts
chunks, the memory chain) was Fernet-encrypted. It is not metadata. Every entry
holds:

    user_request           up to 500 chars of the user's VERBATIM message
    final_answer_preview   300 chars of the reply
    <the dict key>         a slug of the request text, e.g.
                           "task:276aa555:hello_sage_and_welcome_to_"

Found 2026-08-20 during an at-rest audit: 71 entries, 102 KB, zero ciphertext.

procedural_memory.py now writes through atrest, so anything saved from here on
is encrypted. That alone would leave the existing file plaintext on disk until
something happened to trigger a save. This sweeps it deliberately instead of
waiting for luck.

WHY A SCRIPT, AND WHY ON THE MACHINE

Same reason as migrate_at_rest.py: the at-rest key must be the one the running
app resolves from config.DATA_DIR. A remote or mounted view of sage_data can
resolve a different one, and encrypting under the wrong key produces a file the
app cannot read.

HOW TO RUN  (with VeridianAI stopped), from the project's backend folder:

    python migrate_procedural_at_rest.py            # do it
    python migrate_procedural_at_rest.py --dry-run  # look first, change nothing

SAFETY

The file is encrypted, then decrypted, and the result is compared against the
original -- both as a parsed object and byte-for-byte -- BEFORE anything is
replaced. The write is atomic (temp + os.replace). If any check fails, the
original is left exactly as it was and the script exits non-zero.

ONE DELIBERATE DEPARTURE FROM migrate_at_rest.py: no plaintext quarantine.

That script copies plaintext originals into _plaintext_quarantine before
replacing them, and leaves the operator to delete the folder afterwards. That
is a reasonable default for images. It is the wrong default HERE: this file's
whole problem is verbatim user text sitting in the clear, and a quarantine copy
would faithfully preserve exactly that, in the same folder tree, indefinitely,
behind a step someone has to remember. A backup whose contents are the thing
you are trying to stop exposing is not a safety net.

The round-trip verification is what makes that safe to omit -- the ciphertext
is proven to decrypt back to the identical object before the original is
touched. If you want a backup anyway, take one BEFORE running this and store it
off the machine, treating it as sensitive.

Honest limitation: os.replace unlinks the old file, it does not scrub the
blocks it occupied. On an SSD those remnants are not reliably erasable from
user space. This closes the file-level exposure, which is what an at-rest
control is; it is not a forensic wipe.
"""
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import atrest                                            # noqa: E402
from config import DATA_DIR, PROCEDURAL_DIR              # noqa: E402


def _count(obj):
    if not isinstance(obj, dict):
        return "?"
    return "%d successful / %d unsuccessful" % (
        len(obj.get("successful") or {}), len(obj.get("unsuccessful") or {}))


def migrate(dry_run=False):
    path = os.path.join(str(PROCEDURAL_DIR), "procedural.json")
    print("[migrate_procedural] at-rest key directory:", DATA_DIR)
    print("[migrate_procedural] target:", path)

    if not os.path.exists(path):
        print("[migrate_procedural] no store present -- nothing to do.")
        return 0

    raw = io.open(path, "rb").read()

    # Idempotent: a second run must be a no-op, not a double-encrypt.
    if atrest.is_encrypted(raw):
        print("[migrate_procedural] already encrypted -- nothing to do.")
        return 0

    try:
        original = json.loads(raw.decode("utf-8"))
    except Exception as e:
        print("[migrate_procedural] ABORT: file is neither our ciphertext nor "
              "parseable JSON (%s: %s). Left untouched." % (type(e).__name__, e))
        return 2

    print("[migrate_procedural] plaintext store found: %s, %d bytes"
          % (_count(original), len(raw)))

    # SYSTEM TIER: this store is an install-wide singleton with no profile
    # context, chain-witnessed alongside the audit chain. Matches the ns=None
    # that procedural_memory.py now uses -- if these two ever disagree, the app
    # cannot read what this wrote.
    blob = atrest.dump_json_encrypted(original)

    # Prove it round-trips BEFORE replacing anything.
    try:
        back = atrest.load_json_auto(blob)
    except Exception as e:
        print("[migrate_procedural] ABORT: ciphertext would not decrypt "
              "(%s: %s). Original left untouched." % (type(e).__name__, e))
        return 3

    if back != original:
        print("[migrate_procedural] ABORT: decrypted object differs from the "
              "original. Original left untouched.")
        return 4

    # And byte-for-byte on a canonical dump, which catches ordering or numeric
    # coercion an == on dicts would forgive.
    if json.dumps(back, sort_keys=True, ensure_ascii=False) != \
            json.dumps(original, sort_keys=True, ensure_ascii=False):
        print("[migrate_procedural] ABORT: canonical re-serialisation differs. "
              "Original left untouched.")
        return 5

    if not atrest.is_encrypted(blob):
        print("[migrate_procedural] ABORT: produced blob is not recognised as "
              "ciphertext. Original left untouched.")
        return 6

    print("[migrate_procedural] round-trip verified: %s, %d bytes ciphertext"
          % (_count(back), len(blob)))

    if dry_run:
        print("[migrate_procedural] --dry-run: nothing written.")
        return 0

    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass
        print("[migrate_procedural] ABORT during write (%s: %s). Original "
              "left untouched." % (type(e).__name__, e))
        return 7

    # Read it back off DISK -- not from the variable we just held in memory.
    final = io.open(path, "rb").read()
    if not atrest.is_encrypted(final):
        print("[migrate_procedural] ERROR: file on disk is not encrypted "
              "after the write. Investigate before starting VeridianAI.")
        return 8
    if atrest.load_json_auto(final) != original:
        print("[migrate_procedural] ERROR: file on disk does not decrypt back "
              "to the original. Investigate before starting VeridianAI.")
        return 9

    print("[migrate_procedural] done -- store encrypted and verified on disk.")
    print("[migrate_procedural] No plaintext copy was kept, on purpose: it "
          "would preserve the verbatim user text this exists to protect.")
    return 0


if __name__ == "__main__":
    sys.exit(migrate(dry_run="--dry-run" in sys.argv))
