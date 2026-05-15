# -*- coding: utf-8 -*-
"""
Persistent state for the tracked-wallet overlay.

One JSON file per address under data/wallet_state/.  Atomic write (tmp + rename)
so a partial write never corrupts the previous snapshot.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Resolved relative to repo root via the caller; no path import from src/paths.py
# to keep this package self-contained.
STATE_DIR = Path("data/wallet_state")


def _state_path(address: str) -> Path:
    return STATE_DIR / f"{address.lower()}.json"


def load_state(address: str) -> Optional[dict]:
    """Return the last persisted snapshot dict, or None if no state exists."""
    path = _state_path(address)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.warning("State read failed for %s: %s", address, exc)
        return None


def save_state(address: str, positions: dict[str, dict], raw: dict | None = None) -> None:
    """
    Atomically persist the current snapshot.

    `positions` is the extracted coin->position mapping.
    `raw` is the full payload (optional, kept for debugging only).
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "address":   address.lower(),
        "ts":        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "positions": positions,
    }
    if raw is not None:
        payload["raw_margin_summary"] = raw.get("marginSummary")

    path = _state_path(address)
    tmp  = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)   # atomic on POSIX and Windows
