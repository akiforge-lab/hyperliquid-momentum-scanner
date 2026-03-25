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
