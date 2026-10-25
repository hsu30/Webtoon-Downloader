"""Bounded retries around complete operations, including response bodies."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Literal, TypeVar

import httpx

T = TypeVar("T")
log = logging.getLogger(__name__)
RetryStrategy = Literal["exponential", "linear", "fixed"]


class ImageContentError(ValueError):
    """A fully received response cannot be decoded as a complete image."""


class ResourceLimitError(ValueError):
    """A configured size, space or pixel budget was exceeded."""


class PageOperationError(Exception):
    def __init__(self, position: int, attempts: int, cause: Exception) -> None:
        self.position = position
        self.attempts = attempts
        self.retry_cause = cause
        super().__init__(f"page={position}; attempts={attempts}; {type(cause).__name__}")


@dataclass(frozen=True)
class RetryPolicy:
    strategy: RetryStrategy | None = "exponential"
    delay: float = 1.0
    max_delay: float = 30.0
    operation_budget: float = 300.0

    def wait(self, retry_number: int) -> float:
        multiplier = {"fixed": 1, "linear": retry_number, "exponential": 2 ** min(retry_number - 1, 30)}
        value = min(self.max_delay, self.delay * multiplier.get(self.strategy or "fixed", 1))
        return float(value * random.uniform(0.8, 1.0))


def retryable(error: Exception) -> bool:
    if isinstance(error, PageOperationError):
        return retryable(error.retry_cause)
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {408, 429, 500, 502, 503, 504}
    if isinstance(error, httpx.LocalProtocolError):
        return "InvalidBodyLengthError" in str(error) or "body length" in str(error).lower()
    return isinstance(
        error, httpx.TimeoutException | httpx.NetworkError | httpx.RemoteProtocolError | ImageContentError
    )


def retry_after(error: Exception) -> float:
    if not isinstance(error, httpx.HTTPStatusError):
        return 0.0
    value = error.response.headers.get("Retry-After", "")
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except ValueError:
        try:
            return float(max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 0.0


class RetryController:
    def __init__(self, policy: RetryPolicy) -> None:
        self.policy = policy
        self.cooldown_until = 0.0

    async def run(
        self,
        operation: Callable[[], Awaitable[T]],
        retries: int,
        *,
        ledger: dict[str, int] | None = None,
        key: str = "",
    ) -> T:
        attempts = ledger if ledger is not None else {}
        limit = 1 + (retries if self.policy.strategy else 0)
        deadline = time.monotonic() + self.policy.operation_budget
        if attempts.get(key, 0) >= limit:
            raise ImageContentError("page_retry_budget_exhausted")
        while True:
            remaining = deadline - time.monotonic()
            cooldown = max(0.0, self.cooldown_until - time.monotonic())
            if cooldown >= remaining:
                raise TimeoutError("retry_time_budget_exhausted")
            await asyncio.sleep(cooldown)
            attempts[key] = attempts.get(key, 0) + 1
            try:
                return await asyncio.wait_for(operation(), max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError as error:
                raise TimeoutError("request_time_budget_exhausted") from error
            except Exception as error:
                server_wait = retry_after(error)
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 429:
                    self.cooldown_until = max(
                        self.cooldown_until, time.monotonic() + max(server_wait, self.policy.delay)
                    )
                if attempts[key] >= limit or not retryable(error):
                    raise
                delay = max(self.policy.wait(attempts[key]), server_wait)
                if delay >= deadline - time.monotonic():
                    raise
                log.info("Retry %s/%s after %s; waiting %.2fs", attempts[key], limit - 1, type(error).__name__, delay)
                await asyncio.sleep(delay)


async def blocking(function: Callable[..., T], *args: object) -> T:
    """Drain a started worker before releasing files/locks on cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
