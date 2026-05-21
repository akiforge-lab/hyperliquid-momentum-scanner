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
from datetime import datetime, timezone
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

from src.wallet_tracker.config       import (
    SEQUENCE_WINDOW_MIN,
    TRACKED_WALLETS,
    WALLET_LABELS,
)
from src.wallet_tracker.diff         import diff_positions
from src.wallet_tracker.hl_positions import extract_positions, fetch_positions
from src.wallet_tracker.state        import load_state, save_state
from src.wallet_tracker.telegram     import send_message

logger = logging.getLogger("track_wallets")


# ---------------------------------------------------------------------------
# Message rendering -- human, deterministic, ASCII only (no emoji per repo rule)
#
# Two layers, both deterministic (no LLM):
#   1. _render_single   -- one readable sentence per change.
#   2. _render_sequence -- when consecutive same-direction RESIZE events land
#      on the same coin within SEQUENCE_WINDOW_MIN, collapse them into one
#      running summary ("A -> B in N min") instead of repeating mechanical
#      lines.  Sequence memory is persisted in the wallet state file.
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


def _fmt_sz(v) -> str:
    """Compact human size: 2 dp for |v| >= 1, else 4 dp; trailing zeros trimmed."""
    if v is None:
        return "?"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "?"
    s = f"{f:.2f}" if abs(f) >= 1 else f"{f:.4f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _within_window(last_ts) -> bool:
    ts = _parse_iso(last_ts)
    if ts is None:
        return False
    return (_now() - ts).total_seconds() <= SEQUENCE_WINDOW_MIN * 60


def _elapsed(start_iso) -> str:
    start = _parse_iso(start_iso)
    if start is None:
        return "a short while"
    mins = max(1, int((_now() - start).total_seconds() // 60))
    if mins < 60:
        return f"{mins}m"
    h, m = divmod(mins, 60)
    return f"{h}h{m:02d}m" if m else f"{h}h"


def _verb(side: str, direction: str) -> str:
    """Direction-aware verb: covering/trimming/pressing/adding."""
    if direction == "INCREASE":
        return "pressing" if side == "SHORT" else "adding to"
    return "covering" if side == "SHORT" else "trimming"


def _resize_direction(change: dict) -> str:
    return change.get("direction") or (
        "INCREASE" if change.get("change_pct", 0) >= 0 else "REDUCE"
    )


def _render_single(label: str, change: dict) -> str:
    """One readable sentence for a single detected change."""
    coin = change.get("coin", "?")
    kind = change.get("kind", "?")

    if kind == "OPEN":
        return (
            f"<b>{label}</b> opened a {change['side']} on <b>{coin}</b> "
            f"-- {_fmt_sz(change.get('szi'))} ({_fmt_usd(change.get('notional'))}, "
            f"{change.get('leverage')}x)."
        )
    if kind == "CLOSE":
        return (
            f"<b>{label}</b> closed their {change['side']} <b>{coin}</b> "
            f"(was {_fmt_usd(change.get('prev_notional'))})."
        )
    if kind == "FLIP":
        return (
            f"<b>{label}</b> flipped <b>{coin}</b>: "
            f"{change['old_side']} -> {change['new_side']} "
            f"(now {_fmt_sz(change.get('new_szi'))}, "
            f"{_fmt_usd(change.get('new_notional'))})."
        )
    if kind == "RESIZE":
        side = change["side"]
        verb = _verb(side, _resize_direction(change))
        pct  = change.get("change_pct", 0) * 100
        return (
            f"<b>{label}</b> is {verb} {side} <b>{coin}</b>: "
            f"{_fmt_sz(change.get('old_szi'))} -> {_fmt_sz(change.get('new_szi'))} "
            f"({pct:+.1f}%, {_fmt_usd(change.get('new_notional'))})."
        )
    if kind == "LEVERAGE_CHANGE":
        return (
            f"<b>{label}</b> changed <b>{coin}</b> leverage: "
            f"{change.get('old_leverage')} -> {change.get('new_leverage')}."
        )
    return f"<b>{label}</b> {kind} {coin}"


def _render_sequence(label: str, coin: str, seq: dict) -> str:
    """Summarise an ongoing same-direction RESIZE sequence as one line."""
    side, direction = seq["side"], seq["direction"]
    verb  = _verb(side, direction)
    a, b  = _fmt_sz(seq["anchor_szi"]), _fmt_sz(seq["last_szi"])
    span  = _elapsed(seq["anchor_ts"])
    steps = seq.get("steps", 1)
    try:
        a0, b0 = abs(float(seq["anchor_szi"])), abs(float(seq["last_szi"]))
        frac = (b0 - a0) / a0 if a0 else 0.0   # +ve grew, -ve shrank
    except Exception:
        frac = 0.0
    pace = "aggressively " if abs(frac) >= 0.5 else "steadily "
    msg = (
        f"<b>{label}</b> is {pace}{verb} {side} <b>{coin}</b>: "
        f"{a} -> {b} in {span} ({steps} moves)."
    )
    if direction == "REDUCE" and -0.95 < frac < 0.0:
        msg += " Looks like de-risking, not a full flip."
    return msg


def _apply_change(label: str, change: dict, sequences: dict) -> str:
    """
    Render one change to a line, updating RESIZE-sequence memory in place.

    Non-RESIZE events (OPEN/CLOSE/FLIP/LEVERAGE_CHANGE) end any active
    sequence for that coin.  Consecutive same-direction RESIZE events within
    SEQUENCE_WINDOW_MIN are collapsed into a running summary.
    """
    coin = change.get("coin")
    if change.get("kind") != "RESIZE":
        sequences.pop(coin, None)
        return _render_single(label, change)

    direction = _resize_direction(change)
    side      = change["side"]
    seq       = sequences.get(coin)
    cont = (
        seq is not None
        and seq.get("side") == side
        and seq.get("direction") == direction
        and _within_window(seq.get("last_ts"))
    )
    if cont:
        seq["last_szi"] = change.get("new_szi")
        seq["last_ts"]  = _iso(_now())
        seq["steps"]    = seq.get("steps", 1) + 1
        return _render_sequence(label, coin, seq)

    # Start a fresh sequence anchored at this event's pre-change size.
    sequences[coin] = {
        "side":       side,
        "direction":  direction,
        "anchor_szi": change.get("old_szi"),
        "anchor_ts":  _iso(_now()),
        "last_szi":   change.get("new_szi"),
        "last_ts":    _iso(_now()),
        "steps":      1,
    }
    return _render_single(label, change)


def _prune_sequences(sequences: dict) -> dict:
    """Drop sequences whose last update is older than SEQUENCE_WINDOW_MIN."""
    return {c: s for c, s in sequences.items() if _within_window(s.get("last_ts"))}


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
    sequences = dict((prev_state or {}).get("sequences") or {})

    if prev_positions is None:
        # First run -- persist and exit silently.
        save_state(address, curr, raw=payload, sequences={})
        logger.info("[%s] first run -- initial snapshot saved, no alerts sent.", label)
        return 0

    changes = diff_positions(prev_positions, curr)
    if not changes:
        save_state(address, curr, raw=payload, sequences=_prune_sequences(sequences))
        logger.info("[%s] no meaningful changes.", label)
        return 0

    # Render each change (updating sequence memory), then send ONE batched
    # message per wallet instead of a separate message per event.
    lines: list[str] = []
    for ch in changes:
        logger.info("[%s] change: %s", label, ch)
        lines.append(_apply_change(label, ch, sequences))

    sequences = _prune_sequences(sequences)
    if dry_run:
        for ln in lines:
            logger.info("[%s] (dry-run) %s", label, ln)
    else:
        send_message("\n\n".join(lines))

    save_state(address, curr, raw=payload, sequences=sequences)
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
