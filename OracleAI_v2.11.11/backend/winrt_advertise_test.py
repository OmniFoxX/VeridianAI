#!/usr/bin/env python3
"""
winrt_advertise_test.py - pure-WinRT BLE peripheral test (NO bless).

bless failed here at the `bleak_winrt` import / a low-level CreateFile on the
BTH\\ device path. This script instead drives the *high-level* WinRT
GattServiceProvider API directly -- the same WinRT projection your working
bleak 3.0.2 central uses -- which does NOT touch that device path. If WinRT can
advertise the BitChat service at all on this machine, this is how.

Uses the `winrt-*` packages already installed for bleak 3.x. No extra install
expected; if an import fails it will say which `winrt-Windows.*` package to add.

RUN (in OracleAI's env, with BitChat OFF so the adapter is free):
    python winrt_advertise_test.py
Then open BitChat on both phones; watch their nearby/people list + this console.
[SUBSCRIBED]/[WRITE] lines mean a phone discovered + connected to Sage. Ctrl+C to stop.
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

_loop: asyncio.AbstractEventLoop = None  # set in main()


def _describe(data: bytes) -> str:
    if len(data) >= 2:
        return f"type={_MSG_TYPES.get(data[1], hex(data[1]))} len={len(data)}"
    return f"len={len(data)}"


_ADV_STATUS = {0: "Created", 1: "Stopped", 2: "Started", 3: "Aborted",
               4: "StartedWithoutAllAdvertisementData"}


def _status(provider) -> str:
    try:
        v = int(provider.advertisement_status)
    except Exception:
        v = provider.advertisement_status
    return f"{v} ({_ADV_STATUS.get(v, '?')})"


async def _handle_write(args):
    try:
        request = await args.get_request_async()
        data = bytes(CryptographicBuffer.copy_to_byte_array(request.value))
        log.info("[WRITE] %s", _describe(data))
        log.info("[WRITE] hex=%s", data.hex())
        # Acknowledge writes that expect a response
        if request.option == GattWriteOption.WRITE_WITH_RESPONSE:
            request.respond()
    except Exception as exc:
        log.error("write handling error: %s", exc)


def on_write_requested(sender, args):
    # Runs on a WinRT thread. Keep the request alive via a deferral while we
    # do the async work on the asyncio loop.
    deferral = args.get_deferral()
    fut = asyncio.run_coroutine_threadsafe(_handle_write(args), _loop)
    fut.add_done_callback(lambda _f: deferral.complete())


def on_subscribers_changed(sender, args):
    try:
        n = len(sender.subscribed_clients)
    except Exception:
        n = "?"
    log.info("[SUBSCRIBED] a central subscribed/unsubscribed -> %s client(s) "
             "(a phone connected to Sage!)", n)


async def main():
    global _loop
    _loop = asyncio.get_running_loop()

    result = await GattServiceProvider.create_async(uuid.UUID(BITCHAT_SERVICE_UUID))
    if result.error != BluetoothError.SUCCESS:
        log.error("create_async failed: %s", result.error)
        return
    provider = result.service_provider
    service = provider.service

    params = GattLocalCharacteristicParameters()
    params.characteristic_properties = (
        GattCharacteristicProperties.WRITE
        | GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE
        | GattCharacteristicProperties.NOTIFY
    )
    params.write_protection_level = GattProtectionLevel.PLAIN
    params.read_protection_level = GattProtectionLevel.PLAIN

    char_result = await service.create_characteristic_async(
        uuid.UUID(BITCHAT_CHARACTERISTIC_UUID), params
    )
    if char_result.error != BluetoothError.SUCCESS:
        log.error("create_characteristic_async failed: %s", char_result.error)
        return
    characteristic = char_result.characteristic
    characteristic.add_write_requested(on_write_requested)
    characteristic.add_subscribed_clients_changed(on_subscribers_changed)

    log.info("provider advertising methods: %s",
             [m for m in dir(provider) if "advert" in m.lower()])

    adv = GattServiceProviderAdvertisingParameters()
    adv.is_connectable = True
    adv.is_discoverable = True

    # WinRT tags the parameterized overload as StartAdvertisingWithParameters,
    # so PyWinRT exposes it as start_advertising_with_parameters(adv). The bare
    # start_advertising() does NOT make us connectable/discoverable (status stays
    # "Created"), so the *_with_parameters form is the one we need.
    started = None
    candidates = []
    if hasattr(provider, "start_advertising_with_parameters"):
        candidates.append(("start_advertising_with_parameters(adv)",
                           lambda: provider.start_advertising_with_parameters(adv)))
    candidates.append(("start_advertising(adv)", lambda: provider.start_advertising(adv)))
    candidates.append(("start_advertising()", lambda: provider.start_advertising()))
    for how, call in candidates:
        try:
            call()
            log.info("called %s -> status=%s", how, _status(provider))
            if int(provider.advertisement_status) == 2:   # Started
                started = how
                break
        except Exception as e:
            log.warning("%s -> %s: %s", how, type(e).__name__, e)
    if started is None:
        log.error("Advertising did not reach 'Started'. See methods/status above.")
        return
    log.info("Advertising BitChat service %s via %s (status=%s)",
             BITCHAT_SERVICE_UUID, started, _status(provider))
    log.info("Open BitChat on both phones; watch nearby/people + this console.")
    log.info("[SUBSCRIBED]/[WRITE] => a phone found + connected to Sage. Ctrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(5)
            log.info("...advertising (status=%s)", _status(provider))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        try:
            provider.stop_advertising()
        except Exception:
            pass
        log.info("Stopped advertising.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
# --- end of file ---
