import yfinance as yf
import pandas as pd
import numpy as np

def calculate_rsi(data, window=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def run_backtest_yfinance(symbol="^NSEI"):
    print(f"Fetching data for {symbol}...")
    df = yf.download(symbol, period="1mo", interval="5m", progress=False)
    if df.empty:
        print("No data found.")
        return

    # Handle MultiIndex columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Indicators
    close = df['Close']
    df['SMA200'] = close.rolling(window=200).mean()
    df['EMA50'] = close.ewm(span=50, adjust=False).mean()
    df['RSI'] = calculate_rsi(close)

    trades = []

    for i in range(200, len(df) - 20):
        row = df.iloc[i]
        c = float(row['Close'])
        sma200 = float(row['SMA200'])
        rsi = float(row['RSI'])

        # LOGIC: Pullback in Uptrend
        # 1. Price above 200 SMA (Bullish Trend)
        # 2. RSI < 45 (Oversold / Pullback)
        if c > sma200 and rsi < 45:
            entry_price = c
            stop_loss = float(df.iloc[i-5:i+1]['Low'].min()) * 0.998
            risk = entry_price - stop_loss
            if risk <= 0:
                continue
            target = entry_price + risk * 1.5

            hit_stop = False
            hit_target = False
            exit_price = entry_price

            for j in range(i + 1, min(i + 50, len(df))):
                future_row = df.iloc[j]
                f_low = float(future_row['Low'])
                f_high = float(future_row['High'])

                if f_low <= stop_loss:
                    hit_stop = True
                    exit_price = stop_loss
                    break
                if f_high >= target:
                    hit_target = True
                    exit_price = target
                    break

            if not hit_stop and not hit_target:
                exit_price = float(df['Close'].iloc[min(i + 50, len(df)-1)])

            pnl = exit_price - entry_price
            trades.append({
                'entry': entry_price,
                'exit': exit_price,
                'pnl': pnl,
                'win': pnl > 0
            })

    df_trades = pd.DataFrame(trades)
    if df_trades.empty:
        print("No trades triggered.")
        return

    win_rate = df_trades['win'].mean() * 100
    pos_pnl = df_trades[df_trades['pnl'] > 0]['pnl'].sum()
    neg_pnl = abs(df_trades[df_trades['pnl'] < 0]['pnl'].sum())
    profit_factor = (pos_pnl / neg_pnl) if neg_pnl > 0 else float('inf')

    print(f"\n--- Backtest Results for {symbol} (5m YFinance) ---")
    print(f"Total Trades: {len(df_trades)}")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Profit Factor: {profit_factor:.2f}")
    print(f"Average Win: {df_trades[df_trades['pnl'] > 0]['pnl'].mean():.2f}")
    print(f"Average Loss: {df_trades[df_trades['pnl'] < 0]['pnl'].mean():.2f}")
    print("------------------------------------------")

if __name__ == "__main__":
    run_backtest_yfinance("^NSEI")
    run_backtest_yfinance("RELIANCE.NS")
