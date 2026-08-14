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
import os
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_EXPORT_PREFIX = "veridianai-export-"

MODE_READABLE = "readable"
MODE_PORTABLE = "portable"

# Never exported, in any mode. A credential is not content.
# .key_migration.json is a statement about this install's own files. Carried
# elsewhere it would tell the destination a conversion had already happened.
NEVER_EXPORT = {".api_keystore.json", ".oracle_pids.json", ".backend_mode",
                ".key_migration.json"}


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

    profile_root = None
    try:
        _pr = sage_engine.user_data_dir(ns)
        profile_root = Path(_pr) if _pr else None
    except Exception:
        profile_root = None

    if profile_root is not None:
        # A PROFILE exports what lives in its own directory, and nothing else.
        #
        # This used to fall through to the shared paths below for every
        # section but chat/archives/evidence, so a non-owner pressing "export
        # my data" received the OWNER's memory chain, procedural memory and
        # uploads in plain text -- and, because the shared downloads folder is
        # where exports are written, any previous PORTABLE export nested
        # inside, which carries KEY/fernet.key: the app-wide key protecting
        # every profile on the install.
        out["procedural"] = ("Learned procedures", profile_root / "procedural_memory")
        out["uploads"] = ("Files you uploaded", profile_root / "uploads")
        out["downloads"] = ("Files VeridianAI created", profile_root / "downloads")
    else:
        try:
            from config import MEMORY_DIR, PROCEDURAL_DIR, SNAPSHOT_DIR, UPLOAD_DIR
            out["memory_chain"] = ("Memory chain (tamper-evident log)", Path(MEMORY_DIR))
            out["procedural"] = ("Learned procedures", Path(PROCEDURAL_DIR))
            out["uploads"] = ("Files you uploaded", Path(UPLOAD_DIR))
            out["snapshots"] = ("Snapshots", Path(SNAPSHOT_DIR))
        except Exception:
            pass
        from state_paths import DOWNLOADS_DIR as _DL
        out["downloads"] = ("Files VeridianAI created", Path(_DL))
        out["docs"] = ("Documentation", base / "docs")
        out["config"] = ("Settings", base / "config.json")

    if profile_root is not None:
        # Belt and braces. The block above is correct today; this makes it
        # impossible for a later edit to reopen the hole by adding a section
        # that resolves somewhere shared. One rule, checked, not remembered.
        out = {k: v for k, v in out.items() if _within(profile_root, v[1])}
    return out


def _within(base: Path, p: Path) -> bool:
    """Is `p` inside `base`? Resolved first, so symlinks and .. cannot lie."""
    try:
        b = os.path.realpath(str(base))
        q = os.path.realpath(str(p))
        return q == b or q.startswith(b + os.sep)
    except Exception:
        return False


def _files_under(p: Path) -> List[Path]:
    """Every real file under `p`, for export.

    v2.15 containment: SYMLINKS ARE SKIPPED, and every survivor is confirmed to
    resolve inside `p`.

    `is_file()` follows links. Without this, a symlink planted inside a
    profile's directory would be followed into whatever it points at -- another
    profile's store, the recovery key, anywhere the backend can read -- and
    packed into that profile's export. That is the same shape as the
    export-containment leak this module already exists to prevent, arriving by a
    different route. Planting one needs filesystem access rather than the app,
    which is why it is hardening rather than an open hole; it is cheap to close
    and expensive to notice later.

    A symlink is skipped rather than resolved-and-included: there is no case
    where a link is legitimate export content, and following one would put a
    file in the archive under a path that is not where it actually lives.
    """
    if not p.exists():
        return []
    if p.is_symlink():
        return []
    if p.is_file():
        return [p] if p.name not in NEVER_EXPORT else []
    try:
        root = p.resolve()
    except OSError:
        return []
    out = []
    # rglob does not descend into symlinked DIRECTORIES on Python 3.13+
    # (recurse_symlinks defaults to False); the per-entry checks below cover
    # older interpreters and the file case either way.
    for f in p.rglob("*"):
        try:
            if f.is_symlink():
                continue
            if not f.is_file() or f.name in NEVER_EXPORT:
                continue
            rp = f.resolve()
            if rp != root and root not in rp.parents:
                continue          # resolved outside the profile's own directory
            # Never pack an export inside an export. The downloads folder is
            # where exports land, so without this each one swallows the last
            # -- and a portable one carries a key with it.
            if f.name.startswith(_EXPORT_PREFIX) and f.suffix.lower() == ".zip":
                continue
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
          is_owner: bool = True,
          export_key: Optional[bytes] = None,
          passphrase: Optional[str] = None) -> Dict:
    """Build the export zip. Returns {ok, filename, path, bytes, mode, notes}.

    `export_key` is the PROFILE's own data key, supplied by the caller from the
    unlocked session. Its presence is what makes a portable export possible for
    a non-owner: the key that travels opens her data and nothing else, where
    the app-wide key would have opened everyone's.

    `passphrase`, if given, means the archive carries a WRAP of that key rather
    than the key itself -- so the file is inert to anyone who does not know the
    passphrase. Optional by decision: an export nobody can open is also a way
    to lose your own data, and the person choosing gets to weigh that.
    """
    notes: List[str] = []
    try:
        mode = (mode or MODE_READABLE).strip().lower()
        if mode not in (MODE_READABLE, MODE_PORTABLE):
            return {"ok": False, "error": f"unknown mode {mode!r}"}
        if mode == MODE_PORTABLE and not is_owner and not export_key:
            # Was flatly refused, because the only key on offer was app-wide.
            # Now it is refused only when we do not hold HER key -- which
            # means her profile is locked, and the honest answer is to say so.
            return {"ok": False, "error":
                    "A portable export has to carry the key that opens it, and "
                    "your profile's key is not unlocked in this session. Sign "
                    "in again, or take a readable export instead."}

        roots = _roots(ns)
        chosen = [s for s in (sections or list(roots)) if s in roots]
        if not chosen:
            return {"ok": False, "error": "no sections selected"}

        # DOWNLOADS_DIR, not STATE_DIR/downloads: exports are user content and
        # belong in sage_data with the rest of it. Writing them into the install
        # directory is what put 35 stale zips there.
        from state_paths import DOWNLOADS_DIR
        outdir = Path(DOWNLOADS_DIR)
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
                            # ns: a readable export must decrypt with the
                            # profile's OWN key, not the system one.
                            data = atrest.read_file_auto(f, ns=ns)
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
                    key_bytes = None
                    if export_key:
                        # A profile's own key. Converted through atrest so the
                        # shape matches what the import side will hand Fernet.
                        key_bytes = _a.fernet_key_bytes(export_key)
                    else:
                        kp = Path(_a._key_path())
                        if kp.exists():
                            key_bytes = kp.read_bytes()
                    if key_bytes and passphrase:
                        import json as _json
                        import keywrap as _kwrap
                        z.writestr("KEY/key.wrapped.json", _json.dumps(
                            _kwrap.wrap_key_with_password(key_bytes.strip(),
                                                          passphrase),
                            indent=2))
                        notes.append("The key is wrapped with your passphrase. "
                                     "The zip itself still opens -- it is an "
                                     "ordinary archive -- but its contents stay "
                                     "encrypted and cannot be decrypted without "
                                     "that passphrase, by anyone, including you.")
                    elif key_bytes:
                        z.writestr("KEY/fernet.key", key_bytes)
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
                "failed": failed, "mode": mode, "notes": notes,
                "protected": bool(passphrase and mode == MODE_PORTABLE),
                "scope": "profile" if ns else "owner"}
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
            "Contents are still encrypted, exactly as stored, and the key",
            "that opens them travels in KEY/.",
            "",
            "  KEY/fernet.key        the key itself. TREAT IT LIKE A PASSWORD:",
            "                        anyone holding this archive can read it.",
            "  KEY/key.wrapped.json  the key wrapped with the passphrase you",
            "                        chose. The archive is inert without it,",
            "                        and there is no way to recover it -- not",
            "                        by us, not by anyone.",
            "",
            "Only one of those two is present, depending on what you chose.",
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
