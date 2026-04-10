import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cli import _run_bot_entrypoint, build_parser


def test_run_parser_accepts_safety_flags():
    parser = build_parser()

    args = parser.parse_args(
        ["run", "--once", "--max-runtime-seconds", "120", "--paper"]
    )

    assert args.command == "run"
    assert args.once is True
    assert args.max_runtime_seconds == 120
    assert args.paper is True


def test_run_parser_accepts_smoke_flag():
    parser = build_parser()

    args = parser.parse_args(["run", "--smoke", "--paper"])

    assert args.command == "run"
    assert args.smoke is True
    assert args.paper is True


@pytest.mark.asyncio
async def test_run_bot_entrypoint_uses_single_cycle_when_requested():
    bot = SimpleNamespace(
        run_single_cycle=AsyncMock(return_value="once-result"),
        run_smoke_test=AsyncMock(return_value="smoke-result"),
        run=AsyncMock(return_value="loop-result"),
        request_shutdown=MagicMock(),
    )

    result = await _run_bot_entrypoint(bot, once=True, max_runtime_seconds=30)

    assert result == "once-result"
    bot.run_single_cycle.assert_awaited_once()
    bot.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_bot_entrypoint_uses_smoke_mode_when_requested():
    bot = SimpleNamespace(
        run_single_cycle=AsyncMock(return_value="once-result"),
        run_smoke_test=AsyncMock(return_value="smoke-result"),
        run=AsyncMock(return_value="loop-result"),
        request_shutdown=MagicMock(),
    )

    result = await _run_bot_entrypoint(bot, smoke=True, max_runtime_seconds=30)

    assert result == "smoke-result"
    bot.run_smoke_test.assert_awaited_once()
    bot.run_single_cycle.assert_not_awaited()
    bot.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_bot_entrypoint_requests_shutdown_on_timeout():
    bot = SimpleNamespace(
        run_single_cycle=AsyncMock(),
        run_smoke_test=AsyncMock(),
        run=AsyncMock(side_effect=lambda: pytest.fail("run should not be awaited directly")),
        request_shutdown=MagicMock(),
    )

    async def slow_cycle():
        await asyncio.sleep(0.05)

    bot.run_single_cycle.side_effect = slow_cycle

    result = await _run_bot_entrypoint(bot, once=True, max_runtime_seconds=0.01)

    assert result is None
    bot.request_shutdown.assert_called_once()
