"""
main_window.py — Top-level tkinter window.

Layout::

    ┌─────────────────────────────────────────────────┐
    │  Connection bar  (API status · MIDI IN · OUT)   │
    ├──────────────┬──────────────────────────────────┤
    │              │  [Monitor tab] [Mapping tab]      │
    │  Settings    │                                   │
    │  Panel       │  (tab content)                    │
    │              │                                   │
    ├──────────────┴──────────────────────────────────┤
    │  Status bar                                      │
    └─────────────────────────────────────────────────┘

All event-bus updates arrive via ``root.after()`` periodic callbacks —
the GUI thread never blocks on I/O.
"""

from __future__ import annotations

import queue
import tkinter as tk
from tkinter import ttk

from app.api_client import ApiClient
from app.config_manager import ConfigManager
from app.event_bus import (
    ConnectionState,
    ConnectionStateEvent,
    ConnectionSubsystem,
    ErrorEvent,
    EventBus,
    MidiCCEvent,
    MidiNoteOffEvent,
    MidiNoteOnEvent,
    MidiPitchBendEvent,
    MidiSendCCEvent,
    MidiSendNoteOffEvent,
    MidiSendNoteOnEvent,
    MidiSendPitchBendEvent,
    MonitorDirection,
    MonitorEvent,
    SubscriptionResultEvent,
)
from app.mapping_engine import MappingEngine
from app.midi_manager import MidiManager
from app.osc_manager import OscManager
from app.gui.settings_panel import SettingsPanel
from app.gui.monitor_panel import MonitorPanel
from app.gui.mapping_panel import MappingPanel

# How often the GUI polls the event bus (ms)
_BUS_POLL_MS = 50

# State → (label text, colour)
_STATE_DISPLAY = {
    ConnectionState.DISCONNECTED: ("Disconnected", "gray"),
    ConnectionState.CONNECTING:   ("Connecting…",  "orange"),
    ConnectionState.CONNECTED:    ("Connected",    "green"),
    ConnectionState.ERROR:        ("Error",        "red"),
}


class MainWindow:
    """Root window — owns the tk.Tk instance and the bus-poll loop."""

    def __init__(
        self,
        bus: EventBus,
        app_config: dict,
        config_mgr: ConfigManager,
        api_client: ApiClient,
        midi_manager: MidiManager,
        osc_manager: OscManager,
        mapping_engine: MappingEngine,
    ) -> None:
        self._bus = bus
        self._app_config = app_config
        self._config_mgr = config_mgr
        self._api_client = api_client
        self._midi_manager = midi_manager
        self._osc_manager = osc_manager
        self._mapping_engine = mapping_engine

        # GUI events destined for this window
        self._gui_queue: queue.Queue = queue.Queue()
        bus.subscribe(
            [ConnectionStateEvent, ErrorEvent, MonitorEvent,
             MidiCCEvent, MidiNoteOnEvent, MidiNoteOffEvent, MidiPitchBendEvent,
             MidiSendCCEvent, MidiSendNoteOnEvent, MidiSendNoteOffEvent, MidiSendPitchBendEvent,
             SubscriptionResultEvent],
            self._gui_queue,
        )

        self._root: tk.Tk | None = None

        # Connection state labels (populated in _build_ui)
        self._conn_labels: dict[ConnectionSubsystem, tk.Label] = {}
        self._status_var: tk.StringVar | None = None
        self._monitor_panel: MonitorPanel | None = None
        self._mapping_panel: MappingPanel | None = None

    def run(self) -> None:
        """Build the window and enter the tkinter main loop (blocks)."""
        self._root = tk.Tk()
        self._root.title("TSW Midi Bridge V2")
        self._root.minsize(900, 560)
        self._build_ui()
        self._root.after(_BUS_POLL_MS, self._poll_bus)
        self._root.mainloop()

    def _build_ui(self) -> None:
        """Create all widgets."""
        root = self._root

        # ── Connection bar ───────────────────────────────────────────────────
        conn_bar = ttk.Frame(root, relief="raised", padding=4)
        conn_bar.pack(side="top", fill="x")

        ttk.Label(conn_bar, text="Status:").pack(side="left", padx=(0, 6))
        for subsystem in (
            ConnectionSubsystem.API,
            ConnectionSubsystem.MIDI_IN,
            ConnectionSubsystem.MIDI_OUT,
            ConnectionSubsystem.OSC_IN,
            ConnectionSubsystem.OSC_OUT,
        ):
            ttk.Label(conn_bar, text=f"{subsystem.value}:").pack(side="left")
            lbl = tk.Label(conn_bar, text="—", width=12, anchor="w",
                           foreground="gray")
            lbl.pack(side="left", padx=(0, 10))
            self._conn_labels[subsystem] = lbl

        # ── Main content area ────────────────────────────────────────────────
        content = ttk.Frame(root)
        content.pack(side="top", fill="both", expand=True)

        # Left: settings sidebar
        settings = SettingsPanel(
            content,
            app_config=self._app_config,
            config_mgr=self._config_mgr,
            api_client=self._api_client,
            midi_manager=self._midi_manager,
            osc_manager=self._osc_manager,
            mapping_engine=self._mapping_engine,
            on_config_loaded=self._on_config_loaded,
        )
        settings.pack(side="left", fill="y", padx=4, pady=4)

        ttk.Separator(content, orient="vertical").pack(side="left", fill="y")

        # Right: tabbed monitor + mapping
        notebook = ttk.Notebook(content)
        notebook.pack(side="left", fill="both", expand=True, padx=4, pady=4)

        max_lines = self._app_config.get("gui", {}).get("monitor_buffer_lines", 200)
        self._monitor_panel = MonitorPanel(notebook, max_lines=max_lines)
        notebook.add(self._monitor_panel, text="MIDI Monitor")

        self._mapping_panel = MappingPanel(notebook)
        notebook.add(self._mapping_panel, text="Mapping")

        # ── Status bar ───────────────────────────────────────────────────────
        self._status_var = tk.StringVar(value="Ready.")
        status_bar = ttk.Label(root, textvariable=self._status_var,
                               relief="sunken", anchor="w", padding=(4, 2))
        status_bar.pack(side="bottom", fill="x")

        # Load initial train config if one is set
        active = self._app_config.get("active_train_config", "")
        if active:
            settings.load_train_config(active)

    def _poll_bus(self) -> None:
        """Drain ``_gui_queue`` and dispatch events to the correct handler."""
        dispatch = {
            ConnectionStateEvent: self._on_connection_state,
            ErrorEvent:           self._on_error,
            MonitorEvent:         self._on_monitor_event,
            SubscriptionResultEvent: self._on_subscription_result,
            # MIDI input events → monitor
            MidiCCEvent:          self._on_midi_in_cc,
            MidiNoteOnEvent:      self._on_midi_in_note_on,
            MidiNoteOffEvent:     self._on_midi_in_note_off,
            MidiPitchBendEvent:   self._on_midi_in_pitch_bend,
            # MIDI send events → monitor
            MidiSendCCEvent:      self._on_midi_out_cc,
            MidiSendNoteOnEvent:  self._on_midi_out_note_on,
            MidiSendNoteOffEvent: self._on_midi_out_note_off,
            MidiSendPitchBendEvent: self._on_midi_out_pitch_bend,
        }
        while True:
            try:
                event = self._gui_queue.get_nowait()
                handler = dispatch.get(type(event))
                if handler:
                    handler(event)
            except queue.Empty:
                break
        if self._root:
            self._root.after(_BUS_POLL_MS, self._poll_bus)

    def _on_connection_state(self, event: ConnectionStateEvent) -> None:
        """Update connection indicator labels."""
        lbl = self._conn_labels.get(event.subsystem)
        if lbl is None:
            return
        text, colour = _STATE_DISPLAY.get(event.state, ("?", "gray"))
        lbl.configure(text=text, foreground=colour)
        if event.message:
            self._set_status(f"{event.subsystem.value}: {event.message}")

    def _on_error(self, event: ErrorEvent) -> None:
        """Display message in status bar."""
        self._set_status(f"[{event.source}] {event.message}")

    def _on_monitor_event(self, event: MonitorEvent) -> None:
        if self._monitor_panel:
            self._monitor_panel.append(event)

    def _on_subscription_result(self, event: SubscriptionResultEvent) -> None:
        """Forward live values to the mapping panel."""
        if self._mapping_panel is None:
            return
        for path, value in event.data.items():
            self._mapping_panel.update_api_value(path, value)

    # --- MIDI input events (in → monitor) -----------------------------------

    def _on_midi_in_cc(self, event: MidiCCEvent) -> None:
        if self._monitor_panel:
            self._monitor_panel.append(MonitorEvent(
                direction=MonitorDirection.IN, midi_type="CC",
                channel=event.channel, number=event.number, value=event.value,
            ))

    def _on_midi_in_note_on(self, event: MidiNoteOnEvent) -> None:
        if self._monitor_panel:
            self._monitor_panel.append(MonitorEvent(
                direction=MonitorDirection.IN, midi_type="NoteOn",
                channel=event.channel, number=event.number, value=event.velocity,
            ))

    def _on_midi_in_note_off(self, event: MidiNoteOffEvent) -> None:
        if self._monitor_panel:
            self._monitor_panel.append(MonitorEvent(
                direction=MonitorDirection.IN, midi_type="NoteOff",
                channel=event.channel, number=event.number,
            ))

    def _on_midi_in_pitch_bend(self, event: MidiPitchBendEvent) -> None:
        if self._monitor_panel:
            self._monitor_panel.append(MonitorEvent(
                direction=MonitorDirection.IN, midi_type="PitchBend",
                channel=event.channel, number=0, value=event.value,
            ))

    # --- MIDI send events (out → monitor) -----------------------------------

    def _on_midi_out_cc(self, event: MidiSendCCEvent) -> None:
        if self._monitor_panel:
            self._monitor_panel.append(MonitorEvent(
                direction=MonitorDirection.OUT, midi_type="CC",
                channel=event.channel, number=event.number, value=event.value,
            ))

    def _on_midi_out_note_on(self, event: MidiSendNoteOnEvent) -> None:
        if self._monitor_panel:
            self._monitor_panel.append(MonitorEvent(
                direction=MonitorDirection.OUT, midi_type="NoteOn",
                channel=event.channel, number=event.number, value=event.velocity,
            ))

    def _on_midi_out_note_off(self, event: MidiSendNoteOffEvent) -> None:
        if self._monitor_panel:
            self._monitor_panel.append(MonitorEvent(
                direction=MonitorDirection.OUT, midi_type="NoteOff",
                channel=event.channel, number=event.number,
            ))

    def _on_midi_out_pitch_bend(self, event: MidiSendPitchBendEvent) -> None:
        if self._monitor_panel:
            self._monitor_panel.append(MonitorEvent(
                direction=MonitorDirection.OUT, midi_type="PitchBend",
                channel=event.channel, number=0, value=event.value,
            ))

    # -----------------------------------------------------------------------

    def _on_config_loaded(self, config: dict) -> None:
        """Called by SettingsPanel after a new train config is loaded."""
        if self._mapping_panel:
            self._mapping_panel.load_config(config)

    def _set_status(self, message: str) -> None:
        if self._status_var:
            self._status_var.set(message)
