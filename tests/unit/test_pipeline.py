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
from datetime import datetime
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from injector import Injector, singleton
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from phd_searcher.database.models.pipeline_run import PipelineRun
from phd_searcher.main import create_app
from phd_searcher.pipeline import progress as progress_mod
from phd_searcher.pipeline import runner as runner_mod
from phd_searcher.pipeline.progress import Progress
from phd_searcher.pipeline.runner import PipelineError, PipelineRunner

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
    state: str = "running"
    stages: list[str] = field(default_factory=list)
    stages_done: dict[str, int] = field(default_factory=dict)
    params: dict[str, object] = field(default_factory=dict)
    current_stage: str | None = None
    stage_total: int | None = None
    stage_done: int = 0
    current_label: str | None = None
    stage_elapsed_seconds: float = 0.0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class FakeProgress(Progress):
    """Legge lo stop dalla riga in-memory invece che dal DB."""

    def __init__(self, rows: dict[int, FakeRow], run_id: int) -> None:
        super().__init__(None, run_id)
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


class FakeRunner(PipelineRunner):
    def __init__(self) -> None:
        super().__init__(
            container=cast(Injector, None),  # gli stadi fake non lo usano
            session_maker=cast(async_sessionmaker[AsyncSession], None),
            engine=cast(AsyncEngine, None),
        )
        self.rows: dict[int, FakeRow] = {}
        self._lock_held = False  # advisory lock finto: ownership della run

    def _make_progress(self, run_id: int) -> Progress:
        return FakeProgress(self.rows, run_id)

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
        self.rows[run_id] = FakeRow(id=run_id, stages=list(stages), params=dict(params))
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
        await progress.begin(3)
        await progress.tick("u0")
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
        await r.start(["slow", "later"], {"limit": 5})
        await started.wait()
        await r.stop()  # scrive state='stopping'; reconcile NON tocca la run (lock tenuto)
        stopping = await r.status()
        proceed.set()
        assert r._task is not None
        await r._task
        stopped = await r.status()
        await r.resume()
        pending = list(r.rows[2].stages)
        assert r._task is not None
        await r._task
        return r, stopping, stopped, pending

    r, stopping, stopped, pending = asyncio.run(scenario())
    assert stopping.state == "stopping"
    assert stopped.state == "stopped"
    assert stopped.stages_done == {}  # lo stadio interrotto non è "done"
    assert stopped.stages_pending == ["slow", "later"]
    assert pending == ["slow", "later"]  # resume ripete lo stadio interrotto
    assert r.rows[2].params == {"limit": 5}  # resume riusa i parametri della run originale
    assert r.rows[2].state == "done"


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


def test_runner_reconcile_leaves_live_owner() -> None:
    async def scenario() -> object:
        r = FakeRunner()
        r.rows[1] = FakeRow(id=1, state="running", stages=["scrape"])
        r._lock_held = True  # un worker vivo (magari un altro processo) possiede la run
        return await r.status()

    status = asyncio.run(scenario())
    assert status.state == "running"  # non toccata
    assert status.error is None


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


def test_pipeline_routes(container: Injector) -> None:
    runner = FakeRunner()
    container.binder.bind(PipelineRunner, to=runner, scope=singleton)
    with TestClient(create_app(container, title="test", version="0.0.0")) as client:
        assert client.get("/v1/pipeline/status").json()["state"] == "idle"
        assert client.post("/v1/pipeline/stop").status_code == 409
        assert client.post("/v1/pipeline/resume").status_code == 409
        assert (
            client.post("/v1/pipeline/start", json={"stages": ["not-a-stage"]}).status_code == 422
        )  # Literal valida i nomi stadio
        assert (
            client.post("/v1/pipeline/start", json={"stages": []}).status_code == 422
        )  # lista vuota rifiutata (run no-op seppellirebbe l'ultima riprendibile)
