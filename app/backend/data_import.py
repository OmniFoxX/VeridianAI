# -*- coding: utf-8 -*-
"""Read a VeridianAI export back in -- the other half of data_export.py.

Export has existed since v2.14 and wrote a zip that NOTHING could read back.
Data could be extracted but not re-homed: to move machines you hand-placed
folders and a key file, following instructions in MANIFEST.txt. That is not
portability, it is a rescue procedure.

WHAT MAKES THIS AN IMPORT RATHER THAN AN UNZIP
----------------------------------------------
Encryption has to be TRANSLATED, not copied. A portable export's contents are
encrypted with the SOURCE install's key; dropping those bytes into this install
produces files it cannot read. So: decrypt with the key that came with the
archive, re-encrypt with this machine's. A readable export arrives as plain
text and is encrypted on the way in, because landing someone's conversations
on disk in the clear would be a downgrade they did not ask for.

WHAT IS DELIBERATELY REFUSED
----------------------------
- **The memory chain cannot be merged.** It is hash-linked and append-only;
  its value is the unbroken sequence. Splicing a foreign chain into yours does
  not combine two histories, it destroys the property that made either one
  worth having. An imported chain is written alongside as a clearly-named
  REFERENCE copy and never joined.
- **Credentials are never imported.** `.api_keystore.json` in particular:
  importing it would install someone else's API keys on this machine and hand
  them a working credential. Export already refuses to write it; import
  refuses again, because an archive is untrusted input and the two checks
  guard different threats.

SECURITY POSTURE
----------------
A zip from elsewhere is hostile input until proven otherwise:
- every entry is resolved and confirmed to land INSIDE its destination
  (zip-slip / CVE-2007-4559 is the oldest trick in the format)
- absolute paths, drive letters, `..` segments and symlinks are rejected
- total uncompressed size and compression ratio are capped (zip bombs)
- nothing is written until the whole archive has been inspected and the caller
  has chosen sections -- the same "show them first" principle as export

Pure-ASCII source.
"""
from __future__ import annotations

import io
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

__all__ = ["inspect_archive", "restore", "MERGE", "REPLACE",
           "MAX_TOTAL_BYTES", "MAX_RATIO"]

KEY_RAW = "KEY/fernet.key"
KEY_WRAPPED = "KEY/key.wrapped.json"

MERGE = "merge"
REPLACE = "replace"

# A desktop app with someone's whole history; generous but not unbounded.
MAX_TOTAL_BYTES = 20 * 1024 * 1024 * 1024        # 20 GB uncompressed
MAX_RATIO = 200                                   # uncompressed:compressed

# Never restored, whatever the archive says. Mirrors data_export.NEVER_EXPORT
# and then some: export protects OUR data leaving, this protects THIS machine
# from what arrives.
NEVER_IMPORT = {
    ".api_keystore.json",     # a live credential -- would install foreign keys
    ".oracle_pids.json",      # process state, meaningless elsewhere
    ".backend_mode",          # machine-specific hardware choice
    ".atrest_key",            # the source key belongs in KEY/, not loose
    ".keywrap.json",          # per-profile key wraps: never adopt another's
    ".key_migration.json",    # a claim about THIS install's files, not theirs
    "fernet.key",
}

# Sections whose contents live encrypted at rest. Anything landing here is
# encrypted on the way in if it is not already.
ENCRYPTED_SECTIONS = {"chat", "archives", "evidence", "procedural", "uploads"}

# Cannot be merged -- see the module docstring.
UNMERGEABLE = {"memory_chain"}


class ImportError_(Exception):
    """Refused: the archive is not usable, or not safe to apply."""


# ---------------------------------------------------------------------------
# Entry-name safety
# ---------------------------------------------------------------------------

def _entry_is_safe(name: str) -> Tuple[bool, str]:
    """Reject anything that could escape its destination."""
    if not name or name.endswith("/"):
        return False, "directory entry"
    n = name.replace("\\", "/")
    if n.startswith("/"):
        return False, "absolute path"
    if ":" in n.split("/")[0]:
        return False, "drive-letter path"
    parts = n.split("/")
    if any(p in ("..", ".") for p in parts):
        return False, "traversal segment"
    if any(p.strip() == "" for p in parts):
        return False, "empty path segment"
    if os.path.basename(n) in NEVER_IMPORT:
        return False, "never imported"
    return True, ""


def _resolve_within(root: Path, rel: str) -> Optional[Path]:
    """Join and CONFIRM the result is inside root. Belt to the name check's
    braces: the string test can be fooled, a resolved-path comparison cannot."""
    try:
        target = (root / rel).resolve()
        rootr = root.resolve()
        if target == rootr or str(target).startswith(str(rootr) + os.sep):
            return target
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Inspection -- always before writing
# ---------------------------------------------------------------------------

def inspect_archive(zip_path) -> Dict:
    """Describe an archive without touching a single destination file."""
    p = Path(zip_path)
    if not p.exists():
        return {"ok": False, "error": "no such file"}
    if not zipfile.is_zipfile(str(p)):
        return {"ok": False, "error": "not a zip archive"}

    out: Dict = {"ok": True, "path": str(p), "sections": [], "warnings": [],
                 "has_key": False, "key_style": None, "needs_passphrase": False,
                 "mode": "unknown", "created": None,
                 "skipped": [], "total_bytes": 0, "file_count": 0}
    sections: Dict[str, Dict] = {}
    total_unc = 0
    total_comp = 0

    try:
        with zipfile.ZipFile(str(p)) as z:
            names = z.namelist()
            if "MANIFEST.txt" in names:
                try:
                    txt = z.read("MANIFEST.txt").decode("utf-8", "replace")
                    out["manifest"] = txt[:4000]
                    for line in txt.splitlines():
                        if line.startswith("Mode "):
                            out["mode"] = line.split(":", 1)[-1].strip()
                        elif line.startswith("Created "):
                            out["created"] = line.split(":", 1)[-1].strip()
                    if "VeridianAI data export" not in txt:
                        out["warnings"].append(
                            "MANIFEST.txt is not in the expected format; this "
                            "may not be a VeridianAI export.")
                except Exception:
                    out["warnings"].append("MANIFEST.txt could not be read.")
            else:
                out["warnings"].append(
                    "No MANIFEST.txt -- this may not be a VeridianAI export. "
                    "Sections are inferred from the folder names.")

            for i in z.infolist():
                if i.is_dir():
                    continue
                total_unc += i.file_size
                total_comp += max(i.compress_size, 1)
                if i.filename == "KEY/fernet.key":
                    out["has_key"] = True
                    out["key_style"] = "raw"
                    continue
                if i.filename == KEY_WRAPPED:
                    # The key is here, wrapped. Say so BEFORE anything is
                    # written, so the UI can ask for the passphrase rather
                    # than discovering the need halfway through a restore.
                    out["has_key"] = True
                    out["key_style"] = "passphrase"
                    out["needs_passphrase"] = True
                    continue
                if i.filename == "MANIFEST.txt":
                    continue
                safe, why = _entry_is_safe(i.filename)
                if not safe:
                    out["skipped"].append({"name": i.filename, "reason": why})
                    continue
                sec = i.filename.replace("\\", "/").split("/")[0]
                s = sections.setdefault(sec, {"key": sec, "files": 0, "bytes": 0})
                s["files"] += 1
                s["bytes"] += i.file_size
    except Exception as e:
        return {"ok": False, "error": "archive unreadable: %s" % e}

    out["total_bytes"] = total_unc
    out["file_count"] = sum(s["files"] for s in sections.values())

    if total_unc > MAX_TOTAL_BYTES:
        return {"ok": False,
                "error": "archive expands to %.1f GB, above the %.0f GB limit"
                         % (total_unc / 1e9, MAX_TOTAL_BYTES / 1e9)}
    if total_comp and (total_unc / total_comp) > MAX_RATIO:
        return {"ok": False,
                "error": "compression ratio %.0f:1 exceeds the %d:1 limit "
                         "(possible zip bomb)" % (total_unc / total_comp, MAX_RATIO)}

    try:
        import data_export as _de
        labels = {k: v[0] for k, v in _de._roots(None).items()}
    except Exception:
        labels = {}
    for sec, s in sorted(sections.items()):
        s["label"] = labels.get(sec, sec)
        s["mergeable"] = sec not in UNMERGEABLE
        if sec in UNMERGEABLE:
            s["note"] = ("Cannot be merged -- a hash chain's value is its "
                         "unbroken sequence. It will be restored alongside as "
                         "a reference copy, not joined to yours.")
        out["sections"].append(s)

    if out["mode"] == "portable" and not out["has_key"]:
        out["warnings"].append(
            "This says it is a portable export but carries no key. Its "
            "contents cannot be decrypted and would be unreadable here.")
    if out["skipped"]:
        out["warnings"].append(
            "%d entr(y/ies) were refused for safety." % len(out["skipped"]))
    return out


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def _local_encrypt(blob: bytes, ns=None) -> bytes:
    import atrest
    return atrest.encrypt_bytes(blob, ns=ns)


def _translate(blob: bytes, src_fernet, ns=None) -> bytes:
    """Bring one file's encryption into this install.

    Encrypted with the source key -> decrypt, then re-encrypt with ours.
    Plain text -> encrypt with ours.
    Either way it lands protected by THIS machine's key, which is the whole
    point: copied bytes would be unreadable here.

    ``ns`` decides WHOSE key "ours" means. Importing into a profile without it
    would drop the bytes into that profile's folder encrypted under the system
    key -- readable, because decrypt falls back, and therefore easy to miss,
    but the profile's own key would not be protecting its own data.
    """
    import atrest
    if atrest.is_encrypted(blob):
        if src_fernet is None:
            raise ImportError_("encrypted content but no key was supplied")
        from cryptography.fernet import InvalidToken
        try:
            blob = src_fernet.decrypt(blob)
        except InvalidToken:
            raise ImportError_("the supplied key does not open this archive")
    return _local_encrypt(blob, ns=ns)


def restore(zip_path, ns=None, sections: Optional[List[str]] = None,
            *, mode: str = MERGE, dry_run: bool = False,
            passphrase: Optional[str] = None) -> Dict:
    """Restore chosen sections into `ns` (None = owner / shared store).

    mode=MERGE   existing files are kept unless the archive has the same path,
                 in which case the current one is backed up first.
    mode=REPLACE the section's destination is cleared first. Destructive, and
                 the caller is expected to have said so out loud.
    """
    info = inspect_archive(zip_path)
    if not info.get("ok"):
        return info
    chosen = set(sections or [s["key"] for s in info["sections"]])

    try:
        import data_export as _de
        roots = _de._roots(ns)
    except Exception as e:
        return {"ok": False, "error": "cannot resolve destinations: %s" % e}

    src_fernet = None
    written, skipped, backed_up, errors = 0, 0, 0, []
    reference_paths = []

    with zipfile.ZipFile(str(zip_path)) as z:
        if info.get("has_key"):
            try:
                from cryptography.fernet import Fernet
                if info.get("key_style") == "passphrase":
                    if not passphrase:
                        return {"ok": False, "needs_passphrase": True,
                                "error": "This archive's key is protected by a "
                                         "passphrase. Nothing has been changed."}
                    import json as _json
                    import keywrap as _kwrap
                    raw = _kwrap.unwrap_key_with_password(
                        _json.loads(z.read(KEY_WRAPPED).decode("utf-8")),
                        passphrase)
                    src_fernet = Fernet(raw.strip())
                else:
                    src_fernet = Fernet(z.read(KEY_RAW).strip())
            except Exception as e:
                # Wrong passphrase lands here too, and must not read as a
                # corrupt archive -- it is the one failure the person can fix.
                bad_pass = type(e).__name__ == "BadKey"
                return {"ok": False, "needs_passphrase": bad_pass,
                        "error": ("That passphrase does not open this archive. "
                                  "Nothing has been changed.") if bad_pass else
                                 ("the archive's key could not be loaded: %s" % e)}

        if mode == REPLACE and not dry_run:
            for sec in chosen:
                if sec in UNMERGEABLE or sec not in roots:
                    continue
                dest = roots[sec][1]
                try:
                    if dest.is_dir():
                        shutil.rmtree(dest, ignore_errors=True)
                    elif dest.exists():
                        dest.unlink()
                except Exception as e:
                    errors.append("could not clear %s: %s" % (sec, e))

        for i in z.infolist():
            if i.is_dir() or i.filename in ("MANIFEST.txt", KEY_RAW, KEY_WRAPPED):
                continue
            safe, _why = _entry_is_safe(i.filename)
            if not safe:
                skipped += 1
                continue
            parts = i.filename.replace("\\", "/").split("/")
            sec, rel = parts[0], "/".join(parts[1:]) or parts[0]
            if sec not in chosen:
                continue

            if sec in UNMERGEABLE:
                # Never joined. Written beside the real one, named so nobody
                # mistakes it for the live chain.
                base = roots.get(sec, (None, None))[1]
                if base is None:
                    skipped += 1
                    continue
                dest_root = Path(str(base) + "_imported_" +
                                 time.strftime("%Y%m%d%H%M%S"))
            elif sec in roots:
                dest_root = roots[sec][1]
                if dest_root.suffix and not dest_root.is_dir():
                    dest_root = dest_root.parent      # section is a single file
            else:
                skipped += 1
                continue

            target = _resolve_within(dest_root, rel)
            if target is None:
                skipped += 1
                errors.append("refused (escapes destination): %s" % i.filename)
                continue

            if dry_run:
                written += 1
                continue

            try:
                blob = z.read(i)
                if sec in ENCRYPTED_SECTIONS or sec in UNMERGEABLE:
                    blob = _translate(blob, src_fernet, ns=ns)
                elif src_fernet is not None:
                    import atrest
                    if atrest.is_encrypted(blob):
                        blob = _translate(blob, src_fernet, ns=ns)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and mode == MERGE:
                    bak = target.with_suffix(
                        target.suffix + ".%d.bak" % int(time.time()))
                    try:
                        shutil.copy2(target, bak)
                        backed_up += 1
                    except Exception:
                        pass
                target.write_bytes(blob)
                written += 1
                if sec in UNMERGEABLE and str(dest_root) not in reference_paths:
                    reference_paths.append(str(dest_root))
            except ImportError_ as e:
                errors.append("%s: %s" % (i.filename, e))
            except Exception as e:
                errors.append("%s: %s" % (i.filename, e))

    return {
        "ok": True, "dry_run": bool(dry_run), "mode": mode,
        "profile": ns or "(owner)",
        "written": written, "skipped": skipped, "backed_up": backed_up,
        "errors": errors[:20], "error_count": len(errors),
        "reference_copies": reference_paths,
        "note": ("Nothing was written -- this was a preview." if dry_run else
                 "Restart VeridianAI so it re-reads what changed."),
    }
