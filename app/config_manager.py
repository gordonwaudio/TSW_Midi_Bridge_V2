"""
config_manager.py — Load, save, and validate configuration files.

Handles two file types:
- ``app_config.json``            — global application settings (project root)
- ``configs/trains/<name>.json`` — per-train mapping configs (V2 format TBD)

The train config schema is intentionally minimal for this initial release.
The full V2 mapping structure will be defined in a future iteration; this
module will be updated to validate it once the schema is finalised.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema


# ---------------------------------------------------------------------------
# JSON Schemas
# ---------------------------------------------------------------------------

APP_CONFIG_SCHEMA: dict = {
    "type": "object",
    "required": ["api", "midi", "gui", "active_train_config"],
    "properties": {
        "api": {
            "type": "object",
            "required": ["host", "port", "comm_key_path", "poll_interval_hz",
                         "request_timeout_s", "subscription_id"],
            "properties": {
                "host":             {"type": "string"},
                "port":             {"type": "integer", "minimum": 1, "maximum": 65535},
                "comm_key_path":    {"type": "string"},
                "api_host_history": {"type": "array", "items": {"type": "string"}},
                "poll_interval_hz": {"type": "number", "minimum": 0.1, "maximum": 60.0},
                "request_timeout_s":{"type": "number", "minimum": 0.1},
                "subscription_id":  {"type": "integer", "minimum": 1},
            },
        },
        "midi": {
            "type": "object",
            "required": ["channel"],
            "properties": {
                "input_devices":  {"type": "array", "items": {"type": "string"}},
                "output_devices": {"type": "array", "items": {"type": "string"}},
                # Legacy single-device fields (kept for migration only)
                "input_device":   {"type": "string"},
                "output_device":  {"type": "string"},
                "channel":        {"type": "integer", "minimum": 1, "maximum": 16},
            },
        },
        "osc": {
            "type": "object",
            "required": ["enabled"],
            "properties": {
                "enabled":      {"type": "boolean"},
                "listen_host":  {"type": "string"},
                "listen_port":  {"type": "integer", "minimum": 1, "maximum": 65535},
                "send_host":    {"type": "string"},
                "send_port":    {"type": "integer", "minimum": 1, "maximum": 65535},
            },
        },
        "gui": {
            "type": "object",
            "properties": {
                "monitor_buffer_lines": {"type": "integer", "minimum": 10},
                "theme":                {"type": "string"},
            },
        },
        "active_train_config": {"type": "string"},
    },
}

# V2 train config schema — placeholder until the new mapping format is defined.
# The only hard requirement for now is that the file is a valid JSON object.
TRAIN_CONFIG_SCHEMA: dict = {
    "type": "object",
    "description": "V2 train mapping config — full schema TBD.",
}

CONFIGS_DIR     = Path(__file__).parent.parent / "configs" / "trains"
APP_CONFIG_PATH = Path(__file__).parent.parent / "app_config.json"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ConfigManager:
    """Loads, validates, and persists both configuration file types."""

    # --- App config ---------------------------------------------------------

    def load_app_config(self, path: Path | str = APP_CONFIG_PATH) -> dict:
        """Load and validate ``app_config.json``.

        Returns the parsed config dict.
        Raises ``jsonschema.ValidationError`` on schema violations.
        Raises ``FileNotFoundError`` / ``json.JSONDecodeError`` on I/O errors.
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.validate_app_config(data)
        return data

    def save_app_config(self, config: dict,
                        path: Path | str = APP_CONFIG_PATH) -> None:
        """Write *config* to disk as pretty-printed JSON."""
        Path(path).write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )

    # --- Train mapping config -----------------------------------------------

    def load_train_config(self, path: Path | str) -> dict:
        """Load a train mapping config file.

        Returns the raw config dict.  Full schema validation will be added
        once the V2 mapping format is finalised.
        Raises ``FileNotFoundError`` / ``json.JSONDecodeError`` on I/O errors.
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.validate_train_config(data)
        return data

    def validate_train_config(self, data: Any) -> None:
        """Validate *data* against the train config schema."""
        jsonschema.validate(instance=data, schema=TRAIN_CONFIG_SCHEMA)

    def validate_app_config(self, data: Any) -> None:
        """Validate *data* against the app config schema."""
        jsonschema.validate(instance=data, schema=APP_CONFIG_SCHEMA)

    # --- Discovery ----------------------------------------------------------

    def list_train_configs(self,
                           configs_dir: Path | str = CONFIGS_DIR) -> list[str]:
        """Return a sorted list of ``*.json`` filenames in *configs_dir*."""
        d = Path(configs_dir)
        if not d.is_dir():
            return []
        return sorted(p.name for p in d.glob("*.json"))
