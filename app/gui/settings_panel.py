"""
settings_panel.py — Left-sidebar settings panel (tkinter Frame).

Controls:
  - MIDI Input devices   (Listbox multi-select, populated from midi_manager)
  - MIDI Output devices  (Listbox multi-select)
  - MIDI Channel         (Spinbox 1–16)
  - Poll Rate Hz         (Spinbox 0.1–60)
  - Train Config         (OptionMenu, populated from configs/trains/)
  - API Host / Port      (Entry fields)
  - CommAPIKey path      (Entry + Browse button)
  - OSC enabled toggle, listen port, send host, send port
  - Connect/Disconnect   (buttons for API and MIDI independently)
  - Save Settings        (button)

All settings changes are written back to ``app_config.json`` immediately.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from app.api_client import ApiClient
from app.config_manager import CONFIGS_DIR, ConfigManager
from app.mapping_engine import MappingEngine
from app.midi_manager import MidiManager
from app.osc_manager import OscManager


class SettingsPanel(ttk.Frame):
    """Sidebar frame containing all user-configurable settings."""

    def __init__(
        self,
        parent: tk.Widget,
        app_config: dict,
        config_mgr: ConfigManager,
        api_client: ApiClient,
        midi_manager: MidiManager,
        osc_manager: OscManager,
        mapping_engine: MappingEngine,
        on_config_loaded: Callable[[dict], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._app_config      = app_config
        self._config_mgr      = config_mgr
        self._api_client      = api_client
        self._midi_manager    = midi_manager
        self._osc_manager     = osc_manager
        self._mapping_engine  = mapping_engine
        self._on_config_loaded = on_config_loaded

        # MIDI device lists from config (used to restore selections after refresh)
        midi_cfg = app_config.get("midi", {})
        self._saved_inputs  = list(midi_cfg.get("input_devices", []))
        self._saved_outputs = list(midi_cfg.get("output_devices", []))

        # Tk variables — initialised before _build() so _build can bind them
        self._midi_ch_var    = tk.IntVar(value=midi_cfg.get("channel", 1))
        self._poll_hz_var    = tk.DoubleVar(value=app_config["api"].get("poll_interval_hz", 1.0))
        self._train_cfg_var  = tk.StringVar(value=app_config.get("active_train_config", ""))
        self._api_host_var   = tk.StringVar(value=app_config["api"].get("host", "127.0.0.1"))
        self._api_host_history: list[str] = list(
            app_config["api"].get("api_host_history", ["127.0.0.1"])
        )
        self._api_port_var   = tk.IntVar(value=app_config["api"].get("port", 31270))
        key_path = app_config["api"].get("comm_key_path", "")
        self._key_path_var   = tk.StringVar(value=key_path)
        self._key_file_var   = tk.StringVar(value=Path(key_path).name if key_path else "")

        # Widget refs set during _build
        self._api_host_combo: ttk.Combobox | None = None
        self._key_file_combo: ttk.Combobox | None = None
        osc = app_config.get("osc", {})
        self._osc_enabled_var   = tk.BooleanVar(value=osc.get("enabled", False))
        self._osc_listen_var    = tk.IntVar(value=osc.get("listen_port", 9000))
        self._osc_send_host_var = tk.StringVar(value=osc.get("send_host", "127.0.0.1"))
        self._osc_send_port_var = tk.IntVar(value=osc.get("send_port", 9001))

        # Listbox widgets (set during _build)
        self._midi_in_listbox:  tk.Listbox | None = None
        self._midi_out_listbox: tk.Listbox | None = None

        self._build()
        self.refresh_device_lists()
        self.refresh_train_config_list()
        self._refresh_key_files()

    # -----------------------------------------------------------------------
    # Build
    # -----------------------------------------------------------------------

    def _build(self) -> None:
        """Create and grid all widgets."""
        self.configure(padding=6)
        row = 0

        def lbl(text: str, r: int) -> None:
            ttk.Label(self, text=text).grid(row=r, column=0, sticky="w", pady=2)

        def entry(var: tk.Variable, r: int, width: int = 18) -> ttk.Entry:
            e = ttk.Entry(self, textvariable=var, width=width)
            e.grid(row=r, column=1, sticky="ew", pady=2)
            return e

        # ── MIDI ────────────────────────────────────────────────────────────
        ttk.Label(self, text="MIDI", font=("TkDefaultFont", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 2))
        row += 1

        # Input devices listbox
        lbl("Input devices:", row)
        self._midi_in_listbox = self._make_device_listbox(row)
        row += 1

        # Output devices listbox
        lbl("Output devices:", row)
        self._midi_out_listbox = self._make_device_listbox(row)
        row += 1

        # Refresh + channel on same row-block
        refresh_frame = ttk.Frame(self)
        refresh_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(refresh_frame, text="Refresh Devices",
                   command=self.refresh_device_lists).pack(side="left")
        ttk.Label(refresh_frame, text="  Ch:").pack(side="left")
        ttk.Spinbox(refresh_frame, from_=1, to=16, textvariable=self._midi_ch_var,
                    width=4).pack(side="left")
        row += 1

        midi_btn_frame = ttk.Frame(self)
        midi_btn_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(midi_btn_frame, text="Connect MIDI",
                   command=self._on_connect_midi).pack(side="left", expand=True, fill="x")
        ttk.Button(midi_btn_frame, text="Disconnect",
                   command=self._on_disconnect_midi).pack(side="left", expand=True, fill="x")
        row += 1

        ttk.Separator(self, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1

        # ── TSW API ─────────────────────────────────────────────────────────
        ttk.Label(self, text="TSW API", font=("TkDefaultFont", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 2))
        row += 1

        lbl("Host:", row)
        self._api_host_combo = ttk.Combobox(
            self, textvariable=self._api_host_var,
            values=self._api_host_history, width=18,
        )
        self._api_host_combo.grid(row=row, column=1, sticky="ew", pady=2)
        row += 1

        lbl("Port:", row)
        ttk.Spinbox(self, from_=1, to=65535, textvariable=self._api_port_var,
                    width=8).grid(row=row, column=1, sticky="w", pady=2)
        row += 1

        lbl("CommAPIKey path:", row)
        key_frame = ttk.Frame(self)
        key_frame.grid(row=row, column=1, sticky="ew", pady=2)
        ttk.Entry(key_frame, textvariable=self._key_path_var, width=14).pack(
            side="left", expand=True, fill="x")
        ttk.Button(key_frame, text="…", width=2,
                   command=self._on_browse_key_path).pack(side="left")
        row += 1

        lbl("Key file:", row)
        key_file_frame = ttk.Frame(self)
        key_file_frame.grid(row=row, column=1, sticky="ew", pady=2)
        self._key_file_combo = ttk.Combobox(
            key_file_frame, textvariable=self._key_file_var, width=14,
        )
        self._key_file_combo.pack(side="left", expand=True, fill="x")
        self._key_file_combo.bind("<<ComboboxSelected>>", self._on_key_file_selected)
        ttk.Button(key_file_frame, text="↺", width=2,
                   command=self._refresh_key_files).pack(side="left")
        row += 1

        lbl("Poll rate (Hz):", row)
        ttk.Spinbox(self, from_=0.1, to=60.0, increment=0.5,
                    textvariable=self._poll_hz_var, width=6,
                    format="%.1f").grid(row=row, column=1, sticky="w", pady=2)
        row += 1

        lbl("Train config:", row)
        self._train_cfg_menu = ttk.OptionMenu(
            self, self._train_cfg_var, "",
            command=self._on_train_config_changed,
        )
        self._train_cfg_menu.grid(row=row, column=1, sticky="ew", pady=2)
        row += 1

        api_btn_frame = ttk.Frame(self)
        api_btn_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(api_btn_frame, text="Connect API",
                   command=self._on_connect_api).pack(side="left", expand=True, fill="x")
        ttk.Button(api_btn_frame, text="Disconnect",
                   command=self._on_disconnect_api).pack(side="left", expand=True, fill="x")
        row += 1

        ttk.Separator(self, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1

        # ── OSC ─────────────────────────────────────────────────────────────
        ttk.Label(self, text="OSC", font=("TkDefaultFont", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 2))
        row += 1

        lbl("Enabled:", row)
        ttk.Checkbutton(self, variable=self._osc_enabled_var).grid(
            row=row, column=1, sticky="w", pady=2)
        row += 1

        lbl("Listen port:", row)
        ttk.Spinbox(self, from_=1, to=65535, textvariable=self._osc_listen_var,
                    width=8).grid(row=row, column=1, sticky="w", pady=2)
        row += 1

        lbl("Send host:", row)
        entry(self._osc_send_host_var, row)
        row += 1

        lbl("Send port:", row)
        ttk.Spinbox(self, from_=1, to=65535, textvariable=self._osc_send_port_var,
                    width=8).grid(row=row, column=1, sticky="w", pady=2)
        row += 1

        ttk.Separator(self, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1

        # ── Save ────────────────────────────────────────────────────────────
        ttk.Button(self, text="Save Settings",
                   command=self._save_settings).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=4)
        row += 1

        self.columnconfigure(1, weight=1)

    def _make_device_listbox(self, grid_row: int) -> tk.Listbox:
        """Create a small multi-select Listbox with scrollbar in column 1."""
        frame = ttk.Frame(self)
        frame.grid(row=grid_row, column=1, sticky="ew", pady=2)
        frame.columnconfigure(0, weight=1)

        lb = tk.Listbox(frame, selectmode=tk.EXTENDED, height=4,
                        exportselection=False, activestyle="dotbox")
        lb.grid(row=0, column=0, sticky="ew")

        sb = ttk.Scrollbar(frame, orient="vertical", command=lb.yview)
        sb.grid(row=0, column=1, sticky="ns")
        lb.configure(yscrollcommand=sb.set)

        return lb

    # -----------------------------------------------------------------------
    # Refresh helpers
    # -----------------------------------------------------------------------

    def refresh_device_lists(self) -> None:
        """Re-populate MIDI device Listboxes and restore previous selection."""
        inputs  = [name for _, name in self._midi_manager.list_input_devices()]
        outputs = [name for _, name in self._midi_manager.list_output_devices()]

        self._populate_listbox(self._midi_in_listbox,  inputs,  self._saved_inputs)
        self._populate_listbox(self._midi_out_listbox, outputs, self._saved_outputs)

    def _populate_listbox(
        self,
        lb: tk.Listbox | None,
        names: list[str],
        selected: list[str],
    ) -> None:
        """Fill *lb* with *names*, selecting any that appear in *selected*."""
        if lb is None:
            return
        lb.delete(0, tk.END)
        for name in names:
            lb.insert(tk.END, name)
        for i, name in enumerate(names):
            if name in selected:
                lb.selection_set(i)

    def refresh_train_config_list(self) -> None:
        """Re-populate the train config dropdown from the configs/trains/ directory."""
        configs = self._config_mgr.list_train_configs()
        menu    = self._train_cfg_menu["menu"]
        menu.delete(0, "end")
        for name in configs:
            menu.add_command(label=name,
                             command=lambda n=name: self._train_cfg_var.set(n))
        if configs and not self._train_cfg_var.get():
            self._train_cfg_var.set(configs[0])

    # -----------------------------------------------------------------------
    # Selection helpers
    # -----------------------------------------------------------------------

    def _get_selected_inputs(self) -> list[str]:
        """Return names of currently selected MIDI input devices."""
        return self._get_listbox_selection(self._midi_in_listbox)

    def _get_selected_outputs(self) -> list[str]:
        """Return names of currently selected MIDI output devices."""
        return self._get_listbox_selection(self._midi_out_listbox)

    def _get_listbox_selection(self, lb: tk.Listbox | None) -> list[str]:
        if lb is None:
            return []
        return [lb.get(i) for i in lb.curselection()]

    # -----------------------------------------------------------------------
    # Button callbacks
    # -----------------------------------------------------------------------

    def _on_connect_api(self) -> None:
        self._save_settings()
        endpoints = self._mapping_engine.get_poll_endpoints()
        self._api_client.connect(endpoints)

    def _on_disconnect_api(self) -> None:
        self._api_client.disconnect()

    def _on_connect_midi(self) -> None:
        self._save_settings()
        in_devs  = self._get_selected_inputs()
        out_devs = self._get_selected_outputs()
        if in_devs:
            self._midi_manager.open_inputs(in_devs)
        if out_devs:
            self._midi_manager.open_outputs(out_devs)

    def _on_disconnect_midi(self) -> None:
        self._midi_manager.close_all_inputs()
        self._midi_manager.close_all_outputs()

    def _on_browse_key_path(self) -> None:
        """Open a file dialog to select a CommAPIKey file."""
        current = self._key_path_var.get()
        initial_dir = str(Path(current).parent) if current else ""
        path = filedialog.askopenfilename(
            title="Select CommAPIKey file",
            initialdir=initial_dir or None,
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self._key_path_var.set(path)
            self._key_file_var.set(Path(path).name)
            self._app_config["api"]["comm_key_path"] = path
            self._config_mgr.save_app_config(self._app_config)
            self._refresh_key_files()

    def _refresh_key_files(self) -> None:
        """Populate the key-file combobox with *.txt files in the key directory."""
        if self._key_file_combo is None:
            return
        path = self._key_path_var.get()
        if not path:
            return
        d = Path(path).parent
        if not d.is_dir():
            return
        files = sorted(p.name for p in d.glob("*.txt"))
        self._key_file_combo["values"] = files

    def _on_key_file_selected(self, *_) -> None:
        """Update comm_key_path when a different key file is chosen from the combobox."""
        filename = self._key_file_var.get()
        if not filename:
            return
        current = self._key_path_var.get()
        if not current:
            return
        new_path = str(Path(current).parent / filename)
        self._key_path_var.set(new_path)

    def _on_train_config_changed(self, *_) -> None:
        """Load the newly selected train config and re-register subscriptions."""
        name = self._train_cfg_var.get()
        if name:
            self.load_train_config(name)

    def load_train_config(self, name: str) -> None:
        """Load *name* from configs/trains/, update the engine, and reconnect API."""
        path = Path(CONFIGS_DIR) / name
        try:
            config = self._config_mgr.load_train_config(path)
        except Exception as exc:
            messagebox.showerror("Config Error", f"Cannot load '{name}':\n{exc}")
            return
        self._mapping_engine.load_config(config)
        self._app_config["active_train_config"] = name
        self._config_mgr.save_app_config(self._app_config)
        if self._on_config_loaded:
            self._on_config_loaded(config)
        # Reconnect API with updated endpoints if already connected
        if self._api_client.is_connected:
            self._api_client.connect(self._mapping_engine.get_poll_endpoints())

    def _save_settings(self) -> None:
        """Persist current widget values to ``app_config.json``."""
        in_devs  = self._get_selected_inputs()
        out_devs = self._get_selected_outputs()

        # Keep _saved_* in sync so refresh_device_lists() restores correctly
        self._saved_inputs  = in_devs
        self._saved_outputs = out_devs

        self._app_config["midi"]["input_devices"]  = in_devs
        self._app_config["midi"]["output_devices"] = out_devs
        self._app_config["midi"]["channel"]         = int(self._midi_ch_var.get())

        current_host = self._api_host_var.get()
        history = list(self._app_config["api"].get("api_host_history", []))
        if current_host in history:
            history.remove(current_host)
        history.insert(0, current_host)
        self._api_host_history = history[:5]
        self._app_config["api"]["api_host_history"] = self._api_host_history
        if self._api_host_combo is not None:
            self._api_host_combo["values"] = self._api_host_history

        self._app_config["api"]["host"]             = current_host
        self._app_config["api"]["port"]             = int(self._api_port_var.get())
        self._app_config["api"]["comm_key_path"]    = self._key_path_var.get()
        self._app_config["api"]["poll_interval_hz"] = float(self._poll_hz_var.get())
        self._app_config["active_train_config"]     = self._train_cfg_var.get()

        osc = self._app_config.setdefault("osc", {})
        osc["enabled"]     = bool(self._osc_enabled_var.get())
        osc["listen_port"] = int(self._osc_listen_var.get())
        osc["send_host"]   = self._osc_send_host_var.get()
        osc["send_port"]   = int(self._osc_send_port_var.get())

        self._config_mgr.save_app_config(self._app_config)
