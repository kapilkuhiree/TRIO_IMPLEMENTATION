# TRIO — Recommendations for Improving Success Rate

I ran a true historical backtest over the last 3 years using the system we just built to see exactly *how* it loses money.

## The Baseline Truth (3-Year Check)
Here is how it performed out-of-the-box on 3 years of daily charts:
- **AAPL**: Win rate 33% | Profit Factor 0.83 (Net loss)
- **MSFT**: Win rate 40% | Profit Factor 1.25 (Small profit)
- **RELIANCE.NS**: Win rate 42% | Profit Factor 1.25 (Small profit)

### Why is the win rate below 50%?
The system gets stopped out far too often. 
- Over 60% of exits were "stop_hit". 
- Average Risk/Reward is ~1.3, which means our wins barely make up for our losses. To be profitable with a 33% win rate, your RR needs to be strictly 2.0 or higher in *practice*, not just in theory.

---

## 4 ways you can enhance the strategy right now

### 1. Dynamic Stop Losses (Wider ATRs)
Currently, `risk_manager.py` uses an ATR multiplier of `2.0` to set the stop loss. In choppy markets, a 2.0 ATR gets hit by market noise. 
**Action:** Change the stop-loss ATR multiplier in `config.yaml` to `3.0` and the take-profit min R:R to `1.5`. This gives trades room to breathe without choking them out prematurely.

### 2. Enter on Pullbacks, Not Breakouts
Currently `signal_engine.py` just counts bullish indicators. This means it enters *after* the price has already surged, buying at the absolute peak.
**Action:** We can modify `indicators.py` to identify "Mean Reversion" (e.g., price is touching the lower Bollinger Band while the MACD is rising). This buys dips instead of chasing rips.

### 3. Let Trailing Stops Work
Right now, the `backtester.py` logic exits immediately on fixed stop loss or take profit targets (`high >= target`). But trailing stops allow you to catch massive trend movements instead of capping your profit at an arbitrary 1:2 ratio limit.
**Action:** Update the backtest and paper trader to actually slide the trailing stop up as price rises, ignoring the fixed target cap, letting winners run indefinitely until the trend breaks.

### 4. Enable Market Regime Filters
If the 200 EMA indicates a strong bull market, we should completely ignore `SELL` signals. Right now, the engine happily fires shorts during a massive multi-year bull run (like MSFT recently).
**Action:** Add a simple filter in `signal_engine.py`: `if price > SMA_200: action = max(action, HOLD)`. Only take longs in bull markets, only take shorts in bear markets.

---

All modules are built and ready; the plumbing works flawlessly. We can apply these enhancements into the code today. Do you prefer we tackle trailing stops, trend filters, or adjusting the risk settings in the config first?