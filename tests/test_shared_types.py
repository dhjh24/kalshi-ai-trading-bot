"""Tests for the shared LLM dataclasses + mirror helper.

These cover what used to be tested via the legacy ``XAIClient`` shim
(``tests/test_xai_client.py``):

* ``mirror_provider_usage`` updates the tracker when the provider does NOT
  self-track on the shared pickle.
* ``mirror_provider_usage`` does NOT double-count when the provider already
  wrote a request_count/total_cost increment to the shared pickle.
* ``LegacyPickleUnpickler`` keeps reading pickles whose dataclasses point at
  the deleted ``src.clients.xai_client`` module.
"""

import io
import pickle
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.clients.shared_types import (
    DailyUsageTracker,
    LegacyPickleUnpickler,
    TradingDecision,
    load_daily_tracker_pickle,
    mirror_provider_usage,
)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def test_trading_decision_dataclass_defaults():
    decision = TradingDecision(action="BUY", side="YES", confidence=0.8, limit_price=55)
    assert decision.action == "BUY"
    assert decision.reasoning is None


def test_daily_usage_tracker_dataclass_defaults():
    tracker = DailyUsageTracker(date="2026-04-24", request_count=3)
    assert tracker.request_count == 3
    assert tracker.daily_limit == 10.0
    assert tracker.is_exhausted is False


def test_mirror_provider_usage_returns_zero_when_result_is_none():
    today = _today()
    prior = DailyUsageTracker(date=today, request_count=2, total_cost=1.0)
    tracker = DailyUsageTracker(date=today, request_count=2, total_cost=1.0)
    saved = []

    cost, returned = mirror_provider_usage(
        provider_client=SimpleNamespace(last_request_metadata=SimpleNamespace(cost=0.5)),
        result=None,
        prior_tracker=prior,
        tracker=tracker,
        save_tracker=saved.append,
    )

    assert cost == 0.0
    assert returned is prior
    assert tracker.request_count == 2
    assert tracker.total_cost == 1.0
    assert saved == []


def test_mirror_provider_usage_persists_when_provider_does_not_track():
    today = _today()
    # Provider did NOT mutate the shared pickle, so prior == tracker.
    prior = DailyUsageTracker(date=today, request_count=2, total_cost=1.0)
    tracker = DailyUsageTracker(date=today, request_count=2, total_cost=1.0)
    saved = []

    provider = SimpleNamespace(
        last_request_metadata=SimpleNamespace(cost=0.75),
    )

    cost, returned = mirror_provider_usage(
        provider_client=provider,
        result="ok",
        prior_tracker=prior,
        tracker=tracker,
        save_tracker=saved.append,
    )

    assert cost == pytest.approx(0.75)
    assert returned is tracker
    assert tracker.request_count == 3
    assert tracker.total_cost == pytest.approx(1.75)
    assert saved == [tracker]


def test_mirror_provider_usage_does_not_double_count_when_provider_self_tracks():
    today = _today()
    # Provider wrote one extra request + 0.75 cost to the shared pickle, so
    # the freshly-loaded `tracker` already reflects the increment vs `prior`.
    prior = DailyUsageTracker(date=today, request_count=2, total_cost=1.0)
    tracker = DailyUsageTracker(date=today, request_count=3, total_cost=1.75)
    saved = []

    provider = SimpleNamespace(
        last_request_metadata=SimpleNamespace(cost=0.75),
    )

    cost, returned = mirror_provider_usage(
        provider_client=provider,
        result="ok",
        prior_tracker=prior,
        tracker=tracker,
        save_tracker=saved.append,
    )

    assert cost == pytest.approx(0.75)
    assert returned is tracker
    # Tracker is left as-is; provider already persisted these values.
    assert tracker.request_count == 3
    assert tracker.total_cost == pytest.approx(1.75)
    assert saved == []


def test_mirror_provider_usage_uses_update_callback_when_provided():
    today = _today()
    prior = DailyUsageTracker(date=today, request_count=2, total_cost=1.0)
    tracker = DailyUsageTracker(date=today, request_count=2, total_cost=1.0)
    saved = []
    cb_calls = []

    def _update(amount: float) -> None:
        cb_calls.append(amount)

    cost, _ = mirror_provider_usage(
        provider_client=SimpleNamespace(last_request_metadata=SimpleNamespace(cost=0.4)),
        result="ok",
        prior_tracker=prior,
        tracker=tracker,
        save_tracker=saved.append,
        update_daily_usage=_update,
    )

    assert cost == pytest.approx(0.4)
    assert cb_calls == [pytest.approx(0.4)]
    # When the caller supplies update_daily_usage, the helper must NOT
    # re-implement the increment+save itself.
    assert tracker.request_count == 2
    assert tracker.total_cost == 1.0
    assert saved == []


def test_mirror_provider_usage_handles_non_numeric_cost():
    today = _today()
    prior = DailyUsageTracker(date=today, request_count=2, total_cost=1.0)
    tracker = DailyUsageTracker(date=today, request_count=2, total_cost=1.0)
    saved = []

    provider = SimpleNamespace(
        last_request_metadata=SimpleNamespace(cost="not-a-number"),
    )

    cost, returned = mirror_provider_usage(
        provider_client=provider,
        result="ok",
        prior_tracker=prior,
        tracker=tracker,
        save_tracker=saved.append,
    )

    assert cost == 0.0
    assert returned is tracker
    assert tracker.request_count == 3
    assert tracker.total_cost == pytest.approx(1.0)


def _legacy_xai_pickle_bytes(tracker: DailyUsageTracker) -> bytes:
    """Produce a pickle stream that references the deleted xai_client module.

    We cannot just set ``DailyUsageTracker.__module__ = "src.clients.xai_client"``
    and call :func:`pickle.dumps`, because Python's pickler resolves the class
    via that module name during serialization and the module no longer exists.
    Instead we override class lookup via a Pickler subclass.
    """

    class _LegacyXaiPickler(pickle.Pickler):
        def reducer_override(self, obj):  # type: ignore[override]
            if isinstance(obj, type) and obj is DailyUsageTracker:
                # Tell pickle to reference the legacy module path during dump.
                return getattr, ("src.clients.xai_client", "DailyUsageTracker")  # noqa: PLE0101
            return NotImplemented

    # Simpler path: monkey-patch the class's reduce-by-name behavior using a
    # custom dispatch via copyreg-like per-class hook.
    buf = io.BytesIO()
    # Pickle protocol 2+ stores classes by ``module + qualname`` strings; we
    # patch only the ``module`` attribute for the duration of the dump and
    # also stub a fake module object in ``sys.modules`` so the pickler's
    # importability check passes.
    import sys
    import types

    legacy_mod_name = "src.clients.xai_client"
    fake_mod = types.ModuleType(legacy_mod_name)
    fake_mod.DailyUsageTracker = DailyUsageTracker  # type: ignore[attr-defined]
    sys.modules[legacy_mod_name] = fake_mod
    original_module = DailyUsageTracker.__module__
    DailyUsageTracker.__module__ = legacy_mod_name
    try:
        pickle.Pickler(buf).dump(tracker)
    finally:
        DailyUsageTracker.__module__ = original_module
        sys.modules.pop(legacy_mod_name, None)
    return buf.getvalue()


def test_legacy_pickle_unpickler_remaps_xai_client_module():
    today = _today()
    tracker = DailyUsageTracker(date=today, request_count=7, total_cost=0.0)
    payload = _legacy_xai_pickle_bytes(tracker)

    # The pickle stream MUST reference the legacy module name so the test
    # actually exercises the remap.
    assert b"src.clients.xai_client" in payload

    loaded = load_daily_tracker_pickle(io.BytesIO(payload))
    assert isinstance(loaded, DailyUsageTracker)
    assert loaded.request_count == 7

    # Same logic via the explicit Unpickler subclass.
    again = LegacyPickleUnpickler(io.BytesIO(payload)).load()
    assert isinstance(again, DailyUsageTracker)
    assert again.request_count == 7
