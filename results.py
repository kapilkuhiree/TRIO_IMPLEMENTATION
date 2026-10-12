import json
import csv
import os
from collections import Counter

print("=== SESSION RESULTS ===")
print()

# Check JSONL log
jsonlog = "output/trade_log.jsonl"
try:
    with open(jsonlog) as f:
        lines = f.readlines()
    print(f"JSONL total events: {len(lines)}")
    
    # Count by asset
    assets = {}
    for l in lines:
        try:
            ev = json.loads(l)
            a = ev.get('asset', 'NONE')
            assets[a] = assets.get(a, 0) + 1
        except:
            pass
    print(f"Asset distribution: {assets}")
    
    # Options events
    options = [json.loads(l) for l in lines if json.loads(l).get('asset')=='options']
    if options:
        print(f"\nOptions events: {len(options)}")
        venues = [o.get('venue') for o in options]
        venue_counts = dict(Counter(venues))
        print(f"Venues: {venue_counts}")
        for o in options:
            print(f"  {o.get('event')}: premium={o.get('premium')} pnl={o.get('pnl')} venue={o.get('venue')} strategy={o.get('strategy')}")
    else:
        print("\nNo options events in JSONL")
        
except FileNotFoundError:
    print('No JSONL log found')

# Check CSV log
csvlog = "output/trade_log.csv"
try:
    with open(csvlog) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    print(f"\nCSV total events: {len(rows)}")
    
    assets = {}
    for r in rows:
        a = r.get('asset', 'NONE')
        assets[a] = assets.get(a, 0) + 1
    print(f"CSV asset distribution: {assets}")
    
    options_rows = [r for r in rows if r.get('asset')=='options']
    if options_rows:
        print(f"\nCSV Options events: {len(options_rows)}")
        for r in options_rows:
            print(f"  {r.get('event')}: symbol={r.get('symbol')} pnl={r.get('pnl')} broker={r.get('broker')}")
    else:
        print("\nNo options events in CSV")
        
except FileNotFoundError:
    print('No CSV log found')

# Check plan
planfile = "output/plans/2026-10-12.json"
if os.path.exists(planfile):
    import json as j
    with open(planfile) as f:
        plan = j.load(f)
    print(f"\nPlan candidates: {len(plan.get('candidates', []))}")
else:
    print("\nNo plan file found")

print("\n=== ANALYSIS COMPLETE ===")