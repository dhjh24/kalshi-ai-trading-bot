import pytest
import pickle
from datetime import datetime
from types import SimpleNamespace

from src.clients.shared_types import DailyUsageTracker, TradingDecision
from src.clients.xai_client import (
    DailyUsageTracker as LegacyDailyUsageTracker,
    TradingDecision as LegacyTradingDecision,
    XAIClient,
)
from src.config.settings import settings


@pytest.mark.asyncio
async def test_xai_client_routes_to_codex_provider(monkeypatch):
    created = {}

    class FakeCodexClient:
        def __init__(self, db_manager=None):
            created["db_manager"] = db_manager

        async def close(self):
            return None

    monkeypatch.setattr(settings.api, "resolve_llm_provider", lambda: "codex")

    import src.clients.codex_client as codex_client_module

    monkeypatch.setattr(codex_client_module, "CodexClient", FakeCodexClient)

    client = XAIClient(db_manager="db-handle")
    provider_client = client._get_provider_client()

    assert isinstance(provider_client, FakeCodexClient)
    assert created["db_manager"] == "db-handle"

    await client.close()


def test_xai_client_reexports_shared_types():
    assert LegacyTradingDecision is TradingDecision
    assert LegacyDailyUsageTracker is DailyUsageTracker

    decision = TradingDecision(action="BUY", side="YES", confidence=0.8, limit_price=55)
    tracker = DailyUsageTracker(date="2026-04-24", request_count=3)

    assert decision.action == "BUY"
    assert tracker.request_count == 3


@pytest.mark.asyncio
async def test_xai_client_mirror_provider_usage_updates_tracker(monkeypatch):
    class FakeProviderClient:
        def __init__(self):
            self.last_request_metadata = SimpleNamespace(
                cost=2.5,
            )

        async def get_completion(self, **kwargs):
            return "ok"

        async def close(self):
            return None

    client = XAIClient()

    tracker = DailyUsageTracker(date=datetime.now().strftime("%Y-%m-%d"), request_count=4)
    monkeypatch.setattr(client, "_load_daily_tracker", lambda: tracker)
    monkeypatch.setattr(client, "_get_provider_client", lambda: FakeProviderClient())

    async def _check_daily_limits_true() -> bool:
        return True

    monkeypatch.setattr(client, "_check_daily_limits", _check_daily_limits_true)

    save_calls = {"count": 0}

    def _save():
        save_calls["count"] += 1

    monkeypatch.setattr(client, "_save_daily_tracker", _save)
    result = await client.get_completion(
        "mirror usage",
        strategy="test",
        query_type="completion",
    )

    assert result == "ok"
    assert client.total_cost == 2.5
    assert client.request_count == 1
    assert tracker.request_count == 5
    assert tracker.total_cost == 2.5
    assert save_calls["count"] >= 1


@pytest.mark.asyncio
async def test_xai_client_mirror_provider_usage_persists_when_provider_does_not_track(monkeypatch, tmp_path):
    usage_file = tmp_path / "daily_ai_usage.pkl"
    today = datetime.now().strftime("%Y-%m-%d")
    with open(usage_file, "wb") as f:
        pickle.dump(
            DailyUsageTracker(date=today, request_count=2, total_cost=1.0),
            f,
        )

    class FakeOpenAIClient:
        def __init__(self, db_manager=None):
            self.last_request_metadata = SimpleNamespace(cost=0.75)

        async def get_completion(self, **kwargs):
            return "ok"

        async def close(self):
            return None

    import src.clients.openai_client as openai_client_module

    monkeypatch.setattr(settings.api, "resolve_llm_provider", lambda: "openai")
    monkeypatch.setattr(openai_client_module, "OpenAIClient", FakeOpenAIClient)

    client = XAIClient()
    client.usage_file = str(usage_file)
    with open(usage_file, "rb") as f:
        client.daily_tracker = pickle.load(f)

    result = await client.get_completion("mirror usage", strategy="test", query_type="completion")
    assert result == "ok"

    with open(usage_file, "rb") as f:
        tracked = pickle.load(f)
    assert tracked.request_count == 3
    assert tracked.total_cost == pytest.approx(1.75)

    reloaded = XAIClient()
    reloaded.usage_file = str(usage_file)
    assert reloaded._load_daily_tracker().request_count == 3
    assert reloaded._load_daily_tracker().total_cost == pytest.approx(1.75)


@pytest.mark.asyncio
async def test_xai_client_mirror_provider_usage_does_not_double_count_when_provider_tracks(monkeypatch, tmp_path):
    usage_file = tmp_path / "daily_ai_usage.pkl"
    today = datetime.now().strftime("%Y-%m-%d")
    with open(usage_file, "wb") as f:
        pickle.dump(
            DailyUsageTracker(date=today, request_count=2, total_cost=1.0),
            f,
        )

    class FakeOpenAIClient:
        def __init__(self, db_manager=None):
            self.last_request_metadata = SimpleNamespace(cost=0.75)

        async def get_completion(self, **kwargs):
            with open(usage_file, "rb") as f:
                tracker = pickle.load(f)
            tracker.request_count += 1
            tracker.total_cost += 0.75
            with open(usage_file, "wb") as f:
                pickle.dump(tracker, f)
            return "ok"

        async def close(self):
            return None

    import src.clients.openai_client as openai_client_module

    monkeypatch.setattr(settings.api, "resolve_llm_provider", lambda: "openai")
    monkeypatch.setattr(openai_client_module, "OpenAIClient", FakeOpenAIClient)

    client = XAIClient()
    client.usage_file = str(usage_file)
    with open(usage_file, "rb") as f:
        client.daily_tracker = pickle.load(f)

    result = await client.get_completion("mirror usage", strategy="test", query_type="completion")
    assert result == "ok"

    with open(usage_file, "rb") as f:
        tracked = pickle.load(f)
    assert tracked.request_count == 3
    assert tracked.total_cost == pytest.approx(1.75)
