# High-win-rate research: can a 1-trade-per-day rule hit 80–90%?

**Verdict: not with long premium. The win rate is a dial you can set, but every
80–90% setting I tested is at or below its own break-even after costs.**

Script: `scripts/research_high_winrate.py`
Data: `output/research/nifty_5m_90d.csv` — Zerodha NIFTY 5m, IST,
**61 sessions** (2026-08-14 → 2026-11-11), 4,514 bars.

## 1. Why 80–90% is a geometry choice

At a fixed stop, a target of `r × risk` breaks even at `1/(1+r)`:

| target | break-even win rate |
| --- | --- |
| 0.10R | 90.9% |
| 0.15R | 87.0% |
| 0.25R | 80.0% |
| 0.50R | 66.7% |
| 1.00R | 50.0% |

So an 85% win rate does not mean "better strategy"; it means "small target,
wide stop". The question is only whether expectancy survives costs.

## 2. Tested rules (one trade per day, first qualifying bar)

| Rule | Entry |
| --- | --- |
| `orb_break` | first 2-close break of the 09:15–09:30 range |
| `vwap_fade` | fade a 1.5-ATR stretch from session VWAP after 11:00 |
| `trend_pullback` | resting limit at EMA20 while the prior bar's trend stack is bullish |
| `gap_fade` | fade a ≥0.25% opening gap at 09:20 |
| `range_fade` | fade a fresh extreme of a compressed day, 12:00–14:00 |

Method: decision on bar close → **fill at next open**; limit entries fill inside
the bar but can only produce a stop on that bar, never a target; a bar touching
both stop and target counts as a **loss**; time exit 15:10. Cost is charged in R
(default 0.10R, sensitivity table in the script).

## 3. Results

Selected rows (net = after 0.10R cost):

| Rule | Target | Win rate | Break-even | Net R | Verdict |
| --- | --- | --- | --- | --- | --- |
| orb_break | 0.10R | 87.9% | 90.9% | **−7.70** | high win rate, negative |
| orb_break | 0.15R | 84.5% | 87.0% | **−7.45** | high win rate, negative |
| orb_break | 0.20R | 82.8% | 83.3% | **−6.20** | dead heat before cost |
| orb_break | 1.00R | 53.4% | 50.0% | −1.80 | positive gross (+4.00R), cost kills it |
| trend_pullback | 0.25R | 76.7% | 80.0% | **−4.25** | below break-even |
| trend_pullback | 2.00R | 30.0% | 33.3% | −2.46 | only positive gross (+0.54R) |
| vwap_fade | any | 19–79% | — | −13 to −30 | no edge at any geometry |
| gap_fade | any | 27–67% | — | −9 to −13 | no edge at any geometry |
| range_fade | 0.20R | 85.7% | 83.3% | −0.50 | 7 trades only, noise |

**Nothing clears break-even.** The 80–90% rows are exactly the "small target,
wide stop" trap: the win rate is real, the edge is not.

## 4. The control that settles it

Long-only random entries, 10:00–13:00, same stop and target, 20 seeds:

| Target | Random win rate | Rule win rate | Break-even |
| --- | --- | --- | --- |
| 0.10R | 79.7% | 83–88% | 90.9% |
| 0.20R | 74.5% | 80–83% | 83.3% |
| 0.25R | 72.4% | 72–77% | 80.0% |
| 1.00R | 43.8% | 47–53% | 50.0% |

A coin-flip entry already wins ~72–80% at these geometries. The rules add a few
points of timing, not the 20+ points needed.

## 5. Statistical honesty

| Estimate | 95% CI | Break-even | Clears? |
| --- | --- | --- | --- |
| orb_break 0.10R: 87.9% (n=58) | 77.1–94.0% | 90.9% | **No** |
| orb_break 0.20R: 82.8% (n=58) | 71.1–90.4% | 83.3% | **No** |
| trend_pullback 0.25R: 76.7% (n=30) | 59.1–88.2% | 80.0% | **No** |

Separating 85% from 80% at 95% confidence needs **~196 trades**. At one trade
per day that is ~0.8 years of evidence. Any single-session 85% claim is noise.

## 6. What would actually produce 80–90% *with* positive expectancy

**Selling premium** — a defined-risk credit spread (bull put / bear call),
1.5–2% out of the money, 1–2 DTE:

- win rate is structurally high (70–90%) because most spreads expire worthless;
- expectancy comes from IV/theta, not from pattern timing;
- the trade-off is inverted R:R — you win the small credit often and lose the
  large width occasionally, plus gap/tail risk.

This is a different risk profile from the current long-premium book. It needs
spread margin, two-leg execution, and a new risk cap (the existing
`max_premium_exposure_pct` model does not describe short legs). It is buildable
on `open_spread`, but it should be shadow-tested for 20–30 sessions before it
touches remote paper.

## 7. Recommendation

1. **Do not build an intraday long-premium strategy aimed at 80–90%.** The
   database says it loses to costs at exactly that geometry.
2. **Keep ORB at 1–2R** (50–55% win rate). That is the correct shape for a
   buyer: pay for the occasional large winner.
3. **If 80–90% is a hard requirement**, the only honest path is a short-premium
   spread sleeve, shadow-first. That is a new strategy class, not a tweak.

## 8. Caveats

- 61 sessions, one regime (Aug–Nov 2026), index-level simulation.
- Costs are modelled in R; at 0.20R (realistic for a ₹1–2 spread plus fees on an
  ATM option) every row above gets worse.
- No option-level fills, slippage or IV crush; a spread sleeve must be measured
  on archived quotes, not index points.
- Absence of evidence here is not proof of impossibility — but the burden of
  proof sits with any 80–90% claim, and 61 sessions cannot supply it.
