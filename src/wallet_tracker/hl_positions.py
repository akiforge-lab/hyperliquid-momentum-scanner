# -*- coding: utf-8 -*-
"""
Read-only fetcher for Hyperliquid wallet positions.

Uses the public `clearinghouseState` endpoint -- no auth, no signing, no
private keys.  This module never sends a transaction and never imports
anything that could.
"""
from __future__ import annotations

import logging
from typing import Optional

import requests

from .config import API_TIMEOUT_SEC, HL_INFO_URL

logger = logging.getLogger(__name__)


def fetch_positions(address: str) -> Optional[dict]:
    """
    Return the raw `clearinghouseState` payload for `address`, or None on error.

    The caller is expected to extract `assetPositions` and friends.  We return
    the full payload so the state file retains a useful snapshot for debugging.

    Fields extracted (renamed to snake_case):
      szi               -- signed size (positive = long, negative = short)
      entry_px          -- entry price
      position_value    -- current notional in USD
      unrealized_pnl    -- current unrealised PnL in USD
      leverage_type     -- "cross" | "isolated"
      leverage_value    -- e.g. 10
    """
    if not address.startswith("0x") or len(address) != 42:
        logger.error("Invalid address format: %r", address)
        return None
    try:
        resp = requests.post(
            HL_INFO_URL,
            json={"type": "clearinghouseState", "user": address},
            timeout=API_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("clearinghouseState fetch failed for %s: %s", address, exc)
        return None


def extract_positions(payload: dict) -> dict[str, dict]:
    """
    Convert the raw payload into a coin->position dict keyed by symbol.

    Returns {} if the payload shape is unexpected.

    """
    out: dict[str, dict] = {}
    for ap in payload.get("assetPositions", []) or []:
        pos = ap.get("position") or {}
        coin = pos.get("coin")
        if not coin:
            continue
        try:
            szi = float(pos.get("szi") or 0)
        except (ValueError, TypeError):
            continue
        if szi == 0:
            continue  # closed position -- exchange may still report a row
        lev = pos.get("leverage") or {}
        out[coin] = {
            "szi":            szi,
            "entry_px":       _safe_float(pos.get("entryPx")),
            "position_value": _safe_float(pos.get("positionValue")),
            "unrealized_pnl": _safe_float(pos.get("unrealizedPnl")),
            "leverage_type":  lev.get("type"),
            "leverage_value": _safe_float(lev.get("value")),
        }
    return out


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
