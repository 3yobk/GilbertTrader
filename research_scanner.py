import yfinance as yf
import pandas_ta as ta
import pandas as pd

# Use your historical small-cap favorites as the default list
MY_SMALL_STOCKS = ["AMC", "GME", "CVNA", "FUBO", "RBLX", "TLRY", "CHPT", "LCID", "GRTX", "MARA"]

def scan_markets(tickers=MY_SMALL_STOCKS, rsi_threshold=30):
    """
    Scans a list of tickers for oversold RSI levels based on a dynamic threshold.
    """
    signals = []
    for ticker in tickers:
        try:
            # Download 5-minute interval data
            df = yf.download(ticker, period="1d", interval="5m", progress=False)

            if df is None or df.empty or len(df) < 14:
                continue

            # Calculate RSI (Relative Strength Index)
            df['RSI'] = ta.rsi(df['Close'], length=14)

            # Safety check: Ensure RSI calculation succeeded
            if 'RSI' not in df or df['RSI'].empty:
                continue

            # Grab the latest RSI value
            val = df['RSI'].iloc[-1]

            # Skip if the value is NaN (common when markets are closed/low volume)
            if pd.isna(val):
                continue

            current_rsi = float(val)

            # Dashboard Feedback: See the logic in real-time
            # This helps you know the scanner is alive even if it doesn't buy
            color = "green" if current_rsi < rsi_threshold else "white"
            print(f"  [Scanner] {ticker.ljust(5)} | RSI: {current_rsi:.2f} (Target: <{rsi_threshold})")

            # Logic: Trigger signal based on the dynamically passed threshold
            if current_rsi < rsi_threshold:
                # Note: '100C' is a placeholder; your AI Risk Manager will refine the strike
                signals.append(f"BTO {ticker} 100C 6/19 (Scanner Alert: RSI is {current_rsi:.1f})")

        except Exception as e:
            print(f"Error scanning {ticker}: {e}")

    return signals