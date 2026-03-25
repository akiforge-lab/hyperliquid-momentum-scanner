# -*- coding: utf-8 -*-
"""
Fetch the full Hyperliquid perpetual futures universe via the official API.
"""
import json
import logging
import requests

from src.paths import DATA_DIR

API_URL = "https://api.hyperliquid.xyz/info"
TIMEOUT = 30

logger = logging.getLogger(__name__)

_UNIVERSE_FILE = DATA_DIR / "universe.json"


def fetch_universe() -> list[str]:
    """
    Return a list of coin names in the Hyperliquid perp universe.
    Saves raw response to data/universe.json.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    payload = {"type": "meta"}
    try:
        resp = requests.post(API_URL, json=payload, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch universe: {exc}") from exc

    data = resp.json()

    # Persist raw response
    with open(_UNIVERSE_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    logger.debug("Saved raw universe data to data/universe.json")

    universe = data.get("universe", [])
    coins = [asset["name"] for asset in universe if "name" in asset]

    if not coins:
        raise ValueError(f"Universe is empty - check API response in {_UNIVERSE_FILE}")

    logger.info("Universe contains %d perp assets", len(coins))

    # Supplement with known xyz: tokenised real-world assets.
    # These are NOT in the {"type":"meta"} response but candleSnapshot works
    # with the full "xyz:<BASE>" coin identifier.
    from src.xyz_symbols import XYZ_UNIVERSE  # import here to avoid circular deps
    existing = set(coins)
    xyz_additions = [c for c in XYZ_UNIVERSE if c not in existing]
    if xyz_additions:
        logger.info("Supplementing universe with %d xyz: symbols", len(xyz_additions))
    coins = coins + xyz_additions

    logger.info("Total universe (perps + xyz:): %d assets", len(coins))
    return coins
