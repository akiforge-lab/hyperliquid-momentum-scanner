# -*- coding: utf-8 -*-
"""
Generate trading signals for pairs based on computed metrics.

Two signal families:

1. Mean-reversion (mr_signal):
   LONG_SPREAD  — zscore <= -ENTRY_Z (buy leg_b, hedge-short leg_a)
   SHORT_SPREAD — zscore >= +ENTRY_Z (sell leg_b, hedge-long leg_a)
   NEUTRAL      — |zscore| <= EXIT_Z (spread near mean, no trade)
   WATCH        — EXIT_Z < |zscore| < ENTRY_Z (approaching entry)
   INVALID      — pair does not meet correlation / mean-reversion criteria

2. Relative momentum (mom_signal):
   Identifies which leg has stronger recent momentum.
   Acts as a confirmation or conflict flag against the mean-reversion signal.
   Labels: CONFIRMS_LONG_SPREAD / CONFIRMS_SHORT_SPREAD / CONFLICTS / NEUTRAL
"""
import logging

import numpy as np
import pandas as pd

ENTRY_Z = 2.0   # z-score threshold for opening a mean-reversion position
EXIT_Z  = 0.5   # z-score threshold below which the spread is considered neutral
MOM_R2_MIN = 0.3  # minimum R2 for a momentum slope to be considered directional

logger = logging.getLogger(__name__)


def _mr_signal(row: pd.Series) -> str:
    """Classify the mean-reversion signal for one pair row."""
    if not row["correlation_ok"] or not row["mean_reversion_ok"]:
        return "INVALID"

    z = row["zscore"]
    if not np.isfinite(z):
        return "INVALID"

    if z <= -ENTRY_Z:
        return "LONG_SPREAD"    # spread below mean: leg_b cheap relative to leg_a
    if z >= +ENTRY_Z:
        return "SHORT_SPREAD"   # spread above mean: leg_b expensive relative to leg_a
    if abs(z) <= EXIT_Z:
        return "NEUTRAL"
    return "WATCH"


def _mom_signal(row: pd.Series, mr: str) -> str:
    """
    Relative momentum modifier.

    Compares annualised log-slope of leg_b vs leg_a.  A positive
    relative momentum (slope_b > slope_a) means leg_b is trending up
    faster — this *confirms* a LONG_SPREAD trade (leg_b should outperform
    further) and *conflicts* with a SHORT_SPREAD trade.
    """
    sa = row.get("slope_a_ann_pct")
    sb = row.get("slope_b_ann_pct")
    r2a = row.get("r2_a")
    r2b = row.get("r2_b")

    # Need at least one side to have a reliable trend
    sa_ok = sa is not None and np.isfinite(sa) and r2a is not None and r2a >= MOM_R2_MIN
    sb_ok = sb is not None and np.isfinite(sb) and r2b is not None and r2b >= MOM_R2_MIN

    if not sa_ok and not sb_ok:
        return "NEUTRAL"

    sa_val = float(sa) if sa is not None else 0.0
    sb_val = float(sb) if sb is not None else 0.0
    rel = sb_val - sa_val   # positive: leg_b outperforming leg_a

    if mr == "LONG_SPREAD":
        # We want leg_b to rise relative to leg_a → confirms if rel > 0
        return "CONFIRMS_LONG_SPREAD" if rel > 0 else "CONFLICTS"
    if mr == "SHORT_SPREAD":
        # We want leg_a to rise relative to leg_b → confirms if rel < 0
        return "CONFIRMS_SHORT_SPREAD" if rel < 0 else "CONFLICTS"
    return "NEUTRAL"


def _mr_score(row: pd.Series, mr: str) -> float:
    """
    Heuristic mean-reversion score in [0, 1].

    Higher is better / more actionable.  Combines:
      - |zscore| / ENTRY_Z (capped at 2.0 so extreme z-scores don't dominate)
      - correlation (the more correlated, the more reliable the spread)
      - half-life quality: penalise very long or missing half-lives
    """
    if mr in ("INVALID", "NEUTRAL"):
        return 0.0

    z_norm = min(abs(row["zscore"]) / ENTRY_Z, 2.0) / 2.0          # 0..1
    # Support both hourly column names (corr_168/corr_72) and legacy daily names
    corr = (row.get("corr_168") or row.get("corr_90")
            or row.get("corr_72") or row.get("corr_30") or 0.0)
    corr_score = max(0.0, (corr - 0.5) / 0.5)                       # 0..1 above 0.5 threshold

    # half_life_hours (hourly data) or half_life_days (legacy daily data)
    hl = row.get("half_life_hours") or row.get("half_life_days")
    is_hourly = row.get("half_life_hours") is not None
    if hl is None or not np.isfinite(hl) or hl <= 0:
        hl_score = 0.0
    elif is_hourly:
        # Prefer half-lives around 12-48 hours; penalise very short (<4h) or long (>96h)
        if hl < 4:
            hl_score = hl / 4.0 * 0.5
        elif hl <= 48:
            hl_score = 1.0
        elif hl <= 96:
            hl_score = 1.0 - (hl - 48) / 48.0
        else:
            hl_score = 0.0
    else:
        # Legacy daily half-life scoring
        if hl < 2:
            hl_score = hl / 2.0 * 0.5
        elif hl <= 30:
            hl_score = 1.0
        elif hl <= 90:
            hl_score = 1.0 - (hl - 30) / 60.0
        else:
            hl_score = 0.0

    return round(0.4 * z_norm + 0.35 * corr_score + 0.25 * hl_score, 4)


def compute_pair_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Append signal columns to the pair metrics DataFrame.

    Input:  DataFrame from compute_pair_metrics (modified copy returned)
    Output: same DataFrame with added columns:
              mr_signal, mom_signal, mr_score, final_pair_score,
              display_pair, long_leg, short_leg, trade_label
    """
    if df.empty:
        return df.copy()

    out = df.copy()

    mr_signals  = []
    mom_signals = []
    mr_scores   = []

    for _, row in out.iterrows():
        mr  = _mr_signal(row)
        mom = _mom_signal(row, mr)
        score = _mr_score(row, mr)
        mr_signals.append(mr)
        mom_signals.append(mom)
        mr_scores.append(score)

    out["mr_signal"]        = mr_signals
    out["mom_signal"]       = mom_signals
    out["mr_score"]         = mr_scores
    out["final_pair_score"] = out["mr_score"]   # placeholder; extend with more factors later

    # Derive display_pair / long_leg / short_leg so the caller never has to
    # interpret LONG_SPREAD / SHORT_SPREAD to know which leg to buy/sell.
    #   LONG_SPREAD  → buy leg_a, sell leg_b  → display as "leg_a-leg_b"
    #   SHORT_SPREAD → sell leg_a, buy leg_b  → display as "leg_b-leg_a"
    #   anything else → display as pair_id (no directional flip)
    display_pairs: list[str] = []
    long_legs:     list[str] = []
    short_legs:    list[str] = []
    for _, row in out.iterrows():
        mr = row["mr_signal"]
        la, lb = row["leg_a"], row["leg_b"]
        if mr == "LONG_SPREAD":
            # spread = log_b - beta*log_a is LOW → buy leg_b (cheap), sell leg_a
            long_legs.append(lb)
            short_legs.append(la)
            display_pairs.append(f"{lb}-{la}")
        elif mr == "SHORT_SPREAD":
            # spread = log_b - beta*log_a is HIGH → sell leg_b (expensive), buy leg_a
            long_legs.append(la)
            short_legs.append(lb)
            display_pairs.append(f"{la}-{lb}")
        else:
            long_legs.append(la)
            short_legs.append(lb)
            display_pairs.append(row["pair_id"])
    out["display_pair"] = display_pairs
    out["long_leg"]     = long_legs
    out["short_leg"]    = short_legs
    out["trade_label"]  = [
        f"Long {ll} / Short {sl}" if ll and sl else pid
        for ll, sl, pid in zip(long_legs, short_legs, out["pair_id"])
    ]

    logger.info(
        "Pair signals: LONG_SPREAD=%d  SHORT_SPREAD=%d  WATCH=%d  NEUTRAL=%d  INVALID=%d",
        (out["mr_signal"] == "LONG_SPREAD").sum(),
        (out["mr_signal"] == "SHORT_SPREAD").sum(),
        (out["mr_signal"] == "WATCH").sum(),
        (out["mr_signal"] == "NEUTRAL").sum(),
        (out["mr_signal"] == "INVALID").sum(),
    )

    return out
