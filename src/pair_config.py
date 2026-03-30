# -*- coding: utf-8 -*-
"""
Pair universe configuration for the 1h Pairs scanner and Pair Momentum scanner.

How the final scan universe is assembled at runtime:
  1. DISCOVERY_ELIGIBLE symbols are resolved via SYMBOL_ALIASES and fetched.
  2. pair_discovery.discover_pairs() generates all C(n,2) combinations,
     pre-filters by corr_168 and data completeness, scores each candidate,
     and returns the top MAX_DISCOVERED_PAIRS.
  3. PINNED_PAIRS are merged in (always included, never subject to scoring).
  4. DENYLIST_SYMBOLS / DENYLIST_PAIRS are never included.
  5. The result is the live pair universe passed to compute_pair_metrics().

Cross-asset xyz:* symbols are intentionally excluded from DISCOVERY_ELIGIBLE
because Hyperliquid does not provide 1h or daily candles for xyz: assets.
"""

# ---------------------------------------------------------------------------
# Symbol alias map — normalises common names to Hyperliquid's exact perp names.
# Hyperliquid uses a "k" prefix for high-supply meme tokens (1 contract = 1000 tokens).
# get_discovery_universe() applies this map automatically; callers don't need to.
# ---------------------------------------------------------------------------
SYMBOL_ALIASES: dict[str, str] = {
    "BONK":  "kBONK",   # kBONK on HL (1000 BONK per contract)
    "PEPE":  "kPEPE",   # kPEPE on HL
    "SHIB":  "kSHIB",   # kSHIB on HL
    "FLOKI": "kFLOKI",  # kFLOKI on HL
}

# ---------------------------------------------------------------------------
# Discovery pool — crypto symbols considered for auto-pair generation.
# ---------------------------------------------------------------------------
# Rules:
#   - Only plain HL perp names (no xyz: prefix).
#   - k-prefix meme tokens listed under their common name; SYMBOL_ALIASES handles
#     the rename to HL's actual contract name (e.g. BONK → kBONK).
#   - Symbols added here should have ≥100 daily closes on HL.
#   - Symbols that fail to fetch data appear in output/pair_universe_debug.csv
#     with reason "no_data" and can be moved to DENYLIST_SYMBOLS.
#
# Universe size (2026-03): ~68 symbols → ~2278 candidate pairs before filtering.
DISCOVERY_ELIGIBLE: list[str] = [
    # Core — always available on HL
    "BTC", "ETH", "SOL", "BNB",

    # Major L1 / L2
    "XRP", "ADA", "AVAX", "DOT", "NEAR", "APT", "SUI",
    "ARB", "OP", "STRK",

    # Alternative L1s with established HL history
    "BCH", "ETC", "ALGO", "FTM",

    # Polygon / zkEVM scaling
    "POL", "ZK",

    # Large-cap alts & memes (k-prefix tokens use common name; alias map resolves)
    "DOGE", "LTC",
    "SHIB",   # → kSHIB via SYMBOL_ALIASES
    "PEPE",   # → kPEPE via SYMBOL_ALIASES

    # Solana meme ecosystem (k-prefix handled by SYMBOL_ALIASES)
    "WIF",
    "BONK",   # → kBONK via SYMBOL_ALIASES
    "BOME",
    "FLOKI",  # → kFLOKI via SYMBOL_ALIASES
    "MEW",

    # DeFi blue-chips
    "LINK", "UNI", "AAVE", "MKR", "CRV",

    # DeFi perps & derivatives
    "SNX", "COMP", "DYDX", "GMX",

    # Liquid staking / restaking / yield
    "LDO", "PENDLE", "ENA", "ETHFI",

    # Cosmos / modular ecosystem
    "INJ", "TIA", "SEI", "ATOM", "OSMO",

    # Solana DeFi
    "JUP", "JTO", "RAY",

    # AI / compute sector
    "WLD", "FET", "RENDER", "TAO",

    # Gaming / culture / metaverse
    "AXS", "SAND", "IMX", "GALA", "APE",

    # Decentralised storage / infrastructure
    "FIL", "GRT", "AR",

    # Oracle / cross-chain infrastructure
    "PYTH", "W",

    # RWA / newer liquid names
    "ONDO",

    # Other liquid names
    "ICP", "STX", "HBAR",
]

# ---------------------------------------------------------------------------
# Pinned pairs — always included regardless of discovery score.
# ---------------------------------------------------------------------------
# High-conviction manual choices (iconic, well-known spreads).
# leg_a / leg_b use the raw common name; get_discovery_universe() resolves aliases.
PINNED_PAIRS: list[dict] = [
    {"id": "BTC-ETH",       "leg_a": "BTC",  "leg_b": "ETH",   "label": "BTC / ETH",   "category": "layer1", "source": "pinned"},
    {"id": "SOL-ETH",       "leg_a": "SOL",  "leg_b": "ETH",   "label": "SOL / ETH",   "category": "layer1", "source": "pinned"},
    {"id": "SOL-BTC",       "leg_a": "SOL",  "leg_b": "BTC",   "label": "SOL / BTC",   "category": "layer1", "source": "pinned"},
    {"id": "WIF-kBONK",     "leg_a": "WIF",  "leg_b": "kBONK", "label": "WIF / kBONK", "category": "meme",   "source": "pinned"},
    {"id": "ENA-ETHFI",     "leg_a": "ENA",  "leg_b": "ETHFI", "label": "ENA / ETHFI", "category": "defi",   "source": "pinned"},
    {"id": "ARB-OP",        "leg_a": "ARB",  "leg_b": "OP",    "label": "ARB / OP",    "category": "layer2", "source": "pinned"},
]

# ---------------------------------------------------------------------------
# Cross-asset xyz: allowlist for pair momentum.
# ---------------------------------------------------------------------------
# Only symbols with >= 100 confirmed HL daily closes are included.
# These participate in pair momentum alongside crypto, generating crypto/xyz
# cross-asset pairs (e.g. BTC vs xyz:XYZ100).
#
# NOTE: xyz:QQQ does NOT exist on Hyperliquid.
#   xyz:XYZ100 is HL's Nasdaq-100 proxy (~$25k HL price; 150 closes).
#   It maps to ^NDX on Yahoo Finance.  It is NOT the same instrument as QQQ
#   (QQQ ~$500, ^NDX ~$22k, xyz:XYZ100 ~$25k) but tracks the same index.
#   Surface this in UI/commands as "XYZ100 (NDX proxy)" so users are aware.
#
# To add more symbols: verify >= 100 HL daily closes in xyz_debug.csv first.
CROSS_ASSET_ELIGIBLE: list[str] = [
    "xyz:XYZ100",  # Nasdaq-100 proxy — closest available to QQQ/NDX on HL
    "xyz:AAPL",    # Apple
    "xyz:NVDA",    # NVIDIA — high crypto correlation during AI cycles
    "xyz:TSLA",    # Tesla — historically correlated with risk-on crypto
    "xyz:MSFT",    # Microsoft
    "xyz:AMZN",    # Amazon
    "xyz:META",    # Meta
    "xyz:GOOGL",   # Alphabet
    "xyz:COIN",    # Coinbase — direct crypto-equity bridge
]

# Human-readable display names for cross-asset symbols.
# Used by UI/Telegram output to clarify proxy relationships.
CROSS_ASSET_DISPLAY_NAMES: dict[str, str] = {
    "xyz:XYZ100": "XYZ100 (NDX proxy)",
    "xyz:AAPL":   "AAPL",
    "xyz:NVDA":   "NVDA",
    "xyz:TSLA":   "TSLA",
    "xyz:MSFT":   "MSFT",
    "xyz:AMZN":   "AMZN",
    "xyz:META":   "META",
    "xyz:GOOGL":  "GOOGL",
    "xyz:COIN":   "COIN",
}


def get_cross_asset_universe() -> list[str]:
    """Return the curated xyz: symbol allowlist for cross-asset pair momentum."""
    return list(CROSS_ASSET_ELIGIBLE)


# ---------------------------------------------------------------------------
# Deny lists — hard exclusions.
# ---------------------------------------------------------------------------
DENYLIST_SYMBOLS: set[str] = set()
DENYLIST_PAIRS: set[frozenset] = set()

# ---------------------------------------------------------------------------
# Discovery thresholds
# ---------------------------------------------------------------------------
MAX_DISCOVERED_PAIRS = 15    # top N auto-discovered pairs kept for the scan
MIN_DISCOVERY_CORR   = 0.40  # minimum corr_168 for a pair to enter candidates
MIN_DISCOVERY_BARS   = 100   # minimum aligned 1h bars required

# Diversified ranking limits — applied to pair_momentum_diversified.csv only.
# Raw pair_momentum.csv is never affected.
MAX_PER_SHORT = 2   # max times the same asset may appear as the short leg
MAX_PER_LONG  = 2   # max times the same asset may appear as the long leg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_discovery_universe() -> list[str]:
    """
    Return the full set of symbols to fetch before running discovery.
    = DISCOVERY_ELIGIBLE (alias-resolved) + any symbols used exclusively in PINNED_PAIRS.
    Applies SYMBOL_ALIASES so callers always receive the correct HL contract names.
    """
    seen: set[str] = set()
    for sym in DISCOVERY_ELIGIBLE:
        seen.add(SYMBOL_ALIASES.get(sym, sym))
    for p in PINNED_PAIRS:
        seen.add(SYMBOL_ALIASES.get(p["leg_a"], p["leg_a"]))
        seen.add(SYMBOL_ALIASES.get(p["leg_b"], p["leg_b"]))
    # Exclude xyz: symbols — HL has no 1h or daily candles for them
    return sorted(s for s in seen if not s.startswith("xyz:"))
