"""Tests for the direct OpenAI client researched-completion path."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.openai_client import OpenAIClient
from src.data.live_trade_research import LIVE_TRADE_RESPONSES_TEXT_FORMAT


def _mock_responses_api_response():
    usage = SimpleNamespace(
        input_tokens=120,
        output_tokens=80,
        total_tokens=200,
        output_tokens_details=SimpleNamespace(reasoning_tokens=9),
    )
    web_search_item = SimpleNamespace(
        type="web_search_call",
        action=SimpleNamespace(
            sources=[
                SimpleNamespace(url="https://www.espn.com/mlb/story/_/id/1"),
            ]
        ),
    )
    message_item = SimpleNamespace(
        type="message",
        content=[
            SimpleNamespace(
                annotations=[
                    SimpleNamespace(url="https://www.reuters.com/markets/crypto/bitcoin/")
                ]
            )
        ],
    )
    return SimpleNamespace(
        id="resp_test",
        model="gpt-5.4",
        status="completed",
        usage=usage,
        output_text='{"summary":"Looks good","confidence":0.7,"key_drivers":[],"risk_flags":[],"recommended_markets":[]}',
        output=[web_search_item, message_item],
    )


class TestOpenAIClient:
    """Coverage for OpenAI researched completions."""

    @pytest.mark.asyncio
    async def test_get_researched_completion_uses_responses_api(self):
        response = _mock_responses_api_response()
        async_client = MagicMock()
        async_client.responses.create = AsyncMock(return_value=response)

        with patch("src.clients.openai_client.AsyncOpenAI", return_value=async_client):
            with patch("src.clients.openai_client.settings") as mock_settings:
                mock_settings.api.openai_api_key = "test-key"
                mock_settings.api.openai_base_url = "https://api.openai.com/v1"
                mock_settings.trading.primary_model = "openai/gpt-5.4"
                mock_settings.trading.fallback_model = "openai/o3"
                mock_settings.trading.ai_temperature = 0
                mock_settings.trading.ai_max_tokens = 8000
                mock_settings.trading.daily_ai_cost_limit = 50.0

                client = OpenAIClient()

        with patch.object(client, "_check_daily_limits", AsyncMock(return_value=True)):
            result = await client.get_researched_completion(
                prompt="Analyze this market.",
                instructions="Use live research.",
                model="openai/gpt-5.4",
                text_format=LIVE_TRADE_RESPONSES_TEXT_FORMAT,
                search_allowed_domains=["espn.com", "reuters.com"],
                strategy="unit_test",
                query_type="live_trade_analysis",
                market_id="KXBTC-TODAY",
            )

        assert result is not None
        assert result["used_web_research"] is True
        assert result["sources"] == [
            "https://www.espn.com/mlb/story/_/id/1",
            "https://www.reuters.com/markets/crypto/bitcoin/",
        ]

        kwargs = async_client.responses.create.await_args.kwargs
        assert kwargs["model"] == "gpt-5.4"
        assert kwargs["instructions"] == "Use live research."
        assert kwargs["text"]["format"]["name"] == "live_trade_analysis"
        assert kwargs["tools"][0]["type"] == "web_search"
        assert kwargs["tools"][0]["filters"]["allowed_domains"] == ["espn.com", "reuters.com"]
        assert kwargs["include"] == ["web_search_call.action.sources"]
        assert client.last_request_metadata.actual_model == "openai/gpt-5.4"
