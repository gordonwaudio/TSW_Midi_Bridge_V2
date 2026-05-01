"""
mapping_panel.py — Live mapping table view (tkinter Frame).

Displays the active train config as a table.  Shows the layout name at the
top and updates the "Live" column in real time as subscription values arrive.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any


class MappingPanel(ttk.Frame):
    """Tabular view of the active train mapping config."""

    _COLUMNS: list[tuple[str, str, int]] = [
        ("id",        "ID",        110),
        ("label",     "Label",     160),
        ("direction", "Direction",  95),
        ("type",      "Type",       70),
        ("ch",        "Ch",         32),
        ("number",    "Num",        40),
        ("active",    "Active",     48),
        ("live",      "Live",       90),
    ]

    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent)
        self._layout_var   = tk.StringVar(value="")
        # api_read_path → tree item id, for live value updates
        self._iid_by_path: dict[str, str] = {}
        self._build()

    def _build(self) -> None:
        # Layout header
        header = ttk.Frame(self)
        header.pack(side="top", fill="x", padx=4, pady=(4, 2))
        ttk.Label(header, text="Layout:").pack(side="left")
        ttk.Label(
            header,
            textvariable=self._layout_var,
            font=("TkDefaultFont", 9, "bold"),
        ).pack(side="left", padx=(4, 0))

        # Tree + scrollbars
        tree_frame = ttk.Frame(self)
        tree_frame.pack(side="top", fill="both", expand=True)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        col_ids = [c[0] for c in self._COLUMNS]
        v_scroll = ttk.Scrollbar(tree_frame, orient="vertical")
        h_scroll = ttk.Scrollbar(tree_frame, orient="horizontal")

        self._tree = ttk.Treeview(
            tree_frame,
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
            self._tree.column(col_id, width=width, minwidth=24, stretch=False)

        self._tree.grid(row=0, column=0, sticky="nsew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll.grid(row=1, column=0, sticky="ew")

    def load_config(self, config: dict) -> None:
        """Populate the table from a train config dict."""
        self.clear()
        self._layout_var.set(config.get("layout", ""))

        for m in config.get("mappings", []):
            midi_type = m.get("midi_type", "")
            number    = m.get("midi_number", "")
            if midi_type == "pitchbend":
                number = "PB"

            iid = self._tree.insert("", "end", values=(
                m.get("id", ""),
                m.get("label", ""),
                m.get("direction", ""),
                midi_type,
                m.get("midi_channel", ""),
                number,
                "Y" if m.get("active", True) else "—",
                "",
            ))

            path = m.get("api_read_path")
            if path:
                self._iid_by_path[path] = iid

    def update_api_value(self, path: str, value: Any) -> None:
        """Update the Live column for the row whose api_read_path matches *path*."""
        iid = self._iid_by_path.get(path)
        if iid is None:
            return
        try:
            current = list(self._tree.item(iid, "values"))
            if len(current) >= 8:
                current[7] = f"{value:.3f}" if isinstance(value, float) else str(value)
                self._tree.item(iid, values=current)
        except Exception:
            pass

    def clear(self) -> None:
        self._tree.delete(*self._tree.get_children())
        self._iid_by_path = {}
        self._layout_var.set("")
