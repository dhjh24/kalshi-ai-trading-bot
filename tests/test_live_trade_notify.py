"""Focused tests for the W9 push-based dashboard refresh hook.

These tests verify that `LiveTradeRefreshNotifier` and its integration with
`LiveTradeDecisionLoop._persist_runtime_state` work as designed:

1. When `LIVE_TRADE_NOTIFY_URL` and `LIVE_TRADE_INTERNAL_REFRESH_TOKEN` are
   both set, every persisted runtime-state change fires exactly one HTTP POST
   to the configured URL with the secret in the `x-internal-token` header.
2. When the env vars are unset, the notifier is silently disabled (no POSTs).
3. Network failures, timeouts, and HTTP 5xx responses MUST NOT propagate out
   of the loop -- the caller would crash the trading cycle if they did.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx
import pytest

from src.jobs.live_trade import (
    LiveTradeRefreshNotifier,
    _resolve_notify_token,
    _resolve_notify_url,
)


class _FakeResponse:
    def __init__(self, status_code: int = 204) -> None:
        self.status_code = status_code


class _RecordingClient:
    """Mocked httpx.AsyncClient that records every POST and returns a stub."""

    def __init__(
        self,
        *,
        timeout: float,
        responses: Optional[List[Any]] = None,
    ) -> None:
        self.timeout = timeout
        self.calls: List[Dict[str, Any]] = []
        self._responses: List[Any] = list(responses or [])

    async def post(
        self,
        url: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": dict(headers or {})})
        if self._responses:
            entry = self._responses.pop(0)
            if isinstance(entry, BaseException):
                raise entry
            return entry
        return _FakeResponse(status_code=204)

    async def aclose(self) -> None:
        return None


def _make_factory(client: _RecordingClient):
    def _factory(*, timeout: float) -> _RecordingClient:
        client.timeout = timeout
        return client

    return _factory


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Default to a known disabled state so tests opt-in explicitly."""
    monkeypatch.delenv("LIVE_TRADE_NOTIFY_URL", raising=False)
    monkeypatch.delenv("LIVE_TRADE_INTERNAL_REFRESH_TOKEN", raising=False)
    yield


def test_resolvers_return_none_when_env_missing():
    """Both helpers must return `None` so callers can short-circuit cleanly."""
    assert _resolve_notify_url() is None
    assert _resolve_notify_token() is None


def test_notifier_is_disabled_without_url_or_token(monkeypatch):
    """Constructing without env vars must produce a disabled notifier."""
    notifier = LiveTradeRefreshNotifier()
    assert notifier.enabled is False
    # `notify` must still be safe to await -- it just becomes a no-op.
    asyncio.run(notifier.notify("live-trade-decisions"))


def test_notifier_posts_token_and_topic_when_enabled():
    """Enabled notifier must POST exactly once per `notify` call with the
    configured shared-secret header and the requested topic."""
    client = _RecordingClient(timeout=0.5)
    notifier = LiveTradeRefreshNotifier(
        url="http://127.0.0.1:4000/internal/live-trade/notify-refresh",
        token="t-secret",
        client_factory=_make_factory(client),
    )

    assert notifier.enabled is True

    result = asyncio.run(notifier.notify("live-trade-decisions"))
    assert result is True
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"].endswith("/internal/live-trade/notify-refresh")
    assert call["json"] == {"topic": "live-trade-decisions"}
    assert call["headers"].get("x-internal-token") == "t-secret"


def test_notifier_normalizes_unknown_topics():
    """Unknown topic strings collapse to the canonical decision-feed topic so
    the Node side never sees a value its zod schema rejects."""
    client = _RecordingClient(timeout=0.5)
    notifier = LiveTradeRefreshNotifier(
        url="http://127.0.0.1:4000/internal/live-trade/notify-refresh",
        token="t-secret",
        client_factory=_make_factory(client),
    )

    asyncio.run(notifier.notify("not-a-real-topic"))
    assert client.calls[0]["json"] == {"topic": "live-trade-decisions"}


def test_notifier_swallows_network_errors():
    """A transport failure must never raise out of `notify`."""
    client = _RecordingClient(
        timeout=0.5,
        responses=[httpx.ConnectError("boom: nothing on the other side")],
    )
    notifier = LiveTradeRefreshNotifier(
        url="http://127.0.0.1:4000/internal/live-trade/notify-refresh",
        token="t-secret",
        client_factory=_make_factory(client),
    )

    result = asyncio.run(notifier.notify("live-trade-decisions"))
    assert result is False  # but no exception raised


def test_notifier_treats_4xx_5xx_as_soft_failures():
    """HTTP errors return False but do not raise."""
    client = _RecordingClient(
        timeout=0.5,
        responses=[_FakeResponse(status_code=500)],
    )
    notifier = LiveTradeRefreshNotifier(
        url="http://127.0.0.1:4000/internal/live-trade/notify-refresh",
        token="t-secret",
        client_factory=_make_factory(client),
    )

    result = asyncio.run(notifier.notify("live-trade-decisions"))
    assert result is False


def test_notifier_de_duplicates_consecutive_warnings():
    """Repeated identical failures should not flood the trade logs."""

    class _Capturing:
        def __init__(self) -> None:
            self.warnings: List[str] = []

        def warning(self, message: str) -> None:
            self.warnings.append(message)

    capturing = _Capturing()
    client = _RecordingClient(
        timeout=0.5,
        responses=[
            httpx.ConnectError("kaboom"),
            httpx.ConnectError("kaboom"),
            httpx.ConnectError("kaboom"),
        ],
    )
    notifier = LiveTradeRefreshNotifier(
        url="http://127.0.0.1:4000/internal/live-trade/notify-refresh",
        token="t-secret",
        client_factory=_make_factory(client),
        logger=capturing,
    )

    for _ in range(3):
        asyncio.run(notifier.notify("live-trade-decisions"))

    # Only the first identical warning is logged; the next two are deduped.
    assert len(capturing.warnings) == 1


class _StubDbManager:
    def __init__(self) -> None:
        self.upsert_calls: List[Any] = []

    async def upsert_live_trade_runtime_state(self, state: Any) -> None:
        self.upsert_calls.append(state)


class _StubLoop:
    """Minimal stand-in that re-uses the real `_persist_runtime_state` body
    via attribute access. We can't easily instantiate `LiveTradeDecisionLoop`
    here without a real DB, so we test the integration via the notifier's
    public surface and a direct call path below."""

    pass


def test_persist_runtime_state_fires_notify_once_per_call(monkeypatch):
    """Hooking the notifier into `_persist_runtime_state` must dispatch
    exactly one HTTP call per persisted state change."""
    from datetime import datetime, timezone

    from src.jobs.live_trade import LiveTradeDecisionLoop
    from src.utils.database import LiveTradeRuntimeState

    client = _RecordingClient(timeout=0.5)
    notifier = LiveTradeRefreshNotifier(
        url="http://127.0.0.1:4000/internal/live-trade/notify-refresh",
        token="t-secret",
        client_factory=_make_factory(client),
    )

    # Skip __init__ entirely so we don't need a Kalshi/router/DB stack just
    # to drive `_persist_runtime_state`.
    loop = LiveTradeDecisionLoop.__new__(LiveTradeDecisionLoop)
    loop.refresh_notifier = notifier
    loop._refresh_notify_tasks = set()
    loop.db_manager = _StubDbManager()
    loop._runtime_state = LiveTradeRuntimeState(
        heartbeat_at=datetime.now(timezone.utc).isoformat(),
        runtime_mode="paper",
        exchange_env="demo",
    )
    loop._resolve_runtime_mode = lambda: "paper"  # type: ignore[assignment]
    loop._resolve_exchange_env = lambda: "demo"  # type: ignore[assignment]

    async def _persist_and_drain(**kwargs):
        await loop._persist_runtime_state(**kwargs)
        if loop._refresh_notify_tasks:
            await asyncio.gather(*list(loop._refresh_notify_tasks))

    asyncio.run(
        _persist_and_drain(
            run_id="run-1",
            loop_status="running",
            step="execution",
            step_status="completed",
            summary="ok",
            healthy=True,
        )
    )

    assert len(loop.db_manager.upsert_calls) == 1
    assert len(client.calls) == 1
    assert client.calls[0]["json"] == {"topic": "runtime-state"}

    # A second call should fire a second notification (one per state write).
    asyncio.run(
        _persist_and_drain(
            run_id="run-1",
            loop_status="completed",
            step="execution",
            step_status="completed",
            completed=True,
        )
    )
    assert len(client.calls) == 2


def test_persist_runtime_state_swallows_notify_failures(monkeypatch):
    """A throwing notifier must NOT break the trading loop."""
    from datetime import datetime, timezone

    from src.jobs.live_trade import LiveTradeDecisionLoop
    from src.utils.database import LiveTradeRuntimeState

    class _ExplodingNotifier(LiveTradeRefreshNotifier):
        def __init__(self) -> None:
            super().__init__(url="http://x", token="y")

        @property
        def enabled(self) -> bool:  # type: ignore[override]
            return True

        async def notify(self, topic: str = "live-trade-decisions") -> bool:  # type: ignore[override]
            raise RuntimeError("notifier crashed")

    loop = LiveTradeDecisionLoop.__new__(LiveTradeDecisionLoop)
    loop.refresh_notifier = _ExplodingNotifier()
    loop._refresh_notify_tasks = set()
    loop.db_manager = _StubDbManager()
    loop._runtime_state = LiveTradeRuntimeState(
        heartbeat_at=datetime.now(timezone.utc).isoformat(),
        runtime_mode="paper",
        exchange_env="demo",
    )
    loop._resolve_runtime_mode = lambda: "paper"  # type: ignore[assignment]
    loop._resolve_exchange_env = lambda: "demo"  # type: ignore[assignment]

    # Must complete without raising.
    async def _persist_and_drain() -> None:
        await loop._persist_runtime_state(
            run_id="run-1",
            loop_status="running",
            step="execution",
            step_status="completed",
        )
        if loop._refresh_notify_tasks:
            await asyncio.gather(*list(loop._refresh_notify_tasks), return_exceptions=True)

    asyncio.run(_persist_and_drain())
    assert len(loop.db_manager.upsert_calls) == 1


def test_persist_runtime_state_does_not_wait_for_slow_notify():
    """A configured but slow Node endpoint must not slow state persistence."""
    from datetime import datetime, timezone

    from src.jobs.live_trade import LiveTradeDecisionLoop
    from src.utils.database import LiveTradeRuntimeState

    class _SlowNotifier(LiveTradeRefreshNotifier):
        def __init__(self) -> None:
            super().__init__(url="http://x", token="y")
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        @property
        def enabled(self) -> bool:  # type: ignore[override]
            return True

        async def notify(self, topic: str = "live-trade-decisions") -> bool:  # type: ignore[override]
            self.started.set()
            await self.release.wait()
            return True

    async def _run() -> None:
        notifier = _SlowNotifier()
        loop = LiveTradeDecisionLoop.__new__(LiveTradeDecisionLoop)
        loop.refresh_notifier = notifier
        loop._refresh_notify_tasks = set()
        loop.db_manager = _StubDbManager()
        loop._runtime_state = LiveTradeRuntimeState(
            heartbeat_at=datetime.now(timezone.utc).isoformat(),
            runtime_mode="paper",
            exchange_env="demo",
        )
        loop._resolve_runtime_mode = lambda: "paper"  # type: ignore[assignment]
        loop._resolve_exchange_env = lambda: "demo"  # type: ignore[assignment]

        await asyncio.wait_for(
            loop._persist_runtime_state(
                run_id="run-1",
                loop_status="running",
                step="execution",
                step_status="completed",
            ),
            timeout=0.05,
        )
        assert len(loop.db_manager.upsert_calls) == 1
        assert loop._refresh_notify_tasks

        await asyncio.wait_for(notifier.started.wait(), timeout=0.05)
        notifier.release.set()
        await asyncio.gather(*list(loop._refresh_notify_tasks))

    asyncio.run(_run())
