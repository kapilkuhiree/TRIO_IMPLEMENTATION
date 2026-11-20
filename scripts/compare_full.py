import yfinance as yf
import pandas as pd
import numpy as np

def calculate_rsi(data, window=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def run_comparative_backtest(symbol="^NSEI"):
    print(f"--- ANALYZING: {symbol} ---")
    df = yf.download(symbol, period="1mo", interval="5m", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    close = df['Close']
    df['SMA200'] = close.rolling(window=200).mean()
    df['RSI'] = calculate_rsi(close)

    # 1. New Strategy (Pullback/Mean Reversion)
    mr_trades = []
    for i in range(200, len(df) - 50):
        c, sma, rsi = float(df['Close'].iloc[i]), float(df['SMA200'].iloc[i]), float(df['RSI'].iloc[i])
        if c > sma and rsi < 45:
            entry, stop = c, float(df['Low'].iloc[i-5:i+1].min()) * 0.998
            target = entry + (entry - stop) * 1.5
            for j in range(i+1, min(i+50, len(df))):
                if float(df['Low'].iloc[j]) <= stop:
                    mr_trades.append(stop - entry); break
                if float(df['High'].iloc[j]) >= target:
                    mr_trades.append(target - entry); break

    # 2. Existing Strategy (ORB)
    orb_trades = []
    for day, group in df.groupby(df.index.date):
        if len(group) < 15: continue
        or_high = float(group.iloc[:3]['High'].max())
        or_low = float(group.iloc[:3]['Low'].min())
        for k in range(3, len(group)):
            c = float(group['Close'].iloc[k])
            if c > or_high:
                entry, stop, target = c, or_low, or_high + (or_high - or_low)
                for _, f_row in group.iloc[k+1:].iterrows():
                    if float(f_row['Low']) <= stop:
                        orb_trades.append(stop - entry); break
                    if float(f_row['High']) >= target:
                        orb_trades.append(target - entry); break
                break

    # Analysis
    print(f"NEW STRATEGY (Pullback): {len(mr_trades)} trades, Win%: {np.mean([x>0 for x in mr_trades])*100:.1f}%, PF: {abs(sum([x for x in mr_trades if x>0])/sum([x for x in mr_trades if x<0])):.2f}")
    print(f"EXISTING STRATEGY (ORB): {len(orb_trades)} trades, Win%: {np.mean([x>0 for x in orb_trades])*100:.1f}%, PF: {abs(sum([x for x in orb_trades if x>0])/sum([x for x in orb_trades if x<0])):.2f}")

if __name__ == "__main__":
    run_comparative_backtest("^NSEI")
    run_comparative_backtest("RELIANCE.NS")
    run_comparative_backtest("SBIN.NS")
    run_comparative_backtest("TCS.NS")
