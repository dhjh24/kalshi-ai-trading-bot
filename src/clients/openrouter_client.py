"""
OpenRouter client for multi-model AI-powered trading decisions.

Routes requests through OpenRouter's chat completions API using the OpenAI
SDK, while exposing OpenRouter-specific request controls like model fallbacks,
provider routing, plugins, and structured outputs.
"""

import asyncio
import json
import os
import pickle
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from json_repair import repair_json
from openai import AsyncOpenAI

from src.clients.xai_client import DailyUsageTracker, TradingDecision
from src.config.settings import settings
from src.utils.kalshi_normalization import get_market_prices, get_market_volume
from src.utils.logging_setup import TradingLoggerMixin, log_error_with_context


MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "anthropic/claude-sonnet-4": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    "anthropic/claude-sonnet-4.5": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    "openai/o3": {"input_per_1k": 0.002, "output_per_1k": 0.008},
    "openai/gpt-4.1": {"input_per_1k": 0.002, "output_per_1k": 0.008},
    "openai/gpt-5.4": {"input_per_1k": 0.0025, "output_per_1k": 0.015},
    "google/gemini-2.5-pro-preview": {"input_per_1k": 0.00125, "output_per_1k": 0.01},
    "google/gemini-3-pro-preview": {"input_per_1k": 0.002, "output_per_1k": 0.012},
    "google/gemini-3.1-pro-preview": {"input_per_1k": 0.002, "output_per_1k": 0.012},
    "google/gemini-3.1-flash-lite-preview": {
        "input_per_1k": 0.00025,
        "output_per_1k": 0.0015,
    },
    "deepseek/deepseek-r1": {"input_per_1k": 0.0008, "output_per_1k": 0.002},
    "deepseek/deepseek-v3.2": {"input_per_1k": 0.00026, "output_per_1k": 0.00038},
    "x-ai/grok-4.1-fast": {"input_per_1k": 0.0002, "output_per_1k": 0.0005},
}

DEFAULT_FALLBACK_ORDER: List[str] = [
    "anthropic/claude-sonnet-4.5",
    "google/gemini-3.1-pro-preview",
    "openai/gpt-5.4",
    "deepseek/deepseek-v3.2",
    "x-ai/grok-4.1-fast",
]

SHARED_USAGE_FILE = "logs/daily_ai_usage.pkl"

TRADING_DECISION_RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "trading_decision",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["BUY", "SELL", "SKIP"]},
                "side": {"type": "string", "enum": ["YES", "NO"]},
                "limit_price": {"type": "integer"},
                "confidence": {"type": "number"},
                "reasoning": {"type": "string"},
            },
            "required": ["action", "side", "limit_price", "confidence", "reasoning"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class ModelCostTracker:
    """Accumulated cost data for a single model."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost: float = 0.0
    request_count: int = 0
    error_count: int = 0
    last_used: Optional[datetime] = None


@dataclass
class OpenRouterResponseMetadata:
    """Metadata captured from the most recent OpenRouter response."""

    request_id: Optional[str] = None
    requested_model: Optional[str] = None
    actual_model: Optional[str] = None
    fallback_models: List[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    cost_details: Dict[str, Any] = field(default_factory=dict)
    service_tier: Optional[str] = None
    finish_reason: Optional[str] = None


class OpenRouterClient(TradingLoggerMixin):
    """
    Async client that accesses multiple frontier models through OpenRouter.

    Features:
        * OpenRouter-native model fallbacks via ``extra_body.models``
        * Provider routing / plugin / trace request passthrough
        * Structured-output support with strict JSON schema requests
        * Cost tracking using OpenRouter's ``usage.cost`` when available
        * Shared daily spend enforcement across OpenRouter/XAI/router callers
    """

    MAX_RETRIES_PER_REQUEST: int = 3
    BASE_BACKOFF: float = 1.0
    MAX_BACKOFF: float = 30.0

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = "anthropic/claude-sonnet-4.5",
        db_manager: Any = None,
    ):
        self.api_key = api_key or settings.api.openrouter_api_key
        self.base_url = settings.api.openrouter_base_url
        self.default_model = default_model
        self.db_manager = db_manager

        default_headers = settings.api.get_openrouter_headers() or None
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=120.0,
            max_retries=0,
            default_headers=default_headers,
        )

        self.temperature = settings.trading.ai_temperature
        self.max_tokens = settings.trading.ai_max_tokens
        self.model_costs: Dict[str, ModelCostTracker] = {
            model: ModelCostTracker(model=model) for model in MODEL_PRICING
        }
        self.total_cost: float = 0.0
        self.request_count: int = 0
        self.usage_file = SHARED_USAGE_FILE
        self.daily_tracker: DailyUsageTracker = self._load_daily_tracker()
        self._last_request_cost: float = 0.0
        self._last_request_metadata = OpenRouterResponseMetadata()

        self.logger.info(
            "OpenRouter client initialized",
            default_model=self.default_model,
            available_models=list(MODEL_PRICING.keys()),
            daily_limit=self.daily_tracker.daily_limit,
            today_cost=self.daily_tracker.total_cost,
            today_requests=self.daily_tracker.request_count,
            attribution_headers=list((default_headers or {}).keys()),
        )

    @property
    def last_request_metadata(self) -> OpenRouterResponseMetadata:
        """Return metadata for the most recent successful request."""
        return self._last_request_metadata

    def _load_daily_tracker(self) -> DailyUsageTracker:
        """Load or create the shared daily usage tracker from disk."""
        today = datetime.now().strftime("%Y-%m-%d")
        daily_limit = getattr(settings.trading, "daily_ai_cost_limit", 50.0)
        os.makedirs("logs", exist_ok=True)

        try:
            if os.path.exists(self.usage_file):
                with open(self.usage_file, "rb") as fh:
                    tracker: DailyUsageTracker = pickle.load(fh)
                if tracker.date != today:
                    tracker = DailyUsageTracker(date=today, daily_limit=daily_limit)
                else:
                    if tracker.daily_limit != daily_limit:
                        tracker.daily_limit = daily_limit
                        if tracker.is_exhausted and tracker.total_cost < daily_limit:
                            tracker.is_exhausted = False
                return tracker
        except Exception as exc:
            self.logger.warning(f"Failed to load daily tracker: {exc}")

        return DailyUsageTracker(date=today, daily_limit=daily_limit)

    def _save_daily_tracker(self) -> None:
        """Persist the shared daily usage tracker to disk."""
        try:
            os.makedirs("logs", exist_ok=True)
            with open(self.usage_file, "wb") as fh:
                pickle.dump(self.daily_tracker, fh)
        except Exception as exc:
            self.logger.error(f"Failed to save daily tracker: {exc}")

    def _update_daily_cost(self, cost: float) -> None:
        """Add cost to the daily tracker and mark the day as exhausted if needed."""
        self.daily_tracker.total_cost += cost
        self.daily_tracker.request_count += 1
        self._save_daily_tracker()

        if self.daily_tracker.total_cost >= self.daily_tracker.daily_limit:
            self.daily_tracker.is_exhausted = True
            self.daily_tracker.last_exhausted_time = datetime.now()
            self._save_daily_tracker()
            self.logger.warning(
                "Daily OpenRouter cost limit reached",
                daily_cost=self.daily_tracker.total_cost,
                daily_limit=self.daily_tracker.daily_limit,
                requests_today=self.daily_tracker.request_count,
            )

    async def _check_daily_limits(self) -> bool:
        """Return True if we are within the shared daily spending limit."""
        self.daily_tracker = self._load_daily_tracker()

        if self.daily_tracker.is_exhausted:
            now = datetime.now()
            if self.daily_tracker.date != now.strftime("%Y-%m-%d"):
                self.daily_tracker = DailyUsageTracker(
                    date=now.strftime("%Y-%m-%d"),
                    daily_limit=self.daily_tracker.daily_limit,
                )
                self._save_daily_tracker()
                self.logger.info(
                    "New day -- OpenRouter daily limits reset",
                    daily_limit=self.daily_tracker.daily_limit,
                )
                return True

            self.logger.info(
                "OpenRouter daily limit reached -- request skipped",
                daily_cost=self.daily_tracker.total_cost,
                daily_limit=self.daily_tracker.daily_limit,
            )
            return False

        return True

    def _calculate_cost(
        self,
        model: Optional[str],
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """Return fallback USD cost for the given token counts."""
        pricing = MODEL_PRICING.get(model or "")
        if pricing is None:
            return (input_tokens + output_tokens) * 0.00001

        input_cost = (input_tokens / 1000.0) * pricing["input_per_1k"]
        output_cost = (output_tokens / 1000.0) * pricing["output_per_1k"]
        return input_cost + output_cost

    def _track_model_cost(
        self,
        model: Optional[str],
        input_tokens: int,
        output_tokens: int,
        cost: float,
    ) -> None:
        """Update the per-model cost tracker."""
        if not model:
            return

        tracker = self.model_costs.get(model)
        if tracker is None:
            tracker = ModelCostTracker(model=model)
            self.model_costs[model] = tracker

        tracker.input_tokens += input_tokens
        tracker.output_tokens += output_tokens
        tracker.total_cost += cost
        tracker.request_count += 1
        tracker.last_used = datetime.now()

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        error_str = str(exc).lower()
        return any(
            indicator in error_str
            for indicator in ["rate limit", "429", "too many requests", "quota"]
        )

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        error_str = str(exc).lower()
        return any(
            indicator in error_str
            for indicator in [
                "rate limit",
                "429",
                "too many requests",
                "timeout",
                "502",
                "503",
                "504",
                "server error",
                "internal error",
                "overloaded",
                "connection reset",
            ]
        )

    def _backoff_delay(self, attempt: int) -> float:
        """Compute exponential backoff delay for *attempt* (0-based)."""
        delay = self.BASE_BACKOFF * (2**attempt)
        return min(delay, self.MAX_BACKOFF)

    def _build_fallback_chain(self, requested_model: Optional[str] = None) -> List[str]:
        """Return an ordered list of models to try."""
        first = requested_model or self.default_model
        chain = [first]
        for model in DEFAULT_FALLBACK_ORDER:
            if model not in chain:
                chain.append(model)
        return chain

    @staticmethod
    def _normalize_messages(
        prompt: Optional[str],
        messages: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Return the message list used for the chat completion request."""
        if messages:
            return messages
        if prompt is None:
            raise ValueError("Either prompt or messages must be provided")
        return [{"role": "user", "content": prompt}]

    @staticmethod
    def _merge_provider_preferences(
        provider: Optional[Dict[str, Any]],
        *,
        require_parameters: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Merge caller provider settings with defaults needed for the request."""
        merged = dict(provider or {})
        if require_parameters:
            merged.setdefault("require_parameters", True)
        return merged or None

    @staticmethod
    def _build_request_metadata(
        strategy: str,
        query_type: str,
        market_id: Optional[str],
        metadata: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, str]]:
        """Build string-only metadata for the OpenRouter request."""
        merged: Dict[str, str] = {}
        for key, value in (metadata or {}).items():
            if value is None:
                continue
            merged[str(key)] = str(value)[:512]

        if strategy:
            merged.setdefault("strategy", strategy)
        if query_type:
            merged.setdefault("query_type", query_type)
        if market_id:
            merged.setdefault("market_id", str(market_id))

        return merged or None

    def _build_request_kwargs(
        self,
        *,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, str]] = None,
        fallback_models: Optional[List[str]] = None,
        provider: Optional[Dict[str, Any]] = None,
        plugins: Optional[List[Dict[str, Any]]] = None,
        session_id: Optional[str] = None,
        trace: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build kwargs for the OpenAI SDK chat completions call."""
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
        }

        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if response_format is not None:
            kwargs["response_format"] = response_format
        if metadata:
            kwargs["metadata"] = metadata

        extra_body: Dict[str, Any] = {}
        normalized_fallbacks = [
            fallback_model
            for fallback_model in (fallback_models or [])
            if fallback_model and fallback_model != model
        ]
        if normalized_fallbacks:
            extra_body["models"] = normalized_fallbacks
            extra_body["route"] = "fallback"
        if provider:
            extra_body["provider"] = provider
        if plugins:
            extra_body["plugins"] = plugins
        if session_id:
            extra_body["session_id"] = session_id
        if trace:
            extra_body["trace"] = trace

        if extra_body:
            kwargs["extra_body"] = extra_body

        return kwargs

    @staticmethod
    def _usage_extra_dict(usage: Any) -> Dict[str, Any]:
        """Return extra usage fields emitted by OpenRouter."""
        extra = getattr(usage, "model_extra", None)
        if isinstance(extra, dict):
            return dict(extra)
        return {}

    def _extract_response_metadata(
        self,
        response: Any,
        *,
        requested_model: str,
        fallback_models: List[str],
    ) -> OpenRouterResponseMetadata:
        """Extract normalized metadata from an OpenRouter chat completion."""
        usage = getattr(response, "usage", None)
        usage_extra = self._usage_extra_dict(usage) if usage is not None else {}
        completion_details = (
            getattr(usage, "completion_tokens_details", None) if usage is not None else None
        )

        input_tokens = getattr(usage, "prompt_tokens", 0) if usage is not None else 0
        output_tokens = getattr(usage, "completion_tokens", 0) if usage is not None else 0
        total_tokens = getattr(usage, "total_tokens", input_tokens + output_tokens)
        reasoning_tokens = (
            getattr(completion_details, "reasoning_tokens", 0) if completion_details else 0
        )

        actual_model = getattr(response, "model", None) or requested_model
        cost = usage_extra.get("cost")
        if cost is None:
            cost = self._calculate_cost(actual_model, input_tokens, output_tokens)

        finish_reason = None
        if getattr(response, "choices", None):
            finish_reason = getattr(response.choices[0], "finish_reason", None)

        return OpenRouterResponseMetadata(
            request_id=getattr(response, "id", None),
            requested_model=requested_model,
            actual_model=actual_model,
            fallback_models=list(fallback_models),
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            total_tokens=int(total_tokens or (input_tokens + output_tokens)),
            reasoning_tokens=int(reasoning_tokens or 0),
            cost=float(cost or 0.0),
            cost_details=usage_extra.get("cost_details") or {},
            service_tier=getattr(response, "service_tier", None),
            finish_reason=finish_reason,
        )

    async def _request_chat_completion(
        self,
        *,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, str]] = None,
        fallback_models: Optional[List[str]] = None,
        provider: Optional[Dict[str, Any]] = None,
        plugins: Optional[List[Dict[str, Any]]] = None,
        session_id: Optional[str] = None,
        trace: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, OpenRouterResponseMetadata]:
        """
        Make a single OpenRouter request with request-level retries.

        Model fallback is delegated to OpenRouter via ``extra_body.models`` rather
        than retried locally model-by-model.
        """
        fallback_models = list(fallback_models or self._build_fallback_chain(model)[1:])
        last_exc: Optional[Exception] = None

        request_kwargs = self._build_request_kwargs(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            metadata=metadata,
            fallback_models=fallback_models,
            provider=provider,
            plugins=plugins,
            session_id=session_id,
            trace=trace,
        )

        for attempt in range(self.MAX_RETRIES_PER_REQUEST):
            try:
                start = time.time()
                response = await self.client.chat.completions.create(**request_kwargs)
                elapsed = time.time() - start

                if not response.choices or not response.choices[0].message:
                    raise ValueError(
                        f"Empty response from {model} on attempt {attempt + 1}"
                    )

                content = response.choices[0].message.content
                if not content:
                    raise ValueError(
                        f"Missing message content from {model} on attempt {attempt + 1}"
                    )

                metadata_obj = self._extract_response_metadata(
                    response,
                    requested_model=model,
                    fallback_models=fallback_models,
                )

                self._last_request_cost = metadata_obj.cost
                self._last_request_metadata = metadata_obj

                self.logger.debug(
                    "OpenRouter completion succeeded",
                    requested_model=model,
                    actual_model=metadata_obj.actual_model,
                    fallback_models=fallback_models,
                    input_tokens=metadata_obj.input_tokens,
                    output_tokens=metadata_obj.output_tokens,
                    reasoning_tokens=metadata_obj.reasoning_tokens,
                    cost=round(metadata_obj.cost, 6),
                    processing_time=round(elapsed, 2),
                    attempt=attempt + 1,
                )

                return content, metadata_obj

            except Exception as exc:
                last_exc = exc

                tracker = self.model_costs.get(model)
                if tracker:
                    tracker.error_count += 1

                is_retryable = self._is_retryable_error(exc)
                self.logger.warning(
                    "OpenRouter request failed",
                    model=model,
                    attempt=attempt + 1,
                    max_retries=self.MAX_RETRIES_PER_REQUEST,
                    retryable=is_retryable,
                    error=str(exc),
                )

                if is_retryable and attempt < self.MAX_RETRIES_PER_REQUEST - 1:
                    delay = self._backoff_delay(attempt)
                    if self._is_rate_limit_error(exc):
                        delay *= 2
                    await asyncio.sleep(delay)
                else:
                    break

        raise last_exc  # type: ignore[misc]

    def _record_request_metrics(self, metadata: OpenRouterResponseMetadata) -> None:
        """Update aggregate and per-model cost tracking for a completed request."""
        self._track_model_cost(
            metadata.actual_model,
            metadata.input_tokens,
            metadata.output_tokens,
            metadata.cost,
        )
        self.total_cost += metadata.cost
        self.request_count += 1
        self._update_daily_cost(metadata.cost)

    async def get_completion(
        self,
        prompt: Optional[str] = None,
        *,
        messages: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        strategy: str = "unknown",
        query_type: str = "completion",
        market_id: Optional[str] = None,
        fallback_models: Optional[List[str]] = None,
        provider: Optional[Dict[str, Any]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        plugins: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        trace: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Get a completion from OpenRouter using the requested model plus fallbacks.

        Falls back via OpenRouter-native routing when ``fallback_models`` is
        provided (or when the default fallback chain is used).
        """
        if not await self._check_daily_limits():
            return None

        selected_model = model or self.default_model
        resolved_messages = self._normalize_messages(prompt, messages)
        request_metadata = self._build_request_metadata(
            strategy,
            query_type,
            market_id,
            metadata,
        )

        try:
            content, response_metadata = await self._request_chat_completion(
                messages=resolved_messages,
                model=selected_model,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=max_tokens or self.max_tokens,
                response_format=response_format,
                metadata=request_metadata,
                fallback_models=fallback_models,
                provider=provider,
                plugins=plugins,
                session_id=session_id,
                trace=trace,
            )

            self._record_request_metrics(response_metadata)

            prompt_preview = prompt
            if prompt_preview is None:
                prompt_preview = json.dumps(resolved_messages)[:2000]

            await self._log_query(
                strategy=strategy,
                query_type=query_type,
                prompt=prompt_preview,
                response=content,
                market_id=market_id,
                tokens_used=response_metadata.total_tokens,
                cost_usd=response_metadata.cost,
            )

            return content

        except Exception as exc:
            log_error_with_context(
                exc,
                {
                    "model": selected_model,
                    "fallback_models": fallback_models
                    or self._build_fallback_chain(selected_model)[1:],
                    "messages_count": len(resolved_messages),
                    "temperature": temperature if temperature is not None else self.temperature,
                    "max_tokens": max_tokens or self.max_tokens,
                    "strategy": strategy,
                    "query_type": query_type,
                },
                "openrouter_completion_failed",
            )
            return None

    async def get_trading_decision(
        self,
        market_data: Dict[str, Any],
        portfolio_data: Dict[str, Any],
        news_summary: str = "",
        model: Optional[str] = None,
        *,
        fallback_models: Optional[List[str]] = None,
        provider: Optional[Dict[str, Any]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        plugins: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        trace: Optional[Dict[str, Any]] = None,
    ) -> Optional[TradingDecision]:
        """
        Obtain a structured trading decision from an OpenRouter model.

        The request uses strict structured outputs by default and only accepts
        providers that support the full request shape.
        """
        if not await self._check_daily_limits():
            return None

        prompt = self._build_trading_prompt(market_data, portfolio_data, news_summary)
        selected_model = model or self.default_model
        merged_provider = self._merge_provider_preferences(
            provider,
            require_parameters=True,
        )
        request_metadata = self._build_request_metadata(
            "openrouter",
            "trading_decision",
            market_data.get("ticker") or market_data.get("market_id"),
            metadata,
        )

        try:
            content, response_metadata = await self._request_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=selected_model,
                temperature=0.1,
                max_tokens=4000,
                response_format=response_format or TRADING_DECISION_RESPONSE_FORMAT,
                metadata=request_metadata,
                fallback_models=fallback_models,
                provider=merged_provider,
                plugins=plugins,
                session_id=session_id,
                trace=trace,
            )

            self._record_request_metrics(response_metadata)
            decision = self._parse_trading_decision(content)

            if decision is not None:
                await self._log_query(
                    strategy="openrouter",
                    query_type="trading_decision",
                    prompt=prompt,
                    response=content,
                    market_id=market_data.get("ticker") or market_data.get("market_id"),
                    tokens_used=response_metadata.total_tokens,
                    cost_usd=response_metadata.cost,
                    confidence_extracted=decision.confidence,
                    decision_extracted=decision.action,
                )
                return decision

            self.logger.warning(
                "Failed to parse trading decision from model response",
                model=response_metadata.actual_model or selected_model,
                response_preview=content[:200] if content else "EMPTY",
            )
            return None

        except Exception as exc:
            log_error_with_context(
                exc,
                {
                    "model": selected_model,
                    "fallback_models": fallback_models
                    or self._build_fallback_chain(selected_model)[1:],
                    "market_title": market_data.get("title", "unknown"),
                },
                "openrouter_trading_decision_failed",
            )
            return None

    def _build_trading_prompt(
        self,
        market_data: Dict[str, Any],
        portfolio_data: Dict[str, Any],
        news_summary: str,
    ) -> str:
        """Build a concise trading-decision prompt."""
        title = market_data.get("title", "Unknown Market")
        yes_bid, yes_ask, no_bid, no_ask = get_market_prices(market_data)
        yes_price = (
            ((yes_bid + yes_ask) / 2.0) * 100
            if yes_bid and yes_ask
            else max(yes_bid, yes_ask) * 100
        )
        no_price = (
            ((no_bid + no_ask) / 2.0) * 100
            if no_bid and no_ask
            else max(no_bid, no_ask) * 100
        )
        volume = get_market_volume(market_data)
        days_to_expiry = market_data.get("days_to_expiry", "Unknown")
        rules = market_data.get("rules", "No specific rules provided")

        cash = portfolio_data.get("cash", portfolio_data.get("balance", 1000))
        max_trade_value = portfolio_data.get(
            "max_trade_value",
            cash * settings.trading.max_position_size_pct / 100,
        )

        truncated_news = (
            news_summary[:800] + "..." if len(news_summary) > 800 else news_summary
        )

        return f"""Analyze this prediction market and provide a trading decision.

Market: {title}
Rules: {rules}
YES price: {yes_price:.1f}c | NO price: {no_price:.1f}c | Volume: ${volume:,.0f}
Days to expiry: {days_to_expiry}

Available cash: ${cash:,.2f} | Max trade value: ${max_trade_value:,.2f}

News/Context:
{truncated_news}

Instructions:
- Estimate the true probability of the event.
- Only trade if your estimated edge (|your_probability - market_price/100|) exceeds 10%.
- Confidence must be >60% to recommend a trade.
- Return a JSON object only. Do not include markdown fences or extra commentary.

Example trade:
{{"action": "BUY", "side": "YES", "limit_price": 55, "confidence": 0.72, "reasoning": "brief explanation"}}

Example skip:
{{"action": "SKIP", "side": "YES", "limit_price": 0, "confidence": 0.40, "reasoning": "insufficient edge"}}
"""

    def _parse_trading_decision(self, response_text: str) -> Optional[TradingDecision]:
        """
        Extract a TradingDecision from model output.

        Structured outputs should already return plain JSON, but the parser keeps
        the older markdown/object extraction path as a defensive fallback.
        """
        try:
            json_str = response_text

            json_match = re.search(r"```(?:json)?\s*(.*?)\s*```", response_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                bare_json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
                if bare_json_match:
                    json_str = bare_json_match.group(0)

            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                repaired = repair_json(json_str)
                if repaired:
                    data = json.loads(repaired)
                else:
                    self.logger.warning("JSON repair returned empty result")
                    return None

            action = str(data.get("action", "SKIP")).upper()
            if action not in {"BUY", "SELL", "SKIP"}:
                action = "SKIP"

            side = str(data.get("side", "YES")).upper()
            if side not in {"YES", "NO"}:
                side = "YES"

            confidence = float(data.get("confidence", 0.5))
            limit_price_raw = data.get("limit_price")
            limit_price = (
                int(round(float(limit_price_raw)))
                if limit_price_raw is not None
                else None
            )
            reasoning = str(data.get("reasoning", "No reasoning provided."))

            return TradingDecision(
                action=action,
                side=side,
                confidence=confidence,
                limit_price=limit_price,
                reasoning=reasoning,
            )

        except Exception as exc:
            self.logger.error(
                f"Error parsing trading decision: {exc}",
                response_preview=response_text[:500] if response_text else "EMPTY",
            )
            return None

    async def _log_query(
        self,
        strategy: str,
        query_type: str,
        prompt: str,
        response: str,
        market_id: Optional[str] = None,
        tokens_used: Optional[int] = None,
        cost_usd: Optional[float] = None,
        confidence_extracted: Optional[float] = None,
        decision_extracted: Optional[str] = None,
    ) -> None:
        """Persist a query record if a database manager is available."""
        if not self.db_manager:
            return
        try:
            from src.utils.database import LLMQuery

            llm_query = LLMQuery(
                timestamp=datetime.now(),
                strategy=strategy,
                query_type=query_type,
                market_id=market_id,
                prompt=prompt[:2000],
                response=response[:5000],
                tokens_used=tokens_used,
                cost_usd=cost_usd,
                confidence_extracted=confidence_extracted,
                decision_extracted=decision_extracted,
            )
            asyncio.create_task(self.db_manager.log_llm_query(llm_query))
        except Exception as exc:
            self.logger.error(f"Failed to log LLM query: {exc}")

    def get_cost_summary(self) -> Dict[str, Any]:
        """Return a summary of costs across all models."""
        self.daily_tracker = self._load_daily_tracker()

        per_model = {}
        for model, tracker in self.model_costs.items():
            if tracker.request_count > 0 or tracker.error_count > 0:
                per_model[model] = {
                    "requests": tracker.request_count,
                    "errors": tracker.error_count,
                    "input_tokens": tracker.input_tokens,
                    "output_tokens": tracker.output_tokens,
                    "total_cost": round(tracker.total_cost, 6),
                    "last_used": tracker.last_used.isoformat() if tracker.last_used else None,
                }

        return {
            "total_cost": round(self.total_cost, 6),
            "total_requests": self.request_count,
            "daily_cost": round(self.daily_tracker.total_cost, 6),
            "daily_limit": self.daily_tracker.daily_limit,
            "daily_exhausted": self.daily_tracker.is_exhausted,
            "last_request": {
                "requested_model": self._last_request_metadata.requested_model,
                "actual_model": self._last_request_metadata.actual_model,
                "fallback_models": self._last_request_metadata.fallback_models,
                "cost": round(self._last_request_metadata.cost, 6),
                "request_id": self._last_request_metadata.request_id,
            },
            "per_model": per_model,
        }

    async def close(self) -> None:
        """Shut down the underlying HTTP client."""
        try:
            await self.client.close()
        except Exception:
            pass
        self.logger.info(
            "OpenRouter client closed",
            total_cost=round(self.total_cost, 6),
            total_requests=self.request_count,
        )
