# -*- coding: utf-8 -*-
"""
Fetch historical daily closing prices for every coin in the HL universe.

Cache format (data/candles/<COIN>.json):
  {
    "version": 2,
    "source":  "hyperliquid" | "yahoo:<SYM>" | "none",
    "closes":  [[timestamp_ms, price], ...],
    "failure_reason": null | "<string>"
  }

Old v1 format (plain list of candle dicts) is handled transparently.
Coin names that contain ':' (e.g. xyz:AAPL) are sanitised to '_' in filenames.
"""
import json
import logging
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from src.price_sources import (
    HL_DELAY,
    candles_to_series,
    candles_to_series_hourly,
    fetch_hl_candles,
    fetch_yahoo_closes,
    yahoo_symbol_for,
)
from src.paths import DAILY_CACHE_DIR, HOURLY_CACHE_DIR

LOOKBACK_DAYS  = 220
LOOKBACK_HOURS = 35 * 24   # 840 hourly bars (~35 days); enough for the 168-bar corr window
CACHE_VERSION  = 2

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def _coin_to_filename(coin: str) -> str:
    """Sanitise coin name for use as a file name (Windows forbids colons)."""
    return coin.replace(":", "_").replace("/", "_").replace("\\", "_")


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache(path: Path) -> Optional[dict]:
    """
    Load cache file. Returns None on missing/corrupt file.
    Handles both v1 (plain candle list) and v2 (dict) formats.
    """
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None

    # v1 compat: plain list of candle dicts
    if isinstance(data, list):
        series = candles_to_series(data)
        closes = [[int(ts.timestamp() * 1000), float(p)] for ts, p in series.items()]
        return {"version": 1, "source": "hyperliquid", "closes": closes, "failure_reason": None}

    return data if isinstance(data, dict) else None


def _save_cache(
    path: Path,
    source: str,
    closes: list,
    failure_reason: Optional[str] = None,
) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "version": CACHE_VERSION,
                    "source": source,
                    "closes": closes,
                    "failure_reason": failure_reason,
                },
                fh,
            )
    except OSError:
        pass


def _closes_to_series(closes: list) -> pd.Series:
    """Convert [[timestamp_ms, price], ...] to UTC-date-indexed pd.Series."""
    if not closes:
        return pd.Series(dtype=float)
    rows = []
    for item in closes:
        try:
            ts = pd.Timestamp(item[0], unit="ms", tz="UTC").normalize()
            rows.append((ts, float(item[1])))
        except (IndexError, ValueError, TypeError):
            continue
    if not rows:
        return pd.Series(dtype=float)
    return (
        pd.DataFrame(rows, columns=["date", "close"])
        .drop_duplicates("date")
        .set_index("date")["close"]
        .sort_index()
    )


# ---------------------------------------------------------------------------
# xyz diagnostic helpers
# ---------------------------------------------------------------------------

def _xyz_diag_entry(
    coin: str,
    hl_ok: bool,
    yahoo_sym_used: Optional[str],
    yahoo_ok: bool,
    n_closes: int,
    failure_stage: str,
    failure_reason: Optional[str],
) -> dict:
    """Build one row for the xyz_debug diagnostics list."""
    return {
        "coin":          coin,
        "hl_ok":         hl_ok,
        "yahoo_sym_used": yahoo_sym_used,
        "yahoo_ok":      yahoo_ok,
        "n_closes":      n_closes,
        "failure_stage": failure_stage,
        "failure_reason": failure_reason,
    }


def _failure_stage_from_cache(cached: dict) -> str:
    """Infer failure_stage label from a cached failure record."""
    reason = (cached.get("failure_reason") or "").lower()
    if "no yahoo" in reason or "no mapping" in reason or "no data from hyperliquid; no yahoo" in reason:
        return "yahoo_mapping_missing"
    if "yahoo finance" in reason or "yahoo" in reason:
        return "yahoo_no_data"
    return "yahoo_mapping_missing"


# ---------------------------------------------------------------------------
# Main fetch function
# ---------------------------------------------------------------------------

def _ms_now() -> int:
    return int(time.time() * 1000)


def fetch_all_prices(
    coins: list[str],
    no_cache: bool = False,
    strict_source: bool = False,
) -> tuple[dict[str, pd.Series], list[dict], list[dict]]:
    """
    Fetch daily close prices for every coin, with optional multi-source fallback.

    Args:
      coins        - list of coin names to fetch
      no_cache     - skip cache and re-fetch from APIs
      strict_source - when True, Yahoo Finance fallback is disabled for ALL coins
                      (both HL perps and xyz: assets).  Only Hyperliquid
                      candleSnapshot data is used.  This prevents silent "Yahoo
                      contamination" of the ranking universe.

    Returns:
      prices    - dict[coin -> pd.Series of closes]
      failures  - list of dicts with keys: coin, source_tried, reason
      xyz_diag  - list of per-xyz: coin diagnostic dicts (for xyz_debug.csv)
                  failure_stage values at fetch time:
                    "ok"                    - data obtained (may still fail history filter)
                    "yahoo_mapping_missing"  - HL failed; no Yahoo mapping defined
                    "yahoo_no_data"          - HL failed; Yahoo mapping tried but returned nothing
                    "yahoo_disabled_strict"  - HL failed; Yahoo skipped (strict source mode)
    """
    raw_dir = DAILY_CACHE_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)

    start_ms = _ms_now() - LOOKBACK_DAYS * 86_400_000
    end_ms   = _ms_now()

    prices:   dict[str, pd.Series] = {}
    failures: list[dict]           = []
    xyz_diag: list[dict]           = []
    total = len(coins)

    for idx, coin in enumerate(coins, 1):
        is_xyz     = coin.startswith("xyz:")
        fname      = _coin_to_filename(coin)
        cache_path = raw_dir / f"{fname}.json"

        # Tracking variables populated below and used to build xyz_diag entry
        hl_ok:         bool          = False
        yahoo_sym_used: Optional[str] = None
        yahoo_ok:      bool          = False
        n_closes:      int           = 0
        failure_stage: str           = "ok"
        failure_reason_str: Optional[str] = None

        # ----------------------------------------------------------------
        # 1. Try cache
        # ----------------------------------------------------------------
        if not no_cache:
            cached = _load_cache(cache_path)
            if cached is not None:
                cached_src      = cached.get("source", "")
                cached_is_yahoo = cached_src.startswith("yahoo:")
                # In strict_source mode: reject cached Yahoo results for non-xyz coins.
                # Without this, a previous non-strict scan's cached Yahoo data (e.g.
                # CANTO -> yahoo:CANTO-USD) would silently survive a strict-mode run.
                skip_yahoo_cache = strict_source and cached_is_yahoo and not is_xyz

                if cached_src != "none" and not skip_yahoo_cache:
                    series = _closes_to_series(cached.get("closes", []))
                    if not series.empty:
                        prices[coin] = series
                        n_closes = len(series)
                        if cached_src == "hyperliquid":
                            hl_ok = True
                        elif cached_is_yahoo:
                            hl_ok = False
                            yahoo_sym_used = cached_src[6:]
                            yahoo_ok = True
                        _progress(idx, total, coin, n_closes, cached_src)
                        if is_xyz:
                            xyz_diag.append(_xyz_diag_entry(
                                coin, hl_ok, yahoo_sym_used, yahoo_ok,
                                n_closes, "ok", None,
                            ))
                        continue
                elif cached_src == "none" and not skip_yahoo_cache:
                    # Previously confirmed no data
                    reason = cached.get("failure_reason") or "no data (cached result)"
                    failures.append({"coin": coin, "source_tried": "cache", "reason": reason})
                    _progress(idx, total, coin, 0, "no data")
                    if is_xyz:
                        xyz_diag.append(_xyz_diag_entry(
                            coin, False, None, False, 0,
                            _failure_stage_from_cache(cached), reason,
                        ))
                    continue
                # skip_yahoo_cache=True: fall through to live HL fetch below

        # ----------------------------------------------------------------
        # 2. Try Hyperliquid candleSnapshot
        # ----------------------------------------------------------------
        candles = fetch_hl_candles(coin, start_ms, end_ms)
        time.sleep(HL_DELAY)
        series = candles_to_series(candles)

        if not series.empty:
            hl_ok    = True
            n_closes = len(series)
            closes   = [[int(ts.timestamp() * 1000), float(p)] for ts, p in series.items()]
            _save_cache(cache_path, source="hyperliquid", closes=closes)
            prices[coin] = series
            _progress(idx, total, coin, n_closes, "hyperliquid")
            if is_xyz:
                xyz_diag.append(_xyz_diag_entry(
                    coin, True, None, False, n_closes, "ok", None,
                ))
            continue

        # ----------------------------------------------------------------
        # 3. Try Yahoo Finance fallback (skipped in strict_source mode)
        # ----------------------------------------------------------------
        if strict_source:
            # In strict source mode Yahoo is never used.  Every coin must come
            # from Hyperliquid candleSnapshot; if HL returned nothing, the coin
            # is excluded from rankings.
            failure_stage = "yahoo_disabled_strict"
            failure_reason_str = (
                "No data from Hyperliquid; Yahoo fallback disabled (--hyperliquid-only)"
            )
        else:
            yahoo_sym = yahoo_symbol_for(coin)
            if yahoo_sym:
                yahoo_sym_used = yahoo_sym
                yf_series = fetch_yahoo_closes(yahoo_sym, start_ms, end_ms)
                if not yf_series.empty:
                    yahoo_ok = True
                    n_closes = len(yf_series)
                    closes = [
                        [int(ts.timestamp() * 1000), float(p)]
                        for ts, p in yf_series.items()
                    ]
                    source_label = f"yahoo:{yahoo_sym}"
                    _save_cache(cache_path, source=source_label, closes=closes)
                    prices[coin] = yf_series
                    _progress(idx, total, coin, n_closes, source_label)
                    if is_xyz:
                        # Flag if this is an index proxy where HL and Yahoo prices differ
                        from src.xyz_symbols import asset_type as xyz_asset_type
                        if xyz_asset_type(coin) == "index":
                            failure_stage = "unsupported_index_proxy"
                            failure_reason_str = (
                                f"HL returned no data; using Yahoo proxy {yahoo_sym} "
                                f"(prices differ from HL oracle — treat data with caution)"
                            )
                            xyz_diag.append(_xyz_diag_entry(
                                coin, False, yahoo_sym, True, n_closes,
                                failure_stage, failure_reason_str,
                            ))
                        else:
                            xyz_diag.append(_xyz_diag_entry(
                                coin, False, yahoo_sym, True, n_closes, "ok", None,
                            ))
                    continue
                failure_stage = "yahoo_no_data"
                failure_reason_str = (
                    f"No data from Hyperliquid or Yahoo Finance (tried symbol: {yahoo_sym})"
                )
            else:
                failure_stage = "yahoo_mapping_missing"
                failure_reason_str = "No data from Hyperliquid; no Yahoo Finance mapping known"

        # ----------------------------------------------------------------
        # 4. Record failure
        # ----------------------------------------------------------------
        _save_cache(cache_path, source="none", closes=[], failure_reason=failure_reason_str)
        failures.append({
            "coin":         coin,
            "source_tried": yahoo_sym_used or "hyperliquid-only",
            "reason":       failure_reason_str,
        })
        logger.info("No data for %s - %s", coin, failure_reason_str)
        _progress(idx, total, coin, 0, "no data")
        if is_xyz:
            xyz_diag.append(_xyz_diag_entry(
                coin, False, yahoo_sym_used, False, 0,
                failure_stage, failure_reason_str,
            ))

    print()
    logger.info(
        "Prices obtained: %d/%d coins; %d with no data",
        len(prices), total, len(failures),
    )
    return prices, failures, xyz_diag


def _progress(idx: int, total: int, coin: str, n: int, source: str) -> None:
    tag = f"[{source}]" if n > 0 else "[no data]"
    print(f"  [{idx:>3}/{total}] {coin:<16} {n:>4} closes  {tag}", end="\r", flush=True)


# ---------------------------------------------------------------------------
# Hourly candle helpers (Pairs module only)
# ---------------------------------------------------------------------------

def _closes_to_series_hourly(closes: list) -> pd.Series:
    """
    Convert [[timestamp_ms, price], ...] to UTC-hour-indexed pd.Series.
    Used for hourly candle cache (data/candles_1h/).
    """
    if not closes:
        return pd.Series(dtype=float)
    rows = []
    for item in closes:
        try:
            ts = pd.Timestamp(item[0], unit="ms", tz="UTC").floor("h")
            rows.append((ts, float(item[1])))
        except (IndexError, ValueError, TypeError):
            continue
    if not rows:
        return pd.Series(dtype=float)
    return (
        pd.DataFrame(rows, columns=["ts", "close"])
        .drop_duplicates("ts")
        .set_index("ts")["close"]
        .sort_index()
    )


def fetch_all_prices_hourly(
    coins: list[str],
    no_cache: bool = False,
) -> tuple[dict[str, pd.Series], list[dict]]:
    """
    Fetch 1h close prices for the Pairs scanner.

    Key differences from fetch_all_prices():
      - Uses interval="1h" when calling Hyperliquid candleSnapshot
      - Cache stored in data/candles_1h/ (never overwrites daily cache)
      - NO Yahoo Finance fallback: hourly data is only available from HL
      - Returns (prices, failures) — no xyz_diag (not needed for pairs)

    Args:
      coins    - list of coin names (plain HL perp names or "xyz:..." names)
      no_cache - discard cached 1h candle data and re-fetch from HL

    Returns:
      prices   - dict[coin -> pd.Series of hourly closes, UTC-hour indexed]
      failures - list of dicts: {coin, source_tried, reason}
    """
    HOURLY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    end_ms   = _ms_now()
    start_ms = end_ms - LOOKBACK_HOURS * 3_600_000

    prices:   dict[str, pd.Series] = {}
    failures: list[dict]           = []
    total = len(coins)

    for idx, coin in enumerate(coins, 1):
        fname      = _coin_to_filename(coin)
        cache_path = HOURLY_CACHE_DIR / f"{fname}.json"

        # ----------------------------------------------------------------
        # 1. Try hourly cache
        # ----------------------------------------------------------------
        if not no_cache:
            cached = _load_cache(cache_path)
            if cached is not None:
                cached_src = cached.get("source", "")
                if cached_src == "hyperliquid":
                    series = _closes_to_series_hourly(cached.get("closes", []))
                    if not series.empty:
                        prices[coin] = series
                        _progress(idx, total, coin, len(series), "1h-cache")
                        continue
                elif cached_src == "none":
                    reason = cached.get("failure_reason") or "no HL 1h data (cached)"
                    failures.append({"coin": coin, "source_tried": "hl-1h", "reason": reason})
                    _progress(idx, total, coin, 0, "no data")
                    continue
                # Corrupt / partial cache → fall through to live fetch

        # ----------------------------------------------------------------
        # 2. Fetch from Hyperliquid (1h interval)
        # ----------------------------------------------------------------
        candles = fetch_hl_candles(coin, start_ms, end_ms, interval="1h")
        time.sleep(HL_DELAY)
        series = candles_to_series_hourly(candles)

        if not series.empty:
            closes = [[int(ts.timestamp() * 1000), float(p)] for ts, p in series.items()]
            _save_cache(cache_path, source="hyperliquid", closes=closes)
            prices[coin] = series
            _progress(idx, total, coin, len(series), "1h-hl")
            continue

        # ----------------------------------------------------------------
        # 3. Record failure (no Yahoo fallback for hourly data)
        # ----------------------------------------------------------------
        reason = "No 1h candle data from Hyperliquid (Yahoo does not provide hourly data)"
        _save_cache(cache_path, source="none", closes=[], failure_reason=reason)
        failures.append({"coin": coin, "source_tried": "hl-1h", "reason": reason})
        logger.info("No 1h data for %s", coin)
        _progress(idx, total, coin, 0, "no data")

    print()
    logger.info(
        "Hourly prices obtained: %d/%d coins; %d with no data",
        len(prices), total, len(failures),
    )
    return prices, failures
