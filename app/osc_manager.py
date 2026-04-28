"""
osc_manager.py — OSC (Open Sound Control) I/O via python-osc.

Inbound OSC messages are received on a dedicated UDP server thread and posted
onto the event bus as ``OscMessageEvent``.

Outbound OSC is consumed from the bus on a separate thread and dispatched to
the configured target host/port via a UDP client, so ``send_message`` calls
never block the mapping engine or GUI.

OSC runs alongside MIDI — both can be active simultaneously.  A single
mapping entry may be bound to either protocol; see ``config_manager.py`` for
the train config schema.

Typical configuration in ``app_config.json``::

    "osc": {
        "enabled": true,
        "listen_host": "0.0.0.0",
        "listen_port": 9000,
        "send_host": "127.0.0.1",
        "send_port": 9001
    }

``listen_host`` / ``listen_port`` — where the server listens for incoming OSC
from any external source (DAW, TouchOSC, etc.).

``send_host`` / ``send_port`` — where the client sends outbound OSC (e.g. a
TouchOSC layout running on a tablet on the LAN).
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from pythonosc import udp_client
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

from app.event_bus import (
    ConnectionState,
    ConnectionStateEvent,
    ConnectionSubsystem,
    EventBus,
    OscMessageEvent,
    OscSendEvent,
)

log = logging.getLogger(__name__)


class OscManager:
    """Manages the OSC UDP server (inbound) and client (outbound).

    Lifecycle::

        mgr = OscManager(bus, osc_config)
        mgr.start_server()
        mgr.start_client()
        ...
        mgr.stop()
    """

    def __init__(self, bus: EventBus, osc_config: dict) -> None:
        """
        Args:
            bus:        The shared event bus.
            osc_config: The ``"osc"`` sub-dict from ``app_config.json``.
        """
        self._bus = bus
        self._config = osc_config

        self._server: ThreadingOSCUDPServer | None = None
        self._server_thread: threading.Thread | None = None

        self._client: udp_client.SimpleUDPClient | None = None

        self._out_queue: queue.Queue = queue.Queue()
        self._out_thread: threading.Thread | None = None
        self._out_stop = threading.Event()

        bus.subscribe([OscSendEvent], self._out_queue)

    # -----------------------------------------------------------------------
    # Server (inbound)
    # -----------------------------------------------------------------------

    def start_server(self) -> None:
        """Start the OSC UDP server on ``listen_host:listen_port``.

        All incoming OSC messages are forwarded to the event bus as
        ``OscMessageEvent`` regardless of address pattern — the mapping
        engine is responsible for routing.

        Emits ``ConnectionStateEvent`` for ``OSC_IN`` on success or failure.
        """
        if self._server is not None:
            return  # already running

        listen_host = self._config.get("listen_host", "0.0.0.0")
        listen_port = self._config.get("listen_port", 9000)

        dispatcher = Dispatcher()
        # Map all addresses to the same catch-all handler
        dispatcher.set_default_handler(self._make_handler("*"))

        try:
            server = ThreadingOSCUDPServer((listen_host, listen_port), dispatcher)
        except OSError as exc:
            self._emit_state(ConnectionSubsystem.OSC_IN, ConnectionState.ERROR,
                             f"Cannot bind OSC server on {listen_host}:{listen_port}: {exc}")
            return

        self._server = server
        self._server_thread = threading.Thread(
            target=server.serve_forever,
            name="OscServer",
            daemon=True,
        )
        self._server_thread.start()
        self._emit_state(ConnectionSubsystem.OSC_IN, ConnectionState.CONNECTED,
                         f"Listening on {listen_host}:{listen_port}")
        log.info("OSC server started on %s:%d", listen_host, listen_port)

    def stop_server(self) -> None:
        """Shut down the OSC UDP server thread."""
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._server_thread:
            self._server_thread.join(timeout=2.0)
            self._server_thread = None
        self._emit_state(ConnectionSubsystem.OSC_IN, ConnectionState.DISCONNECTED)

    # -----------------------------------------------------------------------
    # Client (outbound)
    # -----------------------------------------------------------------------

    def start_client(self) -> None:
        """Open the UDP client targeting ``send_host:send_port`` and start
        the output-drain thread.

        Emits ``ConnectionStateEvent`` for ``OSC_OUT`` on success or failure.
        """
        if self._client is not None:
            return  # already running

        send_host = self._config.get("send_host", "127.0.0.1")
        send_port = self._config.get("send_port", 9001)

        try:
            self._client = udp_client.SimpleUDPClient(send_host, send_port)
        except Exception as exc:
            self._emit_state(ConnectionSubsystem.OSC_OUT, ConnectionState.ERROR,
                             f"Cannot create OSC client for {send_host}:{send_port}: {exc}")
            return

        self._out_stop.clear()
        self._out_thread = threading.Thread(
            target=self._output_loop, name="OscOutput", daemon=True,
        )
        self._out_thread.start()
        self._emit_state(ConnectionSubsystem.OSC_OUT, ConnectionState.CONNECTED,
                         f"Sending to {send_host}:{send_port}")
        log.info("OSC client started, target %s:%d", send_host, send_port)

    def stop_client(self) -> None:
        """Stop the output-drain thread and tear down the UDP client."""
        self._out_stop.set()
        if self._out_thread:
            self._out_thread.join(timeout=2.0)
            self._out_thread = None
        self._client = None
        self._emit_state(ConnectionSubsystem.OSC_OUT, ConnectionState.DISCONNECTED)

    def stop(self) -> None:
        """Convenience: stop both server and client."""
        self.stop_server()
        self.stop_client()

    # -----------------------------------------------------------------------
    # Outbound helpers
    # -----------------------------------------------------------------------

    def send_message(self, address: str, *args: Any) -> None:
        """Send an OSC message immediately (bypasses the output queue).

        Prefer posting ``OscSendEvent`` onto the bus instead, unless you
        need synchronous delivery.
        """
        if self._client is None:
            log.warning("OSC send_message called but client is not started")
            return
        try:
            self._client.send_message(address, list(args))
        except Exception as exc:
            log.error("OSC send_message failed for %s: %s", address, exc)

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _make_handler(self, address: str):
        """Return a python-osc handler function for *address* that posts an
        ``OscMessageEvent`` onto the bus.

        This is the callback signature required by ``pythonosc.dispatcher``::

            handler(address: str, *args) -> None
        """
        def handler(addr: str, *args: Any) -> None:
            self._bus.put(OscMessageEvent(address=addr, args=list(args)))
        return handler

    def _output_loop(self) -> None:
        """Thread target — drains ``_out_queue`` and sends OSC messages."""
        while not self._out_stop.is_set():
            try:
                event = self._out_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if self._client is None:
                continue
            try:
                self._client.send_message(event.address, event.args)
            except Exception:
                log.exception("OSC output error for address %s", event.address)

    def _emit_state(self, subsystem: ConnectionSubsystem,
                    state: ConnectionState, message: str = "") -> None:
        self._bus.put(ConnectionStateEvent(
            subsystem=subsystem, state=state, message=message
        ))
