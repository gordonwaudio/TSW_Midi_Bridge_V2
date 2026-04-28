"""
mapping_engine.py — Bidirectional MIDI / OSC ↔ TSW API translation.

V2 STUB: The mapping logic in this module is intentionally unimplemented.
A new train config format and translation strategy will be defined in a
future iteration.  The public interface (start / stop / load_config /
get_poll_endpoints) is preserved so the rest of the application wires up
correctly and all subsystems can be exercised independently.

When the V2 config format is finalised:
  1. Update ``config_manager.TRAIN_CONFIG_SCHEMA`` with the new schema.
  2. Implement ``_handle_*`` methods below with the new translation logic.
  3. Rebuild the lookup tables in ``load_config``.
"""

from __future__ import annotations

import logging
import queue
import threading

from app.event_bus import (
    EventBus,
    MidiCCEvent,
    MidiNoteOffEvent,
    MidiNoteOnEvent,
    MidiPitchBendEvent,
    OscMessageEvent,
    SubscriptionResultEvent,
)

log = logging.getLogger(__name__)


class MappingEngine:
    """Receives MIDI/OSC/subscription events and translates them to API calls.

    V2 stub — translation is not yet implemented.  Events are consumed from
    the bus so queues do not grow unboundedly, but no output events are emitted.

    Lifecycle::

        engine = MappingEngine(bus)
        engine.load_config(config_dict)
        engine.start()
        ...
        engine.stop()
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._config: dict = {}

        self._in_queue: queue.Queue = queue.Queue()
        bus.subscribe(
            [MidiCCEvent, MidiNoteOnEvent, MidiNoteOffEvent, MidiPitchBendEvent,
             OscMessageEvent, SubscriptionResultEvent],
            self._in_queue,
        )

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def start(self) -> None:
        """Start the processing thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="MappingEngine", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the processing thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    # -----------------------------------------------------------------------
    # Config management
    # -----------------------------------------------------------------------

    def load_config(self, config: dict) -> None:
        """Store the active train config.

        Full lookup-table construction will be added once the V2 mapping
        format is defined.  Safe to call at runtime.
        """
        self._config = config
        log.warning("MappingEngine.load_config: V2 mapping logic not yet implemented.")

    def get_poll_endpoints(self) -> list[str]:
        """Return API paths that should be registered as subscriptions.

        Returns an empty list until the V2 config format is implemented.
        """
        # TODO: derive poll endpoints from the V2 config structure
        return []

    # -----------------------------------------------------------------------
    # Event handlers (stubs — called from the processing thread)
    # -----------------------------------------------------------------------

    def _handle_midi_cc(self, event: MidiCCEvent) -> None:
        # TODO: implement MIDI CC → API translation using V2 config
        pass

    def _handle_midi_note_on(self, event: MidiNoteOnEvent) -> None:
        # TODO: implement Note On → API translation using V2 config
        pass

    def _handle_midi_note_off(self, event: MidiNoteOffEvent) -> None:
        # TODO: implement Note Off → API translation using V2 config
        pass

    def _handle_midi_pitch_bend(self, event: MidiPitchBendEvent) -> None:
        # TODO: implement Pitch Bend → API translation using V2 config
        pass

    def _handle_osc_message(self, event: OscMessageEvent) -> None:
        # TODO: implement OSC → API translation using V2 config
        pass

    def _handle_subscription_result(self, event: SubscriptionResultEvent) -> None:
        # TODO: implement API poll → MIDI/OSC output using V2 config
        pass

    # -----------------------------------------------------------------------
    # Processing loop
    # -----------------------------------------------------------------------

    def _run(self) -> None:
        """Thread target — dispatches incoming events to the correct handler."""
        dispatch = {
            MidiCCEvent:             self._handle_midi_cc,
            MidiNoteOnEvent:         self._handle_midi_note_on,
            MidiNoteOffEvent:        self._handle_midi_note_off,
            MidiPitchBendEvent:      self._handle_midi_pitch_bend,
            OscMessageEvent:         self._handle_osc_message,
            SubscriptionResultEvent: self._handle_subscription_result,
        }
        while not self._stop_event.is_set():
            try:
                event = self._in_queue.get(timeout=0.1)
                handler = dispatch.get(type(event))
                if handler:
                    handler(event)
            except queue.Empty:
                continue
            except Exception:
                log.exception("MappingEngine error handling %r", event)
