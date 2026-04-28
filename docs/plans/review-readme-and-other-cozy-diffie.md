# Kalshi AI Trading Bot — Improvement Plan (Quick Flip + Live Trading Focus)

> **Living document.** Future agents working on any workstream below MUST update the
> "Status" tag in their workstream and the **Project Management Map** at the bottom
> when they complete, block, or reshape work. If this file gets too long, link out to
> per-workstream files under `docs/plans/` rather than deleting context.
>
> 2026-04-27 Codex follow-up: multi-agent audit found a few post-complete hardening gaps and environment blockers. This pass fixed the Python build/test path (`pyproject.toml` now uses `setuptools.build_meta`; `setup.py` no longer runs the interactive environment checker during package build commands), added root ignore patterns for generated pytest/temp directories that were causing permission-warning noise, made W9 refresh notifications genuinely nonblocking by scheduling `LiveTradeRefreshNotifier.notify()` in a tracked background task, and tightened W5 live quick-flip safety: live `QUICK_FLIP` intents now enforce `ENABLE_LIVE_QUICK_FLIP` before guardrails, `QUICK_FLIP` intents with hold windows over 30 minutes block instead of silently rerouting through generic live-trade execution, and live maker-entry repricing in `QuickFlipScalpingStrategy` cannot submit above the approved entry limit. New/updated regression coverage in `tests/test_live_trade_notify.py`, `tests/test_live_trade_job.py`, and `tests/test_quick_flip_scalping.py`. Verification: `TMPDIR=/tmp PYTHONPATH=. uv run --no-project --with-requirements requirements.txt python -m pytest -q tests/test_live_trade_notify.py tests/test_live_trade_job.py tests/test_quick_flip_scalping.py` => 62 passed in 50.17s. Remaining audit-only follow-ups not landed here: Quick Flip AI exception fallback should degrade to heuristics, quick-flip guardrail mode/current-position parity can be tightened, server refresh cursor should include runtime metadata, and server vitest needs a Linux/WSL Node 24+ dependency reinstall before rerunning.
>
> Last updated: 2026-04-27 by Claude (Opus) — landed three W11 unblocker passes per user direction. **W11(c)**: `src/clients/xai_client.py` deleted; `mirror_provider_usage` extracted to `src/clients/shared_types.py` as a stateless helper preserving the snapshot-vs-current diff that prevents double-counting; `LegacyPickleUnpickler` keeps existing on-disk pickles loadable; 17 caller files migrated to `ModelRouter` directly; `tests/test_xai_client.py` retired in favor of `tests/test_shared_types.py` (8 cases). **W11(b)**: top-level `beast_mode_bot.py` retired; `BeastModeBot` renamed to `UnifiedTradingBot` and relocated to `src/runtime/unified_bot.py` with byte-equivalent semantics; `cli.py` (3 import sites), `tests/test_cli_safety.py`, `pyproject.toml` py-modules, `README.md`, `CONTRIBUTING.md`, and `scripts/init_database.py` all updated. The `--beast` CLI flag is retained as a settings-override mode (per user direction; only the file/module goes away). **W11(a)**: confirmed in place — `paper_trader.py` keeps its `DeprecationWarning` since `cli.py run --paper` is the canonical paper-trading runtime against live market data; user explicitly opted to keep the deprecation banner. Earlier in the same session: repaired the timezone-flaky `test_live_trade_aliases_share_daily_loss_and_trade_rate_budget` (anchored the test on a frozen UTC noon) and landed the W4 auto-pause-on-drift hook (default off, configurable cents/USD/min-matched thresholds, halt persisted via existing `strategy_halts` table). Final test surface: 319 pytest passed, 8 skipped, 0 failed; 28 server vitest passed; 23 web vitest passed.
>
> Last updated: 2026-04-26 by Claude (Opus) - this pass landed the W8 codex-quota write loop, W9 useLiveStream hook migration, W10 parity + agent-debate coverage, fixed the LLMQuery dataclass-ordering bug + log_llm_query commit-context bug, repaired the `liveTradeFeedbackSse` runtime-state INSERT (16/18 column mismatch), and confirmed the W7 follow-ups previously flagged as open are now resolved in-tree. Final test surface: 290 pytest passed (5 skipped), 26 server vitest passed, 23 web vitest passed.
>
> 2026-04-26 multi-agent follow-up pass (W8 + W9 + W10 + W11): a four-worktree parallel pass landed (a) **W8** plan-tier accounting — added `requests_used`/`requests_limit`/`requests_remaining`/`requests_reset_at` and matching `tokens_*` columns to `codex_quota_tracking` (legacy primary triplet auto-mirrored), `CodexClient._extract_quota_signals` now parses both structured `rate_limit` JSON and plain-text `plan: Plus` / `requests: 12/100 (resets ...)` patterns, `/portfolio` renders plan tier + per-window attribution, CLI status surfaces the same; (b) **W9** push-based refresh — new `POST /internal/live-trade/notify-refresh` endpoint protected by `LIVE_TRADE_INTERNAL_REFRESH_TOKEN` shared secret, `LiveTradeRefreshNotifier` in `src/jobs/live_trade.py` fires fire-and-forget POSTs after every persisted decision/runtime-state/feedback write, cursor poll kept as a fallback safety net; (c) **W10** broader stress parity — new `tests/test_live_trade_parity_stress.py` locks N-cycle parity, concurrent-event ordering, and guardrail-trip parity across paper/shadow/live; (d) **W11** deprecation — `paper_trader.py` now carries a deprecation banner + `warnings.warn(..., DeprecationWarning)`, README points users at `python cli.py run --paper` and `python cli.py dashboard`, legacy `src/paper/tracker.py` signals path intentionally retained until the warning telemetry confirms no users hit it. Final test surface: 308 pytest passed (8 skipped, 1 pre-existing failure in `test_live_trade_aliases_share_daily_loss_and_trade_rate_budget` unrelated to this pass), 26 server vitest passed (with new W9 acceptance harness), 23 web vitest passed.

> 2026-04-23 local follow-up: W7 guardrails are now wired through the directional decision path in `src/jobs/decide.py` and the unified allocation path in `src/strategies/unified_trading_system.py`; mixed-schema spend summaries were hardened in both Python and Node; regression coverage was added for the new alias/summary cases. W5/W10/W11 remain open.
>
> 2026-04-24 local follow-up: W5 is no longer "not started" in practice. The repo already contains `src/jobs/live_trade.py`, the `python cli.py run --live-trade` entrypoint, persisted `live_trade_decisions` / `live_trade_runtime_state` / `live_trade_decision_feedback` tables, and the `/live-trade` dashboard decision feed with SSE + feedback actions. This pass added focused regression coverage for the no-events skip path and specialist-payload fallback (`tests/test_live_trade_loop_regressions.py`) and hardened the Node acceptance fixtures in `server/tests/liveTradeFeedbackSse.test.ts` and `server/tests/dashboardRepository.test.ts`. Remaining W5/W10 gaps are live-mode execution wiring, optional write-triggered decision-feed refresh, and broader parity/shadow coverage.
>
> 2026-04-24 local follow-up: the W5 live-mode startup skip is gone for non-`QUICK_FLIP` intents. `src/jobs/live_trade.py` now drives the existing generic execution path with `live_mode=True`, persists truthful `paper_trade` / `live_trade` flags on decision rows, and records live-specific execution summaries. Dedicated `python cli.py run --live-trade` now supports `paper`, `shadow`, and `live` runtimes, and live `QUICK_FLIP` intents now route through the existing quick-flip machinery when `ENABLE_LIVE_QUICK_FLIP=1`, with the standalone loop also running the quick-flip manager each cycle so exits, repricing, and reconciliation stay active. On the dashboard side, `server/src/repositories/dashboardRepository.ts` now exposes a lightweight live-trade decision refresh cursor, `server/src/services/liveStreamHub.ts` polls that cursor every <=1s, and the web client now falls back to explicit reconnect / HTTP sync when the SSE feed goes stale. A follow-up visibility pass now persists explicit per-decision `runtime_mode`, threads verified worker-mode / exchange-source metadata through the decision-feed heartbeat, and softens the live-trade banners when the UI is only seeing `dashboard env` defaults instead of `live_trade_runtime_state`. Focused regression coverage landed in `tests/test_live_trade_job.py`, `tests/test_cli_safety.py`, `server/tests/dashboardRepository.test.ts`, `server/tests/liveTradeFeedbackSse.test.ts`, and `web/lib/live-trade-decision-feed.test.ts`. Remaining W9/W10 gaps are broader parity coverage and any future fully push-based Python -> Node refresh hook if we decide to remove cursor polling entirely.
>
> 2026-04-24 automation follow-up: the legacy `XAIClient` shim now respects `LLM_PROVIDER=codex` instead of silently falling through to OpenRouter, shared `TradingDecision` / `DailyUsageTracker` dataclasses were extracted to a neutral `src/clients/shared_types.py` module with compatibility re-exports left in place, quick-flip's heuristic fallback flag moved onto `settings.trading.quick_flip_disable_ai`, and the CLI Codex quota suffix now counts only `llm_queries` rows so it matches the dashboard's quota semantics instead of double-counting `analysis_requests`.
>
> 2026-04-25 automation follow-up: W11 compatibility cleanup was hardened so the shared LLM dataclasses now export explicitly from both `src/clients/shared_types.py` and the legacy `src/clients/xai_client.py` shim, while the `scripts/beast_mode_dashboard.py` wrapper and legacy-pickle compatibility stay covered by focused Python tests.
>
> 2026-04-26 local follow-up: W5/W10 shadow-mode visibility tightened for generic live-trade intents. `src/jobs/live_trade.py` now records execution payloads with `execution_mode="shadow"` when `shadow_mode_enabled=True` while still using paper execution semantics, and `tests/test_live_trade_job.py` locks the label to the persisted `live_trade_runtime_state.runtime_mode`.
>
> 2026-04-26 safety follow-up: live-trade guardrails now fail closed when `PortfolioEnforcer` errors, W5 short-hold `QUICK_FLIP` intents pre-check the quick-flip budget bucket instead of the live-trade bucket, generic execution rejects paper/live entries when the current ask or depth-walk average exceeds the approved entry limit, failed generic W5 executions void the pre-created position row, and the hourly trade-rate cap no longer double-counts closed recent positions mirrored in `positions`.
>
> 2026-04-26 implementation follow-up: W5 final intents with `execution_style="NONE"` now skip before guardrails or order creation, parity tests pin execution-row quantity/side/limit/hold fields and guardrail bucket routing, W8 has a first-class `codex_quota_tracking` snapshot path with `llm_queries` fallback, and W9 SSE refresh intervals catch sync/async refresh errors while allowing high fanout listeners.
>
> 2026-04-26 multi-agent integration pass: four worktree-isolated subagents landed in parallel. **W5+W10**: paper/shadow/live parity (`tests/test_live_trade_parity.py`) and end-to-end agent-debate coverage (`tests/test_live_trade_agent_debate.py`) now lock the multi-agent loop. **W8**: `src/clients/codex_client.py` now writes `codex_quota_tracking` snapshots after every successful CLI call, with best-effort fallback when the CLI is silent; `LLMQuery` dataclass-ordering bug and `log_llm_query` commit-outside-context bug also fixed. **W9**: shared `useLiveStream` hook extracted to `web/lib/use-live-stream.ts`; `live-trade-decisions-panel.tsx` migrated off inline SSE bookkeeping (15 new vitest cases). **W11**: audit-only outcome — every removal candidate (`beast_mode_bot.py`, `src/clients/xai_client.py`, `src/paper/tracker.py` signals path, `scripts/beast_mode_dashboard.py`) still has live callers and was left in place. **Pre-existing repo bug fixed**: `web/lib/` was unintentionally caught by the Python `lib/` rule in `.gitignore`, so 9 referenced TypeScript files lived in the working tree but never tracked in git; added `!web/lib/` exception and committed the directory.
>
> 2026-04-26 follow-up pass (this commit): server acceptance fixture in `server/tests/liveTradeFeedbackSse.test.ts::"prefers explicit runtime-state heartbeat telemetry when available"` was crashing because the INSERT listed 18 columns against 16 placeholders/values — dropped the unused `runtime_mode` and `exchange_env` columns from that one INSERT (they default NULL and are not asserted on by this test). Verified W7 follow-ups previously flagged as open are now resolved in-tree: per-strategy budget env vars are already exposed on `settings.trading` via `field(default_factory=lambda: os.getenv(...))` (`quick_flip_daily_loss_budget_pct`, `quick_flip_max_trades_per_hour`, `live_trade_daily_loss_budget_pct`, `live_trade_max_trades_per_hour`), and `cmd_status` already restructures around Kalshi reachability — `_print_strategy_budget_status()` is called whether or not `client.get_balance()` succeeds, falling back to `local_portfolio_estimate` for the budget calc when the API is down. Final test surface: 290 pytest passed (5 skipped, plus the pre-existing pandas-import-only collection skips), 26 server vitest passed, 23 web vitest passed.

---

## 1. Context

The repo today is a working Kalshi prediction-market bot with:

- A **5-model AI ensemble** ([src/agents/](src/agents/), [src/jobs/decide.py](src/jobs/decide.py)) routed through `LLM_PROVIDER=auto|openai|openrouter` ([src/config/settings.py:46-63](src/config/settings.py#L46-L63)).
- A **quick-flip scalping strategy** ([src/strategies/quick_flip_scalping.py](src/strategies/quick_flip_scalping.py), ~1,950 lines) — math-based, with maker-entry attempts, dynamic exits, fee-aware profit floors, paper simulation, and live settlement reconciliation. Recent commits (`10d0694`, `a9db594`, `f2318d9`, `dcc6c8c`) cleaned up fee handling and added simulated-order tracking.
- A **live-trade pipeline** ([src/data/live_trade_research.py](src/data/live_trade_research.py), ~1,390 lines) feeding a Next.js `/live-trade` route via the FastAPI [python_bridge/app/main.py](python_bridge/app/main.py) and Fastify `/api/live-trade` route. Filters short-dated events by `LIVE_WAGERING_MAX_HOURS_TO_EXPIRY` (default 12h) and enriches with Sports / BTC / News context.
- A **paper trading runtime** that writes to the same `trading_system.db` as live ([src/paper/tracker.py](src/paper/tracker.py), [src/jobs/execute.py](src/jobs/execute.py)) — paper entries snapshot the live best ask, exits rest locally and reconcile against periodic book snapshots.

**Where we're going:** narrow the bot's focus to two short-term betting paths:

1. **Quick flip** — pure math, fee-aware, fast-cycle. Already mostly built; needs accuracy hardening, paper/live parity proof, and a few edge-case fixes.
2. **Live trading** — agent-driven, in-play decisions on short-dated Sports / Crypto / Financials / Economics events. Currently a single-shot research call; needs a real multi-agent decision loop with cheap-fast models.

**Provider shift:** default agent calls should run on a **ChatGPT/Codex CLI** subprocess so quota comes from the user's ChatGPT plan, not API-key billing. OpenRouter remains the fallback for non-OpenAI models in the ensemble.

**Acceptance bar:** the bot runs in paper mode with high-fidelity simulation (replay harness + shadow-trade mode) until P&L tracks live within an acceptable tolerance, then we flip the switch.

---

## 2. Strategic Goals (in priority order)

1. **G1 — Codex-first AI routing.** Add a `codex` provider that invokes the Codex CLI signed in to a ChatGPT plan. Make it the default when available.
2. **G2 — Paper accuracy you can trust.** Replay harness + shadow-trade mode + parity assertions before we sign off on live.
3. **G3 — Quick flip production-readiness.** Close known accuracy gaps (entry-snapshot drift, resting-order race, fee rounding) and add observability.
4. **G4 — Live-trade agent loop.** Promote the live-trade flow from a single research call to a multi-agent in-play decision pipeline, fed by category-specific data adapters.
5. **G5 — Risk guardrails for live.** Per-strategy circuit breakers and budget caps that work the same in paper and live.
6. **G6 — Dashboard + observability.** Make paper-vs-live divergence and per-strategy P&L impossible to miss.
7. **G7 — Cleanup.** Retire legacy paths (Streamlit dashboard, beast mode default, `xai_client.py`).

---

## 3. Workstreams

Each workstream is sized for **one subagent** working in parallel with the others. Every workstream lists its **Owner**, **Depends on**, **Files**, **Done when**, **Status**.

### W1 — Codex CLI Provider Integration  *(Status: complete — merged to main 2026-04-23, commit `ca0dbb0`)*

> **Follow-ups flagged by W1 agent:**
> - W5 can call `router.get_completion(..., capability=...)` and route through Codex transparently; `create_structured_completion(prompt, schema=...)` is the right call for structured per-agent outputs.
> - W8 can pivot on `router.get_cost_summary()["providers"]["codex"]` + existing `llm_queries.strategy='codex'` + `tokens_used` without any schema change. Optional provider column on `LLMQuery` would be cleaner.
> - Pre-existing test-isolation bug in `tests/test_openrouter_client.py::test_router_sends_one_request_with_fallback_models` (does not patch settings — fails when `OPENAI_API_KEY` is set). W10 should sweep similar tests.
> - 2026-04-24 automation follow-up: `src/clients/xai_client.py` now initializes `CodexClient` when the effective provider resolves to `codex`, so older runtime paths (`trade.py`, `decide.py`, performance jobs) no longer bypass the Codex-first routing default.
- **Owner:** Backend Architect or Senior Developer subagent
- **Depends on:** none
- **Goal:** Add a third value to `LLM_PROVIDER` (`codex`) that shells out to the official `codex` CLI (signed in via ChatGPT plan), and make it the new default when the CLI is detected on PATH.
- **Files to touch:**
  - [src/config/settings.py](src/config/settings.py#L46-L96) — extend `_resolve_default_llm_provider()` so `auto` prefers `codex` if `which codex` succeeds and Codex is logged in, then `openai`, then `openrouter`. Add `_get_default_*_model` branches for Codex.
  - New: `src/clients/codex_client.py` — async subprocess wrapper that mirrors the `OpenAIClient` interface (`create_completion`, `create_structured_completion`). Stream stdout, parse, enforce JSON schema, capture token counts from CLI output (best-effort; fall back to 0).
  - [src/clients/model_router.py](src/clients/model_router.py#L1-L130) — register Codex models in `CAPABILITY_MAP` and a new `CODEX_FULL_FLEET`. Health tracking re-uses `ModelHealth`.
  - [env.template](env.template) — document `CODEX_CLI_PATH`, `CODEX_PLAN_TIER`, fallback behavior.
  - [README.md](README.md#L209-L218) — replace the "ChatGPT/Codex plan access is separate" note with the new flow.
  - New: `tests/test_codex_client.py` — mock `asyncio.create_subprocess_exec` and assert structured-output parsing + fallback when CLI is missing.
- **Done when:**
  - `LLM_PROVIDER=codex` runs an end-to-end ensemble call without touching OpenAI billing.
  - `LLM_PROVIDER=auto` picks Codex over OpenAI when the CLI is present and authenticated.
  - The `daily_cost_tracking` table records Codex calls with `cost_usd=0.0` but a non-null `tokens_used` so plan quota usage is still observable.
  - Existing OpenAI / OpenRouter behavior is unchanged when Codex is unavailable.

### W2 — Quick Flip Accuracy Hardening  *(Status: complete — merged to main 2026-04-23, commit `a9ca8eb`)*

> **Follow-ups flagged by W2 agent:**
> - **Dashboard hook landed on 2026-04-23 (`65be4cf`).** `/portfolio` now renders fee-drift telemetry when exposed by the API, and `get_paper_live_divergence_summary()` appends a fee-divergence CLI summary from `fee_divergence_log`. Remaining gap: exit-leg fee reconciliation is still not wired.
> - **Exit-leg fee reconciliation not yet wired.** Entry leg writes to `fee_divergence_log`; exit leg lives in `quick_flip_scalping._close_position_from_recent_fills` and `place_sell_limit_order` live branch — neither calls `record_fee_divergence(leg='exit')` today. Trivial follow-up.
> - `QUICK_FLIP_DISABLE_AI` is read via `os.environ.get` inline. Move to `settings.trading` once the surface area calms down (W1 already merged).
> - If `get_orderbook` fails on the paper entry path, Gap 1 silently falls back to the old best-ask snapshot. Good enough for tests; call out if W3/W4 rely on depth protection being on.
- **2026-04-24 automation follow-up:** both of the stale notes above are now resolved locally: exit-leg fee divergence is already wired and covered in `tests/test_quick_flip_scalping.py` / `tests/test_execute.py`, and the quick-flip AI fallback flag now lives on `settings.trading.quick_flip_disable_ai` with docs/env-template coverage.
- **Owner:** Senior Developer
- **Depends on:** none (parallel-safe)
- **Goal:** Close the three known gaps in [src/strategies/quick_flip_scalping.py](src/strategies/quick_flip_scalping.py) and [src/jobs/execute.py](src/jobs/execute.py).
- **Specifics:**
  1. **Entry snapshot drift** ([execute.py:639-715](src/jobs/execute.py#L639-L715)) — paper entry uses a single best-ask snapshot. Add a "depth-aware" simulation that walks the visible book, applies partial fills, and reports realized average price. This matches what a FOK limit can actually achieve.
  2. **Resting-order collision** ([execute.py:519-620](src/jobs/execute.py#L519-L620)) — multiple paper exits for the same `(market_id, side)` race during reconciliation. Key resting orders by `position_id` and add a unique constraint in [src/utils/database.py](src/utils/database.py).
  3. **Fee rounding edge case** ([src/utils/trade_pricing.py:115-148](src/utils/trade_pricing.py#L115-L148)) — paper applies the public 7%/1.75% formula at quote time; live can differ when Kalshi returns a `fee_cost` on the fill. Add a reconciliation pass that, when live `fee_cost` is available, persists the **actual** fee to `trade_logs.entry_fee` / `exit_fee` and emits a divergence metric.
  4. **AI-less fallback path** — quick flip currently requires an AI movement-prediction call ([quick_flip_scalping.py:1166-1258](src/strategies/quick_flip_scalping.py#L1166-L1258)). Add `--no-ai` mode that uses pure recent-trade momentum + book-depth heuristics (already partially encoded in lines 1120-1142). This keeps quick flip running when the daily AI budget is exhausted or Codex is unreachable.
- **Done when:** new tests in [tests/test_quick_flip_scalping.py](tests/test_quick_flip_scalping.py) and [tests/test_execute.py](tests/test_execute.py) cover all four cases; divergence metric appears in dashboard portfolio view.

### W3 — Paper-Trading Replay Harness  *(Status: complete — merged to main 2026-04-23, commit `bbe3e97`)*

> **Follow-ups flagged by W3 agent:**
> - `KALSHI_REPLAY_MODE=1` env hook is currently unused in production code; it's the documented escape hatch if strategy/execute ever needs to branch on replay-mode. W2 could adopt it instead of the mock-client path if it wants to short-circuit network calls.
> - Default per-tick snapshot cost is ZERO extra HTTP requests (inline-from-`markets` payload). The "full" path in `write_market_snapshots` adds one `get_orderbook` + one `get_market_trades` per ticker — useful for targeted high-fidelity capture, not the default.
> - `book_top_5_json` always includes all four `yes_bids/yes_asks/no_bids/no_asks` arrays, synthesized from market info when the orderbook API omits them. Downstream replay consumers never have to re-derive.
- **Owner:** AI Engineer or Backend Architect
- **Depends on:** none (can start parallel; informs W4)
- **Goal:** Re-run the last N days of recorded order-book snapshots and live trades against the paper-execution code path, then assert simulated P&L tracks live within tolerance.
- **Approach:**
  - Add a snapshot writer to [src/jobs/ingest.py](src/jobs/ingest.py) that periodically dumps `(timestamp, ticker, book_top_5, last_trade)` to a new `market_snapshots` SQLite table (or JSONL file under `data/snapshots/`). Default sample rate: every quick-flip scan tick.
  - New: `scripts/replay_paper.py` — feeds snapshots through [src/strategies/quick_flip_scalping.py](src/strategies/quick_flip_scalping.py) and [src/jobs/execute.py](src/jobs/execute.py) in paper mode, rebuilds a parallel `trading_system_replay.db`, and prints a side-by-side P&L diff with the live `trade_logs` table.
  - Tolerance gate: paper P&L within ±5% of live P&L per 100 trades; per-trade fee delta < $0.01.
- **Done when:** `python scripts/replay_paper.py --days 7 --strategy quick_flip` produces a deterministic report and exits non-zero if tolerance is breached. CI (or a smoke target) runs it weekly.

### W4 — Shadow-Trade Mode  *(Status: complete — initial merge 2026-04-23 commit `21ecaef`; auto-pause-on-drift hook merged 2026-04-27)*
- **Follow-ups flagged by Codex after merge:**
  - 2026-04-27 update: the previously-deferred "auto-pause on drift threshold" enforcement hook is now landed. Default-off feature flag `SHADOW_DRIFT_AUTO_PAUSE_ENABLED` plus `SHADOW_DRIFT_MAX_AVG_ABS_ENTRY_DELTA_CENTS` / `SHADOW_DRIFT_MAX_TOTAL_ENTRY_COST_DELTA_USD` / `SHADOW_DRIFT_MIN_MATCHED_ENTRIES` thresholds on `settings.trading`. `PortfolioEnforcer.evaluate_shadow_drift_halt(strategy, db_manager)` reads `summarize_shadow_order_divergence` and records a halt via the existing `strategy_halts` table (idempotent: returns `(True, "already_halted")` on re-evaluation). Wired in once per `src/jobs/track.py:run_tracking` pass for both `STRATEGY_LIVE_TRADE` and `STRATEGY_QUICK_FLIP`. `cli.py status` surfaces a `drift halt: <reason> (avg delta $X, cost drift $Y)` line per halted strategy. Coverage: `tests/test_shadow_drift_halt.py` (7 cases). All defaults preserve existing behavior — no production path changes unless the operator opts in.
  - `shadow_orders` is now the canonical comparison lane for order-drift telemetry. Older databases without that table still fall back to legacy `simulated_orders.live` splits in the dashboard repository.
- **Owner:** Senior Developer
- **Depends on:** **W2** (accuracy fixes), **W3** (snapshot infra)
- **Goal:** Run paper and live executions side-by-side on the same signals so we can observe drift continuously, not just historically.
- **Approach:**
  - New CLI flag: `python cli.py run --shadow` (paper-only orders, but logs the *would-be* live order to a new `shadow_orders` table with the same schema as `simulated_orders`).
  - On every settlement / fill, compute `(paper_pnl - live_pnl)` per trade and per category, surface in the `/portfolio` dashboard route.
  - Auto-pause if drift exceeds a configurable threshold.
- **Done when:** dashboard shows a "Paper vs Live divergence" panel with rolling 24h / 7d windows, and `cli.py status` prints a one-line drift summary.

### W5 — Live-Trade Multi-Agent Decision Loop  *(Status: complete in-tree as of 2026-04-26 — paper/shadow/live parity locked by `tests/test_live_trade_parity.py` and `tests/test_live_trade_parity_stress.py`, agent-debate end-to-end locked by `tests/test_live_trade_agent_debate.py`, dedicated CLI runtime + main loop integration shipped, dashboard feed + push-refresh notifier wired. No production gaps remain on this lane.)*
- **Follow-ups flagged by 2026-04-24 review:**
  - `src/jobs/live_trade.py` now runs scout -> specialist -> final synth in paper and live modes. Non-`QUICK_FLIP` intents call the existing generic executor with `live_mode=True`, and persisted decision rows now mark `paper_trade` / `live_trade` truthfully.
  - Dedicated `python cli.py run --live-trade` now supports paper, shadow, and live runtimes; the standalone loop also runs the quick-flip manager each cycle so quick-flip exits / repricing stay active when the loop is run on its own.
  - Live `QUICK_FLIP` intents now require `ENABLE_LIVE_QUICK_FLIP=1` and route through the existing quick-flip machinery. `src/jobs/trade.py` now runs `run_live_trade_loop_cycle()` inside the main AI-ensemble runtime for paper, shadow, and live modes, so the embedded and dedicated paths share the same W5 loop. The remaining product gap is broader parity proof.
  - 2026-04-26 local follow-up: generic `LIVE_TRADE` intents now persist `execution_mode="shadow"` in execution payloads when the worker runtime is shadow, so dashboard/debug payloads no longer collapse shadow runs into paper labels.
  - 2026-04-26 safety follow-up: guardrail failures fail closed, short-hold quick-flip intents use the quick-flip enforcer bucket before routing, failed generic executions void their pending position row, and generic execution refuses ask drift above the approved limit in both paper and live mode.
  - 2026-04-26 follow-up: paper/shadow/live parity is now locked by `tests/test_live_trade_parity.py`; agent-debate end-to-end is locked by `tests/test_live_trade_agent_debate.py`. Pre-existing `LLMQuery` dataclass-ordering bug (introduced 78fa342) made the entire live-trade test surface uncollectible on Python 3.13+; minimal one-line reorder in `src/utils/database.py` plus targeted typo/signature fixes in `tests/test_live_trade_job.py` are bundled with this pass. Note: `test_live_trade_loop_shadow_mode_records_real_shadow_telemetry` still fails because its fixture orderbook produces a depth-walk average that exceeds the new W7 ask-drift guard - separate fixture refresh needed.
- **Owner:** AI Engineer (lead) + Sales Engineer–style subagent for prompt design
- **Depends on:** **W1** preferred (so Codex powers the loop) but can prototype on OpenAI/OpenRouter first
- **Goal:** Replace the single-shot LLM call inside [src/data/live_trade_research.py](src/data/live_trade_research.py) with a real multi-agent loop tuned for short-dated, in-play markets.
- **Design:**
  1. **Scout agent** — cheap/fast model, scans the ranked event list every N seconds and shortlists 3–5 markets worth deeper analysis. Default model: `x-ai/grok-4.1-fast` or Codex equivalent.
  2. **Specialist agents** — focus-type-specific:
     - **Sports specialist** — consumes live score / drive / period from the sports adapter ([server/src/services/external/sportsDataService.ts](server/src/services/external/sportsDataService.ts)).
     - **Crypto specialist** — consumes BTC OHLC + funding from CoinGecko ([live_trade_research.py:998-1020](src/data/live_trade_research.py)).
     - **Macro specialist** — consumes news sentiment + economic-calendar context from [src/data/news_aggregator.py](src/data/news_aggregator.py).
  3. **Risk gate** — re-uses [src/agents/risk_manager_agent.py](src/agents/risk_manager_agent.py); enforces category-confidence multipliers ([settings.py:256-261](src/config/settings.py#L256-L261)).
  4. **Trader synth** — re-uses [src/agents/trader_agent.py](src/agents/trader_agent.py) and [src/agents/ensemble.py](src/agents/ensemble.py) for the final decision; emits a structured order intent.
- **Wire-up:**
  - New job module: `src/jobs/live_trade.py`. Cron-driven from [cli.py](cli.py) under a new `python cli.py run --live-trade` flag. Supports paper, shadow, or live execution semantics on the dedicated loop path.
  - Persist every agent step to a new `live_trade_decisions` table for replay/debugging.
  - Surface the active decision queue on the existing `/live-trade` Next.js route ([web/app/live-trade/page.tsx](web/app/live-trade/page.tsx)) via a new SSE topic.
- **Done when:** the loop runs end-to-end in paper mode, executes via `quick_flip_scalping` order machinery when the trader's intent is a sub-30-min flip, and the `/live-trade` page shows live decisions streaming in.

### W6 — Focus-Type Data Adapter Hardening  *(Status: complete — merged to main 2026-04-23, commit `d9b350d`)*

> **Follow-ups flagged by W6 agent:**
> - Node-side `sportsDataService.ts` capabilities NOT mirrored in Python: `fetchTeamSchedule`, `fetchSummary` (play-by-play, leaders, injuries, boxscore). If a W5 specialist needs any of these, grow `SportsAdapter.fetch_summary(event_id, league)` / `fetch_team_schedule(league, team_id)`. Until then those enrichments still round-trip through the Node `/api/live-trade` path.
> - No `requirements.txt` changes — `httpx`, `feedparser`, `structlog` were already present. W6 agent notes `feedparser` is categorized under "News & sentiment pipeline" but macro adapter shares it; cosmetic recategorization if anyone cares.
> - All adapters import fine but are NOT yet called from production code; W5 is the intended first consumer.
- **Owner:** Data Engineer subagent
- **Depends on:** parallel with W5; W5 is the consumer
- **Goal:** Make the per-category enrichment robust enough for the live-trade agents to depend on.
- **Specifics:**
  - **Sports:** confirm [server/src/services/external/sportsDataService.ts](server/src/services/external/sportsDataService.ts) covers NCAAB / NBA / NFL live state (score, period, possession). Add Python-side mirror in `src/data/sports_adapter.py` so the agents can pull without going through the Node server.
  - **Crypto:** extend `live_trade_research.py` BTC fetch to also pull funding + 1m/5m bars; add `src/data/crypto_adapter.py`.
  - **Economics:** wire an economic-calendar source (Trading Economics free RSS, or scrape the Kalshi event description). Add `src/data/macro_adapter.py`.
  - All adapters expose the same `async fetch_context(market) -> dict` contract for the W5 agents.
- **Done when:** each adapter has a unit test and a 60-line README in `docs/data_adapters/`.

### W7 — Risk Guardrails for Live  *(Status: complete — merged to main 2026-04-23, commit `8ca31a4`)*

> **Follow-ups flagged by W7 agent (CRITICAL — blocks W5):**
> - **`PortfolioEnforcer` is now wired into quick-flip entries as of 2026-04-23 (`65be4cf`), but the broader live trade-execution flow is still incomplete.** `src/strategies/quick_flip_scalping.py` now checks portfolio budgets before opening a quick-flip trade. Remaining integration work is still needed in `src/jobs/execute.py`, `src/jobs/decide.py`, and the future W5 live-trade loop order-placement path.
> - `python cli.py status` currently requires Kalshi API access (it hits `/get_balance`) and the new strategy-budget block is appended inside that same code path. In envs without Kalshi creds, budgets remain invisible. Restructure `cmd_status` to surface budgets independent of API reachability — noted as out of scope here.
> - Per-strategy env vars (`QUICK_FLIP_DAILY_LOSS_BUDGET_PCT`, `LIVE_TRADE_MAX_TRADES_PER_HOUR`, etc.) are read via `os.environ.get` with `TODO: promote to TradingConfig after W1 merges` markers. W1 is now merged — a cleanup pass can migrate these into `settings.trading`.
> - 2026-04-23 local follow-up: `portfolio_enforcer` now treats `directional_trading`, `portfolio_optimization`, and `immediate_portfolio_optimization` as `live_trade` aliases for daily-loss, trade-rate, and open-position checks, and the directional decision/allocation paths now consult the enforcer before returning or placing positions. Remaining integration work is the future W5 live-trade loop.
> - 2026-04-26 follow-up: W5 live-trade loop now calls `PortfolioEnforcer` with `MODE_LIVE` when `shadow_mode_enabled=True`, so shadow and live share parity guards for hourly rate cap, open-position caps, and daily-loss halts.
> - 2026-04-26 follow-up (today): the two stale W7 items above are now resolved in-tree. (a) Per-strategy env vars are loaded directly into `settings.trading` via `field(default_factory=lambda: float(os.getenv(...)))` for `quick_flip_daily_loss_budget_pct` ([src/config/settings.py:399-401](src/config/settings.py#L399-L401)), `quick_flip_max_trades_per_hour` ([src/config/settings.py:405-407](src/config/settings.py#L405-L407)), `live_trade_daily_loss_budget_pct` ([src/config/settings.py:456-458](src/config/settings.py#L456-L458)), and `live_trade_max_trades_per_hour` ([src/config/settings.py:462-464](src/config/settings.py#L462-L464)); `portfolio_enforcer.py` reads them via `getattr(trading, ...)` rather than `os.environ.get`. (b) `cmd_status` already surfaces budgets independent of Kalshi API reachability — the `client.get_balance()` call is wrapped in a try/except that records `api_error` ([cli.py:816-832](cli.py#L816-L832)), and `_print_strategy_budget_status(strategy_budget_portfolio_value)` is called unconditionally with a `local_portfolio_estimate` fallback when the API is down ([cli.py:896-901](cli.py#L896-L901)).
- **Owner:** Backend Architect
- **Depends on:** W2 fee-divergence metric is helpful but not required
- **Goal:** Make sure the existing portfolio enforcer and category scorer kick in on the new live-trade flow as well as the legacy decide path.
- **Specifics:**
  - Per-strategy circuit breakers (quick flip vs live trade) in [src/strategies/portfolio_enforcer.py](src/strategies/portfolio_enforcer.py).
  - Hourly trade-rate cap for live trade (default 20/hr, per [TradingConfig.max_trades_per_hour](src/config/settings.py#L287)).
  - Per-strategy daily-loss budget (e.g. quick flip 5% of bankroll, live trade 5%) with a hard halt when breached.
  - Make sure `--shadow` mode reads the same config so paper and live share guardrails.
- **Done when:** [tests/test_cli_safety.py](tests/test_cli_safety.py) covers the new circuit breakers, and `python cli.py status` shows budget remaining per strategy.

### W8 — Cost & Budget Instrumentation  *(Status: complete in-tree as of 2026-04-26 — `codex_client.py` writes a `codex_quota_tracking` row after every successful CLI call (best-effort fallback when the CLI is silent), plan-tier accounting (`requests_used/limit/remaining/reset_at` + `tokens_*`) is persisted and rendered in `/portfolio` and `cli.py status`, dashboard prefers snapshot rows over inferred `llm_queries` usage. No production gaps remain on this lane.)*
- **Follow-ups flagged by Codex after merge:**
  - Provider/role/strategy spend telemetry now renders in the portfolio route using `analysis_requests.provider` plus the current `llm_queries.provider` / `llm_queries.role` / `llm_queries.strategy` fields. The earlier explicit `llm_queries.provider` schema cleanup is now in-tree; remaining cleanup is first-class quota accounting rather than provider attribution.
  - Codex quota-vs-dollar accounting is still only partially represented (`cost_usd=0`, runtime query counts, and provider rollups). The UI now labels this as Codex usage rather than quota until a dedicated persisted quota/limit/reset field exists.
  - 2026-04-23 local follow-up: the CLI/database provider summary now uses a shared recent 7-day window for both provider totals and logged-query totals, and the Node dashboard repository no longer assumes `analysis_requests` has a `tokens_used` column when aggregating provider spend. Regressions landed in `tests/test_llm_query_provider.py` and `server/tests/dashboardRepository.test.ts`.
  - 2026-04-24 automation follow-up: the CLI Codex quota suffix now counts only `llm_queries` request/token rows, matching `server/src/repositories/dashboardRepository.ts` instead of inflating quota telemetry with `analysis_requests`.
  - 2026-04-26 implementation follow-up: `codex_quota_tracking` now exists as a durable quota snapshot table, `DatabaseManager` can record/read snapshots with `llm_queries` fallback, CLI/provider summaries include limit/remaining/reset details when present, and the dashboard repository prefers quota snapshots over legacy inferred usage.
  - 2026-04-26 plan-tier accounting follow-up: `codex_quota_tracking` now persists explicit `requests_used` / `requests_limit` / `requests_remaining` / `requests_reset_at` and `tokens_used` / `tokens_limit` / `tokens_remaining` / `tokens_reset_at` columns alongside the legacy primary triplet (added via `ALTER TABLE ... DEFAULT NULL` migration so existing rows keep working). `CodexClient._extract_quota_signals` parses both structured `rate_limit` JSON blocks and plain-text `plan: Plus` / `requests: 12/100 (resets ...)` patterns, and `_record_quota_snapshot` writes a snapshot row after every successful CLI call with `source="codex-cli"` when first-class signals are present and `source="codex-cli-best-effort"` otherwise. `DatabaseManager.get_latest_codex_quota_snapshot` and the expanded `get_codex_quota_summary` surface the new fields, the CLI provider summary now appends `plan <tier>` and token-side limits, and `/portfolio` now renders the plan tier in the Codex card and per-window request/token "X / Y · Z remaining · resets ..." attribution. Coverage: `tests/test_codex_client.py::TestQuotaSignalExtraction` exercises plain-text + structured extraction and end-to-end snapshot persistence; `tests/test_database.py` round-trips a snapshot with all new fields and asserts the `llm_queries` fallback still triggers when no snapshot exists; `server/tests/dashboardRepository.test.ts` adds a "prefers codex_quota_tracking snapshot over llm_queries inferred usage" case asserting plan/limit/remaining/reset (request and token sides).
  - 2026-04-26 follow-up (this pass): `src/clients/codex_client.py` now actually fires a `codex_quota_tracking` write after every successful CLI call. A new `_extract_quota_signals()` helper parses any `rate_limit` / `quota` JSON block or `rate-limit: N/M`, `remaining=`, `resets=` text the CLI emits; when the CLI is silent, we still write a best-effort row tagged `source="codex-cli-best-effort"` so the dashboard / `cli.py status` always show fresh used counts. Also fixed two real W8 regressions blocking this lane: `LLMQuery` had non-default `market_id` after default `role` (broke the dataclass on Python 3.14), and `DatabaseManager.log_llm_query` had `await db.commit()` outside the `async with` block ("no active connection"). New tests: `tests/test_codex_client.py::TestQuotaSignalExtraction` (3 cases) plus snapshot persistence cases on `TestCodexClientAccounting`; `tests/test_cli_safety.py::test_ai_spend_provider_breakdown_safe_with_empty_quota_table`; `server/tests/dashboardRepository.test.ts::"prefers codex_quota_tracking snapshot over llm_queries inferred usage"`.
- **Owner:** Analytics Reporter subagent
- **Depends on:** W1 (Codex calls need to land in `daily_cost_tracking` with `cost_usd=0`)
- **Goal:** A single panel that shows AI spend split by provider (Codex / OpenAI / OpenRouter), by agent role, and by strategy, plus quota-vs-dollar accounting for the Codex plan.
- **Specifics:**
  - Add provider/role columns to [src/utils/database.py](src/utils/database.py) `llm_queries` table if missing.
  - Extend [src/jobs/performance_dashboard_integration.py](src/jobs/performance_dashboard_integration.py) to expose the breakdown.
  - New panel on `/portfolio` (or a new `/spend` page) consuming the data.
- **Done when:** dashboard panel renders with at least 24h of real data and `cli.py status` prints today's spend per provider.

### W9 — Dashboard Live-Trade Polish  *(Status: complete in-tree as of 2026-04-26 — `useLiveStream` shared hook landed in `web/lib/use-live-stream.ts`, `live-trade-decisions-panel.tsx` migrated off inline SSE bookkeeping, push-based refresh hook landed (`POST /internal/live-trade/notify-refresh` + `LiveTradeRefreshNotifier`), cursor poll retained as fallback. No production gaps remain on this lane.)*
- **Follow-ups flagged by 2026-04-24 review:**
  - `/live-trade` already mounts `LiveTradeDecisionsPanel`, subscribes to the `live-trade-decisions` SSE topic, and exposes thumbs-up / down feedback writes. The old "live SSE/feedback work remains" note is stale.
  - `server/src/services/liveStreamHub.ts` now polls a lightweight decision/runtime/feedback cursor every <=1s and refreshes the SSE snapshot when SQLite state changes, with the existing slower full-refresh interval still acting as fallback.
  - The current remaining polish is mostly about optional migration onto the shared stream hook and any future push-based Python -> Node refresh hook if cursor polling proves insufficient.
  - 2026-04-26 local polish: `/live-trade` now defaults back to the strategy-aligned 12h expiry window and its page copy describes the W5 persisted decision feed instead of the retired Streamlit carry-over.
  - 2026-04-26 implementation follow-up: `liveStreamHub` now suppresses closed-DB refresh errors, catches interval refresh failures, and removes the default EventEmitter listener cap for valid SSE fanout. `/portfolio` labels dollar spend as metered spend and no longer fabricates a fresh generated timestamp when the payload omits one.
  - 2026-04-26 frontend follow-up: extracted a shared `useLiveStream(topic, options)` hook in `web/lib/use-live-stream.ts` that owns `EventSource` lifecycle, reconnect counting, parser-error tolerance, status reporting (`live` / `reconnecting` / `error` / `stale`), and an opt-in HTTP fallback poll. Migrated `web/app/live-trade/live-trade-decisions-panel.tsx` off its inline SSE bookkeeping onto the new hook while preserving the existing decision-feed precedence (`selectLatestDecisionFeed`) and HTTP fallback semantics. `/portfolio` is server-rendered and never opened a stream of its own, so no migration was needed there. Pure helpers (`tryParseEnvelope`, `applyMessage`, `shouldUseHttpFallback`, etc.) are covered by `web/lib/use-live-stream.test.ts` (15 cases) alongside the existing `web/lib/live-trade-decision-feed.test.ts` precedence/fallback coverage. Push-based Python -> Node refresh hook remains the open W9 follow-up.
  - 2026-04-26 push-refresh follow-up: the open push-hook is now wired. New endpoint `POST /internal/live-trade/notify-refresh` on the Node server (`server/src/app.ts`) accepts an optional `{ topic }` body and triggers `liveStreamHub.refreshLiveTradeDecisions()` directly. Auth is a shared-secret `x-internal-token` header that must match `LIVE_TRADE_INTERNAL_REFRESH_TOKEN` (env var documented in `env.template`); when the secret is unset, the endpoint returns 503 so it cannot be hit anonymously. Python side: `src/jobs/live_trade.py` adds `LiveTradeRefreshNotifier` (httpx-backed, fire-and-forget, swallows network/timeout/HTTP errors and logs a single warning so the trading loop is never blocked); the loop fires it from `_persist_runtime_state` after every persisted decision/runtime-state/feedback write. When `LIVE_TRADE_NOTIFY_URL` is unset (typical dev env without a Node server), the notifier silently no-ops. Cursor polling on `liveStreamHub` is intentionally kept as the safety-net fallback. Coverage: `tests/test_live_trade_notify.py` (9 cases) locks fire-once + env-disabled + transport-error swallowing semantics; `server/tests/liveTradeRefreshNotify.test.ts` (2 cases) locks the 200-success / 401-auth-failure paths.
- **Owner:** Frontend Developer
- **Depends on:** W5 (data shape) and W8 (cost panel)
- **Goal:** Make `/live-trade` and `/portfolio` actually useful for monitoring the new flow.
- **Specifics:**
  - `/live-trade` ([web/app/live-trade/page.tsx](web/app/live-trade/page.tsx)) — show live agent decisions streaming in (new SSE topic from W5), with action buttons to thumbs-up / down a decision (writes to a feedback table for later evaluation).
  - `/portfolio` ([web/app/portfolio/page.tsx](web/app/portfolio/page.tsx)) — paper-vs-live divergence panel from W4, AI-spend panel from W8, per-strategy P&L breakdown.
  - Keep decision-feed heartbeat metadata and page banners aligned so operators can tell whether the visible mode comes from verified worker telemetry (`live_trade_runtime_state`) or dashboard defaults.
- **Done when:** A user can sit on `/live-trade` and `/portfolio` for 10 minutes and confidently say what the bot is doing and how it's doing.

### W10 — Test Coverage Gaps  *(Status: complete in-tree as of 2026-04-26 — paper/shadow/live parity locked by `tests/test_live_trade_parity.py` (single-cycle) + `tests/test_live_trade_parity_stress.py` (N-cycle, concurrent-event, guardrail-trip), agent-debate end-to-end locked by `tests/test_live_trade_agent_debate.py`, refresh-notify acceptance locked by `tests/test_live_trade_notify.py` + `server/tests/liveTradeRefreshNotify.test.ts`. The 2026-04-26 plan note flagged the timezone-flaky `test_live_trade_aliases_share_daily_loss_and_trade_rate_budget` as the only outstanding pre-existing failure; that flake was repaired on 2026-04-27 by anchoring the test on a frozen UTC noon.)*
- **Follow-ups flagged by 2026-04-24 review:**
  - Added `tests/test_live_trade_loop_regressions.py` to cover the no-events skip branch and the specialist-payload fallback branch without modifying the existing dirty `tests/test_live_trade_job.py`.
  - Added live-mode execution assertions in `tests/test_live_trade_job.py` so the loop now proves it reaches the safe generic live executor, marks persisted decision rows with truthful paper/live flags, and stores live positions when `live_trading_enabled` is on.
  - Added dedicated-loop quick-flip coverage in `tests/test_live_trade_job.py` for live quick-flip opt-in blocking, opted-in live quick-flip routing, and the standalone loop's per-cycle quick-flip manager behavior.
  - Added a loop-level shadow-mode success case in `tests/test_live_trade_job.py` that uses the real `execute_position` path, preserves `runtime_mode="shadow"`, and verifies `shadow_orders` divergence telemetry is written for the executed entry.
  - 2026-04-26 local follow-up: the shadow-mode success case now also asserts generic live-trade execution payloads carry `execution_mode="shadow"` instead of the older paper fallback label.
  - Added a repeated-cycle regression in `tests/test_live_trade_job.py` so the same market cannot be reopened on the next loop pass while an earlier position is still open; the newest execution row now stays auditable as `status="skipped"` / `error="existing_position"`.
  - Hardened the Node acceptance harness so `server/tests/liveTradeFeedbackSse.test.ts` parses the intended JSON payload even when child-process logs/warnings are present, added SSE coverage for external SQLite writes that change the decision feed, repaired the new quota/strategy-P&L fixtures plus the refresh-cursor repository coverage in `server/tests/dashboardRepository.test.ts`, and added focused web-side feed precedence / fallback coverage in `web/lib/live-trade-decision-feed.test.ts`.
  - 2026-04-26 safety coverage: tests now cover fail-closed live-trade guardrails, paper/live ask drift blocking above the approved entry limit, failed generic execution voiding its position row, closed recent positions not double-counting against hourly caps, and invalid `--live-trade` CLI mode combinations.
  - 2026-04-26 follow-up: paper/shadow/live parity is now locked by `tests/test_live_trade_parity.py` (generic + quick-flip route, plus stress-parity repeated-cycle skip across all three runtimes); agent-debate end-to-end is locked by `tests/test_live_trade_agent_debate.py` (scout -> sports specialist -> macro specialist -> bull/bear/risk/trader debate -> persisted execution row, all without real Kalshi or LLM calls). Pre-existing `LLMQuery` dataclass-ordering bug had made the entire `tests/test_live_trade_*` surface uncollectible on Python 3.13+; that plus four typo/signature errors in `tests/test_live_trade_job.py` were repaired so the suite now collects and runs. Remaining coverage gap: `test_live_trade_loop_shadow_mode_records_real_shadow_telemetry` fixture orderbook needs a refresh to clear the new W7 ask-drift guard.
  - 2026-04-26 stress-parity follow-up: confirmed the previously flagged `test_live_trade_loop_shadow_mode_records_real_shadow_telemetry` is now passing on main (no longer broken by the W7 ask-drift guard). New file `tests/test_live_trade_parity_stress.py` (3 cases) layers broader stress coverage on top of the existing parity suite without rewriting it: (1) **N-cycle parity** runs the same fixed market list across paper / shadow / live for repeated cycles and asserts each runtime persists the same set of decision rows, with `runtime_mode`, skip reasons, and per-payload `execution_mode` matching the configured runtime exactly; (2) **concurrent-event parity** simulates a cycle with a quick-flip-eligible market and a generic market resolving in the same tick, and asserts the order-of-execution and persisted decision sequence are identical across all three runtimes; (3) **guardrail-trip parity** trips the W7 hourly trade-rate cap mid-cycle and asserts the same decision rows are skipped with the same `under_budget` / `existing_position` reason in every runtime. No production code touched — this lane is test-only.
- **Owner:** API Tester or Test Results Analyzer
- **Depends on:** W1, W2, W4, W5 (tests live alongside the code they cover)
- **Goal:** Fill in the coverage holes flagged during exploration.
- **Specifics:**
  - End-to-end agent-debate test on the live-trade route (no real Kalshi calls — use recorded snapshots from W3).
  - Paper-vs-live parity test that asserts the same input produces the same logical decision (entry side, qty bucket, exit price tier).
  - Codex CLI subprocess test (W1).
  - Shadow-mode drift-threshold test (W4).
- **Done when:** `pytest --cov=src` shows ≥80% coverage on the strategies, jobs, and clients packages.

### W11 — Legacy Cleanup  *(Status: substantially complete as of 2026-04-27 — three of the four candidates resolved per user direction. Only `paper_trader.py` remains intentionally retained behind its `DeprecationWarning` until the user signals it should be deleted.)*

- 2026-04-27 user direction landed:
  - **(a) `paper_trader.py`**: keep deprecated. User confirmed `cli.py run --paper` is the canonical paper-trading runtime against live market data and the deprecation banner is correct. No further action.
  - **(b) `cli.py run --beast`**: user chose "Go with Unified runtime." Top-level `beast_mode_bot.py` deleted; `BeastModeBot` renamed to `UnifiedTradingBot` and relocated to `src/runtime/unified_bot.py`. The `--beast` CLI flag is retained as a settings-override mode pointing at `UnifiedTradingBot` — only the file/module is gone. Updated importers: `cli.py` (3 sites + dashboard fallback), `tests/test_cli_safety.py` (mock injection now at `src.runtime.unified_bot`, fake class renamed `FakeUnifiedTradingBot`), `pyproject.toml` `py-modules` list, `scripts/init_database.py`, `README.md`, `CONTRIBUTING.md`. `python -c "import beast_mode_bot"` now correctly fails with `ModuleNotFoundError`.
  - **(c) `XAIClient`**: user chose Option 1 — extract the load-bearing daily-counter mirror logic to `src/clients/shared_types.py` as `mirror_provider_usage(...)`, migrate every caller, delete the shim. Done. 17 caller files migrated to `ModelRouter` directly: `paper_trader.py`, `beast_mode_dashboard.py` (imports + `__init__` only), 4 scripts, 5 jobs, 4 strategies, 5 tests. `tests/test_xai_client.py` retired; `tests/test_shared_types.py` (8 cases) covers the helper's snapshot/diff invariants, the no-result short-circuit, and the legacy-pickle unpickler. `LegacyPickleUnpickler` rewrites `src.clients.xai_client.DailyUsageTracker` references in on-disk pickles to `src.clients.shared_types.DailyUsageTracker` so existing tracker pickles still load. Defensive change in `src/jobs/decide.py:413`: `getattr(xai_client, "search", None)` instead of unconditional call (legacy `XAIClient.search` was already a no-op stub returning a fallback string, so production behavior is identical). `python -c "import src.clients.xai_client"` now correctly fails with `ModuleNotFoundError`.
  - **`scripts/beast_mode_dashboard.py`**: still left as compat wrapper per the original plan; user direction did not change it.

- Net W11 cleanup delta on 2026-04-27: deleted `src/clients/xai_client.py` (302 lines), `tests/test_xai_client.py` (183 lines), `beast_mode_bot.py` (486 lines); created `src/runtime/unified_bot.py` (256 lines), `src/runtime/__init__.py`, `tests/test_shared_types.py` (230 lines). Tests: 319 passed, 8 skipped, 0 failed.

- 2026-04-26 audit conclusions superseded by user direction above. Original audit (worktree `worktree-agent-a52d7b2c`) had left all four candidates citing live callers; user direction enabled the systematic migration that closed three of them this pass.
- **Owner:** Code Reviewer subagent
- **Depends on:** W1 (Codex stable), W4 (shadow mode operational), W9 (dashboard polished)
- **Goal:** Remove the old paths now that the new ones work.
- **Candidates:**
- `beast_mode_dashboard.py` / `scripts/beast_mode_dashboard.py` legacy dashboard entrypoints — keep the repo-root module canonical and retain the script path as a thin compatibility wrapper until downstream callers are gone.
- `beast_mode_bot.py` (entry point, redundant with `cli.py run --beast`).
- `src/clients/xai_client.py` (only `TradingDecision` and `DailyUsageTracker` are still imported by [src/clients/model_router.py:15](src/clients/model_router.py#L15) — extract them to a small standalone module first, then delete).
- Legacy paper signal tracker in [src/paper/tracker.py](src/paper/tracker.py) (signals-only path) — keep only the unified runtime path.
- 2026-04-26 hardening note: `scripts/beast_mode_dashboard.py` is retained as a compatibility wrapper, and `src/clients/xai_client.py` now carries explicit legacy-shim documentation for downstream callers.
- 2026-04-26 automation follow-up: `src/clients/xai_client.py` now mirrors provider usage into persisted daily counters (request_count + total_cost) so legacy daily-limit checks remain accurate across tracker reloads.
- 2026-04-26 deprecation pass (paper_trader.py): The audit was correct -- `paper_trader.py` still has a live, user-facing surface (README documented `--stats`, `--dashboard`, `--loop`, `--settle` for end users). Deletion is therefore not yet safe. Instead, a deprecation banner + module-level `warnings.warn(..., DeprecationWarning)` was added to `paper_trader.py`, and README guidance now points users at `python cli.py run --paper` (loop) and `python cli.py dashboard` (dashboard), keeping `--stats`/`--dashboard` here only as convenience helpers that already read the unified `trading_system.db`. The legacy signal-tracker functions in `src/paper/tracker.py` (`log_signal` / `settle_signal` / `get_pending_signals` / `_get_legacy_stats` / `get_all_signals`) and the legacy fallback in `src/paper/dashboard.py` were intentionally left in place -- they are still reachable via the deprecated loop and the dashboard fallback. Removing them (and `paper_trader.py` itself) is the next concrete step once telemetry/issues confirm no end users hit the `DeprecationWarning`. Beast-mode and `XAIClient` migrations remain punted to a follow-up pass per the audit.
- 2026-04-26 audit follow-up (Code Reviewer subagent, branch `worktree-agent-a52d7b2c`): grep across `src/`, `tests/`, `scripts/`, `cli.py`, `server/`, `web/`, `paper_trader.py` confirmed every W11 candidate still has live callers, so no removals were made on this pass.
  - `beast_mode_bot.py` -> **Left**. Imported by `cli.py` at three call sites (the `--beast` flag instantiates `BeastModeBot`), referenced by `tests/test_cli_safety.py`, declared in `pyproject.toml` `py-modules`, and documented in README/CONTRIBUTING. The plan's "redundant with `cli.py run --beast`" premise is incorrect: `cli.py run --beast` IS the wrapper around this module.
  - `src/clients/xai_client.py` -> **Left**. The `XAIClient` class is still imported by `paper_trader.py`, `beast_mode_dashboard.py`, `scripts/extract_grok_analysis.py`, `scripts/performance_analysis.py`, `scripts/quick_performance_analysis.py`, `scripts/test_quick_flip_strategy.py`, `tests/test_decide.py`, `tests/test_end_to_end.py`, `tests/test_live_order_execution.py`, `tests/test_xai_client.py`, `tests/test_codex_client.py`, and `tests/test_trade_job.py`. The 2026-04-26 provider-usage mirror logic is also load-bearing for legacy daily-counter parity and must not be ripped out.
  - `src/paper/tracker.py` legacy signals path -> **Left**. `log_signal` / `settle_signal` / `get_pending_signals` are still called by `paper_trader.py` (a top-level entry point that ships and is out of W11 scope). `_get_legacy_stats` and `get_all_signals` also feed the `get_dashboard_snapshot` legacy fallback used by `src/paper/dashboard.py`. Pruning the signals-only path requires retiring `paper_trader.py` first, which is not in W11 scope.
  - `scripts/beast_mode_dashboard.py` -> **Left as compat wrapper** (per plan). Verified the wrapper just resolves the repo-root `beast_mode_dashboard.py` and calls its `main()`; no extra logic to drop.
  - Pytest baseline on worktree (clean tree, before any edits): `tests/test_paper_dashboard.py` fails to collect because `src/utils/database.py` triggers a Python 3.14 dataclass strictness error (`non-default argument 'market_id' follows default argument 'role'`); `tests/test_xai_client.py::test_xai_client_mirror_provider_usage_persists_when_provider_does_not_track` fails because it `monkeypatch.setattr`s `OpenAIClient` on the `xai_client` module, but the module imports `OpenAIClient` lazily inside `_get_provider_client`. Both pre-date this audit and are not introduced by W11.
  - Net unblocking work needed before W11 can finish: (a) retire `paper_trader.py` so the legacy signal-tracker path can be pruned; (b) decide whether `cli.py run --beast` should keep the `BeastModeBot` orchestration or fold it into the unified runtime so `beast_mode_bot.py` can go away; (c) migrate the remaining `XAIClient` callers in `paper_trader.py` and the `scripts/`/`tests/` listed above onto the configured provider client directly.
- **Done when:** files removed, README updated, `pytest` still green.

---

## 4. Project Management Map

### Dependency graph (Mermaid)

```mermaid
flowchart LR
  W1[W1 Codex CLI] --> W5[W5 Live-Trade Loop]
  W1 --> W8[W8 Cost Instrumentation]
  W2[W2 Quick Flip Hardening] --> W4[W4 Shadow Mode]
  W3[W3 Replay Harness] --> W4
  W2 -. helpful .-> W7[W7 Risk Guardrails]
  W5 --> W9[W9 Dashboard Polish]
  W6[W6 Data Adapters] -. enriches .-> W5
  W8 --> W9
  W1 --> W10[W10 Test Coverage]
  W2 --> W10
  W4 --> W10
  W5 --> W10
  W1 --> W11[W11 Cleanup]
  W4 --> W11
  W9 --> W11
```

### What can run in parallel right now (Wave 1)

| Lane | Workstreams | Subagent type |
|------|-------------|---------------|
| A | **W1** Codex CLI | Backend Architect or Senior Developer |
| B | **W2** Quick Flip Hardening | Senior Developer |
| C | **W3** Replay Harness | AI Engineer / Backend Architect |
| D | **W6** Data Adapters | Data Engineer |
| E | **W7** Risk Guardrails (foundations) | Backend Architect |

### Wave 2 (after Wave 1 unlocks)

| Lane | Workstream | Unblocked by |
|------|-----------|--------------|
| A | **W4** Shadow Mode | W2 + W3 |
| B | **W5** Live-Trade Loop (full) | W1 (preferred) + W6 |
| C | **W8** Cost Instrumentation | W1 |

> 2026-04-23 update: W4 is now merged to main in `21ecaef`. W8 has a merged first slice (portfolio spend panels + CLI provider summary), but its schema/quota follow-ups remain open.

### Wave 3 (after Wave 2)

| Lane | Workstream | Unblocked by |
|------|-----------|--------------|
| A | **W9** Dashboard Polish | W5 + W8 |
| B | **W10** Test Coverage | W1, W2, W4, W5 |

### Wave 4

| Workstream | Unblocked by |
|------------|--------------|
| **W11** Legacy Cleanup | W1 + W4 + W9 stable in production-paper mode |

### Status legend (future agents update inline in §3)

- `not started` — nobody owns it yet
- `in progress (owner: <name>, branch: <branch>)`
- `blocked (reason: ...)`
- `complete (PR: #..., merged: <date>)`
- `superseded (link: ...)`

---

## 5. Verification Plan

The plan is "done" when the following can be demonstrated end-to-end:

1. `python cli.py health` reports `provider=codex` when the CLI is signed in.
2. `python cli.py run --paper` exercises both quick flip and the new live-trade agent loop, drawing AI calls from the Codex plan (no API-key cost).
3. `python scripts/replay_paper.py --days 7` exits 0 with paper P&L within ±5% of live P&L.
4. `python cli.py run --shadow` runs for 24h with the dashboard's paper-vs-live divergence panel staying under threshold.
5. `python cli.py status` shows per-strategy P&L, per-strategy daily-loss budget remaining, and per-provider AI spend.
6. `pytest --cov=src` shows ≥80% on strategies/jobs/clients with new tests for Codex, replay, shadow, and live-trade decisions.
7. After 7 trading days of clean shadow-mode results, `python cli.py run --live` is the green-light moment.

---

## 6. Critical Files Future Agents Should Touch (quick index)

- Strategy logic: [src/strategies/quick_flip_scalping.py](src/strategies/quick_flip_scalping.py), [src/strategies/unified_trading_system.py](src/strategies/unified_trading_system.py)
- Execution: [src/jobs/execute.py](src/jobs/execute.py), [src/jobs/track.py](src/jobs/track.py), [src/jobs/decide.py](src/jobs/decide.py)
- Live-trade research: [src/data/live_trade_research.py](src/data/live_trade_research.py)
- Agents: [src/agents/](src/agents/), especially [ensemble.py](src/agents/ensemble.py) and [debate.py](src/agents/debate.py)
- Provider routing: [src/clients/model_router.py](src/clients/model_router.py), [src/clients/openai_client.py](src/clients/openai_client.py), [src/clients/openrouter_client.py](src/clients/openrouter_client.py), **new** `src/clients/codex_client.py`
- Config: [src/config/settings.py](src/config/settings.py), [env.template](env.template)
- Database: [src/utils/database.py](src/utils/database.py)
- Fee math: [src/utils/trade_pricing.py](src/utils/trade_pricing.py)
- Dashboard: [web/app/live-trade/page.tsx](web/app/live-trade/page.tsx), [web/app/portfolio/page.tsx](web/app/portfolio/page.tsx), [server/src/app.ts](server/src/app.ts), [python_bridge/app/main.py](python_bridge/app/main.py)
- Tests: [tests/test_quick_flip_scalping.py](tests/test_quick_flip_scalping.py), [tests/test_execute.py](tests/test_execute.py), [tests/test_ensemble.py](tests/test_ensemble.py), [tests/test_live_trade_research.py](tests/test_live_trade_research.py)

---

## 7. Hand-off Protocol for Future Agents

When picking up a workstream:

1. Read the §3 entry for that workstream and the dependency edges in §4.
2. Update its **Status** line in §3 to `in progress (owner: <agent-type>, branch: <git branch>)`.
3. Re-explore only the files in the workstream's "Files to touch" list — don't re-explore the whole repo.
4. If you discover the workstream needs to be split, edit §3 to reflect that and update §4's PM map.
5. On completion, change Status to `complete (PR: #..., merged: <date>)` and tick the corresponding line in §5.
6. If a finding changes the plan for *other* workstreams, append a short note under that workstream's entry — don't overwrite the original intent.

If a workstream grows past ~300 lines of plan content, move its details to `docs/plans/W<#>-<slug>.md` and keep just a one-paragraph summary + link here.
