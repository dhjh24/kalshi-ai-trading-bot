# Kalshi Automated Performance Analysis System

This subsystem handles post-trade performance review, risk checks, scheduler-driven analysis, and dashboard-facing summaries for the trading stack.

## What it does

- Runs scheduled or on-demand performance analysis against the local SQLite trading database
- Calculates portfolio health metrics such as cash reserves, capital utilization, open positions, and realized P&L
- Produces prioritized action items for critical, high, medium, and low severity issues
- Exposes dashboard-friendly summary and alert helpers through the performance integration jobs
- Supports an interactive CLI manager for status checks and emergency review flows

## Current implementation notes

- The automated performance analyzer uses `ModelRouter` for its AI-written analysis layer
- Model calls follow the same provider selection as the rest of the app: `LLM_PROVIDER=auto|codex|openai|openrouter`
- Performance analysis is separate from the new Node dashboard stack, but its outputs can still be surfaced by dashboard integration jobs
- Some constructor and variable names still say `xai_client` for compatibility with older call sites; they now receive a router-like object with `get_completion(...)`

## Main entrypoints

- `scripts/performance_system_manager.py`
- `src/jobs/automated_performance_analyzer.py`
- `src/jobs/performance_scheduler.py`
- `src/jobs/performance_dashboard_integration.py`
- `src/jobs/performance_analyzer.py`

## Typical commands

```bash
# Start the scheduled performance system
python scripts/performance_system_manager.py --start

# Run an immediate analysis
python scripts/performance_system_manager.py --analyze

# Show current status
python scripts/performance_system_manager.py --status

# Run emergency review flow
python scripts/performance_system_manager.py --emergency

# Open interactive mode
python scripts/performance_system_manager.py --interactive
```

## Scheduler controls

```bash
python scripts/performance_system_manager.py --start --daily-time 09:00 --weekly-day monday
python scripts/performance_system_manager.py --start --health-threshold 50
```

## Metrics and checks

The current analyzer focuses on:

- Cash reserve thresholds
- Capital utilization
- Position concentration
- Largest-position risk
- Manual versus automated trade performance
- Aggregate win rate and realized P&L

It stores reports and reads from the same SQLite data used elsewhere in the app, especially `positions`, `trade_logs`, and analysis report tables.

## Relationship to the dashboard

- The primary dashboard is now the Node stack launched with `python cli.py dashboard`
- Performance analysis jobs are backend utilities, not standalone UI pages
- Dashboard integrations read summaries, alerts, and health-style metrics from the performance jobs when needed

## Caveats

- The performance subsystem is still more script-oriented than the rest of the app
- Some files in this area predate the provider-routing refactor, so treat this subsystem as adjacent to the main trading runtime rather than identical to it
