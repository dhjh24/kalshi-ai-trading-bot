"""
Configuration settings for the Kalshi trading system.
Manages trading parameters, API configurations, and risk management settings.
"""

import os
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def _get_bool_env(name: str, default: bool = False) -> bool:
    """Return a boolean environment variable value."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _get_csv_env_list(name: str) -> List[str]:
    """Return a comma-separated environment variable as a cleaned list."""
    raw_value = os.getenv(name, "")
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def _get_kalshi_env() -> str:
    """Return the configured Kalshi environment."""
    return os.getenv("KALSHI_ENV", "prod").strip().lower() or "prod"


def _get_kalshi_base_url() -> str:
    """Resolve the Kalshi REST base URL from env settings."""
    override = os.getenv("KALSHI_API_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")

    env_name = _get_kalshi_env()
    if env_name == "demo":
        return "https://demo-api.kalshi.co"
    return "https://api.elections.kalshi.com"


def _get_llm_provider() -> str:
    """Return the requested LLM provider mode."""
    return os.getenv("LLM_PROVIDER", "auto").strip().lower() or "auto"


def _resolve_default_llm_provider() -> str:
    """
    Resolve the effective provider from environment state.

    `auto` prefers direct OpenAI access when an API key is configured, then
    falls back to OpenRouter.
    """
    provider = _get_llm_provider()
    if provider != "auto":
        return provider
    if os.getenv("OPENAI_API_KEY", "").strip():
        return "openai"
    return "openrouter"


def _get_default_primary_model() -> str:
    """Return the default primary model for the active provider."""
    env_override = os.getenv("PRIMARY_MODEL", "").strip()
    if env_override:
        return env_override

    if _resolve_default_llm_provider() == "openai":
        return "openai/gpt-5.4"
    return "anthropic/claude-sonnet-4.5"


def _get_default_fallback_model() -> str:
    """Return the default fallback model for the active provider."""
    env_override = os.getenv("FALLBACK_MODEL", "").strip()
    if env_override:
        return env_override

    if _resolve_default_llm_provider() == "openai":
        return "openai/o3"
    return "deepseek/deepseek-v3.2"


def _get_default_sentiment_model() -> str:
    """Return the default sentiment model for the active provider."""
    env_override = os.getenv("SENTIMENT_MODEL", "").strip()
    if env_override:
        return env_override

    if _resolve_default_llm_provider() == "openai":
        return "openai/gpt-4.1"
    return "google/gemini-3.1-flash-lite-preview"


@dataclass
class APIConfig:
    """API configuration settings."""
    kalshi_api_key: str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY", ""))
    kalshi_env: str = field(default_factory=_get_kalshi_env)
    kalshi_base_url: str = field(default_factory=_get_kalshi_base_url)
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openrouter_api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    llm_provider: str = field(default_factory=_get_llm_provider)
    openai_base_url: str = "https://api.openai.com/v1"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_http_referer: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_HTTP_REFERER", "").strip()
    )
    openrouter_title: str = field(
        default_factory=lambda: os.getenv(
            "OPENROUTER_TITLE", "Kalshi AI Trading Bot"
        ).strip()
    )

    # xai_api_key removed — all models now route through OpenRouter

    def get_openrouter_headers(self) -> Dict[str, str]:
        """Return optional OpenRouter attribution headers."""
        headers: Dict[str, str] = {}
        if self.openrouter_http_referer:
            headers["HTTP-Referer"] = self.openrouter_http_referer
        if self.openrouter_title:
            headers["X-OpenRouter-Title"] = self.openrouter_title
        return headers

    def resolve_llm_provider(self) -> str:
        """Return the effective provider after applying `auto` fallback rules."""
        if self.llm_provider != "auto":
            return self.llm_provider
        if self.openai_api_key:
            return "openai"
        return "openrouter"


@dataclass
class EnsembleConfig:
    """Multi-model ensemble configuration."""
    enabled: bool = True
    # Model roster for ensemble decisions — all via OpenRouter (April 2026)
    models: Dict[str, Dict] = field(default_factory=lambda: {
        "anthropic/claude-sonnet-4.5": {"provider": "openrouter", "role": "news_analyst", "weight": 0.30},
        "google/gemini-3.1-pro-preview": {"provider": "openrouter", "role": "forecaster", "weight": 0.30},
        "openai/gpt-5.4": {"provider": "openrouter", "role": "risk_manager", "weight": 0.20},
        "deepseek/deepseek-v3.2": {"provider": "openrouter", "role": "bull_researcher", "weight": 0.10},
        "x-ai/grok-4.1-fast": {"provider": "openrouter", "role": "bear_researcher", "weight": 0.10},
    })
    trader_model: str = "x-ai/grok-4.1-fast"
    min_models_for_consensus: int = 3
    disagreement_threshold: float = 0.25  # Std dev above this = low confidence
    parallel_requests: bool = True
    debate_enabled: bool = True
    calibration_tracking: bool = True
    max_ensemble_cost: float = 0.50  # Max cost per ensemble decision

    @staticmethod
    def _normalize_role(role: str) -> str:
        """Normalize legacy role names to the current agent role set."""
        role = str(role or "").strip()
        if role == "lead_analyst":
            return "news_analyst"
        return role

    def normalized_models(self) -> Dict[str, Dict[str, Any]]:
        """Return the configured model map with normalized role names."""
        normalized: Dict[str, Dict[str, Any]] = {}
        for model_id, cfg in self.models.items():
            role = self._normalize_role(cfg.get("role", ""))
            normalized[model_id] = {
                **cfg,
                "role": role,
                "weight": float(cfg.get("weight", 0.0)),
            }
        return normalized

    def get_role_model_map(self) -> Dict[str, str]:
        """Return role -> model mapping used by the ensemble/debate system."""
        provider = settings.api.resolve_llm_provider()
        if provider == "openai":
            role_map = {
                cfg["role"]: model_id
                for model_id, cfg in self.normalized_models().items()
                if cfg["role"] and cfg.get("provider") == "openai"
            }
            openai_defaults = {
                "forecaster": "openai/gpt-4.1",
                "news_analyst": "openai/o3",
                "bull_researcher": "openai/o3",
                "bear_researcher": "openai/gpt-4.1",
                "risk_manager": "openai/gpt-5.4",
                "trader": "openai/gpt-5.4",
            }
            role_map = {**openai_defaults, **role_map}
        else:
            role_map = {
                cfg["role"]: model_id
                for model_id, cfg in self.normalized_models().items()
                if cfg["role"]
            }

        role_map["trader"] = role_map.get("trader", self.trader_model)
        return role_map

    def get_role_weights(self) -> Dict[str, float]:
        """Return role -> weight mapping for weighted ensemble aggregation."""
        return {
            cfg["role"]: float(cfg.get("weight", 0.0))
            for cfg in self.normalized_models().values()
            if cfg["role"]
        }


@dataclass
class SentimentConfig:
    """News and sentiment analysis configuration."""
    enabled: bool = True
    rss_feeds: List[str] = field(default_factory=lambda: [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
    ])
    sentiment_model: str = field(default_factory=_get_default_sentiment_model)
    cache_ttl_minutes: int = 30
    max_articles_per_source: int = 10
    relevance_threshold: float = 0.3


# Trading strategy configuration — DISCIPLINED DEFAULTS (sane risk management)
# Beast mode is still available via --beast flag, but NOT the default.
# Discipline defaults based on live prediction market trading experience.
# NCAAB NO-side: 74% WR, +10% ROI — ONLY profitable category.
# Economic trades: -70% ROI, 78% of all losses.
@dataclass
class TradingConfig:
    """Trading strategy configuration."""
    # Position sizing and risk management — DISCIPLINED DEFAULTS
    max_position_size_pct: float = 3.0  # SANE: 3% per position (was 5% "beast mode")
    max_daily_loss_pct: float = 10.0    # SANE: 10% daily loss limit (was 15%)
    max_positions: int = 10              # SANE: 10 concurrent positions (was 15)
    min_balance: float = 100.0          # SANE: $100 minimum balance (was $50)
    
    # Market filtering criteria — DISCIPLINED
    min_volume: float = 500.0           # SANE: Higher volume requirement (was 200 beast mode)
    max_time_to_expiry_days: int = 14   # SANE: Shorter timeframes (was 30)
    
    # AI decision making — DATA-DRIVEN THRESHOLDS  
    min_confidence_to_trade: float = 0.45   # LOOSENED: 45% confidence minimum (was 60%, approved 2026-03-29)
                                           # Based on analysis: 65% was too conservative, bot finding 0 eligible markets
                                           # NCAAB NO-side showed 74% WR at +10% ROI, suggesting value at lower thresholds
    
    # Category-specific confidence adjustments (applied as multipliers to base threshold)
    category_confidence_adjustments: Dict[str, float] = field(default_factory=lambda: {
        "sports": 0.90,      # Sports showed best performance (NCAAB 74% WR), lower threshold
        "economics": 1.15,   # Economics showed -70% ROI, higher threshold required  
        "politics": 1.05,    # Slight increase for political volatility
        "default": 1.0       # Base multiplier for other categories
    })
    
    scan_interval_seconds: int = 60      # SANE: 60-second scan interval (was 30)
    
    # AI model configuration
    primary_model: str = field(default_factory=_get_default_primary_model)
    fallback_model: str = field(default_factory=_get_default_fallback_model)
    ai_temperature: float = 0  # Lower temperature for more consistent JSON output
    ai_max_tokens: int = 8000    # Reasonable limit for reasoning-oriented models
    
    # Position sizing (LEGACY - now using Kelly-primary approach)
    default_position_size: float = 3.0  # REDUCED: Now using Kelly Criterion as primary method (was 5%, now 3%)
    position_size_multiplier: float = 1.0  # Multiplier for AI confidence
    
    # Kelly Criterion settings (PRIMARY position sizing method) — DISCIPLINED
    use_kelly_criterion: bool = True        # Use Kelly Criterion for position sizing (PRIMARY METHOD)
    kelly_fraction: float = 0.25            # SANE: Quarter-Kelly (was 0.75 beast mode — gambling)
    max_single_position: float = 0.03       # SANE: 3% max position cap (was 0.05 beast mode)
    
    # Live trading mode control
    live_trading_enabled: bool = field(default_factory=lambda: _get_bool_env("LIVE_TRADING_ENABLED"))
    paper_trading_mode: bool = field(default_factory=lambda: not _get_bool_env("LIVE_TRADING_ENABLED"))
    
    # Trading frequency - MORE FREQUENT
    market_scan_interval: int = 30          # DECREASED: Scan every 30 seconds (was 60)
    position_check_interval: int = 15       # DECREASED: Check positions every 15 seconds (was 30)
    max_trades_per_hour: int = 20           # INCREASED: Allow more trades per hour (was 10, now 20)
    run_interval_minutes: int = 10          # DECREASED: Run more frequently (was 15, now 10)
    num_processor_workers: int = 5      # Number of concurrent market processor workers

    # Unified strategy capital allocation
    market_making_allocation: float = field(
        default_factory=lambda: float(os.getenv("MARKET_MAKING_ALLOCATION", "0.40"))
    )
    directional_allocation: float = field(
        default_factory=lambda: float(os.getenv("DIRECTIONAL_ALLOCATION", "0.50"))
    )
    arbitrage_allocation: float = field(
        default_factory=lambda: float(os.getenv("ARBITRAGE_ALLOCATION", "0.10"))
    )
    
    # Market selection preferences
    preferred_categories: List[str] = field(default_factory=lambda: _get_csv_env_list("PREFERRED_CATEGORIES"))
    excluded_categories: List[str] = field(default_factory=lambda: _get_csv_env_list("EXCLUDED_CATEGORIES"))
    prefer_live_wagering: bool = field(default_factory=lambda: _get_bool_env("PREFER_LIVE_WAGERING"))
    live_wagering_max_hours_to_expiry: int = field(
        default_factory=lambda: int(os.getenv("LIVE_WAGERING_MAX_HOURS_TO_EXPIRY", "12"))
    )
    
    # High-confidence, near-expiry strategy
    enable_high_confidence_strategy: bool = True
    high_confidence_threshold: float = 0.95  # LLM confidence needed
    high_confidence_market_odds: float = 0.90 # Market price to look for
    high_confidence_expiry_hours: int = 24   # Max hours until expiry

    # AI trading criteria - MORE PERMISSIVE
    max_analysis_cost_per_decision: float = 0.15  # INCREASED: Allow higher cost per decision (was 0.10, now 0.15)
    min_confidence_threshold: float = 0.45  # DECREASED: Lower confidence threshold (was 0.55, now 0.45)

    # Quick flip strategy
    enable_quick_flip: bool = field(default_factory=lambda: _get_bool_env("ENABLE_QUICK_FLIP"))
    enable_live_quick_flip: bool = field(
        default_factory=lambda: _get_bool_env("ENABLE_LIVE_QUICK_FLIP")
    )
    quick_flip_allocation: float = field(
        default_factory=lambda: float(os.getenv("QUICK_FLIP_ALLOCATION", "0.0"))
    )
    quick_flip_min_entry_price: float = field(
        default_factory=lambda: float(os.getenv("QUICK_FLIP_MIN_ENTRY_PRICE", "0.01"))
    )
    quick_flip_max_entry_price: float = field(
        default_factory=lambda: float(os.getenv("QUICK_FLIP_MAX_ENTRY_PRICE", "0.20"))
    )
    quick_flip_min_profit_margin: float = field(
        default_factory=lambda: float(os.getenv("QUICK_FLIP_MIN_PROFIT_MARGIN", "0.10"))
    )
    quick_flip_max_position_size: int = field(
        default_factory=lambda: int(os.getenv("QUICK_FLIP_MAX_POSITION_SIZE", "100"))
    )
    quick_flip_max_concurrent_positions: int = field(
        default_factory=lambda: int(os.getenv("QUICK_FLIP_MAX_CONCURRENT_POSITIONS", "50"))
    )
    quick_flip_capital_per_trade: float = field(
        default_factory=lambda: float(os.getenv("QUICK_FLIP_CAPITAL_PER_TRADE", "50.0"))
    )
    quick_flip_confidence_threshold: float = field(
        default_factory=lambda: float(os.getenv("QUICK_FLIP_CONFIDENCE_THRESHOLD", "0.6"))
    )
    quick_flip_max_hold_minutes: int = field(
        default_factory=lambda: int(os.getenv("QUICK_FLIP_MAX_HOLD_MINUTES", "30"))
    )
    quick_flip_min_market_volume: int = field(
        default_factory=lambda: int(os.getenv("QUICK_FLIP_MIN_MARKET_VOLUME", "2000"))
    )
    quick_flip_max_hours_to_expiry: int = field(
        default_factory=lambda: int(os.getenv("QUICK_FLIP_MAX_HOURS_TO_EXPIRY", "72"))
    )
    quick_flip_max_bid_ask_spread: float = field(
        default_factory=lambda: float(os.getenv("QUICK_FLIP_MAX_BID_ASK_SPREAD", "0.03"))
    )
    quick_flip_min_top_of_book_size: int = field(
        default_factory=lambda: int(os.getenv("QUICK_FLIP_MIN_TOP_OF_BOOK_SIZE", "25"))
    )
    quick_flip_min_net_profit: float = field(
        default_factory=lambda: float(os.getenv("QUICK_FLIP_MIN_NET_PROFIT", "0.10"))
    )
    quick_flip_min_net_roi: float = field(
        default_factory=lambda: float(os.getenv("QUICK_FLIP_MIN_NET_ROI", "0.03"))
    )
    quick_flip_recent_trade_window_seconds: int = field(
        default_factory=lambda: int(os.getenv("QUICK_FLIP_RECENT_TRADE_WINDOW_SECONDS", "3600"))
    )
    quick_flip_min_recent_trade_count: int = field(
        default_factory=lambda: int(os.getenv("QUICK_FLIP_MIN_RECENT_TRADE_COUNT", "5"))
    )
    quick_flip_max_target_vs_recent_trade_gap: float = field(
        default_factory=lambda: float(os.getenv("QUICK_FLIP_MAX_TARGET_VS_RECENT_TRADE_GAP", "0.01"))
    )
    quick_flip_maker_entry_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("QUICK_FLIP_MAKER_ENTRY_TIMEOUT_SECONDS", "180"))
    )
    quick_flip_maker_entry_poll_seconds: int = field(
        default_factory=lambda: int(os.getenv("QUICK_FLIP_MAKER_ENTRY_POLL_SECONDS", "5"))
    )
    quick_flip_maker_entry_reprice_seconds: int = field(
        default_factory=lambda: int(os.getenv("QUICK_FLIP_MAKER_ENTRY_REPRICE_SECONDS", "30"))
    )
    quick_flip_dynamic_exit_reprice_seconds: int = field(
        default_factory=lambda: int(os.getenv("QUICK_FLIP_DYNAMIC_EXIT_REPRICE_SECONDS", "60"))
    )
    quick_flip_stop_loss_pct: float = field(
        default_factory=lambda: float(os.getenv("QUICK_FLIP_STOP_LOSS_PCT", "0.08"))
    )

    # Cost control and market analysis frequency - MORE PERMISSIVE
    daily_ai_budget: float = 10.0  # INCREASED: Higher daily budget (was 5.0, now 10.0)
    max_ai_cost_per_decision: float = 0.08  # INCREASED: Higher per-decision cost (was 0.05, now 0.08)
    analysis_cooldown_hours: int = 3  # DECREASED: Shorter cooldown (was 6, now 3)
    max_analyses_per_market_per_day: int = 4  # INCREASED: More analyses per day (was 2, now 4)
    
    # Daily AI spending limits - SAFETY CONTROLS
    # Default is $10/day — conservative limit to prevent runaway API spend.
    # Raise via DAILY_AI_COST_LIMIT env var or by editing this value directly.
    # e.g. export DAILY_AI_COST_LIMIT=25  (for more aggressive scanning)
    daily_ai_cost_limit: float = field(default_factory=lambda: float(os.getenv("DAILY_AI_COST_LIMIT", "10.0")))
    enable_daily_cost_limiting: bool = True  # Enable daily cost limits
    sleep_when_limit_reached: bool = True  # Sleep until next day when limit reached

    # Enhanced market filtering to reduce analyses - MORE PERMISSIVE
    min_volume_for_ai_analysis: float = 200.0  # DECREASED: Much lower threshold (was 500, now 200)
    exclude_low_liquidity_categories: List[str] = field(default_factory=lambda: [
        # REMOVED weather and entertainment - trade all categories
    ])


@dataclass
class LoggingConfig:
    """Logging configuration."""
    log_level: str = "DEBUG"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    log_file: str = "logs/trading_system.log"
    enable_file_logging: bool = True
    enable_console_logging: bool = True
    max_log_file_size: int = 10 * 1024 * 1024  # 10MB
    backup_count: int = 5


# BEAST MODE UNIFIED TRADING SYSTEM CONFIGURATION 🚀
# These settings control the advanced multi-strategy trading system

# === CAPITAL ALLOCATION ACROSS STRATEGIES ===
# Allocate capital across different trading approaches
market_making_allocation: float = 0.40  # 40% for market making (spread profits)
directional_allocation: float = 0.50    # 50% for directional trading (AI predictions) 
arbitrage_allocation: float = 0.10      # 10% for arbitrage opportunities

  # === PORTFOLIO OPTIMIZATION SETTINGS ===
# Kelly Criterion is now the PRIMARY position sizing method (moved to TradingConfig)
# total_capital: DYNAMICALLY FETCHED from Kalshi balance - never hardcoded!
use_risk_parity: bool = True            # Equal risk allocation vs equal capital
rebalance_hours: int = 6                # Rebalance portfolio every 6 hours
min_position_size: float = 5.0          # Minimum position size ($5 vs $10)
max_opportunities_per_batch: int = 50   # Limit opportunities to prevent optimization issues

# === RISK MANAGEMENT LIMITS ===
# Portfolio-level risk constraints — DISCIPLINED DEFAULTS
# Conservative defaults based on live trading experience. Beast mode available via CLI flag.
max_volatility: float = 0.40            # SANE: 40% volatility max (was 80%)
max_correlation: float = 0.70           # SANE: 70% correlation max (was 95%)
max_drawdown: float = 0.15              # SANE: 15% drawdown limit (was 50% — suicidal)
max_sector_exposure: float = 0.30       # SANE: 30% sector concentration (was 90%)

# === PERFORMANCE TARGETS ===
# System performance objectives - MORE AGGRESSIVE FOR MORE TRADES
target_sharpe: float = 0.3              # DECREASED: Lower Sharpe requirement (was 0.5, now 0.3)
target_return: float = 0.15             # INCREASED: Higher return target (was 0.10, now 0.15)
min_trade_edge: float = 0.08           # DECREASED: Lower edge requirement (was 0.15, now 8%)
min_confidence_for_large_size: float = 0.50  # DECREASED: Lower confidence requirement (was 0.65, now 50%)

# === DYNAMIC EXIT STRATEGIES ===
# Enhanced exit strategy settings - MORE AGGRESSIVE
use_dynamic_exits: bool = True
profit_threshold: float = 0.20          # DECREASED: Take profits sooner (was 0.25, now 0.20)
loss_threshold: float = 0.15            # INCREASED: Allow larger losses (was 0.10, now 0.15)
confidence_decay_threshold: float = 0.25  # INCREASED: Allow more confidence decay (was 0.20, now 0.25)
max_hold_time_hours: int = 240          # INCREASED: Hold longer (was 168, now 240 hours = 10 days)
volatility_adjustment: bool = True      # Adjust exits based on volatility

# === MARKET MAKING STRATEGY ===
# Settings for limit order market making - MORE AGGRESSIVE
enable_market_making: bool = True       # Enable market making strategy
min_spread_for_making: float = 0.01     # DECREASED: Accept smaller spreads (was 0.02, now 1¢)
max_inventory_risk: float = 0.15        # INCREASED: Allow higher inventory risk (was 0.10, now 15%)
order_refresh_minutes: int = 15         # Refresh orders every 15 minutes
max_orders_per_market: int = 4          # Maximum orders per market (2 each side)

# === MARKET SELECTION (ENHANCED FOR MORE OPPORTUNITIES) ===
# Removed time restrictions - trade ANY deadline with dynamic exits!
# max_time_to_expiry_days: REMOVED      # No longer used - trade any timeline!
min_volume_for_analysis: float = 200.0  # DECREASED: Much lower minimum volume (was 1000, now 200)
min_volume_for_market_making: float = 500.0  # DECREASED: Lower volume for market making (was 2000, now 500)
min_price_movement: float = 0.02        # DECREASED: Lower minimum range (was 0.05, now 2¢)
max_bid_ask_spread: float = 0.15        # INCREASED: Allow wider spreads (was 0.10, now 15¢)
min_confidence_long_term: float = 0.45  # DECREASED: Lower confidence for distant expiries (was 0.65, now 45%)

# === COST OPTIMIZATION (MORE GENEROUS) ===
# Enhanced cost controls for the beast mode system
daily_ai_budget: float = 15.0           # INCREASED: Higher budget for more opportunities (was 10.0, now 15.0)
max_ai_cost_per_decision: float = 0.12  # INCREASED: Higher per-decision limit (was 0.08, now 0.12)
analysis_cooldown_hours: int = 2        # DECREASED: Much shorter cooldown (was 4, now 2)
max_analyses_per_market_per_day: int = 6  # INCREASED: More analyses per day (was 3, now 6)
skip_news_for_low_volume: bool = True   # Skip expensive searches for low volume
news_search_volume_threshold: float = 1000.0  # News threshold

# === SYSTEM BEHAVIOR ===
# Overall system behavior settings
beast_mode_enabled: bool = True         # Enable the unified advanced system
fallback_to_legacy: bool = True         # Fallback to legacy system if needed
log_level: str = "INFO"                 # Logging level
performance_monitoring: bool = True     # Enable performance monitoring

# === ADVANCED FEATURES ===
# Cutting-edge features for maximum performance
cross_market_arbitrage: bool = False    # Enable when arbitrage module ready
multi_model_ensemble: bool = True       # Multi-model ensemble decisions (ENABLED)
sentiment_analysis: bool = True         # News sentiment analysis (ENABLED)
websocket_streaming: bool = True        # WebSocket real-time data (ENABLED)
options_strategies: bool = False        # Complex options strategies (future)
algorithmic_execution: bool = False     # Smart order execution (future)


@dataclass
class Settings:
    """Main settings class combining all configuration."""
    api: APIConfig = field(default_factory=APIConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    ensemble: EnsembleConfig = field(default_factory=EnsembleConfig)
    sentiment: SentimentConfig = field(default_factory=SentimentConfig)

    def validate(self) -> bool:
        """Validate configuration settings."""
        if self.api.kalshi_env not in {"prod", "demo"}:
            raise ValueError("KALSHI_ENV must be 'prod' or 'demo'")

        if self.api.llm_provider not in {"auto", "openai", "openrouter"}:
            raise ValueError("LLM_PROVIDER must be 'auto', 'openai', or 'openrouter'")

        if not self.api.kalshi_api_key:
            raise ValueError("KALSHI_API_KEY environment variable is required")

        if self.api.resolve_llm_provider() == "openai" and not self.api.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is required when LLM_PROVIDER resolves to 'openai'"
            )

        if self.api.resolve_llm_provider() == "openrouter" and not self.api.openrouter_api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is required when LLM_PROVIDER resolves to 'openrouter'"
            )

        if self.trading.max_position_size_pct <= 0 or self.trading.max_position_size_pct > 100:
            raise ValueError("max_position_size_pct must be between 0 and 100")

        if self.trading.min_confidence_to_trade <= 0 or self.trading.min_confidence_to_trade > 1:
            raise ValueError("min_confidence_to_trade must be between 0 and 1")

        if self.trading.quick_flip_allocation < 0 or self.trading.quick_flip_allocation > 1:
            raise ValueError("quick_flip_allocation must be between 0 and 1")

        if self.trading.quick_flip_confidence_threshold <= 0 or self.trading.quick_flip_confidence_threshold > 1:
            raise ValueError("quick_flip_confidence_threshold must be between 0 and 1")

        if self.trading.quick_flip_min_market_volume < 0:
            raise ValueError("quick_flip_min_market_volume must be non-negative")

        if self.trading.quick_flip_max_hours_to_expiry <= 0:
            raise ValueError("quick_flip_max_hours_to_expiry must be positive")

        if self.trading.quick_flip_max_bid_ask_spread <= 0 or self.trading.quick_flip_max_bid_ask_spread >= 1:
            raise ValueError("quick_flip_max_bid_ask_spread must be between 0 and 1")

        if self.trading.quick_flip_min_top_of_book_size <= 0:
            raise ValueError("quick_flip_min_top_of_book_size must be positive")

        if self.trading.quick_flip_min_net_profit < 0:
            raise ValueError("quick_flip_min_net_profit must be non-negative")

        if self.trading.quick_flip_min_net_roi < 0 or self.trading.quick_flip_min_net_roi >= 1:
            raise ValueError("quick_flip_min_net_roi must be between 0 and 1")

        if self.trading.quick_flip_recent_trade_window_seconds <= 0:
            raise ValueError("quick_flip_recent_trade_window_seconds must be positive")

        if self.trading.quick_flip_min_recent_trade_count < 0:
            raise ValueError("quick_flip_min_recent_trade_count must be non-negative")

        if (
            self.trading.quick_flip_max_target_vs_recent_trade_gap < 0
            or self.trading.quick_flip_max_target_vs_recent_trade_gap >= 1
        ):
            raise ValueError("quick_flip_max_target_vs_recent_trade_gap must be between 0 and 1")

        if self.trading.quick_flip_maker_entry_timeout_seconds <= 0:
            raise ValueError("quick_flip_maker_entry_timeout_seconds must be positive")

        if self.trading.quick_flip_maker_entry_poll_seconds <= 0:
            raise ValueError("quick_flip_maker_entry_poll_seconds must be positive")

        if self.trading.quick_flip_maker_entry_reprice_seconds <= 0:
            raise ValueError("quick_flip_maker_entry_reprice_seconds must be positive")

        if self.trading.quick_flip_dynamic_exit_reprice_seconds <= 0:
            raise ValueError("quick_flip_dynamic_exit_reprice_seconds must be positive")

        if self.trading.quick_flip_stop_loss_pct <= 0 or self.trading.quick_flip_stop_loss_pct >= 1:
            raise ValueError("quick_flip_stop_loss_pct must be between 0 and 1")

        for allocation_name in (
            "market_making_allocation",
            "directional_allocation",
            "arbitrage_allocation",
        ):
            allocation_value = getattr(self.trading, allocation_name)
            if allocation_value < 0 or allocation_value > 1:
                raise ValueError(f"{allocation_name} must be between 0 and 1")

        total_allocation = (
            self.trading.market_making_allocation +
            self.trading.directional_allocation +
            self.trading.arbitrage_allocation +
            self.trading.quick_flip_allocation
        )
        if total_allocation > 1.0:
            raise ValueError("strategy allocations must sum to 1.0 or less")

        if self.trading.live_wagering_max_hours_to_expiry <= 0:
            raise ValueError("live_wagering_max_hours_to_expiry must be positive")

        required_roles = {
            "news_analyst",
            "forecaster",
            "risk_manager",
            "bull_researcher",
            "bear_researcher",
            "trader",
        }
        missing_roles = required_roles - set(self.ensemble.get_role_model_map())
        if missing_roles:
            missing = ", ".join(sorted(missing_roles))
            raise ValueError(f"ensemble config is missing required roles: {missing}")

        return True


# Global settings instance
settings = Settings()

# Validate settings on import
try:
    settings.validate()
except ValueError as e:
    print(f"Configuration validation error: {e}")
    print("Please check your environment variables and configuration.")
