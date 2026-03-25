# -*- coding: utf-8 -*-
"""
Compute momentum metrics for each asset:
  - 90-day log-price regression slope
  - R-squared (R2)
  - Momentum Score = slope_ann x R2
  - 100-day moving average
  - LONG / SHORT / NEUTRAL signal
"""
import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

MIN_CLOSES   = 100   # minimum daily closes required
SLOPE_WINDOW = 90    # regression window (days)
MA_WINDOW    = 100   # moving-average window (days)
MIN_R2       = 0.5   # R2 threshold for directional signal

logger = logging.getLogger(__name__)


@dataclass
class AssetMetrics:
    coin:            str
    n_closes:        int
    latest_price:    float
    slope_ann_pct:   float   # annualised log-slope in %
    r2:              float
    momentum_score:  float   # slope_ann_pct x r2
    ma100:           float
    signal:          str     # LONG | SHORT | NEUTRAL
    source:          str     # data source used


def _annualise(slope_per_day: float) -> float:
    return slope_per_day * 252 * 100   # in percent


def _regress(series: pd.Series) -> tuple[float, float]:
    """
    Fit log(price) = a + b*t over the last SLOPE_WINDOW closes.
    Returns (slope_per_day, r_squared); both NaN if insufficient data.
    """
    window = series.iloc[-SLOPE_WINDOW:]
    if len(window) < SLOPE_WINDOW:
        return float("nan"), float("nan")
    log_p = np.log(window.values.astype(float))
    t = np.arange(len(log_p), dtype=float)
    slope, _, r_value, _, _ = stats.linregress(t, log_p)
    return slope, r_value ** 2


def compute_momentum(
    prices: dict[str, pd.Series],
    sources: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, list[dict]]:
    """
    Compute metrics for all coins that pass the minimum-history filter.

    Args:
      prices  – {coin: Series of closes}
      sources – optional {coin: source_label} from fetch layer

    Returns:
      df            – DataFrame sorted by momentum_score descending
      skipped_hist  – list of {coin, n_closes, reason} for short-history coins
    """
    if sources is None:
        sources = {}

    records:      list[AssetMetrics] = []
    skipped_hist: list[dict]         = []

    for coin, series in prices.items():
        series = series.replace([np.inf, -np.inf], np.nan).dropna()
        series = series[series > 0]
        n = len(series)

        if n < MIN_CLOSES:
            skipped_hist.append({
                "coin":     coin,
                "n_closes": n,
                "reason":   f"Only {n} closes (need {MIN_CLOSES})",
            })
            logger.debug("Skipping %s - only %d closes", coin, n)
            continue

        slope, r2 = _regress(series)
        if np.isnan(slope) or np.isnan(r2):
            logger.warning("Regression failed for %s", coin)
            continue

        slope_ann = _annualise(slope)
        momentum_score = slope_ann * r2
        ma100 = float(series.iloc[-MA_WINDOW:].mean())
        latest = float(series.iloc[-1])

        if slope_ann > 0 and r2 > MIN_R2 and latest > ma100:
            signal = "LONG"
        elif slope_ann < 0 and r2 > MIN_R2 and latest < ma100:
            signal = "SHORT"
        else:
            signal = "NEUTRAL"

        records.append(AssetMetrics(
            coin=coin,
            n_closes=n,
            latest_price=latest,
            slope_ann_pct=slope_ann,
            r2=r2,
            momentum_score=momentum_score,
            ma100=ma100,
            signal=signal,
            source=sources.get(coin, "unknown"),
        ))

    logger.info(
        "Computed metrics for %d assets; %d skipped (insufficient history)",
        len(records), len(skipped_hist),
    )

    if not records:
        raise ValueError("No assets passed the minimum-history filter.")

    df = pd.DataFrame([
        {
            "coin":            m.coin,
            "n_closes":        m.n_closes,
            "latest_price":    round(m.latest_price,   6),
            "slope_ann_pct":   round(m.slope_ann_pct,  2),
            "r2":              round(m.r2,              4),
            "momentum_score":  round(m.momentum_score, 4),
            "ma100":           round(m.ma100,           6),
            "signal":          m.signal,
            "source":          m.source,
        }
        for m in records
    ])
    df.sort_values("momentum_score", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df, skipped_hist
