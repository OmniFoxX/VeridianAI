#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_export.py -- let people take their own data out.

WHY
---
Everything VeridianAI stores is Fernet-encrypted at rest. That is the right
default for a memory tool: the chain is tamper-evident and the contents are
unreadable to anything that is not this application.

It also meant there was no way to get your data OUT. Printing produced an
encrypted blob. Copying the folder produced files nothing else can open. For an
application whose entire premise is that the data belongs to the user, "yours,
but only in here" is not ownership.

TWO MODES, BECAUSE THEY ARE DIFFERENT JOBS
------------------------------------------
READABLE   Decrypted. Plain .json / .md / .txt / original uploads.
           For reading, printing, keeping, or moving to another tool.
           Loses the tamper-evidence -- deliberately, knowingly, on request.

PORTABLE   The encrypted blobs exactly as stored, PLUS the key.
           For moving to another machine with integrity intact. The memory
           chain stays verifiable, which a decrypted copy can never be.

The second is the one people forget to build. A plaintext backup of a
tamper-evident log is no longer tamper-evident, and someone migrating machines
should not have to give that up.

WHY PORTABLE IS OWNER-ONLY
--------------------------
The Fernet key is APP-WIDE (atrest._key_path -> sage_data), not per-user. In a
multi-user install, handing a non-owner the key hands them the key protecting
everyone else's data too. Their own data they may take freely -- in readable
form, decrypted, scoped to their namespace. The shared key is not theirs to
export.

WHAT IS NEVER INCLUDED
----------------------
`.api_keystore.json` -- a live credential, not user content. Someone restoring
on a new machine should rotate rather than transplant it. Nothing that
functions as a password travels in an export the user might email to themselves.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MODE_READABLE = "readable"
MODE_PORTABLE = "portable"

# Never exported, in any mode. A credential is not content.
NEVER_EXPORT = {".api_keystore.json", ".oracle_pids.json", ".backend_mode"}


# --------------------------------------------------------------------------
def _roots(ns=None) -> Dict[str, Tuple[str, Path]]:
    """section -> (human label, directory or file).

    Resolved through the same helpers the app writes with, so a section can
    never point somewhere the data is not.
    """
    out: Dict[str, Tuple[str, Path]] = {}
    try:
        import sage_engine
        from state_paths import STATE_DIR
        mem = Path(sage_engine._memory_file(ns))
        out["chat"] = ("Current conversation", mem)
        out["archives"] = ("Saved conversations", Path(sage_engine._archive_folder(ns)))
        out["evidence"] = ("Research sources (CRAIID)", mem.parent / ".evidence_ledger.dat")
        base = Path(STATE_DIR)
    except Exception:
        return out

    try:
        from config import MEMORY_DIR, PROCEDURAL_DIR, SNAPSHOT_DIR, UPLOAD_DIR
        out["memory_chain"] = ("Memory chain (tamper-evident log)", Path(MEMORY_DIR))
        out["procedural"] = ("Learned procedures", Path(PROCEDURAL_DIR))
        out["uploads"] = ("Files you uploaded", Path(UPLOAD_DIR))
        out["snapshots"] = ("Snapshots", Path(SNAPSHOT_DIR))
    except Exception:
        pass

    out["downloads"] = ("Files VeridianAI created", base / "downloads")
    out["docs"] = ("Documentation", base / "docs")
    out["config"] = ("Settings", base / "config.json")
    return out


def _files_under(p: Path) -> List[Path]:
    if not p.exists():
        return []
    if p.is_file():
        return [p] if p.name not in NEVER_EXPORT else []
    out = []
    for f in p.rglob("*"):
        try:
            if f.is_file() and f.name not in NEVER_EXPORT:
                out.append(f)
        except Exception:
            continue
    return out


def inventory(ns=None) -> Dict:
    """What is available to export, with sizes.

    The checklist shows this BEFORE the user picks. Uploads and snapshots can
    be gigabytes; someone choosing "everything" should be choosing it knowingly
    rather than discovering it as a stalled progress bar.
    """
    out = {"sections": [], "total_bytes": 0}
    for key, (label, root) in _roots(ns).items():
        files = _files_under(root)
        size = 0
        enc = 0
        for f in files:
            try:
                size += f.stat().st_size
                with open(f, "rb") as fh:
                    head = fh.read(8)
                if head.lstrip()[:6] == b"gAAAAA":
                    enc += 1
            except Exception:
                continue
        out["sections"].append({
            "key": key, "label": label, "files": len(files),
            "bytes": size, "encrypted_files": enc,
            "present": bool(files),
        })
        out["total_bytes"] += size
    return out


# --------------------------------------------------------------------------
def _safe_arcname(section: str, root: Path, f: Path) -> str:
    """Zip entry name, contained under the section folder.

    Built from a relative_to() and then sanitised: a zip is an archive format
    with a documented traversal problem, and "these are our own files" is the
    assumption behind most of the CVEs.
    """
    try:
        rel = f.relative_to(root) if root.is_dir() else Path(f.name)
    except Exception:
        rel = Path(f.name)
    parts = [p for p in rel.parts if p not in ("..", "/", "\\") and ":" not in p]
    return "/".join([section] + parts) if parts else f"{section}/{f.name}"


def build(ns=None, mode: str = MODE_READABLE,
          sections: Optional[List[str]] = None,
          is_owner: bool = True) -> Dict:
    """Build the export zip. Returns {ok, filename, path, bytes, mode, notes}."""
    notes: List[str] = []
    try:
        mode = (mode or MODE_READABLE).strip().lower()
        if mode not in (MODE_READABLE, MODE_PORTABLE):
            return {"ok": False, "error": f"unknown mode {mode!r}"}
        if mode == MODE_PORTABLE and not is_owner:
            # See the module docstring: the key is app-wide.
            return {"ok": False, "error":
                    "Portable export includes the encryption key, which "
                    "protects every profile on this install. Use a readable "
                    "export for your own data."}

        roots = _roots(ns)
        chosen = [s for s in (sections or list(roots)) if s in roots]
        if not chosen:
            return {"ok": False, "error": "no sections selected"}

        from state_paths import STATE_DIR
        outdir = Path(STATE_DIR) / "downloads"
        outdir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        name = f"veridianai-export-{mode}-{stamp}.zip"
        target = outdir / name

        try:
            import atrest
        except Exception:
            atrest = None
            if mode == MODE_READABLE:
                notes.append("Encryption module unavailable - encrypted files "
                             "were copied as-is and will not be readable.")

        counted = decrypted = failed = 0
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            for sec in chosen:
                label, root = roots[sec]
                for f in _files_under(root):
                    arc = _safe_arcname(sec, root, f)
                    try:
                        if mode == MODE_READABLE and atrest is not None:
                            data = atrest.read_file_auto(f)   # decrypts if needed
                            with open(f, "rb") as fh:
                                was_enc = fh.read(8).lstrip()[:6] == b"gAAAAA"
                            if was_enc:
                                decrypted += 1
                        else:
                            data = f.read_bytes()             # verbatim
                        z.writestr(arc, data)
                        counted += 1
                    except Exception:
                        failed += 1
                        continue

            if mode == MODE_PORTABLE:
                # The key travels WITH the data, or the data is a brick.
                try:
                    import atrest as _a
                    kp = Path(_a._key_path())
                    if kp.exists():
                        z.writestr("KEY/fernet.key", kp.read_bytes())
                        notes.append("Includes the encryption key. Treat this "
                                     "file like a password.")
                    else:
                        notes.append("WARNING: no encryption key found - the "
                                     "encrypted contents cannot be restored.")
                except Exception as e:
                    notes.append(f"WARNING: key not included ({e}). The "
                                 f"encrypted contents cannot be restored.")

            z.writestr("MANIFEST.txt", _manifest(mode, chosen, roots, counted,
                                                 decrypted, failed, notes))

        size = target.stat().st_size
        print(f"[export] {name}: {counted} file(s), {size/1048576:.1f} MB, "
              f"mode={mode}, decrypted={decrypted}, failed={failed}", flush=True)
        return {"ok": True, "filename": name, "path": str(target),
                "bytes": size, "files": counted, "decrypted": decrypted,
                "failed": failed, "mode": mode, "notes": notes}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _manifest(mode, chosen, roots, counted, decrypted, failed, notes) -> str:
    lines = [
        "VeridianAI data export",
        "=" * 60,
        f"Created : {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Mode    : {mode}",
        f"Files   : {counted}" + (f"  ({failed} could not be read)" if failed else ""),
        "",
    ]
    if mode == MODE_READABLE:
        lines += [
            "READABLE EXPORT",
            "-" * 60,
            f"{decrypted} encrypted file(s) were DECRYPTED for this export.",
            "",
            "These files are now plain text. Anyone who can open this zip can",
            "read them. The memory chain in here is a COPY and is no longer",
            "tamper-evident: its integrity guarantee lived in the encrypted",
            "original, not in the words. Keep it somewhere you would keep any",
            "other personal document.",
            "",
            "To move VeridianAI to another machine WITH that guarantee intact,",
            "use a portable export instead.",
        ]
    else:
        lines += [
            "PORTABLE EXPORT",
            "-" * 60,
            "Contents are still encrypted, exactly as stored. KEY/fernet.key",
            "is the key that opens them.",
            "",
            "TREAT THE KEY LIKE A PASSWORD. Anyone holding both the key and",
            "this archive can read everything in it.",
            "",
            "To restore: copy the folders into the new install's sage_data",
            "directory, and put fernet.key where the new install keeps its own",
            "(sage_data root). The Open Data Folder button in Settings shows",
            "you exactly where that is.",
        ]
    lines += ["", "Sections included", "-" * 60]
    for s in chosen:
        lines.append(f"  {s:14} {roots[s][0]}")
    if notes:
        lines += ["", "Notes", "-" * 60] + [f"  - {n}" for n in notes]
    lines += ["", "Not included", "-" * 60,
              "  .api_keystore.json  (a live credential -- rotate it on the new",
              "                       machine rather than carrying it across)"]
    return "\n".join(lines) + "\n"
