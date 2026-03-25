# -*- coding: utf-8 -*-
"""
Write pair-trading result files to output/.

Files written:
  output/pair_metrics.csv             - all computed pairs with metrics + signals
  output/pair_signals.csv             - actionable subset (LONG_SPREAD / SHORT_SPREAD only)
  output/pair_scan_summary.json       - machine-readable scan summary
  output/pair_universe_discovered.csv - full discovery candidate table (all statuses)
"""
import json
import logging
from datetime import datetime, timezone


import pandas as pd

from src.pair_signals import ENTRY_Z, EXIT_Z
from src.pair_metrics import (
    ZSCORE_WINDOW,
    MIN_PAIR_CLOSES,
    TIMEFRAME,
    HL_MEAN_REVERT_MIN,
    HL_MEAN_REVERT_MAX,
)
from src.paths import OUTPUT_DIR
logger = logging.getLogger(__name__)

ACTIVE_SIGNALS = {"LONG_SPREAD", "SHORT_SPREAD"}


def save_pair_results(
    df: pd.DataFrame,
    skipped: list[dict],
    n_configured: int,
    used_cache: bool = True,
    discovery_df: "pd.DataFrame | None" = None,
) -> dict:
    """
    Write all pair output files.  Returns the pair_scan_summary dict.

    Args:
      df            - signals DataFrame from compute_pair_signals()
      skipped       - list of skipped pair dicts
      n_configured  - total pairs attempted (discovered + pinned)
      used_cache    - whether 1h candles came from cache
      discovery_df  - full discovery candidate table from discover_pairs();
                      written to pair_universe_discovered.csv if provided

    Works correctly even when df is empty (all pairs skipped): writes empty
    CSVs with correct headers and a valid JSON summary — does not raise.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Metrics CSV — all computed pairs
    if df.empty:
        df.to_csv(OUTPUT_DIR / "pair_metrics.csv", index=False)
        signals_df = pd.DataFrame()
        signals_df.to_csv(OUTPUT_DIR / "pair_signals.csv", index=False)
    else:
        df.to_csv(OUTPUT_DIR / "pair_metrics.csv", index=False)
        signals_df = (
            df[df["mr_signal"].isin(ACTIVE_SIGNALS)]
            .sort_values("final_pair_score", ascending=False)
            .reset_index(drop=True)
        )
        signals_df.to_csv(OUTPUT_DIR / "pair_signals.csv", index=False)

    # Count signal breakdown
    def _count(col: str, val: str) -> int:
        return int((df[col] == val).sum()) if not df.empty and col in df.columns else 0

    n_computed   = len(df)
    n_long       = _count("mr_signal", "LONG_SPREAD")
    n_short      = _count("mr_signal", "SHORT_SPREAD")
    n_watch      = _count("mr_signal", "WATCH")
    n_neutral    = _count("mr_signal", "NEUTRAL")
    n_invalid    = _count("mr_signal", "INVALID")

    # ---- Discovery file + summary stats ----
    n_disc_total      = 0
    n_disc_discovered = 0
    n_disc_pinned     = 0
    n_disc_excluded   = 0
    if discovery_df is not None and not discovery_df.empty:
        from src.pair_discovery import write_discovery_output
        write_discovery_output(discovery_df)
        n_disc_total      = len(discovery_df)
        n_disc_discovered = int((discovery_df["status"] == "discovered").sum())
        n_disc_pinned     = int((discovery_df["status"] == "pinned").sum())
        n_disc_excluded   = int(discovery_df["status"].str.startswith("excluded").sum())

    summary: dict = {
        "scan_timestamp":           datetime.now(timezone.utc).isoformat(),
        "timeframe":                TIMEFRAME,
        "used_cache":               used_cache,
        "pairs_configured":         n_configured,
        "pairs_computed":           n_computed,
        "pairs_skipped":            len(skipped),
        "long_spread":              n_long,
        "short_spread":             n_short,
        "watch":                    n_watch,
        "neutral":                  n_neutral,
        "invalid":                  n_invalid,
        "entry_zscore_threshold":   ENTRY_Z,
        "exit_zscore_threshold":    EXIT_Z,
        "zscore_window_bars":       ZSCORE_WINDOW,
        "min_pair_closes":          MIN_PAIR_CLOSES,
        "half_life_range_hours":    [HL_MEAN_REVERT_MIN, HL_MEAN_REVERT_MAX],
        "skipped_pairs":            skipped,
        # Discovery stats (populated when auto-discovery is used)
        "discovery_candidates_total":    n_disc_total,
        "discovery_auto_selected":       n_disc_discovered,
        "discovery_pinned":              n_disc_pinned,
        "discovery_excluded":            n_disc_excluded,
    }

    with open(OUTPUT_DIR / "pair_scan_summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    logger.info("Pair output written to %s/", OUTPUT_DIR)
    print(f"  pair_metrics.csv             ({n_computed} rows)")
    print(f"  pair_signals.csv             ({len(signals_df)} active signals)")
    if discovery_df is not None:
        print(f"  pair_universe_discovered.csv ({n_disc_total} candidates, "
              f"{n_disc_discovered} selected, {n_disc_excluded} excluded)")
    print(f"  pair_scan_summary.json")

    return summary
