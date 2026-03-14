# 🤖 Kalshi AI Trading Bot

<div align="center">

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/ryanfrigo/kalshi-ai-trading-bot?style=flat&color=yellow)](https://github.com/ryanfrigo/kalshi-ai-trading-bot/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/ryanfrigo/kalshi-ai-trading-bot?style=flat&color=blue)](https://github.com/ryanfrigo/kalshi-ai-trading-bot/network)
[![GitHub Issues](https://img.shields.io/github/issues/ryanfrigo/kalshi-ai-trading-bot)](https://github.com/ryanfrigo/kalshi-ai-trading-bot/issues)
[![Last Commit](https://img.shields.io/github/last-commit/ryanfrigo/kalshi-ai-trading-bot)](https://github.com/ryanfrigo/kalshi-ai-trading-bot/commits/main)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

**An autonomous trading bot for [Kalshi](https://kalshi.com) prediction markets powered by a five-model AI ensemble.**

Five frontier LLMs debate every trade. The system only enters when they agree.

[Quick Start](#-quick-start) · [Features](#-features) · [How It Works](#-how-it-works) · [Configuration](#configuration-reference) · [Contributing](CONTRIBUTING.md) · [Kalshi API Docs](https://trading-api.readme.io/reference/getting-started)

</div>

---

> ⚠️ **Disclaimer — This is experimental software for educational and research purposes only.** Trading involves substantial risk of loss. Only trade with capital you can afford to lose. Past performance does not guarantee future results. This software is not financial advice. The authors are not responsible for any financial losses incurred through the use of this software.

---

## 🚀 Quick Start

**Three steps to get running in paper-trading mode (no real money):**

```bash
# 1. Clone and set up
git clone https://github.com/ryanfrigo/kalshi-ai-trading-bot.git
cd kalshi-ai-trading-bot
python setup.py        # creates .venv, installs deps, checks config

# 2. Add your API keys
cp env.template .env   # then open .env and fill in KALSHI_API_KEY,
                       # XAI_API_KEY, and OPENROUTER_API_KEY

# 3. Run in paper-trading mode (no real orders)
python cli.py run --paper
```

Then open the live dashboard in another terminal:

```bash
python cli.py dashboard
```

> **Need API keys?**
> - Kalshi key + private key → [kalshi.com/account/settings](https://kalshi.com/account/settings) ([API docs](https://trading-api.readme.io/reference/getting-started))
> - xAI key → [console.x.ai](https://console.x.ai/)
> - OpenRouter key → [openrouter.ai](https://openrouter.ai/)

---

## ✅ Features

### Multi-Model AI Ensemble
- ✅ **Five frontier LLMs** collaborate on every decision — Grok-4, Claude Sonnet 4, GPT-4o, Gemini 2.5 Flash, DeepSeek R1
- ✅ **Role-based specialization** — each model plays a distinct analytical role (forecaster, bull, bear, risk manager, news analyst)
- ✅ **Consensus gating** — positions are skipped when models diverge beyond a configurable confidence threshold
- ✅ **Deterministic outputs** — temperature=0 for reproducible AI reasoning

### Trading Strategies
- ✅ **Directional trading** (50% of capital) — AI-predicted probability edge with Kelly Criterion sizing
- ✅ **Market making** (40%) — automated limit orders capturing bid-ask spread
- ✅ **Arbitrage detection** (10%) — cross-market opportunity scanning

### Risk Management
- ✅ **Fractional Kelly** position sizing (0.75x Kelly for volatility control)
- ✅ **Hard daily loss limit** — stops trading at 15% drawdown
- ✅ **Max drawdown circuit breaker** — halts at 50% portfolio drawdown
- ✅ **Sector concentration cap** — no more than 90% in any single category
- ✅ **Daily AI cost budget** — stops spending when API costs hit $50/day

### Dynamic Exit Strategies
- ✅ Trailing take-profit at 20% gain
- ✅ Stop-loss at 15% per position
- ✅ Confidence-decay exits when AI conviction drops
- ✅ Time-based exits (10-day max hold)
- ✅ Volatility-adjusted thresholds

### Observability
- ✅ **Real-time Streamlit dashboard** — portfolio value, positions, P&L, AI decision logs
- ✅ **Paper trading mode** — simulate trades without real orders; track outcomes on settled markets
- ✅ **SQLite telemetry** — every trade, AI decision, and cost metric logged locally
- ✅ **Unified CLI** — `run`, `dashboard`, `status`, `health`, `backtest` commands

---

## 🧠 How It Works

The bot runs a four-stage pipeline on a continuous loop:

```
  INGEST               DECIDE (5-Model Ensemble)    EXECUTE       TRACK
 --------             ─────────────────────────    ---------    --------
                      ┌─────────────────────────┐
  Kalshi    ────────► │  Grok-4  (Forecaster 30%)│
  REST API            ├─────────────────────────┤
                      │  Claude  (News Analyst 20%)│
  WebSocket ────────► ├─────────────────────────┤
  Stream              │  GPT-4o  (Bull Case   20%)│  ──► Kalshi  ──► P&L
                      ├─────────────────────────┤      Order       Win Rate
  RSS / News ───────► │  Gemini  (Bear Case   15%)│      Router     Sharpe
  Feeds               ├─────────────────────────┤               Drawdown
                      │  DeepSeek(Risk Mgr    15%)│      Kelly    Cost
  Volume &  ────────► └─────────────────────────┘      Sizing   Budget
  Price Data             Debate → Consensus
                         Confidence Calibration
```

### Stage 1 — Ingest
Market data, order book snapshots, and news feeds are pulled via the Kalshi REST API and WebSocket stream. RSS feeds from financial news sources supplement the signal.

### Stage 2 — Decide (Multi-Model Ensemble)
Each of the five models analyzes the incoming data from its assigned perspective and returns a probability estimate + confidence score. The ensemble combines weighted votes:

| Model | Role | Weight |
|---|---|---|
| Grok-4 (xAI) | Lead Forecaster | 30% |
| Claude Sonnet 4 (OpenRouter) | News Analyst | 20% |
| GPT-4o (OpenRouter) | Bull Researcher | 20% |
| Gemini 2.5 Flash (OpenRouter) | Bear Researcher | 15% |
| DeepSeek R1 (OpenRouter) | Risk Manager | 15% |

If the weighted confidence falls below `min_confidence_to_trade` (default: 0.50), the opportunity is skipped. If models disagree significantly, position size is automatically reduced.

### Stage 3 — Execute
Qualifying trades are sized using the **Kelly Criterion** (fractional 0.75x) and routed through Kalshi's order API. Market-making orders are placed symmetrically around the mid-price.

### Stage 4 — Track
Every decision is written to a local SQLite database. The dashboard and `--stats` commands surface cumulative P&L, win rate, Sharpe ratio, and per-strategy breakdowns in real time.

---

## 📦 Installation

### Prerequisites

- Python 3.12 or later
- A [Kalshi](https://kalshi.com) account with API access ([API docs](https://trading-api.readme.io/reference/getting-started))
- An [xAI](https://console.x.ai/) API key (Grok-4)
- An [OpenRouter](https://openrouter.ai/) API key (Claude, GPT-4o, Gemini, DeepSeek)

### Automated Setup (Recommended)

```bash
git clone https://github.com/ryanfrigo/kalshi-ai-trading-bot.git
cd kalshi-ai-trading-bot
python setup.py
```

The setup script will:
- ✅ Check Python version compatibility
- ✅ Create virtual environment
- ✅ Install all dependencies (with Python 3.14 compatibility handling)
- ✅ Test that the dashboard can run
- ✅ Print troubleshooting guidance

### Manual Installation

```bash
git clone https://github.com/ryanfrigo/kalshi-ai-trading-bot.git
cd kalshi-ai-trading-bot

python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate          # Windows

# Python 3.14 users only:
export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1

pip install -r requirements.txt
```

### Configuration

```bash
cp env.template .env   # fill in your keys
```

| Variable | Description |
|---|---|
| `KALSHI_API_KEY` | Your Kalshi API key ID |
| `XAI_API_KEY` | xAI key for Grok-4 |
| `OPENROUTER_API_KEY` | OpenRouter key (Claude, GPT-4o, Gemini, DeepSeek) |
| `OPENAI_API_KEY` | Optional fallback |

Place your Kalshi private key as `kalshi_private_key` (no extension) in the project root. Download from [Kalshi Settings → API](https://kalshi.com/account/settings). This file is git-ignored.

### Initialize the Database

```bash
python -m src.utils.database
```

> ⚠️ Use `-m` flag — running `python src/utils/database.py` directly will fail with a module import error.

---

## 🖥️ Running

```bash
# Paper trading (no real orders — safe to test)
python cli.py run --paper

# Live trading (real money)
python cli.py run --live

# Launch monitoring dashboard
python cli.py dashboard

# Check portfolio balance and open positions
python cli.py status

# Verify all API connections
python cli.py health
```

Or invoke the bot script directly:

```bash
python beast_mode_bot.py              # Paper trading
python beast_mode_bot.py --live       # Live trading
python beast_mode_bot.py --dashboard  # Dashboard mode
```

---

## 📊 Paper Trading Dashboard

Simulate trades without risking real money. Every signal is logged to SQLite and a static HTML dashboard renders cumulative P&L, win rate, and per-signal details after markets settle.

```bash
# Scan markets and log signals
python paper_trader.py

# Continuous scanning every 15 minutes
python paper_trader.py --loop --interval 900

# Settle markets and update outcomes
python paper_trader.py --settle

# Regenerate HTML dashboard
python paper_trader.py --dashboard

# Print stats to terminal
python paper_trader.py --stats
```

The dashboard writes to `docs/paper_dashboard.html` — open locally or host via GitHub Pages.

---

## 🗂️ Project Structure

```
kalshi-ai-trading-bot/
├── beast_mode_bot.py          # Main bot entry point
├── cli.py                     # Unified CLI: run, dashboard, status, health, backtest
├── paper_trader.py            # Paper trading signal tracker
├── pyproject.toml             # PEP 621 project metadata
├── requirements.txt           # Pinned dependencies
├── env.template               # Environment variable template
│
├── src/
│   ├── agents/                # Multi-model ensemble (forecaster, bull/bear, risk, trader)
│   ├── clients/               # API clients (Kalshi, xAI, OpenRouter, WebSocket)
│   ├── config/                # Settings and trading parameters
│   ├── data/                  # News aggregation and sentiment analysis
│   ├── events/                # Async event bus for real-time streaming
│   ├── jobs/                  # Core pipeline: ingest, decide, execute, track, evaluate
│   ├── strategies/            # Market making, portfolio optimization, quick flip
│   └── utils/                 # Database, logging, prompts, risk helpers
│
├── scripts/                   # Utility and diagnostic scripts
├── docs/                      # Additional documentation + paper dashboard HTML
└── tests/                     # Pytest test suite
```

---

## ⚙️ Configuration Reference

All trading parameters live in `src/config/settings.py`:

```python
# Position sizing
max_position_size_pct  = 5.0     # Max 5% of balance per position
max_positions          = 15      # Up to 15 concurrent positions
kelly_fraction         = 0.75    # Fractional Kelly multiplier

# Market filtering
min_volume             = 200     # Minimum contract volume
max_time_to_expiry_days = 30     # Trade contracts up to 30 days out
min_confidence_to_trade = 0.50   # Minimum ensemble confidence to enter

# AI settings
primary_model          = "grok-4"
ai_temperature         = 0       # Deterministic outputs
ai_max_tokens          = 8000

# Risk management
max_daily_loss_pct     = 15.0    # Hard daily loss limit
daily_ai_cost_limit    = 50.0    # Max daily AI API spend (USD)
```

The ensemble configuration (model roster, weights, debate settings) lives in `EnsembleConfig` in the same file.

---

## 📈 Performance Tracking

Every trade, AI decision, and cost metric is recorded to `trading_system.db` (local SQLite). Use the dashboard or scripts in `scripts/` to review:

- Cumulative P&L and win rate
- Sharpe ratio and maximum drawdown
- AI confidence calibration
- Cost per trade and daily API budget utilization
- Per-strategy breakdowns (directional vs. market making)

---

## 🛠️ Development

### Running Tests

```bash
pytest tests/          # full suite
pytest tests/ -v       # verbose
pytest --cov=src       # with coverage
```

### Code Quality

```bash
black src/ tests/ cli.py beast_mode_bot.py
isort src/ tests/ cli.py beast_mode_bot.py
mypy src/
```

### Adding a New Strategy

1. Create a module in `src/strategies/`
2. Wire it into `src/strategies/unified_trading_system.py`
3. Set allocation percentage in `src/config/settings.py`
4. Add tests in `tests/`

---

## 🔧 Troubleshooting

<details>
<summary><strong>Bot not placing live trades despite --live flag</strong></summary>

Check logs for the mode confirmation string:

```bash
grep -i "live trading\|paper trading\|LIVE ORDER\|PAPER TRADE" logs/trading_system.log | tail -20
```

- `"LIVE TRADING MODE ENABLED"` → correct
- `"Paper trading mode"` → still in paper mode; verify API key has TRADING permissions in [Kalshi Settings](https://kalshi.com/account/settings)

</details>

<details>
<summary><strong>Dashboard won't launch / import errors</strong></summary>

Import errors in VS Code are IDE linter warnings, not runtime errors.

```bash
# Fix: activate venv, then run from project root
source .venv/bin/activate
python beast_mode_dashboard.py
```

Set VS Code Python interpreter to `.venv/bin/python` via `Cmd+Shift+P → Python: Select Interpreter`.

</details>

<details>
<summary><strong>Python 3.14 PyO3 compatibility error</strong></summary>

```bash
# Quick fix
export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1
pip install -r requirements.txt

# Recommended: use Python 3.13
pyenv install 3.13.1 && pyenv local 3.13.1
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

</details>

---

## 🤝 Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for full guidelines.

**Quick steps:**

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Make changes, add tests, run `pytest` and `black`
4. Commit with [conventional commit](https://www.conventionalcommits.org/) format: `feat: add new model weight config`
5. Open a Pull Request

**Good first issues:** look for the [`good first issue`](https://github.com/ryanfrigo/kalshi-ai-trading-bot/issues?q=label%3A%22good+first+issue%22) label.

---

## 📚 Resources

- [Kalshi Trading API Docs](https://trading-api.readme.io/reference/getting-started)
- [Kalshi API Authentication](https://trading-api.readme.io/reference/authentication)
- [Kalshi Markets Overview](https://kalshi.com/markets)
- [OpenRouter Model Catalog](https://openrouter.ai/models)
- [xAI API (Grok)](https://console.x.ai/)

---

## 📄 License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

---

<div align="center">

**If this project is useful to you, consider giving it a ⭐**

Made with ❤️ for the Kalshi trading community

</div>
