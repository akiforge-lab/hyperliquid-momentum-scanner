# -*- coding: utf-8 -*-
"""
Hyperliquid Momentum Scanner - CLI entry point and importable module.

As a script:
  python main.py [--no-cache] [--verbose] [--hyperliquid-only]

As a module (used by app.py):
  from main import run_scan
  summary = run_scan(no_cache=False, strict_source=False)
"""
import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

import pandas as pd

from src.paths import OUTPUT_DIR, DAILY_CACHE_DIR, HOURLY_CACHE_DIR, DATA_DIR

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from src.fetch_universe import fetch_universe
from src.fetch_prices import fetch_all_prices, fetch_all_prices_hourly, _coin_to_filename
from src.compute_momentum import compute_momentum, MIN_CLOSES
from src.output import print_results, save_results
from src.pair_config import PINNED_PAIRS, get_discovery_universe
from src.pair_discovery import discover_pairs
from src.pair_metrics import compute_pair_metrics, TIMEFRAME as PAIRS_TIMEFRAME
from src.pair_signals import compute_pair_signals
from src.pair_output import save_pair_results


def run_risk_overlay_scan() -> dict:
    """
    Collect the crypto risk overlay (DVOL, funding stress, OI stress).

    Runs BEFORE the main momentum scan so regime context is visible alongside
    trade signals.  Does NOT modify momentum_score or any pipeline output.

    Writes:
      output/risk_overlay.json  — current snapshot
      data/risk_history.json    — rolling history for z-score windows

    Returns the overlay dict.
    """
    from src.risk_overlay import run_risk_overlay
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    return run_risk_overlay()


def run_scan(
    no_cache: bool = False,
    verbose: bool = False,
    strict_source: bool = False,
) -> dict:
    """
    Run the full momentum scan pipeline.

    Args:
      no_cache     – discard cached candle data and re-fetch from APIs
      verbose      – enable DEBUG logging
      strict_source – when True, Yahoo Finance fallback is disabled for all
                      coins; only Hyperliquid candleSnapshot data is used.
                      Assets that cannot produce >=MIN_CLOSES candles from HL
                      alone are excluded from rankings.

    Returns the scan_summary dict written to output/scan_summary.json.
    Raises on fatal errors (e.g. universe API down, no assets pass filter).
    """
    DATA_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    candle_cache = DAILY_CACHE_DIR
    if no_cache and candle_cache.exists():
        print("--no-cache: clearing cached candle data...", flush=True)
        shutil.rmtree(candle_cache)

    # ------------------------------------------------------------------
    # Step 1 - Universe
    # ------------------------------------------------------------------
    print("Step 1/3  Fetching Hyperliquid perpetual futures universe...", flush=True)
    coins = fetch_universe()
    universe_size = len(coins)
    print(f"          {universe_size} assets in universe", flush=True)
    if strict_source:
        print("          [strict source mode] Yahoo Finance fallback is DISABLED", flush=True)

    # ------------------------------------------------------------------
    # Step 2 - Historical prices
    # ------------------------------------------------------------------
    print(f"Step 2/3  Fetching daily closes (>={MIN_CLOSES} required)...", flush=True)
    prices, failures, xyz_diag = fetch_all_prices(
        coins,
        no_cache=no_cache,
        strict_source=strict_source,
    )

    n_with_data = len(prices)
    n_no_data   = len(failures)
    print(
        f"          {n_with_data}/{universe_size} assets have data  |  {n_no_data} with no data",
        flush=True,
    )

    # Build source map (inferred from cache files written by fetch_all_prices)
    sources: dict[str, str] = {}
    cache_dir = DAILY_CACHE_DIR
    if cache_dir.exists():
        for coin in prices:
            path = cache_dir / f"{_coin_to_filename(coin)}.json"
            try:
                with open(path, encoding="utf-8") as fh:
                    d = json.load(fh)
                sources[coin] = d.get("source", "unknown") if isinstance(d, dict) else "hyperliquid"
            except Exception:
                sources[coin] = "unknown"

    # Source breakdown (for scan_summary.json)
    source_breakdown: dict[str, int] = {}
    for coin in prices:
        src = sources.get(coin, "unknown")
        if src == "hyperliquid":
            key = "hyperliquid"
        elif src.startswith("yahoo:"):
            key = "yahoo"
        else:
            key = "other"
        source_breakdown[key] = source_breakdown.get(key, 0) + 1

    # ------------------------------------------------------------------
    # Step 3 - Compute momentum
    # ------------------------------------------------------------------
    print("Step 3/3  Computing momentum scores...", flush=True)
    df, skipped_hist = compute_momentum(prices, sources=sources)

    # FIX: n_sufficient = coins that passed the >=MIN_CLOSES filter.
    # Previously this was computed as len(df) + len(skipped_hist) which
    # equals n_with_data (every asset in prices is either computed or skipped),
    # causing the summary to show 235 instead of 209.
    n_sufficient = n_with_data - len(skipped_hist)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    print_results(
        df,
        universe_size=universe_size,
        n_with_data=n_with_data,
        n_sufficient=n_sufficient,
        n_missing_mapping=n_no_data,
    )
    summary = save_results(
        df,
        failures=failures,
        skipped_hist=skipped_hist,
        universe_size=universe_size,
        n_with_data=n_with_data,
        n_sufficient=n_sufficient,
        xyz_diag=xyz_diag,
        source_breakdown=source_breakdown,
        strict_source=strict_source,
        used_cache=not no_cache,
    )
    return summary


def run_pairs_scan(
    no_cache: bool = False,
    verbose: bool = False,
    strict_source: bool = False,
) -> dict:
    """
    Run the pairs trading scan pipeline using 1-hour candles.

    Pipeline:
      1. Fetch 1h candles for the discovery universe (~40 liquid crypto symbols)
         using a SEPARATE cache (data/candles_1h/) — daily cache is untouched.
      2. Auto-discover the best pairs via correlation / half-life scoring.
         PINNED_PAIRS are always included.  DENYLIST_* are always excluded.
         Top MAX_DISCOVERED_PAIRS auto pairs are selected.
      3. Run full pair metrics + signals only on the selected pairs.
      4. Write outputs (pair_metrics.csv, pair_signals.csv,
         pair_universe_discovered.csv, pair_scan_summary.json).

    Args:
      no_cache      – discard cached 1h candle data and re-fetch from HL
      verbose       – enable DEBUG logging
      strict_source – reserved for future use; ignored (Yahoo never used for 1h)

    Returns the pair_scan_summary dict.
    """
    HOURLY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    discovery_coins = get_discovery_universe()
    print(
        f"Pairs Step 1/3  Fetching {PAIRS_TIMEFRAME} candles for "
        f"{len(discovery_coins)} symbols (data/candles_1h/ cache)...",
        flush=True,
    )

    # fetch_all_prices_hourly only uses HL candleSnapshot interval="1h"
    # and caches to data/candles_1h/ — the daily Momentum cache is never touched.
    prices, failures = fetch_all_prices_hourly(
        discovery_coins,
        no_cache=no_cache,
    )

    missing = [f["coin"] for f in failures]
    if missing:
        print(f"               No 1h data for: {', '.join(missing)}", flush=True)
    print(f"               Got data for {len(prices)}/{len(discovery_coins)} symbols", flush=True)

    # ----------------------------------------------------------------
    # Step 2: Auto-discover best pairs from available 1h data
    # ----------------------------------------------------------------
    print("Pairs Step 2/3  Discovering pairs...", flush=True)
    selected_pairs, discovery_df = discover_pairs(prices)

    print(
        f"Pairs Step 3/3  Computing metrics for {len(selected_pairs)} pairs "
        f"({len(PINNED_PAIRS)} pinned)...",
        flush=True,
    )
    metrics_df, skipped = compute_pair_metrics(prices, selected_pairs)

    signals_df = compute_pair_signals(metrics_df)
    summary = save_pair_results(
        signals_df,
        skipped=skipped,
        n_configured=len(selected_pairs),
        used_cache=not no_cache,
        discovery_df=discovery_df,
    )
    return summary


def run_pair_momentum_scan(
    no_cache: bool = False,
    verbose:  bool = False,
) -> dict:
    """
    Run the Pair Momentum scan.

    Uses the SAME daily candle cache as the Momentum scanner (data/candles/).
    Regression window, annualisation, and scoring are identical to the
    single-asset Momentum scanner — only the input series differs:
      Momentum      → log(price_USD)
      Pair Momentum → log(price_A) - log(price_B)

    Pipeline:
      1. Fetch daily candles for the discovery universe via fetch_all_prices().
      2. Compute log_ratio momentum scores for all C(n,2) pair combinations.
      3. Keep top 30 by |momentum_score|.
      4. Write output/pair_momentum.csv + output/pair_momentum_summary.json.

    Returns the summary dict.
    """
    from src.pair_momentum import compute_pair_momentum, save_pair_momentum

    DAILY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    all_coins = fetch_universe()   # full HL perps + xyz: (same source as run_scan)
    n_coins = len(all_coins)
    n_pairs_max = n_coins * (n_coins - 1) // 2
    print(
        f"Pair Momentum 1/2  Fetching daily candles for {n_coins} symbols "
        f"(up to {n_pairs_max} candidate pairs)...",
        flush=True,
    )

    prices, failures, _xyz_diag = fetch_all_prices(all_coins, no_cache=no_cache)
    missing_coins = [f["coin"] for f in failures]
    if missing_coins:
        print(f"                   No daily data for: {', '.join(missing_coins)}", flush=True)
    n_crypto = sum(1 for s in prices if not s.startswith("xyz:"))
    n_xyz    = sum(1 for s in prices if s.startswith("xyz:"))
    print(
        f"                   Got data for {len(prices)}/{n_coins} symbols "
        f"({n_crypto} crypto + {n_xyz} xyz)",
        flush=True,
    )

    # Write pair_universe_debug.csv — single loop, kind inferred from symbol prefix.
    _failures_map = {f["coin"]: f.get("reason", "no_data") for f in failures}
    _debug_rows = []
    for sym in all_coins:
        kind = "cross_asset" if sym.startswith("xyz:") else "crypto"
        if sym in prices:
            _debug_rows.append({"symbol": sym, "status": "included",
                                 "bars": len(prices[sym]), "reason": "", "kind": kind})
        else:
            _debug_rows.append({"symbol": sym, "status": "excluded", "bars": 0,
                                 "reason": _failures_map.get(sym, "no_data"), "kind": kind})
    pd.DataFrame(_debug_rows).to_csv(OUTPUT_DIR / "pair_universe_debug.csv", index=False)
    print(
        f"                   pair_universe_debug.csv  "
        f"({len(prices)} included, {len(missing_coins)} excluded)",
        flush=True,
    )

    n_syms = len(prices)
    print(
        f"Pair Momentum 2/2  Computing daily momentum scores for "
        f"{n_syms*(n_syms-1)//2} pairs "
        f"({n_syms} symbols: {n_crypto} crypto + {n_xyz} xyz)...",
        flush=True,
    )
    df = compute_pair_momentum(prices)
    summary = save_pair_momentum(df)
    print(
        f"                   {summary['pairs_computed']} pairs kept  "
        f"(+score: {summary['positive_score']}  -score: {summary['negative_score']})",
        flush=True,
    )
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Hyperliquid Perpetuals Momentum Scanner")
    p.add_argument("--no-cache", action="store_true",
                   help="Discard cached candle data and re-fetch from APIs")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable debug logging")
    p.add_argument("--hyperliquid-only", action="store_true",
                   help=(
                       "Strict source mode: disable Yahoo Finance fallback for all coins. "
                       "Only Hyperliquid candleSnapshot data is used. Assets that cannot "
                       "reach >=100 closes from HL alone are excluded from rankings."
                   ))
    p.add_argument("--pairs-only", action="store_true",
                   help=(
                       "Run only the pairs scan (skips single-asset momentum scan). "
                       "Reuses cached candle data unless --no-cache is also set."
                   ))
    p.add_argument("--risk-overlay", action="store_true",
                   help=(
                       "Collect crypto risk overlay signals (Deribit DVOL, HL funding "
                       "stress, OI change) before the main scan.  Writes "
                       "output/risk_overlay.json and data/risk_history.json.  "
                       "Does not modify momentum scores."
                   ))
    return p.parse_args()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    # Use stdout so logging and print() share the same stream and stay ordered.
    logging.basicConfig(
        format="%(asctime)s  %(levelname)-7s  %(name)s - %(message)s",
        datefmt="%H:%M:%S",
        level=level,
        stream=sys.stdout,
    )
    if not verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("yfinance").setLevel(logging.WARNING)
        logging.getLogger("peewee").setLevel(logging.WARNING)


if __name__ == "__main__":
    args = _parse_args()
    _setup_logging(args.verbose)
    if args.pairs_only:
        run_pairs_scan(
            no_cache=args.no_cache,
            verbose=args.verbose,
            strict_source=args.hyperliquid_only,
        )
    else:
        # Risk overlay runs first so regime context precedes trade signals.
        # btc_rv will be None on ephemeral runners (no candle cache yet);
        # risk_score falls back to funding_z and oi_z in that case.
        if args.risk_overlay:
            run_risk_overlay_scan()

        run_scan(
            no_cache=args.no_cache,
            verbose=args.verbose,
            strict_source=args.hyperliquid_only,
        )
        # Run pair momentum after main scan.
        # Always pass no_cache=False: run_scan() already cleared and rebuilt
        # data/candles/ when --no-cache was set, so the cache is warm here.
        run_pair_momentum_scan(no_cache=False, verbose=args.verbose)
