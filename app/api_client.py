"""
api_client.py — TSW6 External Interface API client.

Runs a dedicated poll-loop thread that reads the TSW subscription at the
configured ``poll_interval_hz`` and posts ``SubscriptionResultEvent`` onto
the event bus.

Write commands (``ApiSetEvent``) are consumed from the bus and executed
immediately on the poll-loop thread to keep all network I/O off the GUI thread.

Auth: the ``DTGCommKey`` header value is re-read from disk on every
``connect()`` call so key rotation is handled transparently.

Subscription response format (TSW6 External Interface API):
    GET /subscription?Subscription=<id> returns a JSON object whose top-level
    key is the endpoint path and whose value is either a bare number or a dict
    containing a "Value" key.  Both formats are handled.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import requests

from app.event_bus import (
    ApiSetEvent,
    ConnectionState,
    ConnectionStateEvent,
    ConnectionSubsystem,
    EventBus,
    SubscriptionResultEvent,
)

log = logging.getLogger(__name__)


_MAX_RETRIES = 10


class ApiClient:
    """HTTP client for the TSW6 External Interface API (port 31270).

    Lifecycle::

        client = ApiClient(bus, config)
        client.connect()          # reads key, starts poll thread (which retries up to 10x)
        ...
        client.disconnect()       # stops poll thread, cleans up subscription
    """

    def __init__(self, bus: EventBus, config: dict) -> None:
        """
        Args:
            bus:    Shared event bus.
            config: The ``"api"`` section of ``app_config.json``.
        """
        self._bus = bus
        self._config = config
        self._session: requests.Session | None = None
        self._comm_key: str = ""
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False

        # Queue of ApiSetEvents to send; filled by bus subscription
        self._send_queue: queue.Queue[ApiSetEvent] = queue.Queue()
        bus.subscribe([ApiSetEvent], self._send_queue)

    # -----------------------------------------------------------------------
    # Connection lifecycle
    # -----------------------------------------------------------------------

    def connect(self, endpoints: list[str]) -> None:
        """Read comm key, start poll thread (which establishes the connection with retries).

        Args:
            endpoints: API paths with ``poll: true`` from the active train config.
        """
        if self._thread and self._thread.is_alive():
            self.disconnect()

        try:
            self._comm_key = self._read_comm_key()
        except Exception as exc:
            self._emit_state(ConnectionState.ERROR, f"Cannot read CommAPIKey: {exc}")
            return

        self._stop_event.clear()
        self._emit_state(ConnectionState.CONNECTING)
        self._thread = threading.Thread(
            target=self._poll_loop,
            args=(endpoints,),
            name="ApiClient",
            daemon=True,
        )
        self._thread.start()

    def disconnect(self) -> None:
        """Stop the poll thread and close the HTTP session."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._session:
            try:
                self._delete_subscription()
            except Exception:
                pass
            self._session.close()
            self._session = None
        self._connected = False
        self._emit_state(ConnectionState.DISCONNECTED)

    @property
    def is_connected(self) -> bool:
        return self._connected and bool(self._thread and self._thread.is_alive())

    # -----------------------------------------------------------------------
    # Core HTTP helpers
    # -----------------------------------------------------------------------

    def get(self, path: str) -> Any:
        """``GET /get/<path>`` — returns the parsed value field from the response."""
        url  = f"{self._base_url()}/get/{path}"
        resp = self._session.get(url, headers=self._headers(),
                                 timeout=self._config["request_timeout_s"])
        resp.raise_for_status()
        data = resp.json()
        # TSW returns {"Value": <v>, ...} for single-endpoint GETs
        if isinstance(data, dict) and "Value" in data:
            return data["Value"]
        return data

    def set(self, path: str, value: Any) -> None:
        """``PATCH /set/<path>?Value=<value>``"""
        url  = f"{self._base_url()}/set/{path}"
        resp = self._session.patch(url, params={"Value": value},
                                   headers=self._headers(),
                                   timeout=self._config["request_timeout_s"])
        resp.raise_for_status()

    def list_node(self, path: str = "") -> dict:
        """``GET /list/<path>`` — returns the full response dict."""
        url  = f"{self._base_url()}/list/{path}" if path else f"{self._base_url()}/list"
        resp = self._session.get(url, headers=self._headers(),
                                 timeout=self._config["request_timeout_s"])
        resp.raise_for_status()
        return resp.json()

    def info(self) -> dict:
        """``GET /info``"""
        resp = self._session.get(f"{self._base_url()}/info",
                                 headers=self._headers(),
                                 timeout=self._config["request_timeout_s"])
        resp.raise_for_status()
        return resp.json()

    # -----------------------------------------------------------------------
    # Subscription management
    # -----------------------------------------------------------------------

    def _register_subscriptions(self, endpoints: list[str]) -> None:
        """DELETE existing subscription then POST each endpoint."""
        sub_id = self._config["subscription_id"]
        # Ignore errors on DELETE — normal on a fresh game start
        try:
            self._delete_subscription()
        except Exception:
            pass

        for endpoint in endpoints:
            url  = f"{self._base_url()}/subscription/{endpoint}"
            resp = self._session.post(
                url,
                params={"Subscription": sub_id},
                headers=self._headers(),
                timeout=self._config["request_timeout_s"],
            )
            resp.raise_for_status()
            log.debug("Registered subscription endpoint: %s", endpoint)

    def _read_subscription(self) -> dict:
        """``GET /subscription?Subscription=<id>`` — returns normalised {path: value}."""
        sub_id = self._config["subscription_id"]
        url    = f"{self._base_url()}/subscription"
        resp   = self._session.get(
            url,
            params={"Subscription": sub_id},
            headers=self._headers(),
            timeout=self._config["request_timeout_s"],
        )
        resp.raise_for_status()
        raw = resp.json()
        return self._parse_subscription_response(raw)

    def _delete_subscription(self) -> None:
        """``DELETE /subscription?Subscription=<id>``"""
        sub_id = self._config["subscription_id"]
        url    = f"{self._base_url()}/subscription"
        resp   = self._session.delete(
            url,
            params={"Subscription": sub_id},
            headers=self._headers(),
            timeout=self._config["request_timeout_s"],
        )
        resp.raise_for_status()

    @staticmethod
    def _parse_subscription_response(raw: Any) -> dict[str, Any]:
        """Normalise the subscription GET response to a flat {path: value} dict.

        Handles three observed TSW API formats:

        Format A — flat dict (most common):
            {"CurrentDrivableActor/...": 0.5, ...}

        Format B — dict of dicts with a "Value" key:
            {"CurrentDrivableActor/...": {"Value": 0.5, ...}, ...}

        Format C — envelope with "Entries" key (some TSW versions):
            {"RequestedSubscriptionID": "...", "Entries": { ... Format A/B ... }}
            or
            {"RequestedSubscriptionID": "...", "Entries": [{"Path": "...", "Value": v}, ...]}
        """
        if not isinstance(raw, dict):
            log.warning("Unexpected subscription response type: %r", type(raw))
            return {}

        log.debug("Raw subscription response: %r", raw)

        # Format C: unwrap envelope
        if "Entries" in raw:
            entries = raw["Entries"]
            if isinstance(entries, list):
                # List of {"Path": ..., "NodeValid": bool, "Values": v} objects
                result = {}
                for item in entries:
                    if not isinstance(item, dict) or "Path" not in item:
                        continue
                    if not item.get("NodeValid", True):
                        log.debug("Skipping invalid node: %s", item["Path"])
                        continue
                    val = item.get("Values")
                    if val is None:
                        log.debug("Skipping None value for: %s", item["Path"])
                        continue
                    # Values is a single-key dict e.g. {"ReturnValue": 1} or {"Speed (ms)": 0.5}
                    if isinstance(val, dict):
                        val = next(iter(val.values()), None)
                    if val is None:
                        continue
                    result[item["Path"]] = val
                return result
            elif isinstance(entries, dict):
                raw = entries  # fall through to Format A/B handling below
            else:
                log.warning("Unexpected 'Entries' type in subscription response: %r", type(entries))
                return {}

        result: dict[str, Any] = {}
        for key, val in raw.items():
            if isinstance(val, dict):
                result[key] = val.get("Value", val)
            else:
                result[key] = val
        return result

    # -----------------------------------------------------------------------
    # Poll loop (runs on dedicated thread)
    # -----------------------------------------------------------------------

    def _connect_with_retries(self, endpoints: list[str]) -> bool:
        """Try to establish (or re-establish) the API connection, retrying up to
        ``_MAX_RETRIES`` times with exponential back-off (2 s → 4 s → … → 30 s cap).

        Returns True on success, False if all attempts failed or stop was requested.
        """
        for attempt in range(_MAX_RETRIES + 1):
            if self._stop_event.is_set():
                return False

            if attempt > 0:
                delay = min(2 ** attempt, 30)
                self._emit_state(
                    ConnectionState.CONNECTING,
                    f"Reconnecting (attempt {attempt}/{_MAX_RETRIES})…",
                )
                log.info("Reconnect attempt %d/%d in %.0f s", attempt, _MAX_RETRIES, delay)
                if self._stop_event.wait(delay):
                    return False

            # Fresh session on every attempt
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
            self._session = requests.Session()

            try:
                self._register_subscriptions(endpoints)
            except Exception as exc:
                log.warning(
                    "Connection attempt %d/%d failed: %s",
                    attempt + 1, _MAX_RETRIES + 1, exc,
                )
                if attempt == _MAX_RETRIES:
                    self._emit_state(
                        ConnectionState.ERROR,
                        f"Connection failed after {_MAX_RETRIES + 1} attempts: {exc}",
                    )
                    return False
                continue

            self._enable_virtual_rail_driver()
            self._connected = True
            self._emit_state(ConnectionState.CONNECTED)
            return True

        return False  # unreachable; satisfies type checker

    def _poll_loop(self, endpoints: list[str]) -> None:
        """Thread target.  Establishes the connection then alternates between
        reading the subscription and draining ``_send_queue`` for SET commands.
        On connection loss, retries up to ``_MAX_RETRIES`` times before giving up.
        """
        if not self._connect_with_retries(endpoints):
            self._connected = False
            return

        interval = 1.0 / max(self._config.get("poll_interval_hz", 1.0), 0.1)
        next_poll = time.monotonic()

        while not self._stop_event.is_set():
            # --- Drain pending SET commands ----------------------------------
            connection_lost = False
            while True:
                try:
                    evt = self._send_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.set(evt.path, evt.value)
                except requests.ConnectionError as exc:
                    log.error("API connection lost during SET %s: %s", evt.path, exc)
                    self._connected = False
                    connection_lost = True
                    break
                except Exception as exc:
                    log.error("API set failed for %s: %s", evt.path, exc)
                    self._bus.put(self._make_error("ApiClient", f"SET {evt.path}: {exc}"))

            if connection_lost:
                self._emit_state(ConnectionState.ERROR, "Connection lost — reconnecting…")
                if not self._connect_with_retries(endpoints):
                    break
                next_poll = time.monotonic()
                continue

            # --- Poll subscription at configured rate -----------------------
            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + interval
                try:
                    data = self._read_subscription()
                    if data:
                        self._bus.put(SubscriptionResultEvent(data=data))
                    else:
                        log.warning("Subscription poll returned empty data — "
                                    "check that endpoints are registered and "
                                    "TSW is running with a drivable actor loaded")
                except requests.ConnectionError as exc:
                    log.error("API connection lost: %s", exc)
                    self._connected = False
                    self._emit_state(ConnectionState.ERROR, "Connection lost — reconnecting…")
                    if not self._connect_with_retries(endpoints):
                        break
                    next_poll = time.monotonic()
                except Exception as exc:
                    log.error("Subscription poll error: %s", exc)
                    self._bus.put(self._make_error("ApiClient", f"Poll error: {exc}"))

            time.sleep(0.005)

        self._connected = False

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _enable_virtual_rail_driver(self) -> None:
        """PATCH VirtualRailDriver.Enabled = 1.0.  Non-fatal if the path is absent."""
        try:
            resp = self._session.patch(
                f"{self._base_url()}/set/VirtualRailDriver.Enabled",
                params={"Value": 1.0},
                headers=self._headers(),
                timeout=self._config["request_timeout_s"],
            )
            resp.raise_for_status()
            log.debug("VirtualRailDriver.Enabled set to 1.0")
        except Exception as exc:
            log.warning("VirtualRailDriver.Enabled could not be set: %s", exc)

    def _read_comm_key(self) -> str:
        """Read ``CommAPIKey.txt`` and return the key, stripped of whitespace."""
        path = self._config.get("comm_key_path", "")
        if not path:
            raise FileNotFoundError("comm_key_path is not configured in app_config.json")
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()

    def _base_url(self) -> str:
        host = self._config["host"]
        port = self._config["port"]
        return f"http://{host}:{port}"

    def _headers(self) -> dict[str, str]:
        return {"DTGCommKey": self._comm_key}

    def _emit_state(self, state: ConnectionState, message: str = "") -> None:
        self._bus.put(ConnectionStateEvent(
            subsystem=ConnectionSubsystem.API,
            state=state,
            message=message,
        ))

    @staticmethod
    def _make_error(source: str, message: str):
        from app.event_bus import ErrorEvent
        return ErrorEvent(source=source, message=message)
