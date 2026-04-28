"""
monitor_panel.py — Scrolling MIDI monitor pane (tkinter Frame).

Displays a capped log of MIDI messages received and sent.

Format of each line::

    [HH:MM:SS.mmm] IN  CC     Ch:1  #7   Val:64
    [HH:MM:SS.mmm] OUT NoteOn Ch:1  #60  Vel:64

IN lines are coloured blue; OUT lines green.
Buffer is capped at ``app_config["gui"]["monitor_buffer_lines"]`` lines.
"""

from __future__ import annotations

import datetime
import tkinter as tk
from tkinter import ttk

from app.event_bus import (
    MidiCCEvent,
    MidiNoteOffEvent,
    MidiNoteOnEvent,
    MidiSendCCEvent,
    MidiSendNoteOffEvent,
    MidiSendNoteOnEvent,
    MonitorDirection,
    MonitorEvent,
)


class MonitorPanel(ttk.Frame):
    """Scrollable text widget showing live MIDI traffic."""

    _TAG_IN  = "midi_in"
    _TAG_OUT = "midi_out"

    def __init__(self, parent: tk.Widget, max_lines: int = 200) -> None:
        super().__init__(parent)
        self._max_lines = max_lines
        self._line_count = 0
        self._build()

    def _build(self) -> None:
        """Create the Text widget, scrollbar, and Clear button."""
        toolbar = ttk.Frame(self)
        toolbar.pack(side="top", fill="x")
        ttk.Button(toolbar, text="Clear", command=self.clear).pack(side="right")

        frame = ttk.Frame(self)
        frame.pack(side="top", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(frame, orient="vertical")
        self._text = tk.Text(
            frame,
            state="disabled",
            wrap="none",
            yscrollcommand=scrollbar.set,
            font=("Courier", 10),
            background="#1e1e1e",
            foreground="#d4d4d4",
        )
        scrollbar.configure(command=self._text.yview)
        scrollbar.pack(side="right", fill="y")
        self._text.pack(side="left", fill="both", expand=True)

        self._text.tag_configure(self._TAG_IN,  foreground="#6ab7ff")  # blue
        self._text.tag_configure(self._TAG_OUT, foreground="#6ef06e")  # green

    def append(self, event: MonitorEvent) -> None:
        """Format *event* and append it to the monitor text widget.

        Trims the buffer to ``_max_lines`` if necessary.
        Must be called from the GUI thread only.
        """
        line = self._format_line(event) + "\n"
        tag  = self._TAG_IN if event.direction == MonitorDirection.IN else self._TAG_OUT

        self._text.configure(state="normal")
        self._text.insert("end", line, tag)
        self._line_count += 1

        if self._line_count > self._max_lines:
            excess = self._line_count - self._max_lines
            self._text.delete("1.0", f"{excess + 1}.0")
            self._line_count = self._max_lines

        self._text.configure(state="disabled")
        self._text.see("end")

    def clear(self) -> None:
        """Clear all monitor lines."""
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")
        self._line_count = 0

    @staticmethod
    def _format_line(event: MonitorEvent) -> str:
        """Return a formatted log line string for *event*."""
        now  = datetime.datetime.now()
        ts   = now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        dir_ = "IN " if event.direction == MonitorDirection.IN else "OUT"
        typ  = event.midi_type.ljust(6)
        ch   = f"Ch:{event.channel}"
        num  = f"#{event.number}"

        if event.value is not None:
            if event.midi_type == "NoteOn":
                val_str = f"Vel:{event.value}"
            else:
                val_str = f"Val:{event.value}"
        else:
            val_str = ""

        return f"[{ts}] {dir_} {typ} {ch:<5} {num:<5} {val_str}"
