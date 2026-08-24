"""Retry durevoli per unità di pipeline, con backoff esponenziale."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from phd_searcher.pipeline.progress import Progress


class RetryExhaustedError(RuntimeError):
    """L'unità ha esaurito i tentativi consentiti nella run corrente."""


class RetryInterruptedError(RuntimeError):
    """Uno stop è arrivato fra due tentativi; lo stadio deve terminare pulitamente."""


_RATE_LIMIT_MARKERS = ("429", "too many requests")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _retry_after_seconds(exc: Exception) -> float | None:
    """Legge Retry-After (secondi o data HTTP) dalle risposte temporaneamente indisponibili."""
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code not in {429, 503}:
        return None
    value = exc.response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max((retry_at - datetime.now(UTC)).total_seconds(), 0.0)


def _is_rate_limit(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in _RATE_LIMIT_MARKERS)


async def _wait_with_stop(progress: Progress, delay: float) -> None:
    """Attende a tranche: anche un cooldown lungo resta sensibile al pulsante Stop."""
    remaining = max(delay, 0.0)
    while remaining:
        chunk = min(remaining, 30.0)
        await asyncio.sleep(chunk)
        remaining -= chunk
        await progress.check_stop()
        if progress.should_stop:
            raise RetryInterruptedError("stopped during retry cooldown")


async def clear_retry(progress: Progress, key: str) -> None:
    """Rimuove un retry non più utile, per esempio dopo la quarantena di una sorgente."""
    checkpoint = await progress.load_checkpoint()
    raw_retries = checkpoint.get("retries", {})
    retries = dict(raw_retries) if isinstance(raw_retries, dict) else {}
    if key not in retries:
        return
    retries.pop(key, None)
    await progress.save_checkpoint(retries=retries)


async def retry_async[T](
    progress: Progress,
    key: str,
    operation: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    rate_limit_delay: float | None = None,
    non_retryable: tuple[type[Exception], ...] = (),
) -> T:
    """Ritenta e persiste tentativi, prossimo istante ed ultimo errore nel checkpoint."""
    checkpoint = await progress.load_checkpoint()
    raw_retries = checkpoint.get("retries", {})
    retries = dict(raw_retries) if isinstance(raw_retries, dict) else {}
    raw_state = retries.get(key, {})
    state = dict(raw_state) if isinstance(raw_state, dict) else {}
    attempts = int(state.get("attempts", 0))

    if attempts >= max_attempts:
        raise RetryExhaustedError(str(state.get("error") or f"retry exhausted for {key}"))

    next_at = _parse_time(state.get("next_at"))
    if next_at is not None:
        delay = max((next_at - datetime.now(UTC)).total_seconds(), 0.0)
        if delay:
            await _wait_with_stop(progress, min(delay, max_delay))

    while attempts < max_attempts:
        await progress.check_stop()
        if progress.should_stop:
            raise RetryInterruptedError(f"stopped before retrying {key}")
        try:
            result = await operation()
        except Exception as exc:
            if isinstance(exc, non_retryable):
                raise
            attempts += 1
            exponential_delay = base_delay * (2 ** (attempts - 1))
            retry_after = _retry_after_seconds(exc)
            configured_rate_delay = (
                rate_limit_delay
                if rate_limit_delay is not None and _is_rate_limit(exc)
                else 0.0
            )
            delay = min(
                max(exponential_delay, retry_after or 0.0, configured_rate_delay),
                max_delay,
            )
            updated_state: dict[str, object] = {
                "attempts": attempts,
                "error": str(exc)[:1000],
                "next_at": (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(),
            }
            if retry_after is not None:
                updated_state["retry_after_seconds"] = retry_after
            retries[key] = updated_state
            await progress.save_checkpoint(retries=retries)
            if attempts >= max_attempts:
                raise RetryExhaustedError(str(exc)) from exc
            await _wait_with_stop(progress, delay)
        else:
            retries.pop(key, None)
            await progress.save_checkpoint(retries=retries)
            return result
    raise RetryExhaustedError(f"retry exhausted for {key}")
