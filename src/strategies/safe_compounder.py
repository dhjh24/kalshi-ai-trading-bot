"""
Safe Compounder Strategy — Ported from ~/dev/apex/safe_compounder.py

NO-side only, edge-based, capital-efficient.

STRATEGY:
- NO side ONLY
- Find near-certain outcomes (EV ~95-99¢)
- Edge = estimated_true_prob - lowest_no_ask > 5¢
- Lowest NO ask must be > 80¢
- Place resting order at lowest_no_ask - 1¢ (maker trade, near-zero fees)
- Position size: max 10% of portfolio value per position (Kelly optional)

KEY INSIGHT: We estimate true probability dynamically:
- YES last price is our primary signal (lower = more certain NO wins)
- Time to expiry amplifies certainty (if YES is at 3¢ with 2 days left, it's ~99%)
- We compare our EV estimate to the actual NO ask price
- Edge = EV - NO ask. Only trade when edge > 5¢.

Integrated with the repo's KalshiClient and DatabaseManager.
Available via: python cli.py run --safe-compounder
"""

import asyncio
import logging
import math
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import aiosqlite

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------

# Skip sports/entertainment — too unpredictable for "near-certain" plays
SKIP_PREFIXES = [
    "KXNBA", "KXNFL", "KXNHL", "KXMLB", "KXUFC", "KXPGA", "KXATP",
    "KXEPL", "KXUCL", "KXLIGA", "KXSERIE", "KXBUNDES", "KXLIGUE",
    "KXWC", "KXMARMAD", "KXMAKEMARMAD", "KXWMARMAD", "KXRT-",
    "KXPERFORM", "KXACTOR", "KXBOND-", "KXOSCAR", "KXBAFTA", "KXSAG",
    "KXSNL", "KXSURVIVOR", "KXTRAITORS", "KXDAILY",
    "KXALBUM", "KXSONG", "KX1SONG", "KX20SONG", "KXTOUR-",
    "KXFEATURE", "KXGTA", "KXBIG10", "KXBIG12", "KXACC", "KXSEC",
    "KXAAC", "KXBIGEAST", "KXNCAAM", "KXCOACH", "KXMV",
    "KXCHESS", "KXBELGIAN", "KXEFL", "KXSUPER", "KXLAMIN",
    "KXWHATSON", "KXWOWHOCKEY",
    "KXMENTION", "KXTMENTION", "KXTRUMPMENTION", "KXTRUMPSAY",
    "KXSPEECH", "KXTSPEECH", "KXADDRESS",
]

SKIP_TITLE_PHRASES = [
    "mention", "say in", "speech mention", "address mention",
]

# Thresholds
MIN_VOLUME = 10
MIN_NO_ASK = 80      # Lowest NO ask must be > 80¢
MIN_EDGE = 5         # Edge (EV - price) must be > 5¢
MAX_POSITION_PCT = 0.10    # Max 10% of portfolio per position
USE_KELLY = True
MIN_CONFIDENCE = 0.4


# -----------------------------------------------------------------------
# Core math
# -----------------------------------------------------------------------

def should_skip(ticker: str) -> bool:
    upper = ticker.upper()
    return any(upper.startswith(p.upper()) for p in SKIP_PREFIXES)


def estimate_true_no_prob(yes_last: int, hours_to_expiry: float) -> int:
    """
    Estimate the true probability that NO wins.
    Returns estimated true NO probability in cents (0-100).
    """
    base_prob = 100 - yes_last

    if hours_to_expiry <= 0:
        return base_prob

    if hours_to_expiry <= 24:
        if yes_last <= 5:
            return min(99, base_prob + 4)
        elif yes_last <= 10:
            return min(98, base_prob + 3)
        elif yes_last <= 15:
            return min(97, base_prob + 2)
        else:
            return min(96, base_prob + 1)
    elif hours_to_expiry <= 72:
        if yes_last <= 5:
            return min(99, base_prob + 3)
        elif yes_last <= 10:
            return min(97, base_prob + 2)
        else:
            return base_prob + 1
    elif hours_to_expiry <= 168:
        if yes_last <= 5:
            return min(98, base_prob + 2)
        elif yes_last <= 10:
            return min(96, base_prob + 1)
        else:
            return base_prob
    else:
        if yes_last <= 3:
            return min(97, base_prob + 1)
        return base_prob


def kelly_fraction(prob_win: float, payout_ratio: float) -> float:
    """Kelly fraction for a binary bet."""
    if payout_ratio <= 0 or prob_win <= 0:
        return 0.0
    prob_lose = 1.0 - prob_win
    f = (prob_win * payout_ratio - prob_lose) / payout_ratio
    return max(0.0, f)


def market_confidence_score(ticker: str, orderbook: dict, market: dict) -> Tuple[float, str]:
    """Return (confidence_score 0-1, reason_str) for a market."""
    reasons = []

    no_side = orderbook.get("no", [])
    yes_side = orderbook.get("yes", [])

    all_levels = []
    for price, qty in yes_side:
        all_levels.append((100 - price, qty))
    for price, qty in no_side:
        all_levels.append((price, qty))

    if all_levels:
        best_ask = min(p for p, q in all_levels)
        total_vol = sum(q for _, q in all_levels)
        vol_within_3c = sum(q for p, q in all_levels if p <= best_ask + 3)
        depth_ratio = vol_within_3c / max(total_vol, 1)
    else:
        depth_ratio = 0.0
        reasons.append("no book")

    best_no_ask = None
    if yes_side:
        best_no_ask = 100 - max(p for p, q in yes_side)
    best_no_bid = max((p for p, q in no_side), default=0) if no_side else 0

    if best_no_ask and best_no_bid > 0:
        spread = best_no_ask - best_no_bid
        spread_pct = spread / max(best_no_ask, 1)
        spread_score = max(0, 1.0 - (spread_pct / 0.10))
        if spread_pct > 0.05:
            reasons.append("wide spread")
    else:
        spread_score = 0.3
        if not reasons:
            reasons.append("unclear spread")

    volume = market.get("volume", 0)
    days_to_expiry = market.get("_days_to_expiry", 30)
    vol_per_day = volume / max(days_to_expiry, 1)
    volume_score = min(1.0, vol_per_day / 50.0)
    if vol_per_day < 10:
        reasons.append("thin volume")

    yes_last = market.get("last_price", 50)
    if best_no_ask:
        price_gap = abs(best_no_ask - (100 - yes_last))
        stability_score = max(0, 1.0 - (price_gap / 15.0))
        if price_gap > 8:
            reasons.append("price gap")
    else:
        stability_score = 0.3

    score = (
        depth_ratio * 0.30
        + spread_score * 0.30
        + volume_score * 0.25
        + stability_score * 0.15
    )

    reason_str = ", ".join(reasons) if reasons else "ok"
    return round(score, 3), reason_str


# -----------------------------------------------------------------------
# SafeCompounder class
# -----------------------------------------------------------------------

class SafeCompounder:
    """
    Edge-based NO-side strategy integrated with repo's KalshiClient.

    Usage:
        compounder = SafeCompounder(client=kalshi_client, db_path="trading_system.db")
        await compounder.run(dry_run=False)
    """

    def __init__(
        self,
        client,  # KalshiClient instance
        db_path: str = "trading_system.db",
        dry_run: bool = True,
        min_no_ask: int = MIN_NO_ASK,
        min_edge: int = MIN_EDGE,
        max_position_pct: float = MAX_POSITION_PCT,
        use_kelly: bool = USE_KELLY,
        min_confidence: float = MIN_CONFIDENCE,
    ):
        self.client = client
        self.db_path = db_path
        self.dry_run = dry_run
        self.min_no_ask = min_no_ask
        self.min_edge = min_edge
        self.max_position_pct = max_position_pct
        self.use_kelly = use_kelly
        self.min_confidence = min_confidence

    async def run(self, dry_run: Optional[bool] = None) -> Dict:
        """
        Full scan: fetch → filter → orderbook check → place maker orders.
        Returns stats dict.
        """
        if dry_run is not None:
            self.dry_run = dry_run

        start = time.time()

        logger.info("=" * 70)
        logger.info("SAFE COMPOUNDER v5 — EDGE-BASED NO-SIDE")
        logger.info(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        logger.info(
            "Rules: NO only | ask > %d¢ | edge > %d¢ | max %.0f%%/position | maker orders",
            self.min_no_ask, self.min_edge, self.max_position_pct * 100,
        )
        logger.info("=" * 70)

        # Get portfolio state
        bal = await self.client.get_balance()
        portfolio = bal.get("portfolio_value", 0)
        cash = bal.get("balance", 0)

        print(f"\n💰 Cash: ${cash/100:.2f} | Portfolio: ${portfolio/100:.2f} | "
              f"Total: ${(cash+portfolio)/100:.2f}\n", flush=True)

        # Step 0: Cancel legacy YES orders
        print("🧹 Step 0: Cancel legacy YES orders...", flush=True)
        cancelled = await self._cancel_yes_orders()

        # Step 1: Fetch all markets
        print("\n📡 Step 1: Fetching all active markets...", flush=True)
        markets = await self._fetch_all_markets()
        print(f"  Fetched {len(markets)} markets", flush=True)

        # Step 2: Filter NO candidates
        print("\n🔍 Step 2: Finding NO-side candidates (YES ≤ 20¢)...", flush=True)
        candidates = self._find_no_candidates(markets)

        # Step 3: Orderbook + edge check
        print(f"\n📊 Step 3: Checking orderbooks for edge ≥ {self.min_edge}¢...", flush=True)
        opportunities = await self._check_orderbook_and_price(candidates)

        # Display top opportunities
        sorted_opps = sorted(
            opportunities, key=lambda x: (-x["edge"], -x["annualized_roi"])
        )
        print(f"\n📋 Top Opportunities:", flush=True)
        for opp in sorted_opps[:20]:
            print(
                f"  NO ask:{opp['lowest_no_ask']}¢ → our:{opp['our_price']}¢ | "
                f"EV:{opp['true_no_prob']}¢ edge:{opp['edge']}¢ | "
                f"YES@{opp['yes_last']}¢ | {opp['roi_pct']:.1f}% "
                f"({opp['annualized_roi']:.0f}%ann) | "
                f"{opp['days_to_expiry']}d | vol:{opp['volume']} | {opp['ticker']}",
                flush=True,
            )
            print(f"    {opp['title']}", flush=True)

        # Step 4: Place orders
        print(f"\n🚀 Step 4: Placing maker orders (ask - 1¢)...", flush=True)
        stats = await self._place_resting_orders(sorted_opps, portfolio, cash)

        elapsed = time.time() - start
        bal = await self.client.get_balance()

        print(f"\n{'='*70}", flush=True)
        print(f"📊 SAFE COMPOUNDER REPORT", flush=True)
        print(f"{'='*70}", flush=True)
        print(f"  Markets scanned:      {len(markets)}", flush=True)
        print(f"  NO candidates:        {len(candidates)}", flush=True)
        print(f"  With edge > {self.min_edge}¢:      {len(opportunities)}", flush=True)
        print(f"  Orders placed:        {stats['placed']}", flush=True)
        print(f"  Instantly filled:     {stats['filled']}", flush=True)
        print(f"  Skipped (existing):   {stats['skipped_existing']}", flush=True)
        print(f"  Errors:               {stats['errors']}", flush=True)
        print(f"  Capital deployed:     ${stats['total_deployed']/100:.2f}", flush=True)
        print(f"  Potential profit:     ${stats['total_potential_profit']/100:.2f}", flush=True)
        print(f"  YES orders cancelled: {cancelled}", flush=True)
        print(f"  Cash:                 ${bal.get('balance', 0)/100:.2f}", flush=True)
        print(f"  Portfolio:            ${bal.get('portfolio_value', 0)/100:.2f}", flush=True)
        print(f"  Elapsed:              {elapsed:.0f}s", flush=True)
        print(f"{'='*70}\n", flush=True)

        return stats

    async def _fetch_all_markets(self) -> List[Dict]:
        """Fetch all active markets from Kalshi."""
        all_markets = []
        cursor = None
        page = 0
        while True:
            try:
                params = {"status": "open", "limit": 200}
                if cursor:
                    params["cursor"] = cursor

                resp = await self.client.get_markets(**params)
                markets = resp.get("markets", [])
                all_markets.extend(markets)

                cursor = resp.get("cursor")
                if not cursor or not markets:
                    break

                page += 1
                if page > 50:  # Safety cap
                    break

                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error("Error fetching markets page %d: %s", page, e)
                break

        return all_markets

    def _find_no_candidates(self, markets: List[Dict]) -> List[Dict]:
        """Filter markets to NO-side candidates."""
        candidates = []
        now = datetime.now(timezone.utc)

        for m in markets:
            ticker = m.get("ticker", "")
            if should_skip(ticker):
                continue

            title_lower = m.get("title", "").lower()
            if any(phrase in title_lower for phrase in SKIP_TITLE_PHRASES):
                continue

            if m.get("volume", 0) < MIN_VOLUME:
                continue

            yes_last = m.get("last_price", 50)
            if yes_last > 20:
                continue

            close_time = m.get("close_time", "")
            hours_to_expiry = 720
            if close_time:
                try:
                    expiry = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                    hours_to_expiry = max(0, (expiry - now).total_seconds() / 3600)
                except Exception:
                    pass

            if hours_to_expiry <= 0:
                continue

            true_no_prob = estimate_true_no_prob(yes_last, hours_to_expiry)

            candidates.append({
                **m,
                "_true_no_prob": true_no_prob,
                "_hours_to_expiry": round(hours_to_expiry, 1),
                "_days_to_expiry": round(hours_to_expiry / 24, 1),
            })

        logger.info("Found %d NO-side candidates (YES last <= 20¢)", len(candidates))
        return candidates

    async def _check_orderbook_and_price(self, candidates: List[Dict]) -> List[Dict]:
        """Check orderbooks and find trades with sufficient edge."""
        opportunities = []

        for i, m in enumerate(candidates):
            ticker = m["ticker"]
            true_no_prob = m["_true_no_prob"]

            try:
                ob_resp = await self.client.get_orderbook(ticker, depth=10)
                ob = ob_resp.get("orderbook", {})
                await asyncio.sleep(0.12)
            except Exception as e:
                logger.debug("Orderbook fetch failed for %s: %s", ticker, e)
                continue

            conf_score, conf_reason = market_confidence_score(ticker, ob, m)
            if conf_score < self.min_confidence:
                logger.debug(
                    "Low confidence (%.2f) %s — %s", conf_score, ticker, conf_reason
                )
                continue

            yes_bids = ob.get("yes", [])
            no_bids = ob.get("no", [])

            lowest_no_ask = None
            if yes_bids:
                highest_yes_bid = max(b[0] for b in yes_bids)
                lowest_no_ask = 100 - highest_yes_bid

            best_no_bid = max((b[0] for b in no_bids), default=0) if no_bids else 0

            if lowest_no_ask is None and best_no_bid > 0:
                lowest_no_ask = best_no_bid + 2

            if lowest_no_ask is None:
                continue

            if lowest_no_ask < self.min_no_ask:
                continue

            edge = true_no_prob - lowest_no_ask
            if edge < self.min_edge:
                continue

            our_price = lowest_no_ask - 1
            if our_price < self.min_no_ask:
                continue

            profit_per_contract = 100 - our_price
            roi_pct = profit_per_contract / our_price * 100
            days = m["_days_to_expiry"] if m["_days_to_expiry"] > 0 else 1
            annualized_roi = (profit_per_contract / our_price) * (365 / days) * 100

            opportunities.append({
                "ticker": ticker,
                "title": m.get("title", "")[:70],
                "side": "no",
                "yes_last": m.get("last_price", 0),
                "true_no_prob": true_no_prob,
                "lowest_no_ask": lowest_no_ask,
                "our_price": our_price,
                "edge": edge,
                "profit": profit_per_contract,
                "roi_pct": roi_pct,
                "annualized_roi": annualized_roi,
                "volume": m.get("volume", 0),
                "days_to_expiry": m["_days_to_expiry"],
                "close_time": m.get("close_time", "")[:10],
                "best_no_bid": best_no_bid,
            })

            if (i + 1) % 25 == 0:
                logger.info(
                    "Checked %d/%d orderbooks, %d viable",
                    i + 1, len(candidates), len(opportunities),
                )

        logger.info(
            "%d opportunities with edge > %d¢", len(opportunities), self.min_edge
        )
        return opportunities

    async def _place_resting_orders(
        self, opportunities: List[Dict], portfolio: int, cash: int
    ) -> Dict:
        """Place NO-side resting orders at lowest_ask - 1¢."""
        # Get existing positions and orders
        try:
            positions_resp = await self.client.get_positions()
            positions = positions_resp.get("market_positions", [])
            pos_tickers = {
                p["ticker"] for p in positions if abs(p.get("position", 0)) > 0
            }
        except Exception:
            pos_tickers = set()

        try:
            orders_resp = await self.client.get_orders(status="resting")
            existing_orders = orders_resp.get("orders", [])
            ord_tickers = {o["ticker"] for o in existing_orders}
        except Exception:
            ord_tickers = set()

        stats = {
            "placed": 0,
            "skipped_existing": 0,
            "skipped_size": 0,
            "filled": 0,
            "errors": 0,
            "total_potential_profit": 0,
            "total_deployed": 0,
        }

        print(
            f"\n{'='*70}\nPLACING MAKER ORDERS — Portfolio: ${portfolio/100:.2f} | "
            f"Cash: ${cash/100:.2f} | {'DRY RUN' if self.dry_run else 'LIVE'}\n"
            f"Max per position: ${portfolio * self.max_position_pct / 100:.2f} ({self.max_position_pct*100:.0f}%)\n"
            f"{'='*70}\n",
            flush=True,
        )

        for opp in opportunities:
            ticker = opp["ticker"]

            if ticker in pos_tickers or ticker in ord_tickers:
                stats["skipped_existing"] += 1
                continue

            contracts = self._calculate_position_size(opp, portfolio, cash)
            if contracts < 1:
                stats["skipped_size"] += 1
                continue

            price = opp["our_price"]
            cost = contracts * price
            profit = contracts * opp["profit"]

            if self.dry_run:
                kelly_info = ""
                if self.use_kelly:
                    true_prob = opp["true_no_prob"] / 100
                    odds = (100 - price) / price
                    kf = kelly_fraction(true_prob, odds)
                    kelly_info = f" kelly:{kf:.1%}"
                print(
                    f"  🏷️ [DRY] NO x{contracts} @ {price}¢ | "
                    f"ask:{opp['lowest_no_ask']}¢ EV:{opp['true_no_prob']}¢ "
                    f"edge:{opp['edge']}¢ | "
                    f"+${profit/100:.2f} ({opp['roi_pct']:.1f}% / {opp['annualized_roi']:.0f}%ann) | "
                    f"{opp['days_to_expiry']}d{kelly_info}",
                    flush=True,
                )
                print(f"    {opp['ticker']} — {opp['title']}", flush=True)
                stats["placed"] += 1
                stats["total_potential_profit"] += profit
                stats["total_deployed"] += cost
                continue

            try:
                r = await self.client.place_order(
                    ticker=ticker,
                    side="no",
                    action="buy",
                    count=contracts,
                    no_price=price,
                )
                order = r.get("order", {})
                status = order.get("status", "?")
                filled = order.get("fill_count", 0)

                if filled > 0:
                    stats["filled"] += filled
                    print(
                        f"  🎯 FILLED NO x{filled}/{contracts} @ {price}¢ | "
                        f"edge:{opp['edge']}¢ +${filled * opp['profit']/100:.2f} | {ticker}",
                        flush=True,
                    )
                else:
                    print(
                        f"  ✅ NO x{contracts} @ {price}¢ | {status} | "
                        f"edge:{opp['edge']}¢ {opp['roi_pct']:.1f}% | {ticker}",
                        flush=True,
                    )

                stats["placed"] += 1
                stats["total_potential_profit"] += profit
                stats["total_deployed"] += cost
                ord_tickers.add(ticker)
                await asyncio.sleep(0.2)

            except Exception as e:
                print(f"  ❌ {ticker}: {e}", flush=True)
                stats["errors"] += 1
                await asyncio.sleep(0.3)

        return stats

    def _calculate_position_size(self, opp: Dict, portfolio: int, cash: int) -> int:
        """Size each position using Kelly or fixed fraction."""
        max_position_value = int(portfolio * self.max_position_pct)
        price = opp["our_price"]

        if self.use_kelly:
            true_prob = opp["true_no_prob"] / 100
            odds = (100 - price) / price
            kf = kelly_fraction(true_prob, odds)
            half_kelly_f = kf * 0.5
            kelly_position = int(portfolio * half_kelly_f)
            position_value = min(kelly_position, max_position_value)
        else:
            position_value = max_position_value

        contracts = max(1, position_value // price)
        contracts = min(contracts, 200)
        return contracts

    async def _cancel_yes_orders(self) -> int:
        """Cancel any resting YES-side orders (legacy)."""
        try:
            orders_resp = await self.client.get_orders(status="resting")
            orders = orders_resp.get("orders", [])
            yes_orders = [o for o in orders if o.get("side") == "yes"]
            cancelled = 0
            for o in yes_orders:
                try:
                    await self.client.cancel_order(o["order_id"])
                    print(
                        f"  🗑️ Cancelled YES: {o['ticker']} @ {o.get('yes_price', '?')}¢",
                        flush=True,
                    )
                    cancelled += 1
                    await asyncio.sleep(0.15)
                except Exception as e:
                    logger.warning("Cancel failed %s: %s", o["ticker"], e)
            if not yes_orders:
                print("  No legacy YES orders.", flush=True)
            return cancelled
        except Exception as e:
            logger.error("Error cancelling YES orders: %s", e)
            return 0

    async def check_fills(self) -> None:
        """Check recent fills and resting orders."""
        bal = await self.client.get_balance()
        portfolio = bal.get("portfolio_value", 0)
        cash = bal.get("balance", 0)
        print(
            f"💰 Cash: ${cash/100:.2f} | Portfolio: ${portfolio/100:.2f} | "
            f"Total: ${(cash+portfolio)/100:.2f}",
            flush=True,
        )

        try:
            orders_resp = await self.client.get_orders(status="resting")
            resting = orders_resp.get("orders", [])
            no_resting = [o for o in resting if o.get("side") == "no"]
            yes_resting = [o for o in resting if o.get("side") == "yes"]
            print(
                f"📋 Resting: {len(no_resting)} NO, {len(yes_resting)} YES",
                flush=True,
            )
        except Exception:
            pass

        try:
            fills_resp = await self.client.get_fills(limit=20)
            fill_list = fills_resp.get("fills", [])
            print(f"\n📊 Last 20 fills:", flush=True)
            for f in fill_list:
                ticker = f.get("ticker", "")
                side = f.get("side", "")
                count = f.get("count", 0)
                price = f.get("yes_price", f.get("no_price", 0))
                created = f.get("created_time", "")[:16]
                print(f"  {created} | {side} x{count} @ {price}¢ | {ticker}", flush=True)
        except Exception:
            pass
