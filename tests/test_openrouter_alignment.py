"""Alignment tests for OpenRouter-facing config and structured-output parsing."""

from unittest.mock import patch

from src.agents.debate import DebateRunner
from src.config.settings import EnsembleConfig, settings
from src.data.sentiment_analyzer import SentimentAnalyzer


def test_ensemble_config_normalizes_legacy_roles_and_adds_trader():
    """Legacy lead_analyst config should normalize to news_analyst."""
    config = EnsembleConfig(
        models={
            "anthropic/claude-sonnet-4.5": {
                "provider": "openrouter",
                "role": "lead_analyst",
                "weight": 0.3,
            },
            "openai/gpt-5.4": {
                "provider": "openrouter",
                "role": "risk_manager",
                "weight": 0.2,
            },
        },
        trader_model="x-ai/grok-4.1-fast",
    )

    with patch.object(settings.api, "resolve_llm_provider", return_value="openrouter"):
        role_map = config.get_role_model_map()

    assert role_map["news_analyst"] == "anthropic/claude-sonnet-4.5"
    assert role_map["trader"] == "x-ai/grok-4.1-fast"
    assert "lead_analyst" not in role_map


def test_debate_runner_uses_normalized_role_models():
    """DebateRunner defaults should reflect the normalized ensemble role config."""
    runner = DebateRunner()
    role_models = settings.ensemble.get_role_model_map()

    assert runner.agents["news_analyst"].model_name == role_models["news_analyst"]
    assert runner.agents["forecaster"].model_name == role_models["forecaster"]
    assert runner.agents["risk_manager"].model_name == role_models["risk_manager"]
    assert runner.agents["trader"].model_name == role_models["trader"]


def test_sentiment_parser_accepts_plain_and_fenced_json():
    """Sentiment parsing should keep working even if a model wraps JSON in fences."""
    plain = SentimentAnalyzer._parse_sentiment_response(
        '{"score": 0.4, "confidence": 0.8, "reasoning": "positive catalyst"}'
    )
    fenced = SentimentAnalyzer._parse_sentiment_response(
        '```json\n{"score": -0.2, "confidence": 0.6, "reasoning": "minor headwind"}\n```'
    )

    assert plain.score == 0.4
    assert fenced.score == -0.2
