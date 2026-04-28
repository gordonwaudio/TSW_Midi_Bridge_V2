"""
midi_manager.py — MIDI device I/O via python-rtmidi.

Supports multiple simultaneous MIDI input and output devices.

Inbound MIDI (CC, Note On, Note Off, Pitch Bend) is received on per-device
rtmidi callback threads and posted onto the event bus immediately.

Outbound MIDI is consumed from the bus on a dedicated output thread so that
``send_*`` calls never block the mapping engine or GUI.  All open output
devices receive every outbound message.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

import rtmidi

from app.event_bus import (
    ConnectionState,
    ConnectionStateEvent,
    ConnectionSubsystem,
    EventBus,
    MidiCCEvent,
    MidiNoteOffEvent,
    MidiNoteOnEvent,
    MidiPitchBendEvent,
    MidiSendCCEvent,
    MidiSendNoteOffEvent,
    MidiSendNoteOnEvent,
    MidiSendPitchBendEvent,
)

# MIDI status byte masks
_STATUS_NOTE_OFF   = 0x80
_STATUS_NOTE_ON    = 0x90
_STATUS_CC         = 0xB0
_STATUS_PITCH_BEND = 0xE0
_CHANNEL_MASK      = 0x0F
_STATUS_MASK       = 0xF0

log = logging.getLogger(__name__)


class MidiManager:
    """Wraps python-rtmidi for device enumeration, input callbacks, and output.

    Multiple input and output devices can be open simultaneously.
    All open inputs post to the same event bus queue.
    All open outputs receive every outbound message.

    Lifecycle::

        mgr = MidiManager(bus)
        mgr.open_inputs(["Minilab3 MIDI", "loopMIDI Port 1"])
        mgr.open_outputs(["Minilab3 MIDI"])
        ...
        mgr.close_all_inputs()
        mgr.close_all_outputs()
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        # device_name → open rtmidi object
        self._midi_ins:  dict[str, rtmidi.MidiIn]  = {}
        self._midi_outs: dict[str, rtmidi.MidiOut] = {}

        # Single output thread drains the queue and sends to all open outputs
        self._out_queue: queue.Queue = queue.Queue()
        self._out_thread: threading.Thread | None = None
        self._out_stop = threading.Event()

        bus.subscribe(
            [MidiSendCCEvent, MidiSendNoteOnEvent, MidiSendNoteOffEvent,
             MidiSendPitchBendEvent],
            self._out_queue,
        )

    # -----------------------------------------------------------------------
    # Device enumeration
    # -----------------------------------------------------------------------

    def list_input_devices(self) -> list[tuple[int, str]]:
        """Return ``[(index, name), ...]`` for all available MIDI input ports."""
        midi_in = rtmidi.MidiIn()
        try:
            return list(enumerate(midi_in.get_ports()))
        finally:
            del midi_in

    def list_output_devices(self) -> list[tuple[int, str]]:
        """Return ``[(index, name), ...]`` for all available MIDI output ports."""
        midi_out = rtmidi.MidiOut()
        try:
            return list(enumerate(midi_out.get_ports()))
        finally:
            del midi_out

    # -----------------------------------------------------------------------
    # Multi-device lifecycle
    # -----------------------------------------------------------------------

    def open_inputs(self, device_names: list[str]) -> None:
        """Close all existing inputs, then open each device in *device_names*.

        Emits a ``ConnectionStateEvent`` for MIDI_IN reflecting the aggregate
        result (CONNECTED if at least one opened, DISCONNECTED if none).
        """
        self.close_all_inputs()
        for name in device_names:
            self._open_single_input(name)
        self._emit_aggregate_state(ConnectionSubsystem.MIDI_IN, self._midi_ins)

    def open_outputs(self, device_names: list[str]) -> None:
        """Close all existing outputs, then open each device in *device_names*,
        and (re)start the output-drain thread if any opened successfully.

        Emits a ``ConnectionStateEvent`` for MIDI_OUT reflecting the aggregate
        result.
        """
        self.close_all_outputs()
        for name in device_names:
            self._open_single_output(name)
        if self._midi_outs:
            self._out_stop.clear()
            self._out_thread = threading.Thread(
                target=self._output_loop, name="MidiOutput", daemon=True,
            )
            self._out_thread.start()
        self._emit_aggregate_state(ConnectionSubsystem.MIDI_OUT, self._midi_outs)

    def close_all_inputs(self) -> None:
        """Close every open MIDI input port."""
        for name in list(self._midi_ins):
            midi_in = self._midi_ins.pop(name)
            try:
                midi_in.close_port()
            except Exception:
                pass
            del midi_in
        self._emit_state(ConnectionSubsystem.MIDI_IN, ConnectionState.DISCONNECTED)

    def close_all_outputs(self) -> None:
        """Stop the output thread and close every open MIDI output port."""
        self._out_stop.set()
        if self._out_thread:
            self._out_thread.join(timeout=2.0)
            self._out_thread = None
        for name in list(self._midi_outs):
            midi_out = self._midi_outs.pop(name)
            try:
                midi_out.close_port()
            except Exception:
                pass
            del midi_out
        self._emit_state(ConnectionSubsystem.MIDI_OUT, ConnectionState.DISCONNECTED)

    # Convenience aliases used by main.py shutdown and legacy callers
    def close_input(self) -> None:
        self.close_all_inputs()

    def close_output(self) -> None:
        self.close_all_outputs()

    # -----------------------------------------------------------------------
    # Outbound helpers (called by external code or tests)
    # -----------------------------------------------------------------------

    def send_cc(self, channel: int, number: int, value: int) -> None:
        """Send a Control Change message to all open output devices."""
        status = _STATUS_CC | ((channel - 1) & _CHANNEL_MASK)
        msg = [status, number & 0x7F, value & 0x7F]
        for midi_out in list(self._midi_outs.values()):
            midi_out.send_message(msg)

    def send_note_on(self, channel: int, number: int, velocity: int = 64) -> None:
        """Send a Note On message to all open output devices."""
        status = _STATUS_NOTE_ON | ((channel - 1) & _CHANNEL_MASK)
        msg = [status, number & 0x7F, velocity & 0x7F]
        for midi_out in list(self._midi_outs.values()):
            midi_out.send_message(msg)

    def send_note_off(self, channel: int, number: int) -> None:
        """Send a Note Off message to all open output devices."""
        status = _STATUS_NOTE_OFF | ((channel - 1) & _CHANNEL_MASK)
        msg = [status, number & 0x7F, 0]
        for midi_out in list(self._midi_outs.values()):
            midi_out.send_message(msg)

    def send_pitch_bend(self, channel: int, value: int) -> None:
        """Send a Pitch Bend message to all open output devices.

        *value* is the unsigned 14-bit integer 0–16383 transmitted as two
        7-bit data bytes (LSB first, then MSB) per the MIDI spec::

            status = 0xE0 | (channel - 1)
            data[0] = value & 0x7F          # LSB
            data[1] = (value >> 7) & 0x7F  # MSB
        """
        status = _STATUS_PITCH_BEND | ((channel - 1) & _CHANNEL_MASK)
        lsb = value & 0x7F
        msb = (value >> 7) & 0x7F
        msg = [status, lsb, msb]
        for midi_out in list(self._midi_outs.values()):
            midi_out.send_message(msg)

    # -----------------------------------------------------------------------
    # Internal — single-device open helpers
    # -----------------------------------------------------------------------

    def _open_single_input(self, device_name: str) -> None:
        """Open one MIDI input port and register its callback."""
        if device_name in self._midi_ins:
            return  # already open
        midi_in = rtmidi.MidiIn()
        ports   = midi_in.get_ports()
        idx     = self._find_port_index(ports, device_name)
        if idx is None:
            del midi_in
            log.warning("MIDI input '%s' not found. Available: %s", device_name, ports)
            return
        try:
            midi_in.open_port(idx)
            midi_in.set_callback(self._midi_callback)
            midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
            self._midi_ins[device_name] = midi_in
            log.info("Opened MIDI input: %s", ports[idx])
        except Exception as exc:
            del midi_in
            log.error("Failed to open MIDI input '%s': %s", device_name, exc)

    def _open_single_output(self, device_name: str) -> None:
        """Open one MIDI output port."""
        if device_name in self._midi_outs:
            return  # already open
        midi_out = rtmidi.MidiOut()
        ports    = midi_out.get_ports()
        idx      = self._find_port_index(ports, device_name)
        if idx is None:
            del midi_out
            log.warning("MIDI output '%s' not found. Available: %s", device_name, ports)
            return
        try:
            midi_out.open_port(idx)
            self._midi_outs[device_name] = midi_out
            log.info("Opened MIDI output: %s", ports[idx])
        except Exception as exc:
            del midi_out
            log.error("Failed to open MIDI output '%s': %s", device_name, exc)

    # -----------------------------------------------------------------------
    # Internal — MIDI callback and output loop
    # -----------------------------------------------------------------------

    def _midi_callback(self, message_data: tuple, _timestamp: float) -> None:
        """rtmidi callback — runs on a per-device rtmidi callback thread.

        Parses raw MIDI bytes and posts typed events onto the bus.
        Note On with velocity 0 is treated as Note Off.
        """
        message, _ = message_data
        if len(message) < 2:
            return

        status_byte = message[0]
        status      = status_byte & _STATUS_MASK
        channel     = (status_byte & _CHANNEL_MASK) + 1  # 1-based

        if status == _STATUS_CC and len(message) >= 3:
            number = message[1]
            value  = message[2]
            self._bus.put(MidiCCEvent(channel=channel, number=number, value=value))

        elif status == _STATUS_NOTE_ON and len(message) >= 3:
            number   = message[1]
            velocity = message[2]
            if velocity == 0:
                self._bus.put(MidiNoteOffEvent(channel=channel, number=number))
            else:
                self._bus.put(MidiNoteOnEvent(channel=channel, number=number,
                                              velocity=velocity))

        elif status == _STATUS_NOTE_OFF and len(message) >= 2:
            number = message[1]
            self._bus.put(MidiNoteOffEvent(channel=channel, number=number))

        elif status == _STATUS_PITCH_BEND and len(message) >= 3:
            lsb   = message[1]
            msb   = message[2]
            value = (msb << 7) | lsb
            self._bus.put(MidiPitchBendEvent(channel=channel, value=value))

    def _output_loop(self) -> None:
        """Thread target — drains ``_out_queue`` and sends to all open outputs."""
        while not self._out_stop.is_set():
            try:
                event = self._out_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                if isinstance(event, MidiSendCCEvent):
                    self.send_cc(event.channel, event.number, event.value)
                elif isinstance(event, MidiSendNoteOnEvent):
                    self.send_note_on(event.channel, event.number, event.velocity)
                elif isinstance(event, MidiSendNoteOffEvent):
                    self.send_note_off(event.channel, event.number)
                elif isinstance(event, MidiSendPitchBendEvent):
                    self.send_pitch_bend(event.channel, event.value)
            except Exception:
                log.exception("MIDI output error for %r", event)

    # -----------------------------------------------------------------------
    # Internal — utilities
    # -----------------------------------------------------------------------

    def _find_port_index(self, port_names: list[str],
                         device_name: str) -> int | None:
        """Return the index of *device_name* in *port_names*, or ``None``."""
        for i, name in enumerate(port_names):
            if device_name in name or name in device_name:
                return i
        return None

    def _emit_state(self, subsystem: ConnectionSubsystem,
                    state: ConnectionState, message: str = "") -> None:
        self._bus.put(ConnectionStateEvent(
            subsystem=subsystem, state=state, message=message
        ))

    def _emit_aggregate_state(
        self,
        subsystem: ConnectionSubsystem,
        devices: dict,
    ) -> None:
        """Emit CONNECTED with device names if any open, else DISCONNECTED."""
        if devices:
            names = ", ".join(devices.keys())
            self._emit_state(subsystem, ConnectionState.CONNECTED, names)
        else:
            self._emit_state(subsystem, ConnectionState.DISCONNECTED)
