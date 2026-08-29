"""Unit test per Progress e PipelineRunner: niente rete, niente Postgres.

Il control plane reale è mediato da Postgres (advisory lock + colonna state). Qui i
primitivi DB/lock sono sostituiti da uno store in-memory: `FakeRunner` tiene un flag
`_lock_held` al posto dell'advisory lock e un dict di righe al posto della tabella, e
`FakeProgress` legge lo stop dalla riga in-memory come farebbe leggendo il DB.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from injector import Injector, singleton
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from phd_searcher.database.models.pipeline_run import PipelineRun
from phd_searcher.main import create_app
from phd_searcher.pipeline import progress as progress_mod
from phd_searcher.pipeline import runner as runner_mod
from phd_searcher.pipeline.progress import Progress, _persisted_label
from phd_searcher.pipeline.retry import RetryExhaustedError, _parse_time, retry_async
from phd_searcher.pipeline.runner import PipelineError, PipelineRunner
from phd_searcher.typedef.pipeline import PipelineStartBody

# --- Progress: matematica avg/ETA ---


def test_progress_avg_and_eta(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = itertools.count(start=0, step=10).__next__
    monkeypatch.setattr(progress_mod, "monotonic", lambda: float(clock()))
    p = Progress()

    async def scenario() -> None:
        await p.begin(4)  # t=0
        await p.tick("a")  # t=10: prima unità, nessuna chiusa
        await p.tick("b")  # t=20: chiude a (10s)
        await p.tick("c")  # t=30: chiude b (10s)
        await p.finish()  # t=40: chiude c (10s)

    asyncio.run(scenario())
    assert p.done == 3
    assert p.avg_seconds == 10.0
    assert p.eta_seconds == 10.0  # (4 - 3) * 10


def test_progress_empty_stage() -> None:
    p = Progress()
    asyncio.run(p.begin(0))
    assert p.avg_seconds is None
    assert p.eta_seconds is None
    assert not p.should_stop


def test_progress_truncates_only_persisted_label() -> None:
    label = "Titolo molto lungo " + ("è" * 600)
    persisted = _persisted_label(label)

    assert persisted is not None
    assert len(persisted) == 512
    assert persisted.endswith("…")
    assert label.endswith("è")


def test_pipeline_start_body_validates_max_pages() -> None:
    assert PipelineStartBody(max_pages=5).max_pages == 5
    assert PipelineStartBody(max_pages=874).max_pages == 874
    with pytest.raises(ValueError, match="greater than or equal"):
        PipelineStartBody(max_pages=0)
    with pytest.raises(ValueError, match="less than or equal"):
        PipelineStartBody(max_pages=1501)


def test_pipeline_start_body_validates_independent_limits() -> None:
    body = PipelineStartBody(limits={"universities": 10, "schema": 3, "index": 100})
    assert body.limits is not None
    assert body.limits.universities == 10
    assert body.limits.schema_items == 3
    with pytest.raises(ValueError, match="greater than or equal"):
        PipelineStartBody(limits={"scrape": 0})


# --- Progress: lo stop arriva dal DB (segnale cross-process) ---


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _FakeSession:
    """Sessione minima: execute ritorna sempre lo state corrente (l'UPDATE lo ignora)."""

    def __init__(self, state_box: list[str]) -> None:
        self._state_box = state_box

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, _stmt: object) -> _FakeResult:
        return _FakeResult(self._state_box[0])

    async def commit(self) -> None:
        return None


def test_progress_reads_stop_from_db() -> None:
    state_box = ["running"]

    def maker() -> _FakeSession:
        return _FakeSession(state_box)

    p = Progress(session_maker=cast(Any, maker), run_id=1)

    async def scenario() -> None:
        await p.begin(3)
        await p.tick("a")
        assert not p.should_stop
        state_box[0] = "stopping"  # un altro worker segnala lo stop scrivendo la riga
        await p.tick("b")  # la prossima unità lo rileva
        assert p.should_stop

    asyncio.run(scenario())


# --- Fake DB + fake advisory lock in-memory ---


@dataclass
class FakeRow:
    id: int
    scheduled_job_id: int | None = None
    state: str = "running"
    stages: list[str] = field(default_factory=list)
    stages_done: dict[str, int] = field(default_factory=dict)
    params: dict[str, object] = field(default_factory=dict)
    checkpoints: dict[str, object] = field(default_factory=dict)
    current_stage: str | None = None
    stage_total: int | None = None
    stage_done: int = 0
    current_label: str | None = None
    stage_elapsed_seconds: float = 0.0
    active_elapsed_seconds: float = 0.0
    active_started_at: datetime | None = None
    active_heartbeat_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class FakeProgress(Progress):
    """Legge lo stop dalla riga in-memory invece che dal DB."""

    def __init__(self, rows: dict[int, FakeRow], run_id: int, stage: str) -> None:
        super().__init__(None, run_id, stage)
        self._rows = rows

    async def _persist(self) -> None:
        row = self._rows.get(cast(int, self._run_id))
        if row is None:
            return
        row.stage_total = self.total
        row.stage_done = self.done
        row.current_label = self.current
        row.stage_elapsed_seconds = self.elapsed_seconds
        if row.state == "stopping":
            self.should_stop = True

    async def load_checkpoint(self) -> dict[str, object]:
        row = self._rows[cast(int, self._run_id)]
        value = row.checkpoints.get(cast(str, self._stage), {})
        self._checkpoint = dict(value) if isinstance(value, dict) else {}
        self._checkpoint_loaded = True
        return dict(self._checkpoint)

    async def save_checkpoint(self, **values: object) -> None:
        row = self._rows[cast(int, self._run_id)]
        await self.load_checkpoint()
        self._checkpoint.update(values)
        row.checkpoints = {**row.checkpoints, cast(str, self._stage): dict(self._checkpoint)}


class FakeRunner(PipelineRunner):
    def __init__(self) -> None:
        super().__init__(
            container=cast(Injector, None),  # gli stadi fake non lo usano
            session_maker=cast(async_sessionmaker[AsyncSession], None),
            engine=cast(AsyncEngine, None),
        )
        self.rows: dict[int, FakeRow] = {}
        self._lock_held = False  # advisory lock finto: ownership della run

    def _make_progress(self, run_id: int, stage: str) -> Progress:
        return FakeProgress(self.rows, run_id, stage)

    async def _acquire_lock(self) -> bool:
        if self._lock_held:
            return False
        self._lock_held = True
        return True

    async def _release_lock(self) -> None:
        self._lock_held = False

    async def _owner_alive(self) -> bool:
        return self._lock_held

    async def _insert_run(self, stages: list[str], params: dict[str, object]) -> int:
        run_id = len(self.rows) + 1
        now = runner_mod._utcnow()
        self.rows[run_id] = FakeRow(
            id=run_id,
            scheduled_job_id=cast("int | None", params.get("_scheduled_job_id")),
            stages=list(stages),
            params=dict(params),
            started_at=now,
            active_started_at=now,
            active_heartbeat_at=now,
        )
        return run_id

    async def _update(self, run_id: int, *, where_state: str | None = None, **values: object) -> None:
        row = self.rows[run_id]
        if where_state is not None and row.state != where_state:
            return
        for key, value in values.items():
            setattr(row, key, value)

    async def _latest(self) -> PipelineRun | None:
        if not self.rows:
            return None
        return cast(PipelineRun, self.rows[max(self.rows)])

    async def _get(self, run_id: int) -> PipelineRun | None:
        return cast(PipelineRun | None, self.rows.get(run_id))

    async def _scheduled_run(self, scheduled_job_id: int) -> PipelineRun | None:
        return cast(
            PipelineRun | None,
            next((row for row in self.rows.values() if row.scheduled_job_id == scheduled_job_id), None),
        )

    async def _merge_stage_done(self, run_id: int, stage: str, count: int) -> None:
        row = self.rows[run_id]
        row.stages_done = {**row.stages_done, stage: count}


# --- PipelineRunner ---


def test_runner_full_run(monkeypatch: pytest.MonkeyPatch) -> None:
    async def stage_a(
        container: object,
        *,
        limit: int | None = None,
        name_like: str | None = None,
        progress: Progress | None = None,
    ) -> int:
        assert progress is not None
        await progress.begin(1)
        await progress.tick("x")
        return 7

    monkeypatch.setattr(runner_mod, "STAGES", {"a": stage_a})

    async def scenario() -> tuple[FakeRunner, object]:
        r = FakeRunner()
        await r.start(["a"], {})
        assert r._task is not None
        await r._task
        assert not r._lock_held  # il lock è rilasciato a fine run
        return r, await r.status()

    _, status = asyncio.run(scenario())
    assert status.state == "done"
    assert status.stages_done == {"a": 7}
    assert status.stages_pending == []


def test_scheduled_runner_is_idempotent_across_reattachment(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = asyncio.Event()

    async def stage(
        container: object,
        *,
        limit: int | None = None,
        name_like: str | None = None,
        progress: Progress | None = None,
    ) -> int:
        await gate.wait()
        return 1

    monkeypatch.setattr(runner_mod, "STAGES", {"index": stage})

    async def scenario() -> tuple[int, int, FakeRunner]:
        runner = FakeRunner()
        first = await runner.ensure_scheduled(41, ["index"], {"limits": {"index": 10}})
        second = await runner.ensure_scheduled(41, ["index"], {"limits": {"index": 10}})
        gate.set()
        assert runner._task is not None
        await runner._task
        return first, second, runner

    first, second, runner = asyncio.run(scenario())
    assert first == second == 1
    assert len(runner.rows) == 1
    assert runner.rows[1].scheduled_job_id == 41


def test_scheduled_runner_resumes_the_existing_failed_row(monkeypatch: pytest.MonkeyPatch) -> None:
    async def stage(
        container: object,
        *,
        limit: int | None = None,
        name_like: str | None = None,
        progress: Progress | None = None,
    ) -> int:
        return 1

    monkeypatch.setattr(runner_mod, "STAGES", {"index": stage})

    async def scenario() -> FakeRunner:
        runner = FakeRunner()
        runner.rows[1] = FakeRow(
            id=1,
            scheduled_job_id=41,
            state="failed",
            stages=["index"],
            started_at=runner_mod._utcnow(),
            finished_at=runner_mod._utcnow(),
            error="connection reset by peer",
        )
        resumed = await runner.ensure_scheduled(41, ["index"], {})
        assert resumed == 1
        assert runner._task is not None
        await runner._task
        return runner

    runner = asyncio.run(scenario())
    assert len(runner.rows) == 1
    assert runner.rows[1].state == "done"
    assert runner.rows[1].stages_done == {"index": 1}


def test_runner_stop_and_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def slow(
        container: object,
        *,
        limit: int | None = None,
        name_like: str | None = None,
        progress: Progress | None = None,
    ) -> int:
        assert progress is not None
        checkpoint = await progress.load_checkpoint()
        await progress.begin(3)
        await progress.tick(f"u{checkpoint.get('next_item', 0)}")
        await progress.save_checkpoint(next_item=1)
        started.set()
        await proceed.wait()  # attende che il test chiami stop() (scrive state='stopping')
        for i in range(1, 3):
            if progress.should_stop:
                break
            await progress.tick(f"u{i}")
        return 1

    async def instant(
        container: object,
        *,
        limit: int | None = None,
        name_like: str | None = None,
        progress: Progress | None = None,
    ) -> int:
        assert progress is not None
        await progress.begin(1)
        await progress.tick("z")
        return 1

    monkeypatch.setattr(runner_mod, "STAGES", {"slow": slow, "later": instant})

    async def scenario() -> tuple[FakeRunner, object, object, list[str]]:
        r = FakeRunner()
        await r.start(["slow", "later"], {"limit": 5, "max_pages": 3})
        await started.wait()
        await r.stop()  # scrive state='stopping'; reconcile NON tocca la run (lock tenuto)
        stopping = await r.status()
        proceed.set()
        assert r._task is not None
        await r._task
        stopped = await r.status()
        await r.resume()
        pending = list(r.rows[1].stages)
        assert r._task is not None
        await r._task
        return r, stopping, stopped, pending

    r, stopping, stopped, pending = asyncio.run(scenario())
    assert stopping.state == "stopping"
    assert stopped.state == "stopped"
    assert stopped.stages_done == {}  # lo stadio interrotto non è "done"
    assert stopped.stages_pending == ["slow", "later"]
    assert pending == ["slow", "later"]  # resume ripete lo stadio interrotto
    assert r.rows[1].params == {"limit": 5, "max_pages": 3}  # resume riusa parametri e stessa run
    assert len(r.rows) == 1
    assert r.rows[1].checkpoints["slow"] == {"next_item": 1}
    assert r.rows[1].state == "done"


def test_runner_uses_independent_stage_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, int | None] = {}

    def stage(name: str):
        async def run_stage(
            container: object,
            *,
            limit: int | None = None,
            name_like: str | None = None,
            progress: Progress | None = None,
        ) -> int:
            seen[name] = limit
            return 0

        return run_stage

    monkeypatch.setattr(runner_mod, "STAGES", {"universities": stage("universities"), "index": stage("index")})

    async def scenario() -> None:
        runner = FakeRunner()
        await runner.start(
            ["universities", "index"],
            {"limit": 5, "limits": {"universities": 20, "index": 700}},
        )
        assert runner._task is not None
        await runner._task

    asyncio.run(scenario())
    assert seen == {"universities": 20, "index": 700}


def test_retry_is_durable_and_clears_after_success() -> None:
    progress = Progress()
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("temporary")
        return "ok"

    result = asyncio.run(retry_async(progress, "unit", flaky, base_delay=0))
    assert result == "ok"
    assert calls == 3
    assert asyncio.run(progress.load_checkpoint())["retries"] == {}


def test_retry_checkpoint_timestamp_is_parsed() -> None:
    parsed = _parse_time("2026-07-21T12:34:56+00:00")
    assert parsed == datetime(2026, 7, 21, 12, 34, 56, tzinfo=UTC)
    assert _parse_time("not-a-date") is None


def test_retry_records_exhausted_error() -> None:
    progress = Progress()

    async def broken() -> None:
        raise RuntimeError("still broken")

    with pytest.raises(RetryExhaustedError, match="still broken"):
        asyncio.run(retry_async(progress, "unit", broken, base_delay=0))
    retries = asyncio.run(progress.load_checkpoint())["retries"]
    assert isinstance(retries, dict)
    assert retries["unit"]["attempts"] == 3
    assert retries["unit"]["error"] == "still broken"


def test_retry_does_not_repeat_explicitly_non_retryable_error() -> None:
    class PermanentError(RuntimeError):
        pass

    progress = Progress()
    calls = 0

    async def denied() -> None:
        nonlocal calls
        calls += 1
        raise PermanentError("permanent")

    with pytest.raises(PermanentError, match="permanent"):
        asyncio.run(
            retry_async(
                progress,
                "unit",
                denied,
                base_delay=0,
                non_retryable=(PermanentError,),
            )
        )
    assert calls == 1
    assert asyncio.run(progress.load_checkpoint()).get("retries", {}) == {}


def test_retry_respects_retry_after_header(monkeypatch: pytest.MonkeyPatch) -> None:
    progress = Progress()
    sleeps: list[float] = []
    calls = 0

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def rate_limited_once() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            request = httpx.Request("GET", "https://query.wikidata.org/sparql")
            response = httpx.Response(429, headers={"Retry-After": "17"}, request=request)
            raise httpx.HTTPStatusError("rate limited", request=request, response=response)
        return "ok"

    monkeypatch.setattr("phd_searcher.pipeline.retry.asyncio.sleep", fake_sleep)
    result = asyncio.run(
        retry_async(progress, "wikidata:test", rate_limited_once, base_delay=2, max_delay=60)
    )
    assert result == "ok"
    assert sleeps == [17]
    assert asyncio.run(progress.load_checkpoint())["retries"] == {}


def test_retry_applies_configured_rate_limit_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    progress = Progress()
    sleeps: list[float] = []
    calls = 0

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def rate_limited_once() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("Blocked by anti-bot protection: HTTP 429 Too Many Requests")
        return "ok"

    monkeypatch.setattr("phd_searcher.pipeline.retry.asyncio.sleep", fake_sleep)
    result = asyncio.run(
        retry_async(
            progress,
            "euraxess:page",
            rate_limited_once,
            base_delay=10,
            max_delay=900,
            rate_limit_delay=300,
        )
    )
    assert result == "ok"
    assert sleeps == [30] * 10
    assert asyncio.run(progress.load_checkpoint())["retries"] == {}


def test_runner_rejects_double_start(monkeypatch: pytest.MonkeyPatch) -> None:
    release = asyncio.Event()

    async def block(
        container: object,
        *,
        limit: int | None = None,
        name_like: str | None = None,
        progress: Progress | None = None,
    ) -> int:
        await release.wait()
        return 1

    monkeypatch.setattr(runner_mod, "STAGES", {"a": block})

    async def scenario() -> None:
        r = FakeRunner()
        await r.start(["a"], {})
        with pytest.raises(PipelineError):  # lock già preso
            await r.start(["a"], {})
        with pytest.raises(PipelineError):  # resume→start→lock già preso
            await r.resume()
        release.set()
        assert r._task is not None
        await r._task

    asyncio.run(scenario())


def test_runner_reconciles_orphan_run() -> None:
    async def scenario() -> object:
        r = FakeRunner()
        r.rows[1] = FakeRow(id=1, state="running", stages=["scrape"])  # lock libero → owner morto
        return await r.status()

    status = asyncio.run(scenario())
    assert status.state == "stopped"
    assert status.error == "interrupted by restart"
    assert status.stages_pending == ["scrape"]


def test_runner_reconcile_uses_last_heartbeat_not_offline_time(monkeypatch: pytest.MonkeyPatch) -> None:
    base = datetime(2026, 7, 29, 10, 0)
    monkeypatch.setattr(runner_mod, "_utcnow", lambda: base + timedelta(hours=8))

    async def scenario() -> object:
        runner = FakeRunner()
        runner.rows[1] = FakeRow(
            id=1,
            state="running",
            stages=["scrape"],
            active_elapsed_seconds=15.0,
            active_started_at=base,
            active_heartbeat_at=base + timedelta(seconds=20),
        )
        return await runner.status()

    status = asyncio.run(scenario())
    assert status.state == "stopped"
    assert status.active_seconds == 35.0


def test_runner_reconcile_leaves_live_owner() -> None:
    async def scenario() -> object:
        r = FakeRunner()
        r.rows[1] = FakeRow(id=1, state="running", stages=["scrape"])
        r._lock_held = True  # un worker vivo (magari un altro processo) possiede la run
        return await r.status()

    status = asyncio.run(scenario())
    assert status.state == "running"  # non toccata
    assert status.error is None


def test_status_marks_database_timestamps_as_utc() -> None:
    async def scenario() -> object:
        runner = FakeRunner()
        runner.rows[1] = FakeRow(id=1, state="done", stages=[], started_at=datetime(2026, 7, 21, 9, 0))
        return await runner.status()

    status = asyncio.run(scenario())
    assert status.started_at is not None
    assert status.started_at.utcoffset() == timedelta(0)


def test_status_exposes_deferred_queue_and_suppresses_misleading_eta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 30, 12, 0)
    monkeypatch.setattr(runner_mod, "_utcnow", lambda: now)

    async def scenario() -> object:
        runner = FakeRunner()
        runner.rows[1] = FakeRow(
            id=1,
            state="running",
            stages=["enrich"],
            current_stage="enrich",
            stage_total=100,
            stage_done=99,
            stage_elapsed_seconds=990.0,
            checkpoints={
                "enrich": {
                    "deferred_details": {"10": {}, "11": {}},
                    "deferred_total": 5,
                    "deferred_processed": 3,
                    "euraxess_rate_limit_streak": 2,
                    "euraxess_cooldown_until": "2026-07-30T12:05:00+00:00",
                }
            },
        )
        runner._lock_held = True
        return await runner.status()

    status = asyncio.run(scenario())
    assert status.current_stage is not None
    assert status.current_stage.eta_seconds is None
    queue = status.current_stage.deferred_queue
    assert queue is not None
    assert (queue.source, queue.processed, queue.total, queue.remaining) == ("EURAXESS", 3, 5, 2)
    assert queue.retry_in_seconds == 300.0
    assert queue.rate_limit_streak == 2


def test_active_time_excludes_stopped_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    base = datetime(2026, 7, 29, 10, 0)
    current = [base + timedelta(seconds=10)]
    monkeypatch.setattr(runner_mod, "_utcnow", lambda: current[0])

    async def scenario() -> tuple[object, object, FakeRow]:
        runner = FakeRunner()
        row = FakeRow(
            id=1,
            state="running",
            stages=["a"],
            active_elapsed_seconds=30.0,
            active_started_at=base,
            started_at=base,
        )
        runner.rows[1] = row

        await runner._finish_active_interval(1, state="stopped")
        stopped = await runner.status()

        # Cinquanta secondi da stopped non devono entrare nel cronometro.
        current[0] = base + timedelta(seconds=60)
        await runner._update(1, state="running", finished_at=None, active_started_at=current[0])
        runner._lock_held = True  # simula il worker che possiede la run ripresa
        current[0] = base + timedelta(seconds=65)
        resumed = await runner.status()
        return stopped, resumed, row

    stopped, resumed, row = asyncio.run(scenario())
    assert stopped.active_seconds == 40.0
    assert resumed.active_seconds == 45.0
    assert row.active_elapsed_seconds == 40.0


def test_runner_failed_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(
        container: object,
        *,
        limit: int | None = None,
        name_like: str | None = None,
        progress: Progress | None = None,
    ) -> int:
        raise RuntimeError("kaputt")

    monkeypatch.setattr(runner_mod, "STAGES", {"a": boom})

    async def scenario() -> tuple[FakeRunner, object]:
        r = FakeRunner()
        await r.start(["a"], {})
        assert r._task is not None
        await r._task
        return r, await r.status()

    r, status = asyncio.run(scenario())
    assert status.state == "failed"
    assert status.error == "kaputt"
    assert status.stages_pending == ["a"]  # resume può ritentare
    assert not r._lock_held  # lock rilasciato anche in caso di errore


def test_stop_on_finished_run_is_rejected() -> None:
    async def scenario() -> None:
        r = FakeRunner()
        r.rows[1] = FakeRow(id=1, state="done", stages=["a"], stages_done={"a": 1})
        with pytest.raises(PipelineError):
            await r.stop()
        assert r.rows[1].state == "done"  # stato terminale non sovrascritto

    asyncio.run(scenario())


# --- routes ---


async def test_pipeline_routes(container: Injector) -> None:
    runner = FakeRunner()
    container.binder.bind(PipelineRunner, to=runner, scope=singleton)
    app = create_app(container, title="test", version="0.0.0")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (await client.get("/v1/pipeline/status")).json()["state"] == "idle"
        assert (await client.post("/v1/pipeline/stop")).status_code == 409
        assert (await client.post("/v1/pipeline/resume")).status_code == 409
        assert (
            await client.post(
                "/v1/pipeline/start",
                json={"stages": ["not-a-stage"]},
            )
        ).status_code == 422
        assert (
            await client.post("/v1/pipeline/start", json={"stages": []})
        ).status_code == 422
