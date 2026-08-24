from pathlib import Path

from scripts.display_refresh_supervisor import (
    DisplayState,
    LogicalMonitor,
    Mode,
    Monitor,
    RefreshController,
    choose_reduced_mode,
    is_pipeline_active,
)


def _monitor(current: str = "3440x1440@143.923") -> Monitor:
    modes = (
        Mode("3440x1440@143.923", 3440, 1440, 143.923, current == "3440x1440@143.923"),
        Mode("3440x1440@120.000", 3440, 1440, 120.0, current == "3440x1440@120.000"),
        Mode("3440x1440@75.050", 3440, 1440, 75.05, current == "3440x1440@75.050"),
        Mode("2560x1440@59.951", 2560, 1440, 59.951, current == "2560x1440@59.951"),
        Mode("3440x1440@75.050+vrr", 3440, 1440, 75.05, False, True),
    )
    return Monitor("DP-2", "IVM", "PL3461WQ", "1171803201427", 'Iiyama 34"', False, modes)


def _state(current: str = "3440x1440@143.923") -> DisplayState:
    monitor = _monitor(current)
    return DisplayState(1, (monitor,), (LogicalMonitor(0, 0, 1.0, 0, True, ("DP-2",)),), 1)


class FakeDisplay:
    def __init__(self) -> None:
        self.current = "3440x1440@143.923"
        self.applied: list[str] = []

    def read_state(self) -> DisplayState:
        return _state(self.current)

    def apply_temporary_mode(self, connector: str, mode_name: str) -> None:
        assert connector == "DP-2"
        self.current = mode_name
        self.applied.append(mode_name)


def test_reduced_mode_preserves_native_resolution_and_avoids_vrr() -> None:
    reduced = choose_reduced_mode(_monitor(), 60.0)

    assert reduced is not None
    assert reduced.name == "3440x1440@75.050"


def test_reduced_mode_uses_exact_same_resolution_60_hz_when_available() -> None:
    monitor = _monitor()
    exact = Mode("3440x1440@60.000", 3440, 1440, 60.0)
    monitor = Monitor(**{**monitor.__dict__, "modes": (*monitor.modes, exact)})

    assert choose_reduced_mode(monitor, 60.0) == exact


def test_pipeline_stopping_remains_active_until_checkpoint() -> None:
    assert is_pipeline_active({"state": "running"})
    assert is_pipeline_active({"state": "stopping"})
    assert not is_pipeline_active({"state": "stopped"})
    assert not is_pipeline_active({"state": "failed"})


def test_controller_restores_exact_original_mode(tmp_path: Path) -> None:
    display = FakeDisplay()
    controller = RefreshController(
        display,
        tmp_path / "lease.json",
        connector="DP-2",
        product="PL3461WQ",
        target_hz=60.0,
    )

    assert "143.923 -> 3440x1440@75.050" in controller.reduce(65)
    assert display.current == "3440x1440@75.050"
    assert "already reduced" in controller.reduce(65)
    assert display.applied == ["3440x1440@75.050"]

    assert "restored" in controller.restore()
    assert display.current == "3440x1440@143.923"
    assert display.applied == ["3440x1440@75.050", "3440x1440@143.923"]
    assert not (tmp_path / "lease.json").exists()


def test_controller_does_not_overwrite_manual_display_change(tmp_path: Path) -> None:
    display = FakeDisplay()
    state_file = tmp_path / "lease.json"
    controller = RefreshController(
        display,
        state_file,
        connector="DP-2",
        product="PL3461WQ",
        target_hz=60.0,
    )
    controller.reduce(65)
    display.current = "3440x1440@120.000"

    assert "manual display override preserved" in controller.reduce(65)
    assert "restore skipped" in controller.restore()
    assert display.current == "3440x1440@120.000"
    assert not state_file.exists()
