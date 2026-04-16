# -*- coding: utf-8 -*-
"""
Persistent time-series of risk overlay snapshots.

File:   data/risk_history.json
Format: JSON array of snapshot dicts, oldest first.  Each entry:

  {
    "ts":                 "2026-04-16T02:17:00Z",  # ISO-8601 UTC
    "dvol_btc":           62.4,                    # may be absent in bootstrap
    "funding_stress":     0.00089,                 # median |funding_i|
    "oi_total_usd":       8.23e9,
    "oi_change_pct_24h":  -0.6,
    "btc_rv_30d_ann":     44.2,
    "vrp_btc":            18.2
  }

Bootstrap entries (seeded from Deribit daily data on first run) contain only
"ts" and "dvol_btc"; all other keys are absent.  get_series() skips None/missing
values transparently.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src.paths import RISK_HISTORY_FILE
from src.risk_config import OI_LOOKBACK_HOURS, OI_TOLERANCE_HOURS, ZSCORE_WINDOW_DAYS

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 400   # ~13 months at one entry per day; prune beyond this


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_history() -> list[dict]:
    """Return full history list, oldest first.  Returns [] if file absent."""
    if not RISK_HISTORY_FILE.exists():
        return []
    try:
        with open(RISK_HISTORY_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            logger.warning("risk_history.json malformed — resetting to empty")
            return []
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Cannot read risk_history.json: %s", exc)
        return []


def save_history(history: list[dict]) -> None:
    """Write history to disk atomically (write-then-rename)."""
    RISK_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RISK_HISTORY_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(history, fh, indent=2)
    tmp.replace(RISK_HISTORY_FILE)


def append_snapshot(snapshot: dict, history: list[dict]) -> list[dict]:
    """
    Return a new list with snapshot appended and capped at _MAX_ENTRIES.
    Does NOT write to disk; call save_history() separately.
    """
    updated = history + [snapshot]
    if len(updated) > _MAX_ENTRIES:
        updated = updated[-_MAX_ENTRIES:]
    return updated


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_series(
    history: list[dict],
    key: str,
    window_days: int = ZSCORE_WINDOW_DAYS,
) -> list[float]:
    """
    Return float values for `key` from entries within the last `window_days`.
    Entries where the key is absent or None are silently skipped.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    values: list[float] = []
    for entry in history:
        try:
            ts = _parse_ts(entry.get("ts", ""))
            if ts < cutoff:
                continue
            val = entry.get(key)
            if val is not None:
                values.append(float(val))
        except (ValueError, TypeError):
            continue
    return values


def get_oi_24h_ago(history: list[dict]) -> Optional[float]:
    """
    Find the oi_total_usd value from the history entry closest to 24 h ago,
    accepting entries within ±OI_TOLERANCE_HOURS of the target.
    Returns None when no suitable entry exists.
    """
    target  = datetime.now(timezone.utc) - timedelta(hours=OI_LOOKBACK_HOURS)
    tol     = timedelta(hours=OI_TOLERANCE_HOURS)
    best: Optional[dict] = None
    best_diff = timedelta.max

    for entry in history:
        try:
            ts   = _parse_ts(entry.get("ts", ""))
            diff = abs(ts - target)
            if diff <= tol and diff < best_diff:
                best_diff = diff
                best = entry
        except (ValueError, TypeError):
            continue

    if best is None:
        return None
    val = best.get("oi_total_usd")
    return float(val) if val is not None else None


def history_day_count(history: list[dict]) -> int:
    """Return the number of distinct calendar dates in history."""
    return len({e.get("ts", "")[:10] for e in history if e.get("ts")})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str) -> datetime:
    """Parse ISO-8601 UTC string.  Raises ValueError on failure."""
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
