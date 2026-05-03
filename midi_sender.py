"""Standalone MIDI output utility — sends CC, Note On, and Note Off messages."""

import tkinter as tk
from tkinter import ttk, messagebox
import rtmidi


class MidiSenderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("MIDI Sender")
        self.root.resizable(False, False)

        self.midi_out = rtmidi.MidiOut()
        self.port_index: int | None = None
        self.status_var = tk.StringVar(value="Ready")

        self._build_device_row()
        self._build_message_rows()
        self._build_send_button()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_device_row(self) -> None:
        frame = ttk.LabelFrame(self.root, text="MIDI Output Device", padding=8)
        frame.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")

        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(
            frame, textvariable=self.device_var, state="readonly", width=40
        )
        self.device_combo.grid(row=0, column=0, padx=(0, 6))
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        ttk.Button(frame, text="Refresh", command=self._refresh_devices).grid(
            row=0, column=1
        )

        self._refresh_devices()

    def _build_message_rows(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Message", padding=8)
        frame.grid(row=1, column=0, padx=12, pady=6, sticky="ew")

        # Message type
        ttk.Label(frame, text="Type:").grid(row=0, column=0, sticky="w")
        self.msg_type = tk.StringVar(value="CC")
        type_frame = ttk.Frame(frame)
        type_frame.grid(row=0, column=1, columnspan=3, sticky="w", pady=(0, 6))
        for label in ("CC", "Note On", "Note Off"):
            ttk.Radiobutton(
                type_frame,
                text=label,
                variable=self.msg_type,
                value=label,
                command=self._on_type_changed,
            ).pack(side="left", padx=(0, 10))

        # Channel
        ttk.Label(frame, text="Channel (1-16):").grid(row=1, column=0, sticky="w", pady=2)
        self.channel_var = tk.IntVar(value=1)
        ttk.Spinbox(frame, from_=1, to=16, textvariable=self.channel_var, width=6).grid(
            row=1, column=1, sticky="w", pady=2
        )

        # Number label (CC# or Note#)
        self.num_label_var = tk.StringVar(value="CC Number (0-127):")
        ttk.Label(frame, textvariable=self.num_label_var).grid(
            row=2, column=0, sticky="w", pady=2
        )
        self.number_var = tk.IntVar(value=0)
        ttk.Spinbox(frame, from_=0, to=127, textvariable=self.number_var, width=6).grid(
            row=2, column=1, sticky="w", pady=2
        )

        # Value label (CC value or velocity)
        self.val_label_var = tk.StringVar(value="Value (0-127):")
        ttk.Label(frame, textvariable=self.val_label_var).grid(
            row=3, column=0, sticky="w", pady=2
        )
        self.value_var = tk.IntVar(value=0)
        self.value_spin = ttk.Spinbox(
            frame, from_=0, to=127, textvariable=self.value_var, width=6
        )
        self.value_spin.grid(row=3, column=1, sticky="w", pady=2)

    def _build_send_button(self) -> None:
        ttk.Button(
            self.root, text="Send", command=self._send, width=20
        ).grid(row=2, column=0, pady=(6, 12))

        ttk.Label(self.root, textvariable=self.status_var, foreground="gray").grid(
            row=3, column=0, pady=(0, 8)
        )

    # ── Event handlers ────────────────────────────────────────────────────────

    def _refresh_devices(self) -> None:
        ports = self.midi_out.get_ports()
        self.device_combo["values"] = ports
        if ports:
            self.device_combo.current(0)
            self._open_port(0)
        else:
            self.device_var.set("")
            self.port_index = None
            self.status_var.set("No MIDI output devices found")

    def _on_device_selected(self, _event=None) -> None:
        idx = self.device_combo.current()
        if idx >= 0:
            self._open_port(idx)

    def _open_port(self, idx: int) -> None:
        if self.midi_out.is_port_open():
            self.midi_out.close_port()
        self.midi_out.open_port(idx)
        self.port_index = idx
        self.status_var.set(f"Opened: {self.midi_out.get_ports()[idx]}")

    def _on_type_changed(self) -> None:
        msg = self.msg_type.get()
        if msg == "CC":
            self.num_label_var.set("CC Number (0-127):")
            self.val_label_var.set("Value (0-127):")
            self.value_spin.configure(state="normal")
        elif msg == "Note On":
            self.num_label_var.set("Note (0-127):")
            self.val_label_var.set("Velocity (0-127):")
            self.value_spin.configure(state="normal")
        else:  # Note Off
            self.num_label_var.set("Note (0-127):")
            self.val_label_var.set("Velocity (0-127):")
            self.value_spin.configure(state="normal")

    def _send(self) -> None:
        if not self.midi_out.is_port_open():
            messagebox.showwarning("No Device", "No MIDI output port is open.")
            return

        ch = max(1, min(16, self.channel_var.get())) - 1  # 0-indexed
        num = max(0, min(127, self.number_var.get()))
        val = max(0, min(127, self.value_var.get()))
        msg_type = self.msg_type.get()

        if msg_type == "CC":
            msg = [0xB0 | ch, num, val]
            label = f"CC {num} = {val}  ch{ch+1}"
        elif msg_type == "Note On":
            msg = [0x90 | ch, num, val]
            label = f"Note On {num} vel {val}  ch{ch+1}"
        else:
            msg = [0x80 | ch, num, val]
            label = f"Note Off {num} vel {val}  ch{ch+1}"

        self.midi_out.send_message(msg)
        self.status_var.set(f"Sent: {label}")

    def _on_close(self) -> None:
        if self.midi_out.is_port_open():
            self.midi_out.close_port()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MidiSenderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
