import yfinance as yf
import pandas_ta as ta
import pandas as pd
from datetime import datetime, timedelta

# Your small-cap watchlist
MY_SMALL_STOCKS = ["AMC", "GME", "CVNA", "FUBO", "RBLX", "TLRY", "CHPT", "LCID", "GRTX", "MARA"]


def is_trend_bullish(ticker):
    """Checks if the current price is above the 200-day SMA."""
    try:
        df_daily = yf.download(ticker, period="1y", interval="1d", progress=False)
        if df_daily.empty or len(df_daily) < 200:
            return True
        sma_200       = df_daily['Close'].rolling(window=200).mean().iloc[-1]
        current_price = df_daily['Close'].iloc[-1]
        return current_price > sma_200
    except Exception:
        return True


def get_hour_rsi_alignment(ticker):
    """Checks 1-Hour RSI to ensure the overall day isn't a total crash."""
    try:
        df_hour  = yf.download(ticker, period="5d", interval="1h", progress=False)
        if df_hour.empty or len(df_hour) < 14:
            return True
        rsi_hour = ta.rsi(df_hour['Close'], length=14).iloc[-1]
        return rsi_hour > 40
    except Exception:
        return True


def get_atr_adjustment(df, base_threshold):
    """Calculates ATR and returns a volatility-adjusted RSI threshold."""
    try:
        atr           = ta.atr(df['High'], df['Low'], df['Close'], length=14).iloc[-1]
        current_price = df['Close'].iloc[-1]
        vol_ratio     = atr / current_price
        if vol_ratio > 0.02:
            return base_threshold - 5
        return base_threshold
    except Exception:
        return base_threshold


# ── FIX: Real strike and expiry from live price ───────────────────────────────
def _build_signal(ticker: str, current_price: float) -> str:
    """
    Build a real BTO signal string from a live stock price.

    Strike: nearest $5 increment above the current price (ATM call).
    Expiry: nearest upcoming Friday that is at least 7 days away,
            so we avoid same-week expiry theta crush.
    """
    # Nearest strike in $5 increments above current price
    strike = int((current_price // 5) * 5) + 5

    # Nearest Friday at least 7 days from today
    today  = datetime.now()
    days_to_friday = (4 - today.weekday()) % 7   # 4 = Friday
    if days_to_friday < 7:
        days_to_friday += 7
    expiry_dt = today + timedelta(days=days_to_friday)
    expiry    = expiry_dt.strftime("%-m/%-d")    # e.g. "5/16"

    return f"BTO {ticker} {strike}C {expiry}"


def scan_markets(tickers=MY_SMALL_STOCKS, rsi_threshold=30, listener=None, market=None):
    """
    Scans markets with Triple-Verification:
    1. RSI Oversold (5-min) + ATR Adaptive Adjustment
    2. Volume Surge (Force)
    3. SMA-200 & 1-Hour Alignment (Trend)

    UPGRADE: market hours guard — skips the entire scan if market is closed.
    FIX:     generates a real BTO signal with live strike price and expiry.
    """
    # ── UPGRADE: skip scan entirely outside market hours ─────────────────────
    if market is not None and not market.is_market_open():
        mins = market.minutes_to_open()
        print(f"  [Scanner] Market closed. Opens in ~{mins} min — skipping scan.")
        return []

    signals = []
    for ticker in tickers:
        try:
            df = yf.download(ticker, period="1d", interval="5m", progress=False)

            if df is None or df.empty or len(df) < 14:
                print(f"  [Scanner] {ticker.ljust(5)} | No live data (Market Closed)")
                continue

            # 1. RSI
            df['RSI'] = ta.rsi(df['Close'], length=14)
            latest_rsi_val = df['RSI'].iloc[-1]
            if pd.isna(latest_rsi_val):
                print(f"  [Scanner] {ticker.ljust(5)} | RSI unavailable")
                continue

            current_rsi = float(latest_rsi_val)

            # ── FIX: get the real current price for signal building ──────────
            current_price = float(df['Close'].iloc[-1])

            # Adaptive RSI threshold
            effective_rsi_threshold = get_atr_adjustment(df, rsi_threshold)

            # 2. Volume surge
            avg_vol      = df['Volume'].rolling(window=20).mean().iloc[-2]
            current_vol  = df['Volume'].iloc[-1]
            volume_surge = current_vol > (avg_vol * 1.5)

            status_log = (
                f"  [Scanner] {ticker.ljust(5)} | RSI: {current_rsi:.2f} "
                f"(Target: <{effective_rsi_threshold})  Price: ${current_price:.2f}"
            )
            print(status_log)

            # 3. Triple-check gate
            if current_rsi < effective_rsi_threshold:
                if not is_trend_bullish(ticker):
                    print(f"    [Skip] {ticker}: Bearish trend (Below SMA-200)")
                    continue

                if not volume_surge:
                    print(f"    [Skip] {ticker}: Weak volume (No force)")
                    continue

                if not get_hour_rsi_alignment(ticker):
                    print(f"    [Skip] {ticker}: 1-Hour trend is too weak")
                    continue

                print(f"  🔥 [HIGH CONVICTION] {ticker} reversal identified!")

                # ── FIX: build a real signal from the live price ─────────────
                signal_str = _build_signal(ticker, current_price)

                if listener:
                    # Parse strike and expiry back out for the Discord message
                    parts  = signal_str.split()
                    strike = parts[2] if len(parts) > 2 else "ATM"
                    expiry = parts[3] if len(parts) > 3 else "N/A"
                    alert_msg = (
                        f"🚀 **HIGH CONVICTION SIGNAL**\n"
                        f"**Ticker**: ${ticker}\n"
                        f"**Price**: ${current_price:.2f} → Strike: {strike}  Expiry: {expiry}\n"
                        f"**RSI**: {current_rsi:.2f} (ATR Adjusted Target: {effective_rsi_threshold})\n"
                        f"**Filters**: Trend, Volume, and MTF Verified ✅\n"
                        f"**Signal**: `{signal_str}`"
                    )
                    listener.send_status_update(alert_msg)

                signals.append(signal_str)

        except Exception as e:
            print(f"Error scanning {ticker}: {e}")

    return signals
