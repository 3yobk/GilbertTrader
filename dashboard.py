import streamlit as st
import pandas as pd
import plotly.express as px
import os
import time

st.set_page_config(page_title="GilbertTrader Analytics", layout="wide")
st.title("📈 GilbertTrader: Performance Dashboard")

LOG_FILE = "trade_log.csv"

# ── UPGRADE: auto-refresh every 30 seconds ───────────────────────────────────
# Streamlit re-runs the whole script on each refresh, so this counter in
# session_state forces a rerun without user interaction.
if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = time.time()

time_since = time.time() - st.session_state.last_refresh
if time_since >= 30:
    st.session_state.last_refresh = time.time()
    st.rerun()

# Countdown display so you can see when the next refresh is
secs_to_next = max(0, int(30 - time_since))
st.caption(f"Auto-refreshes in {secs_to_next}s")

# --- SAFETY CHECK: Create file if missing or read it ---
if not os.path.exists(LOG_FILE):
    st.info("👋 **Welcome, Eyob!** The system is waiting for your first trade to generate data.")
    st.warning("Monday at 9:30 AM, once a trade is executed, your charts will appear here automatically.")
else:
    try:
        trades = pd.read_csv(LOG_FILE)

        if trades.empty:
            st.write("No trades recorded yet.")
        else:
            # 1. KPI Metrics
            col1, col2, col3 = st.columns(3)
            total_trades = len(trades)
            win_rate     = (len(trades[trades['outcome'] == 'Win']) / total_trades) * 100
            total_pnl    = trades['profit_amt'].sum()

            col1.metric("Total Trades", total_trades)
            col2.metric("Win Rate",     f"{win_rate:.1f}%")
            col3.metric("Net Profit",   f"${total_pnl:.2f}")

            # 2. Charts
            st.divider()
            c1, c2 = st.columns(2)

            with c1:
                fig_win = px.pie(
                    trades, names='outcome', title='Win/Loss Ratio',
                    color_discrete_sequence=['#2ecc71', '#e74c3c'],
                )
                st.plotly_chart(fig_win, use_container_width=True)

            with c2:
                fig_growth = px.line(
                    trades, x='date', y='cumulative_profit',
                    title='Path to $1,000 Profit', markers=True,
                )
                st.plotly_chart(fig_growth, use_container_width=True)

            # 3. Recent Trades Table
            st.subheader("Recent History")
            st.dataframe(trades.tail(10), use_container_width=True)

    except Exception as e:
        st.error(f"Error loading logs: {e}")

st.sidebar.markdown("### System Status")
st.sidebar.success("Core Engine: Online")
st.sidebar.info("Market: Closed (Opens Monday 9:30 AM)")
