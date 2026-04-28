"""
mapping_panel.py — Live mapping table view (tkinter Frame).

Displays a summary of the active train config as a read-only table.
The exact columns will evolve once the V2 train config format is finalised.
For now the panel shows the raw keys of the loaded config object so the
UI remains usable during development.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any


class MappingPanel(ttk.Frame):
    """Tabular view of the active train mapping config."""

    # Column definitions: (id, heading, width)
    _COLUMNS: list[tuple[str, str, int]] = [
        ("key",   "Config Key",  200),
        ("value", "Value",       400),
    ]

    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        """Create the Treeview widget and scrollbars."""
        col_ids = [c[0] for c in self._COLUMNS]

        v_scroll = ttk.Scrollbar(self, orient="vertical")
        h_scroll = ttk.Scrollbar(self, orient="horizontal")

        self._tree = ttk.Treeview(
            self,
            columns=col_ids,
            show="headings",
            yscrollcommand=v_scroll.set,
            xscrollcommand=h_scroll.set,
            selectmode="browse",
        )
        v_scroll.configure(command=self._tree.yview)
        h_scroll.configure(command=self._tree.xview)

        for col_id, heading, width in self._COLUMNS:
            self._tree.heading(col_id, text=heading)
            self._tree.column(col_id, width=width, minwidth=30, stretch=False)

        self._tree.grid(row=0, column=0, sticky="nsew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll.grid(row=1, column=0, sticky="ew")

        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

    def load_config(self, config: dict) -> None:
        """Populate the table from a train config dict.

        Shows top-level keys and a short string representation of their values.
        This will be replaced with a proper mapping table once the V2 config
        format is defined.
        """
        self.clear()
        for key, value in config.items():
            display_val = self._fmt(value)
            self._tree.insert("", "end", values=(key, display_val))

    def update_api_value(self, path: str, value: Any) -> None:
        """No-op placeholder — will be wired to live API values in V2."""
        pass

    def clear(self) -> None:
        """Remove all rows from the table."""
        self._tree.delete(*self._tree.get_children())

    @staticmethod
    def _fmt(value: Any) -> str:
        if isinstance(value, float):
            return f"{value:.3f}"
        if isinstance(value, (list, dict)):
            s = str(value)
            return s[:80] + "…" if len(s) > 80 else s
        return str(value)
