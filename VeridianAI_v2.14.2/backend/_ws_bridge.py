"""
_ws_bridge.py -- fake-WebSocket adapter for the OpenAI-compatible endpoint
==========================================================================

v2.2 Session 2 (2026-05-31): lets POST /v1/chat/completions invoke the
existing WebSocket chat handler (main.ws_chat) unchanged, so external
clients (Continue.dev, Claude Desktop, any OpenAI-compatible caller)
get the COMPLETE Sage pipeline -- persona, agentic loop, tool dispatch,
procedural-memory writes, hash-chain witnessing, AIQNudge -- without
the handler being aware it's not talking to a real WebSocket.

DESIGN
------
The chat handler signature is::

    async def ws_chat(websocket: WebSocket):
        await websocket.accept()
        while True:
            data = await websocket.receive_json()
            ...
            await websocket.send_json(...)

This module's `_MockWebSocket` implements EXACTLY the three methods the
handler actually uses (per audit 2026-05-31: accept, receive_json,
send_json). The bridge orchestrates the handler's lifecycle:

  1. Schedule `ws_chat(mock)` as an asyncio task.
  2. Mock's first `receive_json()` returns the queued user message.
  3. Handler processes the turn -- emitting "token", "tool_call",
     "tool_result", "agent_step", and finally "done" events via
     `send_json()`. All are captured.
  4. On the "done" event, the bridge sets a close signal.
  5. Mock's second `receive_json()` awaits the close signal, then
     raises WebSocketDisconnect. Handler exits cleanly.
  6. Bridge returns the collected events to the caller.

ZERO MODIFICATIONS to ws_chat. Purely additive substrate that runs
alongside the original WebSocket route.

STREAMING SUPPORT
-----------------
Pass an `on_token` async callback to `run_full_pipeline`; it fires
each time the handler emits a "token" event, before the event is
appended to the collected list. The caller (the SSE generator in
the chat-completions route) uses this to push chunks downstream in
real time.

TOOL CALLS
----------
`tool_call` and `tool_result` events are collected but NOT surfaced
through the streaming callback. Per Session-1 design call: tool work
runs server-side, transparent to Continue. The OpenAI streaming
protocol has no native slot for these events; exposing them would
force clients to parse OracleAI-specific event shapes.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, List, Optional

from fastapi import WebSocketDisconnect


# ---------------------------------------------------------------------------
# Mock WebSocket
# ---------------------------------------------------------------------------

class _MockWebSocket:
    """In-process stand-in for a starlette/FastAPI WebSocket.

    Implements only the surface the chat handler actually uses
    (audit 2026-05-31: accept, receive_json, send_json). Other
    attributes are NOT mocked -- if ws_chat ever starts using them
    in a future refactor, the bridge will fail loudly and we'll
    update the mock to match. That's intentional: silent
    coverage gaps are worse than visible breakage.
    """

    def __init__(
        self,
        initial_message: dict,
        events_out: List[dict],
        turn_done: asyncio.Event,
        close_signal: asyncio.Event,
        on_token: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        self._initial = initial_message
        self._initial_sent = False
        self._events = events_out
        self._turn_done = turn_done
        self._close_signal = close_signal
        self._on_token = on_token

    async def accept(self) -> None:
        # The real WebSocket would do the handshake here. We are
        # already "connected" -- no-op.
        return None

    async def receive_json(self) -> dict:
        if not self._initial_sent:
            self._initial_sent = True
            return self._initial
        # Second-and-later calls: wait until the bridge signals
        # disconnect, then raise. This matches what would happen if
        # the real WebSocket client disconnected after one turn.
        await self._close_signal.wait()
        raise WebSocketDisconnect(code=1000)

    async def send_json(self, data: dict) -> None:
        self._events.append(data)
        ev_type = data.get("type")
        # Streaming hook -- fire callback for token events ONLY.
        # Tool / step / nudge events stay server-side per design.
        if ev_type == "token" and self._on_token is not None:
            try:
                await self._on_token(data)
            except Exception:
                # A misbehaving stream consumer must not break the
                # handler -- collect events as usual and continue.
                pass
        # End-of-turn signal: the chat handler emits {"type": "done"}
        # after the final assistant message is fully delivered.
        if ev_type == "done":
            self._turn_done.set()


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

async def run_full_pipeline(
    user_messages: list,
    *,
    model_id: Optional[str] = None,
    options: Optional[dict] = None,
    on_token: Optional[Callable[[dict], Awaitable[None]]] = None,
    handler_timeout_sec: float = 600.0,
) -> List[dict]:
    """Run one turn of the full chat pipeline and return the captured events.

    user_messages : list of {"role": ..., "content": ...} dicts (OpenAI shape)
    model_id      : optional explicit model id (None -> let auto-route decide)
    options       : optional dict forwarded to the handler's `options` field
    on_token      : optional async callback fired for each "token" event
    handler_timeout_sec : safety cap on how long the handler can run

    Returns the list of send_json payloads emitted during the turn.
    Caller is responsible for assembling whatever surface they need
    (final text, OpenAI choices array, etc.) from these events.

    Never raises on pipeline errors -- the handler's own error handling
    emits {"type": "error", ...} events that the caller can inspect.
    Will raise on bridge-level issues (e.g., handler import failed).
    """
    # Late import to avoid circular: main imports this module.
    import main as _main

    initial_message = {
        "action": "chat",
        "messages": user_messages,
        "model_id": model_id,
        "options": options or {},
    }

    events: List[dict] = []
    turn_done = asyncio.Event()
    close_signal = asyncio.Event()

    mock = _MockWebSocket(
        initial_message=initial_message,
        events_out=events,
        turn_done=turn_done,
        close_signal=close_signal,
        on_token=on_token,
    )

    handler_task = asyncio.create_task(
        _main.ws_chat(mock),
        name="_ws_bridge.ws_chat_call",
    )
    done_task = asyncio.create_task(
        turn_done.wait(),
        name="_ws_bridge.done_wait",
    )

    try:
        # Wait for either: the handler signals turn-done, or the
        # handler task finishes (cleanly or with an exception), or
        # we hit the safety timeout.
        await asyncio.wait(
            [handler_task, done_task],
            timeout=handler_timeout_sec,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        # Whatever happened above, signal disconnect so the handler's
        # next receive_json (if any) raises and the task can finish.
        close_signal.set()
        done_task.cancel()

    # Give the handler up to 5 seconds to exit cleanly after the
    # disconnect signal. If it doesn't, cancel it -- the events we
    # collected are still valid.
    try:
        await asyncio.wait_for(handler_task, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if not handler_task.done():
            handler_task.cancel()
    except WebSocketDisconnect:
        # Expected: the disconnect propagated out instead of being
        # absorbed by the handler's try/except. Either way, fine.
        pass
    except Exception:
        # Handler raised something else (rare). Events still valid.
        pass

    return events


# ---------------------------------------------------------------------------
# Event assemblers
# ---------------------------------------------------------------------------

def assemble_assistant_text(events: List[dict]) -> str:
    """Concatenate all 'token' chunks from a pipeline run into one
    string -- the assistant's final reply for the turn.

    Other event types (tool_call, tool_result, agent_step, etc.) are
    ignored: tool work happened server-side and its visible output
    is already reflected in subsequent token chunks the model
    generated AFTER seeing the tool result.
    """
    parts = []
    for ev in events:
        if ev.get("type") == "token":
            content = ev.get("content")
            if content is not None:
                parts.append(str(content))
    return "".join(parts)


def extract_error(events: List[dict]) -> Optional[str]:
    """If the pipeline emitted an error event, return its message.
    Otherwise None. Useful for translating pipeline errors into HTTP 500
    responses or OpenAI error envelopes.
    """
    for ev in events:
        if ev.get("type") == "error":
            return str(ev.get("content") or ev.get("message") or ev)
    return None
