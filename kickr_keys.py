#!/usr/bin/env python3
"""
KICKR Keys  —  Wahoo KICKR Core 2 power → keyboard input
Reads BLE Cycling Power data and maps wattage zones to held key combos.
"""

import asyncio
import struct
import sys
import argparse
from collections import deque
from pathlib import Path

# ── Dependency checks ──────────────────────────────────────────────────────────
try:
    import tomllib                        # built-in Python 3.11+
except ImportError:
    try:
        import tomli as tomllib           # pip install tomli  (Python 3.9–3.10)
    except ImportError:
        print("ERROR: Run  pip install tomli  (you're on Python < 3.11)")
        sys.exit(1)

try:
    from bleak import BleakScanner, BleakClient
except ImportError:
    print("ERROR: Run  pip install bleak")
    sys.exit(1)

try:
    from pynput.keyboard import Key, Controller, KeyCode
except ImportError:
    print("ERROR: Run  pip install pynput")
    sys.exit(1)

# ── BLE UUIDs ──────────────────────────────────────────────────────────────────
CYCLING_POWER_SERVICE_UUID      = "00001818-0000-1000-8000-00805f9b34fb"
CYCLING_POWER_MEASUREMENT_UUID  = "00002a63-0000-1000-8000-00805f9b34fb"

# ── Keyboard helper ────────────────────────────────────────────────────────────
keyboard = Controller()

SPECIAL_KEY_MAP: dict[str, Key] = {
    "alt":       Key.alt,
    "alt_l":     Key.alt_l,
    "alt_r":     Key.alt_r,
    "ctrl":      Key.ctrl,
    "ctrl_l":    Key.ctrl_l,
    "ctrl_r":    Key.ctrl_r,
    "shift":     Key.shift,
    "shift_l":   Key.shift_l,
    "shift_r":   Key.shift_r,
    "space":     Key.space,
    "enter":     Key.enter,
    "tab":       Key.tab,
    "esc":       Key.esc,
    "caps_lock": Key.caps_lock,
    "up":        Key.up,
    "down":      Key.down,
    "left":      Key.left,
    "right":     Key.right,
    **{f"f{n}": getattr(Key, f"f{n}") for n in range(1, 13)},
}

def str_to_key(key_str: str) -> Key | KeyCode:
    """Convert a config string like 'alt' or 'w' into a pynput key."""
    lower = key_str.lower()
    if lower in SPECIAL_KEY_MAP:
        return SPECIAL_KEY_MAP[lower]
    if len(key_str) == 1:
        return KeyCode.from_char(key_str)
    raise ValueError(f"Unknown key: '{key_str}' — check your config.toml")


# ── BLE power parsing ──────────────────────────────────────────────────────────
def parse_power_watts(data: bytearray) -> int:
    """
    Cycling Power Measurement (0x2A63):
      Bytes 0-1  →  Flags (uint16 LE)
      Bytes 2-3  →  Instantaneous Power (sint16 LE, Watts)
    """
    if len(data) < 4:
        return 0
    power = struct.unpack_from("<h", data, 2)[0]   # signed 16-bit
    return max(0, int(power))


# ── Config loader ──────────────────────────────────────────────────────────────
def load_config(path: str = "config.toml") -> dict:
    p = Path(path)
    if not p.exists():
        print(f"ERROR: Config file not found → {p.resolve()}")
        sys.exit(1)
    with open(p, "rb") as f:
        cfg = tomllib.load(f)
    # Pre-compile key objects
    for zone in cfg.get("power_map", []):
        zone["_keys"] = [str_to_key(k) for k in zone.get("keys", [])]
    return cfg


# ── Main application class ─────────────────────────────────────────────────────
class KICKRKeys:
    def __init__(self, config: dict):
        self.config   = config
        self.dev_name = config["device"]["name"]
        settings      = config.get("settings", {})

        self.smoothing       = int(settings.get("smoothing_window",   5))
        self.dead_band_watts = float(settings.get("dead_band_watts",  5))

        # Zones sorted highest → lowest watt threshold
        self.zones = sorted(config["power_map"],
                            key=lambda z: z["min_watts"], reverse=True)

        self.power_buffer : deque[int] = deque(maxlen=self.smoothing)
        self.current_zone : dict | None = None
        self.pressed_keys : set         = set()
        self._running     : bool        = True
        self._avg_watts   : float       = 0.0

    # ── Zone lookup ────────────────────────────────────────────────────────────
    def _get_zone(self, avg_watts: float) -> dict:
        for zone in self.zones:
            if avg_watts >= zone["min_watts"]:
                return zone
        return self.zones[-1]

    # ── Key management ─────────────────────────────────────────────────────────
    def _release_all(self):
        for key in list(self.pressed_keys):
            try:
                keyboard.release(key)
            except Exception:
                pass
        self.pressed_keys.clear()

    def _apply_zone(self, zone: dict):
        if zone is self.current_zone:
            return
        self._release_all()
        self.current_zone = zone
        for key in zone["_keys"]:
            keyboard.press(key)
            self.pressed_keys.add(key)
        label = zone.get("label", f"{zone['min_watts']}w+")
        keys  = zone.get("keys", [])
        print(f"\n  ▶  Zone: {label:<12}  Keys: {keys if keys else '(none)'}")

    # ── BLE callback ───────────────────────────────────────────────────────────
    def _on_power(self, _sender, data: bytearray):
        watts = parse_power_watts(data)
        self.power_buffer.append(watts)
        avg   = sum(self.power_buffer) / len(self.power_buffer)
        self._avg_watts = avg

        zone  = self._get_zone(avg)
        self._apply_zone(zone)

        label = zone.get("label", "?")
        print(f"\r  Power: {watts:3d} W  (avg {avg:5.1f} W)  Zone: {label:<12}",
              end="", flush=True)

    # ── Device discovery ───────────────────────────────────────────────────────
    async def _find_device(self):
        print(f"  Scanning for '{self.dev_name}' …  (10 s)")
        devices = await BleakScanner.discover(timeout=10.0)
        for d in devices:
            if d.name and self.dev_name.lower() in d.name.lower():
                return d
        return None

    # ── Main loop ──────────────────────────────────────────────────────────────
    async def run(self):
        device = await self._find_device()
        if not device:
            print(f"\nERROR: '{self.dev_name}' not found.")
            print("  • Make sure the trainer is on and broadcasting.")
            print("  • Pair it in Windows Bluetooth settings first.")
            print("  • Run with --scan to list all visible BLE devices.")
            print("  • Check the 'name' field in config.toml.")
            return

        print(f"  Found   →  {device.name}  ({device.address})")
        print("  Connecting …")

        async with BleakClient(device.address, timeout=15.0) as client:
            print("  Connected!  Subscribing to power data …\n")
            print("  Press Ctrl-C to stop.\n")

            await client.start_notify(
                CYCLING_POWER_MEASUREMENT_UUID, self._on_power
            )

            while self._running:
                await asyncio.sleep(0.05)

            await client.stop_notify(CYCLING_POWER_MEASUREMENT_UUID)

        self._release_all()
        print("\n  Disconnected. All keys released.")


# ── BLE scanner utility ────────────────────────────────────────────────────────
async def scan_ble():
    print("Scanning for all BLE devices (10 s) …\n")
    devices = await BleakScanner.discover(timeout=10.0)
    if not devices:
        print("No devices found. Make sure Bluetooth is on.")
        return
    print(f"{'Name':<35}  {'Address':<20}  RSSI")
    print("─" * 65)
    for d in sorted(devices, key=lambda x: x.rssi or -999, reverse=True):
        name = d.name or "(no name)"
        print(f"{name:<35}  {d.address:<20}  {d.rssi} dBm")


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="KICKR Keys — bike power to keyboard"
    )
    parser.add_argument("--config", default="config.toml",
                        help="Path to config file (default: config.toml)")
    parser.add_argument("--scan", action="store_true",
                        help="Scan and list all nearby BLE devices, then exit")
    args = parser.parse_args()

    banner = """
  ╔═══════════════════════════════════════╗
  ║       K I C K R   K E Y S            ║
  ║   Bike Power  →  Keyboard Input      ║
  ╚═══════════════════════════════════════╝
"""
    print(banner)

    if args.scan:
        asyncio.run(scan_ble())
        return

    config = load_config(args.config)
    app    = KICKRKeys(config)

    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        print("\n  Stopping …")
        app._release_all()
        print("  All keys released. Goodbye.")


if __name__ == "__main__":
    main()
