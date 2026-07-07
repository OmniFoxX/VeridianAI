# BitChat BLE peripheral daemon (Linux) - setup

`bless_ble_daemon.py` gives Sage a real BLE **peripheral/advertiser** role on a
Linux host (Raspberry Pi, spare Linux box, or WSL2+custom-kernel+USB dongle),
since Windows here can't advertise. It reuses the already-fixed protocol code
(`bitchat.py` + `encryption.py`) verbatim and only swaps the radio layer
(bleak-central -> bless-peripheral) via a transport shim. It exposes the SAME
WebSocket contract as `bitchat_ble_gateway.py`, so OracleAI talks to it unchanged.

## On the Linux host
1. Copy the project's `bitchat-python/` folder next to this file, OR set:
       export BITCHAT_PYTHON_ROOT=/path/to/OracleAI_v2.11.11
2. Install deps:
       pip install bless fastapi uvicorn cryptography bleak aioconsole pybloom-live
3. Make sure Bluetooth is up:  `bluetoothctl show`  (Powered: yes)
4. Run:
       python3 bless_ble_daemon.py          # advertises + serves WS on :8080
   Env: BITCHAT_WS_HOST (default 0.0.0.0), BITCHAT_WS_PORT (default 8080)

## Point OracleAI at it
BitChat is opt-in/off by default now, and the Windows gateway need not run.
In OracleAI -> Socials -> BitChat settings, set:
    host = <Linux host IP>   (Pi/Linux LAN IP, or the WSL2 IP)
    port = 8080
Then Connect. OracleAI's bridge speaks the daemon's WS exactly like the gateway's.
(If you prefer keeping localhost:8080 on Windows, the Windows gateway can be made
a thin forwarder to the daemon later - say the word and I'll add that.)

## What should happen
- Daemon logs: `[BLE] advertising 'Sage' service f47b5e2d-...`
- A phone discovers Sage, connects, subscribes, and initiates Noise.
- Daemon logs the handshake (`[GW] RECV ...` for messages) and Sage appears on
  the phone. Messages flow both ways through the WS to OracleAI.

## Live-tweak spots (first run on a real radio)
The protocol is verified, but these bless specifics are exercised live:
- `update_value(service, char)` notify semantics + MTU (large packets may need
  the existing fragmentation path, which is already wired through the shim).
- the write-request / subscribe callback signatures on your bless version.
Paste any traceback from first run and these are quick adjustments.
