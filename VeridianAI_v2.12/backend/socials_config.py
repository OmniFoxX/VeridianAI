"""Persistent, secret-aware config for Socials channels — stored in sage_data.

Bot tokens are SECRETS, so they live in sage_data (OUTSIDE the project), never in
config.json (which gets zipped / synced / distributed). Masked snapshots never
return a token to the UI — only whether one is set. Pure stdlib, thread-safe.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

_SECRET_KEYS = {"token", "app_password"}


class SocialsConfig:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = {}
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = data if isinstance(data, dict) else {}
        except Exception:
            self._data = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, channel: str) -> dict:
        with self._lock:
            return dict(self._data.get(channel, {}))

    def set(self, channel: str, settings: dict) -> None:
        with self._lock:
            cur = dict(self._data.get(channel, {}))
            for k, v in (settings or {}).items():
                # never clobber a stored secret with an empty value (clear() does that)
                if k in _SECRET_KEYS and (v is None or v == ""):
                    continue
                cur[k] = v
            self._data[channel] = cur
            self._save()

    def clear(self, channel: str, keys=None) -> None:
        with self._lock:
            if keys is None:
                self._data.pop(channel, None)
            else:
                cur = dict(self._data.get(channel, {}))
                for k in keys:
                    cur.pop(k, None)
                self._data[channel] = cur
            self._save()

    def masked(self) -> dict:
        """Per-channel settings with secrets replaced by has_<key> booleans."""
        with self._lock:
            out = {}
            for chan, settings in self._data.items():
                # Reserved router-level keys (e.g. "__router__") are not
                # channels; keep them out of the per-channel snapshot.
                if isinstance(chan, str) and chan.startswith("__"):
                    continue
                m = {}
                for k, v in settings.items():
                    if k in _SECRET_KEYS:
                        m["has_" + k] = bool(v)
                    else:
                        m[k] = v
                out[chan] = m
            return out
