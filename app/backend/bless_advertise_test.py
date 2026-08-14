#!/usr/bin/env python3
"""
bless_advertise_test.py - DE-RISK TEST for the "phones can't see Sage" wall.

bleak (Sage's BLE lib) is central-only: it can scan/connect OUT but cannot
ADVERTISE, so the phones' scanners never discover Sage. This standalone script
uses `bless` to advertise the *real* BitChat service UUID and serve its
characteristic (write + notify), then logs anything a phone does.

GOAL: confirm Windows/WinRT can actually advertise this 128-bit service UUID in a
way your iPhone/Galaxy discover. If a phone connects and we see [WRITE] lines,
WinRT advertising works and we proceed to full integration. If nothing ever
connects, WinRT won't advertise it and we move Sage's BitChat to Linux/BlueZ.

SETUP (in the SAME Python env OracleAI uses):
    pip install bless
RUN:
    python bless_advertise_test.py
Then on each phone open BitChat and watch its people/nearby list + this console.
Ctrl+C to stop.
"""
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [bless-test] %(levelname)s %(message)s")
log = logging.getLogger("bless-test")

# The REAL BitChat BLE identifiers (must match exactly for the phones to find us)
BITCHAT_SERVICE_UUID        = "f47b5e2d-4a9e-4c5a-9b3f-8e1d2c3a4b5c"
BITCHAT_CHARACTERISTIC_UUID = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"
DEVICE_NAME                 = "Sage"

try:
    from bless import (                      # type: ignore
        BlessServer,
        BlessGATTCharacteristic,
        GATTCharacteristicProperties,
        GATTAttributePermissions,
    )
except Exception as exc:                     # pragma: no cover
    print("\n[bless-test] Could not import 'bless'. Install it first:\n"
          '    pip install bless\n'
          f"(import error: {exc})")
    sys.exit(1)

_MSG_TYPES = {
    0x01: "ANNOUNCE", 0x02: "KEY_EXCHANGE", 0x03: "LEAVE", 0x04: "MESSAGE",
    0x10: "NOISE_HANDSHAKE_INIT", 0x11: "NOISE_HANDSHAKE_RESP",
    0x12: "NOISE_ENCRYPTED", 0x13: "NOISE_IDENTITY_ANNOUNCE",
    0x20: "VERSION_HELLO", 0x21: "VERSION_ACK",
}


def _describe(data: bytearray) -> str:
    """Best-effort one-liner: BitChat packet type lives at byte index 1."""
    if len(data) >= 2:
        t = _MSG_TYPES.get(data[1], f"0x{data[1]:02x}")
        return f"type={t} len={len(data)}"
    return f"len={len(data)}"


def read_request(characteristic: "BlessGATTCharacteristic", **kwargs) -> bytearray:
    return characteristic.value or bytearray()


def write_request(characteristic: "BlessGATTCharacteristic", value: bytearray, **kwargs):
    # A phone wrote to us -> it discovered AND connected to Sage. This is the win.
    characteristic.value = value
    log.info("[WRITE] %s", _describe(value))
    log.info("[WRITE] hex=%s", bytes(value).hex())


async def main():
    server = BlessServer(name=DEVICE_NAME)
    server.read_request_func = read_request
    server.write_request_func = write_request

    await server.add_new_service(BITCHAT_SERVICE_UUID)
    flags = (
        GATTCharacteristicProperties.read
        | GATTCharacteristicProperties.write
        | GATTCharacteristicProperties.write_without_response
        | GATTCharacteristicProperties.notify
    )
    perms = GATTAttributePermissions.readable | GATTAttributePermissions.writeable
    await server.add_new_characteristic(
        BITCHAT_SERVICE_UUID, BITCHAT_CHARACTERISTIC_UUID, flags, None, perms
    )

    await server.start()
    log.info("Advertising '%s' with BitChat service %s", DEVICE_NAME, BITCHAT_SERVICE_UUID)
    log.info("Open BitChat on each phone and watch its nearby/people list + this console.")
    log.info("If you see [WRITE] lines, a phone discovered + connected to Sage. Ctrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await server.stop()
        log.info("Stopped advertising.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
# --- end of file ---
