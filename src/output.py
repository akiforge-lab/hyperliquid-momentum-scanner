# -*- coding: utf-8 -*-
"""
Save results to output/ and print a console summary.

Files written:
  output/all_rankings.csv
  output/top_longs.csv
  output/top_shorts.csv
  output/missing_symbols.csv
  output/scan_summary.json
  output/xyz_debug.csv        (xyz: asset diagnostics with momentum metrics)
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from src.xyz_symbols import asset_type as xyz_asset_type, XYZ_UNIVERSE
from src.paths import OUTPUT_DIR
TOP_N = 10

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _top_longs(df: pd.DataFrame, n: int = TOP_N) -> pd.DataFrame:
    return (
        df[df["signal"] == "LONG"]
        .sort_values("momentum_score", ascending=False)
        .head(n)
        .reset_index(drop=True)
    )


def _top_shorts(df: pd.DataFrame, n: int = TOP_N) -> pd.DataFrame:
    return (
        df[df["signal"] == "SHORT"]
        .sort_values("momentum_score", ascending=True)   # most negative first
        .head(n)
        .reset_index(drop=True)
    )


def _fmt_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "  (none)"
    cols = ["coin", "latest_price", "slope_ann_pct", "r2", "momentum_score", "ma100", "source"]
    display = df[[c for c in cols if c in df.columns]].copy()
    display.index = range(1, len(display) + 1)
    return display.to_string()


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

def print_results(
    df: pd.DataFrame,
    universe_size: int,
    n_with_data: int,
    n_sufficient: int,
    n_missing_mapping: int,
) -> None:
    longs  = _top_longs(df)
    shorts = _top_shorts(df)
    sep = "=" * 72

    print(f"\n{sep}")
    print("HYPERLIQUID MOMENTUM SCANNER")
    print(sep)
    print(f"  Universe size (perps + xyz:)    : {universe_size}")
    print(f"  Assets with price data          : {n_with_data}")
    print(f"  Assets with >={100} daily closes : {n_sufficient}")
    print(f"  Assets excluded (no data)       : {n_missing_mapping}")
    print(f"  Assets computed                 : {len(df)}")
    print(f"  LONG candidates                 : {len(df[df['signal'] == 'LONG'])}")
    print(f"  SHORT candidates                : {len(df[df['signal'] == 'SHORT'])}")
    print(sep)

    print(f"\nTOP {TOP_N} LONG CANDIDATES")
    print("  (slope > 0, R2 > 0.5, price > 100-DMA)")
    print(_fmt_table(longs))

    print(f"\nTOP {TOP_N} SHORT CANDIDATES")
    print("  (slope < 0, R2 > 0.5, price < 100-DMA)")
    print(_fmt_table(shorts))
    print()


# ---------------------------------------------------------------------------
# xyz_debug.csv builder
# ---------------------------------------------------------------------------

def _build_xyz_debug(
    xyz_diag: list[dict],
    df: pd.DataFrame,
    skipped_hist: list[dict],
) -> pd.DataFrame:
    """
    Build the xyz_debug DataFrame from raw fetch diagnostics, enriched with
    momentum metrics for coins that made it into the rankings.

    Columns returned:
      hyperliquid_symbol, parsed_base_symbol, asset_type_guess,
      hyperliquid_fetch_ok, yahoo_symbol_used, yahoo_fetch_ok,
      number_of_closes, failure_stage, exclusion_reason,
      slope_ann_pct, r2, momentum_score, signal

    failure_stage values (final, after history filter):
      in_rankings                   - passed all filters; appears in rankings
      insufficient_history_after_fetch - price data found but < MIN_CLOSES
      yahoo_no_data                 - HL failed; Yahoo tried but returned nothing
      yahoo_mapping_missing         - HL failed; no Yahoo mapping defined
      yahoo_disabled_strict         - HL failed; Yahoo skipped (strict source mode)
      unsupported_index_proxy       - index; Yahoo proxy used (prices differ from HL)
    """
    if not xyz_diag:
        return pd.DataFrame()

    # Build lookups for efficient join
    ranked_coins  = set(df["coin"]) if not df.empty else set()
    skipped_coins = {s["coin"]: s for s in skipped_hist}

    # Momentum metrics keyed by coin (for xyz coins in rankings)
    xyz_in_df: dict[str, dict] = {}
    if not df.empty:
        for _, row in df[df["coin"].str.startswith("xyz:")].iterrows():
            xyz_in_df[row["coin"]] = {
                "slope_ann_pct":  row.get("slope_ann_pct"),
                "r2":             row.get("r2"),
                "momentum_score": row.get("momentum_score"),
                "signal":         row.get("signal"),
            }

    rows = []
    for entry in xyz_diag:
        coin   = entry["coin"]
        stage  = entry.get("failure_stage", "ok")
        reason = entry.get("failure_reason") or ""

        # Promote transitional "ok" to final outcome
        if stage == "ok":
            if coin in ranked_coins:
                stage  = "in_rankings"
                reason = ""
            elif coin in skipped_coins:
                stage  = "insufficient_history_after_fetch"
                n      = entry.get("n_closes", 0)
                reason = f"Only {n} daily closes (need >= 100)"

        # Momentum columns — populated only for coins in rankings
        metrics = xyz_in_df.get(coin, {})

        rows.append({
            "hyperliquid_symbol":   coin,
            "parsed_base_symbol":   coin[4:] if coin.startswith("xyz:") else coin,
            "asset_type_guess":     xyz_asset_type(coin),
            "hyperliquid_fetch_ok": entry.get("hl_ok", False),
            "yahoo_symbol_used":    entry.get("yahoo_sym_used") or "",
            "yahoo_fetch_ok":       entry.get("yahoo_ok", False),
            "number_of_closes":     entry.get("n_closes", 0),
            "failure_stage":        stage,
            "exclusion_reason":     reason,
            "slope_ann_pct":        metrics.get("slope_ann_pct"),
            "r2":                   metrics.get("r2"),
            "momentum_score":       metrics.get("momentum_score"),
            "signal":               metrics.get("signal") or "",
        })

    # Ensure every XYZ_UNIVERSE symbol appears even if missing from xyz_diag
    seen = {r["hyperliquid_symbol"] for r in rows}
    for coin in XYZ_UNIVERSE:
        if coin not in seen:
            rows.append({
                "hyperliquid_symbol":   coin,
                "parsed_base_symbol":   coin[4:],
                "asset_type_guess":     xyz_asset_type(coin),
                "hyperliquid_fetch_ok": False,
                "yahoo_symbol_used":    "",
                "yahoo_fetch_ok":       False,
                "number_of_closes":     0,
                "failure_stage":        "yahoo_mapping_missing",
                "exclusion_reason":     "Symbol not processed (not in fetched universe)",
                "slope_ann_pct":        None,
                "r2":                   None,
                "momentum_score":       None,
                "signal":               "",
            })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------

def save_results(
    df: pd.DataFrame,
    failures: list[dict],
    skipped_hist: list[dict],
    universe_size: int,
    n_with_data: int,
    n_sufficient: int,
    xyz_diag: Optional[list[dict]] = None,
    source_breakdown: Optional[dict[str, int]] = None,
    strict_source: bool = False,
    used_cache: bool = True,
) -> dict:
    """
    Write all output files. Returns the scan_summary dict.

    n_sufficient should be (n_with_data - len(skipped_hist)), i.e. the count of
    assets that had >= MIN_CLOSES closes and actually entered momentum computation.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    n_long  = int((df["signal"] == "LONG").sum())
    n_short = int((df["signal"] == "SHORT").sum())

    # all_rankings.csv — includes source column for provenance
    df.to_csv(OUTPUT_DIR / "all_rankings.csv", index=False)

    # top_longs.csv
    _top_longs(df).to_csv(OUTPUT_DIR / "top_longs.csv", index=False)

    # top_shorts.csv
    _top_shorts(df).to_csv(OUTPUT_DIR / "top_shorts.csv", index=False)

    # xyz_rankings.csv — all xyz: coins present in computed rankings,
    # sorted by |momentum_score| so the dashboard can render them as a
    # unified strength table (same pattern as the main momentum rankings).
    xyz_rank_df = df[df["coin"].str.startswith("xyz:", na=False)].copy()
    if not xyz_rank_df.empty:
        xyz_rank_df["_abs"] = xyz_rank_df["momentum_score"].abs()
        xyz_rank_df = xyz_rank_df.sort_values("_abs", ascending=False).drop(columns=["_abs"])
    _xyz_save_cols = [c for c in ["coin", "slope_ann_pct", "r2", "momentum_score", "signal", "n_closes"]
                      if c in xyz_rank_df.columns]
    xyz_rank_df[_xyz_save_cols].to_csv(OUTPUT_DIR / "xyz_rankings.csv", index=False)

    # missing_symbols.csv
    missing_rows = []
    for f in failures:
        missing_rows.append({
            "coin":         f["coin"],
            "category":     "no_price_data",
            "source_tried": f.get("source_tried", ""),
            "reason":       f.get("reason", ""),
        })
    for s in skipped_hist:
        missing_rows.append({
            "coin":         s["coin"],
            "category":     "insufficient_history",
            "source_tried": "",
            "reason":       s.get("reason", ""),
        })
    missing_df = pd.DataFrame(missing_rows)
    missing_df.to_csv(OUTPUT_DIR / "missing_symbols.csv", index=False)

    # xyz_debug.csv
    xyz_debug_df = _build_xyz_debug(xyz_diag or [], df, skipped_hist)
    xyz_debug_df.to_csv(OUTPUT_DIR / "xyz_debug.csv", index=False)
    n_xyz         = len(xyz_debug_df)
    n_xyz_data    = int((xyz_debug_df["number_of_closes"] > 0).sum()) if n_xyz else 0
    n_xyz_suff    = int((xyz_debug_df["number_of_closes"] >= 100).sum()) if n_xyz else 0
    n_xyz_ranked  = int((xyz_debug_df["failure_stage"] == "in_rankings").sum()) if n_xyz else 0
    n_xyz_missing = n_xyz - n_xyz_ranked

    # scan_summary.json
    summary: dict = {
        "scan_timestamp":                datetime.now(timezone.utc).isoformat(),
        "used_cache":                    used_cache,
        "strict_source_mode":            strict_source,
        "universe_size":                 universe_size,
        "assets_with_data":              n_with_data,
        # BUG FIX: this was previously passed as len(df)+len(skipped_hist) which
        # equals n_with_data, not the actual count of assets with >=100 closes.
        "assets_with_sufficient_history": n_sufficient,
        "assets_excluded_no_data":       len(failures),
        "assets_excluded_short_history": len(skipped_hist),
        "assets_computed":               len(df),
        "long_candidates":               n_long,
        "short_candidates":              n_short,
        "source_breakdown":              source_breakdown or {},
        "xyz_total":                     n_xyz,
        "xyz_with_price_data":           n_xyz_data,
        "xyz_with_sufficient_history":   n_xyz_suff,
        "xyz_in_rankings":               n_xyz_ranked,
        "xyz_missing_count":             n_xyz_missing,
    }
    with open(OUTPUT_DIR / "scan_summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    logger.info("Output written to %s/", OUTPUT_DIR)
    print(f"Output saved to {OUTPUT_DIR}/")
    print(f"  all_rankings.csv      ({len(df)} rows)")
    print(f"  top_longs.csv         ({n_long} rows)")
    print(f"  top_shorts.csv        ({n_short} rows)")
    print(f"  missing_symbols.csv   ({len(missing_rows)} rows)")
    print(f"  xyz_rankings.csv      ({len(xyz_rank_df)} xyz: symbols in rankings)")
    print(f"  xyz_debug.csv         ({n_xyz} xyz: symbols; {n_xyz_ranked} in rankings)")
    print(f"  scan_summary.json")

    return summary
