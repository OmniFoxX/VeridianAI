#!/usr/bin/env python3
"""
winrt_advertise_test.py - pure-WinRT BLE peripheral advertising diagnostic.

The dongle ACCEPTS the peripheral command (status went to Aborted, not
"unsupported"), so we now tune parameters. This tries the connectable/
discoverable combinations with a FRESH provider each time and logs the
advertisement_status_changed event to reveal why any attempt aborts.

RUN (BitChat OFF in OracleAI so the dongle is free):
    python winrt_advertise_test.py
Watch for 'STARTED' + whether a phone shows Sage / [WRITE] lines. Ctrl+C to stop.
"""
import asyncio
import logging
import sys
import uuid

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [winrt-test] %(levelname)s %(message)s")
log = logging.getLogger("winrt-test")

BITCHAT_SERVICE_UUID        = "f47b5e2d-4a9e-4c5a-9b3f-8e1d2c3a4b5c"
BITCHAT_CHARACTERISTIC_UUID = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"

try:
    from winrt.windows.devices.bluetooth import BluetoothError
    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattServiceProvider,
        GattLocalCharacteristicParameters,
        GattCharacteristicProperties,
        GattServiceProviderAdvertisingParameters,
        GattProtectionLevel,
        GattWriteOption,
    )
    from winrt.windows.security.cryptography import CryptographicBuffer
except Exception as exc:  # pragma: no cover
    print("\n[winrt-test] Missing a WinRT projection package. Likely fix:\n"
          "    pip install winrt-Windows.Devices.Bluetooth.GenericAttributeProfile "
          "winrt-Windows.Security.Cryptography\n"
          f"(import error: {exc})")
    sys.exit(1)

_MSG_TYPES = {
    0x01: "ANNOUNCE", 0x02: "KEY_EXCHANGE", 0x03: "LEAVE", 0x04: "MESSAGE",
    0x10: "NOISE_HANDSHAKE_INIT", 0x11: "NOISE_HANDSHAKE_RESP",
    0x12: "NOISE_ENCRYPTED", 0x13: "NOISE_IDENTITY_ANNOUNCE",
    0x20: "VERSION_HELLO", 0x21: "VERSION_ACK",
}
_ADV_STATUS = {0: "Created", 1: "Stopped", 2: "Started", 3: "Aborted",
               4: "StartedWithoutAllAdvertisementData"}
_loop = None


def _sname(v) -> str:
    try:
        v = int(v)
    except Exception:
        pass
    return f"{v} ({_ADV_STATUS.get(v, '?')})"


def _status(provider) -> str:
    return _sname(provider.advertisement_status)


def _describe(data: bytes) -> str:
    if len(data) >= 2:
        return f"type={_MSG_TYPES.get(data[1], hex(data[1]))} len={len(data)}"
    return f"len={len(data)}"


async def _handle_write(args):
    try:
        request = await args.get_request_async()
        data = bytes(CryptographicBuffer.copy_to_byte_array(request.value))
        log.info("[WRITE] %s hex=%s", _describe(data), data.hex())
        if request.option == GattWriteOption.WRITE_WITH_RESPONSE:
            request.respond()
    except Exception as exc:
        log.error("write handling error: %s", exc)


def on_write_requested(sender, args):
    deferral = args.get_deferral()
    fut = asyncio.run_coroutine_threadsafe(_handle_write(args), _loop)
    fut.add_done_callback(lambda _f: deferral.complete())


def on_subscribers_changed(sender, args):
    try:
        n = len(sender.subscribed_clients)
    except Exception:
        n = "?"
    log.info("[SUBSCRIBED] central subscribed/unsubscribed -> %s client(s) "
             "(a phone connected to Sage!)", n)


def on_adv_status_changed(sender, args):
    try:
        st = args.status
    except Exception:
        st = sender.advertisement_status
    err = getattr(args, "error", None)
    log.info("[ADV EVENT] status=%s error=%s", _sname(st), err)


async def _new_provider():
    result = await GattServiceProvider.create_async(uuid.UUID(BITCHAT_SERVICE_UUID))
    if result.error != BluetoothError.SUCCESS:
        raise RuntimeError(f"create_async: {result.error}")
    provider = result.service_provider
    params = GattLocalCharacteristicParameters()
    params.characteristic_properties = (
        GattCharacteristicProperties.WRITE
        | GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE
        | GattCharacteristicProperties.NOTIFY
    )
    params.write_protection_level = GattProtectionLevel.PLAIN
    params.read_protection_level = GattProtectionLevel.PLAIN
    cres = await provider.service.create_characteristic_async(
        uuid.UUID(BITCHAT_CHARACTERISTIC_UUID), params)
    if cres.error != BluetoothError.SUCCESS:
        raise RuntimeError(f"create_characteristic: {cres.error}")
    ch = cres.characteristic
    ch.add_write_requested(on_write_requested)
    ch.add_subscribed_clients_changed(on_subscribers_changed)
    provider.add_advertisement_status_changed(on_adv_status_changed)
    return provider


async def _attempt(connectable: bool, discoverable: bool):
    log.info("=== attempt: connectable=%s discoverable=%s ===", connectable, discoverable)
    provider = await _new_provider()
    adv = GattServiceProviderAdvertisingParameters()
    adv.is_connectable = connectable
    adv.is_discoverable = discoverable
    try:
        provider.start_advertising_with_parameters(adv)
    except Exception as e:
        log.warning("start_advertising_with_parameters -> %s: %s", type(e).__name__, e)
        return None
    await asyncio.sleep(3)
    log.info("settled status=%s", _status(provider))
    if int(provider.advertisement_status) == 2:      # Started
        return provider
    try:
        provider.stop_advertising()
    except Exception:
        pass
    return None


async def main():
    global _loop
    _loop = asyncio.get_running_loop()
    live = None
    for conn, disc in ((True, True), (True, False), (False, True)):
        live = await _attempt(conn, disc)
        if live is not None:
            log.info(">>> ADVERTISING STARTED with connectable=%s discoverable=%s <<<",
                     conn, disc)
            break
    if live is None:
        log.error("No parameter combo reached 'Started'. See [ADV EVENT] lines above.")
        return
    log.info("Open BitChat on both phones; watch nearby/people + this console.")
    log.info("[SUBSCRIBED]/[WRITE] => a phone found + connected to Sage. Ctrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(5)
            log.info("...advertising (status=%s)", _status(live))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        try:
            live.stop_advertising()
        except Exception:
            pass
        log.info("Stopped advertising.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
# --- end of file ---
