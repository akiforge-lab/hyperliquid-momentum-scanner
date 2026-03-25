# -*- coding: utf-8 -*-
"""
Auto pair discovery for the 1h Pairs scanner.

Algorithm
---------
1. Filter `prices` to eligible crypto symbols (non-xyz, non-denylisted,
   have >= MIN_DISCOVERY_BARS aligned bars with at least one counterpart).
2. Generate all C(n, 2) combinations from the eligible symbol list.
3. For each candidate pair compute QUICK metrics in a single pass:
     - n_aligned          inner-join bar count
     - data_completeness  n_aligned / EXPECTED_BARS (0..1)
     - corr_168           Pearson log-return correlation over last 168 bars
     - half_life_hours    OU half-life from AR(1) OLS (None if non-mean-reverting)
     - vol_ratio          1 - |vol_a - vol_b| / max(vol_a, vol_b)  (0..1)
4. Apply pre-filters:
     - both symbols not in DENYLIST_SYMBOLS
     - frozenset({a,b}) not in DENYLIST_PAIRS
     - n_aligned >= MIN_DISCOVERY_BARS
     - corr_168 >= MIN_DISCOVERY_CORR
5. Compute discovery_score (transparent, 0..1) for each surviving pair:
     discovery_score =
       0.45 * corr_score        # (corr_168 - MIN_DISCOVERY_CORR) / (1 - MIN_DISCOVERY_CORR)
     + 0.35 * hl_score          # quality of half-life range; 0 if non-mean-reverting
     + 0.20 * completeness      # data completeness vs EXPECTED_BARS
6. PINNED_PAIRS are merged in with discovery_score = 1.1 (beats any auto score).
   They are included unconditionally regardless of MIN_DISCOVERY_CORR.
7. Sort all candidates by discovery_score, select top MAX_DISCOVERED_PAIRS
   from the auto-discovered set, then append any PINNED_PAIRS not already selected.
8. Write the FULL candidate table (all statuses) to
   output/pair_universe_discovered.csv for transparency.

Statistical notes:
  - Half-life is estimated from AR(1) OLS on the log-price spread residual
    (same method as pair_metrics.py, just on the full aligned history).
  - Volatility similarity is informational; it does not gate selection.
  - Quick metrics are intentionally cheaper than the full pair_metrics pass:
    no zscore normalisation, no 24h slopes, no alpha subtraction from spread.
  - The full compute_pair_metrics() run only happens on the selected pairs,
    not on all 700+ candidates.
"""
import itertools
import logging
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats

from src.pair_config import (
    DENYLIST_PAIRS,
    DENYLIST_SYMBOLS,
    MAX_DISCOVERED_PAIRS,
    MIN_DISCOVERY_BARS,
    MIN_DISCOVERY_CORR,
    PINNED_PAIRS,
)
from src.paths import OUTPUT_DIR

EXPECTED_BARS  = 600    # ~25 days at 1h; used for completeness normalisation
CORR_WINDOW    = 168    # same as pair_metrics.CORR_WINDOW_168
HL_MIN_HOURS   = 4      # same as pair_metrics.HL_MEAN_REVERT_MIN
HL_MAX_HOURS   = 168    # same as pair_metrics.HL_MEAN_REVERT_MAX
HL_IDEAL_MAX   = 48     # half-lives up to 48h score full marks
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Quick metrics for a single candidate pair
# ---------------------------------------------------------------------------

def _quick_metrics(
    s_a: pd.Series,
    s_b: pd.Series,
) -> Optional[dict]:
    """
    Compute lightweight discovery metrics for one candidate pair.

    Returns None if the pair does not have enough aligned data.
    All operations use the full aligned history.
    """
    # Align and sanitise
    df = pd.concat({"a": s_a, "b": s_b}, axis=1).dropna()
    df = df[(df["a"] > 0) & (df["b"] > 0)]
    n = len(df)
    if n < MIN_DISCOVERY_BARS:
        return None

    # ---- Correlation (log-returns, last CORR_WINDOW bars) ----
    w = df.tail(CORR_WINDOW)
    ret_a = np.log(w["a"]).diff().dropna()
    ret_b = np.log(w["b"]).diff().dropna()
    if len(ret_a) < 10:
        return None
    try:
        corr, _ = stats.pearsonr(ret_a, ret_b)
    except Exception:
        return None
    if not np.isfinite(corr):
        return None

    # ---- Half-life (AR(1) OLS on log-price spread) ----
    hl_hours: Optional[float] = None
    try:
        log_a = np.log(df["a"].values)
        log_b = np.log(df["b"].values)
        beta, alpha, _, _, _ = stats.linregress(log_a, log_b)
        if np.isfinite(beta) and np.isfinite(alpha):
            spread = log_b - beta * log_a - alpha
            s_series = pd.Series(spread, index=df.index)
            lag   = s_series.iloc[:-1].values
            delta = s_series.diff().dropna().values
            lam, _, _, _, _ = stats.linregress(lag, delta)
            if np.isfinite(lam) and lam < 0:
                hl_hours = float(-np.log(2) / lam)
    except Exception:
        pass

    # ---- Volatility similarity ----
    vol_a = float(np.log(df["a"]).diff().std())
    vol_b = float(np.log(df["b"]).diff().std())
    if max(vol_a, vol_b) > 1e-10:
        vol_ratio = 1.0 - abs(vol_a - vol_b) / max(vol_a, vol_b)
    else:
        vol_ratio = 0.0

    return {
        "n_aligned":    n,
        "corr_168":     round(corr, 4),
        "half_life_hours": round(hl_hours, 1) if hl_hours is not None and np.isfinite(hl_hours) else None,
        "vol_ratio":    round(max(0.0, vol_ratio), 4),
    }


# ---------------------------------------------------------------------------
# Discovery scoring
# ---------------------------------------------------------------------------

def _discovery_score(
    corr_168: float,
    half_life_hours: Optional[float],
    n_aligned: int,
) -> float:
    """
    Transparent heuristic score in [0, 1].

    Weights:
      0.45 — correlation quality (normalised above MIN_DISCOVERY_CORR)
      0.35 — half-life quality  (prefers 4–48h; penalises >48h; 0 if None)
      0.20 — data completeness  (n_aligned / EXPECTED_BARS, capped at 1)
    """
    # Correlation component
    corr_range = 1.0 - MIN_DISCOVERY_CORR
    corr_score = (corr_168 - MIN_DISCOVERY_CORR) / corr_range if corr_range > 0 else 0.0
    corr_score = max(0.0, min(1.0, corr_score))

    # Half-life component
    hl = half_life_hours
    if hl is None or not np.isfinite(hl) or hl <= 0:
        hl_score = 0.0
    elif hl < HL_MIN_HOURS:
        hl_score = (hl / HL_MIN_HOURS) * 0.4          # very short hl: partial credit
    elif hl <= HL_IDEAL_MAX:
        hl_score = 1.0
    elif hl <= HL_MAX_HOURS:
        hl_score = 1.0 - (hl - HL_IDEAL_MAX) / (HL_MAX_HOURS - HL_IDEAL_MAX)
    else:
        hl_score = 0.0

    # Completeness component
    completeness = min(n_aligned / EXPECTED_BARS, 1.0)

    return round(0.45 * corr_score + 0.35 * hl_score + 0.20 * completeness, 4)


# ---------------------------------------------------------------------------
# Main discovery function
# ---------------------------------------------------------------------------

def discover_pairs(
    prices: dict[str, pd.Series],
) -> tuple[list[dict], pd.DataFrame]:
    """
    Discover the best crypto pair candidates from available 1h price data.

    Uses module-level config from pair_config.py:
      PINNED_PAIRS, DENYLIST_SYMBOLS, DENYLIST_PAIRS,
      MAX_DISCOVERED_PAIRS, MIN_DISCOVERY_CORR, MIN_DISCOVERY_BARS.

    Args:
      prices - dict[coin -> pd.Series of hourly closes]
                (output of fetch_all_prices_hourly)

    Returns:
      selected_pairs  - list of pair config dicts (same schema as old PAIRS;
                        has "source": "pinned" | "discovered")
      discovery_df    - full candidate DataFrame (all statuses) for output file
    """
    # ----------------------------------------------------------------
    # 1. Eligible symbols: in prices, not xyz:, not denylisted, enough bars
    # ----------------------------------------------------------------
    eligible = [
        sym for sym, ser in prices.items()
        if not sym.startswith("xyz:")
        and sym not in DENYLIST_SYMBOLS
        and len(ser) >= MIN_DISCOVERY_BARS
    ]
    eligible.sort()

    n_eligible = len(eligible)
    n_candidates = n_eligible * (n_eligible - 1) // 2
    logger.info(
        "Discovery: %d eligible symbols -> %d candidate pairs",
        n_eligible, n_candidates,
    )
    print(
        f"               Discovery: {n_eligible} eligible symbols "
        f"-> {n_candidates} candidate pairs...",
        flush=True,
    )

    # ----------------------------------------------------------------
    # 2. Build a set of pinned pair IDs for de-duplication
    # ----------------------------------------------------------------
    pinned_ids:     set[str]       = {p["id"] for p in PINNED_PAIRS}
    pinned_key_set: set[frozenset] = {
        frozenset({p["leg_a"], p["leg_b"]}) for p in PINNED_PAIRS
    }

    # ----------------------------------------------------------------
    # 3. Iterate over all C(n,2) combinations
    # ----------------------------------------------------------------
    rows: list[dict] = []

    for sym_a, sym_b in itertools.combinations(eligible, 2):
        key = frozenset({sym_a, sym_b})
        pair_id = f"{sym_a}-{sym_b}"

        # Denylist check
        if sym_a in DENYLIST_SYMBOLS or sym_b in DENYLIST_SYMBOLS:
            rows.append(_exclusion_row(pair_id, sym_a, sym_b,
                                       "excluded_denylist", "symbol in DENYLIST_SYMBOLS"))
            continue
        if key in DENYLIST_PAIRS:
            rows.append(_exclusion_row(pair_id, sym_a, sym_b,
                                       "excluded_denylist", "pair in DENYLIST_PAIRS"))
            continue

        # Quick metrics
        qm = _quick_metrics(prices[sym_a], prices[sym_b])
        if qm is None:
            rows.append(_exclusion_row(pair_id, sym_a, sym_b,
                                       "excluded_insufficient_data",
                                       f"fewer than {MIN_DISCOVERY_BARS} aligned bars"))
            continue

        corr   = qm["corr_168"]
        hl     = qm["half_life_hours"]
        n_aln  = qm["n_aligned"]
        vratio = qm["vol_ratio"]

        # Correlation pre-filter
        if corr < MIN_DISCOVERY_CORR:
            rows.append({
                "pair_id":           pair_id,
                "leg_a":             sym_a,
                "leg_b":             sym_b,
                "status":            "excluded_low_correlation",
                "exclusion_reason":  f"corr_168={corr:.3f} < {MIN_DISCOVERY_CORR}",
                "discovery_score":   None,
                "corr_168":          corr,
                "half_life_hours":   hl,
                "n_aligned":         n_aln,
                "vol_ratio":         vratio,
            })
            continue

        # Passed all filters — compute score
        score = _discovery_score(corr, hl, n_aln)
        is_pinned = key in pinned_key_set
        status = "pinned" if is_pinned else "candidate"

        rows.append({
            "pair_id":          pair_id,
            "leg_a":            sym_a,
            "leg_b":            sym_b,
            "status":           status,
            "exclusion_reason": "",
            "discovery_score":  score,
            "corr_168":         corr,
            "half_life_hours":  hl,
            "n_aligned":        n_aln,
            "vol_ratio":        vratio,
        })

    # ----------------------------------------------------------------
    # 4. Select top N auto-discovered pairs
    # ----------------------------------------------------------------
    candidates_df = pd.DataFrame(rows)

    if candidates_df.empty:
        logger.warning("Discovery: no candidates survived pre-filters")
        return list(PINNED_PAIRS), _empty_discovery_df()

    # Auto-discovered candidates only (not pinned, not excluded)
    auto_mask = candidates_df["status"] == "candidate"
    auto_df = (
        candidates_df[auto_mask]
        .sort_values("discovery_score", ascending=False)
        .reset_index(drop=True)
    )

    top_auto = auto_df.head(MAX_DISCOVERED_PAIRS)
    # Mark selected vs candidate in the full table
    selected_ids: set[str] = set(top_auto["pair_id"])
    candidates_df.loc[
        auto_mask & candidates_df["pair_id"].isin(selected_ids),
        "status"
    ] = "discovered"

    # ----------------------------------------------------------------
    # 5. Build final pair list (selected auto + pinned, no duplicates)
    # ----------------------------------------------------------------
    selected_pairs: list[dict] = []
    seen_keys: set[frozenset] = set()

    # Pinned first (preserve pinned pair config exactly)
    for p in PINNED_PAIRS:
        k = frozenset({p["leg_a"], p["leg_b"]})
        if k not in seen_keys:
            selected_pairs.append(p)
            seen_keys.add(k)

    # Then top auto-discovered
    for _, row in top_auto.iterrows():
        k = frozenset({row["leg_a"], row["leg_b"]})
        if k in seen_keys:
            continue
        selected_pairs.append({
            "id":       row["pair_id"],
            "leg_a":    row["leg_a"],
            "leg_b":    row["leg_b"],
            "label":    f"{row['leg_a']} / {row['leg_b']}",
            "category": _infer_category(row["leg_a"], row["leg_b"]),
            "source":   "discovered",
        })
        seen_keys.add(k)

    n_disc   = (candidates_df["status"] == "discovered").sum()
    n_pinned = (candidates_df["status"] == "pinned").sum()
    n_excl   = candidates_df["status"].str.startswith("excluded").sum()
    logger.info(
        "Discovery result: %d discovered + %d pinned = %d pairs; %d excluded",
        n_disc, len(PINNED_PAIRS), len(selected_pairs), n_excl,
    )
    print(
        f"               Discovered: {n_disc} auto + {len(PINNED_PAIRS)} pinned "
        f"= {len(selected_pairs)} pairs  ({n_excl} excluded)",
        flush=True,
    )

    return selected_pairs, candidates_df


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _exclusion_row(
    pair_id: str,
    leg_a: str,
    leg_b: str,
    status: str,
    reason: str,
) -> dict:
    return {
        "pair_id":          pair_id,
        "leg_a":            leg_a,
        "leg_b":            leg_b,
        "status":           status,
        "exclusion_reason": reason,
        "discovery_score":  None,
        "corr_168":         None,
        "half_life_hours":  None,
        "n_aligned":        None,
        "vol_ratio":        None,
    }


def _empty_discovery_df() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "pair_id", "leg_a", "leg_b", "status", "exclusion_reason",
        "discovery_score", "corr_168", "half_life_hours", "n_aligned", "vol_ratio",
    ])


# Category heuristics for auto-discovered pairs
_L1S    = {"BTC", "ETH", "SOL", "BNB", "ADA", "AVAX", "DOT", "NEAR", "APT", "SUI", "ATOM"}
_L2S    = {"ARB", "OP", "STRK"}
_MEMES  = {"DOGE", "SHIB", "PEPE", "WIF", "BONK", "BOME", "FLOKI"}
_DEFI   = {"LINK", "UNI", "AAVE", "MKR", "CRV", "LDO", "PENDLE", "ENA", "ETHFI",
           "JUP", "JTO", "RAY"}


def _infer_category(leg_a: str, leg_b: str) -> str:
    """Heuristic category tag for auto-discovered pairs."""
    both = {leg_a, leg_b}
    if both <= _L1S:    return "layer1"
    if both <= _L2S:    return "layer2"
    if both <= _MEMES:  return "meme"
    if both <= _DEFI:   return "defi"
    if both & _MEMES:   return "meme"
    if both & _L2S:     return "layer2"
    if both & _DEFI:    return "defi"
    return "mixed"


def write_discovery_output(discovery_df: pd.DataFrame) -> None:
    """Write the full discovery candidate table to output/pair_universe_discovered.csv."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "pair_universe_discovered.csv"
    discovery_df.to_csv(path, index=False)
    logger.debug("Wrote %s (%d rows)", path, len(discovery_df))
