"""
event_bus.py — Thread-safe publish/subscribe event bus.

All inter-subsystem communication passes through here.  Each subsystem
creates one or more ``queue.Queue`` objects and registers them with
``EventBus.subscribe()``.  When any subsystem calls ``EventBus.put(event)``,
the event is fanned-out to every queue that subscribed to that event type.

No subsystem should hold a direct reference to another subsystem.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

class ConnectionSubsystem(Enum):
    API = "API"
    MIDI_IN = "MIDI_IN"
    MIDI_OUT = "MIDI_OUT"
    OSC_IN = "OSC_IN"
    OSC_OUT = "OSC_OUT"


class ConnectionState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    ERROR = auto()


class MonitorDirection(Enum):
    IN = "IN"
    OUT = "OUT"


# --- MIDI input events (midi_manager → mapping_engine + gui) ---------------

@dataclass
class MidiCCEvent:
    channel: int   # 1-16
    number: int    # 0-127
    value: int     # 0-127


@dataclass
class MidiNoteOnEvent:
    channel: int   # 1-16
    number: int    # 0-127
    velocity: int  # 0-127


@dataclass
class MidiNoteOffEvent:
    channel: int   # 1-16
    number: int    # 0-127


# --- MIDI input events — pitchbend (midi_manager → mapping_engine + gui) ---

@dataclass
class MidiPitchBendEvent:
    """Inbound Pitch Bend received from a MIDI controller.

    *value* is the unsigned 14-bit integer 0–16383 decoded from the two
    7-bit data bytes (LSB first, then MSB) per the MIDI spec.
    """
    channel: int  # 1-16
    value: int    # 0-16383


# --- MIDI output events (mapping_engine → midi_manager + gui) --------------

@dataclass
class MidiSendCCEvent:
    channel: int
    number: int
    value: int


@dataclass
class MidiSendNoteOnEvent:
    channel: int
    number: int
    velocity: int = 64


@dataclass
class MidiSendNoteOffEvent:
    channel: int
    number: int


@dataclass
class MidiSendPitchBendEvent:
    """Send a 14-bit Pitch Bend message (status 0xEn).

    *value* is the unsigned 14-bit integer 0–16383, where 0 = minimum and
    16383 = maximum.  For speed telemetry transmitted as tenths of kph the
    value is the integer speed * 10 (e.g. 1000 = 100.0 kph).
    """
    channel: int  # 1-16
    value: int    # 0-16383


# --- OSC events (osc_manager ↔ mapping_engine + gui) -----------------------

@dataclass
class OscMessageEvent:
    """Inbound OSC message received from any OSC client.

    *address* is the OSC address pattern (e.g. ``/train/throttle``).
    *args* is the list of typed arguments decoded by python-osc (floats,
    ints, strings, booleans, etc.).
    """
    address: str
    args: list[Any]


@dataclass
class OscSendEvent:
    """Instructs the OSC manager to send a message to the configured target.

    *address* is the OSC address pattern.
    *args* is the list of values to pack as OSC arguments.  python-osc
    infers the type tag from the Python type of each element.
    """
    address: str
    args: list[Any]


# --- API events (api_client ↔ mapping_engine) ------------------------------

@dataclass
class ApiSetEvent:
    """Instructs the API client to PATCH a value."""
    path: str    # e.g. "CurrentDrivableActor/Throttle(Lever).InputValue"
    value: Any   # float or bool


@dataclass
class SubscriptionResultEvent:
    """Bulk poll result returned by GET /subscription."""
    data: dict[str, Any]  # endpoint path → value


# --- Status / log events (any → gui) ---------------------------------------

@dataclass
class ConnectionStateEvent:
    subsystem: ConnectionSubsystem
    state: ConnectionState
    message: str = ""


@dataclass
class MonitorEvent:
    """A single line to display in the MIDI monitor pane."""
    direction: MonitorDirection
    midi_type: str   # "CC", "NoteOn", "NoteOff"
    channel: int
    number: int
    value: int | None = None


@dataclass
class ErrorEvent:
    """Non-fatal error to display in the status bar."""
    source: str
    message: str


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class EventBus:
    """
    Fan-out publish/subscribe bus backed by ``queue.Queue``.

    Usage::

        bus = EventBus()

        # Subscriber creates its own queue and registers interest
        my_q: queue.Queue = queue.Queue()
        bus.subscribe([MidiCCEvent, MidiNoteOnEvent], my_q)

        # Publisher emits an event
        bus.put(MidiCCEvent(channel=1, number=7, value=64))

        # Subscriber drains its queue (e.g. from root.after() or a worker thread)
        while True:
            try:
                event = my_q.get_nowait()
                handle(event)
            except queue.Empty:
                break
    """

    def __init__(self) -> None:
        self._subscriptions: dict[type, list[queue.Queue]] = {}
        self._lock = threading.Lock()

    def subscribe(self, event_types: list[type], q: queue.Queue) -> None:
        """Register *q* to receive all events whose type is in *event_types*."""
        with self._lock:
            for t in event_types:
                self._subscriptions.setdefault(t, []).append(q)

    def unsubscribe(self, event_types: list[type], q: queue.Queue) -> None:
        """Remove *q* from receiving the given event types."""
        with self._lock:
            for t in event_types:
                try:
                    self._subscriptions[t].remove(q)
                except (KeyError, ValueError):
                    pass

    def put(self, event: Any) -> None:
        """Publish *event* to all queues subscribed to its type."""
        event_type = type(event)
        with self._lock:
            targets = list(self._subscriptions.get(event_type, []))
        for q in targets:
            q.put_nowait(event)

    def drain(self, q: queue.Queue) -> list[Any]:
        """Return all currently queued items from *q* without blocking."""
        items: list[Any] = []
        while True:
            try:
                items.append(q.get_nowait())
            except queue.Empty:
                break
        return items
