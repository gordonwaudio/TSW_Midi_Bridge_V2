"""
app.py — TSW API Interrogator main window.

Provides three raw API operations against the TSW6 External Interface:
  LIST  — GET  /list/<suffix>      → large scrollable JSON with search/copy
  GET   — GET  /get/<path>         → 3-line response display
  SET   — PATCH /set/<path>?Value= → float entry with ±step, 3-line response

All HTTP calls run on worker threads; results are delivered back to the
GUI thread via root.after(0, ...).  Reads/writes the shared app_config.json
so host, port, and CommAPIKey settings are shared with the bridge.
"""

from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Config I/O  (shared with the bridge via app_config.json)
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent.parent / "app_config.json"
_SEARCH_TAG  = "search_hit"


def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    return {
        "api": {
            "host": "127.0.0.1",
            "port": 31270,
            "comm_key_path": "",
            "api_host_history": [],
            "request_timeout_s": 2.0,
        }
    }


def _save_config(cfg: dict) -> None:
    with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


# ---------------------------------------------------------------------------
# Minimal HTTP client (instantiated on Connect, used on worker threads)
# ---------------------------------------------------------------------------

class _ApiClient:
    def __init__(self, host: str, port: int, key: str, timeout: float) -> None:
        self._base    = f"http://{host}:{port}"
        self._headers = {"DTGCommKey": key}
        self._timeout = timeout
        self._session = requests.Session()

    def list_node(self, suffix: str) -> Any:
        path = suffix.lstrip("/")
        url  = f"{self._base}/list/{path}" if path else f"{self._base}/list"
        r    = self._session.get(url, headers=self._headers, timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def get(self, path: str) -> Any:
        url = f"{self._base}/get/{path.lstrip('/')}"
        r   = self._session.get(url, headers=self._headers, timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def set(self, path: str, value: float) -> Any:
        url = f"{self._base}/set/{path.lstrip('/')}"
        r   = self._session.patch(url, params={"Value": value},
                                  headers=self._headers, timeout=self._timeout)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"status": r.status_code, "text": r.text}

    def close(self) -> None:
        self._session.close()


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class InterrogatorApp:
    def __init__(self) -> None:
        self._cfg    = _load_config()
        self._client: _ApiClient | None = None

        self._root = tk.Tk()
        self._root.title("TSW API Interrogator")
        self._root.minsize(960, 640)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()

    def run(self) -> None:
        self._root.mainloop()

    # -----------------------------------------------------------------------
    # Top-level layout
    # -----------------------------------------------------------------------

    def _build(self) -> None:
        api_cfg = self._cfg.get("api", {})

        # Shared tk variables
        self._host_var     = tk.StringVar(value=api_cfg.get("host", "127.0.0.1"))
        self._port_var     = tk.IntVar(value=api_cfg.get("port", 31270))
        key_path           = api_cfg.get("comm_key_path", "")
        self._key_path_var = tk.StringVar(value=key_path)
        self._key_file_var = tk.StringVar(value=Path(key_path).name if key_path else "")
        self._status_var   = tk.StringVar(value="Disconnected")
        self._host_history: list[str] = list(api_cfg.get("api_host_history", []))

        # Operation variables
        self._list_path_var  = tk.StringVar()
        self._get_path_var   = tk.StringVar()
        self._set_path_var   = tk.StringVar()
        self._set_value_var  = tk.StringVar(value="0.0")
        self._set_step_var   = tk.StringVar(value="0.1")
        self._search_var     = tk.StringVar()

        # Outer paned window: narrow connection sidebar | wide content area
        pw = ttk.PanedWindow(self._root, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=4, pady=4)

        left  = ttk.Frame(pw, padding=6)
        right = ttk.Frame(pw, padding=6)
        pw.add(left,  weight=0)
        pw.add(right, weight=1)

        self._build_connection(left)
        self._build_right(right)

    # -----------------------------------------------------------------------
    # Connection sidebar
    # -----------------------------------------------------------------------

    def _build_connection(self, parent: ttk.Frame) -> None:
        r = 0

        ttk.Label(parent, text="Connection",
                  font=("TkDefaultFont", 9, "bold")).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(0, 6))
        r += 1

        def lbl(text: str) -> None:
            nonlocal r
            ttk.Label(parent, text=text).grid(row=r, column=0, sticky="w", pady=2)

        lbl("Host:")
        self._host_combo = ttk.Combobox(
            parent, textvariable=self._host_var,
            values=self._host_history, width=18)
        self._host_combo.grid(row=r, column=1, sticky="ew", pady=2)
        r += 1

        lbl("Port:")
        ttk.Spinbox(parent, from_=1, to=65535, textvariable=self._port_var,
                    width=8).grid(row=r, column=1, sticky="w", pady=2)
        r += 1

        lbl("Key path:")
        kf = ttk.Frame(parent)
        kf.grid(row=r, column=1, sticky="ew", pady=2)
        ttk.Entry(kf, textvariable=self._key_path_var, width=14).pack(
            side="left", expand=True, fill="x")
        ttk.Button(kf, text="…", width=2, command=self._browse_key).pack(side="left")
        r += 1

        lbl("Key file:")
        kff = ttk.Frame(parent)
        kff.grid(row=r, column=1, sticky="ew", pady=2)
        self._key_file_combo = ttk.Combobox(
            kff, textvariable=self._key_file_var, width=14)
        self._key_file_combo.pack(side="left", expand=True, fill="x")
        self._key_file_combo.bind("<<ComboboxSelected>>", self._on_key_file_selected)
        ttk.Button(kff, text="↺", width=2,
                   command=self._refresh_key_files).pack(side="left")
        r += 1

        bf = ttk.Frame(parent)
        bf.grid(row=r, column=0, columnspan=2, sticky="ew", pady=8)
        self._connect_btn    = ttk.Button(bf, text="Connect",    command=self._connect)
        self._disconnect_btn = ttk.Button(bf, text="Disconnect", command=self._disconnect,
                                          state="disabled")
        self._connect_btn.pack(side="left", expand=True, fill="x")
        self._disconnect_btn.pack(side="left", expand=True, fill="x")
        r += 1

        lbl("Status:")
        self._status_label = ttk.Label(parent, textvariable=self._status_var,
                                       foreground="red", wraplength=140)
        self._status_label.grid(row=r, column=1, sticky="w", pady=2)
        r += 1

        parent.columnconfigure(1, weight=1)
        self._refresh_key_files()

    # -----------------------------------------------------------------------
    # Right content: LIST / GET / SET sections
    # -----------------------------------------------------------------------

    def _build_right(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)   # LIST expands
        parent.rowconfigure(1, weight=0)
        parent.rowconfigure(2, weight=0)

        self._build_list_section(parent, row=0)
        self._build_get_section(parent,  row=1)
        self._build_set_section(parent,  row=2)

    # -- LIST ----------------------------------------------------------------

    def _build_list_section(self, parent: tk.Widget, row: int) -> None:
        lf = ttk.LabelFrame(parent, text="LIST  —  GET /list/<suffix>", padding=6)
        lf.grid(row=row, column=0, sticky="nsew", pady=(0, 4))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(1, weight=1)

        # Input row
        top = ttk.Frame(lf)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Path suffix:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        list_path_entry = ttk.Entry(top, textvariable=self._list_path_var)
        list_path_entry.grid(row=0, column=1, sticky="ew")
        list_path_entry.bind("<Return>", lambda _e: self._do_list())
        ttk.Button(top, text="LIST", width=8,
                   command=self._do_list).grid(row=0, column=2, padx=(6, 0))

        # Large response text widget
        txt_frame = ttk.Frame(lf)
        txt_frame.grid(row=1, column=0, sticky="nsew")
        txt_frame.columnconfigure(0, weight=1)
        txt_frame.rowconfigure(0, weight=1)

        self._list_text = tk.Text(
            txt_frame, wrap="none", font=("TkFixedFont", 10),
            undo=False, state="normal")
        vsb = ttk.Scrollbar(txt_frame, orient="vertical",   command=self._list_text.yview)
        hsb = ttk.Scrollbar(txt_frame, orient="horizontal", command=self._list_text.xview)
        self._list_text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._list_text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self._list_text.tag_configure(_SEARCH_TAG, background="#FFD700")

        # Search bar
        sb = ttk.Frame(lf)
        sb.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(sb, text="Search:").pack(side="left")
        self._search_entry = ttk.Entry(sb, textvariable=self._search_var, width=30)
        self._search_entry.pack(side="left", padx=4)
        self._search_entry.bind("<Return>", lambda _e: self._search_list())
        ttk.Button(sb, text="Find All",
                   command=self._search_list).pack(side="left")
        ttk.Button(sb, text="Clear",
                   command=self._clear_search).pack(side="left", padx=(2, 12))
        self._match_label = ttk.Label(sb, text="")
        self._match_label.pack(side="left")
        ttk.Button(sb, text="Copy Selection",
                   command=self._copy_list_selection).pack(side="right")

    # -- GET -----------------------------------------------------------------

    def _build_get_section(self, parent: tk.Widget, row: int) -> None:
        lf = ttk.LabelFrame(parent, text="GET  —  GET /get/<path>", padding=6)
        lf.grid(row=row, column=0, sticky="ew", pady=(0, 4))
        lf.columnconfigure(0, weight=1)

        top = ttk.Frame(lf)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Path:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        get_entry = ttk.Entry(top, textvariable=self._get_path_var)
        get_entry.grid(row=0, column=1, sticky="ew")
        get_entry.bind("<Return>", lambda _e: self._do_get())
        ttk.Button(top, text="GET", width=8,
                   command=self._do_get).grid(row=0, column=2, padx=(6, 0))

        self._get_text = self._make_response_box(lf, row=1)

    # -- SET -----------------------------------------------------------------

    def _build_set_section(self, parent: tk.Widget, row: int) -> None:
        lf = ttk.LabelFrame(
            parent, text="SET  —  PATCH /set/<path>?Value=<value>", padding=6)
        lf.grid(row=row, column=0, sticky="ew")
        lf.columnconfigure(0, weight=1)

        # Path row
        path_row = ttk.Frame(lf)
        path_row.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        path_row.columnconfigure(1, weight=1)
        ttk.Label(path_row, text="Path:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        set_path_entry = ttk.Entry(path_row, textvariable=self._set_path_var)
        set_path_entry.grid(row=0, column=1, sticky="ew")
        set_path_entry.bind("<Return>", lambda _e: self._do_set())

        # Value row: [−] [value entry] [+]  Step: [step entry]  [SET]
        val_row = ttk.Frame(lf)
        val_row.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(val_row, text="Value:").pack(side="left", padx=(0, 4))
        ttk.Button(val_row, text="−", width=2,
                   command=self._decrement_value).pack(side="left")
        self._set_value_entry = ttk.Entry(
            val_row, textvariable=self._set_value_var, width=12,
            justify="center")
        self._set_value_entry.pack(side="left", padx=2)
        ttk.Button(val_row, text="+", width=2,
                   command=self._increment_value).pack(side="left")
        ttk.Label(val_row, text="   Step:").pack(side="left")
        ttk.Entry(val_row, textvariable=self._set_step_var,
                  width=8, justify="center").pack(side="left", padx=4)
        ttk.Button(val_row, text="SET", width=8,
                   command=self._do_set).pack(side="right", padx=(6, 0))

        self._set_text = self._make_response_box(lf, row=2)

    # -----------------------------------------------------------------------
    # Shared widget factories
    # -----------------------------------------------------------------------

    def _make_response_box(self, parent: tk.Widget, row: int) -> tk.Text:
        """3-line read-only scrollable text widget."""
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)

        txt = tk.Text(frame, height=3, wrap="none",
                      font=("TkFixedFont", 10), state="disabled")
        vsb = ttk.Scrollbar(frame, orient="vertical",   command=txt.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        txt.grid(row=0, column=0, sticky="ew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        return txt

    # -----------------------------------------------------------------------
    # Connection callbacks
    # -----------------------------------------------------------------------

    def _connect(self) -> None:
        host     = self._host_var.get().strip()
        port     = int(self._port_var.get())
        key_path = self._key_path_var.get().strip()

        if not host:
            messagebox.showerror("Connect", "Host is required.")
            return
        if not key_path:
            messagebox.showerror("Connect", "CommAPIKey path is required.")
            return

        try:
            key = Path(key_path).read_text(encoding="utf-8").strip()
        except Exception as exc:
            messagebox.showerror("Connect", f"Cannot read CommAPIKey:\n{exc}")
            return

        if self._client is not None:
            self._client.close()

        timeout = self._cfg.get("api", {}).get("request_timeout_s", 2.0)
        self._client = _ApiClient(host, port, key, timeout)

        self._set_status("Connected", "green")
        self._connect_btn.configure(state="disabled")
        self._disconnect_btn.configure(state="normal")

        # Persist host to history
        self._update_host_history(host)
        self._save_current_settings()

    def _disconnect(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._set_status("Disconnected", "red")
        self._connect_btn.configure(state="normal")
        self._disconnect_btn.configure(state="disabled")

    def _on_close(self) -> None:
        self._disconnect()
        self._root.destroy()

    # -----------------------------------------------------------------------
    # API operation callbacks  (all fire a background thread)
    # -----------------------------------------------------------------------

    def _do_list(self) -> None:
        if not self._check_connected():
            return
        suffix = self._list_path_var.get().strip()
        self._run_in_thread(
            lambda: self._client.list_node(suffix),
            self._on_list_result,
        )

    def _do_get(self) -> None:
        if not self._check_connected():
            return
        path = self._get_path_var.get().strip()
        if not path:
            messagebox.showerror("GET", "Path is required.")
            return
        self._run_in_thread(
            lambda: self._client.get(path),
            lambda data: self._set_response_box(self._get_text, data),
        )

    def _do_set(self) -> None:
        if not self._check_connected():
            return
        path = self._set_path_var.get().strip()
        if not path:
            messagebox.showerror("SET", "Path is required.")
            return
        try:
            value = float(self._set_value_var.get())
        except ValueError:
            messagebox.showerror("SET", "Value must be a number.")
            return
        self._run_in_thread(
            lambda: self._client.set(path, value),
            lambda data: self._set_response_box(self._set_text, data),
        )

    # -----------------------------------------------------------------------
    # Result handlers (called on GUI thread via root.after)
    # -----------------------------------------------------------------------

    def _on_list_result(self, data: Any) -> None:
        pretty = json.dumps(data, indent=2)
        self._list_text.configure(state="normal")
        self._list_text.delete("1.0", "end")
        self._list_text.insert("1.0", pretty)
        # Leave state=normal so text is selectable / copyable
        self._match_label.configure(text="")
        self._search_var.set("")

    def _set_response_box(self, widget: tk.Text, data: Any) -> None:
        pretty = json.dumps(data, indent=2)
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", pretty)
        widget.configure(state="disabled")

    # -----------------------------------------------------------------------
    # Search / copy helpers for LIST box
    # -----------------------------------------------------------------------

    def _search_list(self) -> None:
        term = self._search_var.get()
        self._list_text.tag_remove(_SEARCH_TAG, "1.0", "end")
        if not term:
            self._match_label.configure(text="")
            return

        count  = 0
        start  = "1.0"
        length = tk.IntVar()
        while True:
            pos = self._list_text.search(
                term, start, stopindex="end",
                count=length, nocase=True)
            if not pos:
                break
            end = f"{pos}+{length.get()}c"
            self._list_text.tag_add(_SEARCH_TAG, pos, end)
            start = end
            count += 1
            if count == 1:
                self._list_text.see(pos)

        self._match_label.configure(
            text=f"{count} match{'es' if count != 1 else ''}")

    def _clear_search(self) -> None:
        self._list_text.tag_remove(_SEARCH_TAG, "1.0", "end")
        self._search_var.set("")
        self._match_label.configure(text="")

    def _copy_list_selection(self) -> None:
        try:
            selected = self._list_text.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            selected = self._list_text.get("1.0", "end")
        self._root.clipboard_clear()
        self._root.clipboard_append(selected)

    # -----------------------------------------------------------------------
    # SET value increment / decrement
    # -----------------------------------------------------------------------

    def _increment_value(self) -> None:
        self._adjust_value(+1)

    def _decrement_value(self) -> None:
        self._adjust_value(-1)

    def _adjust_value(self, sign: int) -> None:
        try:
            val  = float(self._set_value_var.get())
            step = float(self._set_step_var.get())
        except ValueError:
            return
        decimals = len(self._set_step_var.get().rstrip("0").split(".")[-1]) \
            if "." in self._set_step_var.get() else 0
        result = round(val + sign * step, decimals)
        self._set_value_var.set(str(result))

    # -----------------------------------------------------------------------
    # CommAPIKey file helpers
    # -----------------------------------------------------------------------

    def _browse_key(self) -> None:
        current = self._key_path_var.get()
        initial = str(Path(current).parent) if current else ""
        path = filedialog.askopenfilename(
            title="Select CommAPIKey file",
            initialdir=initial or None,
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self._key_path_var.set(path)
            self._key_file_var.set(Path(path).name)
            self._refresh_key_files()

    def _refresh_key_files(self) -> None:
        path = self._key_path_var.get()
        if not path:
            return
        d = Path(path).parent
        if not d.is_dir():
            return
        files = sorted(p.name for p in d.glob("*.txt"))
        self._key_file_combo["values"] = files

    def _on_key_file_selected(self, *_) -> None:
        filename = self._key_file_var.get()
        if not filename:
            return
        current = self._key_path_var.get()
        if not current:
            return
        new_path = str(Path(current).parent / filename)
        self._key_path_var.set(new_path)

    # -----------------------------------------------------------------------
    # Config persistence
    # -----------------------------------------------------------------------

    def _update_host_history(self, host: str) -> None:
        history = self._host_history[:]
        if host in history:
            history.remove(host)
        history.insert(0, host)
        self._host_history = history[:5]
        self._host_combo["values"] = self._host_history

    def _save_current_settings(self) -> None:
        api = self._cfg.setdefault("api", {})
        api["host"]             = self._host_var.get().strip()
        api["port"]             = int(self._port_var.get())
        api["comm_key_path"]    = self._key_path_var.get().strip()
        api["api_host_history"] = self._host_history
        _save_config(self._cfg)

    # -----------------------------------------------------------------------
    # Utilities
    # -----------------------------------------------------------------------

    def _set_status(self, text: str, colour: str) -> None:
        self._status_var.set(text)
        self._status_label.configure(foreground=colour)

    def _check_connected(self) -> bool:
        if self._client is None:
            messagebox.showwarning("Not connected",
                                   "Connect to the API first.")
            return False
        return True

    def _run_in_thread(self, fn, on_success) -> None:
        """Run *fn* on a worker thread; deliver result to GUI via root.after."""
        def worker():
            try:
                result = fn()
                self._root.after(0, lambda: on_success(result))
            except requests.HTTPError as exc:
                msg = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                self._root.after(0, lambda m=msg: messagebox.showerror("API Error", m))
            except requests.ConnectionError:
                self._root.after(0, lambda: messagebox.showerror(
                    "Connection Error",
                    "Could not reach the API.\nCheck host, port, and that TSW is running."))
            except Exception as exc:
                self._root.after(0, lambda m=str(exc): messagebox.showerror("Error", m))

        threading.Thread(target=worker, daemon=True).start()
