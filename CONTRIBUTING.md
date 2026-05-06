# Contributing to Kalshi AI Trading Bot

Thanks for helping improve the project.

## How to contribute

We welcome:

- Bug reports
- Feature requests
- Code contributions
- Documentation updates
- Tests and reliability improvements
- Performance work

## Development setup

### Prerequisites

- Python 3.12+
- Node.js 24+
- Git
- A Kalshi API account for integration testing
- At least one model route: a signed-in `codex` CLI, `OPENAI_API_KEY`, or `OPENROUTER_API_KEY`

### Local setup

```bash
git clone https://github.com/cdavisv/kalshi-ai-trading-bot.git
cd kalshi-ai-trading-bot

python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

pip install -r requirements.txt
npm install

cp env.template .env
python -m src.utils.database
```

Optional: launch the dashboard locally with `python cli.py dashboard`.
For model routing, `LLM_PROVIDER=auto` prefers the signed-in Codex CLI, then direct OpenAI,
then OpenRouter.

## Code standards

### Python

- Follow PEP 8
- Use type hints on new code
- Keep functions focused and testable
- Prefer clear naming over cleverness

### Formatting and checks

```bash
black src/ tests/ cli.py
isort src/ tests/ cli.py
mypy src/
```

### Dashboard workspace

```bash
npm run lint --workspace server
npm run lint --workspace web
```

## Testing

Run the relevant suites for the code you changed.

```bash
pytest tests/
pytest tests/test_live_trade_job.py tests/test_quick_flip_scalping.py
pytest tests/test_decide.py
pytest --cov=src --cov-report=html

npm run test --workspace server
npm run test --workspace web
```

Please avoid real external API calls in automated tests when mocks or fixtures are practical.

## Workflow

1. Create a branch from `cdavisv/kalshi-ai-trading-bot`, or fork that repository first if you do not have write access.
2. Make your changes.
3. Add or update tests.
4. Update docs when behavior changes.
5. Run the relevant checks locally.
6. Open a pull request with a clear summary.

## Commit messages

Prefer conventional commits:

- `feat:` new behavior
- `fix:` bug fix
- `docs:` documentation only
- `refactor:` internal restructuring
- `test:` test changes
- `chore:` tooling or maintenance

## Pull request checklist

- Tests pass locally for the changed area
- Formatting and lint checks pass
- No secrets or private keys are included
- Docs are updated if commands, env vars, routes, or user-visible behavior changed
- The PR description explains the motivation and impact
