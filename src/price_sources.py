# -*- coding: utf-8 -*-
"""
Price-source mapping layer.

Priority order per coin:
  1. Hyperliquid candleSnapshot  (primary for all HL perps)
  2. Yahoo Finance               (fallback for delisted crypto and xyz: assets)
"""
import logging
from typing import Optional

import pandas as pd
import requests

HL_API_URL = "https://api.hyperliquid.xyz/info"
HL_TIMEOUT = 30
HL_DELAY = 0.15  # seconds between HL requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol mapping tables
# ---------------------------------------------------------------------------

# xyz: commodity -> Yahoo Finance futures ticker
_XYZ_COMMODITY: dict[str, str] = {
    "GOLD": "GC=F", "XAU": "GC=F",
    "SILVER": "SI=F", "XAG": "SI=F",
    "OIL": "CL=F", "WTI": "CL=F", "CRUDE": "CL=F",
    "NATGAS": "NG=F",
    "WHEAT": "ZW=F",
    "CORN": "ZC=F",
    "COPPER": "HG=F",
    "PLATINUM": "PL=F",
    "PALLADIUM": "PA=F",
}

# xyz: index -> Yahoo Finance symbol
_XYZ_INDEX: dict[str, str] = {
    "SPX": "^GSPC", "SP500": "^GSPC",
    "NDX": "^NDX", "NASDAQ": "^NDX",
    "DJI": "^DJI", "DOW": "^DJI",
    "VIX": "^VIX",
    "RUT": "^RUT",
    "NIKKEI": "^N225",
    "DAX": "^GDAXI",
    "FTSE": "^FTSE",
    "CAC": "^FCHI",
}

# Known delisted / renamed coins on Hyperliquid -> Yahoo Finance symbol
_DELISTED_CRYPTO: dict[str, str] = {
    "MATIC":    "MATIC-USD",
    "RNDR":     "RNDR-USD",
    "FTM":      "FTM-USD",
    "RENDER":   "RENDER-USD",
    "ILV":      "ILV-USD",
    "BADGER":   "BADGER-USD",
    "BLZ":      "BLZ-USD",
    "BNT":      "BNT-USD",
    "LOOM":     "LOOM-USD",
    "RDNT":     "RDNT-USD",
    "ORBS":     "ORBS-USD",
    "STRAX":    "STRAX-USD",
    "CANTO":    "CANTO-USD",
    "NTRN":     "NTRN-USD",
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def yahoo_symbol_for(coin: str) -> Optional[str]:
    """
    Return the Yahoo Finance symbol for a Hyperliquid coin name, or None if unknown.

    For xyz: coins the explicit XYZ_YAHOO_MAP table is consulted first so that
    the mapping is documented and version-controlled.  Unknown xyz: coins that
    are not in the map return None rather than guessing a ticker.

    Examples:
      xyz:AAPL  -> AAPL      (stock)
      xyz:GOLD  -> GC=F      (commodity future)
      xyz:XYZ100 -> ^NDX     (index proxy; prices differ from HL oracle)
      MATIC     -> MATIC-USD (delisted crypto fallback)
    """
    if coin.startswith("xyz:"):
        from src.xyz_symbols import XYZ_YAHOO_MAP  # avoid module-level circular import
        return XYZ_YAHOO_MAP.get(coin)  # None if no mapping defined

    return _DELISTED_CRYPTO.get(coin)


def fetch_hl_candles(
    coin: str,
    start_ms: int,
    end_ms: int,
    interval: str = "1d",
) -> list[dict]:
    """
    Fetch candles from Hyperliquid candleSnapshot.

    Args:
      coin     - HL coin name (e.g. "BTC", "xyz:AAPL")
      start_ms - epoch ms for the start of the window
      end_ms   - epoch ms for the end of the window
      interval - candle interval: "1d" (default), "1h", "4h", "15m", etc.
                 Only "1d" is used by the Momentum scanner.
                 "1h" is used by the Pairs scanner.

    Returns [] on any error.
    """
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    try:
        resp = requests.post(HL_API_URL, json=payload, timeout=HL_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.debug("HL candleSnapshot failed for %s (%s): %s", coin, interval, exc)
        return []


def candles_to_series(candles: list[dict]) -> pd.Series:
    """
    Convert Hyperliquid candle list to UTC-date-indexed close-price Series.
    Used by the daily (1d) Momentum scanner.  Each bar is collapsed to its
    calendar date; duplicate dates are dropped (keeps last).
    """
    if not candles:
        return pd.Series(dtype=float)
    rows = []
    for c in candles:
        try:
            ts = pd.Timestamp(c["T"], unit="ms", tz="UTC").normalize()
            rows.append((ts, float(c["c"])))
        except (KeyError, ValueError, TypeError):
            continue
    if not rows:
        return pd.Series(dtype=float)
    return (
        pd.DataFrame(rows, columns=["date", "close"])
        .drop_duplicates("date")
        .set_index("date")["close"]
        .sort_index()
    )


def candles_to_series_hourly(candles: list[dict]) -> pd.Series:
    """
    Convert Hyperliquid candle list to UTC-hour-indexed close-price Series.
    Used by the Pairs scanner (1h candles).  Timestamps are floored to the
    nearest hour; duplicates are dropped (keeps last).
    """
    if not candles:
        return pd.Series(dtype=float)
    rows = []
    for c in candles:
        try:
            ts = pd.Timestamp(c["T"], unit="ms", tz="UTC").floor("h")
            rows.append((ts, float(c["c"])))
        except (KeyError, ValueError, TypeError):
            continue
    if not rows:
        return pd.Series(dtype=float)
    return (
        pd.DataFrame(rows, columns=["ts", "close"])
        .drop_duplicates("ts")
        .set_index("ts")["close"]
        .sort_index()
    )


def fetch_yahoo_closes(yahoo_symbol: str, start_ms: int, end_ms: int) -> pd.Series:
    """Fetch daily closes from Yahoo Finance via yfinance. Returns empty Series on error."""
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed; skipping Yahoo Finance for %s", yahoo_symbol)
        return pd.Series(dtype=float)
    try:
        start_dt = pd.Timestamp(start_ms, unit="ms").strftime("%Y-%m-%d")
        end_dt   = pd.Timestamp(end_ms,   unit="ms").strftime("%Y-%m-%d")
        hist = yf.Ticker(yahoo_symbol).history(
            start=start_dt, end=end_dt, interval="1d", auto_adjust=True
        )
        if hist.empty:
            return pd.Series(dtype=float)
        series = hist["Close"].copy()
        series.index = pd.to_datetime(series.index, utc=True).normalize()
        return series.sort_index()
    except Exception as exc:
        logger.debug("Yahoo Finance failed for %s: %s", yahoo_symbol, exc)
        return pd.Series(dtype=float)
