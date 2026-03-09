# Kalshi AI Trading Bot

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

**Multi-model AI trading bot for Kalshi prediction markets.**

An autonomous trading system that combines a five-model AI ensemble, portfolio
optimization, market making, and dynamic exit strategies to find and exploit
edges on [Kalshi](https://kalshi.com) event contracts.

> **Disclaimer -- This is experimental software for educational and research
> purposes only.** Trading involves substantial risk of loss. Only trade with
> capital you can afford to lose. Past performance does not guarantee future
> results. This software is not financial advice. Use at your own risk. The
> authors are not responsible for any financial losses incurred through the use
> of this software.

---

## Architecture

```
                           Kalshi AI Trading Bot

  INGEST               DECIDE (Multi-Agent)         EXECUTE          TRACK
 --------             ----------------------        ---------       --------
                      +--------------------+
  Kalshi    --------> |  Grok-4            |
  REST API            |  (Forecaster, 30%) |
                      +--------------------+
  WebSocket --------> +--------------------+
  Stream              |  Claude Sonnet 4   |
                      |  (News Analyst,20%)|        Kalshi        Portfolio
  RSS/News  --------> +--------------------+ -----> Order  -----> P&L, Win
  Feeds               |  GPT-4o            |        Router        Rate,
                      |  (Bull Case, 20%)  |                      Sharpe,
  Volume &  --------> +--------------------+        Kelly         Drawdown
  Price Data          |  Gemini 2.5 Flash  |        Criterion     Tracking
                      |  (Bear Case, 15%)  |        Sizing
                      +--------------------+                      Cost &
                      |  DeepSeek R1       |        Risk          Budget
                      |  (Risk Mgr, 15%)   |        Parity        Monitor
                      +--------------------+        Alloc

                        Debate & Consensus
                        Confidence Calibration
```

---

## Features

### Multi-Model Ensemble

Five frontier LLMs collaborate on every trading decision. Each model is assigned
a distinct analytical role and weighted vote. When model opinions diverge beyond
a configurable threshold the system reduces position size or skips the trade.

| Model | Provider | Role | Weight |
|---|---|---|---|
| Grok-4 | xAI | Lead Forecaster | 30% |
| Claude Sonnet 4 | OpenRouter | News Analyst | 20% |
| GPT-4o | OpenRouter | Bull Researcher | 20% |
| Gemini 2.5 Flash | OpenRouter | Bear Researcher | 15% |
| DeepSeek R1 | OpenRouter | Risk Manager | 15% |

### Trading Strategies

| Strategy | Allocation | Description |
|---|---|---|
| Directional Trading | 50% | AI-predicted probability edge with Kelly sizing |
| Market Making | 40% | Automated limit orders capturing bid-ask spreads |
| Arbitrage Detection | 10% | Cross-market opportunity scanning |

### Portfolio Optimization

- **Kelly Criterion** position sizing with fractional Kelly (0.75x) for volatility control
- **Risk parity** allocation across concurrent positions
- **Dynamic rebalancing** every six hours
- Hard limits on daily loss (15%), max drawdown (50%), and sector concentration (90%)

### Dynamic Exit Strategies

- Trailing take-profit at 20% gain
- Stop-loss at 15% drawdown per position
- Confidence-decay exits when AI conviction drops
- Time-based exits with a 10-day maximum hold
- Volatility-adjusted thresholds

### Real-Time Dashboard

A Streamlit web dashboard provides live views of:

- Portfolio value and balance
- Open positions with entry prices and P&L
- AI decision logs and confidence scores
- Cost monitoring and daily budget utilization
- Strategy-level performance breakdown

---

## Quick Start

### Prerequisites

- Python 3.12 or later
- A [Kalshi](https://kalshi.com) account with API access
- An [xAI](https://console.x.ai/) API key (Grok-4)
- An [OpenRouter](https://openrouter.ai/) API key (for the remaining four models)

### Installation

#### Quick Setup (Recommended)

```bash
# Clone the repository
git clone https://github.com/yourusername/kalshi-ai-trading-bot.git
cd kalshi-ai-trading-bot

# Run the automated setup script
python setup.py
```

The setup script will:
- ✅ Check Python version compatibility
- ✅ Create virtual environment if needed  
- ✅ Install dependencies with Python 3.14 compatibility
- ✅ Test that the dashboard can run
- ✅ Provide troubleshooting guidance

#### Manual Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/kalshi-ai-trading-bot.git
cd kalshi-ai-trading-bot

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate          # Windows

# For Python 3.14 users: Set compatibility flag
export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1  # macOS/Linux
# set PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1   # Windows

# Install dependencies
pip install -r requirements.txt

# Test the installation
python beast_mode_dashboard.py --summary
```

### Configuration

```bash
# Copy the template and fill in your keys
cp env.template .env
```

Required variables in `.env`:

| Variable | Description |
|---|---|
| `KALSHI_API_KEY` | Your Kalshi API key ID |
| `XAI_API_KEY` | xAI API key for Grok-4 |
| `OPENROUTER_API_KEY` | OpenRouter key for ensemble models |
| `OPENAI_API_KEY` | OpenAI key (optional fallback) |

You must also place your Kalshi private key file as `kalshi_private_key` (no file
extension) in the project root. Download it from
[Kalshi Settings → API](https://kalshi.com/account/settings). This file is
git-ignored.

### Initialize the Database

```bash
python -m src.utils.database
```

> **Note:** Running `python src/utils/database.py` directly may fail with
> `ModuleNotFoundError: No module named 'src'`. Always use the `-m` flag.

## Troubleshooting

### Dashboard Won't Launch

**Problem**: VS Code/IDE shows import errors or dashboard fails to start.

**Solutions**:

1. **Run the setup script first**:
   ```bash
   cd kalshi-ai-trading-bot
   python setup.py
   ```

2. **Check you're in the right directory**:
   ```bash
   # Make sure you see beast_mode_dashboard.py
   ls beast_mode_dashboard.py
   ```

3. **Verify virtual environment is activated**:
   ```bash
   # Should show your project venv path
   which python
   
   # Activate if needed
   source .venv/bin/activate  # macOS/Linux
   # .venv\Scripts\activate   # Windows
   ```

4. **Install/reinstall dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

5. **Test the dashboard**:
   ```bash
   # Quick test
   python beast_mode_dashboard.py --summary
   
   # If that works, try the full dashboard
   python beast_mode_dashboard.py
   ```

6. **Fix VS Code Python interpreter**:
   - Press `Cmd+Shift+P` (Mac) or `Ctrl+Shift+P` (Windows/Linux)
   - Type "Python: Select Interpreter"
   - Choose the interpreter from your `.venv` folder

**Still having issues?** Run `python setup.py` for automated diagnosis.

### Python 3.14 Compatibility Issues

**Problem**: `error: the configured Python interpreter version (3.14) is newer than PyO3's maximum supported version (3.13)`

**Solutions**:

1. **Automatic fix** (use the setup script):
   ```bash
   python setup.py
   ```
   The setup script automatically handles Python 3.14 compatibility.

2. **Manual fix**:
   ```bash
   # Set compatibility flag before installing
   export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1  # macOS/Linux
   set PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1     # Windows
   
   pip install -r requirements.txt
   ```

3. **Recommended** (use supported Python version):
   ```bash
   # Install Python 3.13 with pyenv
   pyenv install 3.13.1
   pyenv local 3.13.1
   
   # Create new virtual environment  
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

**Note**: Python 3.14 support is experimental. For production use, Python 3.12-3.13 is recommended.

---

### Running

```bash
# Paper trading (default -- no real orders)
python cli.py run --paper

# Live trading (real money)
python cli.py run --live

# Launch the monitoring dashboard
python cli.py dashboard

# Check portfolio balance and positions
python cli.py status

# Verify all connections and configuration
python cli.py health
```

Or run the bot directly:

```bash
python beast_mode_bot.py              # Paper trading
python beast_mode_bot.py --live       # Live trading
python beast_mode_bot.py --dashboard  # Dashboard mode
```

---

## Project Structure

```
kalshi-ai-trading-bot/
|-- beast_mode_bot.py          # Main bot entry point (BeastModeBot class)
|-- cli.py                     # Unified CLI: run, dashboard, status, health, backtest
|-- pyproject.toml             # PEP 621 project metadata and build config
|-- requirements.txt           # Pinned dependencies
|-- env.template               # Environment variable template
|
|-- src/
|   |-- agents/                # Multi-agent ensemble (forecaster, bull/bear, risk, trader)
|   |-- clients/               # API clients (Kalshi, xAI, OpenRouter, WebSocket)
|   |-- config/                # Settings and trading parameters
|   |-- data/                  # News aggregation and sentiment analysis
|   |-- events/                # Async event bus for real-time streaming
|   |-- jobs/                  # Core pipeline: ingest, decide, execute, track, evaluate
|   |-- strategies/            # Market making, portfolio optimization, quick flip
|   +-- utils/                 # Database, logging, prompts, risk helpers
|
|-- scripts/                   # Utility and diagnostic scripts (20 scripts)
|-- docs/                      # Additional documentation
|-- src/paper/                 # Paper trading signal tracker and dashboard generator
|
+-- tests/                     # Pytest test suite
```

---

## Paper Trading Dashboard

Track what the bot *would* trade without risking real money. Every signal is
logged to SQLite and a static HTML dashboard shows cumulative P&L, win rate, and
per-signal details.

```bash
# Scan markets and log signals (no real orders)
python paper_trader.py

# Continuous scanning every 15 minutes
python paper_trader.py --loop --interval 900

# Check settled markets and update outcomes
python paper_trader.py --settle

# Regenerate the HTML dashboard
python paper_trader.py --dashboard

# Print stats to terminal
python paper_trader.py --stats
```

The dashboard is written to `docs/paper_dashboard.html` — open it locally or
serve via GitHub Pages.

---

## Configuration Reference

All trading parameters live in `src/config/settings.py`. Key defaults:

```python
# Position sizing
max_position_size_pct  = 5.0     # Max 5% of balance per position
max_positions          = 15      # Up to 15 concurrent positions
kelly_fraction         = 0.75    # Fractional Kelly multiplier

# Market filtering
min_volume             = 200     # Minimum contract volume
max_time_to_expiry_days = 30     # Trade contracts up to 30 days out
min_confidence_to_trade = 0.50   # Minimum ensemble confidence

# AI settings
primary_model          = "grok-4"
ai_temperature         = 0       # Deterministic outputs
ai_max_tokens          = 8000

# Risk management
max_daily_loss_pct     = 15.0    # Hard daily loss limit
daily_ai_cost_limit    = 50.0    # Max daily AI API spend (USD)
```

The ensemble configuration (model roster, weights, debate settings) is in
`EnsembleConfig` within the same file.

---

## Performance

Performance tracking is built in. The bot records every trade, AI decision, and
cost metric to a local SQLite database (`trading_system.db`). Use the dashboard
or the scripts in `scripts/` to review:

- Cumulative P&L and win rate
- Sharpe ratio and maximum drawdown
- AI confidence calibration curves
- Cost per trade and daily API budget utilization
- Per-strategy breakdowns (directional vs. market making)

---

## Development

### Running Tests

```bash
# Run the full test suite
python run_tests.py

# Or use pytest directly
pytest tests/
```

### Code Quality

```bash
# Format
black src/ tests/ cli.py beast_mode_bot.py
isort src/ tests/ cli.py beast_mode_bot.py

# Type check
mypy src/
```

### Adding a New Strategy

1. Create a module in `src/strategies/`.
2. Implement the strategy logic and wire it into `src/strategies/unified_trading_system.py`.
3. Add allocation percentage in `src/config/settings.py`.
4. Write tests in `tests/`.

---

## Troubleshooting

### Bot Stuck in "Ingestion Mode" or Not Placing Live Trades

**Problem**: Bot shows open positions incrementing in dashboard but no actual trades execute, even with `--live` flag.

**Solution**: This typically indicates the bot is running in paper mode despite the `--live` flag. Check:

1. **Verify command usage**:
   ```bash
   python cli.py run --live  # Correct
   python beast_mode_bot.py --live  # Also correct (direct script)
   ```

2. **Check logs for mode confirmation**:
   ```bash
   # Look for these log messages:
   # "LIVE TRADING MODE ENABLED - REAL MONEY WILL BE USED"  ← Good (live mode)
   # "Paper trading mode - orders will be simulated"       ← Problem (paper mode)
   
   # Also look for order execution logs:
   # "LIVE ORDER PLACED for [market]"     ← Good (live mode)  
   # "PAPER TRADE SIMULATED for [market]" ← Problem (paper mode)
   ```

3. **Verify API permissions**:
   - Log into your Kalshi dashboard
   - Check that your API key has **TRADING** permissions (not just read-only)
   - Verify your API key hasn't expired

4. **Check rate limiting**:
   ```bash
   grep "rate limit\|429" logs/trading_system.log
   ```

5. **Verify configuration**:
   - Check `src/config/settings.py` has correct settings
   - Ensure your `.env` file has valid `KALSHI_API_KEY`

### Dashboard Won't Launch

**Problem**: VS Code/IDE shows import errors like "Import aiosqlite could not be resolved".

**Solution**: These are IDE linter warnings, not runtime errors. To fix:

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Set correct Python interpreter** in VS Code:
   - Press `Cmd+Shift+P` (Mac) or `Ctrl+Shift+P` (Windows/Linux)
   - Type "Python: Select Interpreter"
   - Choose the interpreter from your virtual environment

3. **Run from project root**:
   ```bash
   cd kalshi-ai-trading-bot
   python beast_mode_dashboard.py
   ```

### Python 3.14 Compatibility Issues

**Problem**: PyO3 error during `pip install`.

**Solutions**:
1. **Quick fix** (may be unstable):
   ```bash
   export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1
   pip install -r requirements.txt
   ```

2. **Recommended** (use supported Python version):
   ```bash
   pyenv install 3.13.1
   pyenv local 3.13.1
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

---

## Contributing

1. Fork the repository.
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Commit your changes with a descriptive message.
4. Push and open a Pull Request.

Please follow the existing code style (Black, isort) and add tests for new
functionality.

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for
details.

---

**Disclaimer**: This software is for educational and research purposes. Trading
involves risk, and you should only trade with capital you can afford to lose.
The authors are not responsible for any financial losses incurred through the use
of this software.
