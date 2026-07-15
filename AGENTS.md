# AGENTS

Cursor project rules live in [`.cursor/rules/`](.cursor/rules/). `project-overview.mdc` always applies; other rules attach by file glob.

## Where to edit what

| Concern | Start here |
|---------|------------|
| CLI / modes / bot loop | `cli.py`, `src/runtime/unified_bot.py` |
| Decisions (debate / ensemble) | `src/agents/`, `src/jobs/decide.py` |
| Orders (paper / shadow / live) | `src/jobs/execute.py` |
| Live-trade lane | `src/jobs/live_trade.py` |
| Kalshi REST / auth | `src/clients/kalshi_client.py`, `src/utils/kalshi_auth.py` |
| LLM providers | `src/clients/model_router.py` |
| Risk gates | `src/utils/execution_safety.py`, `src/strategies/portfolio_enforcer.py` |
| Dashboard UI | `web/` |
| Dashboard API / SSE | `server/` |
| Manual analysis bridge | `python_bridge/` |
| Env names (no secrets) | `env.template` |

## LLM agent roles

| Role | File | Cursor rule |
|------|------|-------------|
| Base | `src/agents/base_agent.py` | `agent-base.mdc` |
| Forecaster | `src/agents/forecaster_agent.py` | `agent-forecaster.mdc` |
| News | `src/agents/news_analyst_agent.py` | `agent-news.mdc` |
| Bull | `src/agents/bull_researcher.py` | `agent-bull.mdc` |
| Bear | `src/agents/bear_researcher.py` | `agent-bear.mdc` |
| Risk | `src/agents/risk_manager_agent.py` | `agent-risk.mdc` |
| Trader | `src/agents/trader_agent.py` | `agent-trader.mdc` |
| Debate / ensemble / decide | `debate.py`, `ensemble.py`, `jobs/decide.py` | `agent-orchestration.mdc` |

## Domain rules

| Rule | Covers |
|------|--------|
| `kalshi-api.mdc` | Kalshi client, auth, pricing |
| `trading-runtime.mdc` | Unified bot, ingest/trade/execute/track |
| `live-trade-lane.mdc` | Live-trade job + research |
| `llm-router.mdc` | ModelRouter + provider clients |
| `risk-safety.mdc` | Safety and portfolio gates |
| `dashboard-stack.mdc` | web / server / bridge / compose |

## Non-negotiables

- Prefer `--paper` / `--shadow` before `--live`
- Agents never place Kalshi orders; prefer `execute.py` (MM / quick-flip / safe-compounder have direct order paths)
- Do not resurrect `paper_trader.py` or `xai_client.py`
- Node 24+ for the dashboard; Python 3.12+
- Never commit `.env` or private key PEMs

See also [CONTRIBUTING.md](CONTRIBUTING.md) and [README.md](README.md).
