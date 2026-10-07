"""
TRIO — Paper Trading Dashboard (local web app, no login, no platform).
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

What this is
------------
A small local website that shows your paper account the way a broker app
would: balance, open positions with live P&L, trade history, and the
signals behind every decision. Runs on YOUR machine at
http://127.0.0.1:5000 — nothing leaves your laptop, no account needed.

Why this exists
---------------
The user asked: "I want to SEE a 10000 balance go up and down as the
strategy trades." The terminal prints that information but it scrolls
away. This dashboard keeps it on screen and refreshes by itself.

How to run
----------
    python scripts/dashboard.py [--symbols RELIANCE.NS TCS.NS ...]
                                [--timeframe 15m] [--capital 10000]
                                [--interval 300] [--port 5000]

Then open http://127.0.0.1:5000 in a browser.

Design notes
------------
- Single file on purpose: the dashboard is a VIEWER, not a new trading
  system. All trading logic stays in src/ (PaperTrader, PaperBroker).
- A background thread runs PaperTrader.scan_once() every `interval`
  seconds. The web page polls /api/state every 10s and re-renders.
- State file: output/dashboard_state.json — the page and the loop share
  it, so a browser refresh never loses history.
- Educational only. Not financial advice. No real orders, ever.
"""

import argparse
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, render_template_string  # noqa: E402

from src.paper_trader import PaperTrader  # noqa: E402
from src.utils import get_logger, load_config, load_env  # noqa: E402

load_env()  # .env must load before any alert/credential read

logger = get_logger("dashboard")

app = Flask(__name__)

STATE_FILE = ROOT / "output" / "dashboard_state.json"

_ctx: Dict[str, Any] = {
    "capital_start": 500000.0,
    "provider": "paper",
    "trader": None,
    "symbols": [],
    "timeframe": "15m",
    "interval": 300,
    "scans": 0,
    "last_scan": None,
    "last_error": None,
    "lock": threading.Lock(),
}


# ---------------------------------------------------------------------------
# Trading loop (background thread)
# ---------------------------------------------------------------------------

def loop_forever() -> None:
    trader: PaperTrader = _ctx["trader"]
    while True:
        try:
            with _ctx["lock"]:
                signals = trader.scan_once()
                _ctx["scans"] += 1
                _ctx["last_scan"] = datetime.now(
                    timezone.utc).isoformat()
                _ctx["last_error"] = None
                _save_state(signals)
        except Exception as exc:  # keep the loop alive no matter what
            with _ctx["lock"]:
                _ctx["last_error"] = f"{type(exc).__name__}: {exc}"
        time.sleep(_ctx["interval"])


def join_poller_forever() -> None:
    """Poll Telegram for new /start joiners every ~30s; welcome them.

    Fail-closed: poller exceptions log and retry. Never touches trading.
    """
    while True:
        try:
            from src.alerts import handle_joins
            handle_joins()
        except Exception as exc:
            logger.warning("Join poller failed: %s", exc)
        time.sleep(30)


def _save_state(signals: List[Any]) -> None:
    trader: PaperTrader = _ctx["trader"]
    bal = trader.broker.get_balance()
    positions = [p.to_dict() for p in trader.broker.get_positions()]
    orders = [o.to_dict() for o in trader.broker.orders.values()]
    closed = list(trader.broker.closed_trades)[-100:]
    n_closed = len(closed)
    n_pass = sum(1 for t in closed if t.get("result") == "PASS")

    equity = bal["total"]
    pnl = equity - _ctx["capital_start"]
    pnl_pct = pnl / _ctx["capital_start"] * 100 if _ctx["capital_start"] else 0

    state = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scans": _ctx["scans"],
        "last_scan": _ctx["last_scan"],
        "last_error": _ctx["last_error"],
        "symbols": _ctx["symbols"],
        "timeframe": _ctx["timeframe"],
        "interval": _ctx["interval"],
        "capital_start": _ctx["capital_start"],
        "balance": bal,
        "provider": _ctx.get("provider", "paper"),
        "equity": round(equity, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
    "positions": positions,
    "orders": orders[-50:],
    "closed": closed,
    "closed_pass": n_pass,
    "closed_fail": n_closed - n_pass,
    "win_rate": round(n_pass / n_closed * 100, 1) if n_closed else 0.0,
    "signals": [s.to_dict() for s in signals][-20:],
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str),
                          encoding="utf-8")


_startup_announced = False  # once-per-boot guard: no double pings


def _send_startup_announcement() -> bool:
    """Verify the boot actually works, then announce it on Telegram.

    Readiness gate (all must pass for a clean STARTED AND WORKING):
      1. broker balance fetch returns a positive total,
      2. the scan loop thread is alive,
      3. Telegram credentials are present.
    Any failure still sends, but as STARTED WITH ISSUES naming the
    problem. Everything is fail-closed: an exception here logs and
    returns False — the trading loop is never blocked by an announcement.
    Broadcasts to all subscribers (same path as trade alerts).
    """
    global _startup_announced
    if _startup_announced:
        return False
    _startup_announced = True
    try:
        from src.alerts import format_startup_message, send_telegram
        from src.utils import utc_now

        trader: PaperTrader = _ctx["trader"]
        issues: list = []
        try:
            bal = trader.broker.get_balance()
            equity = float(bal.get("total", 0) or 0)
            if equity <= 0:
                issues.append(
                    f"broker balance unreadable (total={bal.get('total')})")
        except Exception as exc:
            equity = 0.0
            issues.append(f"broker balance fetch failed: {exc}")
        try:
            n_positions = len(trader.broker.get_positions())
        except Exception as exc:
            n_positions = 0
            issues.append(f"position read failed: {exc}")
        alive = any(getattr(th, "name", "") == "scan-loop"
                    and th.is_alive() for th in threading.enumerate())
        if not alive:
            issues.append("scan loop thread not running")
        cfg = load_config()
        ft = cfg.get("forward_test", {})
        session = (f"{ft.get('session_start', '09:20')}"
                   f"–{ft.get('session_end', '15:15')} IST")
        msg = format_startup_message({
            "booted_at": utc_now(),
            "provider": _ctx.get("provider", "paper"),
            "equity": equity,
            "positions": n_positions,
            "symbols": len(_ctx.get("symbols", [])),
            "interval": _ctx.get("interval", 300),
            "session": session,
            "capital_start": _ctx.get("capital_start", 0),
            "issues": issues,
        })
        sent = send_telegram(msg)
        logger.info("Startup announcement sent=%s issues=%s", sent, issues)
        return bool(sent)
    except Exception as exc:
        logger.warning("Startup announcement failed: %s", exc)
        return False


def _load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            # Telegram "run on phone" path: expose the last alert payload so
            # /api/state doubles as a pollable signal feed (no login, no
            # websocket needed — any phone browser or Tasker/HTTP-Shortcuts
            # poll works).
            alert_file = STATE_FILE.parent / "last_alert.json"
            if alert_file.exists():
                try:
                    state["last_alert"] = json.loads(
                        alert_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return state
        except Exception:
            pass
    return {"status": "starting — first scan in progress…"}


# ---------------------------------------------------------------------------
# Web routes
# ---------------------------------------------------------------------------

@app.route("/")
def index() -> str:
    return render_template_string(PAGE)


@app.route("/ticket")
def ticket_page() -> str:
    """Phone-friendly order-ticket view: biggest text, one signal, checklist."""
    return render_template_string(TICKET_PAGE)


TICKET_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TRIO Ticket</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, Segoe UI, Roboto, sans-serif; margin: 0;
         background: #0b0e14; color: #e6e6e6; padding: 20px; }
  .t { font-size: 15px; color: #888; }
  .sym { font-size: 34px; font-weight: 800; margin: 4px 0; }
  .act { font-size: 44px; font-weight: 800; margin: 8px 0; }
  .buy { color: #4ade80; } .sell { color: #f87171; } .hold { color: #9ca3af; }
  .row { display: flex; justify-content: space-between; font-size: 22px;
         padding: 8px 0; border-bottom: 1px solid #1c2230; }
  .row b { font-variant-numeric: tabular-nums; }
  ol { font-size: 17px; line-height: 1.7; color: #c9c9c9; }
  #when { color: #666; font-size: 13px; margin-top: 16px; }
  .none { font-size: 24px; color: #888; margin-top: 40px; text-align: center; }
</style>
</head>
<body>
<div class="t">TRIO order ticket (paper signal — tap it into Zerodha yourself)</div>
<div id="body"><div class="none">loading…</div></div>
<div id="when"></div>
<script>
async function tick(){
  try {
    const r = await fetch('/api/alert'); const j = await r.json();
    if (!j.signal) { document.getElementById('body').innerHTML =
      '<div class="none">no signals yet</div>'; return; }
    const s = j.signal, t = j.zerodha_ticket || {};
    const cls = s.action === 'BUY' ? 'buy' : (s.action === 'SELL' ? 'sell' : 'hold');
    document.getElementById('body').innerHTML =
      `<div class="sym">${s.symbol || ''}</div>` +
      `<div class="act ${cls}">${s.action || 'HOLD'}</div>` +
      `<div class="row"><span>Entry</span><b>${s.entry_price ?? '—'}</b></div>` +
      `<div class="row"><span>Stop</span><b>${s.stop_loss ?? '—'}</b></div>` +
      `<div class="row"><span>Target</span><b>${s.target ?? '—'}</b></div>` +
      `<div class="row"><span>Qty</span><b>${s.position_size ?? '—'}</b></div>` +
      `<div class="row"><span>Rank</span><b>${s.rank ?? '—'} (${s.setup_name || ''})</b></div>` +
      `<div class="row"><span>Confidence</span><b>${s.confidence ?? '—'}%</b></div>` +
      `<ol>${(t.checklist || []).map(c => `<li>${c}</li>`).join('')}</ol>`;
    document.getElementById('when').textContent = 'updated ' + (j.updated_at || '');
  } catch(e) { /* keep last rendered */ }
}
tick(); setInterval(tick, 15000);
</script>
</body>
</html>
"""


@app.route("/api/state")
def api_state() -> Any:
    return jsonify(_load_state())


@app.route("/api/alert")
def api_alert() -> Any:
    """Latest actionable signal as a phone-friendly payload.

    Poll this from a phone browser, Tasker, or HTTP Shortcuts:
      http://<your-laptop-ip>:5000/api/alert
    Returns the most recent BUY/SELL candidate (or HOLD state) with
    entry/stop/target/size/rank, plus a ready-to-paste Zerodha order ticket.
    """
    state = _load_state()
    sigs = state.get("signals", []) or []
    actionable = [s for s in sigs if s.get("action") in ("BUY", "SELL")]
    sig = actionable[-1] if actionable else (sigs[-1] if sigs else None)
    if not sig:
        return jsonify({"status": "no signals yet — first scan in progress"})
    ticket = _order_ticket(sig)
    return jsonify({"signal": sig, "zerodha_ticket": ticket,
                    "updated_at": state.get("updated_at")})


def _order_ticket(sig: Dict[str, Any]) -> Dict[str, Any]:
    """Translate one engine signal into a Zerodha order-ticket checklist.

    Paper trades CNC-style; the ticket mirrors that so what you tap on
    the phone matches what the bot simulated. Nothing here places orders.
    """
    action = sig.get("action", "HOLD")
    return {
        "symbol": sig.get("symbol"),
        "transaction_type": action,          # BUY or SELL
        "product": "CNC",                    # delivery; MIS only if you choose it
        "order_type": "LIMIT",
        "price": sig.get("entry_price"),
        "quantity": sig.get("position_size"),
        "stop_loss": sig.get("stop_loss"),
        "target": sig.get("target"),
        "rank": sig.get("rank"),
        "setup": sig.get("setup_name"),
        "confidence": sig.get("confidence"),
        "checklist": [
            "1. Product = CNC (MIS only for intraday short setups)",
            "2. LIMIT price = entry shown",
            "3. Quantity = position_size shown (never more)",
            "4. Place SL-M stop-loss order at stop shown",
            "5. SELL without holdings will reject — expected, not an error",
        ],
    }


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TRIO Paper Trading — ₹<span id="eq">…</span></title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, Segoe UI, Roboto, sans-serif; margin: 0;
         background: #0b0e14; color: #e6e6e6; }
  header { padding: 16px 24px; border-bottom: 1px solid #222; display: flex;
           gap: 24px; align-items: baseline; flex-wrap: wrap; }
  h1 { font-size: 18px; margin: 0; }
  .kpi { display: flex; gap: 28px; padding: 16px 24px; flex-wrap: wrap; }
  .card { background: #12161f; border: 1px solid #222; border-radius: 10px;
          padding: 12px 18px; min-width: 150px; }
  .card .lbl { font-size: 11px; color: #888; text-transform: uppercase; }
  .card .val { font-size: 22px; font-weight: 700; }
  .pos { color: #4ade80; } .neg { color: #f87171; } .flat { color: #e6e6e6; }
  section { padding: 8px 24px 24px; }
  h2 { font-size: 14px; color: #aaa; text-transform: uppercase; letter-spacing: .06em; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid #1c2230; }
  th { color: #888; font-weight: 600; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; }
  .buy { background: #14532d; color: #bbf7d0; }
  .sell { background: #7f1d1d; color: #fecaca; }
  .hold { background: #1f2937; color: #9ca3af; }
  #meta { color: #777; font-size: 12px; padding: 0 24px 24px; }
  .warn { background: #3b2f0b; border: 1px solid #92600a; color: #fcd34d;
          padding: 10px 14px; border-radius: 8px; margin: 12px 24px 0; font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>TRIO — Paper Trading (simulated, no real money)</h1>
  <span id="meta-line">connecting…</span>
</header>
<div id="err"></div>
<div class="kpi">
  <div class="card"><div class="lbl">Equity</div><div class="val" id="equity">…</div></div>
  <div class="card"><div class="lbl">Day P&amp;L</div><div class="val" id="pnl">…</div></div>
  <div class="card"><div class="lbl">Available</div><div class="val" id="avail">…</div></div>
  <div class="card"><div class="lbl">Open positions</div><div class="val" id="npos">…</div></div>
  <div class="card"><div class="lbl">Scans</div><div class="val" id="scans">…</div></div>
  <div class="card"><div class="lbl">Win rate</div><div class="val" id="winrate">…</div></div>
</div>
<section>
  <h2>Open positions</h2>
  <table><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Avg</th>
  <th>Last</th><th>P&amp;L</th></tr></thead><tbody id="pos"></tbody></table>
</section>
<section>
  <h2>Latest signals (ranked — only these traded)</h2>
  <table><thead><tr><th>Rank</th><th>Symbol</th><th>Action</th><th>Entry</th><th>Stop</th>
  <th>Target</th><th>Conf</th><th>Setup</th><th>Why (first reason)</th></tr></thead>
  <tbody id="sig"></tbody></table>
</section>
<section>
  <h2>Closed trades — PASS / FAIL scoreboard</h2>
  <table><thead><tr><th>Result</th><th>Symbol</th><th>Side</th><th>Qty</th>
  <th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Why closed</th></tr></thead>
  <tbody id="closed"></tbody></table>
</section>
<section>
  <h2>Recent orders</h2>
  <table><thead><tr><th>ID</th><th>Symbol</th><th>Side</th><th>Qty</th>
  <th>Price</th><th>Status</th></tr></thead><tbody id="ord"></tbody></table>
</section>
<div id="meta"></div>
<script>
const $ = id => document.getElementById(id);
function money(x){ return (x<0?'−₹':'₹') + Math.abs(x).toLocaleString('en-IN',{maximumFractionDigits:2}); }
function cls(x){ return x>0?'pos':(x<0?'neg':'flat'); }
async function tick(){
  try {
    const r = await fetch('/api/state'); const s = await r.json();
    if (!s.equity && s.status) { $('meta-line').textContent = s.status; return; }
    document.title = 'TRIO Paper — ' + money(s.pnl);
    $('equity').textContent = money(s.equity); $('equity').className = 'val '+cls(s.pnl);
    $('pnl').textContent = (s.pnl>=0?'+':'') + money(s.pnl) + ' (' + s.pnl_pct + '%)';
    $('pnl').className = 'val ' + cls(s.pnl);
    $('avail').textContent = money(s.balance.available);
  $('npos').textContent = s.positions.length;
  $('scans').textContent = s.scans;
  const wr = (s.closed_pass + s.closed_fail) > 0
    ? s.win_rate + '% (' + s.closed_pass + ' PASS / ' + s.closed_fail + ' FAIL)' : 'no closed trades yet';
  $('winrate').textContent = wr;
  $('winrate').className = 'val ' + (s.win_rate >= 50 ? 'pos' : (s.win_rate > 0 ? 'neg' : 'flat'));
    $('meta-line').textContent = s.symbols.join(', ') + ' · ' + s.timeframe +
      ' · every ' + s.interval + 's';
    $('pos').innerHTML = s.positions.length ? s.positions.map(p =>
      `<tr><td>${p.symbol}</td><td>${p.side}</td><td>${p.quantity}</td>` +
      `<td>${p.avg_price}</td><td>${p.current_price}</td>` +
      `<td class="${cls(p.pnl)}">${money(p.pnl)} (${(p.pnl_pct||0).toFixed(2)}%)</td></tr>`
    ).join('') : '<tr><td colspan="6" style="color:#666">flat — no open positions</td></tr>';
    $('sig').innerHTML = s.signals.slice().reverse().map(g => {
      const a = (g.action||'HOLD');
      const pill = a==='BUY'?'buy':(a==='SELL'?'sell':'hold');
      const why = (g.reasoning && g.reasoning[0]) || '';
      const skip = g.skipped ? ` <span class="pill hold">SKIPPED: ${g.skipped}</span>` : '';
      return `<tr><td><b>${g.rank ?? '—'}</b></td><td>${g.symbol}</td>` +
        `<td><span class="pill ${pill}">${a}</span>${skip}</td>` +
        `<td>${g.entry_price ?? '—'}</td><td>${g.stop_loss ?? '—'}</td>` +
        `<td>${g.target ?? '—'}</td><td>${g.confidence}%</td>` +
        `<td style="color:#7dd3fc">${g.setup_name || ''}</td>` +
        `<td style="color:#999">${why}</td></tr>`;
    }).join('');
  $('ord').innerHTML = s.orders.slice().reverse().slice(0,15).map(o =>
    `<tr><td style="color:#666">${String(o.order_id).slice(-8)}</td>` +
    `<td>${o.symbol}</td><td>${o.side}</td><td>${o.quantity}</td>` +
    `<td>${o.price}</td><td>${o.status}</td></tr>`
  ).join('') || '<tr><td colspan="6" style="color:#666">no orders yet</td></tr>';
  $('closed').innerHTML = (s.closed || []).slice().reverse().map(t => {
    const pass = t.result === 'PASS';
    return `<tr><td><span class="pill ${pass ? 'buy' : 'sell'}">${t.result}</span></td>` +
      `<td>${t.symbol}</td><td>${t.side}</td><td>${t.quantity}</td>` +
      `<td>${t.entry}</td><td>${t.exit}</td>` +
      `<td class="${cls(t.pnl)}">${money(t.pnl)} (${t.pnl_pct}%)</td>` +
      `<td style="color:#999">${t.reason}</td></tr>`;
  }).join('') || '<tr><td colspan="8" style="color:#666">no closed trades yet — open a position and close it to score it</td></tr>';
    $('meta').textContent = 'updated ' + s.updated_at +
      (s.last_error ? ' · last error: ' + s.last_error : '');
    $('err').innerHTML = s.last_error
      ? `<div class="warn">Last scan error: ${s.last_error} (loop kept running)</div>` : '';
  } catch(e){ $('meta-line').textContent = 'waiting for server…'; }
}
tick(); setInterval(tick, 10000);
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="TRIO paper trading dashboard")
    ap.add_argument("--symbols", nargs="+", default=None)
    ap.add_argument("--timeframe", default="15m")
    ap.add_argument("--capital", type=float, default=None)
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()

    cfg = load_config()
    symbols = args.symbols or cfg.get("symbols", ["RELIANCE.NS"])
    # Default capital follows the risk config (Rs5L with the MegaBull
    # provider), so sizing, exposure and the dashboard always agree.
    start_capital = args.capital if args.capital else \
        (cfg.get("risk_management", {}) or {}).get("capital", 10000.0)

    _ctx["capital_start"] = start_capital
    _ctx["symbols"] = symbols
    _ctx["timeframe"] = args.timeframe
    _ctx["interval"] = args.interval
    _ctx["trader"] = PaperTrader(
        symbols=symbols,
        timeframe=args.timeframe,
        initial_capital=start_capital,
    )

    # Anchor the baseline to the broker's own ledger (MegaBull's
    # virtualMoney when remote). A --capital override still wins, but a
    # stale hardcoded default can never silently shrink the book again.
    try:
        anchor = _ctx["trader"].broker.get_balance().get("total")
        if anchor and (args.capital is None):
            _ctx["capital_start"] = anchor
            start_capital = anchor
    except Exception as exc:
        logger.warning("Could not anchor capital_start: %s", exc)
    _ctx["provider"] = getattr(_ctx["trader"], "broker_name", "paper")

    _ctx["provider"] = getattr(_ctx["trader"], "broker_name", "paper")
    t = threading.Thread(target=loop_forever, daemon=True, name="scan-loop")
    t.start()

    j = threading.Thread(target=join_poller_forever, daemon=True,
                         name="join-poller")
    j.start()

    _send_startup_announcement()
    print("=" * 64)
    print("  TRIO paper dashboard: http://127.0.0.1:%d" % args.port)
    print("  Symbols: %s | %s every %ds | capital %.2f (SIMULATED) | broker: %s" % (
        ", ".join(symbols), args.timeframe, args.interval, start_capital,
        _ctx["provider"]))
    print("  No login. No platform. No real money. Ctrl+C to stop.")
    print("=" * 64)
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()