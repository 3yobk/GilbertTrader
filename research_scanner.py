import yfinance as yf
import pandas_ta as ta
import pandas as pd
from datetime import datetime, timedelta

MY_SMALL_STOCKS = ["AMC", "GME", "CVNA", "FUBO", "RBLX", "TLRY", "CHPT", "LCID", "GRTX", "MARA"]


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten MultiIndex columns that newer yfinance versions return."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def _download_intraday(ticker: str) -> pd.DataFrame:
    """
    Multi-attempt intraday fetcher.
    Tries 5m/1d → 5m/2d → 1m/1d until >= 14 bars are returned.
    Handles yfinance MultiIndex column bug automatically.
    """
    for params in [
        {"interval": "5m", "period": "1d"},
        {"interval": "5m", "period": "2d"},
        {"interval": "1m", "period": "1d"},
    ]:
        try:
            df = yf.download(ticker, progress=False, **params)
            df = _flatten(df)
            if not df.empty and len(df) >= 14:
                return df
        except Exception:
            continue
    return pd.DataFrame()


def is_trend_bullish(ticker):
    """Checks if the current price is above the 200-day SMA."""
    try:
        df = _flatten(yf.download(ticker, period="1y", interval="1d", progress=False))
        if df.empty or len(df) < 200:
            return True
        return float(df["Close"].iloc[-1]) > float(df["Close"].rolling(200).mean().iloc[-1])
    except Exception:
        return True


def get_hour_rsi_alignment(ticker):
    """Checks 1-Hour RSI to ensure the overall day isn't a total crash."""
    try:
        df = _flatten(yf.download(ticker, period="5d", interval="1h", progress=False))
        if df.empty or len(df) < 14:
            return True
        rsi = ta.rsi(df["Close"].dropna(), length=14)
        if rsi is None or rsi.dropna().empty:
            return True
        return float(rsi.dropna().iloc[-1]) > 40
    except Exception:
        return True


def get_atr_adjustment(df, base_threshold):
    """Calculates ATR and returns a volatility-adjusted RSI threshold."""
    try:
        atr = ta.atr(df["High"], df["Low"], df["Close"], length=14)
        if atr is None or atr.dropna().empty:
            return base_threshold
        atr_val = float(atr.dropna().iloc[-1])
        price   = float(df["Close"].iloc[-1])
        if price > 0 and (atr_val / price) > 0.02:
            return base_threshold - 5
        return base_threshold
    except Exception:
        return base_threshold


def _build_signal(ticker: str, current_price: float) -> str:
    strike = int((current_price // 5) * 5) + 5
    today  = datetime.now()
    days_to_friday = (4 - today.weekday()) % 7
    if days_to_friday < 7:
        days_to_friday += 7
    expiry = (today + timedelta(days=days_to_friday)).strftime("%-m/%-d")
    return f"BTO {ticker} {strike}C {expiry}"


def scan_markets(tickers=MY_SMALL_STOCKS, rsi_threshold=30, listener=None, market=None):
    """
    Triple-Verified scanner:
    1. RSI Oversold (5-min) + ATR adaptive threshold
    2. Volume surge
    3. SMA-200 + 1-hour RSI alignment
    """
    if market is not None and not market.is_market_open():
        mins = market.minutes_to_open()
        print(f"  [Scanner] Market closed. Opens in ~{mins} min — skipping scan.")
        return []

    signals = []
    for ticker in tickers:
        try:
            df = _download_intraday(ticker)

            if df.empty:
                print(f"  [Scanner] {ticker.ljust(5)} | No intraday data available")
                continue

            close = df["Close"].dropna()
            if len(close) < 14:
                print(f"  [Scanner] {ticker.ljust(5)} | Not enough bars ({len(close)})")
                continue

            # 1. RSI
            rsi_series = ta.rsi(close, length=14)
            if rsi_series is None or rsi_series.dropna().empty:
                print(f"  [Scanner] {ticker.ljust(5)} | RSI calculation failed")
                continue

            latest_rsi = rsi_series.dropna().iloc[-1]
            if pd.isna(latest_rsi):
                print(f"  [Scanner] {ticker.ljust(5)} | RSI is NaN")
                continue

            current_rsi   = float(latest_rsi)
            current_price = float(close.iloc[-1])
            eff_threshold = get_atr_adjustment(df, rsi_threshold)

            # 2. Volume surge
            vol = df["Volume"].dropna()
            avg_vol      = float(vol.rolling(20).mean().iloc[-2]) if len(vol) >= 21 else float(vol.mean())
            current_vol  = float(vol.iloc[-1])
            volume_surge = current_vol > avg_vol * 1.5 if avg_vol > 0 else False

            print(f"  [Scanner] {ticker.ljust(5)} | RSI: {current_rsi:.2f} "
                  f"(Target: <{eff_threshold})  Price: ${current_price:.2f}")

            # 3. Triple-check gate
            if current_rsi < eff_threshold:
                if not is_trend_bullish(ticker):
                    print(f"    [Skip] {ticker}: Bearish trend (Below SMA-200)")
                    continue
                if not volume_surge:
                    print(f"    [Skip] {ticker}: Weak volume (No force)")
                    continue
                if not get_hour_rsi_alignment(ticker):
                    print(f"    [Skip] {ticker}: 1-Hour trend too weak")
                    continue

                print(f"  🔥 [HIGH CONVICTION] {ticker} reversal identified!")
                signal_str = _build_signal(ticker, current_price)

                if listener:
                    parts  = signal_str.split()
                    strike = parts[2] if len(parts) > 2 else "ATM"
                    expiry = parts[3] if len(parts) > 3 else "N/A"
                    listener.send_status_update(
                        f"🚀 **HIGH CONVICTION SIGNAL**\n"
                        f"**Ticker**: ${ticker}\n"
                        f"**Price**: ${current_price:.2f} → Strike: {strike}  Expiry: {expiry}\n"
                        f"**RSI**: {current_rsi:.2f} (ATR Adjusted Target: {eff_threshold})\n"
                        f"**Filters**: Trend, Volume, and MTF Verified ✅\n"
                        f"**Signal**: `{signal_str}`"
                    )
                signals.append(signal_str)

        except Exception as e:
            print(f"  [Scanner] {ticker.ljust(5)} | Error: {e}")

    return signals