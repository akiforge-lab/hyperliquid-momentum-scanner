# -*- coding: utf-8 -*-
"""
Pair Momentum Scanner.

For each unordered pair (A, B) drawn from the discovery universe, computes:

    log_ratio = log(price_A) - log(price_B)

Then applies the SAME slope/R² momentum model used by the single-asset
Momentum scanner (src/compute_momentum.py) — only the input series differs:

    Momentum      uses  log(price_USD)
    Pair Momentum uses  log(price_A) - log(price_B)

Uses DAILY candles (shared cache: data/candles/).
Regression window, annualisation, and scoring are identical to Momentum.

Direction:
    score > 0  →  A/B ratio rising  →  Long A, Short B, display: A-B
    score < 0  →  A/B ratio falling →  Long B, Short A, display: B-A

Top TOP_N pairs by |momentum_score| are kept.
"""
import json
import logging
from itertools import combinations
import numpy as np
import pandas as pd
from scipy import stats

from src.paths import OUTPUT_DIR

MIN_BARS         = 100   # minimum aligned daily bars required (matches Momentum MIN_CLOSES)
SLOPE_WINDOW     = 100   # regression window in days (matches Momentum scanner)
TRADING_DAYS     = 252   # annualisation factor (matches compute_momentum._annualise)
TOP_N            = 30    # keep top N pairs by |momentum_score|
TIMEFRAME        = "1d"
logger     = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _regress(series: pd.Series) -> tuple[float, float]:
    """
    Fit log_ratio = a + b*t over the last SLOPE_WINDOW bars.
    Returns (slope_per_bar, r_squared); both NaN if insufficient data.
    Mirrors compute_momentum._regress() exactly.
    """
    window = series.iloc[-SLOPE_WINDOW:]
    if len(window) < SLOPE_WINDOW:
        return float("nan"), float("nan")
    t = np.arange(len(window), dtype=float)
    slope, _, r_value, _, _ = stats.linregress(t, window.values)
    return slope, r_value ** 2


def _annualise(slope_per_day: float) -> float:
    """Annualise a per-bar slope from daily data → % per year.
    Identical to compute_momentum._annualise()."""
    return slope_per_day * TRADING_DAYS * 100


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------

def compute_pair_momentum(prices: dict[str, pd.Series]) -> pd.DataFrame:
    """
    Compute momentum score for all C(n,2) pairs from the prices dict.

    Args:
      prices -- {symbol: pd.Series with daily datetime index}

    Returns:
      DataFrame of top TOP_N pairs by |momentum_score|, sorted by
      momentum_score descending (strongest positive → most negative).
      Empty DataFrame if no pairs pass the minimum-bars filter.
    """
    symbols = sorted(prices.keys())
    records: list[dict] = []
    skipped = 0

    for sym_a, sym_b in combinations(symbols, 2):
        s_a = prices[sym_a].replace([np.inf, -np.inf], np.nan).dropna()
        s_b = prices[sym_b].replace([np.inf, -np.inf], np.nan).dropna()

        # Align on common timestamps
        aligned = pd.DataFrame({"a": s_a, "b": s_b}).dropna()
        n = len(aligned)
        if n < MIN_BARS:
            skipped += 1
            continue

        # Log-ratio series: log(A) - log(B)
        with np.errstate(invalid="ignore", divide="ignore"):
            log_ratio = (
                np.log(aligned["a"].values.astype(float))
                - np.log(aligned["b"].values.astype(float))
            )
        log_ratio_s = pd.Series(log_ratio, index=aligned.index)
        log_ratio_s = log_ratio_s.replace([np.inf, -np.inf], np.nan).dropna()

        if len(log_ratio_s) < SLOPE_WINDOW:
            skipped += 1
            continue

        slope, r2 = _regress(log_ratio_s)
        if not np.isfinite(slope) or not np.isfinite(r2):
            skipped += 1
            continue

        slope_ann_pct  = _annualise(slope)
        momentum_score = round(slope_ann_pct * r2, 4)

        # Direction: positive score → A outperforming B → Long A / Short B
        if momentum_score >= 0:
            long_asset  = sym_a
            short_asset = sym_b
        else:
            long_asset  = sym_b
            short_asset = sym_a

        records.append({
            "pair_id":        f"{sym_a}-{sym_b}",   # canonical order (stable ID)
            "long_asset":     long_asset,
            "short_asset":    short_asset,
            "display_pair":   f"{long_asset}-{short_asset}",
            "trade_label":    f"Long {long_asset} / Short {short_asset}",
            "slope_ann_pct":  round(slope_ann_pct, 2),
            "r2":             round(r2, 4),
            "momentum_score": momentum_score,
            "bars_used":      n,
            "timeframe":      TIMEFRAME,
        })

    logger.info(
        "Pair momentum: %d total pairs evaluated, %d skipped (<min bars), "
        "%d passed; keeping top %d by |score|.",
        len(records) + skipped, skipped, len(records), TOP_N,
    )

    if not records:
        logger.warning("No pairs passed the minimum-bars filter.")
        return pd.DataFrame(columns=[
            "pair_id", "long_asset", "short_asset", "display_pair",
            "trade_label", "slope_ann_pct", "r2", "momentum_score",
            "bars_used", "timeframe",
        ])

    df = pd.DataFrame(records)
    # Keep top N by |score|, then sort for display: strongest positive first
    df["_abs"] = df["momentum_score"].abs()
    df = df.nlargest(TOP_N, "_abs").drop(columns=["_abs"])
    df.sort_values("momentum_score", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_pair_momentum(df: pd.DataFrame) -> dict:
    """Write output/pair_momentum.csv and output/pair_momentum_summary.json."""
    OUTPUT_DIR.mkdir(exist_ok=True)

    csv_path = OUTPUT_DIR / "pair_momentum.csv"
    df.to_csv(csv_path, index=False)
    print(f"  pair_momentum.csv ({len(df)} pairs)", flush=True)

    n_pos = int((df["momentum_score"] > 0).sum()) if not df.empty else 0
    n_neg = int((df["momentum_score"] < 0).sum()) if not df.empty else 0

    summary = {
        "pairs_computed":    len(df),
        "positive_score":    n_pos,
        "negative_score":    n_neg,
        "timeframe":         TIMEFRAME,
        "slope_window_bars": SLOPE_WINDOW,
        "min_bars":          MIN_BARS,
        "top_n":             TOP_N,
    }
    json_path = OUTPUT_DIR / "pair_momentum_summary.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    print("  pair_momentum_summary.json", flush=True)

    return summary
