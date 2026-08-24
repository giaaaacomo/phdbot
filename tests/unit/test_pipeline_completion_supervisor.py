from scripts.pipeline_completion_supervisor import (
    _saved_followup_run,
    completion_action,
    is_transient_failure,
)


def _schedule(state: str, run_id: int | None = 67) -> dict[str, object]:
    return {"state": state, "pipeline_run_id": run_id}


def _pipeline(state: str = "running", run_id: int = 67) -> dict[str, object]:
    return {"state": state, "run_id": run_id}


def test_active_schedule_never_triggers_followup() -> None:
    for state in ("scheduled", "starting", "waiting_pipeline", "running"):
        assert (
            completion_action(
                _schedule(state),
                _pipeline("failed"),
                expected_run=67,
            )
            == "wait"
        )


def test_completed_schedule_deploys_the_prebuilt_index_followup() -> None:
    assert (
        completion_action(
            _schedule("done"),
            _pipeline("done"),
            expected_run=67,
        )
        == "deploy_index"
    )


def test_failed_schedule_salvages_index_but_respects_deliberate_stop() -> None:
    assert (
        completion_action(
            _schedule("failed"),
            _pipeline("failed"),
            expected_run=67,
        )
        == "deploy_index"
    )
    assert (
        completion_action(
            _schedule("failed"),
            _pipeline("stopped"),
            expected_run=67,
        )
        == "report_only"
    )


def test_changed_schedule_run_aborts_instead_of_touching_another_pipeline() -> None:
    assert (
        completion_action(
            _schedule("done", 68),
            _pipeline("done", 68),
            expected_run=67,
        )
        == "abort"
    )


def test_only_operational_failures_are_automatically_resumed() -> None:
    assert is_transient_failure("HTTP 429 rate limit")
    assert is_transient_failure("Ollama connection timed out")
    assert not is_transient_failure("database schema is invalid")
    assert not is_transient_failure(None)


def test_saved_followup_run_survives_each_indexing_checkpoint_shape() -> None:
    assert _saved_followup_run({"followup_run": 68}) == 68
    assert _saved_followup_run({"followup_status": {"run_id": 68}}) == 68
    assert _saved_followup_run({"followup_status": {"run_id": "68"}}) is None
    assert _saved_followup_run({}) is None
