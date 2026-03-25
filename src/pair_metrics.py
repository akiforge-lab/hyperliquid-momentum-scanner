# -*- coding: utf-8 -*-
"""
Compute pair-trading metrics from 1-hour candles.

Timeframe: 1h (Hyperliquid candleSnapshot interval="1h")
Cache:     data/candles_1h/

Window interpretations at 1h resolution:
  MIN_PAIR_CLOSES = 100    minimum aligned hourly bars required
  ZSCORE_WINDOW   = 120    ~ 5 days of hourly bars
  CORR_WINDOW_72  =  72    ~ 3 days
  CORR_WINDOW_168 = 168    ~ 1 week
  MOM_WINDOW_24   =  24    ~ 1 day  (short momentum slope)
  MOM_WINDOW_72   =  72    ~ 3 days (medium momentum slope)
  OU half-life is in HOURS (not days).
  mean_reversion_ok: 4h <= half_life_hours <= 168h (4 hours to 1 week)

Statistical shortcuts (TODO for future upgrades):
  - Beta = static full-history OLS on log-prices; rolling beta not yet implemented.
  - Half-life uses AR(1) OLS; Johansen cointegration test not yet run.
  - Annualisation factor for hourly crypto = 8760 (24*365, continuous market).
"""
import logging
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats

# ---------------------------------------------------------------------------
# Window constants (hourly)
# ---------------------------------------------------------------------------
MIN_PAIR_CLOSES = 100   # minimum aligned hourly bars to attempt computation
ZSCORE_WINDOW   = 120   # ~5 days; used for z-score normalisation
CORR_WINDOW_72  = 72    # ~3 days; short correlation window
CORR_WINDOW_168 = 168   # ~1 week; long correlation window
MOM_WINDOW_24   = 24    # ~1 day;  short momentum slope
MOM_WINDOW_72   = 72    # ~3 days; medium momentum slope
BETA_MIN_CLOSES = 60    # minimum bars for a reliable OLS beta
HL_MEAN_REVERT_MIN = 4    # minimum half-life in hours to consider mean-reverting
HL_MEAN_REVERT_MAX = 168  # maximum half-life in hours (> 1 week = random walk for our purposes)
HOURS_PER_YEAR  = 8760.0  # 24 * 365 (crypto trades continuously)
TIMEFRAME       = "1h"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _align(series_a: pd.Series, series_b: pd.Series) -> pd.DataFrame:
    """
    Inner-join two price series on their index (hourly UTC timestamps),
    drop any NaN rows, and drop rows where either price is non-positive.
    """
    df = pd.concat({"a": series_a, "b": series_b}, axis=1).dropna()
    df = df[(df["a"] > 0) & (df["b"] > 0)]
    return df


def _ols_beta(log_a: np.ndarray, log_b: np.ndarray) -> tuple[float, float]:
    """
    Regress log_b on log_a to estimate the hedge ratio (beta).
    Returns (beta, alpha).  Both NaN if the regression fails.
    """
    if len(log_a) < BETA_MIN_CLOSES:
        return float("nan"), float("nan")
    try:
        beta, alpha, _, _, _ = stats.linregress(log_a, log_b)
        if not (np.isfinite(beta) and np.isfinite(alpha)):
            return float("nan"), float("nan")
        return float(beta), float(alpha)
    except Exception:
        return float("nan"), float("nan")


def _zscore(spread: pd.Series, window: int) -> Optional[float]:
    """
    Z-score of the latest spread value relative to the rolling mean/std
    over the last `window` hourly observations.
    Returns None if std is degenerate (zero or NaN).
    """
    w   = spread.iloc[-window:]
    mu  = float(w.mean())
    std = float(w.std(ddof=1))
    if not (np.isfinite(std) and std > 1e-10):
        return None
    return float((float(spread.iloc[-1]) - mu) / std)


def _half_life_hours(spread: pd.Series) -> Optional[float]:
    """
    Estimate Ornstein-Uhlenbeck half-life via AR(1) OLS regression:
        d_spread[t] = lambda * spread[t-1] + epsilon

    half_life_hours = -ln(2) / lambda_hat

    Because the spread is computed from hourly bars, the resulting half-life
    is naturally in hours.

    Returns None (non-mean-reverting) when:
      - lambda_hat >= 0 (random walk or explosive process)
      - regression fails for any reason
    """
    if len(spread) < 10:
        return None
    try:
        lag   = spread.iloc[:-1].values
        delta = spread.diff().dropna().values
        lambda_hat, _, _, _, _ = stats.linregress(lag, delta)
        if not np.isfinite(lambda_hat) or lambda_hat >= 0:
            return None
        return float(-np.log(2) / lambda_hat)
    except Exception:
        return None


def _log_return_corr(aligned: pd.DataFrame, window: int) -> Optional[float]:
    """
    Pearson correlation of log-returns over the last `window` hourly rows.
    Log-returns avoid spurious correlation from shared price trends.
    """
    w = aligned.tail(window)
    if len(w) < max(5, window // 3):
        return None
    ret_a = np.log(w["a"]).diff().dropna()
    ret_b = np.log(w["b"]).diff().dropna()
    if len(ret_a) < 5:
        return None
    try:
        r, _ = stats.pearsonr(ret_a, ret_b)
        return float(r) if np.isfinite(r) else None
    except Exception:
        return None


def _momentum_slope_hourly(series: pd.Series, window: int) -> tuple[Optional[float], Optional[float]]:
    """
    Fit log(price) = a + b*t over the last `window` hourly bars.
    Returns (slope_ann_pct, r2).  Both None if insufficient data.

    Annualisation: slope_per_hour * HOURS_PER_YEAR * 100
    (crypto trades continuously; HOURS_PER_YEAR = 8760)
    """
    if len(series) < window:
        return None, None
    w = series.iloc[-window:]
    try:
        log_p = np.log(w.values.astype(float))
        t = np.arange(len(log_p), dtype=float)
        slope, _, r_value, _, _ = stats.linregress(t, log_p)
        slope_ann_pct = float(slope * HOURS_PER_YEAR * 100)
        r2 = float(r_value ** 2)
        return slope_ann_pct, r2
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_pair_metrics(
    prices: dict[str, pd.Series],
    pairs: list[dict],
) -> tuple[pd.DataFrame, list[dict]]:
    """
    Compute 1h-based metrics for all configured pairs.

    Args:
      prices  - dict[coin -> pd.Series of hourly closes] from fetch_all_prices_hourly
      pairs   - list of pair config dicts from pair_config.PAIRS

    Returns:
      df      - DataFrame with one row per successfully computed pair
      skipped - list of {"pair_id": ..., "reason": ...} for failed pairs
    """
    records: list[dict] = []
    skipped: list[dict] = []

    for pair in pairs:
        pid   = pair["id"]
        leg_a = pair["leg_a"]
        leg_b = pair["leg_b"]

        # Check presence
        if leg_a not in prices:
            skipped.append({"pair_id": pid, "reason": f"leg_a ({leg_a}) missing from price data"})
            logger.debug("Pair %s skipped: %s missing", pid, leg_a)
            continue
        if leg_b not in prices:
            skipped.append({"pair_id": pid, "reason": f"leg_b ({leg_b}) missing from price data"})
            logger.debug("Pair %s skipped: %s missing", pid, leg_b)
            continue

        aligned = _align(prices[leg_a], prices[leg_b])
        n = len(aligned)

        if n < MIN_PAIR_CLOSES:
            skipped.append({"pair_id": pid,
                            "reason": f"only {n} aligned hourly bars (need {MIN_PAIR_CLOSES})"})
            logger.debug("Pair %s skipped: only %d aligned bars", pid, n)
            continue

        log_a = np.log(aligned["a"].values)
        log_b = np.log(aligned["b"].values)

        # Beta / spread
        beta, alpha = _ols_beta(log_a, log_b)
        if not (np.isfinite(beta) and np.isfinite(alpha)):
            skipped.append({"pair_id": pid,
                            "reason": "beta estimation failed (OLS did not converge)"})
            continue

        spread_series = pd.Series(
            log_b - beta * log_a - alpha,
            index=aligned.index,
        )

        # Z-score
        zscore = _zscore(spread_series, ZSCORE_WINDOW)
        if zscore is None:
            skipped.append({"pair_id": pid,
                            "reason": "degenerate spread (std = 0 or NaN)"})
            continue

        # Spread stats over zscore window
        w_spread    = spread_series.iloc[-ZSCORE_WINDOW:]
        spread_mean = float(w_spread.mean())
        spread_std  = float(w_spread.std(ddof=1))

        # Half-life (in hours for hourly data)
        hl_hours = _half_life_hours(spread_series)
        mean_reversion_ok = (
            hl_hours is not None
            and np.isfinite(hl_hours)
            and HL_MEAN_REVERT_MIN <= hl_hours <= HL_MEAN_REVERT_MAX
        )

        # Correlations (72h and 168h windows)
        corr_72  = _log_return_corr(aligned, CORR_WINDOW_72)
        corr_168 = _log_return_corr(aligned, CORR_WINDOW_168)
        # Use the longer window as the primary correlation for signal gating
        corr_val = corr_168 if corr_168 is not None else corr_72
        correlation_ok = corr_val is not None and corr_val >= 0.5

        # Momentum slopes (24h and 72h windows, annualised for readability)
        slope_a_24, r2_a_24 = _momentum_slope_hourly(aligned["a"], MOM_WINDOW_24)
        slope_b_24, r2_b_24 = _momentum_slope_hourly(aligned["b"], MOM_WINDOW_24)
        slope_a_72, r2_a_72 = _momentum_slope_hourly(aligned["a"], MOM_WINDOW_72)
        slope_b_72, r2_b_72 = _momentum_slope_hourly(aligned["b"], MOM_WINDOW_72)

        # Use the 72h slope as the primary (matches signal logic in pair_signals.py)
        slope_a = slope_a_72
        r2_a    = r2_a_72
        slope_b = slope_b_72
        r2_b    = r2_b_72

        records.append({
            "pair_id":            pid,
            "leg_a":              leg_a,
            "leg_b":              leg_b,
            "category":           pair.get("category", ""),
            "source":             pair.get("source", "discovered"),  # "pinned" | "discovered"
            "timeframe":          TIMEFRAME,
            "n_aligned":          n,
            "bars_used":          min(n, ZSCORE_WINDOW),
            "beta":               round(beta, 4),
            "alpha":              round(alpha, 6),
            "spread_current":     round(float(spread_series.iloc[-1]), 6),
            "spread_mean":        round(spread_mean, 6),
            "spread_std":         round(spread_std, 6),
            "zscore":             round(zscore, 4),
            "zscore_threshold":   2.0,   # ENTRY_Z from pair_signals (documented here for output)
            "half_life_hours":    round(hl_hours, 1) if hl_hours is not None else None,
            "corr_72":            round(corr_72,  4) if corr_72  is not None else None,
            "corr_168":           round(corr_168, 4) if corr_168 is not None else None,
            # Primary (72h) slopes
            "slope_a_ann_pct":    round(slope_a, 2) if slope_a is not None else None,
            "r2_a":               round(r2_a,    4) if r2_a    is not None else None,
            "slope_b_ann_pct":    round(slope_b, 2) if slope_b is not None else None,
            "r2_b":               round(r2_b,    4) if r2_b    is not None else None,
            # 24h slopes (shorter-horizon momentum)
            "slope_a_24h_ann_pct": round(slope_a_24, 2) if slope_a_24 is not None else None,
            "slope_b_24h_ann_pct": round(slope_b_24, 2) if slope_b_24 is not None else None,
            "mean_reversion_ok":  mean_reversion_ok,
            "correlation_ok":     correlation_ok,
            # Raw latest prices for reference
            "latest_price_a":     round(float(aligned["a"].iloc[-1]), 6),
            "latest_price_b":     round(float(aligned["b"].iloc[-1]), 6),
        })

    logger.info(
        "Pair metrics (1h) computed: %d pairs; %d skipped",
        len(records), len(skipped),
    )

    if not records:
        return pd.DataFrame(), skipped

    df = pd.DataFrame(records)
    # Sort by absolute z-score descending (most actionable first)
    df["_abs_z"] = df["zscore"].abs()
    df.sort_values("_abs_z", ascending=False, inplace=True)
    df.drop(columns=["_abs_z"], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df, skipped
