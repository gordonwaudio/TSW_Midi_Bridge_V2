# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project Overview

TSW Midi Bridge is a Python 3.11+ desktop application bridging the **Train Sim World 6 External Interface API** (JSON over TCP on port 31270) with an external **MIDI device**. MIDI CC and Note On/Off messages drive in-game train controls; live simulation data is polled from TSW and sent back as MIDI CC/Note messages.

Primary target platform is **Windows** (must run alongside TSW6). macOS/Linux is a secondary goal.

---

## Commands

Once the project skeleton exists, standard commands will be:

```bash
# Install runtime dependencies
pip install -r requirements.txt

# Install dev dependencies
pip install -r requirements-dev.txt

# Run the application
python -m app.main

# Run all tests
pytest

# Run a single test file
pytest tests/test_mapping_engine.py

# Run a single test by name
pytest tests/test_mapping_engine.py::test_scale_linear_clamp

# Lint
ruff check .

# Auto-fix lint issues
ruff check . --fix

# Build Windows executable
pyinstaller tsw_midi_bridge.spec
```

---

## Architecture

Five subsystems run on separate threads, communicating exclusively via a shared `event_bus.py` (`queue.Queue`). **No direct shared state between threads.**

```
MIDI Hardware  ←→  midi_manager.py  ←→
                                        event_bus  ←→  mapping_engine.py  ←→  api_client.py  ←→  TSW6
OSC Network    ←→  osc_manager.py   ←→
                                              ↑
                                         gui/ (tkinter)
                                    (consumes bus via root.after())
```

| Module | Role |
|---|---|
| `app/main.py` | Entry point; wires subsystems together |
| `app/api_client.py` | HTTP requests to TSW API; subscription management; poll loop thread |
| `app/midi_manager.py` | `python-rtmidi` wrapper; device enumeration; MIDI in callback; MIDI out send |
| `app/osc_manager.py` | `python-osc` wrapper; UDP server thread (rx); UDP client (tx); OSC ↔ bus bridge |
| `app/mapping_engine.py` | Loads train config; builds lookup tables; scales values between MIDI/OSC and API ranges |
| `app/config_manager.py` | Load/save/validate `app_config.json` and train mapping configs via `jsonschema` |
| `app/event_bus.py` | Typed event dataclasses + `queue.Queue` wrapper |
| `app/gui/` | tkinter panels; all updates via `root.after()`, never blocking |

### TSW API Mechanics

- **Auth:** Every request requires header `DTGCommKey: <key>`. The key is read dynamically from `CommAPIKey.txt` on disk (path in `app_config.json`) — never hardcoded or cached past a connection attempt.
- **GET data:** `GET /get/<node>/<node>.<Endpoint>` — dot separates the final endpoint from the path.
- **SET data:** `PATCH /set/<path>?Value=<value>`
- **Subscriptions (preferred for polling):** `POST /subscription/<endpoint>` with param `Subscription=<id>` to register; `GET /subscription?Subscription=<id>` to bulk-read all registered endpoints in one call. Always DELETE then re-register on app startup to handle game-not-restarted scenarios.
- **Discovery:** `GET /list` and `GET /list/<node>` return the tree. Nodes use `/`, endpoints use `.` notation.

### Train Mapping Config Format

Train configs live in `configs/<train_name>.json` as an **array of mapping dicts**. Each dict has:
- `direction`: `bidirectional` | `midi_to_api` | `api_to_midi`
- `protocol`: `midi` (default, omit for all existing entries) | `osc`
- For MIDI entries: `midi_type`: `cc` | `note` | `pitchbend`
- For OSC entries: `osc_address` (e.g. `"/train/throttle"`), `osc_arg_type`: `float` | `int`
- `api_read_path` (for reads/subscriptions) and `api_set_path` (for writes)
- `api_min`/`api_max` — the **read** range returned by `GetCurrentNotchIndex` or equivalent (used for API → MIDI/OSC output scaling)
- `api_set_min`/`api_set_max` *(optional, default `0.0`/`1.0`)* — the **write** range accepted by `InputValue` (used for MIDI/OSC → API input scaling). TSW `InputValue` endpoints almost always expect `0.0–1.0`, so these can be omitted for the common case.
- `midi_min`/`midi_max` (MIDI) or `osc_out_min`/`osc_out_max` (OSC) with `scaling`
- `poll: true` entries are registered in the subscription; `poll: false` entries are write-only

> **Read/write range asymmetry:** `GetCurrentNotchIndex` returns an integer notch index (e.g. 0–4 for wipers), but `InputValue` almost always accepts a normalised float. The common case is 0.0–1.0 so `api_set_min`/`api_set_max` can be omitted. However some controls use a **signed** range: the Class 323 Power Brake Handle expects `-1.0–1.0` (full brake = -1.0, off = 0.0, full power = 1.0). For those controls set `"api_set_min": -1.0, "api_set_max": 1.0` explicitly. `api_min`/`api_max` still express the notch index range (0–8) used only for the read/display direction.

**Scaling types:**
- `"linear"` — proportional map from `[api_min, api_max]` to `[midi_min, midi_max]` or `[osc_out_min, osc_out_max]`, clamped
- `"m_s_to_kph"` — converts m/s to kph as a float: `value * 3.6`. Used with `osc_arg_type: "float"` for direct kph readouts.
- `"m_s_to_tenths_kph"` — converts m/s to tenths of kph as a direct integer: `round(value * 36)`. Used with `midi_type: "pitchbend"` or `osc_arg_type: "int"` to transmit speed with 0.1 kph resolution.

**Note actions (multi-path with independent values):**
For `midi_type: "note"` entries, you can use `note_on_actions` and `note_off_actions` (lists of `{path, value}` dicts) instead of `note_on_value`/`note_off_value` + `api_set_path`. Each action fires an independent API set, so different paths can receive different values on press vs. release. This is train-config-specific — only include it in configs that need it. Example: AWS button on Class 323 sends `AWS_Reset=1.0` and `VigilancePedal=0.0` on press, then `AWS_Reset=0.0` and `VigilancePedal=1.0` on release.

**Pitchbend notes:**
- `midi_number` is not used; omit it for `pitchbend` entries.
- `midi_min`/`midi_max` range is 0–16383 (14-bit).
- Wire format: status `0xE0 | (channel-1)`, LSB `value & 0x7F`, MSB `(value >> 7) & 0x7F`.

**OSC notes:**
- `python-osc` (`python-osc>=1.8`) is used for the UDP server/client.
- Listen port (inbound) and send host/port (outbound) are configured in `app_config.json` under `"osc"`.
- OSC and MIDI can be active simultaneously; a single TSW endpoint can have separate MIDI and OSC mapping entries.
- OSC is disabled by default (`"osc": {"enabled": false}`); set `"enabled": true` and configure ports to activate.

The mapping engine builds three lookup tables at load time:
- **MIDI → API:** keyed by `(midi_type, midi_channel, midi_number)` — pitchbend uses `midi_number=None`
- **OSC → API:** keyed by `osc_address`
- **API → output:** keyed by `api_read_path` (routes to MIDI or OSC based on `protocol`)

### App Config

`app_config.json` (checked in with safe defaults) controls API host/port, `comm_key_path`, `poll_interval_hz` (default 1.0), MIDI device names, MIDI channel, and `active_train_config` filename.

---

## Key Constraints

- **GUI thread must never block.** API calls and MIDI callbacks run on worker threads; GUI consumes the event bus via periodic `root.after()` callbacks.
- **`CommAPIKey.txt` must be re-read on every connect/reconnect** — the key can be rotated at any time by deleting the file.
- **API paths for controls differ between locomotive types** — each loco needs its own config file. Use `GET /list/CurrentDrivableActor` in TSW to discover the correct paths for a new locomotive.
- **TSW API uses plain HTTP only** (no HTTPS). Always use `http://` in requests.
- `docs/api/` is excluded from git (contains the API PDF and local CommAPIKey).
