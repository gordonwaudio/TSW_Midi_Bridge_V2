"""
mapping_engine.py — Bidirectional MIDI ↔ TSW API translation.

Translates between the three message flows defined in the train config JSON:

  api_to_midi   — SubscriptionResultEvent → MidiSend*Event
  midi_to_api   — MidiCC/NoteOn/NoteOff → ApiSetEvent
  bidirectional — both directions simultaneously

Scaling modes supported:
  linear              — proportional map between api and MIDI ranges
  notch_lookup        — CC value selects a notch index; notch_values[index]
                        is sent to the API. Reverse: notch index → CC.
  m_s_to_tenths_kph   — converts m/s to tenths-of-kph integer (×36),
                        used with midi_type=pitchbend for speed telemetry.

CC zone thresholds (used for cc_low / cc_mid / cc_high and cc_lo_value /
cc_hi_value mappings):
  0–31   → low
  32–95  → mid
  96–127 → high
"""

from __future__ import annotations

import logging
import queue
import threading

from app.event_bus import (
    ApiSetEvent,
    EventBus,
    MidiCCEvent,
    MidiNoteOffEvent,
    MidiNoteOnEvent,
    MidiPitchBendEvent,
    MidiSendCCEvent,
    MidiSendPitchBendEvent,
    OscMessageEvent,
    SubscriptionResultEvent,
)

log = logging.getLogger(__name__)

_CC_LOW_MAX  = 31
_CC_HIGH_MIN = 96


class MappingEngine:
    """Receives MIDI/OSC/subscription events and translates them to API calls.

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

        # Lookup tables rebuilt on every load_config call
        # api_read_path → mapping entry  (direction: api_to_midi | bidirectional)
        self._api_to_output: dict[str, dict] = {}
        # (midi_type, channel, number) → mapping entry  (direction: midi_to_api | bidirectional)
        # pitchbend entries use number=None
        self._midi_to_api: dict[tuple, dict] = {}

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
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="MappingEngine", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    # -----------------------------------------------------------------------
    # Config management
    # -----------------------------------------------------------------------

    def load_config(self, config: dict) -> None:
        """Store the active train config and rebuild lookup tables."""
        self._config = config
        self._build_lookup_tables(config)
        log.info(
            "MappingEngine: loaded layout=%s, %d active mappings, %d poll endpoints",
            config.get("layout", "?"),
            sum(1 for m in config.get("mappings", []) if m.get("active", True)),
            len(self.get_poll_endpoints()),
        )

    def get_poll_endpoints(self) -> list[str]:
        """Return API read paths that should be registered in the subscription."""
        return [
            m["api_read_path"]
            for m in self._config.get("mappings", [])
            if m.get("poll", False)
            and m.get("active", True)
            and "api_read_path" in m
        ]

    def _build_lookup_tables(self, config: dict) -> None:
        self._api_to_output = {}
        self._midi_to_api   = {}

        for mapping in config.get("mappings", []):
            if not mapping.get("active", True):
                continue

            direction = mapping.get("direction", "")
            midi_type = mapping.get("midi_type", "cc")
            channel   = mapping.get("midi_channel", 1)
            number    = mapping.get("midi_number")

            if direction in ("api_to_midi", "bidirectional"):
                path = mapping.get("api_read_path")
                if path:
                    self._api_to_output[path] = mapping

            if direction in ("midi_to_api", "bidirectional"):
                key = ("pitchbend", channel, None) if midi_type == "pitchbend" \
                      else (midi_type, channel, number)
                self._midi_to_api[key] = mapping

    # -----------------------------------------------------------------------
    # Event handlers
    # -----------------------------------------------------------------------

    def _handle_subscription_result(self, event: SubscriptionResultEvent) -> None:
        for path, raw_value in event.data.items():
            mapping = self._api_to_output.get(path)
            if mapping is None:
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                log.debug("Non-numeric subscription value for %s: %r", path, raw_value)
                continue

            midi_type = mapping.get("midi_type", "cc")
            channel   = mapping.get("midi_channel", 1)
            scaling   = mapping.get("scaling", "linear")

            if midi_type == "pitchbend":
                if scaling == "m_s_to_tenths_kph":
                    tenths = round(value * 36)
                    tenths = max(0, min(16383, tenths))
                    self._bus.put(MidiSendPitchBendEvent(channel=channel, value=tenths))
                else:
                    log.warning("Unsupported pitchbend scaling '%s' on %s", scaling, path)

            elif midi_type == "cc":
                cc_val = self._api_to_cc(value, mapping)
                number = mapping.get("midi_number", 0)
                self._bus.put(MidiSendCCEvent(channel=channel, number=number, value=cc_val))

    def _handle_midi_cc(self, event: MidiCCEvent) -> None:
        mapping = self._midi_to_api.get(("cc", event.channel, event.number))
        if mapping is None:
            return

        scaling = mapping.get("scaling", "linear")
        cc_val  = event.value

        # Multi-path zone actions (cc_low / cc_mid / cc_high lists)
        if "cc_low" in mapping or "cc_mid" in mapping or "cc_high" in mapping:
            for action in self._cc_zone_actions(cc_val, mapping):
                self._bus.put(ApiSetEvent(path=action["path"], value=action["value"]))
            return

        # Single-path zone values (cc_lo_value / cc_hi_value)
        if "cc_hi_value" in mapping or "cc_lo_value" in mapping:
            api_path = mapping.get("api_set_path", "")
            if not api_path:
                return
            if cc_val <= _CC_LOW_MAX:
                self._bus.put(ApiSetEvent(path=api_path, value=mapping.get("cc_lo_value", 0.0)))
            elif cc_val >= _CC_HIGH_MIN:
                self._bus.put(ApiSetEvent(path=api_path, value=mapping.get("cc_hi_value", 1.0)))
            # mid zone: no action
            return

        # notch_lookup: CC → index → notch_values[index]
        if scaling == "notch_lookup":
            notch_values = mapping.get("notch_values", [])
            n = len(notch_values)
            if n == 0:
                return
            index = round(cc_val / 127 * (n - 1)) if n > 1 else 0
            index = max(0, min(n - 1, index))
            api_value = notch_values[index]

        else:  # linear
            api_set_min = float(mapping.get("api_set_min", 0.0))
            api_set_max = float(mapping.get("api_set_max", 1.0))
            t = cc_val / 127
            api_value = api_set_min + t * (api_set_max - api_set_min)

        api_path = mapping.get("api_set_path", "")
        if api_path:
            self._bus.put(ApiSetEvent(path=api_path, value=api_value))

    def _handle_midi_note_on(self, event: MidiNoteOnEvent) -> None:
        mapping = self._midi_to_api.get(("note", event.channel, event.number))
        if mapping is None:
            return
        if "note_on_actions" in mapping:
            for action in mapping["note_on_actions"]:
                self._bus.put(ApiSetEvent(path=action["path"], value=action["value"]))
        elif "note_on_value" in mapping:
            path = mapping.get("api_set_path", "")
            if path:
                self._bus.put(ApiSetEvent(path=path, value=mapping["note_on_value"]))

    def _handle_midi_note_off(self, event: MidiNoteOffEvent) -> None:
        mapping = self._midi_to_api.get(("note", event.channel, event.number))
        if mapping is None:
            return
        if "note_off_actions" in mapping:
            for action in mapping["note_off_actions"]:
                self._bus.put(ApiSetEvent(path=action["path"], value=action["value"]))
        elif "note_off_value" in mapping:
            path = mapping.get("api_set_path", "")
            if path:
                self._bus.put(ApiSetEvent(path=path, value=mapping["note_off_value"]))

    def _handle_midi_pitch_bend(self, event: MidiPitchBendEvent) -> None:
        pass  # inbound pitch bend → API not currently used in train configs

    def _handle_osc_message(self, event: OscMessageEvent) -> None:
        pass  # OSC support reserved for future use

    # -----------------------------------------------------------------------
    # Scaling helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _api_to_cc(value: float, mapping: dict) -> int:
        """Scale an API value to a 0–127 CC integer."""
        scaling = mapping.get("scaling", "linear")
        api_min = float(mapping.get("api_min", 0))
        api_max = float(mapping.get("api_max", 1))

        if scaling == "notch_lookup":
            # API value is a notch index integer; map index → 0–127
            n_notches = int(round(api_max - api_min)) + 1
            index     = int(round(value)) - int(round(api_min))
            index     = max(0, min(n_notches - 1, index))
            if n_notches <= 1:
                return 0
            return max(0, min(127, round(index / (n_notches - 1) * 127)))

        # linear
        if api_max == api_min:
            return 0
        t = (value - api_min) / (api_max - api_min)
        return max(0, min(127, round(t * 127)))

    @staticmethod
    def _cc_zone_actions(cc_val: int, mapping: dict) -> list[dict]:
        """Return the action list for the CC zone the value falls in."""
        if cc_val <= _CC_LOW_MAX:
            return mapping.get("cc_low", [])
        if cc_val >= _CC_HIGH_MIN:
            return mapping.get("cc_high", [])
        return mapping.get("cc_mid", [])

    # -----------------------------------------------------------------------
    # Processing loop
    # -----------------------------------------------------------------------

    def _run(self) -> None:
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
