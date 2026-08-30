"""report_issue.py -- let a person report AI-generated content that went wrong.

WHY THIS EXISTS

The Microsoft Store rejected v2.16.1 for one reason: VeridianAI documented an
email address to report problems and provided no MECHANISM inside the app. A
way written in a manual is not a way. This is the mechanism.

WHAT IT IS NOT

It is not telemetry. Nothing here runs on its own, nothing is uploaded, and no
network call is made from this module at all. It writes ONE readable file into
the person's own downloads folder and tells them where it is. Sending it is a
separate act they perform themselves, with the file in front of them.

That is a deliberate choice for an app that keeps health information on a local
machine. An automatic reporter would be a channel out of the building that the
person did not open, which is exactly what this product promises not to have.

THE LEAST DATA THAT CAN STILL FIX THE PROBLEM

Todd: "I don't want any more data than the absolute least that is needed to
rectify whatever the issue was." <- Verbatim & chain-witnessed I said that (TD)

So the default report is the flagged reply plus the facts needed to reproduce
it -- version, build, OS, backend, model. Nothing else. The prompt that caused
it, earlier turns, the model's reasoning trace: all useful, all OPT-IN, all
unticked, because they are the person's own words and this app's users may be
carrying patient information in them.

The flagged text arrives from the caller rather than being read out of storage.
The dialog shows the person exactly that text and lets them edit it before it
is written, so what lands in the file is what they read and approved -- not
whatever the server decided to include on their behalf.

READABLE ON PURPOSE

Markdown, not an encrypted bundle. The person is about to email this to a
stranger; they are entitled to open it first and see every word. An opaque
attachment asks for trust that this product does not ask for anywhere else.
"""
from __future__ import annotations

import io
import json
import os
import platform
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

# Where reports are sent. Also printed inside the file, so the copy on disk
# still says where it was meant to go long after the dialog is closed.
SUPPORT_EMAIL = "Todd@MentiSphereSoftware.com"
SUPPORT_SUBJECT = "VeridianAI content report"

# Everything the caller may ask to include. The first is always present; the
# rest are opt-in and default OFF. Declared here rather than inferred from the
# payload so an unknown key cannot quietly widen what gets written.
OPTIONAL_PARTS = ("prompt", "context", "reasoning", "environment_detail")

_MAX_FIELD = 20000        # per text field, generous for a reply, bounded
_MAX_CONTEXT_TURNS = 6


def _clip(text, limit=_MAX_FIELD) -> str:
    s = "" if text is None else str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + "\n\n[...truncated at %d characters by VeridianAI...]" % limit


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")


def _environment(detail: bool = False) -> Dict[str, str]:
    """The facts needed to reproduce a content problem, and no others.

    Deliberately NOT collected: username, machine name, IP, file paths, install
    location, hardware serials. None of them help explain why a model generated
    something, and every one of them identifies a person or a machine.
    """
    env = {}
    try:
        from state_paths import PROJECT_DIR
        _mf = Path(PROJECT_DIR) / "build_manifest.json"
        if _mf.exists():
            _doc = json.loads(_mf.read_text(encoding="utf-8"))
            _m = _doc.get("manifest") or {}
            env["VeridianAI version"] = str(_m.get("version", "unknown"))
            env["Build id"] = str(_m.get("build_id", "unknown"))
    except Exception:
        pass
    env.setdefault("VeridianAI version", "unknown")
    env["Operating system"] = "%s %s" % (platform.system(), platform.release())
    if detail:
        # Only on request. The architecture and Python build occasionally
        # matter for a crash; they never matter for "the model generated something
        # wrong", which is what this feature is for.
        env["Architecture"] = platform.machine()
        env["Python"] = platform.python_version()
    return env


def build(flagged: str,
          description: str = "",
          model: str = "",
          backend: str = "",
          include: Optional[Dict] = None,
          prompt: str = "",
          context_turns: Optional[List[Dict]] = None,
          reasoning: str = "",
          downloads_dir=None,
          ns=None) -> Dict:
    """Write the report file. Returns {ok, path, filename, bytes, included}.

    `include` is the person's tickbox state. An absent or false key means the
    section is omitted -- the check is for an explicit true, so a malformed
    payload errs towards writing LESS, never more.
    """
    include = include or {}

    def wants(part: str) -> bool:
        return part in OPTIONAL_PARTS and include.get(part) is True

    included = ["flagged reply", "app and model details"]
    L: List[str] = []
    A = L.append

    A("# VeridianAI - content report")
    A("")
    A("Generated %s by the person using VeridianAI, from the Report button "
      "in the app." % _stamp())
    A("")
    A("This file was written locally and sent by nobody. If you are reading "
      "it, someone chose to send it to you.")
    A("")
    A("Send to: %s" % SUPPORT_EMAIL)
    A("")
    A("---")
    A("")

    A("## What the person said was wrong")
    A("")
    A(_clip(description).strip() or "_(no description given)_")
    A("")

    A("## The reply being reported")
    A("")
    A("> Shown to the person verbatim before sending, and editable by them. "
      "What follows is what they approved.")
    A("")
    A("```")
    A(_clip(flagged).rstrip() or "(empty)")
    A("```")
    A("")

    A("## App and model")
    A("")
    for k, v in _environment(detail=wants("environment_detail")).items():
        A("- **%s:** %s" % (k, v))
    if model:
        A("- **Model:** %s" % _clip(model, 300))
    if backend:
        A("- **Backend:** %s" % _clip(backend, 120))
    if wants("environment_detail"):
        included.append("extra environment detail")
    A("")

    if wants("prompt"):
        included.append("the prompt that produced it")
        A("## The prompt that produced it")
        A("")
        A("```")
        A(_clip(prompt).rstrip() or "(empty)")
        A("```")
        A("")

    if wants("context") and context_turns:
        included.append("earlier turns (%d)" % min(len(context_turns),
                                                   _MAX_CONTEXT_TURNS))
        A("## Earlier turns, for context")
        A("")
        for turn in list(context_turns)[-_MAX_CONTEXT_TURNS:]:
            role = str((turn or {}).get("role", "?"))
            A("**%s:**" % role)
            A("")
            A("```")
            A(_clip(str((turn or {}).get("content", "")), 4000).rstrip())
            A("```")
            A("")

    if wants("reasoning") and reasoning:
        included.append("the model's reasoning trace")
        A("## The model's reasoning trace")
        A("")
        A("> The model's own working, including steps it discarded. Not an "
          "explanation of the answer.")
        A("")
        A("```")
        A(_clip(reasoning).rstrip())
        A("```")
        A("")

    A("---")
    A("")
    A("## What is NOT in this file")
    A("")
    A("Written down so the person can check it, and so the person receiving "
      "it knows what they did not get:")
    A("")
    A("- no username, machine name, IP address or install path")
    A("- no other conversations, archives, uploads or documents")
    A("- no other profile's data of any kind")
    A("- no credentials, keys or API tokens")
    A("- nothing that was not listed on the consent screen and ticked")
    A("")
    A("Sections included in this report: %s." % ", ".join(included))
    A("")

    body = "\n".join(L).rstrip() + "\n"

    # Written beside the person's other generated files, per profile, which is
    # the folder the app already teaches them how to open.
    try:
        outdir = Path(downloads_dir) if downloads_dir else Path.cwd()
        outdir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"ok": False, "error": "could not open the downloads folder: %s"
                % type(e).__name__}

    name = "veridianai-content-report-%s.md" % time.strftime("%Y%m%d-%H%M%S")
    target = outdir / name
    try:
        target.write_text(body, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": "could not write the report: %s"
                % type(e).__name__}

    return {"ok": True, "filename": name, "path": str(target),
            "bytes": len(body.encode("utf-8")), "included": included,
            "support_email": SUPPORT_EMAIL, "subject": SUPPORT_SUBJECT}
