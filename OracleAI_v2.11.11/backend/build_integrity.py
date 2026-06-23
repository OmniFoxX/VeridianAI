"""OracleAI build provenance + integrity — signed build manifest.

Official builds ship a `build_manifest.json` signed with the maintainer's Ed25519
PRIVATE key (held only by OmniFoxX, never distributed). The PUBLIC key ships with
the app. At startup the app verifies the signature and re-hashes the shipped
files, so:
  * an official build can PROVE its authenticity, and
  * ANY edit — especially to the encryption (atrest / secret_locator) or CRAIID
    modules — is flagged as a MODIFIED, unofficial build.

This is tamper-EVIDENCE + provenance, not prevention: it can't stop someone
editing their own copy, but a fork cannot forge the maintainer's signature, so a
modified build self-identifies as not-official (and its public-key fingerprint
won't match the one OmniFoxX publishes at the canonical repo). That gives the
maintainer clean repudiation: "the official build's fingerprint is X; this isn't it."

CLI:
  python build_integrity.py keygen        # ONE-TIME: create the signing keypair
  python build_integrity.py genmanifest   # BUILD-TIME: hash + sign -> build_manifest.json
  python build_integrity.py verify         # check this install (prints JSON)
  python build_integrity.py selftest       # crypto round-trip + tamper test (temp dir)
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

CANONICAL_REPO = "https://github.com/OmniFoxX"
PRODUCT = "OracleAI"

# Once you run `keygen`, paste the printed fingerprint here to LOCK provenance:
# verify() will then mark a build "official" only if its shipped public key
# matches this fingerprint (so a swapped key reads as "foreign_key"). Leaving it
# blank still verifies the signature, just without pinning to your specific key.
OFFICIAL_FINGERPRINT = "486f75266989ccdab2ed8d64eea29297"

_THIS = Path(__file__).resolve()
ROOT = _THIS.parent.parent                 # project root
BACKEND = _THIS.parent
MANIFEST_PATH = ROOT / "build_manifest.json"
PUBKEY_PATH = BACKEND / "build_pubkey.pem"  # SHIPS with the app (public)

# Files whose modification matters most. Globs are relative to the project root.
SENSITIVE_GLOBS = [
    "backend/atrest.py",
    "backend/secret_locator.py",
    "backend/handoff_guard.py",
    "backend/build_integrity.py",
    "backend/craiid/*.py",
]

# What goes in the FULL manifest: shipped source/assets only — never data, secrets,
# models, or caches.
# Hash CODE / static assets only. Deliberately NOT .json/.txt/.md — those are
# runtime state (config.json, chat_memory.json) or docs that legitimately change,
# and hashing them caused false "modified" flags on normal use (enabling
# multiuser, creating a profile, even chatting).
INCLUDE_EXT = {".py", ".js", ".html", ".css", ".bat", ".ps1",
               ".webmanifest", ".vbs"}
EXCLUDE_DIRS = {"node_modules", "__pycache__", ".git", "dist", "build",
                "downloads", "archives", "logs", "uploads", "vlts_archives",
                "reconstructs", "OracleAI_Icon_files",
                # User-modifiable / runtime data — meant to change, never hashed:
                "skills", "memory_log", "models", "bundled_models", "prompts"}
# Never hash these specific files (generated / runtime state / self) — belt &
# suspenders in case a runtime extension is ever re-added to INCLUDE_EXT.
EXCLUDE_FILES = {"build_manifest.json", "config.json", "chat_memory.json",
                 "ui_prefs.json", ".backend_mode", "package-lock.json"}


def _data_dir() -> Path:
    try:
        if str(BACKEND) not in sys.path:
            sys.path.insert(0, str(BACKEND))
        from config import DATA_DIR
        return Path(DATA_DIR)
    except Exception:
        return ROOT.parent / "sage_data"


def _privkey_path() -> Path:
    # Private key lives in sage_data (OUTSIDE the project), like the Fernet key.
    return _data_dir() / ".oai_signing_key.pem"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if p.name in EXCLUDE_FILES:
            continue
        if p.suffix.lower() not in INCLUDE_EXT:
            continue
        yield rel


def _expand_sensitive(root: Path):
    out = []
    for pat in SENSITIVE_GLOBS:
        for p in sorted(root.glob(pat)):
            if p.is_file():
                out.append(str(p.relative_to(root)).replace("\\", "/"))
    return sorted(set(out))


def _fingerprint(pub) -> str:
    from cryptography.hazmat.primitives import serialization
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:32]


def _detect_version() -> str:
    for cand in (ROOT / "electron" / "package.json",):
        try:
            return str(json.loads(cand.read_text(encoding="utf-8")).get("version") or "0.0.0")
        except Exception:
            pass
    return "0.0.0"


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


# --------------------------------------------------------------------------- #
# keygen / genmanifest / verify
# --------------------------------------------------------------------------- #
def keygen(priv_path: Path = None, pub_path: Path = None, force: bool = False) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv_path = Path(priv_path or _privkey_path())
    pub_path = Path(pub_path or PUBKEY_PATH)
    if priv_path.exists() and not force:
        return {"status": "exists", "private_key": str(priv_path),
                "note": "use force=True to overwrite (INVALIDATES prior signatures)"}
    priv = Ed25519PrivateKey.generate()
    priv_path.parent.mkdir(parents=True, exist_ok=True)
    priv_path.write_bytes(priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    pub_path.parent.mkdir(parents=True, exist_ok=True)
    pub_path.write_bytes(priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    try:
        os.chmod(priv_path, 0o600)
    except Exception:
        pass
    return {"status": "created", "private_key": str(priv_path),
            "public_key": str(pub_path), "fingerprint": _fingerprint(priv.public_key())}


def genmanifest(root: Path = None, priv_path: Path = None, pub_path: Path = None,
                out_path: Path = None, version: str = None) -> dict:
    from cryptography.hazmat.primitives import serialization
    root = Path(root or ROOT)
    priv_path = Path(priv_path or _privkey_path())
    out_path = Path(out_path or MANIFEST_PATH)
    if not priv_path.exists():
        return {"status": "no_private_key", "expected": str(priv_path),
                "note": "run keygen first"}
    priv = serialization.load_pem_private_key(priv_path.read_bytes(), password=None)
    files = {str(rel).replace("\\", "/"): _sha256(root / rel) for rel in _iter_files(root)}
    body = {
        "schema": "oracleai_build_manifest", "manifest_version": 1,
        "product": PRODUCT, "version": version or _detect_version(),
        "canonical_repo": CANONICAL_REPO,
        "build_id": uuid.uuid4().hex,
        "built_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "fingerprint": _fingerprint(priv.public_key()),
        "integrity_sensitive": _expand_sensitive(root),
        "files": files,
    }
    sig = priv.sign(_canonical(body))
    out_path.write_text(json.dumps(
        {"manifest": body, "algo": "ed25519",
         "signature_b64": base64.b64encode(sig).decode("ascii")},
        indent=2), encoding="utf-8")
    return {"status": "written", "manifest": str(out_path), "files": len(files),
            "sensitive": len(body["integrity_sensitive"]), "fingerprint": body["fingerprint"]}


def verify(root: Path = None, manifest_path: Path = None, pub_path: Path = None) -> dict:
    root = Path(root or ROOT)
    manifest_path = Path(manifest_path or MANIFEST_PATH)
    pub_path = Path(pub_path or PUBKEY_PATH)
    res = {"status": "unknown", "product": PRODUCT, "canonical_repo": CANONICAL_REPO,
           "official": False, "signature_valid": False, "files_ok": True,
           "mismatches": [], "sensitive_modified": [], "pubkey_fingerprint": None}
    try:
        if not manifest_path.exists():
            res["status"] = "no_manifest"; return res
        if not pub_path.exists():
            res["status"] = "no_pubkey"; return res
        from cryptography.hazmat.primitives import serialization
        from cryptography.exceptions import InvalidSignature
        pub = serialization.load_pem_public_key(pub_path.read_bytes())
        fp = _fingerprint(pub)
        res["pubkey_fingerprint"] = fp
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        body = doc["manifest"]
        sig = base64.b64decode(doc["signature_b64"])
        res["version"] = body.get("version")
        res["build_id"] = body.get("build_id")
        try:
            pub.verify(sig, _canonical(body))
            res["signature_valid"] = True
        except InvalidSignature:
            res["signature_valid"] = False
        sensitive = set(body.get("integrity_sensitive", []))
        for rel, want in body.get("files", {}).items():
            p = root / rel
            got = _sha256(p) if p.exists() else None
            if got != want:
                res["files_ok"] = False
                res["mismatches"].append(rel)
                if rel in sensitive:
                    res["sensitive_modified"].append(rel)
        official_fp = OFFICIAL_FINGERPRINT.strip()
        res["fingerprint_pinned"] = bool(official_fp)
        res["fingerprint_matches"] = (not official_fp) or (fp == official_fp)
        res["official"] = bool(res["signature_valid"] and res["files_ok"] and res["fingerprint_matches"])
        if not res["signature_valid"]:
            res["status"] = "signature_invalid"
        elif official_fp and fp != official_fp:
            res["status"] = "foreign_key"
        elif not res["files_ok"]:
            res["status"] = "modified"
        else:
            res["status"] = "official"
    except Exception as e:
        res["status"] = "error"; res["error"] = f"{type(e).__name__}: {e}"
    return res


def selftest() -> int:
    """Prove sign -> verify round-trip + tamper detection in a temp dir."""
    global OFFICIAL_FINGERPRINT
    _saved_fp = OFFICIAL_FINGERPRINT
    OFFICIAL_FINGERPRINT = ""  # test core logic independent of the pinned prod key
    import tempfile
    d = Path(tempfile.mkdtemp())
    root = d / "proj"; (root / "backend" / "craiid").mkdir(parents=True)
    (root / "backend" / "atrest.py").write_text("# fake atrest\nX=1\n", encoding="utf-8")
    (root / "backend" / "craiid" / "core.py").write_text("# fake craiid\nY=2\n", encoding="utf-8")
    (root / "app.js").write_text("console.log('hi')\n", encoding="utf-8")
    priv = d / "priv.pem"; pub = root / "backend" / "build_pubkey.pem"
    mani = root / "build_manifest.json"
    k = keygen(priv_path=priv, pub_path=pub)
    g = genmanifest(root=root, priv_path=priv, out_path=mani)
    v1 = verify(root=root, manifest_path=mani, pub_path=pub)
    # tamper a sensitive file
    (root / "backend" / "atrest.py").write_text("# fake atrest\nX=666  # evil\n", encoding="utf-8")
    v2 = verify(root=root, manifest_path=mani, pub_path=pub)
    print("keygen:", k.get("status"), "fp:", k.get("fingerprint"))
    print("genmanifest:", g.get("status"), "files:", g.get("files"))
    print("verify (clean):", v1.get("status"), "| sig:", v1.get("signature_valid"), "| files_ok:", v1.get("files_ok"))
    print("verify (tampered atrest):", v2.get("status"), "| sensitive_modified:", v2.get("sensitive_modified"))
    ok = (v1["status"] == "official" and v1["signature_valid"]
          and v2["status"] == "modified" and "backend/atrest.py" in v2["sensitive_modified"])
    print("SELFTEST:", "PASS" if ok else "FAIL")
    OFFICIAL_FINGERPRINT = _saved_fp
    return 0 if ok else 1


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else "verify"
    if cmd == "keygen":
        print(json.dumps(keygen(force=("--force" in argv)), indent=2))
    elif cmd == "genmanifest":
        print(json.dumps(genmanifest(), indent=2))
    elif cmd == "verify":
        print(json.dumps(verify(), indent=2))
    elif cmd == "selftest":
        return selftest()
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
