"""Aggregate walk-forward blocks: totals, exits, per-symbol, per-block table."""
import json, glob

fs = sorted(glob.glob("output/walkforward/block_*.json"))
t = w = 0
p = 0.0
sq = sl = tp = ts = 0
per = {}
print(f"{'block':>6} {'dates':<23} {'tr':>4} {'win%':>6} {'pnl':>10}")
print("-" * 55)
for f in fs:
    b = json.load(open(f))
    t += b["trades"]; w += b["wins"]; p += b["pnl"]
    for k, v in b.get("exit_reasons", {}).items():
        if k == "squareoff": sq += v
        elif k == "stop_hit": sl += v
        elif k == "target_hit": tp += v
        elif k == "time-stop": ts += v
    for s, v in b["per_symbol"].items():
        d = per.setdefault(s, {"t": 0, "w": 0, "p": 0.0})
        d["t"] += v["trades"]; d["w"] += v["wins"]; d["p"] += v["pnl"]
    d0, d1 = b["dates"][0], b["dates"][-1]
    wr = b["win_rate"]
    print(f"{b['block']:>6} {d0}->{d1} {b['trades']:>4} {wr:>5}% {b['pnl']:>10.2f}")

print("-" * 55)
print(f"TOTAL blocks={len(fs)} trades={t} wins={w} "
      f"win%={round(w/t*100,1) if t else 0} pnl={round(p,2):+.2f}")
print(f"EXITS: squareoff={sq} stop={sl} target={tp} time-stop={ts}")
print()
print(f"{'symbol':<14} {'tr':>4} {'win%':>6} {'pnl':>10}")
print("-" * 40)
for s, d in sorted(per.items(), key=lambda x: x[1]["p"], reverse=True):
    wr = round(d["w"]/d["t"]*100, 1) if d["t"] else 0.0
    print(f"{s:<14} {d['t']:>4} {wr:>5}% {d['p']:>10.2f}")
negs = [s for s, d in per.items() if d["p"] < 0]
print()
print(f"Negative names ({len(negs)}): {', '.join(sorted(negs))}")
