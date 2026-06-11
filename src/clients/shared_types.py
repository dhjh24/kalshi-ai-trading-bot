"""Shared LLM dataclasses used across provider clients and legacy shims."""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Tuple

__all__ = [
    "TradingDecision",
    "DailyUsageTracker",
    "LegacyPickleUnpickler",
    "load_daily_tracker_pickle",
    "mirror_provider_usage",
]


@dataclass
class TradingDecision:
    """Represents an AI trading decision."""

    action: str
    side: str
    confidence: float
    limit_price: Optional[int] = None
    reasoning: Optional[str] = None
    # Estimated TRUE probability that the market resolves YES (0-1), pooled
    # from the agents' forecasts. Distinct from `confidence`, which is the
    # model's certainty in its own decision. None on legacy single-model paths.
    fair_yes_probability: Optional[float] = None


@dataclass
class DailyUsageTracker:
    """Track daily AI usage and costs across providers."""

    date: str
    total_cost: float = 0.0
    request_count: int = 0
    daily_limit: float = 10.0
    is_exhausted: bool = False
    last_exhausted_time: Optional[datetime] = None


class LegacyPickleUnpickler(pickle.Unpickler):
    """Restricted unpickler for the local daily-usage tracker.

    Pickled :class:`DailyUsageTracker` snapshots written before the W11(c)
    cleanup carry ``__module__ == "src.clients.xai_client"``. After that
    module is deleted those snapshots would fail to unpickle; this remap
    keeps the on-disk format readable without resurrecting the dead module.

    The file lives under ``logs/`` and is not a network protocol, but it is
    still mutable local state. Restrict globals to the tracker shape instead
    of allowing a crafted pickle to import arbitrary callables.
    """

    def find_class(self, module: str, name: str):  # type: ignore[override]
        if module == "src.clients.xai_client" and name == "DailyUsageTracker":
            module = "src.clients.shared_types"

        if module == "src.clients.shared_types" and name == "DailyUsageTracker":
            return DailyUsageTracker
        if module == "datetime" and name == "datetime":
            return datetime

        raise pickle.UnpicklingError(f"Unsupported class in daily usage tracker: {module}.{name}")


def load_daily_tracker_pickle(fh) -> Any:
    """Load a pickled tracker via :class:`LegacyPickleUnpickler`."""
    return LegacyPickleUnpickler(fh).load()


def mirror_provider_usage(
    *,
    provider_client: Any,
    result: Any,
    prior_tracker: DailyUsageTracker,
    tracker: DailyUsageTracker,
    save_tracker: Callable[[DailyUsageTracker], None],
    update_daily_usage: Optional[Callable[[float], None]] = None,
) -> Tuple[float, DailyUsageTracker]:
    """Mirror provider-side usage into a caller-owned daily tracker.

    Returns (cost_observed, refreshed_tracker). The caller passes its own
    pickle-load and pickle-save callables so this helper stays stateless.
    The defensive snapshot-vs-current diff that prevents double-counting
    when the provider already wrote to the shared pickle is preserved
    exactly as it was in XAIClient._mirror_provider_usage.

    Parameters
    ----------
    provider_client:
        The underlying provider client (Codex/OpenAI/OpenRouter). Must expose
        a ``last_request_metadata`` attribute with a ``cost`` field, or the
        observed cost falls back to ``0.0``.
    result:
        The completion/decision result returned by the provider call. ``None``
        signals a failed call and the helper returns ``(0.0, prior_tracker)``
        without touching state.
    prior_tracker:
        Snapshot of the tracker captured BEFORE the provider call. The diff
        against the freshly-reloaded tracker is what tells us whether the
        provider already persisted a request_count/total_cost increment to
        the shared pickle.
    tracker:
        The current in-memory tracker. Updated in place when the provider
        did not self-track (``request_count`` += 1, ``total_cost`` += cost).
    save_tracker:
        Caller-supplied persistence function used to write the refreshed
        tracker back to disk after an in-helper update.
    update_daily_usage:
        Optional callback invoked with the observed cost when the provider
        did not self-track. When omitted, the helper performs the same
        increment-and-save logic inline against ``tracker``.

    Returns
    -------
    tuple[float, DailyUsageTracker]
        The cost observed via the provider's metadata and the (possibly
        refreshed) tracker the caller should adopt as its current view.
    """
    if result is None:
        return 0.0, prior_tracker

    metadata = getattr(provider_client, "last_request_metadata", None)
    raw_cost = getattr(metadata, "cost", 0.0) if metadata is not None else 0.0
    try:
        cost = float(raw_cost)
    except (TypeError, ValueError):
        cost = 0.0

    if (
        tracker.date == prior_tracker.date
        and tracker.request_count == prior_tracker.request_count
        and abs(tracker.total_cost - prior_tracker.total_cost) < 1e-12
    ):
        if update_daily_usage is not None:
            update_daily_usage(cost)
        else:
            tracker.total_cost += cost
            tracker.request_count += 1
            save_tracker(tracker)

    return cost, tracker
