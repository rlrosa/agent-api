import os
import pytest
from app.config import get_settings
from app.ratelimit import is_rate_limit_error, rate_limit_manager


def test_rate_limit_classifier():
    assert is_rate_limit_error(error="HTTP 429 Too Many Requests")
    assert is_rate_limit_error(stderr="Error: rate limit exceeded for model")
    assert is_rate_limit_error(stdout="QuotaFailure: RESOURCE_EXHAUSTED")
    assert is_rate_limit_error(exit_code=429)
    assert not is_rate_limit_error(stdout="All good", stderr="", exit_code=0)


@pytest.mark.asyncio
async def test_aimd_halving_and_recovery():
    rate_limit_manager.reset()
    assert rate_limit_manager.effective_concurrency == 3

    # Handle rate limit (should halve 3 -> 1)
    await rate_limit_manager.handle_rate_limit(attempts=1)
    assert rate_limit_manager.effective_concurrency == 1
    assert rate_limit_manager.consecutive_successes == 0

    # 3 consecutive successes should recover 1 -> 2 -> 3
    await rate_limit_manager.handle_success()
    await rate_limit_manager.handle_success()
    assert rate_limit_manager.effective_concurrency == 1

    await rate_limit_manager.handle_success()
    assert rate_limit_manager.effective_concurrency == 2
