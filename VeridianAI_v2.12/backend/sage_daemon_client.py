"""
sage_daemon_client.py — TCP client for sage_daemon.py
-----------------------------------------------------
Length-prefixed JSON protocol over localhost TCP. Used by sage_engine.py
to offload memory-log mechanics (read, verify, summarize) to the daemon
so those operations don't consume tokens in Sage's agentic context.

v2.1.3: port changed 9999 → 9998 (9999 is used by ipc_bridge for the
privacy browser mirror). Also fixed a variable shadowing bug where the
outgoing and incoming length prefixes reused the same name.

v2.1.5: MLM training data logger added to send_request(). Every
successful daemon call appends one CSV row to:
    backend/mlm_training_data/daemon_calls.csv
Format: f1,f2,f3,f4,f5,label  (feature_dim=5, no header, no quotes)
Used to train the Sage Daemon MLM pre-router once sufficient data
has accumulated. Logging failures are non-fatal — a log error never
breaks a daemon call.
"""

import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Dict, Optional


class SageDaemonClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9998,
        timeout: float = 5.0,
        retries: int = 3,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self.logger = logging.getLogger(__name__)
        self._sock: Optional[socket.socket] = None
        self._auth_token = self._load_auth_token()

    @staticmethod
    def _load_auth_token() -> Optional[str]:
        """Read the shared socket token (#69 F5). None if unavailable; the
        daemon ignores it unless require_socket_auth is enabled."""
        try:
            from config import DATA_DIR
            from handoff_guard import load_or_create_socket_token
            return load_or_create_socket_token(DATA_DIR)
        except Exception:
            return None

    # --- MLM training data logger -------------
    @staticmethod
    def _log_mlm_training_row(
        action: str,
        payload: Dict[str, Any],
        source: str = "internal",
    ) -> None:
        """
        Append one MLM training row after a confirmed successful daemon call.

        CSV format (no header, no quotes):
            f1,f2,f3,f4,f5,label

        Features:
            f1  payload length normalized  len(json(payload)) / 4096, clamped 0-1
            f2  has 'entries' key          1.0 / 0.0
            f3  has 'count' key            1.0 / 0.0
            f4  has 'summary_type' key     1.0 / 0.0
            f5  source tier encoded        oracle=0.33, sage=0.66, internal=1.0

        feature_dim=5 must match --features arg passed to train_mlm.py.
        Logging failures are silently swallowed — never break a daemon call.
        """
        try:
            payload_str = json.dumps(payload, separators=(",", ":"))

            f1 = round(min(len(payload_str) / 4096, 1.0), 6)
            f2 = 1.0 if "entries"      in payload else 0.0
            f3 = 1.0 if "count"        in payload else 0.0
            f4 = 1.0 if "summary_type" in payload else 0.0
            f5 = {"oracle": 0.33, "sage": 0.66}.get(source, 1.0)

            row = f"{f1},{f2},{f3},{f4},{f5},{action}\n"

            log_path = (
                Path(__file__).resolve().parent
                / "mlm_training_data"
                / "daemon_calls.csv"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)

            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(row)

        except Exception:
            # Logging must never raise — silently skip on any error
            pass

    # --- Connection management -------------
    def _connect(self) -> socket.socket:
        last_err: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                sock = socket.create_connection(
                    (self.host, self.port), timeout=self.timeout
                )
                sock.settimeout(self.timeout)
                self.logger.debug(
                    f"Connected to daemon at {self.host}:{self.port}"
                )
                return sock
            except Exception as e:
                last_err = e
                self.logger.warning(
                    f"Daemon connection attempt {attempt + 1}/{self.retries} failed: {e}"
                )
                time.sleep(0.5 * (attempt + 1))
        raise ConnectionError(
            f"Unable to connect to daemon at {self.host}:{self.port} "
            f"after {self.retries} attempts: {last_err}"
        )

    def _ensure_socket(self) -> socket.socket:
        if self._sock is None:
            self._sock = self._connect()
        return self._sock

    # --- Framed send/receive -------------------
    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        """Read exactly n bytes or raise ConnectionError."""
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Socket closed mid-read")
            buf += chunk
        return buf

    def send_request(
        self,
        action: str,
        payload: Optional[Dict[str, Any]] = None,
        source: str = "internal",        # v2.1.5: caller passes tier name
    ) -> Dict[str, Any]:
        """Send a request to the daemon and return the decoded response dict.

        The daemon protocol is:
          - 8 ASCII digit length prefix
          - UTF-8 JSON body

        Retries on connection errors. Returns the parsed JSON response on
        success. Raises ConnectionError on unrecoverable failure.

        Args:
            action:  Daemon action string (e.g. "ping", "read_recent")
            payload: Optional dict merged into the request body
            source:  Caller tier name for MLM logging ("oracle","sage","internal")
        """
        if payload is None:
            payload = {}
        request = {"action": action, **payload}
        if self._auth_token:
            request["auth_token"] = self._auth_token
        body = json.dumps(request, separators=(",", ":")).encode("utf-8")
        out_header = f"{len(body):08d}".encode("utf-8")
        frame = out_header + body

        last_err: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                sock = self._ensure_socket()
                sock.sendall(frame)

                # Read response header
                in_header = self._recv_exact(sock, 8)
                try:
                    length = int(in_header.decode("utf-8"))
                except ValueError:
                    raise ConnectionError(
                        f"Bad response header: {in_header!r}"
                    )

                # Read response body
                response_data = self._recv_exact(sock, length)
                parsed = json.loads(response_data.decode("utf-8"))

                # v2.1.5: log successful call for MLM training data
                # Only reached on clean parse — never logs retries or errors
                self._log_mlm_training_row(action, payload, source)

                return parsed

            except (ConnectionError, socket.timeout, OSError, json.JSONDecodeError) as e:
                last_err = e
                self.logger.warning(
                    f"Daemon comms error (attempt {attempt + 1}/{self.retries}): {e}"
                )
                try:
                    if self._sock is not None:
                        self._sock.close()
                except Exception:
                    pass
                self._sock = None
                if attempt == self.retries - 1:
                    raise
                time.sleep(0.5 * (attempt + 1))

        raise ConnectionError(
            f"send_request gave up after {self.retries} attempts: {last_err}"
        )

    # --- Convenience wrappers -----------
    def ping(self) -> bool:
        """Return True if the daemon answered a ping."""
        try:
            resp = self.send_request("ping")
            return bool(resp.get("pong"))
        except Exception:
            return False

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None