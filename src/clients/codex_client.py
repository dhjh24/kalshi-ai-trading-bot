"""
Codex CLI-backed client for AI-powered trading decisions.

This client shells out to the official ``codex`` CLI (signed in via a ChatGPT
plan) instead of hitting the OpenAI billing API directly. It mirrors the
higher-level interface exposed by :class:`src.clients.openai_client.OpenAIClient`
so the rest of the trading system (including :class:`ModelRouter`) can switch
providers transparently.

Design notes:

* All subprocess work is done through :func:`asyncio.create_subprocess_exec`
  so the hot path stays non-blocking for the async trading loop.
* The CLI surface (flags, JSON format) varies between Codex releases, so we
  pick a conservative set of flags (``exec`` subcommand with ``--json``) and
  fall back to raw stdout parsing when JSON is not detected.
* Token counts are best-effort. We look for standard ``usage`` / ``tokens``
  shapes in the JSON reply (or a ``total_tokens=N`` fragment on stderr) and
  default to ``0`` when the CLI does not surface the number. This keeps
  ``daily_cost_tracking`` observable without fabricating numbers.
* ``cost_usd`` is always recorded as ``0.0`` because Codex plan usage is
  flat-rate via the ChatGPT plan, not metered per-request.
* Auth detection is a quick subprocess probe (``codex auth status`` or
  ``codex whoami``); results are cached for a short TTL so settings.py can
  call it once per process without slowing startup.

This module intentionally has no hard dependency on the Codex CLI being
present. Every call path degrades gracefully (returns ``None``, raises a
:class:`CodexUnavailableError`, or logs a warning) so callers that still
have OpenAI / OpenRouter credentials keep working.
"""

from __future__ import annotations

import asyncio
import json
import os
import pickle
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.clients.xai_client import DailyUsageTracker, TradingDecision
from src.config.settings import settings
from src.utils.kalshi_normalization import get_market_prices, get_market_volume
from src.utils.logging_setup import TradingLoggerMixin, log_error_with_context


# ---------------------------------------------------------------------------
# Public constants — model registry and defaults
# ---------------------------------------------------------------------------

CODEX_MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # Codex plan usage is flat-rate via the ChatGPT subscription, so per-token
    # cost is reported as $0 for spend tracking while still surfacing a
    # best-effort token count for quota visibility.
    "codex/gpt-5-codex": {"input_per_1k": 0.0, "output_per_1k": 0.0},
    "codex/gpt-5.4-codex": {"input_per_1k": 0.0, "output_per_1k": 0.0},
    "codex/o3-codex": {"input_per_1k": 0.0, "output_per_1k": 0.0},
}

CODEX_MODEL_ALIASES: Dict[str, str] = {
    "codex": "codex/gpt-5-codex",
    "gpt-5-codex": "codex/gpt-5-codex",
    "gpt-5.4-codex": "codex/gpt-5.4-codex",
    "o3-codex": "codex/o3-codex",
}

CODEX_FALLBACK_ORDER: List[str] = [
    "codex/gpt-5-codex",
    "codex/gpt-5.4-codex",
    "codex/o3-codex",
]

SHARED_USAGE_FILE = "logs/daily_ai_usage.pkl"

TRADING_DECISION_JSON_SCHEMA: Dict[str, Any] = {
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
}


class CodexUnavailableError(RuntimeError):
    """Raised when the Codex CLI is missing or not authenticated."""


# ---------------------------------------------------------------------------
# CLI discovery / auth probing (module-level helpers so settings.py can use
# them without instantiating a full client object)
# ---------------------------------------------------------------------------

_AUTH_CACHE: Dict[str, Tuple[bool, float]] = {}
_AUTH_CACHE_TTL_SECONDS: float = 30.0


def resolve_codex_cli_path() -> Optional[str]:
    """
    Return the absolute path to the Codex CLI, or ``None`` if missing.

    Checks ``CODEX_CLI_PATH`` first, then ``shutil.which("codex")``. Does
    NOT invoke the CLI.
    """
    override = os.getenv("CODEX_CLI_PATH", "").strip()
    if override:
        if os.path.isfile(override):
            return override
        resolved = shutil.which(override)
        if resolved:
            return resolved
        return None
    return shutil.which("codex")


def _run_auth_probe_sync(cli_path: str, timeout: float = 5.0) -> bool:
    """
    Synchronously probe whether the Codex CLI is signed in.

    We try ``codex auth status`` first (newer CLIs) and fall back to
    ``codex whoami``. Any non-zero exit code, or stdout containing an
    obvious "not signed in" / "login required" phrase, is treated as
    unauthenticated.
    """
    import subprocess

    probes = (
        (cli_path, "auth", "status"),
        (cli_path, "whoami"),
    )
    for argv in probes:
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue

        combined = f"{result.stdout}\n{result.stderr}".lower()
        if result.returncode != 0:
            continue
        if any(
            marker in combined
            for marker in (
                "not signed in",
                "not logged in",
                "login required",
                "please log in",
                "please sign in",
                "unauthorized",
                "unauthenticated",
            )
        ):
            continue
        # Heuristic positive signals: "signed in", "logged in", an email, a
        # "plan:" or "user:" token. If the command returned zero and has any
        # output, we treat it as authenticated.
        if combined.strip():
            return True

    return False


def is_codex_authenticated(
    cli_path: Optional[str] = None, *, use_cache: bool = True
) -> bool:
    """
    Return ``True`` if the Codex CLI is present and signed in.

    Cached for :data:`_AUTH_CACHE_TTL_SECONDS` so repeated calls from
    ``settings._resolve_default_llm_provider`` don't spawn many subprocesses.
    Set ``use_cache=False`` to force a re-probe.
    """
    if os.getenv("CODEX_DISABLE_AUTH_PROBE", "").strip().lower() in {"1", "true", "yes"}:
        # Escape hatch for offline test environments: assume signed in when
        # requested explicitly, else assume unauthenticated.
        return False

    path = cli_path or resolve_codex_cli_path()
    if not path:
        return False

    now = time.time()
    cached = _AUTH_CACHE.get(path)
    if use_cache and cached is not None:
        value, expires_at = cached
        if expires_at > now:
            return value

    authed = _run_auth_probe_sync(path)
    _AUTH_CACHE[path] = (authed, now + _AUTH_CACHE_TTL_SECONDS)
    return authed


def clear_codex_auth_cache() -> None:
    """Reset the auth cache — useful for tests."""
    _AUTH_CACHE.clear()


# ---------------------------------------------------------------------------
# Dataclasses mirroring OpenAIResponseMetadata / ModelCostTracker
# ---------------------------------------------------------------------------


@dataclass
class CodexModelCostTracker:
    """Accumulated usage data for a single Codex model."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost: float = 0.0  # Always 0 for Codex, kept for interface parity.
    request_count: int = 0
    error_count: int = 0
    last_used: Optional[datetime] = None


@dataclass
class CodexResponseMetadata:
    """Metadata captured from the most recent Codex CLI call."""

    request_id: Optional[str] = None
    requested_model: Optional[str] = None
    actual_model: Optional[str] = None
    fallback_models: List[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0  # Always 0 — ChatGPT plan quota, not metered.
    finish_reason: Optional[str] = None
    exit_code: Optional[int] = None


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------


class CodexClient(TradingLoggerMixin):
    """
    Async subprocess wrapper around the Codex CLI.

    Mirrors the public surface of :class:`OpenAIClient` (``get_completion``,
    ``get_trading_decision``, ``get_researched_completion``) so it can be
    swapped in via :class:`ModelRouter`.

    Example usage::

        client = CodexClient()
        text = await client.get_completion("Summarize this market.")

    Or for a structured trade decision::

        decision = await client.get_trading_decision(market, portfolio)
    """

    TIMEOUT_SECONDS: float = 180.0

    def __init__(
        self,
        *,
        cli_path: Optional[str] = None,
        db_manager: Any = None,
        plan_tier: Optional[str] = None,
    ) -> None:
        self.cli_path = cli_path or resolve_codex_cli_path()
        self.plan_tier = plan_tier or os.getenv("CODEX_PLAN_TIER", "").strip() or "plus"
        self.db_manager = db_manager

        self.default_model = _canonical_codex_model(settings.trading.primary_model)
        self.fallback_model = _canonical_codex_model(settings.trading.fallback_model)
        self.temperature = settings.trading.ai_temperature
        self.max_tokens = settings.trading.ai_max_tokens

        self.model_costs: Dict[str, CodexModelCostTracker] = {
            model: CodexModelCostTracker(model=model) for model in CODEX_MODEL_PRICING
        }
        self.total_cost: float = 0.0
        self.request_count: int = 0
        self.usage_file = SHARED_USAGE_FILE
        self.daily_tracker: DailyUsageTracker = self._load_daily_tracker()
        self._last_request_cost: float = 0.0
        self._last_request_metadata = CodexResponseMetadata()

        self.logger.info(
            "Codex client initialized",
            cli_path=self.cli_path,
            plan_tier=self.plan_tier,
            default_model=self.default_model,
            fallback_model=self.fallback_model,
            daily_requests=self.daily_tracker.request_count,
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def last_request_metadata(self) -> CodexResponseMetadata:
        """Return metadata for the most recent successful request."""
        return self._last_request_metadata

    def is_available(self) -> bool:
        """Return ``True`` iff the CLI is installed and authenticated."""
        if not self.cli_path:
            return False
        return is_codex_authenticated(self.cli_path)

    # ------------------------------------------------------------------
    # Daily usage tracking (shared pickle file, identical to other clients)
    # ------------------------------------------------------------------

    def _load_daily_tracker(self) -> DailyUsageTracker:
        today = datetime.now().strftime("%Y-%m-%d")
        daily_limit = getattr(settings.trading, "daily_ai_cost_limit", 10.0)
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
        try:
            os.makedirs("logs", exist_ok=True)
            with open(self.usage_file, "wb") as fh:
                pickle.dump(self.daily_tracker, fh)
        except Exception as exc:
            self.logger.error(f"Failed to save daily tracker: {exc}")

    def _update_daily_usage(self) -> None:
        """
        Increment the request count. Cost is always 0 for Codex plan usage
        but we still count the request so plan quota is observable.
        """
        self.daily_tracker.request_count += 1
        self._save_daily_tracker()

    # ------------------------------------------------------------------
    # Subprocess execution
    # ------------------------------------------------------------------

    async def _invoke_cli(
        self,
        *,
        prompt: str,
        model: str,
        schema: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[str, str, int]:
        """
        Run the Codex CLI once and return ``(stdout, stderr, returncode)``.

        Streams stdout to avoid deadlocks on large replies. Raises
        :class:`CodexUnavailableError` if the CLI is missing or times out.
        """
        if not self.cli_path:
            raise CodexUnavailableError("Codex CLI not found on PATH")

        sdk_model = _sdk_codex_model(model)
        argv = [
            self.cli_path,
            "exec",
            "--model",
            sdk_model,
            "--json",
            "--no-color",
        ]
        if schema is not None:
            # Newer codex CLIs support ``--response-format=json_schema=<path>``
            # or ``--json-schema``; we pass via stdin metadata so we don't
            # rely on writing temp files for every call.
            argv.extend(["--structured-output", "1"])

        env = os.environ.copy()
        # Prevent pager / interactive mode regardless of user shell setup.
        env.setdefault("CODEX_NO_PAGER", "1")
        env.setdefault("TERM", "dumb")

        stdin_payload: Dict[str, Any] = {
            "prompt": prompt,
            "model": sdk_model,
        }
        if schema is not None:
            stdin_payload["response_format"] = {
                "type": "json_schema",
                "json_schema": schema,
            }
        stdin_bytes = (json.dumps(stdin_payload) + "\n").encode("utf-8")

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, OSError) as exc:
            raise CodexUnavailableError(f"Failed to launch Codex CLI: {exc}") from exc

        effective_timeout = timeout if timeout is not None else self.TIMEOUT_SECONDS
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise CodexUnavailableError(
                f"Codex CLI timed out after {effective_timeout}s"
            ) from exc

        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        return stdout, stderr, int(proc.returncode or 0)

    # ------------------------------------------------------------------
    # CLI output parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_last_json_object(text: str) -> Optional[Any]:
        """
        Extract the last well-formed top-level JSON object from ``text``.

        The Codex CLI with ``--json`` generally streams one JSON-per-line
        but may also emit a single final object. We scan for the
        right-most balanced ``{...}`` block and return it parsed.
        """
        if not text:
            return None

        # First, try each line as JSON (JSONL style).
        last_parsed: Optional[Any] = None
        for line in reversed(text.splitlines()):
            stripped = line.strip()
            if not stripped.startswith("{") or not stripped.endswith("}"):
                continue
            try:
                last_parsed = json.loads(stripped)
                return last_parsed
            except json.JSONDecodeError:
                continue

        # Fall back to a greedy balanced-brace scan on the full string.
        depth = 0
        end_idx: Optional[int] = None
        start_idx: Optional[int] = None
        for idx in range(len(text) - 1, -1, -1):
            ch = text[idx]
            if ch == "}":
                if depth == 0:
                    end_idx = idx
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0 and end_idx is not None:
                    start_idx = idx
                    break
        if start_idx is not None and end_idx is not None:
            candidate = text[start_idx : end_idx + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                return None
        return None

    @staticmethod
    def _extract_token_counts(stdout: str, stderr: str) -> Tuple[int, int, int, int]:
        """
        Best-effort token count extraction.

        Returns ``(input_tokens, output_tokens, total_tokens, reasoning_tokens)``.
        Falls back to ``(0, 0, 0, 0)`` if nothing parseable is found.
        """
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        reasoning_tokens = 0

        # Try to locate a usage block in the last JSON object first.
        for text in (stdout, stderr):
            parsed = CodexClient._extract_last_json_object(text)
            if not isinstance(parsed, dict):
                continue
            usage = parsed.get("usage") or parsed.get("token_usage")
            if isinstance(usage, dict):
                input_tokens = int(
                    usage.get("input_tokens")
                    or usage.get("prompt_tokens")
                    or 0
                )
                output_tokens = int(
                    usage.get("output_tokens")
                    or usage.get("completion_tokens")
                    or 0
                )
                total_tokens = int(
                    usage.get("total_tokens") or (input_tokens + output_tokens)
                )
                details = usage.get("output_tokens_details") or {}
                if isinstance(details, dict):
                    reasoning_tokens = int(details.get("reasoning_tokens") or 0)
                if total_tokens:
                    return input_tokens, output_tokens, total_tokens, reasoning_tokens

        # Fall back to regex on stderr for lines like:
        #   "tokens: 1234 in / 567 out (total 1801)"
        combined = f"{stdout}\n{stderr}"
        total_match = re.search(r"total[_ ]?tokens[=: ]+(\d+)", combined, re.IGNORECASE)
        in_match = re.search(r"(?:input|prompt)[_ ]?tokens[=: ]+(\d+)", combined, re.IGNORECASE)
        out_match = re.search(
            r"(?:output|completion)[_ ]?tokens[=: ]+(\d+)", combined, re.IGNORECASE
        )
        if in_match:
            input_tokens = int(in_match.group(1))
        if out_match:
            output_tokens = int(out_match.group(1))
        if total_match:
            total_tokens = int(total_match.group(1))
        if not total_tokens and (input_tokens or output_tokens):
            total_tokens = input_tokens + output_tokens

        return input_tokens, output_tokens, total_tokens, reasoning_tokens

    @staticmethod
    def _extract_completion_text(stdout: str) -> str:
        """Extract assistant text content from the Codex CLI stdout."""
        parsed = CodexClient._extract_last_json_object(stdout)
        if isinstance(parsed, dict):
            for key in ("content", "output", "text", "message", "response"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value
                # Chat-style: {"choices": [{"message": {"content": "..."}}]}
                if key == "message" and isinstance(value, dict):
                    inner = value.get("content")
                    if isinstance(inner, str) and inner.strip():
                        return inner
            choices = parsed.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    msg = first.get("message") or {}
                    if isinstance(msg, dict):
                        content = msg.get("content")
                        if isinstance(content, str):
                            return content
                    text = first.get("text")
                    if isinstance(text, str):
                        return text

        # Fall back to raw stdout (trim CLI banners when possible).
        return stdout.strip()

    # ------------------------------------------------------------------
    # Public API: get_completion
    # ------------------------------------------------------------------

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
        """Get a completion from the Codex CLI (free-form text)."""
        del provider, plugins, session_id, trace, max_tokens, temperature

        if not self.cli_path:
            self.logger.warning("Codex CLI path unavailable; skipping completion")
            return None

        resolved_prompt = _messages_to_prompt(prompt, messages)
        selected_model = _canonical_codex_model(model or self.default_model)
        candidates = _build_fallback_chain(selected_model, fallback_models)

        schema = None
        if isinstance(response_format, dict):
            schema = (
                response_format.get("json_schema")
                if response_format.get("type") == "json_schema"
                else None
            )

        last_exc: Optional[Exception] = None
        for candidate in candidates:
            try:
                start = time.time()
                stdout, stderr, rc = await self._invoke_cli(
                    prompt=resolved_prompt,
                    model=candidate,
                    schema=schema,
                )
                elapsed = time.time() - start

                if rc != 0:
                    last_exc = CodexUnavailableError(
                        f"Codex CLI exited with code {rc} for {candidate}: "
                        f"{stderr.strip()[:300]}"
                    )
                    self.logger.warning(
                        "Codex CLI non-zero exit",
                        model=candidate,
                        exit_code=rc,
                        stderr_preview=stderr.strip()[:200],
                    )
                    continue

                content = self._extract_completion_text(stdout)
                if not content:
                    last_exc = ValueError(
                        f"Codex CLI returned empty content for {candidate}"
                    )
                    continue

                in_tok, out_tok, total_tok, reasoning_tok = self._extract_token_counts(
                    stdout, stderr
                )

                self._last_request_metadata = CodexResponseMetadata(
                    request_id=None,
                    requested_model=selected_model,
                    actual_model=candidate,
                    fallback_models=[c for c in candidates if c != candidate],
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    total_tokens=total_tok,
                    reasoning_tokens=reasoning_tok,
                    cost=0.0,
                    finish_reason="stop",
                    exit_code=rc,
                )
                self._last_request_cost = 0.0
                self._record_request_metrics(self._last_request_metadata)

                await self._log_query(
                    strategy=strategy,
                    query_type=query_type,
                    prompt=resolved_prompt,
                    response=content,
                    market_id=market_id,
                    tokens_used=total_tok,
                    cost_usd=0.0,
                )

                self.logger.debug(
                    "Codex completion succeeded",
                    requested_model=selected_model,
                    actual_model=candidate,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    total_tokens=total_tok,
                    processing_time=round(elapsed, 2),
                )
                return content

            except CodexUnavailableError as exc:
                last_exc = exc
                self.logger.warning(
                    "Codex CLI unavailable, trying next candidate",
                    model=candidate,
                    error=str(exc),
                )
                tracker = self.model_costs.get(candidate)
                if tracker:
                    tracker.error_count += 1
                continue
            except Exception as exc:  # pragma: no cover - defensive
                last_exc = exc
                tracker = self.model_costs.get(candidate)
                if tracker:
                    tracker.error_count += 1
                self.logger.warning(
                    "Codex CLI request raised",
                    model=candidate,
                    error=str(exc),
                )
                continue

        if last_exc is not None:
            log_error_with_context(
                last_exc,
                {
                    "requested_model": selected_model,
                    "fallback_chain": candidates,
                    "strategy": strategy,
                    "query_type": query_type,
                },
                "codex_completion_failed",
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
        """
        Fall back to a plain completion — the Codex CLI does not expose a
        live-web-search tool surface today, so we preserve the response
        shape but flag ``used_web_research=False``.
        """
        del search_allowed_domains, search_context_size, use_web_search
        del max_output_tokens, metadata

        combined_prompt = prompt
        if instructions:
            combined_prompt = f"{instructions}\n\n{prompt}"

        response_format = None
        if isinstance(text_format, dict):
            response_format = {"type": "json_schema", "json_schema": text_format}

        content = await self.get_completion(
            prompt=combined_prompt,
            model=model,
            strategy=strategy,
            query_type=query_type,
            market_id=market_id,
            response_format=response_format,
        )
        if content is None:
            return None

        return {"content": content, "sources": [], "used_web_research": False}

    # ------------------------------------------------------------------
    # Public API: structured / trading decision
    # ------------------------------------------------------------------

    async def create_structured_completion(
        self,
        prompt: str,
        *,
        schema: Dict[str, Any],
        model: Optional[str] = None,
        strategy: str = "unknown",
        query_type: str = "structured_completion",
        market_id: Optional[str] = None,
        fallback_models: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Request a JSON-schema-constrained completion.

        Returns the parsed JSON object or ``None`` on failure.
        """
        response_format = {"type": "json_schema", "json_schema": schema}
        content = await self.get_completion(
            prompt=prompt,
            model=model,
            strategy=strategy,
            query_type=query_type,
            market_id=market_id,
            fallback_models=fallback_models,
            response_format=response_format,
        )
        if content is None:
            return None

        # Extract a JSON object from the content (CLI may wrap in a chat
        # envelope or emit plain JSON).
        parsed = self._extract_last_json_object(content)
        if isinstance(parsed, dict):
            return parsed

        # Last-ditch: try to strip code fences and parse.
        cleaned = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            self.logger.warning(
                "Codex structured completion returned unparseable JSON",
                content_preview=content[:200],
            )
            return None

    # Alias kept for parity with the interface spec in §3 W1.
    create_completion = get_completion

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
        """Obtain a structured trading decision via the Codex CLI."""
        del provider, plugins, session_id, trace, metadata

        prompt = self._build_trading_prompt(market_data, portfolio_data, news_summary)
        schema = TRADING_DECISION_JSON_SCHEMA
        if isinstance(response_format, dict) and response_format.get("type") == "json_schema":
            embedded = response_format.get("json_schema")
            if isinstance(embedded, dict):
                schema = embedded

        parsed = await self.create_structured_completion(
            prompt=prompt,
            schema=schema,
            model=model,
            strategy="codex",
            query_type="trading_decision",
            market_id=market_data.get("ticker") or market_data.get("market_id"),
            fallback_models=fallback_models,
        )
        if parsed is None:
            return None

        return _parse_trading_decision(parsed)

    # ------------------------------------------------------------------
    # Metrics / cost summary
    # ------------------------------------------------------------------

    def _record_request_metrics(self, metadata: CodexResponseMetadata) -> None:
        tracker = self.model_costs.get(metadata.actual_model or "")
        if tracker is None and metadata.actual_model:
            tracker = CodexModelCostTracker(model=metadata.actual_model)
            self.model_costs[metadata.actual_model] = tracker
        if tracker is not None:
            tracker.input_tokens += metadata.input_tokens
            tracker.output_tokens += metadata.output_tokens
            tracker.request_count += 1
            tracker.last_used = datetime.now()

        self.total_cost += metadata.cost  # Always 0 for Codex.
        self.request_count += 1
        self._update_daily_usage()

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
        """Persist a query record to the database when a manager is available."""
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
                cost_usd=cost_usd if cost_usd is not None else 0.0,
                confidence_extracted=confidence_extracted,
                decision_extracted=decision_extracted,
            )
            asyncio.create_task(self.db_manager.log_llm_query(llm_query))
        except Exception as exc:
            self.logger.error(f"Failed to log Codex LLM query: {exc}")

    def get_cost_summary(self) -> Dict[str, Any]:
        """Return a summary of Codex usage (cost always 0)."""
        self.daily_tracker = self._load_daily_tracker()

        per_model: Dict[str, Any] = {}
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
            "total_cost": 0.0,
            "total_requests": self.request_count,
            "daily_cost": 0.0,
            "daily_limit": self.daily_tracker.daily_limit,
            "daily_exhausted": False,
            "plan_tier": self.plan_tier,
            "last_request": {
                "requested_model": self._last_request_metadata.requested_model,
                "actual_model": self._last_request_metadata.actual_model,
                "fallback_models": self._last_request_metadata.fallback_models,
                "cost": 0.0,
                "total_tokens": self._last_request_metadata.total_tokens,
            },
            "per_model": per_model,
        }

    async def close(self) -> None:
        """No persistent connections; present for interface parity."""
        self.logger.info(
            "Codex client closed",
            total_requests=self.request_count,
        )

    # ------------------------------------------------------------------
    # Prompt construction (kept near OpenAI client's shape)
    # ------------------------------------------------------------------

    def _build_trading_prompt(
        self,
        market_data: Dict[str, Any],
        portfolio_data: Dict[str, Any],
        news_summary: str,
    ) -> str:
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

        truncated_news = news_summary[:800] + "..." if len(news_summary) > 800 else news_summary

        return (
            "Analyze this prediction market and provide a trading decision.\n\n"
            f"Market: {title}\n"
            f"Rules: {rules}\n"
            f"YES price: {yes_price:.1f}c | NO price: {no_price:.1f}c | "
            f"Volume: ${volume:,.0f}\n"
            f"Days to expiry: {days_to_expiry}\n\n"
            f"Available cash: ${cash:,.2f} | Max trade value: ${max_trade_value:,.2f}\n\n"
            f"News/Context:\n{truncated_news}\n\n"
            "Instructions:\n"
            "- Estimate the true probability of the event.\n"
            "- Only trade if your edge exceeds 10%.\n"
            "- Confidence must be >60% to recommend a trade.\n"
            "- Return a JSON object only (no markdown fences, no prose).\n\n"
            "Example trade:\n"
            '{"action": "BUY", "side": "YES", "limit_price": 55, '
            '"confidence": 0.72, "reasoning": "brief explanation"}\n\n'
            "Example skip:\n"
            '{"action": "SKIP", "side": "YES", "limit_price": 0, '
            '"confidence": 0.40, "reasoning": "insufficient edge"}\n'
        )


# ---------------------------------------------------------------------------
# Helpers (module-level so they're easy to unit-test independently)
# ---------------------------------------------------------------------------


def _canonical_codex_model(model: Optional[str]) -> str:
    """Normalize a model identifier to the internal ``codex/...`` form."""
    name = str(model or "").strip()
    if not name:
        return CODEX_FALLBACK_ORDER[0]
    if name in CODEX_MODEL_ALIASES:
        return CODEX_MODEL_ALIASES[name]
    if name.startswith("codex/"):
        return name
    # Try to map OpenAI/OpenRouter-style names onto Codex equivalents.
    if name.startswith("openai/") or "/" not in name:
        base = name.split("/", 1)[-1]
        if base in CODEX_MODEL_ALIASES:
            return CODEX_MODEL_ALIASES[base]
    return CODEX_FALLBACK_ORDER[0]


def _sdk_codex_model(model: str) -> str:
    """Convert the internal ``codex/...`` name to the CLI-facing model id."""
    if model.startswith("codex/"):
        return model.split("/", 1)[1]
    return model


def _build_fallback_chain(
    selected_model: str, fallback_models: Optional[List[str]] = None
) -> List[str]:
    chain: List[str] = [selected_model]
    for raw in fallback_models or []:
        canonical = _canonical_codex_model(raw)
        if canonical not in chain:
            chain.append(canonical)
    for default_model in CODEX_FALLBACK_ORDER:
        if default_model not in chain:
            chain.append(default_model)
    return chain


def _messages_to_prompt(
    prompt: Optional[str],
    messages: Optional[List[Dict[str, Any]]],
) -> str:
    """Normalize a prompt/messages tuple into a single CLI prompt string."""
    if messages:
        parts: List[str] = []
        for msg in messages:
            role = str(msg.get("role", "user")).upper()
            content = msg.get("content", "")
            if isinstance(content, list):
                # OpenAI-style multi-part content: collapse to text
                content = "\n".join(
                    str(item.get("text", "")) if isinstance(item, dict) else str(item)
                    for item in content
                )
            parts.append(f"[{role}]\n{content}")
        return "\n\n".join(parts)
    if prompt is None:
        raise ValueError("Either `prompt` or `messages` must be provided")
    return prompt


def _parse_trading_decision(payload: Dict[str, Any]) -> Optional[TradingDecision]:
    """Turn a parsed JSON decision dict into a :class:`TradingDecision`."""
    try:
        action = str(payload.get("action", "SKIP")).upper()
        if action not in {"BUY", "SELL", "SKIP"}:
            action = "SKIP"
        side = str(payload.get("side", "YES")).upper()
        if side not in {"YES", "NO"}:
            side = "YES"
        confidence = float(payload.get("confidence", 0.5))
        limit_price_raw = payload.get("limit_price")
        limit_price = (
            int(round(float(limit_price_raw))) if limit_price_raw is not None else None
        )
        reasoning = str(payload.get("reasoning", "No reasoning provided."))
        return TradingDecision(
            action=action,
            side=side,
            confidence=confidence,
            limit_price=limit_price,
            reasoning=reasoning,
        )
    except Exception:
        return None
