# KICKR Keys — Setup Guide

Turns your Wahoo KICKR Core 2 wattage into keyboard inputs for PC games.

---

## 1. Install Python

Download **Python 3.11+** from https://python.org/downloads  
✅ Check **"Add Python to PATH"** during install.

---

## 2. Pair your trainer

Open **Windows Settings → Bluetooth & devices → Add device**  
Pair your KICKR Core 2. It shows up as something like `KICKR CORE XXXX`.

---

## 3. Install dependencies

Open a terminal in this folder and run:

```
pip install -r requirements.txt
```

---

## 4. Find your device name

Run the scanner to see what BLE name your trainer broadcasts:

```
python kickr_keys.py --scan
```

Copy the exact name (or any unique portion of it) into `config.toml` under `[device] name`.

---

## 5. Configure your zones

Edit **`config.toml`** — it's heavily commented. Key things to set:

| Setting | What it does |
|---|---|
| `device.name` | Partial BLE name of your KICKR |
| `settings.smoothing_window` | Readings to average (reduces power jitter) |
| `[[power_map]]` blocks | Watt threshold → keys held |

---

## 6. Run it

Double-click **`run.bat`**, or from a terminal:

```
python kickr_keys.py
```

If keystrokes don't register in-game, **right-click run.bat → Run as administrator**.

---

## Supported key names

| Type | Examples |
|---|---|
| Letters/numbers | `"w"` `"a"` `"1"` `"e"` |
| Modifiers | `"shift"` `"ctrl"` `"alt"` `"alt_l"` `"alt_r"` |
| Special | `"space"` `"enter"` `"esc"` `"tab"` `"caps_lock"` |
| Arrow keys | `"up"` `"down"` `"left"` `"right"` |
| Function keys | `"f1"` … `"f12"` |

---

## Skyrim default keybinds (for reference)

| Action | Default Key |
|---|---|
| Move forward | `W` |
| Sprint | `Alt` (held while moving) |
| Sneak | `Ctrl` |
| Walk toggle | `Caps Lock` |
