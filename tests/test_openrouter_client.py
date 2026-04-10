"""Tests for the OpenRouter client and model router."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.openrouter_client import (
    MODEL_PRICING,
    OpenRouterClient,
    TRADING_DECISION_RESPONSE_FORMAT,
)


def _mock_response(
    *,
    content='{"ok": true}',
    model="openai/gpt-5.4",
    prompt_tokens=100,
    completion_tokens=50,
    cost=0.123,
):
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=11),
        model_extra={
            "cost": cost,
            "cost_details": {"upstream_inference_prompt_cost": 0.02},
        },
    )
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(
        id="gen-test",
        model=model,
        choices=[choice],
        usage=usage,
        service_tier="default",
    )


class TestOpenRouterClient:
    """Tests for OpenRouterClient."""

    def test_model_pricing_registry(self):
        """Verify current production models are in the fallback pricing registry."""
        expected_models = [
            "anthropic/claude-sonnet-4.5",
            "google/gemini-3.1-pro-preview",
            "openai/gpt-5.4",
            "deepseek/deepseek-v3.2",
            "x-ai/grok-4.1-fast",
        ]
        for model in expected_models:
            assert model in MODEL_PRICING, f"Missing model pricing: {model}"
            pricing = MODEL_PRICING[model]
            assert "input_per_1k" in pricing
            assert "output_per_1k" in pricing

    def test_client_initialization_uses_openrouter_headers(self):
        """Test client initializes with the configured attribution headers."""
        async_client = MagicMock()

        with patch("src.clients.openrouter_client.AsyncOpenAI", return_value=async_client) as ctor:
            with patch("src.clients.openrouter_client.settings") as mock_settings:
                mock_settings.api.openrouter_api_key = "test-key"
                mock_settings.api.openrouter_base_url = "https://openrouter.ai/api/v1"
                mock_settings.api.get_openrouter_headers.return_value = {
                    "HTTP-Referer": "https://example.com",
                    "X-OpenRouter-Title": "Kalshi Bot",
                }
                mock_settings.trading.ai_temperature = 0
                mock_settings.trading.ai_max_tokens = 8000
                mock_settings.trading.daily_ai_cost_limit = 50.0

                client = OpenRouterClient()

        ctor.assert_called_once()
        assert ctor.call_args.kwargs["default_headers"]["HTTP-Referer"] == "https://example.com"
        assert client.total_cost == 0.0
        assert client.request_count == 0

    def test_extract_response_metadata_prefers_usage_cost(self):
        """usage.cost should beat the fallback pricing table when present."""
        client = OpenRouterClient.__new__(OpenRouterClient)
        response = _mock_response(model="google/gemini-3.1-pro-preview", cost=0.456)

        metadata = client._extract_response_metadata(
            response,
            requested_model="anthropic/claude-sonnet-4.5",
            fallback_models=["google/gemini-3.1-pro-preview"],
        )

        assert metadata.requested_model == "anthropic/claude-sonnet-4.5"
        assert metadata.actual_model == "google/gemini-3.1-pro-preview"
        assert metadata.cost == pytest.approx(0.456)
        assert metadata.reasoning_tokens == 11
        assert metadata.cost_details["upstream_inference_prompt_cost"] == 0.02

    def test_extract_response_metadata_falls_back_to_registry_cost(self):
        """Fallback pricing should be used when usage.cost is absent."""
        client = OpenRouterClient.__new__(OpenRouterClient)
        response = _mock_response(model="x-ai/grok-4.1-fast", cost=None)
        response.usage.model_extra = {}

        metadata = client._extract_response_metadata(
            response,
            requested_model="x-ai/grok-4.1-fast",
            fallback_models=[],
        )

        expected = client._calculate_cost("x-ai/grok-4.1-fast", 100, 50)
        assert metadata.cost == pytest.approx(expected)

    def test_build_request_kwargs_truncates_fallback_models_to_three(self):
        """OpenRouter should never send more than 3 fallback models in one request."""
        client = OpenRouterClient.__new__(OpenRouterClient)
        client._logger = MagicMock()

        kwargs = client._build_request_kwargs(
            messages=[{"role": "user", "content": "hello"}],
            model="anthropic/claude-sonnet-4.5",
            fallback_models=[
                "google/gemini-3.1-pro-preview",
                "openai/gpt-5.4",
                "deepseek/deepseek-v3.2",
                "x-ai/grok-4.1-fast",
            ],
        )

        assert kwargs["extra_body"]["models"] == [
            "google/gemini-3.1-pro-preview",
            "openai/gpt-5.4",
            "deepseek/deepseek-v3.2",
        ]

    def test_extract_affordable_max_tokens_from_credit_error(self):
        """Credit errors should expose a smaller retryable max_tokens value."""
        exc = Exception(
            "Error code: 402 - This request requires more credits, but can only afford 2349."
        )

        affordable = OpenRouterClient._extract_affordable_max_tokens(exc)

        assert affordable is not None
        assert 0 < affordable < 2349
        assert OpenRouterClient._is_retryable_error(exc) is True

    @pytest.mark.asyncio
    async def test_get_completion_passes_openrouter_request_shape(self):
        """Request payload should include fallback models and OpenRouter-specific fields."""
        response = _mock_response(content='{"status":"ok"}', model="deepseek/deepseek-v3.2")
        async_client = MagicMock()
        async_client.chat.completions.create = AsyncMock(return_value=response)

        with patch("src.clients.openrouter_client.AsyncOpenAI", return_value=async_client):
            with patch("src.clients.openrouter_client.settings") as mock_settings:
                mock_settings.api.openrouter_api_key = "test-key"
                mock_settings.api.openrouter_base_url = "https://openrouter.ai/api/v1"
                mock_settings.api.get_openrouter_headers.return_value = {}
                mock_settings.trading.ai_temperature = 0
                mock_settings.trading.ai_max_tokens = 8000
                mock_settings.trading.daily_ai_cost_limit = 50.0

                client = OpenRouterClient()

        with patch.object(client, "_check_daily_limits", AsyncMock(return_value=True)):
            result = await client.get_completion(
                prompt="hello",
                model="openai/gpt-5.4",
                fallback_models=["deepseek/deepseek-v3.2", "x-ai/grok-4.1-fast"],
                provider={"sort": "price"},
                plugins=[{"id": "response-healing"}],
                metadata={"request_kind": "unit_test"},
                session_id="session-123",
                trace={"trace_id": "trace-123"},
            )

        assert result == '{"status":"ok"}'
        kwargs = async_client.chat.completions.create.await_args.kwargs
        assert kwargs["model"] == "openai/gpt-5.4"
        assert kwargs["messages"] == [{"role": "user", "content": "hello"}]
        assert kwargs["extra_body"]["models"] == [
            "deepseek/deepseek-v3.2",
            "x-ai/grok-4.1-fast",
        ]
        assert kwargs["extra_body"]["provider"]["sort"] == "price"
        assert kwargs["extra_body"]["plugins"] == [{"id": "response-healing"}]
        assert kwargs["extra_body"]["session_id"] == "session-123"
        assert kwargs["extra_body"]["trace"] == {"trace_id": "trace-123"}
        assert kwargs["metadata"]["request_kind"] == "unit_test"
        assert kwargs["metadata"]["query_type"] == "completion"
        assert client.last_request_metadata.actual_model == "deepseek/deepseek-v3.2"

    @pytest.mark.asyncio
    async def test_request_chat_completion_retries_credit_errors_with_lower_max_tokens(self):
        """OpenRouter credit errors should retry with a reduced token cap."""
        response = _mock_response(content='{"status":"ok"}')
        credit_error = Exception(
            "Error code: 402 - This request requires more credits, or fewer max_tokens. "
            "You requested up to 3000 tokens, but can only afford 2349."
        )
        async_client = MagicMock()
        async_client.chat.completions.create = AsyncMock(
            side_effect=[credit_error, response]
        )

        with patch("src.clients.openrouter_client.AsyncOpenAI", return_value=async_client):
            with patch("src.clients.openrouter_client.settings") as mock_settings:
                mock_settings.api.openrouter_api_key = "test-key"
                mock_settings.api.openrouter_base_url = "https://openrouter.ai/api/v1"
                mock_settings.api.get_openrouter_headers.return_value = {}
                mock_settings.trading.ai_temperature = 0
                mock_settings.trading.ai_max_tokens = 8000
                mock_settings.trading.daily_ai_cost_limit = 50.0

                client = OpenRouterClient()

        with patch("src.clients.openrouter_client.asyncio.sleep", AsyncMock()):
            content, _metadata = await client._request_chat_completion(
                messages=[{"role": "user", "content": "hello"}],
                model="anthropic/claude-sonnet-4.5",
                max_tokens=3000,
            )

        assert content == '{"status":"ok"}'
        first_call = async_client.chat.completions.create.await_args_list[0].kwargs
        second_call = async_client.chat.completions.create.await_args_list[1].kwargs
        assert first_call["max_tokens"] == 3000
        assert 0 < second_call["max_tokens"] < 2349

    @pytest.mark.asyncio
    async def test_get_trading_decision_uses_structured_outputs(self):
        """Trading decisions should request strict JSON schema output."""
        response = _mock_response(
            content='{"action":"BUY","side":"YES","limit_price":54,"confidence":0.78,"reasoning":"Edge exists"}',
            model="openai/gpt-5.4",
        )
        async_client = MagicMock()
        async_client.chat.completions.create = AsyncMock(return_value=response)

        with patch("src.clients.openrouter_client.AsyncOpenAI", return_value=async_client):
            with patch("src.clients.openrouter_client.settings") as mock_settings:
                mock_settings.api.openrouter_api_key = "test-key"
                mock_settings.api.openrouter_base_url = "https://openrouter.ai/api/v1"
                mock_settings.api.get_openrouter_headers.return_value = {}
                mock_settings.trading.ai_temperature = 0
                mock_settings.trading.ai_max_tokens = 8000
                mock_settings.trading.max_position_size_pct = 3.0
                mock_settings.trading.daily_ai_cost_limit = 50.0

                client = OpenRouterClient()

        with patch.object(client, "_check_daily_limits", AsyncMock(return_value=True)):
            decision = await client.get_trading_decision(
                market_data={"title": "Test Market", "ticker": "TEST", "yes_price": 51, "no_price": 49, "volume": 1000},
                portfolio_data={"cash": 1000, "max_trade_value": 30},
                news_summary="Test summary",
            )

        assert decision is not None
        assert decision.action == "BUY"
        assert decision.limit_price == 54
        kwargs = async_client.chat.completions.create.await_args.kwargs
        assert kwargs["response_format"] == TRADING_DECISION_RESPONSE_FORMAT
        assert kwargs["extra_body"]["provider"]["require_parameters"] is True

    def test_parse_trading_decision_valid_json(self):
        """Trading decisions should parse plain JSON."""
        client = OpenRouterClient.__new__(OpenRouterClient)
        client._logger = MagicMock()

        decision = client._parse_trading_decision(
            '{"action":"SELL","side":"NO","limit_price":44,"confidence":0.61,"reasoning":"priced too high"}'
        )
        assert decision is not None
        assert decision.action == "SELL"
        assert decision.side == "NO"
        assert decision.reasoning == "priced too high"

    def test_fallback_chain_ordering(self):
        """The requested model should stay first in the default fallback chain."""
        client = OpenRouterClient.__new__(OpenRouterClient)
        chain = client._build_fallback_chain("openai/gpt-5.4")
        assert chain[0] == "openai/gpt-5.4"
        assert "deepseek/deepseek-v3.2" in chain


class TestModelRouter:
    """Tests for the ModelRouter."""

    def test_capability_routing_uses_current_models(self):
        """Capability map should reflect the current model roster."""
        from src.clients.model_router import CAPABILITY_MAP

        assert "fast" in CAPABILITY_MAP
        assert any("grok-4.1-fast" in model for model, _ in CAPABILITY_MAP["fast"])
        assert any(
            "gemini-3.1-pro-preview" in model for model, _ in CAPABILITY_MAP["reasoning"]
        )

    @pytest.mark.asyncio
    async def test_router_sends_one_request_with_fallback_models(self):
        """ModelRouter should delegate one OpenRouter request with native fallbacks."""
        from src.clients.model_router import ModelRouter

        openrouter_client = MagicMock()
        openrouter_client.get_completion = AsyncMock(return_value="ok")
        openrouter_client.last_request_metadata = SimpleNamespace(
            actual_model="deepseek/deepseek-v3.2"
        )

        router = ModelRouter(openrouter_client=openrouter_client)
        result = await router.get_completion("hello", model="openai/gpt-5.4")

        assert result == "ok"
        kwargs = openrouter_client.get_completion.await_args.kwargs
        assert kwargs["model"] == "openai/gpt-5.4"
        assert "deepseek/deepseek-v3.2" in kwargs["fallback_models"]

    @pytest.mark.asyncio
    async def test_router_uses_openai_client_when_provider_defaults_to_openai(self):
        """OpenAI should be used as the active backend when selected in settings."""
        from src.clients.model_router import ModelRouter

        openai_client = MagicMock()
        openai_client.get_completion = AsyncMock(return_value="ok")
        openai_client.last_request_metadata = SimpleNamespace(
            actual_model="openai/gpt-5.4"
        )
        openrouter_client = MagicMock()
        openrouter_client.get_completion = AsyncMock(return_value="unexpected")

        with patch("src.clients.model_router.settings") as mock_settings:
            mock_settings.api.resolve_llm_provider.return_value = "openai"
            mock_settings.trading.daily_ai_cost_limit = 50.0

            router = ModelRouter(
                openai_client=openai_client,
                openrouter_client=openrouter_client,
            )

        result = await router.get_completion("hello", capability="reasoning")

        assert result == "ok"
        openai_client.get_completion.assert_awaited()
        openrouter_client.get_completion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_router_researched_completion_falls_back_to_standard_completion(self):
        """Non-OpenAI providers should fall back to standard completions without web search."""
        from src.clients.model_router import ModelRouter

        openrouter_client = MagicMock()
        openrouter_client.get_completion = AsyncMock(return_value='{"ok": true}')

        with patch("src.clients.model_router.settings") as mock_settings:
            mock_settings.api.resolve_llm_provider.return_value = "openrouter"
            mock_settings.trading.daily_ai_cost_limit = 50.0

            router = ModelRouter(openrouter_client=openrouter_client)

        result = await router.get_researched_completion(
            prompt="Market payload",
            instructions="Analyze carefully",
            response_format={"type": "json_schema", "json_schema": {"name": "test", "strict": True, "schema": {"type": "object"}}},
            strategy="unit_test",
            query_type="researched_completion",
        )

        assert result == {
            "content": '{"ok": true}',
            "sources": [],
            "used_web_research": False,
        }
        kwargs = openrouter_client.get_completion.await_args.kwargs
        assert "Analyze carefully" in kwargs["prompt"]

    def test_cost_summary_contains_provider_and_health(self):
        """Aggregate summaries should keep provider and health sections."""
        from src.clients.model_router import ModelRouter

        router = ModelRouter()
        summary = router.get_cost_summary()
        assert "providers" in summary
        assert "model_health" in summary
