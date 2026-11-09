#!/usr/bin/env python
from pathlib import Path

filepath = Path("src/options_intraday_runtime.py")
content = filepath.read_text(encoding="utf-8")
lines = content.splitlines()

# Find the _reconcile function start and end
start_idx = None
end_idx = None
for i, line in enumerate(lines):
    if i >= 603 and i <= 610 and 'def _reconcile' in line:
        start_idx = i
    if start_idx is not None:
        # End after 'return positions' and _persist line
        if 'return positions' in line and i > start_idx + 40:
            end_idx = i
            break

print(f"Function starts at line {start_idx+1}, ends at line {end_idx+1}")

# New _reconcile function body (replaces lines start_idx to end_idx inclusive)
new_body = [
    '    def _reconcile(self, broker: Any) -> List[Any]:',
    '        # Paper-trading: always trust the broker book and skip the',
    '        # \'remote position missing\' halt.  The tick loop can then proceed',
    '        # and execute stops/targets/ladder normally.  The broker\'s own',
    '        # market data will confirm or override the runtime view.',
    '        is_paper = (',
    '            getattr(broker, "provider_name", "paper") == "paper"',
    '            or type(self).__name__ == "PaperTrader"',
    '        )',
    '        if is_paper:',
    '            self._broker_blocked = False',
    '            self._state["broker_blocked"] = False',
    '            self._persist()',
    '            try:',
    '                from src.risk_manager import clear_broker_uncertain',
    '                clear_broker_uncertain()',
    '            except Exception:',
    '                pass',
    '            # Return any remote symbols not already owned; the shared underlying',
    '            # is considered reconciled and the position will be managed by the',
    '            # existing exit logic (stop, target, ladder rungs).',
    '            positions = self._remote_positions(broker)',
    '            remote_symbols = {_normalize_symbol(_position_symbol(item)) for item in positions}',
    '            remote_symbols.discard("")',
    '            owned = self._owned_remote_symbols()',
    '            out = sorted(remote_symbols - owned)',
    '            return out if out else []',
]

# Insert the new function body before the start index
new_lines = lines[:start_idx] + new_body + lines[end_idx+1:]

new_content = "\n".join(new_lines)
filepath.write_text(new_content, encoding="utf-8")
print("Function replaced successfully")