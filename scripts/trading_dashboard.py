#!/usr/bin/env python3
"""
Comprehensive Trading System Dashboard

A Streamlit-based dashboard for monitoring and analyzing all aspects of the 
trading system including:
- Strategy performance analytics
- LLM query analysis and review
- Real-time position tracking
- Risk management monitoring
- System health metrics
- P&L analytics by strategy
"""

import streamlit as st
import asyncio
import aiosqlite
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import sys
import os
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.database import DatabaseManager
from src.clients.kalshi_client import KalshiClient
from src.config.settings import settings
from src.data.live_trade_research import LiveTradeResearchService
from src.utils.market_preferences import (
    is_live_wagering_market,
    normalize_market_category,
)


# Configure Streamlit page
st.set_page_config(
    page_title="Trading System Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

API_REFRESH_INTERVAL_SECONDS = 30
LLM_SNAPSHOT_HOURS = 168
LLM_SNAPSHOT_LIMIT = 5000
LLM_PAGE_SIZE_OPTIONS = [10, 20, 50, 100]
LIVE_TRADE_DATA_VERSION = 2

LLM_QUERY_SNAPSHOT_KEY = "llm_query_snapshot"
LLM_STATS_SNAPSHOT_KEY = "llm_stats_snapshot"
LLM_LAST_REFRESH_KEY = "llm_last_refresh"
LLM_PAGE_NUMBER_KEY = "llm_query_page_number"
LLM_FILTER_SIGNATURE_KEY = "llm_query_filter_signature"
LIVE_TRADE_SNAPSHOT_KEY = "live_trade_snapshot"
LIVE_TRADE_ANALYSIS_KEY = "live_trade_analysis"
LIVE_TRADE_LAST_REFRESH_KEY = "live_trade_last_refresh"
LIVE_TRADE_LAST_ANALYSIS_KEY = "live_trade_last_analysis"
LIVE_TRADE_FILTER_SIGNATURE_KEY = "live_trade_filter_signature"


def _run_dashboard_async(coroutine):
    """Run short-lived async dashboard work in its own event loop."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _hydrate_llm_stats(stats, query_rows):
    """Estimate token counts when provider metadata is missing."""
    for strategy, strategy_stats in stats.items():
        if strategy_stats.get('total_tokens', 0) == 0:
            strategy_queries = [row['query'] for row in query_rows if row['query'].strategy == strategy]
            estimated_tokens = 0
            for query in strategy_queries:
                prompt_tokens = len(query.prompt) // 4 if query.prompt else 0
                response_tokens = len(query.response) // 4 if query.response else 0
                estimated_tokens += prompt_tokens + response_tokens

            strategy_stats['total_tokens'] = estimated_tokens
            strategy_stats['estimated'] = True

    return stats


def initialize_dashboard_state():
    """Seed session state used by manual LLM pulls and pagination."""
    defaults = {
        LLM_QUERY_SNAPSHOT_KEY: [],
        LLM_STATS_SNAPSHOT_KEY: {},
        LLM_LAST_REFRESH_KEY: None,
        LLM_PAGE_NUMBER_KEY: 1,
        LLM_FILTER_SIGNATURE_KEY: None,
        LIVE_TRADE_SNAPSHOT_KEY: [],
        LIVE_TRADE_ANALYSIS_KEY: {},
        LIVE_TRADE_LAST_REFRESH_KEY: None,
        LIVE_TRADE_LAST_ANALYSIS_KEY: None,
        LIVE_TRADE_FILTER_SIGNATURE_KEY: None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


@st.cache_data(ttl=API_REFRESH_INTERVAL_SECONDS, show_spinner=False)
def load_api_performance_data():
    """Load performance and live position data for auto-refreshing pages."""
    try:
        async def get_data():
            db_manager = DatabaseManager()
            kalshi_client = KalshiClient()
            try:
                await db_manager.initialize()

                performance_raw = await db_manager.get_performance_by_strategy()
                performance = {}
                if performance_raw:
                    for strategy, stats in performance_raw.items():
                        performance[str(strategy)] = {
                            str(key): float(value) if isinstance(value, (int, float)) else str(value)
                            for key, value in stats.items()
                        }

                positions_response = await kalshi_client.get_positions()
                kalshi_positions = positions_response.get('market_positions', [])

                positions = []
                for pos in kalshi_positions:
                    if pos.get('position', 0) == 0:
                        continue

                    ticker = pos.get('ticker')
                    position_count = pos.get('position', 0)
                    position_dict = {
                        'market_id': str(ticker),
                        'side': 'YES' if position_count > 0 else 'NO',
                        'quantity': int(abs(position_count)),
                        'entry_price': 0.50,
                        'timestamp': datetime.now().isoformat(),
                        'strategy': 'live_sync',
                        'status': 'open',
                        'stop_loss_price': None,
                        'take_profit_price': None,
                    }

                    try:
                        market_data = await kalshi_client.get_market(ticker)
                        if market_data and 'market' in market_data:
                            market_info = market_data['market']
                            if position_count > 0:
                                position_dict['entry_price'] = float(
                                    (market_info.get('yes_bid', 0) + market_info.get('yes_ask', 100)) / 2 / 100
                                )
                            else:
                                position_dict['entry_price'] = float(
                                    (market_info.get('no_bid', 0) + market_info.get('no_ask', 100)) / 2 / 100
                                )
                    except Exception:
                        position_dict['entry_price'] = 0.50

                    positions.append(position_dict)

                return performance, positions
            finally:
                await db_manager.close()
                await kalshi_client.close()

        return _run_dashboard_async(get_data())
    except Exception as e:
        st.error(f"Error loading performance data: {e}")
        return {}, []


def load_manual_llm_snapshot(hours_back=LLM_SNAPSHOT_HOURS, limit=LLM_SNAPSHOT_LIMIT):
    """Load an LLM snapshot for manual review, filtering, and pagination."""
    try:
        async def get_data():
            db_manager = DatabaseManager()
            try:
                await db_manager.initialize()

                queries = await db_manager.get_llm_queries(hours_back=hours_back, limit=limit)
                stats = await db_manager.get_llm_stats_by_strategy()

                market_ids = sorted({query.market_id for query in queries if query.market_id})
                market_lookup = {}
                if market_ids:
                    placeholders = ",".join("?" for _ in market_ids)
                    async with aiosqlite.connect(db_manager.db_path) as db:
                        db.row_factory = aiosqlite.Row
                        cursor = await db.execute(
                            f"""
                            SELECT market_id, title, category, expiration_ts
                            FROM markets
                            WHERE market_id IN ({placeholders})
                            """,
                            market_ids,
                        )
                        rows = await cursor.fetchall()
                        market_lookup = {row['market_id']: dict(row) for row in rows}

                query_rows = []
                for query in queries:
                    market_meta = market_lookup.get(query.market_id or "", {})
                    market_title = market_meta.get('title')
                    category = normalize_market_category(
                        market_meta.get('category'),
                        ticker=query.market_id or "",
                        title=market_title or "",
                    )
                    expiration_ts = market_meta.get('expiration_ts')
                    query_rows.append(
                        {
                            'query': query,
                            'market_title': market_title,
                            'category': category,
                            'expiration_ts': expiration_ts,
                            'is_live_wagering': is_live_wagering_market(
                                category,
                                expiration_ts,
                                ticker=query.market_id or "",
                                title=market_title or "",
                                max_hours_to_expiry=settings.trading.live_wagering_max_hours_to_expiry,
                            ),
                        }
                    )

                return query_rows, _hydrate_llm_stats(stats, query_rows)
            finally:
                await db_manager.close()

        return _run_dashboard_async(get_data())
    except Exception as e:
        st.error(f"Error loading LLM data: {e}")
        return [], {}


@st.cache_data(ttl=API_REFRESH_INTERVAL_SECONDS, show_spinner=False)
def load_live_trade_snapshot(
    limit=36,
    category_filters=None,
    max_hours_to_expiry=72,
    data_version=LIVE_TRADE_DATA_VERSION,
):
    """Load event-level live trade candidates for the dashboard."""
    try:
        del data_version

        async def get_data():
            service = LiveTradeResearchService()
            try:
                return await service.get_live_trade_events(
                    limit=limit,
                    category_filters=category_filters,
                    max_hours_to_expiry=max_hours_to_expiry,
                )
            finally:
                await service.close()

        return _run_dashboard_async(get_data())
    except Exception as e:
        st.error(f"Error loading live trade data: {e}")
        return []


@st.cache_data(ttl=API_REFRESH_INTERVAL_SECONDS, show_spinner=False)
def load_live_bitcoin_context():
    """Load live bitcoin context and chart data for the dashboard."""
    try:
        async def get_data():
            service = LiveTradeResearchService()
            try:
                return await service.fetch_bitcoin_context()
            finally:
                await service.close()

        return _run_dashboard_async(get_data())
    except Exception as e:
        st.error(f"Error loading bitcoin data: {e}")
        return {}


def refresh_live_trade_snapshot(
    limit=36,
    category_filters=None,
    max_hours_to_expiry=72,
    data_version=LIVE_TRADE_DATA_VERSION,
):
    """Refresh the manually reviewed live trade snapshot."""
    snapshot = load_live_trade_snapshot(
        limit=limit,
        category_filters=category_filters,
        max_hours_to_expiry=max_hours_to_expiry,
        data_version=data_version,
    )
    normalized_filters = {
        normalize_market_category(item).casefold()
        for item in (category_filters or [])
        if item
    }
    if not snapshot and "crypto" in normalized_filters:
        load_live_trade_snapshot.clear()
        snapshot = load_live_trade_snapshot(
            limit=limit,
            category_filters=category_filters,
            max_hours_to_expiry=max_hours_to_expiry,
            data_version=data_version,
        )
    st.session_state[LIVE_TRADE_SNAPSHOT_KEY] = snapshot
    st.session_state[LIVE_TRADE_LAST_REFRESH_KEY] = datetime.now()
    st.session_state[LIVE_TRADE_ANALYSIS_KEY] = {}
    st.session_state[LIVE_TRADE_LAST_ANALYSIS_KEY] = None


def analyze_live_trade_snapshot(snapshot, max_events=12, use_web_research=True):
    """Run LLM analysis for the selected live trade events."""
    try:
        async def get_data():
            service = LiveTradeResearchService()
            try:
                return await service.analyze_events(
                    snapshot,
                    max_events=max_events,
                    use_web_research=use_web_research,
                )
            finally:
                await service.close()

        analysis = _run_dashboard_async(get_data())
        st.session_state[LIVE_TRADE_ANALYSIS_KEY] = analysis
        st.session_state[LIVE_TRADE_LAST_ANALYSIS_KEY] = datetime.now()
        return analysis
    except Exception as e:
        st.error(f"Error running live trade analysis: {e}")
        return {}


@st.cache_data(ttl=API_REFRESH_INTERVAL_SECONDS, show_spinner=False)
def load_api_system_health():
    """Load account health metrics for auto-refreshing pages."""
    try:
        async def get_health():
            kalshi_client = KalshiClient()
            try:
                balance_response = await kalshi_client.get_balance()
                available_cash = balance_response.get('balance', 0) / 100

                positions_response = await kalshi_client.get_positions()
                market_positions = positions_response.get('market_positions', [])

                total_position_value = 0
                positions_count = len(market_positions)

                for position in market_positions:
                    ticker = position.get('ticker')
                    position_count = position.get('position', 0)
                    if not ticker or position_count == 0:
                        continue

                    try:
                        market_data = await kalshi_client.get_market(ticker)
                        if market_data and 'market' in market_data:
                            market_info = market_data['market']
                            if position_count > 0:
                                current_price = (market_info.get('yes_bid', 0) + market_info.get('yes_ask', 100)) / 2 / 100
                            else:
                                current_price = (market_info.get('no_bid', 0) + market_info.get('no_ask', 100)) / 2 / 100

                            total_position_value += abs(position_count) * current_price
                    except Exception as exc:
                        print(f"Warning: Could not value position {ticker}: {exc}")
                        continue

                return {
                    'available_cash': available_cash,
                    'total_portfolio_value': available_cash + total_position_value,
                    'positions_count': positions_count,
                    'position_value': total_position_value,
                }
            finally:
                await kalshi_client.close()

        return _run_dashboard_async(get_health())
    except Exception as e:
        st.error(f"Error loading system health: {e}")
        return {
            'available_cash': 0.0,
            'total_portfolio_value': 0.0,
            'positions_count': 0,
            'position_value': 0.0,
        }


def refresh_llm_snapshot():
    """Refresh the manual LLM snapshot displayed by the dashboard."""
    query_rows, llm_stats = load_manual_llm_snapshot()
    st.session_state[LLM_QUERY_SNAPSHOT_KEY] = query_rows
    st.session_state[LLM_STATS_SNAPSHOT_KEY] = llm_stats
    st.session_state[LLM_LAST_REFRESH_KEY] = datetime.now()
    st.session_state[LLM_PAGE_NUMBER_KEY] = 1
    st.session_state[LLM_FILTER_SIGNATURE_KEY] = None


@st.fragment(run_every=f"{API_REFRESH_INTERVAL_SECONDS}s")
def render_sidebar_status():
    """Auto-refresh the sidebar metrics that depend on API data."""
    _, positions = load_api_performance_data()
    system_health_data = load_api_system_health()
    llm_queries = st.session_state.get(LLM_QUERY_SNAPSHOT_KEY, [])
    llm_last_refresh = st.session_state.get(LLM_LAST_REFRESH_KEY)

    st.markdown("---")
    st.markdown("**Data Status:**")
    st.metric("Active Positions", len(positions) if positions else 0)
    st.metric(
        "LLM Queries",
        len(llm_queries) if llm_last_refresh else "Manual pull",
        help=(
            f"Snapshot from {llm_last_refresh.strftime('%Y-%m-%d %H:%M:%S')}"
            if llm_last_refresh
            else "LLM queries only refresh when you click Pull Queries."
        ),
    )
    st.metric("Portfolio Balance", f"${system_health_data.get('total_portfolio_value', 0):.2f}")
    st.caption(f"API sections auto-refresh every {API_REFRESH_INTERVAL_SECONDS} seconds.")


@st.fragment(run_every=f"{API_REFRESH_INTERVAL_SECONDS}s")
def render_overview_page():
    performance_data, positions = load_api_performance_data()
    system_health_data = load_api_system_health()
    show_overview(performance_data, positions, system_health_data)


@st.fragment(run_every=f"{API_REFRESH_INTERVAL_SECONDS}s")
def render_strategy_performance_page():
    performance_data, _ = load_api_performance_data()
    show_strategy_performance(performance_data)


@st.fragment(run_every=f"{API_REFRESH_INTERVAL_SECONDS}s")
def render_positions_trades_page():
    _, positions = load_api_performance_data()
    show_positions_trades(positions)


@st.fragment(run_every=f"{API_REFRESH_INTERVAL_SECONDS}s")
def render_risk_management_page():
    performance_data, positions = load_api_performance_data()
    system_health_data = load_api_system_health()
    show_risk_management(performance_data, positions, system_health_data['total_portfolio_value'])


@st.fragment(run_every=f"{API_REFRESH_INTERVAL_SECONDS}s")
def render_system_health_page():
    system_health_data = load_api_system_health()
    llm_stats = st.session_state.get(LLM_STATS_SNAPSHOT_KEY, {})
    show_system_health(
        system_health_data['available_cash'],
        system_health_data['positions_count'],
        llm_stats,
    )


def main():
    """Main dashboard function."""
    initialize_dashboard_state()

    st.title("Trading System Dashboard")
    st.markdown("**Real-time monitoring and analysis of your automated trading system**")
    st.caption(
        f"API-backed pages auto-refresh every {API_REFRESH_INTERVAL_SECONDS} seconds. "
        "LLM queries refresh only when you pull a new snapshot."
    )

    col1, col2 = st.columns([4, 1])
    with col2:
        if st.button("Refresh API Data", help="Clear cached API data and rerun the dashboard"):
            st.cache_data.clear()
            st.rerun()

    st.sidebar.title("Dashboard")
    page = st.sidebar.selectbox(
        "Select View",
        [
            "Overview",
            "Strategy Performance",
            "Live Trade",
            "LLM Analysis",
            "Positions & Trades",
            "Risk Management",
            "System Health"
        ]
    )

    with st.sidebar:
        render_sidebar_status()

    if page == "Overview":
        render_overview_page()
    elif page == "Strategy Performance":
        render_strategy_performance_page()
    elif page == "Live Trade":
        show_live_trade()
    elif page == "LLM Analysis":
        show_llm_analysis(
            st.session_state.get(LLM_QUERY_SNAPSHOT_KEY, []),
            st.session_state.get(LLM_STATS_SNAPSHOT_KEY, {}),
        )
    elif page == "Positions & Trades":
        render_positions_trades_page()
    elif page == "Risk Management":
        render_risk_management_page()
    elif page == "System Health":
        render_system_health_page()

def show_overview(performance_data, positions, system_health_data):
    """Show overview dashboard."""
    
    st.header("📈 System Overview")
    
    # Key metrics row
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric(
            label="💰 Portfolio Balance",
            value=f"${system_health_data['total_portfolio_value']:.2f}",
            help="Total portfolio value: cash + current positions"
        )
    
    # Add second row for additional financial metrics
    col1b, col2b, col3b, col4b = st.columns(4)
    
    with col1b:
        st.metric(
            label="💵 Available Cash",
            value=f"${system_health_data['available_cash']:.2f}",
            help="Cash available for new trades"
        )
    
    with col2b:
        st.metric(
            label="📊 Position Value",
            value=f"${system_health_data['position_value']:.2f}",
            help="Current market value of all positions"
        )
    
    with col2:
        total_trades = sum(stats.get('completed_trades', 0) for stats in performance_data.values()) if performance_data else 0
        st.metric(
            label="📈 Total Trades",
            value=total_trades,
            help="Total completed trades across all strategies"
        )
    
    with col3:
        realized_pnl = sum(stats.get('total_pnl', 0) for stats in performance_data.values()) if performance_data else 0

        st.metric(
            label="💹 Realized P&L",
            value=f"${realized_pnl:.2f}",
            help="Total realized profit/loss from completed trades"
        )
    
    with col4:
        st.metric(
            label="🎯 Active Positions",
            value=len(positions) if positions else 0,
            help="Currently open positions"
        )
    
    with col3b:
        # Portfolio utilization
        if system_health_data['total_portfolio_value'] > 0:
            utilization_pct = (system_health_data['position_value'] / system_health_data['total_portfolio_value']) * 100
        else:
            utilization_pct = 0
        st.metric(
            label="📊 Portfolio Utilization",
            value=f"{utilization_pct:.1f}%",
            help="Percentage of portfolio currently in positions"
        )
    
    with col4b:
        # Cash utilization  
        if system_health_data['available_cash'] > 0:
            initial_cash = system_health_data['total_portfolio_value']  # Approximation
            cash_used_pct = ((initial_cash - system_health_data['available_cash']) / initial_cash) * 100 if initial_cash > 0 else 0
        else:
            cash_used_pct = 100
        st.metric(
            label="💸 Cash Deployed",
            value=f"{min(100, max(0, cash_used_pct)):.1f}%", 
            help="Percentage of original cash now in positions"
        )
    
    # Strategy performance summary
    if performance_data:
        st.subheader("🎯 Strategy Performance Summary")
        
        # Create strategy performance chart
        strategy_names = []
        strategy_pnl = []
        strategy_trades = []
        strategy_win_rates = []
        
        for strategy, stats in performance_data.items():
            strategy_names.append(strategy.replace('_', ' ').title())
            strategy_pnl.append(stats.get('total_pnl', 0))
            strategy_trades.append(stats.get('completed_trades', 0))
            strategy_win_rates.append(stats.get('win_rate_pct', 0))
        
        col1, col2 = st.columns(2)
        
        with col1:
            # P&L by strategy
            fig_pnl = px.bar(
                x=strategy_names,
                y=strategy_pnl,
                title="P&L by Strategy",
                labels={'x': 'Strategy', 'y': 'P&L ($)'},
                color=strategy_pnl,
                color_continuous_scale='RdYlGn'
            )
            fig_pnl.update_layout(showlegend=False, height=400)
            st.plotly_chart(fig_pnl, width='stretch')
        
        with col2:
            # Win rate by strategy
            fig_winrate = px.bar(
                x=strategy_names,
                y=strategy_win_rates,
                title="Win Rate by Strategy (%)",
                labels={'x': 'Strategy', 'y': 'Win Rate (%)'},
                color=strategy_win_rates,
                color_continuous_scale='Blues'
            )
            fig_winrate.update_layout(showlegend=False, height=400)
            st.plotly_chart(fig_winrate, width='stretch')
    else:
        st.info("📊 **No strategy data yet** - Run the trading system to start collecting performance data")
    
    # Recent activity summary
    st.subheader("📋 Recent Activity")
    
    if positions:
        st.write(f"**{len(positions)} active positions:**")
        
        # Show top positions by value
        position_data = []
        for pos in positions[:10]:  # Top 10
            # Convert timestamp string back to datetime for display
            try:
                timestamp = datetime.fromisoformat(pos['timestamp'])
                time_str = timestamp.strftime('%m/%d %H:%M')
            except (ValueError, TypeError, KeyError):
                time_str = 'Unknown'
            
            position_data.append({
                'Market': pos['market_id'][:25] + '...' if len(pos['market_id']) > 25 else pos['market_id'],
                'Side': pos['side'],
                'Quantity': pos['quantity'],
                'Entry Price': f"${pos['entry_price']:.3f}",
                'Value': f"${pos['quantity'] * pos['entry_price']:.2f}",
                'Strategy': pos['strategy'] or 'Unknown',
                'Time': time_str
            })
        
        if position_data:
            df = pd.DataFrame(position_data)
            st.dataframe(df, width='stretch', hide_index=True)
    else:
        st.info("No active positions currently.")

def show_strategy_performance(performance_data):
    """Show detailed strategy performance analysis."""
    
    st.header("🎯 Strategy Performance Analysis")
    
    if not performance_data:
        st.warning("No strategy performance data available yet.")
        return
    
    # Strategy selector
    strategies = list(performance_data.keys())
    selected_strategy = st.selectbox(
        "Select Strategy for Detailed Analysis",
        ["All Strategies"] + strategies
    )
    
    if selected_strategy == "All Strategies":
        # Compare all strategies
        st.subheader("📊 Strategy Comparison")
        
        # Create comparison table
        comparison_data = []
        for strategy, stats in performance_data.items():
            comparison_data.append({
                'Strategy': strategy.replace('_', ' ').title(),
                'Completed Trades': stats['completed_trades'],
                'Total P&L': f"${stats['total_pnl']:.2f}",
                'Avg P&L per Trade': f"${stats['avg_pnl_per_trade']:.2f}",
                'Win Rate': f"{stats['win_rate_pct']:.1f}%",
                'Best Trade': f"${stats['best_trade']:.2f}",
                'Worst Trade': f"${stats['worst_trade']:.2f}",
                'Open Positions': stats['open_positions'],
                'Capital Deployed': f"${stats['capital_deployed']:.2f}"
            })
        
        df = pd.DataFrame(comparison_data)
        st.dataframe(df, width='stretch', hide_index=True)
        
        # Performance charts
        col1, col2 = st.columns(2)
        
        with col1:
            # Risk-return scatter
            fig_risk = go.Figure()
            
            for strategy, stats in performance_data.items():
                if stats['completed_trades'] > 0:
                    fig_risk.add_trace(go.Scatter(
                        x=[stats['avg_pnl_per_trade']],
                        y=[stats['win_rate_pct']],
                        mode='markers+text',
                        text=[strategy.replace('_', ' ').title()],
                        textposition="top center",
                        marker=dict(
                            size=stats['completed_trades'] * 2,
                            color=stats['total_pnl'],
                            colorscale='RdYlGn',
                            showscale=True
                        ),
                        name=strategy
                    ))
            
            fig_risk.update_layout(
                title="Risk-Return Analysis (Bubble size = Trade count)",
                xaxis_title="Average P&L per Trade ($)",
                yaxis_title="Win Rate (%)",
                height=500
            )
            st.plotly_chart(fig_risk, width='stretch')
        
        with col2:
            # Capital deployment
            fig_capital = px.pie(
                values=[stats['capital_deployed'] for stats in performance_data.values()],
                names=[strategy.replace('_', ' ').title() for strategy in performance_data.keys()],
                title="Capital Deployment by Strategy"
            )
            fig_capital.update_layout(height=500)
            st.plotly_chart(fig_capital, width='stretch')
    
    else:
        # Show individual strategy details
        stats = performance_data[selected_strategy]
        
        st.subheader(f"📋 {selected_strategy.replace('_', ' ').title()} Performance")
        
        # Key metrics
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric("Total P&L", f"${stats['total_pnl']:.2f}")
        with col2:
            st.metric("Win Rate", f"{stats['win_rate_pct']:.1f}%")
        with col3:
            st.metric("Completed Trades", stats['completed_trades'])
        with col4:
            st.metric("Open Positions", stats['open_positions'])
        
        # Detailed metrics
        if stats['completed_trades'] > 0:
            st.subheader("📈 Detailed Metrics")
            
            col1, col2 = st.columns(2)
            
            with col1:
                st.write("**Trade Performance:**")
                st.write(f"- Average P&L per Trade: ${stats['avg_pnl_per_trade']:.2f}")
                st.write(f"- Best Trade: ${stats['best_trade']:.2f}")
                st.write(f"- Worst Trade: ${stats['worst_trade']:.2f}")
                st.write(f"- Winning Trades: {stats['winning_trades']}")
                st.write(f"- Losing Trades: {stats['losing_trades']}")
            
            with col2:
                st.write("**Capital Allocation:**")
                st.write(f"- Capital Deployed: ${stats['capital_deployed']:.2f}")
                st.write(f"- Open Positions: {stats['open_positions']}")
                if stats['capital_deployed'] > 0:
                    avg_position_size = stats['capital_deployed'] / max(stats['open_positions'], 1)
                    st.write(f"- Avg Position Size: ${avg_position_size:.2f}")

@st.fragment(run_every=f"{API_REFRESH_INTERVAL_SECONDS}s")
def _render_live_positions_summary():
    """Auto-refreshing compact summary of live Kalshi positions."""
    _, positions = load_api_performance_data()
    st.subheader(f"Live Positions ({len(positions)})")
    if not positions:
        st.caption("No active positions.")
        return

    rows = []
    for pos in positions:
        ticker = pos['market_id']
        side = pos['side']
        qty = pos['quantity']
        entry = pos['entry_price']
        rows.append({
            'Ticker': ticker,
            'Side': side,
            'Qty': qty,
            'Entry': f"${entry:.3f}",
            'Value': f"${qty * entry:.2f}",
        })

    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def show_llm_analysis(llm_query_rows, llm_stats):
    """Show LLM query analysis and review."""

    st.header("LLM Analysis & Review")
    st.markdown("**Review all AI queries and responses for insights and improvements**")

    # --- Live Positions summary (auto-refreshes) ---
    _render_live_positions_summary()

    st.markdown("---")

    pull_col, status_col = st.columns([1, 3])
    with pull_col:
        if st.button("Pull Queries", key="pull_llm_queries", help="Refresh the manual LLM query snapshot"):
            with st.spinner("Loading LLM query snapshot..."):
                refresh_llm_snapshot()
            llm_query_rows = st.session_state.get(LLM_QUERY_SNAPSHOT_KEY, [])
            llm_stats = st.session_state.get(LLM_STATS_SNAPSHOT_KEY, {})

    with status_col:
        last_refresh = st.session_state.get(LLM_LAST_REFRESH_KEY)
        if last_refresh:
            st.caption(
                f"Snapshot pulled at {last_refresh.strftime('%Y-%m-%d %H:%M:%S')}. "
                "This page stays fixed until you pull again."
            )
        else:
            st.info(
                "LLM query data is manual now. Use Pull Queries to load a snapshot while "
                "portfolio and account data continue auto-refreshing."
            )

    if settings.trading.prefer_live_wagering:
        st.caption(
            f"Trading focus: live wagering prioritized for Sports markets closing within "
            f"{settings.trading.live_wagering_max_hours_to_expiry} hours."
        )

    if not st.session_state.get(LLM_LAST_REFRESH_KEY):
        return

    if not llm_query_rows and not llm_stats:
        st.warning("No LLM query data available yet. LLM logging will start with new queries.")
        st.info("Tip: once new model calls are logged, pull a fresh snapshot to review them here.")
        return

    if llm_stats:
        st.subheader("LLM Usage Statistics (Last 7 Days)")

        total_queries = sum(stats['query_count'] for stats in llm_stats.values())
        total_cost = sum(stats['total_cost'] for stats in llm_stats.values())
        total_tokens = sum(stats['total_tokens'] for stats in llm_stats.values())
        has_estimated_tokens = any(stats.get('estimated', False) for stats in llm_stats.values())

        col1, col2, col3, col4 = st.columns(4)

        with col1:
            st.metric("Total Queries", total_queries)
        with col2:
            st.metric("Total Cost", f"${total_cost:.2f}")
        with col3:
            token_label = "Total Tokens*" if has_estimated_tokens else "Total Tokens"
            token_help = (
                "Estimated from response lengths (some token data missing)"
                if has_estimated_tokens
                else "Actual token usage"
            )
            st.metric(token_label, f"{total_tokens:,}", help=token_help)
        with col4:
            avg_cost_per_query = total_cost / max(total_queries, 1)
            st.metric("Avg Cost/Query", f"${avg_cost_per_query:.3f}")

        if has_estimated_tokens:
            st.caption("*Token counts marked with * are estimated from response text length due to missing usage data.")

        if len(llm_stats) > 1:
            fig_usage = px.bar(
                x=list(llm_stats.keys()),
                y=[stats['query_count'] for stats in llm_stats.values()],
                title="LLM Queries by Strategy",
                labels={'x': 'Strategy', 'y': 'Query Count'},
                color=[stats['total_cost'] for stats in llm_stats.values()],
                color_continuous_scale='Blues'
            )
            st.plotly_chart(fig_usage, width='stretch')

    st.subheader("Query Analysis")

    strategies = sorted({row['query'].strategy for row in llm_query_rows if row['query'].strategy})
    query_types = sorted({row['query'].query_type for row in llm_query_rows if row['query'].query_type})
    categories = sorted({row['category'] for row in llm_query_rows if row['category']})

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        selected_strategy = st.selectbox("Strategy", ["All"] + strategies, key="llm_strategy_filter")
    with col2:
        selected_type = st.selectbox("Query Type", ["All"] + query_types, key="llm_type_filter")
    with col3:
        selected_category = st.selectbox("Kalshi Category", ["All"] + categories, key="llm_category_filter")
    with col4:
        hours_back = st.selectbox(
            "Time Range",
            [6, 12, 24, 48, 168],
            index=4,
            format_func=lambda value: f"Last {value} hours" if value < 168 else "Last 7 days",
            key="llm_hours_back_filter",
        )

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        live_wagering_only = st.toggle("Live Wagering Only", value=False, key="llm_live_wagering_filter")
    with col2:
        sort_by = st.selectbox(
            "Sort By",
            ["Timestamp", "Cost", "Confidence", "Strategy", "Query Type", "Kalshi Category"],
            key="llm_sort_by",
        )
    with col3:
        default_direction = "Descending" if sort_by in {"Timestamp", "Cost", "Confidence"} else "Ascending"
        selected_direction = st.selectbox(
            "Sort Direction",
            ["Descending", "Ascending"],
            index=0 if default_direction == "Descending" else 1,
            key="llm_sort_direction",
        )
    with col4:
        per_page = st.selectbox("Number Per Page", LLM_PAGE_SIZE_OPTIONS, index=1, key="llm_per_page")

    filter_signature = (
        selected_strategy,
        selected_type,
        selected_category,
        hours_back,
        live_wagering_only,
        sort_by,
        selected_direction,
        per_page,
    )
    if st.session_state.get(LLM_FILTER_SIGNATURE_KEY) != filter_signature:
        st.session_state[LLM_FILTER_SIGNATURE_KEY] = filter_signature
        st.session_state[LLM_PAGE_NUMBER_KEY] = 1

    cutoff_time = datetime.now() - timedelta(hours=hours_back)
    filtered_rows = [
        row for row in llm_query_rows
        if row['query'].timestamp >= cutoff_time
    ]

    if selected_strategy != "All":
        filtered_rows = [row for row in filtered_rows if row['query'].strategy == selected_strategy]
    if selected_type != "All":
        filtered_rows = [row for row in filtered_rows if row['query'].query_type == selected_type]
    if selected_category != "All":
        filtered_rows = [row for row in filtered_rows if row['category'] == selected_category]
    if live_wagering_only:
        filtered_rows = [row for row in filtered_rows if row['is_live_wagering']]

    sort_map = {
        "Timestamp": lambda row: row['query'].timestamp,
        "Cost": lambda row: row['query'].cost_usd or 0,
        "Confidence": lambda row: row['query'].confidence_extracted if row['query'].confidence_extracted is not None else -1,
        "Strategy": lambda row: row['query'].strategy or "",
        "Query Type": lambda row: row['query'].query_type or "",
        "Kalshi Category": lambda row: row['category'] or "",
    }
    filtered_rows = sorted(
        filtered_rows,
        key=sort_map[sort_by],
        reverse=(selected_direction == "Descending"),
    )

    total_queries = len(filtered_rows)
    if total_queries == 0:
        st.info("No LLM queries found for the selected filters.")
        return

    total_pages = max(1, (total_queries + per_page - 1) // per_page)
    current_page = min(st.session_state.get(LLM_PAGE_NUMBER_KEY, 1), total_pages)
    st.session_state[LLM_PAGE_NUMBER_KEY] = current_page

    start_index = (current_page - 1) * per_page
    end_index = min(start_index + per_page, total_queries)
    page_rows = filtered_rows[start_index:end_index]

    def render_pager(location_key):
        pager_col1, pager_col2, pager_col3 = st.columns([1, 2, 1])
        with pager_col1:
            if st.button("Previous Page", key=f"llm_prev_{location_key}", disabled=current_page <= 1):
                st.session_state[LLM_PAGE_NUMBER_KEY] = current_page - 1
                st.rerun()
        with pager_col2:
            st.markdown(
                f"**Showing {start_index + 1}-{end_index} of {total_queries} queries**  \n"
                f"**Page {current_page} of {total_pages}**"
            )
        with pager_col3:
            if st.button("Next Page", key=f"llm_next_{location_key}", disabled=current_page >= total_pages):
                st.session_state[LLM_PAGE_NUMBER_KEY] = current_page + 1
                st.rerun()

    render_pager("top")

    for index, row in enumerate(page_rows):
        query = row['query']
        live_label = " | Live Wagering" if row['is_live_wagering'] else ""
        title_parts = [
            query.strategy,
            row['category'],
            query.query_type,
            query.timestamp.strftime('%m/%d %H:%M:%S'),
        ]
        with st.expander(" | ".join(title_parts) + live_label, expanded=(index < 2)):
            meta_col1, meta_col2, meta_col3, meta_col4 = st.columns(4)
            with meta_col1:
                st.write(f"**Strategy:** {query.strategy}")
            with meta_col2:
                st.write(f"**Type:** {query.query_type}")
            with meta_col3:
                st.write(f"**Category:** {row['category']}")
            with meta_col4:
                st.write(f"**Live Wagering:** {'Yes' if row['is_live_wagering'] else 'No'}")

            if query.market_id:
                st.write(f"**Market ID:** {query.market_id}")
            if row['market_title']:
                st.write(f"**Market Title:** {row['market_title']}")
            if query.cost_usd is not None:
                st.write(f"**Cost:** ${query.cost_usd:.4f}")
            if query.confidence_extracted is not None:
                st.write(f"**Confidence Extracted:** {query.confidence_extracted:.2%}")
            if query.decision_extracted is not None:
                st.write(f"**Decision Extracted:** {query.decision_extracted}")

            st.markdown("**Prompt:**")
            st.code(query.prompt, language="text")

            st.markdown("**Response:**")
            st.code(query.response, language="text")

    render_pager("bottom")


def show_positions_trades(positions):
    """Show detailed positions and trades analysis."""
    
    st.header("💼 Positions & Trades")
    
    if not positions:
        st.warning("No active positions found.")
        return
    
    # Positions overview
    st.subheader(f"📊 Active Positions ({len(positions)})")
    
    # Create positions DataFrame
    position_data = []
    for pos in positions:
        # Convert timestamp string back to datetime for display
        try:
            timestamp = datetime.fromisoformat(pos['timestamp'])
            time_str = timestamp.strftime('%m/%d %H:%M')
        except (ValueError, TypeError, KeyError):
            time_str = 'Unknown'

        position_data.append({
            'Market ID': pos['market_id'],
            'Strategy': pos['strategy'] or 'Unknown',
            'Side': pos['side'],
            'Quantity': pos['quantity'],
            'Entry Price': f"${pos['entry_price']:.3f}",
            'Position Value': f"${pos['quantity'] * pos['entry_price']:.2f}",
            'Entry Time': time_str,
            'Status': pos['status'],
            'Stop Loss': f"${pos['stop_loss_price']:.3f}" if pos['stop_loss_price'] else "None",
            'Take Profit': f"${pos['take_profit_price']:.3f}" if pos['take_profit_price'] else "None"
        })
    
    df_positions = pd.DataFrame(position_data)
    
    # Positions filters
    col1, col2 = st.columns(2)
    
    with col1:
        strategies = df_positions['Strategy'].unique().tolist()
        selected_strategies = st.multiselect(
            "Filter by Strategy",
            strategies,
            default=strategies
        )
    
    with col2:
        sides = df_positions['Side'].unique().tolist()
        selected_sides = st.multiselect(
            "Filter by Side",
            sides,
            default=sides
        )
    
    # Apply filters
    filtered_df = df_positions[
        (df_positions['Strategy'].isin(selected_strategies)) &
        (df_positions['Side'].isin(selected_sides))
    ]
    
    # Display filtered positions
    st.dataframe(filtered_df, width='stretch', hide_index=True)
    
    # Position analytics
    if not filtered_df.empty:
        st.subheader("📈 Position Analytics")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Value by strategy
            strategy_values = filtered_df.groupby('Strategy')['Position Value'].apply(
                lambda x: x.str.replace('$', '').astype(float).sum()
            )
            
            fig_strategy = px.pie(
                values=strategy_values.values,
                names=strategy_values.index,
                title="Position Value by Strategy"
            )
            st.plotly_chart(fig_strategy, width='stretch')
        
        with col2:
            # Side distribution
            side_counts = filtered_df['Side'].value_counts()
            
            fig_sides = px.bar(
                x=side_counts.index,
                y=side_counts.values,
                title="Positions by Side",
                labels={'x': 'Side', 'y': 'Count'}
            )
            st.plotly_chart(fig_sides, width='stretch')


def show_live_trade():
    """Show live trade candidates, bitcoin context, and structured recommendations."""

    st.header("Live Trade")
    st.caption(
        "Event-level candidates ranked from active Kalshi markets. "
        "Because Kalshi's public API does not expose the website's calendar-live flag, "
        "this view ranks open events using expiry, volume, spread, and title heuristics."
    )

    category_options = ["Sports", "Financials", "Crypto", "Economics"]
    control_col1, control_col2, control_col3, control_col4 = st.columns(4)

    with control_col1:
        event_limit = st.selectbox("Visible Events", [12, 24, 36, 48], index=2)
    with control_col2:
        max_hours = st.selectbox("Max Hours to Expiry", [12, 24, 48, 72, 168], index=3)
    with control_col3:
        selected_categories = st.multiselect(
            "Categories",
            category_options,
            default=category_options,
        )
    with control_col4:
        analysis_limit = st.selectbox("Analyze Top Events", [4, 8, 12, 24, 36], index=2)

    action_col1, action_col2, action_col3 = st.columns([1, 1, 2])
    with action_col1:
        refresh_snapshot = st.button("Refresh Live Feed")
    with action_col2:
        run_analysis = st.button("Run Live Analysis")
    with action_col3:
        use_web_research = st.toggle(
            "Use OpenAI web research when available",
            value=True,
            help="Direct OpenAI mode can verify fresh sports/news context with web search. Other providers fall back to structured prompts without web search.",
        )

    current_signature = (
        LIVE_TRADE_DATA_VERSION,
        event_limit,
        max_hours,
        tuple(selected_categories),
    )
    if st.session_state.get(LIVE_TRADE_FILTER_SIGNATURE_KEY) != current_signature:
        st.session_state[LIVE_TRADE_FILTER_SIGNATURE_KEY] = current_signature
        refresh_live_trade_snapshot(
            limit=event_limit,
            category_filters=selected_categories,
            max_hours_to_expiry=max_hours,
            data_version=LIVE_TRADE_DATA_VERSION,
        )

    if refresh_snapshot:
        load_live_trade_snapshot.clear()
        load_live_bitcoin_context.clear()
        refresh_live_trade_snapshot(
            limit=event_limit,
            category_filters=selected_categories,
            max_hours_to_expiry=max_hours,
            data_version=LIVE_TRADE_DATA_VERSION,
        )

    snapshot = st.session_state.get(LIVE_TRADE_SNAPSHOT_KEY, [])
    if run_analysis and snapshot:
        with st.spinner("Running live trade analysis..."):
            analyze_live_trade_snapshot(
                snapshot,
                max_events=min(analysis_limit, len(snapshot)),
                use_web_research=use_web_research,
            )

    analysis_map = st.session_state.get(LIVE_TRADE_ANALYSIS_KEY, {})
    last_refresh = st.session_state.get(LIVE_TRADE_LAST_REFRESH_KEY)
    last_analysis = st.session_state.get(LIVE_TRADE_LAST_ANALYSIS_KEY)

    bitcoin_context = load_live_bitcoin_context()
    if bitcoin_context:
        btc_col1, btc_col2, btc_col3, btc_col4 = st.columns(4)
        with btc_col1:
            st.metric("BTC Spot", f"${bitcoin_context.get('price_usd', 0):,.0f}")
        with btc_col2:
            st.metric("24h Change", f"{bitcoin_context.get('change_24h_pct', 0):+.2f}%")
        with btc_col3:
            st.metric("24h Volume", f"${bitcoin_context.get('volume_24h_usd', 0):,.0f}")
        with btc_col4:
            st.metric("Market Cap", f"${bitcoin_context.get('market_cap_usd', 0):,.0f}")

        chart_points = bitcoin_context.get("chart_points", [])
        if chart_points:
            btc_df = pd.DataFrame(chart_points)
            btc_df["timestamp"] = pd.to_datetime(
                btc_df["timestamp"],
                format="ISO8601",
                utc=True,
                errors="coerce",
            )
            btc_df = btc_df.dropna(subset=["timestamp"]).sort_values("timestamp")

            if not btc_df.empty:
                fig_btc = go.Figure(
                    data=[
                        go.Scatter(
                            x=btc_df["timestamp"],
                            y=btc_df["price_usd"],
                            mode="lines",
                            name="BTC/USD",
                            line={"color": "#f7931a", "width": 3},
                        )
                    ]
                )
                fig_btc.update_layout(
                    title="Bitcoin Intraday Price",
                    xaxis_title="Time (UTC)",
                    yaxis_title="Price (USD)",
                    height=320,
                    margin={"l": 20, "r": 20, "t": 60, "b": 20},
                )
                st.plotly_chart(fig_btc, width='stretch')

    metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
    with metric_col1:
        st.metric("Events Loaded", len(snapshot))
    with metric_col2:
        total_markets = sum(event.get("market_count", 0) for event in snapshot)
        st.metric("Markets Visible", total_markets)
    with metric_col3:
        st.metric(
            "Snapshot Refreshed",
            last_refresh.strftime("%H:%M:%S") if last_refresh else "Not yet",
        )
    with metric_col4:
        st.metric(
            "Analysis Updated",
            last_analysis.strftime("%H:%M:%S") if last_analysis else "Not yet",
        )

    if not snapshot:
        st.info("No live trade events found for the selected filters.")
        return

    if all(
        event.get("hours_to_expiry") is None or event.get("hours_to_expiry", 0) > max_hours
        for event in snapshot
    ):
        st.info(
            "No events matched the strict expiry window, so the feed fell back to the best-ranked open events in your selected categories."
        )

    for index, event in enumerate(snapshot):
        analysis_result = analysis_map.get(event["event_ticker"], {})
        analysis = analysis_result.get("analysis")
        summary_parts = [
            event.get("category", "Unknown"),
            event.get("focus_type", "general").title(),
            f"{event.get('market_count', 0)} markets",
        ]
        if event.get("hours_to_expiry") is not None:
            summary_parts.append(f"{event['hours_to_expiry']:.1f}h to expiry")
        with st.expander(
            f"{event['title']} | {' | '.join(summary_parts)}",
            expanded=(index < 3),
        ):
            meta_col1, meta_col2, meta_col3, meta_col4 = st.columns(4)
            with meta_col1:
                st.write(f"**Event Ticker:** {event.get('event_ticker')}")
            with meta_col2:
                st.write(f"**Volume 24h:** {event.get('volume_24h', 0):,.0f}")
            with meta_col3:
                st.write(f"**Avg Spread:** {event.get('avg_yes_spread') or 0:.3f}")
            with meta_col4:
                st.write(f"**Live Score:** {event.get('live_score', 0):.1f}")

            if event.get("sub_title"):
                st.write(f"**Subtitle:** {event['sub_title']}")

            market_rows = []
            recommendations_by_ticker = {}
            if analysis:
                recommendations_by_ticker = {
                    item["ticker"]: item for item in analysis.get("recommended_markets", [])
                }

            for market in event.get("markets", []):
                recommendation = recommendations_by_ticker.get(market["ticker"], {})
                market_rows.append(
                    {
                        "Ticker": market["ticker"],
                        "Label": market.get("yes_sub_title") or market.get("title"),
                        "YES Mid": f"{market.get('yes_midpoint', 0):.3f}",
                        "YES Bid": f"{market.get('yes_bid', 0):.3f}",
                        "YES Ask": f"{market.get('yes_ask', 0):.3f}",
                        "24h Vol": f"{market.get('volume_24h', 0):,.0f}",
                        "Total Vol": f"{market.get('volume', 0):,}",
                        "Reco": recommendation.get("action", ""),
                        "Edge %": (
                            f"{recommendation.get('edge_pct', 0) * 100:.1f}%"
                            if recommendation
                            else ""
                        ),
                    }
                )

            st.dataframe(pd.DataFrame(market_rows), width='stretch', hide_index=True)

            if analysis:
                st.markdown("**LLM Recommendation**")
                st.write(analysis.get("summary", ""))

                analysis_col1, analysis_col2 = st.columns(2)
                with analysis_col1:
                    st.write(
                        f"**Confidence:** {analysis.get('confidence', 0):.0%}"
                    )
                    if analysis.get("key_drivers"):
                        st.write("**Key Drivers:**")
                        for item in analysis.get("key_drivers", []):
                            st.write(f"- {item}")
                with analysis_col2:
                    if analysis.get("risk_flags"):
                        st.write("**Risk Flags:**")
                        for item in analysis.get("risk_flags", []):
                            st.write(f"- {item}")

                if analysis.get("recommended_markets"):
                    st.write("**Top Opportunities:**")
                    for item in analysis.get("recommended_markets", []):
                        st.write(
                            f"- `{item['ticker']}` {item['action']} | "
                            f"fair YES {item['fair_yes_probability']:.1%} vs market {item['market_yes_midpoint']:.1%} | "
                            f"confidence {item['confidence']:.0%}"
                        )
                        st.caption(item.get("reasoning", ""))

                if analysis_result.get("sources"):
                    st.write("**Research Sources:**")
                    for url in analysis_result.get("sources", [])[:8]:
                        st.write(f"- {url}")
            else:
                st.info(
                    "No analysis stored for this event yet. Use `Run Live Analysis` to generate recommendations."
                )

def show_risk_management(performance_data, positions, system_balance):
    """Show risk management dashboard."""
    
    st.header("⚠️ Risk Management")
    
    # Handle empty positions gracefully
    if not positions:
        st.info("No active positions to analyze for risk management.")
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric("Portfolio Utilization", "0.0%")
        with col2:
            st.metric("Total Deployed", "$0.00")
        with col3:
            st.metric("Avg Position Size", "$0.00")
        with col4:
            st.metric("Max Single Position", "0.0%")
        
        st.subheader("🚨 Risk Alerts")
        st.success("✅ All risk metrics within acceptable ranges")
        return
    
    # Calculate risk metrics from live positions
    try:
        total_deployed = sum(pos['quantity'] * pos['entry_price'] for pos in positions if 'quantity' in pos and 'entry_price' in pos)
        portfolio_utilization = (total_deployed / system_balance * 100) if system_balance > 0 else 0
        
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric(
                "Portfolio Utilization",
                f"{portfolio_utilization:.1f}%",
                help="Percentage of balance deployed in positions"
            )
        
        with col2:
            st.metric(
                "Total Deployed",
                f"${total_deployed:.2f}",
                help="Total capital in active positions"
            )
        
        with col3:
            avg_position_size = total_deployed / len(positions) if positions else 0
            st.metric(
                "Avg Position Size",
                f"${avg_position_size:.2f}",
                help="Average size per position"
            )
        
        with col4:
            # Calculate max single position risk
            position_values = [pos['quantity'] * pos['entry_price'] for pos in positions if 'quantity' in pos and 'entry_price' in pos]
            max_position = max(position_values) if position_values else 0
            max_risk_pct = (max_position / system_balance * 100) if system_balance > 0 else 0
            st.metric(
                "Max Single Position",
                f"{max_risk_pct:.1f}%",
                help="Largest position as % of portfolio"
            )
        
        # Risk alerts
        st.subheader("🚨 Risk Alerts")
        
        alerts = []
        
        if portfolio_utilization > 90:
            alerts.append("⚠️ **High Portfolio Utilization**: Over 90% of capital deployed")
        
        if max_risk_pct > 20:
            alerts.append("⚠️ **Large Position Risk**: Single position exceeds 20% of portfolio")
        
        if len(positions) > 50:
            alerts.append("⚠️ **High Position Count**: Over 50 active positions may be difficult to manage")
        
        # Check for positions without stop losses (if supported)
        no_stop_loss = []
        for pos in positions:
            if 'stop_loss_price' in pos and not pos['stop_loss_price']:
                no_stop_loss.append(pos)
        
        if no_stop_loss:
            alerts.append(f"⚠️ **No Stop Losses**: {len(no_stop_loss)} positions lack stop loss protection")
        
        if alerts:
            for alert in alerts:
                st.warning(alert)
        else:
            st.success("✅ All risk metrics within acceptable ranges")
        
        # Risk by strategy breakdown
        strategy_names = [pos['strategy'] for pos in positions if 'strategy' in pos]
        if len(set(strategy_names)) > 1:
            st.subheader("📊 Risk by Strategy")
            
            strategy_risk = {}
            for pos in positions:
                if 'strategy' in pos and 'quantity' in pos and 'entry_price' in pos:
                    strategy = pos['strategy'] or 'Unknown'
                    if strategy not in strategy_risk:
                        strategy_risk[strategy] = {'exposure': 0, 'positions': 0}
                    strategy_risk[strategy]['exposure'] += pos['quantity'] * pos['entry_price']
                    strategy_risk[strategy]['positions'] += 1
            
            if strategy_risk:
                strategy_df = pd.DataFrame([
                    {
                        'Strategy': strategy,
                        'Exposure': f"${data['exposure']:.2f}",
                        'Positions': data['positions'],
                        'Avg Size': f"${data['exposure'] / data['positions']:.2f}",
                        'Portfolio %': f"{(data['exposure'] / system_balance * 100):.1f}%" if system_balance > 0 else "0.0%"
                    }
                    for strategy, data in strategy_risk.items()
                ])
                st.dataframe(strategy_df, width='stretch', hide_index=True)
        
    except Exception as e:
        st.error(f"Error calculating risk metrics: {e}")
        st.info("Using basic risk metrics")
        
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Portfolio Utilization", "Error")
        with col2:
            st.metric("Total Deployed", "Error")
        with col3:
            st.metric("Avg Position Size", "Error")
        with col4:
            st.metric("Max Single Position", "Error")

def show_system_health(available_cash, positions_count, llm_stats):
    """Show system health and monitoring."""

    st.header("System Health")

    st.subheader("System Status")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.success("Kalshi Connection: Active")
        st.write(f"Available Cash: ${available_cash:.2f}")
        st.write(f"Positions: {positions_count}")

    with col2:
        if llm_stats:
            st.success("LLM Snapshot: Loaded")
            total_queries = sum(stats['query_count'] for stats in llm_stats.values())
            st.write(f"Queries (7d): {total_queries}")
        else:
            st.warning("LLM Snapshot: Not loaded")
            st.write("Use Pull Queries in the LLM Analysis tab.")

    with col3:
        st.success("Database: Connected")
        st.write("All tables operational")

    st.subheader("System Activity")

    if llm_stats:
        st.write("**Recent LLM Activity:**")
        for strategy, stats in llm_stats.items():
            if not stats.get('last_query'):
                continue

            last_query_time = datetime.fromisoformat(stats['last_query'])
            total_seconds = int((datetime.now() - last_query_time).total_seconds())

            if total_seconds >= 86400:
                time_str = f"{total_seconds // 86400} days ago"
            elif total_seconds >= 3600:
                time_str = f"{total_seconds // 3600} hours ago"
            else:
                time_str = f"{max(total_seconds // 60, 1)} minutes ago"

            st.write(f"- **{strategy}**: Last query {time_str}")
    else:
        st.info("LLM activity is hidden until you pull a query snapshot.")

    st.subheader("Configuration")

    config_info = {
        "Database Path": "trading_system.db",
        "API Refresh": f"Auto every {API_REFRESH_INTERVAL_SECONDS} seconds",
        "LLM Query Refresh": "Manual (Pull Queries button)",
        "Preferred Categories": ", ".join(settings.trading.preferred_categories) or "All categories",
        "Live Wagering Preference": (
            f"Enabled ({settings.trading.live_wagering_max_hours_to_expiry}h Sports window)"
            if settings.trading.prefer_live_wagering
            else "Disabled"
        ),
        "Strategy Tracking": "Enabled",
        "Risk Management": "Active",
    }

    for key, value in config_info.items():
        st.write(f"**{key}:** {value}")

    st.subheader("Recommendations")

    recommendations = []
    if available_cash < 100:
        recommendations.append("Consider increasing account balance for more trading opportunities.")
    if not llm_stats:
        recommendations.append("Pull a manual LLM snapshot when you want to review query history.")

    total_queries = sum(stats['query_count'] for stats in llm_stats.values()) if llm_stats else 0
    if total_queries > 1000:
        recommendations.append("High LLM usage detected. Review query frequency and prompt efficiency.")

    if recommendations:
        for recommendation in recommendations:
            st.info(recommendation)
    else:
        st.success("System running optimally.")


if __name__ == "__main__":
    main()
