import json

# Check trade log for options events
print("=== CHECKING TRADE LOG FOR OPTIONS EVENTS ===")
with open('output/trade_log.jsonl') as f:
    lines = f.readlines()
options_events = []
for l in lines:
    try:
        ev = json.loads(l)
        if ev.get('asset') == 'options':
            options_events.append(ev)
    except:
        pass

print(f"Total JSONL lines: {len(lines)}")
print(f"Options events found: {len(options_events)}")

if options_events:
    print("\nOptions events details:")
    for ev in options_events:
        print(f"  ts={ev.get('ts')}")
        print(f"  event={ev.get('event')}")
        print(f"  venue={ev.get('venue')}")
        print(f"  strategy={ev.get('strategy')}")
        print(f"  pnl={ev.get('pnl')}")
        print(f"  broker={ev.get('broker')}")
else:
    print("No options events in trade log")

# Check alerts config
print("\n=== CHECKING ALERTS CONFIG ===")
try:
    with open('config/config.yaml') as f:
        content = f.read()
    # Check for options alert config
    if 'options' in content.lower() and 'alert' in content.lower():
        print("Options alert configuration found in config")
        # Print relevant lines
        for i, line in enumerate(content.split('\n'), 1):
            if 'option' in line.lower() or 'alert' in line.lower():
                print(f"  Line {i}: {line.strip()}")
    else:
        print("No options alert configuration found")
except FileNotFoundError:
    print("config/config.yaml not found")

# Check subscribers_options.json
import os
if os.path.exists('output/subscribers_options.json'):
    print("\n=== subscribers_options.json EXISTS ===")
    with open('output/subscribers_options.json') as f:
        content = f.read()
    print(f"Content: {content[:200]}")
else:
    print("\n=== subscribers_options.json NOT FOUND ===")

# Check general subscribers.json
if os.path.exists('output/subscribers.json'):
    print("\n=== subscribers.json EXISTS ===")
    with open('output/subscribers.json') as f:
        content = f.read()
    print(f"Content: {content[:200]}")
else:
    print("\n=== subscribers.json NOT FOUND ===")