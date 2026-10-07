"""Aggregate ONLY blocks 15-25 (one consistent 60-day calendar).

Blocks 1-14 were computed on an older 36-session calendar; blocks 15-25 on
the newer 49-session calendar. Mixing them double-counts overlapping dates.
This script reports the clean set: blocks 15-25 (~Aug 26 -> Oct 05).
"""
import json, glob

fs = [f"output/walkforward/block_{i:02d}.json" for i in range(15, 26)]
t = w = 0
p = 0.0
sq = sl = tp = 0
per = {}
for f in fs:
    b = json.load(open(f))
    t += b["trades"]; w += b["wins"]; p += b["pnl"]
    for k, v in b.get("exit_reasons", {}).items():
        if k == "squareoff": sq += v
        elif k == "stop_hit": sl += v
        elif k == "target_hit": tp += v
    for s, v in b["per_symbol"].items():
        d = per.setdefault(s, {"t": 0, "w": 0, "p": 0.0})
        d["t"] += v["trades"]; d["w"] += v["wins"]; d["p"] += v["pnl"]

print(f"BLOCKS 15-25 (one calendar, ~Aug 26 -> Oct 05): "
      f"trades={t} wins={w} win%={round(w/t*100,1)} pnl={round(p,2):+.2f}")
print(f"EXITS: squareoff={sq} stop={sl} target={tp}")
print()
print(f"{'symbol':<14} {'tr':>4} {'win%':>6} {'pnl':>10}")
print("-" * 40)
for s, d in sorted(per.items(), key=lambda x: x[1]["p"], reverse=True):
    wr = round(d["w"]/d["t"]*100, 1) if d["t"] else 0.0
    print(f"{s:<14} {d['t']:>4} {wr:>5}% {d['p']:>10.2f}")
negs = [s for s, d in per.items() if d["p"] < 0]
print()
print(f"Negative names ({len(negs)}): {', '.join(sorted(negs))}")
print(f"Blocks 1-14 EXCLUDED (stale calendar - would double-count).")
