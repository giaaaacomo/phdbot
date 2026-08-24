#!/usr/bin/env python3
"""Temporarily reduce an external monitor's refresh while PHDBOT is active.

This is deliberately a host-side user-session helper.  The PHDBOT API stays
inside Docker and never receives access to the GNOME session bus.  Display
changes are temporary, preserve the complete live layout, and are restored
only when the monitor still uses the mode applied by this helper.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_ACTIVE_STATES = frozenset({"running", "stopping"})
_DEFAULT_INTERVAL_SECONDS = 300.0
_DEFAULT_UNAVAILABLE_GRACE_SECONDS = 60.0


@dataclass(frozen=True)
class Mode:
    name: str
    width: int
    height: int
    refresh_hz: float
    is_current: bool = False
    is_variable: bool = False


@dataclass(frozen=True)
class Monitor:
    connector: str
    vendor: str
    product: str
    serial: str
    display_name: str
    is_builtin: bool
    modes: tuple[Mode, ...]
    color_mode: int | None = None
    rgb_range: int | None = None

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.vendor, self.product, self.serial)

    @property
    def current_mode(self) -> Mode | None:
        return next((mode for mode in self.modes if mode.is_current), None)


@dataclass(frozen=True)
class LogicalMonitor:
    x: int
    y: int
    scale: float
    transform: int
    is_primary: bool
    connectors: tuple[str, ...]


@dataclass(frozen=True)
class DisplayState:
    serial: int
    monitors: tuple[Monitor, ...]
    logical_monitors: tuple[LogicalMonitor, ...]
    layout_mode: int

    def monitor_by_connector(self, connector: str) -> Monitor | None:
        return next((monitor for monitor in self.monitors if monitor.connector == connector), None)

    def monitor_by_identity(self, identity: tuple[str, str, str]) -> Monitor | None:
        return next((monitor for monitor in self.monitors if monitor.identity == identity), None)


@dataclass
class RefreshLease:
    identity: tuple[str, str, str]
    connector: str
    original_mode: str
    reduced_mode: str
    run_id: int | str | None
    applied: bool = False
    suppressed: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> RefreshLease:
        raw_identity = raw.get("identity")
        if not isinstance(raw_identity, list) or len(raw_identity) != 3:
            raise ValueError("invalid monitor identity in refresh lease")
        if not all(isinstance(value, str) for value in raw_identity):
            raise ValueError("invalid monitor identity values in refresh lease")
        connector = raw.get("connector")
        original_mode = raw.get("original_mode")
        reduced_mode = raw.get("reduced_mode")
        if not all(isinstance(value, str) for value in (connector, original_mode, reduced_mode)):
            raise ValueError("invalid mode data in refresh lease")
        run_id = raw.get("run_id")
        if run_id is not None and not isinstance(run_id, int | str):
            raise ValueError("invalid run id in refresh lease")
        return cls(
            identity=(raw_identity[0], raw_identity[1], raw_identity[2]),
            connector=connector,
            original_mode=original_mode,
            reduced_mode=reduced_mode,
            run_id=run_id,
            applied=raw.get("applied") is True,
            suppressed=raw.get("suppressed") is True,
        )


class DisplayBackend(Protocol):
    def read_state(self) -> DisplayState: ...

    def apply_temporary_mode(self, connector: str, mode_name: str) -> None: ...


def choose_reduced_mode(monitor: Monitor, target_hz: float) -> Mode | None:
    """Pick the lowest same-resolution fixed mode at or above the target."""
    current = monitor.current_mode
    if current is None:
        return None
    same_resolution = [
        mode
        for mode in monitor.modes
        if mode.width == current.width
        and mode.height == current.height
        and not mode.is_variable
        and mode.refresh_hz < current.refresh_hz - 0.5
    ]
    if not same_resolution:
        return None
    at_or_above = [mode for mode in same_resolution if mode.refresh_hz >= target_hz - 0.5]
    if at_or_above:
        return min(at_or_above, key=lambda mode: mode.refresh_hz)
    return min(same_resolution, key=lambda mode: abs(mode.refresh_hz - target_hz))


def is_pipeline_active(status: dict[str, Any]) -> bool:
    return status.get("state") in _ACTIVE_STATES


def _property_int(properties: object, name: str) -> int | None:
    if not hasattr(properties, "get"):
        return None
    value = properties.get(name)  # type: ignore[union-attr]
    return int(value) if isinstance(value, int) else None


class MutterDisplay:
    """Minimal typed client for org.gnome.Mutter.DisplayConfig."""

    _NAME = "org.gnome.Mutter.DisplayConfig"
    _PATH = "/org/gnome/Mutter/DisplayConfig"
    _INTERFACE = "org.gnome.Mutter.DisplayConfig"

    @staticmethod
    def _libraries() -> tuple[Any, Any]:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        return Gio, GLib

    def _proxy(self) -> object:
        gio, _ = self._libraries()
        return gio.DBusProxy.new_for_bus_sync(
            gio.BusType.SESSION,
            gio.DBusProxyFlags.NONE,
            None,
            self._NAME,
            self._PATH,
            self._INTERFACE,
            None,
        )

    def read_state(self) -> DisplayState:
        gio, _ = self._libraries()
        result = self._proxy().call_sync(
            "GetCurrentState",
            None,
            gio.DBusCallFlags.NO_AUTO_START,
            10_000,
            None,
        )
        serial = int(result[0])
        monitors: list[Monitor] = []
        for raw_monitor in result[1]:
            spec = raw_monitor[0]
            properties = raw_monitor[2]
            modes: list[Mode] = []
            for raw_mode in raw_monitor[1]:
                mode_properties = raw_mode[6]
                modes.append(
                    Mode(
                        name=str(raw_mode[0]),
                        width=int(raw_mode[1]),
                        height=int(raw_mode[2]),
                        refresh_hz=float(raw_mode[3]),
                        is_current="is-current" in mode_properties,
                        is_variable=(
                            mode_properties.get("refresh-rate-mode") == "variable"
                            or str(raw_mode[0]).endswith("+vrr")
                        ),
                    )
                )
            monitors.append(
                Monitor(
                    connector=str(spec[0]),
                    vendor=str(spec[1]),
                    product=str(spec[2]),
                    serial=str(spec[3]),
                    display_name=str(properties.get("display-name", spec[0])),
                    is_builtin=properties.get("is-builtin") is True,
                    modes=tuple(modes),
                    color_mode=_property_int(properties, "color-mode"),
                    rgb_range=_property_int(properties, "rgb-range"),
                )
            )
        logical_monitors = tuple(
            LogicalMonitor(
                x=int(raw[0]),
                y=int(raw[1]),
                scale=float(raw[2]),
                transform=int(raw[3]),
                is_primary=bool(raw[4]),
                connectors=tuple(str(spec[0]) for spec in raw[5]),
            )
            for raw in result[2]
        )
        properties = result[3]
        layout_mode = _property_int(properties, "layout-mode") or 1
        return DisplayState(serial, tuple(monitors), logical_monitors, layout_mode)

    def _configuration(self, state: DisplayState, connector: str, mode_name: str) -> list[tuple[Any, ...]]:
        _, glib = self._libraries()
        configuration: list[tuple[Any, ...]] = []
        for logical in state.logical_monitors:
            physical: list[tuple[str, str, dict[str, Any]]] = []
            for active_connector in logical.connectors:
                monitor = state.monitor_by_connector(active_connector)
                if monitor is None or monitor.current_mode is None:
                    raise RuntimeError(f"active monitor {active_connector} has no current mode")
                selected_mode = mode_name if active_connector == connector else monitor.current_mode.name
                if not any(mode.name == selected_mode for mode in monitor.modes):
                    raise RuntimeError(f"mode {selected_mode} is unavailable on {active_connector}")
                options: dict[str, Any] = {}
                if monitor.color_mode is not None:
                    options["color-mode"] = glib.Variant("u", monitor.color_mode)
                if monitor.rgb_range is not None:
                    options["rgb-range"] = glib.Variant("u", monitor.rgb_range)
                physical.append((active_connector, selected_mode, options))
            configuration.append(
                (
                    logical.x,
                    logical.y,
                    logical.scale,
                    logical.transform,
                    logical.is_primary,
                    physical,
                )
            )
        return configuration

    def _apply(self, state: DisplayState, connector: str, mode_name: str, method: int) -> None:
        gio, glib = self._libraries()
        parameters = glib.Variant(
            "(uua(iiduba(ssa{sv}))a{sv})",
            (
                state.serial,
                method,
                self._configuration(state, connector, mode_name),
                {"layout-mode": glib.Variant("u", state.layout_mode)},
            ),
        )
        self._proxy().call_sync(
            "ApplyMonitorsConfig",
            parameters,
            gio.DBusCallFlags.NO_AUTO_START,
            10_000,
            None,
        )

    def apply_temporary_mode(self, connector: str, mode_name: str) -> None:
        # Verify first (method 0), then re-read the serial and apply temporarily
        # (method 1).  Persistent GNOME monitor settings are never overwritten.
        self._apply(self.read_state(), connector, mode_name, 0)
        self._apply(self.read_state(), connector, mode_name, 1)
        confirmed = self.read_state().monitor_by_connector(connector)
        if confirmed is None or confirmed.current_mode is None or confirmed.current_mode.name != mode_name:
            raise RuntimeError(f"GNOME did not apply {mode_name} to {connector}")


class RefreshController:
    def __init__(
        self,
        display: DisplayBackend,
        state_file: Path,
        *,
        connector: str,
        product: str | None,
        target_hz: float,
    ) -> None:
        self._display = display
        self._state_file = state_file
        self._connector = connector
        self._product = product
        self._target_hz = target_hz

    def _load_lease(self) -> RefreshLease | None:
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(raw, dict):
            raise ValueError("refresh state is not a JSON object")
        return RefreshLease.from_dict(raw)

    def _save_lease(self, lease: RefreshLease) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_file.with_suffix(f"{self._state_file.suffix}.tmp")
        temporary.write_text(json.dumps(asdict(lease), indent=2), encoding="utf-8")
        os.replace(temporary, self._state_file)

    def _clear_lease(self) -> None:
        self._state_file.unlink(missing_ok=True)

    def _target_monitor(self, state: DisplayState) -> Monitor | None:
        exact = state.monitor_by_connector(self._connector)
        if exact is not None and not exact.is_builtin and (self._product is None or exact.product == self._product):
            return exact
        candidates = [
            monitor
            for monitor in state.monitors
            if not monitor.is_builtin
            and monitor.current_mode is not None
            and (self._product is None or monitor.product == self._product)
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def reduce(self, run_id: int | str | None) -> str:
        state = self._display.read_state()
        lease = self._load_lease()
        if lease is not None:
            monitor = state.monitor_by_identity(lease.identity)
            if monitor is None or monitor.current_mode is None:
                return "waiting for leased monitor"
            if lease.suppressed:
                return "manual display override preserved"
            if lease.applied:
                if monitor.current_mode.name != lease.reduced_mode:
                    lease.suppressed = True
                    self._save_lease(lease)
                    return "manual display override preserved"
                return f"already reduced to {lease.reduced_mode}"
            self._display.apply_temporary_mode(monitor.connector, lease.reduced_mode)
            lease.connector = monitor.connector
            lease.applied = True
            self._save_lease(lease)
            return f"reduced {monitor.display_name} to {lease.reduced_mode}"

        monitor = self._target_monitor(state)
        if monitor is None or monitor.current_mode is None:
            return "target monitor unavailable"
        reduced = choose_reduced_mode(monitor, self._target_hz)
        if reduced is None:
            return "no lower same-resolution mode available"
        lease = RefreshLease(
            identity=monitor.identity,
            connector=monitor.connector,
            original_mode=monitor.current_mode.name,
            reduced_mode=reduced.name,
            run_id=run_id,
        )
        # Persist before modesetting so a restarted helper can always recover.
        self._save_lease(lease)
        self._display.apply_temporary_mode(monitor.connector, reduced.name)
        lease.applied = True
        self._save_lease(lease)
        return f"reduced {monitor.display_name}: {lease.original_mode} -> {lease.reduced_mode}"

    def restore(self) -> str:
        lease = self._load_lease()
        if lease is None:
            return "nothing to restore"
        state = self._display.read_state()
        monitor = state.monitor_by_identity(lease.identity)
        if monitor is None or monitor.current_mode is None:
            return "waiting for leased monitor"
        if lease.suppressed or monitor.current_mode.name != lease.reduced_mode:
            self._clear_lease()
            return "restore skipped because the display was changed manually"
        self._display.apply_temporary_mode(monitor.connector, lease.original_mode)
        self._clear_lease()
        return f"restored {monitor.display_name} to {lease.original_mode}"


def _pipeline_status(base_url: str) -> dict[str, Any]:
    request = Request(f"{base_url.rstrip('/')}/v1/pipeline/status", method="GET")
    with urlopen(request, timeout=5) as response:
        parsed = json.load(response)
    if not isinstance(parsed, dict):
        raise TypeError("PHDBOT returned a non-object pipeline status")
    return parsed


def _log(message: str) -> None:
    print(f"{datetime.now(UTC).isoformat()} {message}", flush=True)


def _default_state_file() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "phdbot-display-refresh.json"
    return Path(f"/run/user/{os.getuid()}/phdbot-display-refresh.json")


def _controller(args: argparse.Namespace) -> RefreshController:
    return RefreshController(
        MutterDisplay(),
        args.state_file,
        connector=args.connector,
        product=args.product,
        target_hz=args.target_hz,
    )


def _watch(args: argparse.Namespace) -> int:
    controller = _controller(args)
    stop_event = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    unavailable_since: float | None = None
    last_message: str | None = None
    _log("PHDBOT display refresh supervisor started")
    try:
        while not stop_event.is_set():
            try:
                status = _pipeline_status(args.base_url)
                unavailable_since = None
                if is_pipeline_active(status):
                    message = controller.reduce(status.get("run_id"))
                else:
                    message = controller.restore()
            except (HTTPError, URLError, OSError, TimeoutError, TypeError, ValueError, json.JSONDecodeError) as exc:
                now = time.monotonic()
                unavailable_since = unavailable_since or now
                if now - unavailable_since < args.unavailable_grace:
                    message = f"PHDBOT status unavailable; retaining display state ({type(exc).__name__})"
                else:
                    try:
                        message = controller.restore()
                    except Exception as restore_exc:  # display service may also be unavailable during logout
                        message = f"display restore deferred ({type(restore_exc).__name__})"
            except Exception as exc:  # a display error must never affect PHDBOT itself
                message = f"display adjustment failed ({type(exc).__name__}: {exc})"
            if message != last_message:
                _log(message)
                last_message = message
            stop_event.wait(args.interval)
    finally:
        try:
            _log(controller.restore())
        except Exception as exc:
            _log(f"final display restore deferred ({type(exc).__name__}: {exc})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8003")
    parser.add_argument(
        "--connector",
        default=None,
        help="Optional display connector; otherwise use the sole external display",
    )
    parser.add_argument(
        "--product",
        default=None,
        help="Optional monitor product constraint",
    )
    parser.add_argument("--target-hz", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=_DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--unavailable-grace", type=float, default=_DEFAULT_UNAVAILABLE_GRACE_SECONDS)
    parser.add_argument("--state-file", type=Path, default=_default_state_file())
    parser.add_argument("--action", choices=("watch", "once", "reduce", "restore", "status"), default="watch")
    args = parser.parse_args()
    controller = _controller(args)

    if args.action == "watch":
        return _watch(args)
    if args.action == "reduce":
        print(controller.reduce("manual"))
        return 0
    if args.action == "restore":
        print(controller.restore())
        return 0
    if args.action == "once":
        status = _pipeline_status(args.base_url)
        message = controller.reduce(status.get("run_id")) if is_pipeline_active(status) else controller.restore()
        print(message)
        return 0

    state = controller._display.read_state()
    target = controller._target_monitor(state)
    if target is None:
        print(json.dumps({"target": None, "lease": None}, indent=2))
        return 0
    reduced = choose_reduced_mode(target, args.target_hz)
    print(
        json.dumps(
            {
                "connector": target.connector,
                "display_name": target.display_name,
                "current_mode": target.current_mode.name if target.current_mode else None,
                "selected_reduced_mode": reduced.name if reduced else None,
                "state_file": str(args.state_file),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
