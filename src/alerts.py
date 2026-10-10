"""
TRIO — Alerts
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Send trading signal notifications via Telegram or email.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import json
import logging
import os
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils import get_env, get_logger, ist_display, load_config

logger = get_logger("alerts")


# ---------------------------------------------------------------------------
# Markdown escaping (Telegram legacy Markdown)
# ---------------------------------------------------------------------------

_MD_SPECIAL = set(r"_*[]()~`>#+-=|{}.!")


def _md_escape(text: Any) -> str:
    """Escape Telegram legacy-Markdown special chars in dynamic text.

    Static markers (e.g. *TRIO BUY —*) are written by us and stay raw;
    everything interpolated (symbols, prices, reasoning lines, setup
    names) passes through here so a line like
    ``8/15 technical indicators bullish (tech_score=0.40)`` cannot break
    parsing with unbalanced underscores (that was a live 400 Bad Request).
    """
    s = "" if text is None else str(text)
    return "".join(f"\\{ch}" if ch in _MD_SPECIAL else ch for ch in s)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _send_telegram_kind(message: str, kind: str,
                        config_override: Optional[Dict[str, Any]] = None) -> bool:
    """Internal: send via kind=equity|options. Separate token/chat pair."""
    if os.environ.get("TRIO_TEST_MODE") == "1" and config_override is None:
        logger.debug("Telegram suppressed in test mode")
        return False
    if config_override is not None:
        tg_cfg = config_override
    elif kind == "options":
        tg_cfg = (_resolve_bot("options")[0] and _tg_cfg("options")[0]) or \
                 load_config().get("alerts", {}).get("telegram", {})
    else:
        tg_cfg = load_config().get("alerts", {}).get("telegram", {})

    if not tg_cfg.get("enabled", False):
        logger.debug("Telegram alerts disabled")
        return False

    if config_override is not None:
        token = get_env(tg_cfg.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
        chat_id = get_env(tg_cfg.get("chat_id_env", "TELEGRAM_CHAT_ID"))
    else:
        token, chat_id, _ = _resolve_bot(kind)

    if not token or not chat_id:
        logger.warning("Telegram credentials not set (kind=%s)", kind)
        return False

    recipients = _subscriber_ids(chat_id, kind=kind)
    if not recipients:
        logger.warning("Telegram has no recipients (kind=%s)", kind)
        return False

    try:
        import requests  # noqa: E402
    except ImportError:
        logger.error("requests not installed — cannot send Telegram alerts")
        return False

    ok_any = False
    for cid in recipients:
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            resp = requests.post(url, json={
                "chat_id": cid, "text": message, "parse_mode": "Markdown",
            }, timeout=10)
            resp.raise_for_status()
            _record_delivery(cid, ok=True, kind=kind)
            ok_any = True
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.error("Telegram send failed for %s: %s", cid, exc)
            _record_delivery(cid, ok=False, blocked=(status == 403), kind=kind)
    if ok_any:
        logger.info("Telegram alert sent to %d recipient(s) (kind=%s)",
                    len(recipients), kind)
    return ok_any


def send_telegram(message: str, config_override: Optional[Dict[str, Any]] = None) -> bool:
    """Send via the OLD intraday/equity bot."""
    return _send_telegram_kind(message, kind="equity",
                               config_override=config_override)


def send_options_telegram(message: str,
                          config_override: Optional[Dict[str, Any]] = None) -> bool:
    """Send via the NEW options bot (8877508167). Falls back to old when unset."""
    return _send_telegram_kind(message, kind="options",
                               config_override=config_override)


# ---------------------------------------------------------------------------
# Legacy resolves: old bot (default channel) vs new options bot
# ---------------------------------------------------------------------------

def _tg_cfg(kind: str = "equity") -> tuple[Dict[str, Any], bool]:
    """Return (tg_cfg, is_options). kind=equity|options."""
    cfg = load_config()
    tg = (cfg.get("alerts", {}) or {}).get("telegram", {}) or {}
    if kind == "options":
        opt = tg.get("options") if isinstance(tg.get("options"), dict) else None
        # fallback to top-level telegram until the user finishes secrets setup
        opt_cfg = (opt or tg)
        return (opt_cfg, bool(opt))
    return (tg, False)


def _resolve_bot(kind: str) -> tuple[str, str, Path]:
    """Return (token, chat_id, subscriber_file) for kind=equity|options."""
    is_opt = (kind == "options")
    cfg = load_config()
    tg = (cfg.get("alerts", {}) or {}).get("telegram", {}) or {}
    if is_opt:
        opt = tg.get("options") if isinstance(tg.get("options"), dict) else {}
        token_env = (opt.get("bot_token_env") or "TELEGRAM_OPTIONS_BOT_TOKEN")
        chat_env = (opt.get("chat_id_env") or "TELEGRAM_OPTIONS_CHAT_ID")
        sub_file = (opt.get("subscriber_file") or "output/subscribers_options.json")
        token = get_env(token_env) or ""
        chat_id = get_env(chat_env) or ""
        # Fallback to the old bot when the new one is not configured yet
        if not token:
            token = get_env(tg.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
            chat_id = get_env(tg.get("chat_id_env", "TELEGRAM_CHAT_ID"))
            sub_file = tg.get("subscriber_file", "output/subscribers.json")
    else:
        token = get_env(tg.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
        chat_id = get_env(tg.get("chat_id_env", "TELEGRAM_CHAT_ID"))
        sub_file = tg.get("subscriber_file", "output/subscribers.json")
    path = Path(sub_file)
    if not path.is_absolute():
        from src.utils import PROJECT_ROOT
        path = PROJECT_ROOT / sub_file
    return (token, chat_id, path)


# ---------------------------------------------------------------------------
# Subscriber store + join handling (open broadcast)
# ---------------------------------------------------------------------------

def _subscriber_file(kind: str = "equity") -> Path:
    """Path to the subscriber JSON for kind=equity|options."""
    try:
        return _resolve_bot(kind)[2]
    except Exception:
        from src.utils import PROJECT_ROOT
        return PROJECT_ROOT / "output/subscribers.json"


def _subscriber_file_legacy() -> Path:
    """Legacy (equity) only — kept for callers that assumed one bot."""
    return _subscriber_file("equity")


def _load_subscribers(kind: str = "equity") -> Dict[str, Any]:
    """Read the subscriber store for kind. Returns {} when missing/corrupt."""
    try:
        path = _subscriber_file(kind)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Subscriber store unreadable: %s", exc)
    return {}


def _save_subscribers(data: Dict[str, Any], kind: str = "equity") -> None:
    """Atomic write of the subscriber store (tmp + rename)."""
    path = _subscriber_file(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _subscriber_ids(fallback_chat_id: Optional[str], kind: str = "equity") -> List[str]:
    """All chat IDs to fan out to. Falls back to the single .env ID."""
    data = _load_subscribers(kind)
    chats = data.get("chats")
    if isinstance(chats, list) and chats:
        return [str(c) for c in chats]
    if fallback_chat_id:
        return [str(fallback_chat_id)]
    return []


def _record_delivery(chat_id: str, ok: bool, blocked: bool = False,
                     kind: str = "equity") -> None:
    """Track consecutive failures; auto-remove after 3 (likely blocked)."""
    if ok and not blocked:
        try:
            data = _load_subscribers(kind)
            fails = data.get("failures", {})
            if str(chat_id) in fails:
                del fails[str(chat_id)]
                data["failures"] = fails
                _save_subscribers(data, kind)
        except Exception:
            pass
        return
    try:
        data = _load_subscribers(kind)
        fails = data.get("failures", {})
        key = str(chat_id)
        fails[key] = int(fails.get(key, 0)) + 1
        if blocked or fails[key] >= 3:
            chats = [str(c) for c in data.get("chats", []) if str(c) != key]
            data["chats"] = chats
            del fails[key]
            logger.warning("Removed Telegram subscriber %s after %s failures",
                           chat_id, "block" if blocked else "3")
        data["failures"] = fails
        _save_subscribers(data, kind)
    except Exception as exc:
        logger.warning("Could not record delivery state: %s", exc)


WELCOME_MESSAGE = """👋 *Welcome to TRIO alerts — you're subscribed.*

From the next signal you'll get entries like this:

🟢 *TRIO BUY — RELIANCE.NS*
Confidence: 78%
Entry: 1,180.00
Stop: 1,160.00
Target: 1,210.00
Qty: 7  |  Risk: ₹140.00  |  R:R 1:1.5

And exits like this:

✅ *CLOSED PASS — RELIANCE.NS*
P&L: ₹+210.00 (+2.54%)
LONG 7 @ 1,180.00 → 1,210.00
Why closed: target-hit

_Educational only. Not financial advice._"""


def _send_one(token: str, chat_id: str, text: str,
              timeout: int = 10) -> bool:
    """Send a single message. Returns True on success. Test-seam."""
    import requests
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text,
                                     "parse_mode": "Markdown"}, timeout=timeout)
    resp.raise_for_status()
    return True


def health_check(kind: str = "equity") -> bool:
    """Ping bot getMe (no message) to verify token validity for kind."""
    token = _resolve_bot(kind)[0]
    if not token:
        return False
    try:
        import requests  # noqa: E402
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        resp.raise_for_status()
        return bool(resp.json().get("ok"))
    except Exception:
        return False


def _handle_joins_kind(kind: str = "equity") -> List[str]:
    """Poll getUpdates for `kind`. Separated token/file per kind."""
    token, chat_id, _ = _resolve_bot(kind)
    try:
        cfg = load_config()
        tg_cfg = cfg.get("alerts", {}).get("telegram", {})
        if not tg_cfg.get("enabled", False):
            return []
    except Exception:
        return []
    if not token:
        return []

    data = _load_subscribers(kind)
    seed = chat_id  # already resolved from kind-specific env
    chats: List[str] = [str(c) for c in data.get("chats", [])]
    if seed and str(seed) not in chats:
        chats.append(str(seed))
        data["chats"] = chats
        _save_subscribers(data, kind)

    offset = data.get("offset", 0)
    try:
        import requests  # noqa: E402
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 0}, timeout=15)
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    except Exception as exc:
        logger.warning("Telegram getUpdates failed (kind=%s): %s", kind, exc)
        return []

    new_ids: List[str] = []
    max_id = offset
    for upd in updates:
        max_id = max(max_id, int(upd.get("update_id", 0)) + 1)
        msg = upd.get("message", {})
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat", {})
        cid = str(chat.get("id", ""))
        if not cid or text != "/start":
            continue
        if cid not in chats:
            chats.append(cid)
            new_ids.append(cid)
            try:
                _send_one(token, cid, WELCOME_MESSAGE)
                logger.info("Welcomed new Telegram subscriber %s (kind=%s)",
                            cid, kind)
            except Exception as exc:
                logger.warning("Welcome message failed for %s: %s", cid, exc)
    data["chats"] = chats
    data["offset"] = max_id
    _save_subscribers(data, kind)
    return new_ids


def handle_joins() -> List[str]:
    """Legacy (equity) join poll — kept for backwards-compat."""
    return _handle_joins_kind("equity")


def handle_options_joins() -> List[str]:
    """New options bot join poll (8877508167)."""
    return _handle_joins_kind("options")


def handle_all_joins() -> List[str]:
    """Poll BOTH bots. Returns combined new ids (deduped)."""
    out: List[str] = []
    for k in ("equity", "options"):
        try:
            out.extend(_handle_joins_kind(k))
        except Exception:
            pass
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(
    subject: str,
    body: str,
    config_override: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Send an email alert via SMTP.

    Requires EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT environment variables.

    Args:
        subject:         Email subject line.
        body:            Email body text.
        config_override: Override alert config.

    Returns:
        True if sent successfully.
    """
    if os.environ.get("TRIO_TEST_MODE") == "1" and config_override is None:
        logger.debug("Email suppressed in test mode")
        return False

    cfg = load_config()
    em_cfg = config_override or cfg.get("alerts", {}).get("email", {})

    if not em_cfg.get("enabled", False):
        logger.debug("Email alerts disabled")
        return False

    sender = get_env(em_cfg.get("sender_env", "EMAIL_SENDER"))
    password = get_env(em_cfg.get("password_env", "EMAIL_PASSWORD"))
    recipient = get_env(em_cfg.get("recipient_env", "EMAIL_RECIPIENT"))

    if not all([sender, password, recipient]):
        logger.warning("Email credentials not fully set")
        return False

    smtp_server = em_cfg.get("smtp_server", "smtp.gmail.com")
    smtp_port = em_cfg.get("smtp_port", 587)

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender, password)
            server.send_message(msg)

        logger.info("Email alert sent to %s", recipient)
        return True
    except Exception as exc:
        logger.error("Email send failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Signal formatter
# ---------------------------------------------------------------------------

def _fmt_num(v: Any, nd: int = 2) -> str:
    """Format a price for Telegram: compact, no trailing noise."""
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def format_signal_message(signal: Any) -> str:
    """Format a TradeSignal into a Telegram-friendly alert.

    Layout answers, in order: WHAT (action+symbol), WHERE (entry),
    WHAT-IF-WRONG (stop), WHAT-IF-RIGHT (target), HOW MUCH (size/risk),
    HOW SURE (confidence/rank/setup), and WHY (top reasons only —
    Telegram cuts messages at ~4096 chars, so the full reasoning list
    stays in the dashboard/terminal).
    """
    d = signal if isinstance(signal, dict) else signal.to_dict()

    action = str(d.get("action", "HOLD")).upper()
    emoji = {"BUY": "🟢", "SELL": "🔴"}.get(action, "⚪")
    symbol = _md_escape(d.get("symbol", "N/A"))
    conf = d.get("confidence", 0)

    lines = [
        f"{emoji} *TRIO {_md_escape(action)} — {symbol}*",
        f"Time: {_md_escape(ist_display(d.get('generated_at')))}",
        f"Confidence: {_md_escape(conf)}%",
    ]
    # Ladder levels (Phase 1: only T1 is armed; T2/T3 converge to target).
    if action in ("BUY", "SELL") and d.get("target_t1"):
        t1 = d.get("target_t1"); t = d.get("target")
        rr = t1 if t1 else None
        t1_s = f"T1:{_md_escape(_fmt_num(t1))} (50% + breakeven)"
        lines.append(t1_s + (f"  |  Full:{_md_escape(_fmt_num(t))}"
                             if t else ""))

    if action in ("BUY", "SELL"):
        lines += [
            f"Entry: {_md_escape(_fmt_num(d.get('entry_price')))}",
            f"Stop: {_md_escape(_fmt_num(d.get('stop_loss')))}",
            f"Target: {_md_escape(_fmt_num(d.get('target')))}",
            f"Qty: {_md_escape(d.get('position_size', 0))}  |  "
            f"Risk: ₹{_md_escape(_fmt_num(d.get('risk_amount')))}  |  "
            f"R:R {_md_escape(d.get('risk_reward_ratio', '—'))}",
        ]
        if d.get("rank") is not None:
            lines.append(
                f"Rank: {_md_escape(d.get('rank'))}  |  "
                f"Setup: {_md_escape(d.get('setup_name', '—'))}")
    else:
        lines.append("_No entry — guard refused. Reason below._")

    reasons = d.get("reasoning", []) or []
    if reasons:
        lines.append("")
        lines.append("*Why:*")
        for reason in reasons[:6]:
            # Strip the noisy per-indicator dump lines; keep verdicts.
            r = str(reason).strip()
            if "value=" in r and "tech_score=" not in r and "Composite" not in r:
                continue
            lines.append(f"• {_md_escape(r)}")
        skipped = [r for r in reasons if "SKIP" in str(r).upper()
                   or "NO PULLBACK" in str(r).upper()
                   or "BLOCKED" in str(r).upper()]
        for s in skipped[:2]:
            esc = f"• {_md_escape(str(s).strip())}"
            if esc not in lines:
                lines.append(esc)

    lines.append("")
    lines.append("_Educational only. Not financial advice._")
    return "\n".join(lines)


def format_startup_message(status: Dict[str, Any]) -> str:
    """Format the boot announcement for Telegram.

    Sent once per process start, AFTER the readiness gate passes, so
    "started" means verified-working. ``status`` keys: booted_at (ISO),
    provider, equity, positions, symbols count, interval, session window,
    capital_start, issues (list of gate failure strings, empty = clean).
    """
    issues = status.get("issues") or []
    emoji = "🚀" if not issues else "⚠️"
    head = "STARTED AND WORKING" if not issues else "STARTED WITH ISSUES"
    lines = [
        f"{emoji} *TRIO {head} — "
        f"{_md_escape(ist_display(status.get('booted_at')))}*",
        "Starting for Mr Kapil Kuhire Sir",
        "",
        f"Broker: {_md_escape(status.get('provider', '—'))}  |  "
        f"Equity: ₹{_md_escape(_fmt_num(status.get('equity')))}",
        f"Positions open: {_md_escape(status.get('positions', 0))}  |  "
        f"Watching: {_md_escape(status.get('symbols', 0))} symbols, "
        f"scan every {_md_escape(status.get('interval', 0))}s",
        f"Session: {_md_escape(status.get('session', '—'))}  |  "
        f"Capital base: ₹{_md_escape(_fmt_num(status.get('capital_start')))}",
    ]
    if issues:
        lines.append("")
        lines.append("*Issues found at boot:*")
        for issue in issues[:5]:
            lines.append(f"• {_md_escape(str(issue))}")
    lines.append("")
    lines.append("_Educational only. Not financial advice._")
    return "\n".join(lines)


def _fmt_held(minutes: Any) -> str:
    """Human holding time: 45 -> '45m', 90 -> '1h 30m', None -> '—'."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return "—"
    if m < 0:
        return "—"
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h {m % 60:02d}m"


def format_exit_message(trade: Dict[str, Any]) -> str:
    """Format a closed-trade record (PASS/FAIL) for Telegram.

    Answers the full audit question: WHAT (symbol/side/qty), WHERE
    (entry -> exit), WHEN (entry/exit timestamps in IST, holding
    duration), HOW MUCH (P&L), and WHY (close reason).
    """
    result = str(trade.get("result", "")).upper()
    emoji = "✅" if result == "PASS" else "❌"
    # Partial half-exits (ladder T1) use their own marker so the final
    # full exit is unmistakable.
    if str(trade.get("reason", "")).startswith("partial"):
        emoji = "🔹"
        result += " (PARTIAL 50%)"
    symbol = _md_escape(trade.get("symbol", "N/A"))
    pnl = trade.get("pnl", 0)
    try:
        pnl_s = f"₹{float(pnl):+,.2f} ({float(trade.get('pnl_pct', 0)):+.2f}%)"
    except (TypeError, ValueError):
        pnl_s = str(pnl)
    lines = [
        f"{emoji} *CLOSED {_md_escape(result)} — {symbol}*",
        f"P&L: {_md_escape(pnl_s)}",
        f"{_md_escape(trade.get('side', ''))} "
        f"{_md_escape(trade.get('quantity', ''))} @ "
        f"{_md_escape(_fmt_num(trade.get('entry')))} → "
        f"{_md_escape(_fmt_num(trade.get('exit')))}",
        f"Entry: {_md_escape(ist_display(trade.get('entry_time')))}",
        f"Exit: {_md_escape(ist_display(trade.get('closed_at')))}",
        f"Held: {_md_escape(_fmt_held(trade.get('holding_minutes')))}",
        f"Why closed: {_md_escape(trade.get('reason', '—'))}",
    ]
    lines.append("")
    lines.append("_Educational only. Not financial advice._")
    return "\n".join(lines)


def send_signal_alert(signal: Any) -> None:
    """Send a signal alert via the correct bot (equity vs options)."""
    message = format_signal_message(signal)
    is_opt = False
    try:
        d = signal if isinstance(signal, dict) else signal.to_dict()
        if d.get("asset") == "options" or (
                isinstance(d.get("option_legs"), dict)
                and d["option_legs"].get("strategy") not in (None, "", "none")):
            is_opt = True
    except Exception:
        pass
    if is_opt:
        send_options_telegram(message)
    else:
        send_telegram(message)
    send_email(
        subject=f"TRIO: {signal.action if hasattr(signal, 'action') else 'Signal'} — {signal.symbol if hasattr(signal, 'symbol') else ''}",
        body=message,
    )


def send_exit_alert(trade: Dict[str, Any]) -> None:
    """Send a closed-trade (PASS/FAIL) alert via the correct bot."""
    message = format_exit_message(trade)
    if str(trade.get("asset", "")).lower() == "options":
        send_options_telegram(message)
    else:
        send_telegram(message)
    send_email(
        subject=f"TRIO CLOSED {trade.get('result', '')} — {trade.get('symbol', '')} "
                f"{trade.get('pnl', '')}",
        body=message,
    )
