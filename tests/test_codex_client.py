"""
Tests for the Codex CLI subprocess client.

These tests mock :func:`asyncio.create_subprocess_exec` and never spawn a real
subprocess, so they pass on machines that do not have the Codex CLI
installed (required by the W1 plan).
"""

from __future__ import annotations

import json
import io
import pickle
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients import codex_client as codex_module
from src.clients.shared_types import DailyUsageTracker
from src.clients.codex_client import (
    CODEX_FALLBACK_ORDER,
    CodexClient,
    CodexUnavailableError,
    TRADING_DECISION_JSON_SCHEMA,
    _build_fallback_chain,
    _canonical_codex_model,
    _messages_to_prompt,
    _parse_trading_decision,
    clear_codex_auth_cache,
    is_codex_authenticated,
    resolve_codex_cli_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Minimal stand-in for ``asyncio.subprocess.Process``."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.communicate = AsyncMock(return_value=(self._stdout, self._stderr))

    def kill(self):  # pragma: no cover - only invoked in timeout test
        pass


def _make_client(**kwargs) -> CodexClient:
    """Instantiate a CodexClient without touching module-level settings state."""
    with patch("src.clients.codex_client.settings") as mock_settings:
        mock_settings.trading.primary_model = "codex/gpt-5-codex"
        mock_settings.trading.fallback_model = "codex/gpt-5.4-codex"
        mock_settings.trading.ai_temperature = 0
        mock_settings.trading.ai_max_tokens = 8000
        mock_settings.trading.daily_ai_cost_limit = 10.0
        mock_settings.trading.max_position_size_pct = 3.0
        defaults = {"cli_path": "/usr/local/bin/codex"}
        defaults.update(kwargs)
        return CodexClient(**defaults)


# ---------------------------------------------------------------------------
# Pure-helper tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_canonical_model_accepts_codex_slash(self):
        assert _canonical_codex_model("codex/gpt-5.4-codex") == "codex/gpt-5.4-codex"

    def test_canonical_model_accepts_alias(self):
        assert _canonical_codex_model("codex") == "codex/gpt-5-codex"
        assert _canonical_codex_model("o3-codex") == "codex/o3-codex"

    def test_canonical_model_maps_openai_prefix_to_default(self):
        # openai/gpt-5.4 has no direct codex analog in the alias table, so
        # we expect the canonical fallback rather than a bare string.
        assert _canonical_codex_model("openai/gpt-5.4") == CODEX_FALLBACK_ORDER[0]

    def test_canonical_model_empty_string_falls_back(self):
        assert _canonical_codex_model(None) == CODEX_FALLBACK_ORDER[0]
        assert _canonical_codex_model("") == CODEX_FALLBACK_ORDER[0]

    def test_build_fallback_chain_preserves_primary(self):
        chain = _build_fallback_chain("codex/o3-codex", ["codex/gpt-5-codex"])
        assert chain[0] == "codex/o3-codex"
        assert "codex/gpt-5-codex" in chain
        # Must dedupe: every entry exactly once.
        assert len(chain) == len(set(chain))

    def test_messages_to_prompt_joins_chat_roles(self):
        prompt = _messages_to_prompt(
            None,
            [
                {"role": "system", "content": "You are a trader."},
                {"role": "user", "content": "Buy YES?"},
            ],
        )
        assert "[SYSTEM]" in prompt
        assert "[USER]" in prompt
        assert "You are a trader." in prompt

    def test_messages_to_prompt_requires_input(self):
        with pytest.raises(ValueError):
            _messages_to_prompt(None, None)

    def test_parse_trading_decision_happy_path(self):
        decision = _parse_trading_decision(
            {
                "action": "BUY",
                "side": "YES",
                "limit_price": 55,
                "confidence": 0.82,
                "reasoning": "positive edge",
            }
        )
        assert decision is not None
        assert decision.action == "BUY"
        assert decision.side == "YES"
        assert decision.limit_price == 55
        assert decision.confidence == pytest.approx(0.82)

    def test_parse_trading_decision_defaults_invalid_enum(self):
        decision = _parse_trading_decision(
            {
                "action": "maybe?",
                "side": "unknown",
                "confidence": "0.5",
                "limit_price": None,
                "reasoning": "unclear",
            }
        )
        assert decision is not None
        assert decision.action == "SKIP"
        assert decision.side == "YES"
        assert decision.limit_price is None


# ---------------------------------------------------------------------------
# CLI discovery / auth probing
# ---------------------------------------------------------------------------


class TestCliDiscovery:
    def setup_method(self):
        clear_codex_auth_cache()

    def teardown_method(self):
        clear_codex_auth_cache()

    def test_resolve_cli_path_prefers_env_override(self, monkeypatch, tmp_path):
        fake = tmp_path / "codex"
        fake.write_text("")
        monkeypatch.setenv("CODEX_CLI_PATH", str(fake))
        assert resolve_codex_cli_path() == str(fake)

    def test_resolve_cli_path_env_override_missing_returns_none(self, monkeypatch):
        monkeypatch.setenv("CODEX_CLI_PATH", "/definitely/does/not/exist/codex")
        with patch("src.clients.codex_client.shutil.which", return_value=None):
            assert resolve_codex_cli_path() is None

    def test_resolve_cli_path_uses_which(self, monkeypatch):
        monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
        with patch(
            "src.clients.codex_client.shutil.which", return_value="/opt/bin/codex"
        ):
            assert resolve_codex_cli_path() == "/opt/bin/codex"

    def test_auth_probe_returns_false_when_cli_missing(self, monkeypatch):
        monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
        monkeypatch.delenv("CODEX_DISABLE_AUTH_PROBE", raising=False)
        with patch("src.clients.codex_client.shutil.which", return_value=None):
            assert is_codex_authenticated() is False

    def test_auth_probe_respects_disable_env(self, monkeypatch):
        monkeypatch.setenv("CODEX_DISABLE_AUTH_PROBE", "1")
        with patch(
            "src.clients.codex_client.shutil.which", return_value="/usr/bin/codex"
        ):
            assert is_codex_authenticated() is False

    def test_auth_probe_caches_result(self, monkeypatch):
        monkeypatch.delenv("CODEX_DISABLE_AUTH_PROBE", raising=False)
        with patch(
            "src.clients.codex_client._run_auth_probe_sync", return_value=True
        ) as probe:
            assert is_codex_authenticated("/usr/bin/codex") is True
            # Second call hits the cache; the probe stays at one invocation.
            assert is_codex_authenticated("/usr/bin/codex") is True
            assert probe.call_count == 1


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------


class TestTokenExtraction:
    def test_extract_tokens_from_json_usage_block(self):
        stdout = json.dumps(
            {
                "content": "hello",
                "usage": {
                    "input_tokens": 123,
                    "output_tokens": 45,
                    "total_tokens": 168,
                    "output_tokens_details": {"reasoning_tokens": 9},
                },
            }
        )
        in_tok, out_tok, total_tok, reasoning = CodexClient._extract_token_counts(
            stdout, ""
        )
        assert in_tok == 123
        assert out_tok == 45
        assert total_tok == 168
        assert reasoning == 9

    def test_extract_tokens_handles_prompt_completion_keys(self):
        stdout = json.dumps(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            }
        )
        in_tok, out_tok, total_tok, _ = CodexClient._extract_token_counts(stdout, "")
        assert (in_tok, out_tok, total_tok) == (10, 20, 30)

    def test_extract_tokens_regex_from_stderr(self):
        stdout = "some non-json output"
        stderr = "INFO total_tokens=512 input_tokens=400 output_tokens=112"
        in_tok, out_tok, total_tok, _ = CodexClient._extract_token_counts(stdout, stderr)
        assert (in_tok, out_tok, total_tok) == (400, 112, 512)

    def test_extract_tokens_absent_returns_zeros(self):
        assert CodexClient._extract_token_counts("plain text", "") == (0, 0, 0, 0)


# ---------------------------------------------------------------------------
# Quota signal extraction (W8 plan-tier attribution)
# ---------------------------------------------------------------------------


class TestQuotaSignalExtraction:
    def test_plain_text_plan_tier_and_request_pair(self):
        """Plain-text rate-limit lines like 'plan: Plus' / 'requests: 12/100 (resets ...)'
        should populate plan_tier, requests_used/limit/remaining, and reset_at.
        """
        stdout = ""
        stderr = (
            "INFO codex rate-limit: plan: Plus\n"
            "INFO requests: 12/100 (resets 2026-04-27T00:00:00Z)\n"
            "INFO tokens: 4500/200000\n"
        )
        signals = CodexClient._extract_quota_signals(stdout, stderr)
        assert signals["plan_tier"] == "Plus"
        assert signals["requests_used"] == 12
        assert signals["requests_limit"] == 100
        assert signals["requests_remaining"] == 88
        assert signals["requests_reset_at"] == "2026-04-27T00:00:00Z"
        assert signals["tokens_used"] == 4500
        assert signals["tokens_limit"] == 200000

    def test_structured_rate_limit_block(self):
        """JSON 'rate_limit' blocks should populate every quota field."""
        stdout = json.dumps(
            {
                "content": "ok",
                "plan": "Pro",
                "rate_limit": {
                    "requests": {
                        "used": 7,
                        "limit": 50,
                        "remaining": 43,
                        "reset_at": "2026-04-27T05:00:00Z",
                    },
                    "tokens": {
                        "used": 1234,
                        "limit": 500000,
                        "remaining": 498766,
                        "reset_at": "2026-04-27T05:00:00Z",
                    },
                },
            }
        )
        signals = CodexClient._extract_quota_signals(stdout, "")
        assert signals["plan_tier"] == "Pro"
        assert signals["requests_used"] == 7
        assert signals["requests_limit"] == 50
        assert signals["requests_remaining"] == 43
        assert signals["requests_reset_at"] == "2026-04-27T05:00:00Z"
        assert signals["tokens_used"] == 1234
        assert signals["tokens_limit"] == 500000
        assert signals["tokens_remaining"] == 498766
        assert signals["tokens_reset_at"] == "2026-04-27T05:00:00Z"
        # raw_payload is captured for downstream debugging
        assert signals["raw_payload"] is not None

    def test_absent_signals_return_all_none(self):
        signals = CodexClient._extract_quota_signals("plain", "")
        for key in (
            "plan_tier",
            "requests_used",
            "requests_limit",
            "requests_remaining",
            "requests_reset_at",
            "tokens_used",
            "tokens_limit",
            "tokens_remaining",
            "tokens_reset_at",
            "raw_payload",
        ):
            assert signals[key] is None, f"Expected {key} to be None"

    @pytest.mark.asyncio
    async def test_record_quota_snapshot_persists_plan_tier_and_attribution(self):
        """A successful CLI completion should record a quota snapshot row."""
        captured = []

        class _FakeDb:
            async def log_llm_query(self, *_args, **_kwargs):
                return None

            def record_codex_quota_snapshot(self, snapshot):
                captured.append(snapshot)

                async def _noop():
                    return 1

                return _noop()

        client = _make_client(db_manager=_FakeDb())

        proc = _FakeProcess(
            stdout=(
                json.dumps(
                    {
                        "content": "ok",
                        "usage": {"input_tokens": 5, "output_tokens": 7},
                        "plan": "Plus",
                        "rate_limit": {
                            "requests": {
                                "used": 12,
                                "limit": 100,
                                "remaining": 88,
                                "reset_at": "2026-04-27T00:00:00Z",
                            },
                            "tokens": {
                                "used": 4500,
                                "limit": 200000,
                                "remaining": 195500,
                                "reset_at": "2026-04-27T00:00:00Z",
                            },
                        },
                    }
                )
                + "\n"
            ).encode(),
            stderr=b"",
            returncode=0,
        )
        with patch(
            "src.clients.codex_client.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            result = await client.get_completion("hi", strategy="unit_test")

        assert result == "ok"
        assert len(captured) == 1
        snapshot = captured[0]
        assert snapshot.plan_tier == "Plus"
        assert snapshot.requests_used == 12
        assert snapshot.requests_limit == 100
        assert snapshot.requests_remaining == 88
        assert snapshot.requests_reset_at == "2026-04-27T00:00:00Z"
        assert snapshot.tokens_used == 4500
        assert snapshot.tokens_limit == 200000
        assert snapshot.tokens_remaining == 195500
        assert snapshot.tokens_reset_at == "2026-04-27T00:00:00Z"
        assert snapshot.source == "codex-cli"

    @pytest.mark.asyncio
    async def test_record_quota_snapshot_falls_back_to_best_effort(self):
        """When the CLI output has no quota signals, source should be ``codex-cli-best-effort``."""
        captured = []

        class _FakeDb:
            async def log_llm_query(self, *_args, **_kwargs):
                return None

            def record_codex_quota_snapshot(self, snapshot):
                captured.append(snapshot)

                async def _noop():
                    return 1

                return _noop()

        client = _make_client(db_manager=_FakeDb())

        proc = _FakeProcess(
            stdout=json.dumps(
                {"content": "ok", "usage": {"prompt_tokens": 3, "completion_tokens": 4}}
            ).encode(),
            stderr=b"",
            returncode=0,
        )
        with patch(
            "src.clients.codex_client.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            await client.get_completion("hi", strategy="unit_test")

        assert len(captured) == 1
        snapshot = captured[0]
        assert snapshot.plan_tier == client.plan_tier
        assert snapshot.requests_limit is None
        assert snapshot.requests_remaining is None
        assert snapshot.tokens_limit is None
        assert snapshot.source == "codex-cli-best-effort"


# ---------------------------------------------------------------------------
# End-to-end client behavior (fully mocked subprocess)
# ---------------------------------------------------------------------------


class TestCodexClientCompletion:
    def test_load_daily_tracker_accepts_legacy_xai_pickle(self, monkeypatch):
        tracker = DailyUsageTracker(
            date=datetime.now().strftime("%Y-%m-%d"),
            request_count=7,
            total_cost=0.0,
        )
        original_module = DailyUsageTracker.__module__
        DailyUsageTracker.__module__ = "src.clients.xai_client"
        try:
            payload = pickle.dumps(tracker)
        finally:
            DailyUsageTracker.__module__ = original_module

        usage_file = "memory://daily_ai_usage.pkl"
        monkeypatch.setattr(codex_module, "SHARED_USAGE_FILE", usage_file)
        monkeypatch.setattr(
            codex_module.os.path,
            "exists",
            lambda path: path == usage_file,
        )

        def _fake_open(path, mode="rb", *args, **kwargs):
            assert path == usage_file
            assert "rb" in mode
            return io.BytesIO(payload)

        with patch("builtins.open", _fake_open):
            client = _make_client()

        assert client.daily_tracker.request_count == 7

    @pytest.mark.asyncio
    async def test_get_completion_parses_json_payload(self):
        client = _make_client()

        proc = _FakeProcess(
            stdout=json.dumps(
                {
                    "content": "Sample answer from Codex.",
                    "usage": {
                        "input_tokens": 50,
                        "output_tokens": 25,
                        "total_tokens": 75,
                    },
                }
            ).encode(),
            stderr=b"",
            returncode=0,
        )

        with patch(
            "src.clients.codex_client.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ) as create:
            result = await client.get_completion("Hello codex", strategy="unit_test")

        assert result == "Sample answer from Codex."
        # Subprocess invoked with exec + --json
        argv = create.await_args.args
        assert argv[0] == client.cli_path
        assert "exec" in argv
        assert "--json" in argv

        metadata = client.last_request_metadata
        assert metadata.total_tokens == 75
        assert metadata.cost == 0.0
        assert metadata.actual_model in CODEX_FALLBACK_ORDER

    @pytest.mark.asyncio
    async def test_quota_tracker_persists_write(self):
        """Request count should be written to the shared pickle tracker."""
        client = _make_client()
        baseline = client.daily_tracker.request_count

        proc = _FakeProcess(
            stdout=json.dumps(
                {"content": "ok", "usage": {"prompt_tokens": 5, "completion_tokens": 7}}
            ).encode()
        )
        with patch(
            "src.clients.codex_client.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            with patch.object(client, "_save_daily_tracker") as save:
                await client.get_completion("another prompt")

        # At least one write per successful request (best-effort quota logging).
        assert save.called
        assert client.daily_tracker.request_count == baseline + 1

    @pytest.mark.asyncio
    async def test_structured_completion_enforces_json_schema(self):
        client = _make_client()
        payload = {
            "action": "BUY",
            "side": "YES",
            "limit_price": 57,
            "confidence": 0.73,
            "reasoning": "Edge above 10%.",
        }
        proc = _FakeProcess(
            stdout=json.dumps(payload).encode(),
            stderr=b"total_tokens=200",
        )
        with patch(
            "src.clients.codex_client.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ) as create:
            parsed = await client.create_structured_completion(
                "Decide.",
                schema=TRADING_DECISION_JSON_SCHEMA,
                strategy="unit_test",
            )

        assert parsed == payload
        argv = create.await_args.args
        # Structured-output flag was passed to the CLI.
        assert "--structured-output" in argv

    @pytest.mark.asyncio
    async def test_trading_decision_round_trip(self):
        client = _make_client()
        payload = {
            "action": "SELL",
            "side": "NO",
            "limit_price": 42,
            "confidence": 0.66,
            "reasoning": "Negative momentum.",
        }
        proc = _FakeProcess(stdout=json.dumps(payload).encode())

        market = {
            "title": "Does team A win?",
            "yes_bid_dollars": 0.4,
            "yes_ask_dollars": 0.45,
            "no_bid_dollars": 0.55,
            "no_ask_dollars": 0.6,
            "volume": 12000,
            "days_to_expiry": 1,
            "ticker": "T-ABC",
        }
        portfolio = {"cash": 500, "balance": 500}

        with patch(
            "src.clients.codex_client.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            decision = await client.get_trading_decision(market, portfolio)

        assert decision is not None
        assert decision.action == "SELL"
        assert decision.side == "NO"
        assert decision.limit_price == 42

    @pytest.mark.asyncio
    async def test_fallback_when_primary_nonzero_exit(self):
        client = _make_client()

        bad = _FakeProcess(stdout=b"", stderr=b"boom", returncode=2)
        good = _FakeProcess(
            stdout=json.dumps({"content": "fallback ok"}).encode(), returncode=0
        )

        create_mock = AsyncMock(side_effect=[bad, good])
        with patch(
            "src.clients.codex_client.asyncio.create_subprocess_exec", create_mock
        ):
            result = await client.get_completion(
                "Retry please",
                model="codex/gpt-5-codex",
                fallback_models=["codex/gpt-5.4-codex"],
            )

        assert result == "fallback ok"
        assert create_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_returns_none_when_cli_missing(self):
        client = _make_client(cli_path=None)
        result = await client.get_completion("anything")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_subprocess_launch_fails(self):
        client = _make_client()
        create_mock = AsyncMock(side_effect=FileNotFoundError("codex gone"))
        with patch(
            "src.clients.codex_client.asyncio.create_subprocess_exec", create_mock
        ):
            result = await client.get_completion("hi")
        assert result is None

    @pytest.mark.asyncio
    async def test_extracts_token_fallback_from_stderr_when_stdout_is_plain(self):
        client = _make_client()
        proc = _FakeProcess(
            stdout=b"plain text output without JSON envelope",
            stderr=b"usage: total_tokens=321 input_tokens=100 output_tokens=221",
        )
        with patch(
            "src.clients.codex_client.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            result = await client.get_completion("hello")
        assert result is not None
        meta = client.last_request_metadata
        assert meta.total_tokens == 321
        assert meta.input_tokens == 100
        assert meta.output_tokens == 221


# ---------------------------------------------------------------------------
# Cost summary + logging parity
# ---------------------------------------------------------------------------


class TestCodexClientAccounting:
    @pytest.mark.asyncio
    async def test_cost_summary_always_zero(self):
        client = _make_client()
        proc = _FakeProcess(
            stdout=json.dumps(
                {"content": "yo", "usage": {"input_tokens": 1, "output_tokens": 2}}
            ).encode()
        )
        with patch(
            "src.clients.codex_client.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            await client.get_completion("something")

        summary = client.get_cost_summary()
        assert summary["total_cost"] == 0.0
        assert summary["daily_cost"] == 0.0
        assert summary["total_requests"] == 1
        last = summary["last_request"]
        assert last["cost"] == 0.0
        assert last["total_tokens"] == 3

    @pytest.mark.asyncio
    async def test_llm_query_logged_with_zero_cost(self):
        db_manager = MagicMock()
        db_manager.log_llm_query = AsyncMock()
        client = _make_client(db_manager=db_manager)

        proc = _FakeProcess(
            stdout=json.dumps(
                {"content": "sure", "usage": {"input_tokens": 4, "output_tokens": 6}}
            ).encode()
        )
        with patch(
            "src.clients.codex_client.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            await client.get_completion(
                "anything", strategy="s", query_type="q", market_id="KXFOO"
            )

        # asyncio.create_task was used to schedule the log call; give the loop
        # a chance to run it by yielding control.
        import asyncio

        await asyncio.sleep(0)
        assert db_manager.log_llm_query.called
        logged = db_manager.log_llm_query.await_args.args[0]
        assert logged.cost_usd == 0.0
        assert logged.tokens_used == 10
        assert logged.market_id == "KXFOO"
        assert logged.strategy == "s"
