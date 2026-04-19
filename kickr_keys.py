#!/usr/bin/env python3
"""
KICKR Keys  —  Wahoo KICKR Core 2 power → keyboard input + resistance control
Reads BLE Cycling Power data, maps wattage zones to held key combos,
and optionally controls trainer resistance via FTMS (ERG or slope mode).
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
# Cycling Power Service (read power output)
CYCLING_POWER_MEASUREMENT_UUID  = "00002a63-0000-1000-8000-00805f9b34fb"

# Fitness Machine Service (control resistance)
FTMS_SERVICE_UUID               = "00001826-0000-1000-8000-00805f9b34fb"
FTMS_CONTROL_POINT_UUID         = "00002ad9-0000-1000-8000-00805f9b34fb"
FTMS_STATUS_UUID                = "00002ada-0000-1000-8000-00805f9b34fb"

# FTMS Control Point op codes
FTMS_OP_REQUEST_CONTROL         = 0x00
FTMS_OP_SET_TARGET_POWER        = 0x05   # ERG mode  — payload: sint16 LE (watts)
FTMS_OP_SET_SIMULATION_PARAMS   = 0x11   # Slope mode — payload: see _cmd_slope()
FTMS_OP_RESPONSE                = 0x80
FTMS_RESULT_SUCCESS             = 0x01

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
    lower = key_str.lower()
    if lower in SPECIAL_KEY_MAP:
        return SPECIAL_KEY_MAP[lower]
    if len(key_str) == 1:
        return KeyCode.from_char(key_str)
    raise ValueError(f"Unknown key: '{key_str}' — check your config.toml")


# ── BLE power parsing ──────────────────────────────────────────────────────────
def parse_power_watts(data: bytearray) -> int:
    """Cycling Power Measurement 0x2A63 — bytes 2-3 are sint16 LE power in watts."""
    if len(data) < 4:
        return 0
    return max(0, int(struct.unpack_from("<h", data, 2)[0]))


# ── Config loader ──────────────────────────────────────────────────────────────
def load_config(path: str = "config.toml") -> dict:
    p = Path(path)
    if not p.exists():
        print(f"ERROR: Config file not found → {p.resolve()}")
        sys.exit(1)
    with open(p, "rb") as f:
        cfg = tomllib.load(f)
    for zone in cfg.get("power_map", []):
        zone["_keys"]     = [str_to_key(k) for k in zone.get("keys",     [])]
        zone["_tap_keys"] = [str_to_key(k) for k in zone.get("tap_keys", [])]
    return cfg


# ── FTMS Resistance Controller ────────────────────────────────────────────────
class ResistanceController:
    """
    Controls trainer resistance via the Fitness Machine Service (FTMS).

    Modes
    -----
    off   — no resistance commands sent; use another app to set resistance.
    erg   — ERG mode: trainer auto-adjusts resistance to hit a target wattage.
            Each zone can define its own erg_watts target.
    slope — Fixed gradient simulation: trainer feels like riding on a constant
            % grade. Set once on connect and never changes during the ride.
    """

    def __init__(self, config: dict, client: BleakClient):
        res_cfg      = config.get("resistance", {})
        self.mode    = res_cfg.get("mode", "off").lower()
        self.client  = client
        self._ready  = asyncio.Event()
        self._last_op: int = 0

        # Slope mode params (sent once on connect)
        self.slope_percent   = float(res_cfg.get("slope_percent",    1.0))
        self.wind_speed_mps  = float(res_cfg.get("wind_speed_mps",   0.0))
        self.crr             = float(res_cfg.get("crr",              0.004))
        self.cw_kg_m         = float(res_cfg.get("cw_kg_m",          0.51))

        # ERG mode — last commanded wattage (to avoid redundant writes)
        self._last_erg_watts: int | None = None

    def _on_status(self, _sender, data: bytearray):
        """Handle FTMS Status / Control Point response notifications."""
        if len(data) >= 3 and data[0] == FTMS_OP_RESPONSE:
            op, result = data[1], data[2]
            if result == FTMS_RESULT_SUCCESS:
                if op == FTMS_OP_REQUEST_CONTROL:
                    self._ready.set()
            else:
                print(f"\n  [FTMS] Command 0x{op:02X} failed (result 0x{result:02X})")

    def _cmd_erg(self, watts: int) -> bytes:
        """FTMS Set Target Power — op 0x05 + sint16 LE."""
        return struct.pack("<Bh", FTMS_OP_SET_TARGET_POWER, max(0, watts))

    def _cmd_slope(self) -> bytes:
        """
        FTMS Set Indoor Bike Simulation Params — op 0x11:
          sint16  wind speed   (0.001 m/s units)
          sint16  grade        (0.01 % units  — so 1.5% → 150)
          uint8   crr          (0.0001 units  — so 0.004 → 40)
          uint8   cw           (0.01 kg/m     — so 0.51  → 51)
        """
        wind  = int(self.wind_speed_mps * 1000)
        grade = int(self.slope_percent  * 100)
        crr   = int(self.crr            * 10000)
        cw    = int(self.cw_kg_m        * 100)
        return struct.pack("<BhhBB",
                           FTMS_OP_SET_SIMULATION_PARAMS,
                           wind, grade, crr, cw)

    async def _write(self, payload: bytes):
        try:
            await self.client.write_gatt_char(
                FTMS_CONTROL_POINT_UUID, payload, response=True
            )
        except Exception as e:
            print(f"\n  [FTMS] Write error: {e}")

    async def setup(self):
        """Subscribe to FTMS status and claim control of the trainer."""
        if self.mode == "off":
            print("  Resistance: OFF (controlled externally)")
            return

        # Check the trainer actually supports FTMS
        services = [str(s.uuid) for s in self.client.services]
        if FTMS_SERVICE_UUID not in services:
            print("  [WARN] FTMS service not found on this device — resistance disabled.")
            self.mode = "off"
            return

        try:
            await self.client.start_notify(FTMS_STATUS_UUID, self._on_status)
        except Exception:
            # Some firmware exposes control point response on the same characteristic
            pass

        # Request control (trainer must ack before accepting commands)
        await self._write(bytes([FTMS_OP_REQUEST_CONTROL]))
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            # Many KICKR firmwares don't send a response but still accept commands
            print("  [FTMS] No control-ack received — continuing anyway.")

        if self.mode == "slope":
            await self._write(self._cmd_slope())
            print(f"  Resistance: SLOPE  ({self.slope_percent:+.1f}% grade — fixed)")
        elif self.mode == "erg":
            print("  Resistance: ERG    (per-zone wattage targets)")

    async def set_erg(self, watts: int):
        """Send a new ERG target. No-ops if wattage unchanged."""
        if self.mode != "erg":
            return
        if watts == self._last_erg_watts:
            return
        self._last_erg_watts = watts
        await self._write(self._cmd_erg(watts))
        print(f"\n  [ERG] Target → {watts} W")

    async def teardown(self):
        if self.mode == "off":
            return
        # Reset to 0 W so trainer goes slack when we disconnect
        try:
            await self._write(self._cmd_erg(0))
        except Exception:
            pass


# ── Main application class ─────────────────────────────────────────────────────
class KICKRKeys:
    def __init__(self, config: dict):
        self.config   = config
        self.dev_name = config["device"]["name"]
        settings      = config.get("settings", {})

        self.smoothing       = int(settings.get("smoothing_window",  5))
        self.dead_band_watts = float(settings.get("dead_band_watts", 3))

        # Zones sorted highest → lowest watt threshold
        self.zones = sorted(config["power_map"],
                            key=lambda z: z["min_watts"], reverse=True)

        self.power_buffer : deque[int]          = deque(maxlen=self.smoothing)
        self.current_zone : dict | None         = None
        self.pressed_keys : set                 = set()
        self._running     : bool                = True
        self._avg_watts   : float               = 0.0
        self._resistance  : ResistanceController | None = None

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

    def _apply_zone_keys(self, zone: dict):
        self._release_all()

        for key in zone["_tap_keys"]:
            keyboard.tap(key)

        for key in zone["_keys"]:
            keyboard.press(key)
            self.pressed_keys.add(key)

        label  = zone.get("label",    f"{zone['min_watts']}w+")
        held   = zone.get("keys",     [])
        tapped = zone.get("tap_keys", [])
        parts  = []
        if held:   parts.append(f"hold={held}")
        if tapped: parts.append(f"tap={tapped}")
        print(f"\n  ▶  Zone: {label:<12}  {', '.join(parts) if parts else '(no keys)'}")

    # ── Zone transition ────────────────────────────────────────────────────────
    async def _enter_zone(self, zone: dict):
        if zone is self.current_zone:
            return
        self.current_zone = zone
        self._apply_zone_keys(zone)

        # Send resistance command for new zone
        if self._resistance and self._resistance.mode == "erg":
            erg_w = zone.get("erg_watts")
            if erg_w is not None:
                await self._resistance.set_erg(int(erg_w))
            else:
                print(f"\n  [WARN] Zone '{zone.get('label')}' has no erg_watts — "
                      "resistance unchanged.")

    # ── BLE power callback ─────────────────────────────────────────────────────
    def _on_power(self, _sender, data: bytearray):
        watts = parse_power_watts(data)
        self.power_buffer.append(watts)
        avg   = sum(self.power_buffer) / len(self.power_buffer)
        self._avg_watts = avg

        zone = self._get_zone(avg)

        # Schedule zone transition (async-safe fire-and-forget)
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda z=zone: asyncio.ensure_future(self._enter_zone(z))
        )

        label = zone.get("label", "?")
        print(f"\r  Power: {watts:3d} W  (avg {avg:5.1f} W)  Zone: {label:<14}",
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
            print("  Connected!\n")

            self._resistance = ResistanceController(self.config, client)
            await self._resistance.setup()

            print("  Subscribing to power data …")
            print("  Press Ctrl-C to stop.\n")

            await client.start_notify(CYCLING_POWER_MEASUREMENT_UUID, self._on_power)

            while self._running:
                await asyncio.sleep(0.05)

            await client.stop_notify(CYCLING_POWER_MEASUREMENT_UUID)
            await self._resistance.teardown()

        self._release_all()
        print("\n  Disconnected. All keys released. Resistance reset to 0 W.")


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
    parser = argparse.ArgumentParser(description="KICKR Keys — bike power to keyboard")
    parser.add_argument("--config", default="config.toml",
                        help="Path to config file (default: config.toml)")
    parser.add_argument("--scan", action="store_true",
                        help="Scan and list all nearby BLE devices, then exit")
    args = parser.parse_args()

    print("""
  ╔═══════════════════════════════════════╗
  ║       K I C K R   K E Y S            ║
  ║   Bike Power  →  Keyboard Input      ║
  ╚═══════════════════════════════════════╝
""")

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
