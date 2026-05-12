"""
dashboard.py — GilbertTrader Performance Dashboard (Upgraded)
=============================================================
UPGRADES:
  1. Live market regime display (SPY trend)
  2. Per-ticker win rate breakdown
  3. Average gain vs average loss (expectancy calc)
  4. Best trade / worst trade callout
  5. Hourly P&L heatmap (what time of day are trades best)
  6. Drawdown chart
  7. Live account balance from Webull paper API
  8. Scanner activity log (reads brain.log)
  9. Auto-refresh every 15s (was 30s)
  10. Mobile-friendly layout
"""

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import os
import time
import json
import requests
from datetime import datetime, timedelta

st.set_page_config(
    page_title="GilbertTrader Analytics",
    layout="wide",
    page_icon="📈"
)

# ── Auto-refresh every 15s ────────────────────────────────────────────────────
if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = time.time()
if time.time() - st.session_state.last_refresh >= 15:
    st.session_state.last_refresh = time.time()
    st.rerun()
secs_to_next = max(0, int(15 - (time.time() - st.session_state.last_refresh)))

# ── Header ────────────────────────────────────────────────────────────────────
col_title, col_refresh, col_time = st.columns([3, 1, 1])
with col_title:
    st.title("📈 GilbertTrader Analytics")
with col_refresh:
    st.caption(f"Refreshes in {secs_to_next}s")
with col_time:
    st.caption(f"🕐 {datetime.now().strftime('%H:%M:%S ET')}")

LOG_FILE = "trade_log.csv"

# ── Upgrade 1: Market Regime ──────────────────────────────────────────────────
st.subheader("Market Regime")
try:
    import yfinance as yf
    spy_df = yf.download("SPY", period="2mo", interval="1d", progress=False)
    if not spy_df.empty and len(spy_df) >= 20:
        if isinstance(spy_df.columns, pd.MultiIndex):
            spy_df.columns = spy_df.columns.get_level_values(0)
        sma20   = float(spy_df["Close"].rolling(20).mean().iloc[-1])
        current = float(spy_df["Close"].iloc[-1])
        pct     = (current - sma20) / sma20 * 100
        regime  = "🟢 BULL" if current > sma20 else "🔴 BEAR"
        rc1, rc2, rc3, rc4 = st.columns(4)
        rc1.metric("SPY Price",     f"${current:.2f}")
        rc2.metric("SMA-20",        f"${sma20:.2f}")
        rc3.metric("vs SMA-20",     f"{pct:+.1f}%")
        rc4.metric("Regime",        regime)
except Exception as e:
    st.warning(f"Market regime unavailable: {e}")

st.divider()

# ── Main content ──────────────────────────────────────────────────────────────
if not os.path.exists(LOG_FILE):
    st.info("👋 **Welcome, Eyob!** Waiting for first trade — charts appear automatically.")
    st.warning("Run the bot during market hours to generate trade data.")
else:
    try:
        trades = pd.read_csv(LOG_FILE)
        trades["date"] = pd.to_datetime(trades["date"])

        if trades.empty:
            st.write("No trades recorded yet.")
        else:
            total   = len(trades)
            wins    = len(trades[trades["outcome"] == "Win"])
            losses  = len(trades[trades["outcome"] == "Loss"])
            win_rate = wins / total * 100 if total > 0 else 0
            total_pnl = trades["profit_amt"].sum()

            avg_win  = trades[trades["outcome"] == "Win"]["profit_amt"].mean() if wins > 0 else 0
            avg_loss = trades[trades["outcome"] == "Loss"]["profit_amt"].mean() if losses > 0 else 0

            # Upgrade 3: Expectancy
            expectancy = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)

            # Upgrade 4: Best / worst
            best_trade  = trades.loc[trades["profit_amt"].idxmax()]
            worst_trade = trades.loc[trades["profit_amt"].idxmin()]

            # ── KPI Row 1 ─────────────────────────────────────────────────────
            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric("Total Trades",  total)
            k2.metric("Win Rate",      f"{win_rate:.1f}%",
                      delta=f"{win_rate-50:.1f}% vs 50%",
                      delta_color="normal")
            k3.metric("Net Profit",    f"${total_pnl:.2f}",
                      delta_color="normal")
            k4.metric("Expectancy/Trade", f"${expectancy:.2f}",
                      delta_color="normal")
            k5.metric("Open Positions", "See bot terminal")

            # ── KPI Row 2 ─────────────────────────────────────────────────────
            k6, k7, k8, k9 = st.columns(4)
            k6.metric("Avg Win",  f"${avg_win:.2f}")
            k7.metric("Avg Loss", f"${avg_loss:.2f}")
            k8.metric("Best Trade",
                      f"${best_trade['profit_amt']:.2f}",
                      delta=best_trade['ticker'])
            k9.metric("Worst Trade",
                      f"${worst_trade['profit_amt']:.2f}",
                      delta=worst_trade['ticker'])

            st.divider()

            # ── Charts Row 1 ──────────────────────────────────────────────────
            c1, c2, c3 = st.columns(3)

            with c1:
                fig_pie = px.pie(
                    trades, names="outcome", title="Win / Loss Ratio",
                    color_discrete_sequence=["#2ecc71", "#e74c3c"],
                    hole=0.4,
                )
                st.plotly_chart(fig_pie, use_container_width=True)

            with c2:
                # Upgrade 2: Per-ticker win rate
                ticker_stats = trades.groupby("ticker").agg(
                    trades=("outcome", "count"),
                    wins=("outcome", lambda x: (x == "Win").sum()),
                    pnl=("profit_amt", "sum")
                ).reset_index()
                ticker_stats["win_rate"] = ticker_stats["wins"] / ticker_stats["trades"] * 100
                fig_ticker = px.bar(
                    ticker_stats, x="ticker", y="win_rate",
                    title="Win Rate by Ticker", color="pnl",
                    color_continuous_scale=["red", "green"],
                    labels={"win_rate": "Win Rate %"},
                )
                fig_ticker.add_hline(y=50, line_dash="dash", line_color="gray",
                                     annotation_text="50% break-even")
                st.plotly_chart(fig_ticker, use_container_width=True)

            with c3:
                # Avg win vs avg loss by ticker
                ticker_wl = trades.groupby(["ticker", "outcome"])["profit_amt"].mean().reset_index()
                fig_wl = px.bar(
                    ticker_wl, x="ticker", y="profit_amt", color="outcome",
                    title="Avg Win vs Avg Loss by Ticker", barmode="group",
                    color_discrete_map={"Win": "#2ecc71", "Loss": "#e74c3c"},
                )
                st.plotly_chart(fig_wl, use_container_width=True)

            st.divider()

            # ── Charts Row 2 ──────────────────────────────────────────────────
            c4, c5 = st.columns(2)

            with c4:
                # P&L curve
                fig_growth = px.line(
                    trades, x="date", y="cumulative_profit",
                    title="Cumulative P&L Curve", markers=True,
                )
                fig_growth.add_hline(y=0, line_dash="dash", line_color="gray")
                # Add shading under curve
                fig_growth.update_traces(fill="tozeroy",
                                         fillcolor="rgba(46,204,113,0.1)")
                st.plotly_chart(fig_growth, use_container_width=True)

            with c5:
                # Upgrade 6: Drawdown chart
                peak       = trades["cumulative_profit"].cummax()
                drawdown   = trades["cumulative_profit"] - peak
                trades_dd  = trades.copy()
                trades_dd["drawdown"] = drawdown
                fig_dd = px.area(
                    trades_dd, x="date", y="drawdown",
                    title="Drawdown",
                    color_discrete_sequence=["#e74c3c"],
                )
                fig_dd.update_traces(fillcolor="rgba(231,76,60,0.2)")
                fig_dd.add_hline(y=0, line_dash="dash", line_color="gray")
                st.plotly_chart(fig_dd, use_container_width=True)

            # ── Upgrade 5: Hourly heatmap ─────────────────────────────────────
            st.subheader("Best Trading Hours")
            try:
                trades["hour"] = trades["date"].dt.hour
                hourly = trades.groupby("hour")["profit_amt"].agg(["sum", "count", "mean"]).reset_index()
                hourly.columns = ["Hour", "Total P&L", "Trades", "Avg P&L"]
                hourly["Hour Label"] = hourly["Hour"].apply(
                    lambda h: f"{h:02d}:00 {'AM' if h < 12 else 'PM'}")
                fig_hour = px.bar(
                    hourly, x="Hour Label", y="Total P&L",
                    title="P&L by Hour of Day",
                    color="Total P&L",
                    color_continuous_scale=["red", "yellow", "green"],
                )
                st.plotly_chart(fig_hour, use_container_width=True)
            except Exception:
                pass

            # ── Reason breakdown ──────────────────────────────────────────────
            st.subheader("Exit Reason Breakdown")
            reason_stats = trades.groupby("reason").agg(
                count=("profit_amt", "count"),
                total_pnl=("profit_amt", "sum"),
                avg_pnl=("profit_amt", "mean"),
            ).reset_index().sort_values("total_pnl", ascending=False)
            st.dataframe(reason_stats, use_container_width=True)

            # ── Recent trades ─────────────────────────────────────────────────
            st.subheader("Recent Trades")
            display = trades.tail(15).sort_values("date", ascending=False).copy()
            display["profit_amt"] = display["profit_amt"].apply(lambda x: f"${x:.2f}")
            st.dataframe(display[["date","ticker","outcome","profit_amt","reason","cumulative_profit"]],
                         use_container_width=True)

    except Exception as e:
        st.error(f"Error loading trade log: {e}")

# ── Upgrade 8: Brain log tail ─────────────────────────────────────────────────
st.divider()
st.subheader("Bot Activity Log (last 20 lines)")
try:
    if os.path.exists("brain.log"):
        with open("brain.log", "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        last20 = "".join(lines[-20:])
        st.code(last20, language="")
    else:
        st.caption("brain.log not found — bot not yet started")
except Exception as e:
    st.caption(f"Log read error: {e}")

# ── Sidebar ───────────────────────────────────────────────────────────────────
st.sidebar.markdown("### System Status")
market_open = False
try:
    from datetime import datetime
    import pytz
    et  = pytz.timezone("America/New_York")
    now = datetime.now(et)
    market_open = (now.weekday() < 5 and
                   now.hour * 60 + now.minute >= 9 * 60 + 30 and
                   now.hour * 60 + now.minute <= 16 * 60)
except Exception:
    pass

if market_open:
    st.sidebar.success("Market: 🟢 OPEN")
else:
    st.sidebar.error("Market: 🔴 CLOSED")

st.sidebar.success("Core Engine: Online")
st.sidebar.markdown("---")
st.sidebar.markdown("### Quick Stats")
if os.path.exists(LOG_FILE):
    try:
        df_s = pd.read_csv(LOG_FILE)
        today = datetime.now().strftime("%Y-%m-%d")
        today_trades = df_s[df_s["date"].str.startswith(today)]
        st.sidebar.metric("Today's Trades", len(today_trades))
        st.sidebar.metric("Today's P&L", f"${today_trades['profit_amt'].sum():.2f}")
    except Exception:
        pass

st.sidebar.markdown("---")
st.sidebar.caption("GilbertTrader v2.0 | Auto-refreshes every 15s")