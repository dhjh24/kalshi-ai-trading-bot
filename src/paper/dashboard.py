"""
Generate a static HTML dashboard for paper trading activity.

The dashboard reads the unified paper-trading runtime stored in
`trading_system.db`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .tracker import get_dashboard_snapshot


def generate_html(output_path: Optional[str] = None, *, db_path: Optional[str] = None) -> str:
    """Generate the dashboard HTML and optionally write it to disk."""
    snapshot = get_dashboard_snapshot(db_path=db_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = _generate_runtime_html(snapshot, now)

    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(html)

    return html


def _generate_runtime_html(snapshot: Dict[str, Any], now: str) -> str:
    stats = snapshot["stats"]
    closed_trades = snapshot["closed_trades"]
    open_positions = snapshot["open_positions"]
    resting_orders = snapshot["resting_orders"]

    chronological_trades = list(reversed(closed_trades))
    running_pnl = 0.0
    pnl_series = []
    for trade in chronological_trades:
        running_pnl += float(trade.get("pnl") or 0.0)
        pnl_series.append(
            {
                "x": trade.get("exit_timestamp") or trade.get("entry_timestamp"),
                "y": round(running_pnl, 2),
            }
        )

    open_rows = "".join(_runtime_open_position_row(position) for position in open_positions)
    order_rows = "".join(_runtime_resting_order_row(order) for order in resting_orders)
    trade_rows = "".join(_runtime_closed_trade_row(trade) for trade in closed_trades)

    chart_json = json.dumps(pnl_series)
    db_path = _escape(snapshot.get("db_path", "trading_system.db"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi AI Bot - Paper Trading Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {{ --bg: #0d1117; --card: #161b22; --card-2: #10161d; --border: #30363d; --text: #c9d1d9;
           --muted: #8b949e; --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff; }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); padding: 24px; }}
  h1 {{ color: #fff; margin-bottom: 6px; }}
  h2 {{ color: #fff; margin-bottom: 10px; font-size: 1.05rem; }}
  .subtitle {{ color: var(--muted); margin-bottom: 18px; font-size: 0.92rem; }}
  .banner {{ background: linear-gradient(135deg, #13261b, #102435); border: 1px solid #355f7c; border-radius: 12px; padding: 14px 16px; margin-bottom: 16px; }}
  .banner strong {{ color: #fff; }}
  .banner .meta {{ color: #9fc3dd; margin-top: 6px; font-size: 0.84rem; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 18px; }}
  .stat {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px; }}
  .stat .value {{ font-size: 1.7rem; font-weight: 700; }}
  .stat .label {{ color: var(--muted); font-size: 0.76rem; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.04em; }}
  .section {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin-bottom: 16px; }}
  .chart-wrap {{ height: 280px; }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: var(--card-2); color: var(--muted); font-size: 0.76rem; text-transform: uppercase; letter-spacing: 0.04em; text-align: left; padding: 10px 12px; }}
  td {{ border-top: 1px solid var(--border); padding: 10px 12px; font-size: 0.85rem; vertical-align: top; }}
  tr:hover {{ background: #1b222b; }}
  .pill {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.74rem; font-weight: 600; }}
  .pill.yes {{ background: #173624; color: var(--green); }}
  .pill.no {{ background: #341a1b; color: #ffb3ac; }}
  .pill.buy {{ background: #132b1d; color: var(--green); }}
  .pill.sell {{ background: #341f13; color: #f0c28e; }}
  .pos {{ color: var(--green); }}
  .neg {{ color: var(--red); }}
  .empty {{ color: var(--muted); text-align: center; padding: 24px 12px; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
  footer {{ margin-top: 18px; color: #6e7681; font-size: 0.8rem; text-align: center; }}
</style>
</head>
<body>
<h1>Paper Trading Dashboard</h1>
<p class="subtitle">Unified paper runtime sourced from <code>trading_system.db</code> - updated {now}</p>

<div class="banner">
  <div><strong>Paper mode mirrors the live runtime.</strong> Entries use live quotes, exits can rest locally, and closed trades reflect fee-aware simulated execution.</div>
  <div class="meta">Source DB: <code>{db_path}</code></div>
</div>

<div class="stats">
  <div class="stat"><div class="value">{stats['closed_trades']}</div><div class="label">Closed Trades</div></div>
  <div class="stat"><div class="value">{stats['open_positions']}</div><div class="label">Open Positions</div></div>
  <div class="stat"><div class="value">{stats['resting_orders']}</div><div class="label">Resting Orders</div></div>
  <div class="stat"><div class="value">{stats['win_rate']}%</div><div class="label">Win Rate</div></div>
  <div class="stat"><div class="value {'pos' if stats['total_pnl'] >= 0 else 'neg'}">${stats['total_pnl']:.2f}</div><div class="label">Total P&amp;L</div></div>
  <div class="stat"><div class="value">${stats['avg_pnl']:.4f}</div><div class="label">Avg P&amp;L / Trade</div></div>
  <div class="stat"><div class="value pos">${stats['best_trade']:.2f}</div><div class="label">Best Trade</div></div>
  <div class="stat"><div class="value neg">${stats['worst_trade']:.2f}</div><div class="label">Worst Trade</div></div>
</div>

<div class="section">
  <h2>Cumulative P&amp;L</h2>
  <div class="chart-wrap">
    <canvas id="pnlChart"></canvas>
  </div>
</div>

<div class="section">
  <h2>Open Paper Positions</h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Opened</th><th>Market</th><th>Side</th><th>Entry</th><th>Qty</th><th>Strategy</th><th>Stop</th><th>Target</th>
      </tr></thead>
      <tbody>{open_rows if open_rows else '<tr><td colspan="8" class="empty">No open paper positions yet. Run <code>python cli.py run --paper</code>.</td></tr>'}</tbody>
    </table>
  </div>
</div>

<div class="section">
  <h2>Resting Paper Orders</h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Placed</th><th>Market</th><th>Action</th><th>Side</th><th>Price</th><th>Qty</th><th>Strategy</th><th>Target</th>
      </tr></thead>
      <tbody>{order_rows if order_rows else '<tr><td colspan="8" class="empty">No resting paper orders.</td></tr>'}</tbody>
    </table>
  </div>
</div>

<div class="section">
  <h2>Closed Paper Trades</h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Closed</th><th>Market</th><th>Side</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&amp;L</th><th>Strategy</th><th>Rationale</th>
      </tr></thead>
      <tbody>{trade_rows if trade_rows else '<tr><td colspan="9" class="empty">No closed paper trades yet.</td></tr>'}</tbody>
    </table>
  </div>
</div>

<footer>
  Generated by kalshi-ai-trading-bot. Paper-only metrics shown here exclude live trades.
</footer>

<script>
const data = {chart_json};
if (data.length > 0) {{
  new Chart(document.getElementById("pnlChart"), {{
    type: "line",
    data: {{
      labels: data.map(d => (d.x || "").slice(0, 19).replace("T", " ")),
      datasets: [{{
        label: "Cumulative P&L ($)",
        data: data.map(d => d.y),
        borderColor: data[data.length - 1].y >= 0 ? "#3fb950" : "#f85149",
        backgroundColor: "transparent",
        tension: 0.25,
        pointRadius: 2,
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ ticks: {{ color: "#8b949e", maxTicksLimit: 8 }}, grid: {{ color: "#21262d" }} }},
        y: {{ ticks: {{ color: "#8b949e", callback: v => "$" + v }}, grid: {{ color: "#21262d" }} }}
      }}
    }}
  }});
}}
</script>
</body>
</html>"""


def _runtime_open_position_row(position: Dict[str, Any]) -> str:
    return f"""
        <tr>
            <td>{_format_ts(position.get('timestamp'))}</td>
            <td title="{_escape(position.get('market_id', ''))}">{_trunc(position.get('market_title') or position.get('market_id', ''), 56)}</td>
            <td><span class="pill {'yes' if str(position.get('side', '')).upper() == 'YES' else 'no'}">{_escape(str(position.get('side', '')))}</span></td>
            <td>{_format_price(position.get('entry_price'))}</td>
            <td>{_format_quantity(position.get('quantity'))}</td>
            <td>{_escape(str(position.get('strategy') or '-'))}</td>
            <td>{_format_price(position.get('stop_loss_price'))}</td>
            <td>{_format_price(position.get('take_profit_price'))}</td>
        </tr>"""


def _runtime_resting_order_row(order: Dict[str, Any]) -> str:
    action = str(order.get("action", "")).lower()
    side = str(order.get("side", "")).upper()
    return f"""
        <tr>
            <td>{_format_ts(order.get('placed_at'))}</td>
            <td title="{_escape(order.get('market_id', ''))}">{_trunc(order.get('market_title') or order.get('market_id', ''), 56)}</td>
            <td><span class="pill {'buy' if action == 'buy' else 'sell'}">{_escape(action.upper())}</span></td>
            <td><span class="pill {'yes' if side == 'YES' else 'no'}">{_escape(side)}</span></td>
            <td>{_format_price(order.get('price'))}</td>
            <td>{_format_quantity(order.get('quantity'))}</td>
            <td>{_escape(str(order.get('strategy') or '-'))}</td>
            <td>{_format_price(order.get('target_price'))}</td>
        </tr>"""


def _runtime_closed_trade_row(trade: Dict[str, Any]) -> str:
    pnl = float(trade.get("pnl") or 0.0)
    pnl_class = "pos" if pnl > 0 else "neg" if pnl < 0 else ""
    side = str(trade.get("side", "")).upper()
    return f"""
        <tr>
            <td>{_format_ts(trade.get('exit_timestamp'))}</td>
            <td title="{_escape(trade.get('market_id', ''))}">{_trunc(trade.get('market_title') or trade.get('market_id', ''), 56)}</td>
            <td><span class="pill {'yes' if side == 'YES' else 'no'}">{_escape(side)}</span></td>
            <td>{_format_price(trade.get('entry_price'))}</td>
            <td>{_format_price(trade.get('exit_price'))}</td>
            <td>{_format_quantity(trade.get('quantity'))}</td>
            <td class="{pnl_class}">${pnl:.2f}</td>
            <td>{_escape(str(trade.get('strategy') or '-'))}</td>
            <td title="{_escape(str(trade.get('rationale') or ''))}">{_trunc(str(trade.get('rationale') or ''), 70)}</td>
        </tr>"""


def _format_ts(value: Any) -> str:
    if not value:
        return "-"
    try:
        return str(value).replace("T", " ")[:16]
    except Exception:
        return str(value)


def _format_price(value: Any) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def _format_quantity(value: Any) -> str:
    if value is None or value == "":
        return "-"
    try:
        quantity = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(quantity - round(quantity)) < 1e-9:
        return str(int(round(quantity)))
    return f"{quantity:.2f}"


def _trunc(value: Any, length: int) -> str:
    text = str(value or "")
    return text[:length] + "..." if len(text) > length else text


def _escape(value: Any) -> str:
    text = str(value or "")
    return (
        text.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "paper_dashboard.html")
    generate_html(out)
    print(f"Dashboard generated: {out}")
