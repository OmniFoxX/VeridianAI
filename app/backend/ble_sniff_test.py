# Standalone BLE scan check. Run from backend/: py ble_sniff_test.py
import asyncio
from bleak import BleakScanner

async def scan():
    print("Scanning for BLE devices for 60 seconds...")
    devices = await BleakScanner.discover(timeout=60.0)
    if devices:
        for d in devices:
            print(f"  {d.address}  {d.name}  {d.details}")
    else:
        print("!! No BLE devices found (but no crash = BLE stack is working)")

asyncio.run(scan())