# -*- coding: utf-8 -*-
"""
Canonical xyz: asset registry for Hyperliquid tokenised real-world assets.

xyz: assets are NOT returned by the {"type":"meta"} perpetual futures API, but
their price history is available via the candleSnapshot API using the full
"xyz:<BASE>" coin identifier.

This module provides:
  XYZ_UNIVERSE  - ordered list of known xyz: symbols to include in scans
  XYZ_YAHOO_MAP - explicit Yahoo Finance fallback symbol for each xyz: coin
  asset_type()  - classify a coin as "stock", "commodity", "index", or "perp"
"""

# ---------------------------------------------------------------------------
# Universe groupings (ordered: most-likely-to-pass-filter first)
# ---------------------------------------------------------------------------

# Equities confirmed to have >= 100 daily candles on HL as of 2026-03-11
_XYZ_STOCKS_CONFIRMED: list[str] = [
    "xyz:AAPL",   # Apple Inc.                  (111 HL closes)
    "xyz:NVDA",   # NVIDIA Corp.                (120 HL closes)
    "xyz:TSLA",   # Tesla Inc.                  (119 HL closes)
    "xyz:MSFT",   # Microsoft Corp.             (113 HL closes)
    "xyz:AMZN",   # Amazon.com Inc.             (114 HL closes)
    "xyz:META",   # Meta Platforms Inc.         (112 HL closes)
    "xyz:GOOGL",  # Alphabet Inc. Class A       (114 HL closes)
    "xyz:COIN",   # Coinbase Global Inc.        (107 HL closes)
]

# Equities near the 100-close threshold (will pass filter in coming weeks)
_XYZ_STOCKS_NEAR: list[str] = [
    "xyz:NFLX",   # Netflix Inc.                (~94 HL closes)
    "xyz:AMD",    # Advanced Micro Devices      (~98 HL closes)
    "xyz:INTC",   # Intel Corp.                 (~99 HL closes)
]

# Newer equity listings (< 100 HL closes; included for xyz_debug.csv coverage)
_XYZ_STOCKS_NEW: list[str] = [
    "xyz:ORCL",   # Oracle Corp.
    "xyz:MU",     # Micron Technology
    "xyz:PLTR",   # Palantir Technologies
    "xyz:MSTR",   # MicroStrategy (Strategy)
    "xyz:HOOD",   # Robinhood Markets
    "xyz:RIVN",   # Rivian Automotive
    "xyz:BABA",   # Alibaba Group
    "xyz:TSM",    # Taiwan Semiconductor
]

# Commodities (HL has some history; Yahoo Finance futures used as fallback)
_XYZ_COMMODITIES: list[str] = [
    "xyz:GOLD",   # Gold spot/futures            (~80 HL closes; Yahoo: GC=F)
    "xyz:SILVER", # Silver                       (~76 HL closes; Yahoo: SI=F)
    "xyz:CL",     # WTI Crude Oil                (~65 HL closes; Yahoo: CL=F)
    "xyz:NATGAS", # Natural Gas                  (~50 HL closes; Yahoo: NG=F)
    "xyz:COPPER", # Copper                       (~31 HL closes; Yahoo: HG=F)
]

# Indices (HL oracle prices differ from Yahoo proxies)
_XYZ_INDICES: list[str] = [
    # HL computes its own index; 150 closes on HL; Yahoo ^NDX is an approx proxy
    "xyz:XYZ100", # Nasdaq-100 proxy             (150 HL closes; Yahoo: ^NDX)
]

# Full universe — process in this order so confirmed stocks appear first
XYZ_UNIVERSE: list[str] = (
    _XYZ_STOCKS_CONFIRMED
    + _XYZ_STOCKS_NEAR
    + _XYZ_STOCKS_NEW
    + _XYZ_COMMODITIES
    + _XYZ_INDICES
)

# ---------------------------------------------------------------------------
# Explicit Yahoo Finance fallback symbols
# ---------------------------------------------------------------------------
# For indices: HL oracle prices and Yahoo prices differ significantly
# (xyz:XYZ100 trades ~$25k while ^NDX is ~$22k). Yahoo is only used when HL
# returns no data, and the mismatch is documented in xyz_debug.csv.

XYZ_YAHOO_MAP: dict[str, str] = {
    # Equities — pass ticker directly to yfinance
    "xyz:AAPL":   "AAPL",
    "xyz:NVDA":   "NVDA",
    "xyz:TSLA":   "TSLA",
    "xyz:MSFT":   "MSFT",
    "xyz:AMZN":   "AMZN",
    "xyz:META":   "META",
    "xyz:GOOGL":  "GOOGL",
    "xyz:COIN":   "COIN",
    "xyz:NFLX":   "NFLX",
    "xyz:AMD":    "AMD",
    "xyz:INTC":   "INTC",
    "xyz:ORCL":   "ORCL",
    "xyz:MU":     "MU",
    "xyz:PLTR":   "PLTR",
    "xyz:MSTR":   "MSTR",
    "xyz:HOOD":   "HOOD",
    "xyz:RIVN":   "RIVN",
    "xyz:BABA":   "BABA",
    "xyz:TSM":    "TSM",
    # Commodities — Yahoo continuous futures tickers
    "xyz:GOLD":   "GC=F",
    "xyz:SILVER": "SI=F",
    "xyz:CL":     "CL=F",
    "xyz:NATGAS": "NG=F",
    "xyz:COPPER": "HG=F",
    # Indices — approximate Yahoo proxies (prices differ from HL oracle)
    "xyz:XYZ100": "^NDX",
}

# ---------------------------------------------------------------------------
# Asset-type classification
# ---------------------------------------------------------------------------

_STOCK_SET: frozenset[str] = frozenset(
    c[4:] for c in _XYZ_STOCKS_CONFIRMED + _XYZ_STOCKS_NEAR + _XYZ_STOCKS_NEW
)
_COMMODITY_SET: frozenset[str] = frozenset(c[4:] for c in _XYZ_COMMODITIES)
_INDEX_SET: frozenset[str] = frozenset(c[4:] for c in _XYZ_INDICES)


def asset_type(coin: str) -> str:
    """
    Return the asset-type classification for a coin name.

    xyz: coins are classified as "stock", "commodity", or "index".
    Non-xyz: coins return "perp".
    Unknown xyz: bases return "unknown".
    """
    if not coin.startswith("xyz:"):
        return "perp"
    base = coin[4:]
    if base in _STOCK_SET:
        return "stock"
    if base in _COMMODITY_SET:
        return "commodity"
    if base in _INDEX_SET:
        return "index"
    return "unknown"
