#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tracked-wallet overlay -- cron entry point.

Read-only.  No signing.  No private keys.  No order placement.

For each wallet in src.wallet_tracker.config.TRACKED_WALLETS:
  1. Fetch positions via Hyperliquid's public clearinghouseState endpoint
  2. Compare against the last persisted snapshot in data/wallet_state/
  3. If meaningful changes are detected, send a Telegram message
  4. Persist the current snapshot atomically

On the very first run (no prior state file), the snapshot is persisted
and no notifications fire -- this avoids a flood of OPEN messages.

Usage on DO (in repo root):
  python scripts/track_wallets.py            # one shot -- designed for cron
  python scripts/track_wallets.py --dry-run  # fetch + diff, no Telegram

Cron (every 5 min):
  */5 * * * * cd /opt/hyperliquid-momentum && \\
    /opt/hyperliquid-momentum/.venv/bin/python scripts/track_wallets.py \\
    >> /var/log/track_wallets.log 2>&1
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Reconfigure stdout/stderr to UTF-8 so the script behaves the same on
# Windows (cp932 default) and Linux (utf-8 default).  Matches the pattern
# used by main.py / app.py.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass  # older Python or non-reconfigurable stream

# Make `src` importable when this script is invoked directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.wallet_tracker.config       import TRACKED_WALLETS, WALLET_LABELS
from src.wallet_tracker.diff         import diff_positions
from src.wallet_tracker.hl_positions import extract_positions, fetch_positions
from src.wallet_tracker.state        import load_state, save_state
from src.wallet_tracker.telegram     import send_message

logger = logging.getLogger("track_wallets")


# ---------------------------------------------------------------------------
# Message rendering -- ASCII tags only (no emoji in source files per repo rule)
# ---------------------------------------------------------------------------

def _label(address: str) -> str:
    return WALLET_LABELS.get(address.lower(), address[:6] + ".." + address[-4:])


def _fmt_usd(v) -> str:
    if v is None:
        return "?"
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "?"


def _fmt_size(v) -> str:
    if v is None:
        return "?"
    try:
        return f"{float(v):.4f}"
    except (TypeError, ValueError):
        return "?"


def render_change(address: str, change: dict) -> str:
    label = _label(address)
    coin  = change.get("coin", "?")
    kind  = change.get("kind", "?")

    if kind == "OPEN":
        return (
            f"[OPEN] <b>{label}</b> {change['side']} <b>{coin}</b>\n"
            f"  size={_fmt_size(change.get('szi'))}  "
            f"notional={_fmt_usd(change.get('notional'))}  "
            f"lev={change.get('leverage')}x"
        )
    if kind == "CLOSE":
        return (
            f"[CLOSE] <b>{label}</b> {change['side']} <b>{coin}</b>\n"
            f"  prev_size={_fmt_size(change.get('prev_szi'))}  "
            f"prev_notional={_fmt_usd(change.get('prev_notional'))}"
        )
    if kind == "FLIP":
        return (
            f"[FLIP] <b>{label}</b> <b>{coin}</b> "
            f"{change['old_side']} -> {change['new_side']}\n"
            f"  {_fmt_size(change.get('old_szi'))} -> {_fmt_size(change.get('new_szi'))}  "
            f"({_fmt_usd(change.get('old_notional'))} -> {_fmt_usd(change.get('new_notional'))})"
        )
    if kind == "RESIZE":
        pct = change.get("change_pct", 0) * 100
        return (
            f"[RESIZE] <b>{label}</b> {change['side']} <b>{coin}</b> "
            f"({pct:+.1f}%)\n"
            f"  {_fmt_size(change.get('old_szi'))} -> {_fmt_size(change.get('new_szi'))}  "
            f"({_fmt_usd(change.get('old_notional'))} -> {_fmt_usd(change.get('new_notional'))})"
        )
    if kind == "LEVERAGE_CHANGE":
        return (
            f"[LEV] <b>{label}</b> {change['side']} <b>{coin}</b>  "
            f"{change.get('old_leverage')} -> {change.get('new_leverage')}"
        )
    return f"<b>{label}</b> {kind} {coin}"


# ---------------------------------------------------------------------------
# Per-wallet processing
# ---------------------------------------------------------------------------

def process_wallet(address: str, dry_run: bool) -> int:
    """Return the number of changes detected for this wallet."""
    label = _label(address)
    logger.info("[%s] fetching positions for %s ...", label, address)

    payload = fetch_positions(address)
    if payload is None:
        logger.warning("[%s] no payload -- skipping this run.", label)
        return 0

    curr = extract_positions(payload)
    logger.info("[%s] %d open position(s)", label, len(curr))

    prev_state = load_state(address)
    prev_positions = (prev_state or {}).get("positions") if prev_state else None

    if prev_positions is None:
        # First run -- persist and exit silently.
        save_state(address, curr, raw=payload)
        logger.info("[%s] first run -- initial snapshot saved, no alerts sent.", label)
        return 0

    changes = diff_positions(prev_positions, curr)
    if not changes:
        save_state(address, curr, raw=payload)
        logger.info("[%s] no meaningful changes.", label)
        return 0

    for ch in changes:
        line = render_change(address, ch)
        logger.info("[%s] change: %s", label, ch)
        if not dry_run:
            send_message(line)

    save_state(address, curr, raw=payload)
    return len(changes)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tracked-wallet overlay (read-only)")
    p.add_argument(
        "--dry-run", action="store_true",
        help="Fetch, diff, log -- but do not send Telegram messages.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    total = 0
    for addr in TRACKED_WALLETS:
        try:
            total += process_wallet(addr, dry_run=args.dry_run)
        except Exception as exc:
            logger.exception("Unhandled error for %s: %s", addr, exc)
    logger.info("Done. %d total change(s) across %d wallet(s).",
                total, len(TRACKED_WALLETS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
