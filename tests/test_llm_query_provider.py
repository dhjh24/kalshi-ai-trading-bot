import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from src.clients.codex_client import CodexClient
from src.clients.openai_client import OpenAIClient
from src.clients.openrouter_client import OpenRouterClient
from src.utils.database import DatabaseManager, LLMQuery


pytestmark = pytest.mark.asyncio


def _make_openai_client(db_manager) -> OpenAIClient:
    async_client = MagicMock()
    with patch("src.clients.openai_client.AsyncOpenAI", return_value=async_client):
        with patch("src.clients.openai_client.settings") as mock_settings:
            mock_settings.api.openai_api_key = "test-key"
            mock_settings.api.openai_base_url = "https://api.openai.com/v1"
            mock_settings.trading.primary_model = "openai/gpt-5.4"
            mock_settings.trading.fallback_model = "openai/o3"
            mock_settings.trading.ai_temperature = 0
            mock_settings.trading.ai_max_tokens = 8000
            mock_settings.trading.daily_ai_cost_limit = 50.0
            return OpenAIClient(db_manager=db_manager)


def _make_openrouter_client(db_manager) -> OpenRouterClient:
    async_client = MagicMock()
    with patch("src.clients.openrouter_client.AsyncOpenAI", return_value=async_client):
        with patch("src.clients.openrouter_client.settings") as mock_settings:
            mock_settings.api.openrouter_api_key = "test-key"
            mock_settings.api.openrouter_base_url = "https://openrouter.ai/api/v1"
            mock_settings.api.get_openrouter_headers.return_value = {}
            mock_settings.trading.ai_temperature = 0
            mock_settings.trading.ai_max_tokens = 8000
            mock_settings.trading.daily_ai_cost_limit = 50.0
            return OpenRouterClient(db_manager=db_manager)


def _make_codex_client(db_manager) -> CodexClient:
    with patch("src.clients.codex_client.settings") as mock_settings:
        mock_settings.trading.primary_model = "codex/gpt-5-codex"
        mock_settings.trading.fallback_model = "codex/gpt-5.4-codex"
        mock_settings.trading.ai_temperature = 0
        mock_settings.trading.ai_max_tokens = 8000
        mock_settings.trading.daily_ai_cost_limit = 10.0
        return CodexClient(cli_path="/usr/local/bin/codex", db_manager=db_manager)


async def test_legacy_llm_queries_migrate_provider_column_and_preserve_rows(
    tmp_path: Path,
):
    db_path = tmp_path / "legacy_llm_queries.db"
    legacy_timestamp = (datetime.now() - timedelta(hours=1)).isoformat()

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE llm_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                strategy TEXT NOT NULL,
                query_type TEXT NOT NULL,
                market_id TEXT,
                prompt TEXT NOT NULL,
                response TEXT NOT NULL,
                tokens_used INTEGER,
                cost_usd REAL,
                confidence_extracted REAL,
                decision_extracted TEXT
            )
            """
        )
        await db.execute(
            """
            INSERT INTO llm_queries (
                timestamp, strategy, query_type, market_id, prompt, response,
                tokens_used, cost_usd, confidence_extracted, decision_extracted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                legacy_timestamp,
                "legacy_strategy",
                "completion",
                "LEGACY-1",
                "legacy prompt",
                "legacy response",
                42,
                0.12,
                0.75,
                "BUY",
            ),
        )
        await db.commit()

    manager = DatabaseManager(db_path=str(db_path))
    await manager.initialize()

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("PRAGMA table_info(llm_queries)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "provider" in columns
        assert "role" in columns

    queries = await manager.get_llm_queries(hours_back=48, limit=10)
    assert len(queries) == 1
    assert queries[0].strategy == "legacy_strategy"
    assert queries[0].provider is None
    assert queries[0].role is None

    await manager.log_llm_query(
        LLMQuery(
            timestamp=datetime.now(),
            strategy="unit_test",
            query_type="completion",
            role="unit_role",
            market_id="NEW-1",
            prompt="fresh prompt",
            response="fresh response",
            provider="openai",
            tokens_used=12,
            cost_usd=0.34,
        )
    )

    queries = await manager.get_llm_queries(hours_back=48, limit=10)
    assert any(query.provider == "openai" for query in queries)
    assert any(query.role == "unit_role" for query in queries)


async def test_ai_spend_provider_breakdown_uses_recent_window(tmp_path: Path):
    db_path = tmp_path / "llm-query-provider.db"
    manager = DatabaseManager(db_path=str(db_path))
    await manager.initialize()
    await manager.upsert_daily_cost(0.33)

    recent_timestamp = datetime.now() - timedelta(days=1)
    old_timestamp = datetime.now() - timedelta(days=10)

    await manager.log_llm_query(
        LLMQuery(
            timestamp=recent_timestamp,
            strategy="quick_flip",
            query_type="completion",
            market_id="RECENT-1",
            prompt="recent prompt",
            response="recent response",
            provider="openai",
            tokens_used=100,
            cost_usd=0.20,
        )
    )
    await manager.log_llm_query(
        LLMQuery(
            timestamp=old_timestamp,
            strategy="quick_flip",
            query_type="completion",
            market_id="OLD-1",
            prompt="old prompt",
            response="old response",
            provider="openrouter",
            tokens_used=500,
            cost_usd=9.99,
        )
    )

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE analysis_requests (
                request_id TEXT PRIMARY KEY,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                completed_at TEXT,
                provider TEXT,
                model TEXT,
                cost_usd REAL,
                sources_json TEXT,
                response_json TEXT,
                context_json TEXT,
                error TEXT
            )
            """
        )
        await db.execute(
            """
            INSERT INTO analysis_requests (
                request_id, target_type, target_id, status, requested_at,
                completed_at, provider, model, cost_usd, sources_json,
                response_json, context_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "recent-analysis",
                "market",
                "RECENT-ANALYSIS",
                "completed",
                recent_timestamp.isoformat(),
                recent_timestamp.isoformat(),
                "codex",
                "gpt-5-codex",
                0.10,
                "{}",
                "{}",
                "{}",
                None,
            ),
        )
        await db.execute(
            """
            INSERT INTO analysis_requests (
                request_id, target_type, target_id, status, requested_at,
                completed_at, provider, model, cost_usd, sources_json,
                response_json, context_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old-analysis",
                "market",
                "OLD-ANALYSIS",
                "completed",
                old_timestamp.isoformat(),
                old_timestamp.isoformat(),
                "anthropic",
                "claude",
                4.00,
                "{}",
                "{}",
                "{}",
                None,
            ),
        )
        await db.commit()

    summary = (await manager.get_ai_spend_provider_breakdown())["summary"]

    assert "today $0.33" in summary
    assert "7d providers" in summary
    assert "openai $0.20" in summary
    assert "codex $0.10" in summary
    assert "openrouter" not in summary
    assert "anthropic" not in summary
    assert "7d logged $0.20 across 1 queries" in summary


async def test_legacy_analysis_requests_migrates_provider_column_and_still_breaks_down(
    tmp_path: Path,
):
    db_path = tmp_path / "legacy-analysis-requests.db"
    legacy_timestamp = (datetime.now() - timedelta(hours=1)).isoformat()

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE analysis_requests (
                request_id TEXT PRIMARY KEY,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                completed_at TEXT,
                model TEXT,
                cost_usd REAL,
                sources_json TEXT,
                response_json TEXT,
                context_json TEXT,
                error TEXT
            )
            """
        )
        await db.execute(
            """
            INSERT INTO analysis_requests (
                request_id, target_type, target_id, status, requested_at,
                completed_at, model, cost_usd, sources_json, response_json,
                context_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-analysis-1",
                "market",
                "MKT-1",
                "completed",
                legacy_timestamp,
                legacy_timestamp,
                "gpt-5-codex",
                0.05,
                "{}",
                "{}",
                "{}",
                None,
            ),
        )
        await db.commit()

    manager = DatabaseManager(db_path=str(db_path))
    await manager.initialize()

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("PRAGMA table_info(analysis_requests)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "provider" in columns

        await db.execute(
            """
            INSERT INTO analysis_requests (
                request_id, target_type, target_id, status, requested_at,
                completed_at, provider, model, cost_usd, sources_json,
                response_json, context_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "migrated-analysis-1",
                "market",
                "MKT-2",
                "completed",
                legacy_timestamp,
                legacy_timestamp,
                "openai",
                "gpt-5-mini",
                0.10,
                "{}",
                "{}",
                "{}",
                None,
            ),
        )
        await db.commit()

    summary = (await manager.get_ai_spend_provider_breakdown())["summary"]
    assert "openai $0.10" in summary
    assert "7d providers" in summary


async def test_ai_spend_provider_breakdown_counts_codex_quota_from_llm_queries_only(
    tmp_path: Path,
):
    db_path = tmp_path / "codex-quota-window.db"
    manager = DatabaseManager(db_path=str(db_path))
    await manager.initialize()
    now = datetime.now()

    await manager.log_llm_query(
        LLMQuery(
            timestamp=now,
            strategy="quick_flip",
            query_type="completion",
            market_id="COD-1",
            prompt="prompt",
            response="response",
            provider="codex",
            tokens_used=321,
            cost_usd=0.0,
        )
    )

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE analysis_requests (
                request_id TEXT PRIMARY KEY,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                completed_at TEXT,
                provider TEXT,
                model TEXT,
                cost_usd REAL,
                sources_json TEXT,
                response_json TEXT,
                context_json TEXT,
                error TEXT
            )
            """
        )
        await db.execute(
            """
            INSERT INTO analysis_requests (
                request_id, target_type, target_id, status, requested_at,
                completed_at, provider, model, cost_usd, sources_json,
                response_json, context_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "codex-analysis",
                "market",
                "COD-ANALYSIS",
                "completed",
                now.isoformat(),
                now.isoformat(),
                "codex",
                "codex/gpt-5-codex",
                0.0,
                "{}",
                "{}",
                "{}",
                None,
            ),
        )
        await db.commit()

    summary = (await manager.get_ai_spend_provider_breakdown())["summary"]

    assert "codex $0.00 (1 req, 321 tok)" in summary


@pytest.mark.parametrize(
    ("factory", "expected_provider"),
    [
        (_make_openai_client, "openai"),
        (_make_openrouter_client, "openrouter"),
        (_make_codex_client, "codex"),
    ],
)
async def test_client_loggers_tag_llm_queries_with_provider(factory, expected_provider):
    db_manager = MagicMock()
    db_manager.log_llm_query = AsyncMock()
    client = factory(db_manager)

    await client._log_query(
        strategy="unit_test",
        query_type="completion",
        prompt="hello",
        response="world",
        market_id="TEST-1",
        tokens_used=11,
        cost_usd=0.22,
    )
    await asyncio.sleep(0)

    logged = db_manager.log_llm_query.await_args.args[0]
    assert logged.provider == expected_provider
    assert logged.market_id == "TEST-1"
    assert logged.strategy == "unit_test"


@pytest.mark.parametrize(
    "factory",
    [_make_openai_client, _make_openrouter_client, _make_codex_client],
)
async def test_client_loggers_tag_llm_queries_with_role_fallback(factory):
    db_manager = MagicMock()
    db_manager.log_llm_query = AsyncMock()
    client = factory(db_manager)

    await client._log_query(
        strategy="unit_test",
        query_type="completion",
        prompt="hello",
        response="world",
        market_id="TEST-1",
        tokens_used=11,
        cost_usd=0.22,
    )
    await asyncio.sleep(0)

    logged = db_manager.log_llm_query.await_args.args[0]
    assert logged.role == "completion"


async def test_client_loggers_respect_explicit_role_override():
    db_manager = MagicMock()
    db_manager.log_llm_query = AsyncMock()
    client = _make_openai_client(db_manager)

    await client._log_query(
        strategy="unit_test",
        query_type="completion",
        role="trade_agent",
        prompt="hello",
        response="world",
        market_id="TEST-1",
        tokens_used=11,
        cost_usd=0.22,
    )
    await asyncio.sleep(0)

    logged = db_manager.log_llm_query.await_args.args[0]
    assert logged.role == "trade_agent"
