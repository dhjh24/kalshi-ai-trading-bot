"""
OpenAI-backed client for AI-powered trading decisions.

This client mirrors the higher-level interface exposed by ``openrouter_client``
so the rest of the trading system can switch providers cleanly.
"""

import ast
import asyncio
import importlib
import json
import os
import pickle
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

from src.clients.shared_types import DailyUsageTracker, TradingDecision
from src.config.settings import settings
from src.utils.kalshi_normalization import get_market_prices, get_market_volume
from src.utils.logging_setup import TradingLoggerMixin, log_error_with_context


def repair_json(payload: str) -> str:
    """Best-effort JSON repair with optional json_repair dependency."""
    try:
        module = importlib.import_module("json_repair")
        external_repair = getattr(module, "repair_json", None)
        if callable(external_repair):
            repaired = external_repair(payload)
            if isinstance(repaired, str):
                return repaired
    except Exception:
        pass

    text = (payload or "").strip()
    if not text:
        return ""

    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r",\s*([}\]])", r"\1", text)

    try:
        json.loads(text)
        return text
    except Exception:
        pass

    try:
        parsed = ast.literal_eval(text)
        return json.dumps(parsed)
    except Exception:
        return ""


OPENAI_MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "openai/o3": {"input_per_1k": 0.002, "output_per_1k": 0.008},
    "openai/gpt-4.1": {"input_per_1k": 0.002, "output_per_1k": 0.008},
    "openai/gpt-5.4": {"input_per_1k": 0.0025, "output_per_1k": 0.015},
}

OPENAI_MODEL_ALIASES: Dict[str, str] = {
    "o3": "openai/o3",
    "gpt-4.1": "openai/gpt-4.1",
    "gpt-5.4": "openai/gpt-5.4",
}

OPENAI_FALLBACK_ORDER: List[str] = [
    "openai/gpt-5.4",
    "openai/o3",
    "openai/gpt-4.1",
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
class OpenAIResponseMetadata:
    """Metadata captured from the most recent OpenAI response."""

    request_id: Optional[str] = None
    requested_model: Optional[str] = None
    actual_model: Optional[str] = None
    fallback_models: List[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    finish_reason: Optional[str] = None


class OpenAIClient(TradingLoggerMixin):
    """Async client for direct OpenAI API access."""

    MAX_RETRIES_PER_MODEL: int = 3
    BASE_BACKOFF: float = 1.0
    MAX_BACKOFF: float = 30.0

    def __init__(self, api_key: Optional[str] = None, db_manager: Any = None):
        self.api_key = api_key or settings.api.openai_api_key
        self.base_url = settings.api.openai_base_url
        self.db_manager = db_manager

        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=120.0,
            max_retries=0,
        )

        self.default_model = self._coerce_openai_model(settings.trading.primary_model)
        self.fallback_model = self._coerce_openai_model(settings.trading.fallback_model)
        self.temperature = settings.trading.ai_temperature
        self.max_tokens = settings.trading.ai_max_tokens
        self.model_costs: Dict[str, ModelCostTracker] = {
            model: ModelCostTracker(model=model) for model in OPENAI_MODEL_PRICING
        }
        self.total_cost: float = 0.0
        self.request_count: int = 0
        self.usage_file = SHARED_USAGE_FILE
        self.daily_tracker: DailyUsageTracker = self._load_daily_tracker()
        self._last_request_cost: float = 0.0
        self._last_request_metadata = OpenAIResponseMetadata()

        self.logger.info(
            "OpenAI client initialized",
            default_model=self.default_model,
            fallback_model=self.fallback_model,
            daily_limit=self.daily_tracker.daily_limit,
            today_cost=self.daily_tracker.total_cost,
            today_requests=self.daily_tracker.request_count,
        )

    @property
    def last_request_metadata(self) -> OpenAIResponseMetadata:
        """Return metadata for the most recent successful request."""
        return self._last_request_metadata

    def _canonical_model_name(self, model: Optional[str]) -> str:
        """Normalize a model identifier to the internal `openai/...` form."""
        name = str(model or "").strip()
        if not name:
            return self.default_model
        if name in OPENAI_MODEL_ALIASES:
            return OPENAI_MODEL_ALIASES[name]
        if name.startswith("openai/"):
            return name
        if "/" in name:
            return name
        return f"openai/{name}"

    def _coerce_openai_model(self, model: Optional[str]) -> str:
        """
        Ensure the selected model is compatible with the direct OpenAI provider.

        If a non-OpenAI model is requested while this client is active, fall back
        to the default OpenAI model instead of hard-failing the whole request path.
        """
        canonical = self._canonical_model_name(model)
        if canonical.startswith("openai/"):
            return canonical

        fallback = OPENAI_FALLBACK_ORDER[0]
        self.logger.warning(
            "Requested model is not available via direct OpenAI provider; using default OpenAI model",
            requested_model=canonical,
            fallback_model=fallback,
        )
        return fallback

    @staticmethod
    def _sdk_model_name(model: str) -> str:
        """Convert the internal `openai/...` name to the SDK-facing model id."""
        if model.startswith("openai/"):
            return model.split("/", 1)[1]
        return model

    def _build_fallback_chain(self, requested_model: Optional[str] = None) -> List[str]:
        """Return an ordered list of OpenAI models to try locally."""
        first = self._coerce_openai_model(requested_model or self.default_model)
        chain = [first]
        for model in OPENAI_FALLBACK_ORDER:
            if model not in chain:
                chain.append(model)
        return chain

    @staticmethod
    def _normalize_messages(
        prompt: Optional[str],
        messages: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Normalize prompt/messages arguments into chat-completions format."""
        if messages:
            return messages
        if prompt is None:
            raise ValueError("Either `prompt` or `messages` must be provided")
        return [{"role": "user", "content": prompt}]

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
                    tracker.daily_limit = daily_limit
                    if tracker.is_exhausted and tracker.total_cost < daily_limit:
                        tracker.is_exhausted = False
                return tracker
        except Exception as exc:
            self.logger.warning(f"Failed to load daily tracker: {exc}")

        return DailyUsageTracker(date=today, daily_limit=daily_limit)

    def _save_daily_tracker(self) -> None:
        """Persist the shared daily usage tracker."""
        try:
            os.makedirs("logs", exist_ok=True)
            with open(self.usage_file, "wb") as fh:
                pickle.dump(self.daily_tracker, fh)
        except Exception as exc:
            self.logger.error(f"Failed to save daily tracker: {exc}")

    def _update_daily_cost(self, cost: float) -> None:
        """Add cost to the shared daily tracker."""
        self.daily_tracker.total_cost += cost
        self.daily_tracker.request_count += 1
        self._save_daily_tracker()

        if self.daily_tracker.total_cost >= self.daily_tracker.daily_limit:
            self.daily_tracker.is_exhausted = True
            self.daily_tracker.last_exhausted_time = datetime.now()
            self._save_daily_tracker()
            self.logger.warning(
                "Daily OpenAI cost limit reached",
                daily_cost=self.daily_tracker.total_cost,
                daily_limit=self.daily_tracker.daily_limit,
                requests_today=self.daily_tracker.request_count,
            )

    async def _check_daily_limits(self) -> bool:
        """Return True if direct OpenAI usage is still within the daily limit."""
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
                    "New day -- OpenAI daily limits reset",
                    daily_limit=self.daily_tracker.daily_limit,
                )
                return True

            self.logger.info(
                "OpenAI daily limit reached -- request skipped",
                daily_cost=self.daily_tracker.total_cost,
                daily_limit=self.daily_tracker.daily_limit,
            )
            return False

        return True

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
        """Compute exponential backoff delay for a retry attempt."""
        delay = self.BASE_BACKOFF * (2**attempt)
        return min(delay, self.MAX_BACKOFF)

    def _calculate_cost(
        self,
        model: Optional[str],
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """Return fallback USD cost for the given token counts."""
        pricing = OPENAI_MODEL_PRICING.get(model or "")
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

    def _extract_response_metadata(
        self,
        response: Any,
        *,
        requested_model: str,
        fallback_models: List[str],
    ) -> OpenAIResponseMetadata:
        """Extract normalized metadata from an OpenAI chat completion response."""
        usage = getattr(response, "usage", None)
        completion_details = (
            getattr(usage, "completion_tokens_details", None) if usage is not None else None
        )

        input_tokens = getattr(usage, "prompt_tokens", 0) if usage is not None else 0
        output_tokens = getattr(usage, "completion_tokens", 0) if usage is not None else 0
        total_tokens = getattr(usage, "total_tokens", input_tokens + output_tokens)
        reasoning_tokens = (
            getattr(completion_details, "reasoning_tokens", 0) if completion_details else 0
        )

        actual_model = self._coerce_openai_model(
            getattr(response, "model", None) or requested_model
        )
        cost = self._calculate_cost(actual_model, int(input_tokens or 0), int(output_tokens or 0))

        finish_reason = None
        if getattr(response, "choices", None):
            finish_reason = getattr(response.choices[0], "finish_reason", None)

        return OpenAIResponseMetadata(
            request_id=getattr(response, "id", None),
            requested_model=requested_model,
            actual_model=actual_model,
            fallback_models=list(fallback_models),
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            total_tokens=int(total_tokens or (input_tokens + output_tokens)),
            reasoning_tokens=int(reasoning_tokens or 0),
            cost=float(cost or 0.0),
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
        fallback_models: Optional[List[str]] = None,
    ) -> Tuple[str, OpenAIResponseMetadata]:
        """Make a completion request with sequential local fallbacks."""
        selected_model = self._coerce_openai_model(model)
        normalized_fallbacks = []
        for fallback_model in (fallback_models or self._build_fallback_chain(selected_model)[1:]):
            coerced = self._coerce_openai_model(fallback_model)
            if coerced not in normalized_fallbacks and coerced != selected_model:
                normalized_fallbacks.append(coerced)

        candidates = [selected_model] + normalized_fallbacks
        last_exc: Optional[Exception] = None

        for candidate in candidates:
            request_kwargs: Dict[str, Any] = {
                "model": self._sdk_model_name(candidate),
                "messages": messages,
            }
            if temperature is not None:
                request_kwargs["temperature"] = temperature
            if max_tokens is not None:
                request_kwargs["max_tokens"] = max_tokens
            if response_format is not None:
                request_kwargs["response_format"] = response_format

            for attempt in range(self.MAX_RETRIES_PER_MODEL):
                try:
                    start = time.time()
                    response = await self.client.chat.completions.create(**request_kwargs)
                    elapsed = time.time() - start

                    if not response.choices or not response.choices[0].message:
                        raise ValueError(
                            f"Empty response from {candidate} on attempt {attempt + 1}"
                        )

                    content = response.choices[0].message.content
                    if not content:
                        raise ValueError(
                            f"Missing message content from {candidate} on attempt {attempt + 1}"
                        )

                    metadata_obj = self._extract_response_metadata(
                        response,
                        requested_model=selected_model,
                        fallback_models=normalized_fallbacks,
                    )

                    self._last_request_cost = metadata_obj.cost
                    self._last_request_metadata = metadata_obj

                    self.logger.debug(
                        "OpenAI completion succeeded",
                        requested_model=selected_model,
                        actual_model=metadata_obj.actual_model,
                        fallback_models=normalized_fallbacks,
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

                    tracker = self.model_costs.get(candidate)
                    if tracker:
                        tracker.error_count += 1

                    is_retryable = self._is_retryable_error(exc)
                    self.logger.warning(
                        "OpenAI request failed",
                        requested_model=selected_model,
                        attempted_model=candidate,
                        attempt=attempt + 1,
                        max_retries=self.MAX_RETRIES_PER_MODEL,
                        retryable=is_retryable,
                        error=str(exc),
                    )

                    if is_retryable and attempt < self.MAX_RETRIES_PER_MODEL - 1:
                        delay = self._backoff_delay(attempt)
                        if self._is_rate_limit_error(exc):
                            delay *= 2
                        await asyncio.sleep(delay)
                    else:
                        break

        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _build_request_metadata(metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        """Build string metadata for Responses API calls."""
        merged: Dict[str, str] = {}
        for key, value in (metadata or {}).items():
            if value is None:
                continue
            merged[str(key)] = str(value)[:512]
        return merged or None

    def _extract_responses_metadata(
        self,
        response: Any,
        *,
        requested_model: str,
        fallback_models: List[str],
    ) -> OpenAIResponseMetadata:
        """Extract normalized metadata from an OpenAI Responses API result."""
        usage = getattr(response, "usage", None)
        output_details = getattr(usage, "output_tokens_details", None) if usage is not None else None

        input_tokens = getattr(usage, "input_tokens", 0) if usage is not None else 0
        output_tokens = getattr(usage, "output_tokens", 0) if usage is not None else 0
        total_tokens = getattr(usage, "total_tokens", input_tokens + output_tokens)
        reasoning_tokens = (
            getattr(output_details, "reasoning_tokens", 0) if output_details is not None else 0
        )

        actual_model = self._coerce_openai_model(
            getattr(response, "model", None) or requested_model
        )
        cost = self._calculate_cost(actual_model, int(input_tokens or 0), int(output_tokens or 0))

        return OpenAIResponseMetadata(
            request_id=getattr(response, "id", None),
            requested_model=requested_model,
            actual_model=actual_model,
            fallback_models=list(fallback_models),
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            total_tokens=int(total_tokens or (input_tokens + output_tokens)),
            reasoning_tokens=int(reasoning_tokens or 0),
            cost=float(cost or 0.0),
            finish_reason=getattr(response, "status", None),
        )

    @staticmethod
    def _extract_researched_output(response: Any) -> Tuple[str, List[str]]:
        """Extract output text and cited URLs from a Responses API payload."""
        text = str(getattr(response, "output_text", "") or "").strip()
        urls: List[str] = []
        seen = set()

        for item in getattr(response, "output", []) or []:
            item_type = getattr(item, "type", "")
            if item_type == "web_search_call":
                action = getattr(item, "action", None)
                for source in getattr(action, "sources", []) or []:
                    url = getattr(source, "url", None)
                    if url and url not in seen:
                        seen.add(url)
                        urls.append(url)
                continue

            if item_type != "message":
                continue

            for content_item in getattr(item, "content", []) or []:
                for annotation in getattr(content_item, "annotations", []) or []:
                    url = getattr(annotation, "url", None)
                    if url and url not in seen:
                        seen.add(url)
                        urls.append(url)

        return text, urls

    async def _request_researched_response(
        self,
        *,
        prompt: str,
        instructions: Optional[str] = None,
        model: str,
        max_output_tokens: Optional[int] = None,
        text_format: Optional[Dict[str, Any]] = None,
        search_allowed_domains: Optional[List[str]] = None,
        search_context_size: str = "medium",
        fallback_models: Optional[List[str]] = None,
        metadata: Optional[Dict[str, str]] = None,
        use_web_search: bool = True,
    ) -> Tuple[str, List[str], OpenAIResponseMetadata]:
        """Make a Responses API request with optional web-search research."""
        selected_model = self._coerce_openai_model(model)
        normalized_fallbacks = []
        for fallback_model in (fallback_models or self._build_fallback_chain(selected_model)[1:]):
            coerced = self._coerce_openai_model(fallback_model)
            if coerced not in normalized_fallbacks and coerced != selected_model:
                normalized_fallbacks.append(coerced)

        candidates = [selected_model] + normalized_fallbacks
        last_exc: Optional[Exception] = None

        for candidate in candidates:
            request_kwargs: Dict[str, Any] = {
                "model": self._sdk_model_name(candidate),
                "input": prompt,
            }
            if instructions:
                request_kwargs["instructions"] = instructions
            if max_output_tokens is not None:
                request_kwargs["max_output_tokens"] = max_output_tokens
            if text_format is not None:
                request_kwargs["text"] = {
                    "format": text_format,
                    "verbosity": "medium",
                }
            if metadata:
                request_kwargs["metadata"] = metadata

            if use_web_search:
                web_tool: Dict[str, Any] = {
                    "type": "web_search",
                    "search_context_size": search_context_size,
                    "user_location": {
                        "type": "approximate",
                        "country": "US",
                        "timezone": "America/Los_Angeles",
                    },
                }
                if search_allowed_domains:
                    web_tool["filters"] = {"allowed_domains": list(search_allowed_domains)}
                request_kwargs["tools"] = [web_tool]
                request_kwargs["include"] = ["web_search_call.action.sources"]

            for attempt in range(self.MAX_RETRIES_PER_MODEL):
                try:
                    start = time.time()
                    response = await self.client.responses.create(**request_kwargs)
                    elapsed = time.time() - start

                    content, sources = self._extract_researched_output(response)
                    if not content:
                        raise ValueError(
                            f"Missing response output text from {candidate} on attempt {attempt + 1}"
                        )

                    metadata_obj = self._extract_responses_metadata(
                        response,
                        requested_model=selected_model,
                        fallback_models=normalized_fallbacks,
                    )

                    self._last_request_cost = metadata_obj.cost
                    self._last_request_metadata = metadata_obj

                    self.logger.debug(
                        "OpenAI researched response succeeded",
                        requested_model=selected_model,
                        actual_model=metadata_obj.actual_model,
                        fallback_models=normalized_fallbacks,
                        input_tokens=metadata_obj.input_tokens,
                        output_tokens=metadata_obj.output_tokens,
                        reasoning_tokens=metadata_obj.reasoning_tokens,
                        cost=round(metadata_obj.cost, 6),
                        source_count=len(sources),
                        processing_time=round(elapsed, 2),
                        attempt=attempt + 1,
                        used_web_search=use_web_search,
                    )

                    return content, sources, metadata_obj

                except Exception as exc:
                    last_exc = exc

                    tracker = self.model_costs.get(candidate)
                    if tracker:
                        tracker.error_count += 1

                    is_retryable = self._is_retryable_error(exc)
                    self.logger.warning(
                        "OpenAI researched request failed",
                        requested_model=selected_model,
                        attempted_model=candidate,
                        attempt=attempt + 1,
                        max_retries=self.MAX_RETRIES_PER_MODEL,
                        retryable=is_retryable,
                        error=str(exc),
                    )

                    if is_retryable and attempt < self.MAX_RETRIES_PER_MODEL - 1:
                        delay = self._backoff_delay(attempt)
                        if self._is_rate_limit_error(exc):
                            delay *= 2
                        await asyncio.sleep(delay)
                    else:
                        break

        raise last_exc  # type: ignore[misc]

    def _record_request_metrics(self, metadata: OpenAIResponseMetadata) -> None:
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
        role: Optional[str] = None,
        market_id: Optional[str] = None,
        fallback_models: Optional[List[str]] = None,
        provider: Optional[Dict[str, Any]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        plugins: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        trace: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Get a completion from OpenAI using local fallback models when needed."""
        del provider, plugins, session_id, trace

        if not await self._check_daily_limits():
            return None

        selected_model = self._coerce_openai_model(model or self.default_model)
        resolved_messages = self._normalize_messages(prompt, messages)

        try:
            content, response_metadata = await self._request_chat_completion(
                messages=resolved_messages,
                model=selected_model,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=max_tokens or self.max_tokens,
                response_format=response_format,
                fallback_models=fallback_models,
            )

            self._record_request_metrics(response_metadata)

            prompt_preview = prompt
            if prompt_preview is None:
                prompt_preview = json.dumps(resolved_messages)[:2000]

            await self._log_query(
                strategy=strategy,
                query_type=query_type,
                role=role or query_type,
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
                "openai_completion_failed",
            )
            return None

    async def get_researched_completion(
        self,
        prompt: str,
        *,
        instructions: Optional[str] = None,
        model: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
        text_format: Optional[Dict[str, Any]] = None,
        search_allowed_domains: Optional[List[str]] = None,
        search_context_size: str = "medium",
        strategy: str = "unknown",
        query_type: str = "researched_completion",
        market_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        use_web_search: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Get a researched completion from the Responses API with optional web search."""
        if not await self._check_daily_limits():
            return None

        selected_model = self._coerce_openai_model(model or self.default_model)
        request_metadata = self._build_request_metadata(
            {
                **(metadata or {}),
                "strategy": strategy,
                "query_type": query_type,
                "market_id": market_id or "",
            }
        )

        try:
            content, sources, response_metadata = await self._request_researched_response(
                prompt=prompt,
                instructions=instructions,
                model=selected_model,
                max_output_tokens=max_output_tokens or self.max_tokens,
                text_format=text_format,
                search_allowed_domains=search_allowed_domains,
                search_context_size=search_context_size,
                metadata=request_metadata,
                use_web_search=use_web_search,
            )

            self._record_request_metrics(response_metadata)

            logged_response = content
            if sources:
                logged_response = (
                    f"{content}\n\nSources:\n" + "\n".join(f"- {url}" for url in sources[:10])
                )

                await self._log_query(
                    strategy=strategy,
                    query_type=query_type,
                    role=query_type,
                    prompt=(instructions or "")[:600] + ("\n\n" if instructions else "") + prompt[:1800],
                    response=logged_response,
                    market_id=market_id,
                    tokens_used=response_metadata.total_tokens,
                    cost_usd=response_metadata.cost,
            )

            return {
                "content": content,
                "sources": sources,
                "used_web_research": bool(use_web_search),
            }

        except Exception as exc:
            log_error_with_context(
                exc,
                {
                    "model": selected_model,
                    "search_allowed_domains": search_allowed_domains or [],
                    "search_context_size": search_context_size,
                    "strategy": strategy,
                    "query_type": query_type,
                    "used_web_search": use_web_search,
                },
                "openai_researched_completion_failed",
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
        role: Optional[str] = None,
        provider: Optional[Dict[str, Any]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        plugins: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        trace: Optional[Dict[str, Any]] = None,
    ) -> Optional[TradingDecision]:
        """Obtain a structured trading decision from a direct OpenAI model."""
        del provider, plugins, session_id, trace

        if not await self._check_daily_limits():
            return None

        prompt = self._build_trading_prompt(market_data, portfolio_data, news_summary)
        selected_model = self._coerce_openai_model(model or self.default_model)

        try:
            content, response_metadata = await self._request_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=selected_model,
                temperature=0.1,
                max_tokens=4000,
                response_format=response_format or TRADING_DECISION_RESPONSE_FORMAT,
                fallback_models=fallback_models,
            )

            self._record_request_metrics(response_metadata)
            decision = self._parse_trading_decision(content)

            if decision is not None:
                await self._log_query(
                    strategy="openai",
                    query_type="trading_decision",
                    role=role or "trading_decision",
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
                    "metadata_keys": sorted((metadata or {}).keys()),
                },
                "openai_trading_decision_failed",
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
            ((yes_bid + yes_ask) / 2.0) * 100 if yes_bid and yes_ask else max(yes_bid, yes_ask) * 100
        )
        no_price = (
            ((no_bid + no_ask) / 2.0) * 100 if no_bid and no_ask else max(no_bid, no_ask) * 100
        )
        volume = get_market_volume(market_data)
        days_to_expiry = market_data.get("days_to_expiry", "Unknown")
        rules = market_data.get("rules", "No specific rules provided")

        cash = portfolio_data.get("cash", portfolio_data.get("balance", 1000))
        max_trade_value = portfolio_data.get(
            "max_trade_value",
            cash * settings.trading.max_position_size_pct / 100,
        )

        truncated_news = news_summary[:800] + "..." if len(news_summary) > 800 else news_summary

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
        """Extract a TradingDecision from model output."""
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
                int(round(float(limit_price_raw))) if limit_price_raw is not None else None
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
        role: Optional[str] = None,
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
                role=role or query_type,
                market_id=market_id,
                prompt=prompt[:2000],
                response=response[:5000],
                provider="openai",
                tokens_used=tokens_used,
                cost_usd=cost_usd,
                confidence_extracted=confidence_extracted,
                decision_extracted=decision_extracted,
            )
            asyncio.create_task(self.db_manager.log_llm_query(llm_query))
        except Exception as exc:
            self.logger.error(f"Failed to log LLM query: {exc}")

    def get_cost_summary(self) -> Dict[str, Any]:
        """Return a summary of costs across OpenAI models."""
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
            "OpenAI client closed",
            total_cost=round(self.total_cost, 6),
            total_requests=self.request_count,
        )
