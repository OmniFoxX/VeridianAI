"""
IPC bridge utilities for OracleAI.
Provides a simple TCP-based localhost communication mechanism.
Used by browser_tool.py to send navigation commands to browser_tool.py.
"""
import json
import socket
import threading
from typing import Callable, Dict, Any

HOST = "127.0.0.1"
PORT = 9999  # Arbitrary but stable localhost port


def send_ipc_message(action: str, payload: Any) -> None:
    """
    Send a JSON message to the IPC server (if listening).
    Failures are silently ignored to preserve headless operation.
    """
    message = {"action": action, "payload": payload}
    data = json.dumps(message).encode("utf-8") + b"\n"
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5) as sock:
            sock.sendall(data)
    except (ConnectionRefusedError, socket.timeout, OSError):
        # No listener available – headless mode continues unaffected
        pass


def start_ipc_server(message_handler: Callable[[Dict[str, Any]], None]) -> threading.Thread:
    """
    Start a blocking TCP server in a background thread.
    Calls message_handler with parsed JSON dict for each received message.
    Returns the thread object (daemon=True).
    """
    def server_loop():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_sock.bind((HOST, PORT))
            server_sock.listen()
            while True:
                conn, addr = server_sock.accept()
                with conn:
                    buffer = b""
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buffer += chunk
                        if b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            try:
                                msg = json.loads(line.decode("utf-8"))
                                message_handler(msg)
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                # Malformed message – ignore
                                pass

    thread = threading.Thread(target=server_loop, daemon=True)
    thread.start()
    return thread