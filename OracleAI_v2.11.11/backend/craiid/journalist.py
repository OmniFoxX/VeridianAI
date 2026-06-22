#!/usr/bin/env python3
"""
CRAIID Journalist (#69) - phase 1: the JANITOR.
================================================

The Journalist's original role is the real-time editor/summarizer: strip noise
and off-topic turns, extract the running theme, and produce a clean theme-
focused summary for the Archivist + Author. That summarizer is phase 2 (it will
use journalist_ops_detector + the embed tier). This file is phase 1: the
janitor half of the same judgment - "what is worth keeping vs. the noise worth
shedding" - which is what keeps OracleAI healthy over LONG uptimes and through
human "moments of too much data".

It bounds the unbounded growth points with CONSERVATIVE, SAFE retention:

  * reconstructs/warm_instance_*.json  -> keep newest N (ephemeral handoff docs)
  * reconstructs/.tmp_warm_*           -> remove leftovers
  * vlts_archives/chunk_*.json         -> remove chunks ORPHANED by a newer key
  * mlm_training_data/daemon_calls.csv -> ROTATE (not delete) past a generous cap
                                          so training data is preserved, not lost
  * sage_data/*.rejected_*             -> remove quarantined forgeries older than N days

It deliberately does NOT touch: the source `archives/` (that IS the depth Todd
wants), the live `chat_memory.json`, or the hash-chained `handoff_audit.log`
(tamper-evidence; rotating it needs chain-aware care - left for later).

FULLY DEFENSIVE: every step is wrapped; a failure is recorded and skipped, never
raised. Empty/sparse install -> a clean no-op. Distribution-safe: paths self-
locate (no hardcoded drive/version), retention is env-tunable.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class Journalist:
    """Phase-1 Journalist: the janitor. Bounds growth; never raises."""

    def __init__(
        self,
        craiid_dir: Optional[Path] = None,
        backend_dir: Optional[Path] = None,
        sage_data_dir: Optional[Path] = None,
        keep_reconstructs: Optional[int] = None,
        mlm_max_lines: Optional[int] = None,
        reject_max_age_days: Optional[int] = None,
    ) -> None:
        here = Path(__file__).resolve()
        self.craiid_dir = Path(craiid_dir) if craiid_dir else here.parent
        if backend_dir:
            self.backend_dir = Path(backend_dir)
        else:
            self.backend_dir = next(
                (p for p in here.parents if p.name == "backend"), here.parent.parent
            )
        if sage_data_dir:
            self.sage_data_dir = Path(sage_data_dir)
        else:
            self.sage_data_dir = self._resolve_sage_data()

        self.reconstructs_dir = self.sage_data_dir / "craiid" / "reconstructs"
        self.vlts_dir = self.craiid_dir / "vlts_archives"
        self.mlm_csv = self.backend_dir / "mlm_training_data" / "daemon_calls.csv"

        # Conservative, env-tunable retention.
        self.keep_reconstructs = keep_reconstructs if keep_reconstructs is not None \
            else _env_int("JOURNALIST_KEEP_RECONSTRUCTS", 20)
        self.mlm_max_lines = mlm_max_lines if mlm_max_lines is not None \
            else _env_int("JOURNALIST_MLM_MAX_LINES", 50000)
        self.reject_max_age_days = reject_max_age_days if reject_max_age_days is not None \
            else _env_int("JOURNALIST_REJECT_AGE_DAYS", 14)

    def _resolve_sage_data(self) -> Path:
        try:
            import sys
            if str(self.backend_dir) not in sys.path:
                sys.path.insert(0, str(self.backend_dir))
            from config import DATA_DIR  # canonical sage_data
            return Path(DATA_DIR)
        except Exception:
            # config layout: <root>/sage_data sits beside the project root.
            return self.backend_dir.parent.parent / "sage_data"

    # ------------------------------------------------------------------ #
    #  Public entry point
    # ------------------------------------------------------------------ #
    def run_maintenance(self) -> Dict[str, Any]:
        """Run every retention step. Returns a report; NEVER raises."""
        report: Dict[str, Any] = {"ok": True, "actions": [], "errors": []}
        for step in (
            self._prune_reconstructs,
            self._prune_tmp_leftovers,
            self._prune_orphaned_vlts_chunks,
            self._rotate_mlm_csv,
            self._prune_aged_rejects,
        ):
            try:
                step(report)
            except Exception as e:  # a single step must never abort the rest
                report["errors"].append(f"{step.__name__}: {type(e).__name__}: {e}")
        report["ok"] = not report["errors"]
        return report

    # ------------------------------------------------------------------ #
    #  Steps
    # ------------------------------------------------------------------ #
    def _prune_reconstructs(self, report: Dict[str, Any]) -> None:
        if not self.reconstructs_dir.exists():
            return
        files = sorted(
            self.reconstructs_dir.glob("warm_instance_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for p in files[self.keep_reconstructs:]:
            if self._safe_unlink(p):
                removed += 1
        if removed:
            report["actions"].append(f"reconstructs: pruned {removed} (kept {self.keep_reconstructs})")

    def _prune_tmp_leftovers(self, report: Dict[str, Any]) -> None:
        removed = 0
        for d in (self.reconstructs_dir, self.vlts_dir):
            if not d.exists():
                continue
            for pat in (".tmp_warm_*", "*.tmp", "*.tmp.*"):
                for p in d.glob(pat):
                    if self._safe_unlink(p):
                        removed += 1
        if removed:
            report["actions"].append(f"tmp leftovers: removed {removed}")

    def _prune_orphaned_vlts_chunks(self, report: Dict[str, Any]) -> None:
        """Remove VLTS chunks left over from a PRIOR (larger) build, i.e. chunk
        files older than the current compression_key.json. The current build's
        chunks are written alongside (or after) the key, so they survive."""
        if not self.vlts_dir.exists():
            return
        key = self.vlts_dir / "compression_key.json"
        if not key.exists():
            return  # no key yet -> the worker hasn't built; leave everything
        key_mtime = key.stat().st_mtime
        removed = 0
        for p in self.vlts_dir.glob("chunk_*.json"):
            try:
                if p.stat().st_mtime < key_mtime - 1.0:  # 1s grace
                    if self._safe_unlink(p):
                        removed += 1
            except OSError:
                continue
        if removed:
            report["actions"].append(f"vlts: pruned {removed} orphaned chunks (older than key)")

    def _rotate_mlm_csv(self, report: Dict[str, Any]) -> None:
        """ROTATE (not truncate) the MLM training CSV past a generous cap so
        training data is bounded but never lost: keep the newest mlm_max_lines
        in the live file, append the overflow to daemon_calls.csv.archive."""
        f = self.mlm_csv
        if not f.exists():
            return
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except OSError:
            return
        if len(lines) <= self.mlm_max_lines:
            return
        overflow = lines[: len(lines) - self.mlm_max_lines]
        keep = lines[len(lines) - self.mlm_max_lines:]
        archive = f.with_name(f.name + ".archive")
        try:
            with open(archive, "a", encoding="utf-8") as af:
                af.writelines(overflow)
        except OSError as e:
            report["errors"].append(f"mlm archive append failed: {e}")
            return  # do NOT truncate if we couldn't preserve the overflow
        self._atomic_write_text(f, "".join(keep))
        report["actions"].append(
            f"mlm csv: rotated {len(overflow)} old rows to .archive (kept newest {self.mlm_max_lines})"
        )

    def _prune_aged_rejects(self, report: Dict[str, Any]) -> None:
        if not self.sage_data_dir.exists():
            return
        cutoff = time.time() - self.reject_max_age_days * 86400
        removed = 0
        for p in self.sage_data_dir.glob("*.rejected_*"):
            try:
                if p.stat().st_mtime < cutoff and self._safe_unlink(p):
                    removed += 1
            except OSError:
                continue
        if removed:
            report["actions"].append(
                f"quarantine: removed {removed} rejects older than {self.reject_max_age_days}d"
            )

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe_unlink(p: Path) -> bool:
        try:
            p.unlink()
            return True
        except OSError:
            return False

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        import secrets
        tmp = path.with_suffix(path.suffix + f".tmp.{secrets.token_hex(4)}")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    #  Phase 2: editor / summarizer (theme extraction)
    # ------------------------------------------------------------------ #
    _STOP = frozenset((
        "a an the and or but if then of to in on for with at by from as is are "
        "was were be been being this that these those it its i you he she we they "
        "me him her us them my your his our their do does did have has had not no "
        "so just very can could would should will shall may might must about into "
        "out up down over under again more most some such only own same than too"
    ).split())

    def _ops_terms(self) -> set:
        if getattr(self, "_ops_cache", None) is None:
            terms = set()
            try:
                lx = self.craiid_dir / "ops_lexicon.txt"
                if lx.exists():
                    terms = {ln.strip().lower()
                             for ln in lx.read_text(encoding="utf-8").splitlines()
                             if ln.strip()}
            except OSError:
                pass
            self._ops_cache = terms
        return self._ops_cache

    @staticmethod
    def _tok(text: str) -> List[str]:
        import re
        return [w.lower() for w in re.findall(r"\b\w+\b", text or "")]

    @staticmethod
    def _jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    @staticmethod
    def _render(turns: List[Dict], theme: List[str]) -> str:
        lines = []
        if theme:
            lines.append("Theme: " + ", ".join(theme[:8]))
        for t in turns:
            c = " ".join(str(t.get("content", "")).split())
            if len(c) > 280:
                c = c[:280] + "..."
            lines.append(f"- {t.get('role', '?')}: {c}")
        return "\n".join(lines)

    def summarize_stream(self, messages, max_turns: int = 12,
                         drop_below: float = 0.18, use_embed: bool = True,
                         use_llm: bool = True) -> Dict[str, Any]:
        """Strip noise/off-topic/redundant turns, extract the running THEME, and
        return a cleaned theme-focused summary. Lightweight NLP (TF-IDF + ops-
        lexicon bias), sharpened by the nomic embed tier when available. NEVER
        raises; sparse input -> trivial pass-through."""
        try:
            import math
            from collections import Counter
            turns = [{"role": str(m.get("role", "?")), "content": str(m.get("content", ""))}
                     for m in (messages or [])
                     if isinstance(m, dict) and str(m.get("content", "")).strip()]
            if len(turns) <= 2:
                return {"theme": [], "summary_turns": turns, "kept": len(turns),
                        "dropped_noise": 0, "method": "passthrough",
                        "text": self._render(turns, [])}
            toks = [self._tok(t["content"]) for t in turns]
            N = len(turns)
            df = Counter()
            for tk in toks:
                for w in set(tk):
                    if w not in self._STOP and len(w) > 2 and not w.isdigit():
                        df[w] += 1
            ops = self._ops_terms()
            # THEME = RECURRING terms (df>=2). A one-off word (df==1) is not a
            # theme - this keeps "weather"/"lunch" asides from polluting it.
            score = {}
            for w, d in df.items():
                if d < 2:
                    continue
                base = d * math.log((N + 1) / (d + 0.5))
                if w in ops:
                    base *= 1.6
                score[w] = base
            theme = [w for w, _ in sorted(score.items(), key=lambda x: x[1], reverse=True)[:12]]
            theme_set = set(theme)
            # coverage: how many DISTINCT theme terms each turn carries
            cov = [len(set(toks[i]) & theme_set) for i in range(N)]
            sims = None
            method = "lexical"
            if use_embed:
                emb = self._embed([t["content"][:1000] for t in turns])
                if emb:
                    method = "semantic"
                    cen = self._centroid(emb)
                    raw = [self._cos(e, cen) for e in emb]
                    smx = max(raw) or 1.0
                    sims = [x / smx for x in raw]
            # Keep a turn if it covers >=2 theme terms, OR is among the last 2
            # (hot recency), OR (semantic) is highly central; drop the rest as
            # off-topic noise; then drop near-duplicates.
            central_min = 0.6
            kept, kept_sets, dropped = [], [], 0
            for i in range(N):
                keep = cov[i] >= 2 or i >= N - 2 or (sims is not None and sims[i] >= central_min)
                if not keep:
                    dropped += 1
                    continue
                ts = set(toks[i])
                if any(self._jaccard(ts, k) > 0.85 for k in kept_sets):
                    dropped += 1
                    continue
                kept.append(i)
                kept_sets.append(ts)
            if len(kept) > max_turns:
                kept = kept[-max_turns:]   # keep the most recent if over the cap
            summary_turns = [turns[i] for i in kept]
            extractive_text = self._render(summary_turns, theme)
            result = {"theme": theme, "summary_turns": summary_turns,
                      "kept": len(summary_turns), "dropped_noise": dropped,
                      "method": method, "text": extractive_text,
                      "extractive_text": extractive_text}
            # Daemon-tier abstractive upgrade: hand the cleaned, theme-relevant
            # turns to the local Daemon LLM (11436) for a faithful prose summary.
            # On ANY failure (tier down/timeout/disabled) _llm_summarize returns
            # None and we keep the extractive text, so the warm-context summary
            # degrades gracefully. Env kill-switch: JOURNALIST_LLM_SUMMARY=0.
            _llm_on = (os.environ.get("JOURNALIST_LLM_SUMMARY", "1")
                       .strip().lower() not in ("0", "false", "no", "off"))
            if use_llm and _llm_on and summary_turns:
                prose = self._llm_summarize(summary_turns, theme)
                if prose:
                    result["llm_summary"] = prose
                    result["text"] = prose
                    result["method"] = method + "+llm"
            return result
        except Exception as e:
            recent = [{"role": str(m.get("role", "?")), "content": str(m.get("content", ""))[:400]}
                      for m in (messages or [])[-6:] if isinstance(m, dict)]
            return {"theme": [], "summary_turns": recent, "kept": len(recent),
                    "dropped_noise": 0, "method": f"error:{type(e).__name__}",
                    "text": self._render(recent, [])}

    # ---- nomic embed tier client (defensive) ----
    def _embed(self, texts):
        """Embed via the nomic embed tier (llama-server, OpenAI-compatible).
        Returns a list of vectors, or None on ANY failure (tier down, timeout,
        bad response) so the summarizer falls back to lexical scoring."""
        if not texts:
            return None
        try:
            import json as _json
            import os as _os
            import urllib.request
            try:
                from config import LLAMA_EMBED_URL as _url
            except Exception:
                _url = _os.environ.get("LLAMA_EMBED_URL", "http://127.0.0.1:11437")
            body = _json.dumps({"input": list(texts), "model": "nomic-embed-text"}).encode("utf-8")
            req = urllib.request.Request(
                _url.rstrip("/") + "/v1/embeddings", data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            vecs = [d.get("embedding") for d in data.get("data", [])]
            if vecs and len(vecs) == len(texts) and all(isinstance(v, list) and v for v in vecs):
                return vecs
            return None
        except Exception:
            return None

    @staticmethod
    def _centroid(vecs):
        dim = len(vecs[0])
        c = [0.0] * dim
        for v in vecs:
            for j in range(dim):
                c[j] += v[j]
        n = len(vecs)
        return [x / n for x in c]

    @staticmethod
    def _cos(a, b):
        import math
        m = min(len(a), len(b))
        dot = sum(a[j] * b[j] for j in range(m))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0

    # ---- Daemon tier (llama-server) call primitive + summarizer (defensive) ----
    def _daemon_chat(self, messages, temperature=0.3, max_tokens=None, timeout=None):
        """Low-level defensive call to the Daemon tier (llama-server, OpenAI
        /v1/chat/completions on 11436). Returns the assistant message content as
        a string, or None on ANY failure (tier down, timeout, bad response).
        This is the SHARED Daemon-tier primitive: _llm_summarize builds on it,
        and future features (e.g. Symposium moderation - Sage refereeing two
        opposing local models) can reuse it for arbitrary Daemon-tier turns.
        NEVER raises. timeout/max_tokens default from env but are per-call
        overridable so a long debate turn can ask for more room than a digest."""
        if not messages:
            return None
        try:
            import json as _json
            import os as _os
            import urllib.request
            try:
                from config import LLAMA_DAEMON_URL as _url
            except Exception:
                _url = _os.environ.get("LLAMA_DAEMON_URL", "http://127.0.0.1:11436")
            try:
                from config import MODEL_DAEMON_NAME as _model
            except Exception:
                _model = ""
            _model = _model or _os.environ.get("LLAMA_DAEMON_MODEL", "") or "daemon"
            if timeout is None:
                timeout = _env_int("JOURNALIST_LLM_TIMEOUT_SEC", 120)
            if max_tokens is None:
                max_tokens = _env_int("JOURNALIST_LLM_MAX_TOKENS", 256)
            payload = {
                "model": _model,
                "messages": list(messages),
                "stream": False,
                "temperature": temperature,
            }
            if max_tokens and int(max_tokens) > 0:
                payload["max_tokens"] = int(max_tokens)
            data = _json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                _url.rstrip("/") + "/v1/chat/completions", data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = _json.loads(resp.read().decode("utf-8"))
            choices = out.get("choices") or []
            if not choices:
                return None
            msg = (choices[0] or {}).get("message") or {}
            text = str(msg.get("content", "")).strip()
            return text or None
        except Exception:
            return None

    def _llm_summarize(self, turns, theme):
        """Abstractive summary via the Daemon tier: build a compact transcript of
        the already-cleaned, theme-relevant turns and ask the local Daemon model
        (through _daemon_chat) for a short faithful prose summary. Returns the
        summary string, or None on ANY failure so the caller keeps the extractive
        summary. NEVER raises."""
        if not turns:
            return None
        try:
            body_lines = []
            for t in turns:
                role = str(t.get("role", "?"))
                content = " ".join(str(t.get("content", "")).split())
                if len(content) > 600:
                    content = content[:600] + "..."
                body_lines.append(role + ": " + content)
            transcript = "\n".join(body_lines)
            theme_str = ", ".join(theme[:8]) if theme else "(none detected)"
            sys_prompt = (
                "You are a concise summarizer for an AI assistant's long-term "
                "memory. Write a faithful 2-4 sentence summary of the conversation "
                "below, focused on the running theme. State only what is present; "
                "do not invent, advise, or add preamble. Output the summary only."
            )
            user_prompt = "Running theme: " + theme_str + "\n\nConversation:\n" + transcript
            text = self._daemon_chat(
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": user_prompt}],
                temperature=0.3,
            )
            if not text:
                return None
            if text.lower().startswith("summary:"):
                text = text[len("summary:"):].strip()
            return text or None
        except Exception:
            return None


# Convenience for the daemon job + manual runs.
def run_maintenance() -> Dict[str, Any]:
    return Journalist().run_maintenance()


def summarize_stream(messages, **kw) -> Dict[str, Any]:
    return Journalist().summarize_stream(messages, **kw)


def llm_summarize(turns, theme=None) -> Optional[str]:
    """Module-level convenience: abstractive summary of `turns` via the Daemon
    tier. None on any failure. Used by sage_daemon's generate_summary 'llm'
    branch and any future Daemon-tier consumer."""
    return Journalist()._llm_summarize(turns, theme or [])


def daemon_chat(messages, **opts) -> Optional[str]:
    """Module-level convenience: raw Daemon-tier (11436) chat completion - the
    SHARED primitive for future features such as Symposium moderation. Returns
    the assistant content string, or None on any failure."""
    return Journalist()._daemon_chat(messages, **opts)


if __name__ == "__main__":  # pragma: no cover
    import json
    print(json.dumps(run_maintenance(), indent=2))
