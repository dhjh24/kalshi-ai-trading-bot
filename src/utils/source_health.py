"""
Source-health derivation helpers.

Adapter modules already return a uniform contract:

    {
        "category": "sports" | "crypto" | "macro" | "weather" | ...,
        "source": "espn.scoreboard" | "coingecko.simple-price" | ...,
        "freshness_seconds": int,
        "signals": dict,
        "error": Optional[str],
        ...
    }

The execution-safety guard already records snapshots from Kalshi market
fetches and the weather contract interpreter directly. The remaining adapters
(sports, crypto, macro, bitcoin) are exercised by ``LiveTradeResearchService``
but historically did not feed into ``source_snapshots``, so the dashboard
showed those slots as empty even when the underlying fetch had succeeded or
failed minutes earlier.

This module provides three small, testable surfaces:

- ``derive_source_snapshot(payload)`` -- normalize an adapter payload into a
  ``SourceHealthSnapshot`` (or ``None`` when the payload is missing fields).
- ``iter_research_payload_snapshots(research_payload)`` -- walk a research
  bundle and emit one snapshot per adapter response inside it.
- ``record_research_payload_snapshots(db_manager, research_payload)`` --
  persist the snapshots best-effort (silent on DB errors so the live-trade
  loop never breaks because of telemetry).

Keeping the helpers pure (no DB on the derivation surface) lets the unit
tests assert behaviour without a database fixture, while the live-trade job
can drop a single ``await`` into the specialist step to start filling in
sports/crypto/macro/bitcoin slots on the safety dashboard.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional


# Maps a research-payload key (set by `LiveTradeResearchService`) to the
# (category, source) pair to use when the adapter response itself is a
# bare dict that does not carry the category/source fields. The bitcoin
# context is the only one in this category today.
RESEARCH_PAYLOAD_DEFAULTS: Dict[str, tuple[str, str]] = {
    "bitcoin_context": ("crypto", "coingecko.simple-price"),
    "news": ("news", "rss-aggregator"),
}


# Research-payload keys we treat as adapter responses. Anything not in this
# set is ignored (e.g. ``event``, ``microstructure``).
RESEARCH_PAYLOAD_ADAPTER_KEYS: tuple[str, ...] = (
    "sports_context",
    "crypto_context",
    "bitcoin_context",
    "macro_context",
    "weather_context",
    "news",
)


@dataclass(frozen=True)
class SourceHealthSnapshot:
    category: str
    source: str
    status: str
    freshness_seconds: int
    summary: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _classify_status(payload: Mapping[str, Any]) -> str:
    """Decide how to label this snapshot based on error/signal content."""

    error = payload.get("error")
    if error:
        normalized = str(error).lower()
        # Hard failures (HTTP 5xx, connection refused, missing creds) get
        # ``unavailable`` so the dashboard tone surfaces as red. Recoverable
        # signals (no team match, no data points) become ``degraded`` so the
        # operator knows to look but the system isn't paged.
        hard_failure_tokens = (
            "fail",
            "timeout",
            "connection",
            "unauthorized",
            "forbidden",
            "httpstatus",
            "http status",
            "network",
            "ssl",
            "dns",
            "rate limit",
            "429",
            "4xx",
            "5xx",
            "unavailable",
            "down",
        )
        if any(token in normalized for token in hard_failure_tokens):
            return "unavailable"
        return "degraded"

    # Adapters that follow the uniform contract expose ``signals``; older
    # payloads (e.g. the news bundle) place data under ``articles`` /
    # ``article_count`` instead. Treat either as evidence of healthy data.
    signals = payload.get("signals")
    if isinstance(signals, Mapping) and len(signals) > 0:
        return "healthy"
    if signals is not None and signals != {} and signals != []:
        return "healthy"

    article_count = payload.get("article_count")
    articles = payload.get("articles")
    try:
        article_count_int = int(article_count) if article_count is not None else 0
    except (TypeError, ValueError):
        article_count_int = 0
    if article_count_int > 0 or (isinstance(articles, list) and len(articles) > 0):
        return "healthy"

    # Compatibility payloads such as ``fetch_bitcoin_context`` predate the
    # uniform adapter contract and expose useful fields directly instead of
    # under ``signals``. Treat any populated data field as healthy so the
    # safety dashboard does not show fresh crypto context as "empty".
    metadata_keys = {
        "category",
        "source",
        "timestamp_utc",
        "freshness_seconds",
        "signals",
        "error",
    }
    content_keys = [key for key in payload.keys() if key not in metadata_keys]
    for key in content_keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return "healthy"

    return "empty"


def derive_source_snapshot(
    payload: Mapping[str, Any],
    *,
    fallback_category: Optional[str] = None,
    fallback_source: Optional[str] = None,
) -> Optional[SourceHealthSnapshot]:
    """Normalize an adapter payload into a ``SourceHealthSnapshot``.

    Returns ``None`` when the payload cannot be reduced to a (category,
    source) pair, even after applying fallbacks. Callers should treat that
    as "skip this snapshot" rather than failing the surrounding flow.
    """

    if not isinstance(payload, Mapping):
        return None

    category = str(payload.get("category") or fallback_category or "").strip()
    source = str(payload.get("source") or fallback_source or "").strip()
    if not category or not source:
        return None

    error = payload.get("error")
    signals = payload.get("signals")
    freshness = payload.get("freshness_seconds")
    try:
        freshness_int = max(0, int(freshness)) if freshness is not None else 0
    except (TypeError, ValueError):
        freshness_int = 0

    article_count_raw = payload.get("article_count")
    try:
        article_count_int = (
            int(article_count_raw) if article_count_raw is not None else None
        )
    except (TypeError, ValueError):
        article_count_int = None

    has_signals = bool(signals) if signals is not None else False
    has_direct_payload = any(
        payload.get(key) not in (None, "", [], {})
        for key in payload.keys()
        if key
        not in {
            "category",
            "source",
            "timestamp_utc",
            "freshness_seconds",
            "signals",
            "error",
        }
    )
    summary: Dict[str, Any] = {
        "has_signals": has_signals or has_direct_payload,
        "error": str(error) if error else None,
    }
    if article_count_int is not None:
        summary["article_count"] = article_count_int

    return SourceHealthSnapshot(
        category=category,
        source=source,
        status=_classify_status(payload),
        freshness_seconds=freshness_int,
        summary=summary,
    )


def iter_research_payload_snapshots(
    research_payload: Mapping[str, Any],
) -> Iterator[SourceHealthSnapshot]:
    """Yield one snapshot per adapter response inside a research bundle.

    Skips keys that are not present, are not mappings, or that cannot be
    reduced to a (category, source) pair via ``derive_source_snapshot``.
    """

    if not isinstance(research_payload, Mapping):
        return

    for key in RESEARCH_PAYLOAD_ADAPTER_KEYS:
        candidate = research_payload.get(key)
        if not isinstance(candidate, Mapping):
            continue
        fallback = RESEARCH_PAYLOAD_DEFAULTS.get(key, (None, None))
        snapshot = derive_source_snapshot(
            candidate,
            fallback_category=fallback[0],
            fallback_source=fallback[1],
        )
        if snapshot is not None:
            yield snapshot


async def record_research_payload_snapshots(
    db_manager: Any, research_payload: Mapping[str, Any]
) -> int:
    """Persist a snapshot per adapter response in ``research_payload``.

    Returns the number of rows written. Errors are swallowed deliberately
    because source-health telemetry must never break the live-trade loop.
    """

    recorder = getattr(db_manager, "record_source_snapshot", None)
    if not callable(recorder):
        return 0

    written = 0
    for snapshot in iter_research_payload_snapshots(research_payload):
        try:
            await recorder(
                category=snapshot.category,
                source=snapshot.source,
                status=snapshot.status,
                freshness_seconds=snapshot.freshness_seconds,
                payload=snapshot.summary,
            )
            written += 1
        except Exception:
            continue
    return written


__all__ = [
    "RESEARCH_PAYLOAD_ADAPTER_KEYS",
    "RESEARCH_PAYLOAD_DEFAULTS",
    "SourceHealthSnapshot",
    "derive_source_snapshot",
    "iter_research_payload_snapshots",
    "record_research_payload_snapshots",
]
