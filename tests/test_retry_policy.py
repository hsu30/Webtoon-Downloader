from __future__ import annotations

import asyncio
import math

import httpx
import pytest

from webtoon_downloader.core.retry import RetryController, RetryPolicy, retry_after


@pytest.mark.parametrize("strategy,bounds", [("fixed", (0.8, 1)), ("linear", (2.4, 3)), ("exponential", (3.2, 4))])
def test_strategies_have_the_named_waiting_behavior(strategy: str, bounds: tuple[float, float]) -> None:
    value = RetryPolicy(strategy=strategy).wait(3)  # type: ignore[arg-type]
    assert bounds[0] <= value <= bounds[1]
    assert RetryPolicy(strategy=strategy, max_delay=2).wait(10) <= 2  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_retry_after_larger_than_operation_budget_stops_without_an_early_request() -> None:
    attempts = 0
    request = httpx.Request("GET", "https://example.invalid")
    response = httpx.Response(429, request=request, headers={"Retry-After": "3600"})

    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        response.raise_for_status()

    controller = RetryController(RetryPolicy(operation_budget=0.1))
    with pytest.raises(httpx.HTTPStatusError):
        await controller.run(operation, retries=3)
    assert attempts == 1
    assert controller.cooldown_until > 0


@pytest.mark.asyncio
async def test_whole_operation_budget_covers_a_body_that_never_finishes() -> None:
    async def slow() -> None:
        await asyncio.sleep(10)

    controller = RetryController(RetryPolicy(operation_budget=0.01))
    with pytest.raises(TimeoutError):
        await controller.run(slow, retries=3)


@pytest.mark.parametrize("value", ["inf", "nan", "-5", "invalid"])
def test_invalid_retry_after_cannot_create_an_infinite_sleep(value: str) -> None:
    request = httpx.Request("GET", "https://example.invalid")
    response = httpx.Response(429, request=request, headers={"Retry-After": value})
    error = httpx.HTTPStatusError("fixture", request=request, response=response)
    assert math.isfinite(retry_after(error))
    assert retry_after(error) >= 0
