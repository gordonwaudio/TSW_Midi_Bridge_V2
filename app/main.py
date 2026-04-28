"""
main.py — Application entry point.

Wires the four subsystems together, then hands control to the tkinter main loop.

    python -m app.main
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)

from app.config_manager import APP_CONFIG_PATH, ConfigManager
from app.event_bus import EventBus
from app.api_client import ApiClient
from app.midi_manager import MidiManager
from app.osc_manager import OscManager
from app.mapping_engine import MappingEngine
from app.gui.main_window import MainWindow


def main() -> None:
    # 1. Load application config
    config_mgr = ConfigManager()
    try:
        app_config = config_mgr.load_app_config(APP_CONFIG_PATH)
    except FileNotFoundError:
        print(f"app_config.json not found at {APP_CONFIG_PATH}. "
              "Using built-in defaults.", file=sys.stderr)
        raise SystemExit(1)

    # 1b. Migrate legacy single-device config to multi-device format
    midi_cfg = app_config.setdefault("midi", {})
    if "input_device" in midi_cfg and "input_devices" not in midi_cfg:
        midi_cfg["input_devices"] = [midi_cfg.pop("input_device")]
    if "output_device" in midi_cfg and "output_devices" not in midi_cfg:
        midi_cfg["output_devices"] = [midi_cfg.pop("output_device")]

    # 2. Create shared event bus
    bus = EventBus()

    # 3. Instantiate subsystems (no I/O yet — connect is user-initiated)
    api_client      = ApiClient(bus, app_config["api"])
    midi_manager    = MidiManager(bus)
    osc_manager     = OscManager(bus, app_config.get("osc", {"enabled": False}))
    mapping_engine  = MappingEngine(bus)

    # 4. Start mapping engine processing thread
    mapping_engine.start()

    # 5. Auto-start OSC if enabled in config
    osc_cfg = app_config.get("osc", {})
    if osc_cfg.get("enabled", False):
        osc_manager.start_server()
        osc_manager.start_client()

    # 6. Build and run GUI (blocks until window is closed)
    window = MainWindow(
        bus=bus,
        app_config=app_config,
        config_mgr=config_mgr,
        api_client=api_client,
        midi_manager=midi_manager,
        osc_manager=osc_manager,
        mapping_engine=mapping_engine,
    )
    try:
        window.run()
    finally:
        mapping_engine.stop()
        api_client.disconnect()
        midi_manager.close_input()
        midi_manager.close_output()
        osc_manager.stop()


if __name__ == "__main__":
    main()
