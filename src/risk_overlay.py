# -*- coding: utf-8 -*-
"""
Crypto risk overlay for the Hyperliquid Momentum Scanner.

Collects three market-stress signals, normalises each with a rolling z-score
against stored history, produces a composite risk_score, and classifies the
current market regime as risk_off / neutral / risk_on.

The overlay is designed to run BEFORE the main momentum scan so that the
regime context is visible alongside trade signals in a single scan run.

Signals (v1)
------------
1. DVOL BTC        — Deribit 30-day annualised implied vol (free public API)
2. Funding stress  — median(|funding_i|) across all live HL perps
3. OI stress       — % change in aggregate HL OI vs 24 h ago

Derived
-------
  vrp_btc = dvol_btc − btc_rv_30d_ann
  (vol risk premium; positive = IV > RV = vol overpriced relative to realised)

  Note: btc_rv_30d_ann is read from the existing daily candle cache
  (data/candles/BTC.json).  On ephemeral CI runners (GitHub Actions) the
  cache does not exist at overlay time, so btc_rv and vrp_btc will be None
  for that run.  This is harmless: risk_score falls back to the remaining
  available z-scores.

  hype_rv_30d_ann is computed for contextual output only; it does NOT enter
  the VRP formula in v1.

Composite
---------
  risk_score = mean(z_vrp_btc, z_funding_stress, z_oi_stress)
               (NaN / None inputs are excluded from the mean)

Outputs
-------
  output/risk_overlay.json   — current snapshot; read by app.py and plugins
  data/risk_history.json     — rolling time-series for z-score windows

Bootstrap
---------
  On first run (history < MIN_HISTORY_DAYS), 90 days of daily DVOL are
  seeded from Deribit so that the DVOL series is immediately usable.
  funding_stress and oi_change_pct_24h z-scores ramp up over subsequent runs.
"""
import json
import logging
import statistics
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import requests

from src.paths import DAILY_CACHE_DIR, OUTPUT_DIR
from src.risk_config import (
    API_TIMEOUT,
    DERIBIT_DVOL_URL,
    HL_API_URL,
    MIN_HISTORY_DAYS,
    REGIME_MULTIPLIERS,
    RISK_OFF_THRESHOLD,
    RISK_ON_THRESHOLD,
    RV_WINDOW_DAYS,
    ZSCORE_WINDOW_DAYS,
)
from src.risk_history import (
    append_snapshot,
    get_oi_24h_ago,
    get_series,
    history_day_count,
    load_history,
    save_history,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def fetch_dvol_btc() -> Optional[float]:
    """
    Fetch the most recent BTC DVOL close from Deribit's public REST API.
    Requests the last 2 hourly bars; returns the final close price.
    Returns None on any failure.
    """
    now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - 2 * 3_600_000   # 2 h back
    params = {
        "currency":        "BTC",
        "start_timestamp": start_ms,
        "end_timestamp":   now_ms,
        "resolution":      3600,
    }
    try:
        resp = requests.get(DERIBIT_DVOL_URL, params=params, timeout=API_TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get("result", {}).get("data", [])
        if not data:
            logger.warning("DVOL: empty response from Deribit")
            return None
        # Each element: [ts_ms, open, high, low, close]
        dvol = round(float(data[-1][4]), 2)
        logger.debug("DVOL BTC: %.2f", dvol)
        return dvol
    except Exception as exc:
        logger.warning("DVOL fetch failed: %s", exc)
        return None


def fetch_hl_market_data() -> tuple[Optional[float], Optional[float]]:
    """
    Fetch HL metaAndAssetCtxs (single POST, same endpoint as fetch_universe).

    Returns
    -------
    funding_stress : median(|funding_i|) across all perps; None on failure.
    oi_total_usd   : sum(openInterest_i × markPx_i); None on failure.
    """
    try:
        resp = requests.post(
            HL_API_URL,
            json={"type": "metaAndAssetCtxs"},
            timeout=API_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()

        if not isinstance(payload, list) or len(payload) < 2:
            logger.warning("metaAndAssetCtxs: unexpected response shape")
            return None, None

        universe   = payload[0].get("universe", [])
        asset_ctxs = payload[1]

        funding_abs: list[float] = []
        oi_usd = 0.0

        for _meta, ctx in zip(universe, asset_ctxs):
            try:
                fr = float(ctx.get("funding") or 0)
                funding_abs.append(abs(fr))
            except (ValueError, TypeError):
                pass
            try:
                oi = float(ctx.get("openInterest") or 0)
                px = float(ctx.get("markPx") or ctx.get("oraclePx") or 0)
                oi_usd += oi * px
            except (ValueError, TypeError):
                pass

        funding_stress = (
            round(statistics.median(funding_abs), 8) if funding_abs else None
        )
        oi_total = round(oi_usd, 0) if oi_usd > 0 else None

        logger.debug(
            "HL market: funding_stress=%.8f  oi_total_usd=%.0f",
            funding_stress or 0, oi_total or 0,
        )
        return funding_stress, oi_total

    except Exception as exc:
        logger.warning("HL metaAndAssetCtxs fetch failed: %s", exc)
        return None, None


def _compute_rv_from_cache(coin: str, window_days: int = RV_WINDOW_DAYS) -> Optional[float]:
    """
    Compute annualised realised volatility from the daily candle cache for
    `coin`.  Returns value as a percentage (matching DVOL units) so that
    VRP = DVOL − RV is directly interpretable.

    Returns None when the cache is absent or has insufficient history.
    """
    cache_file = DAILY_CACHE_DIR / f"{coin}.json"
    if not cache_file.exists():
        logger.debug("%s candle cache absent — cannot compute RV", coin)
        return None
    try:
        with open(cache_file, encoding="utf-8") as fh:
            data = json.load(fh)
        closes_raw = data.get("closes", [])
        needed = window_days + 1
        if len(closes_raw) < needed:
            logger.debug(
                "%s cache too short (%d < %d bars) for %d-day RV",
                coin, len(closes_raw), needed, window_days,
            )
            return None
        closes  = np.array([float(c[1]) for c in closes_raw[-needed:]])
        log_ret = np.diff(np.log(closes))
        rv_ann_pct = float(np.std(log_ret, ddof=1)) * np.sqrt(252) * 100
        logger.debug("%s RV %dd: %.2f%%", coin, window_days, rv_ann_pct)
        return round(rv_ann_pct, 2)
    except Exception as exc:
        logger.warning("%s RV computation failed: %s", coin, exc)
        return None


def compute_btc_rv() -> Optional[float]:
    """BTC 30-day annualised RV from daily candle cache (% units)."""
    return _compute_rv_from_cache("BTC", RV_WINDOW_DAYS)


def compute_hype_rv() -> Optional[float]:
    """HYPE 30-day annualised RV — contextual output only, not in VRP formula."""
    return _compute_rv_from_cache("HYPE", RV_WINDOW_DAYS)


# ---------------------------------------------------------------------------
# History bootstrap  (first-run seed from Deribit daily data)
# ---------------------------------------------------------------------------

def _bootstrap_dvol_history(history: list[dict]) -> list[dict]:
    """
    If history has fewer than MIN_HISTORY_DAYS entries, fetch up to
    ZSCORE_WINDOW_DAYS of daily DVOL from Deribit and prepend as minimal
    snapshots (ts + dvol_btc only).  Existing dates are never overwritten.

    These bootstrap entries contribute to the dvol_btc series used by the
    vrp_z z-score once vrp_btc values start accumulating in future runs.
    """
    if len(history) >= MIN_HISTORY_DAYS:
        return history   # already seeded

    logger.info("Bootstrapping DVOL history from Deribit (last %d days)…", ZSCORE_WINDOW_DAYS)
    now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - ZSCORE_WINDOW_DAYS * 86_400_000
    params = {
        "currency":        "BTC",
        "start_timestamp": start_ms,
        "end_timestamp":   now_ms,
        "resolution":      "1D",
    }
    try:
        resp = requests.get(DERIBIT_DVOL_URL, params=params, timeout=API_TIMEOUT)
        resp.raise_for_status()
        data_pts = resp.json().get("result", {}).get("data", [])
        if not data_pts:
            logger.warning("DVOL bootstrap: empty response")
            return history

        existing_dates = {e.get("ts", "")[:10] for e in history}
        new_entries: list[dict] = []
        for point in data_pts:
            ts_ms = int(point[0])
            ts_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            date_str = ts_dt.strftime("%Y-%m-%d")
            if date_str in existing_dates:
                continue
            new_entries.append({
                "ts":       ts_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "dvol_btc": round(float(point[4]), 2),  # close
            })

        if not new_entries:
            return history

        merged = new_entries + history
        merged.sort(key=lambda e: e.get("ts", ""))
        logger.info("Bootstrapped %d daily DVOL entries", len(new_entries))
        return merged

    except Exception as exc:
        logger.warning("DVOL bootstrap failed: %s", exc)
        return history


# ---------------------------------------------------------------------------
# Z-score normalisation
# ---------------------------------------------------------------------------

def rolling_zscore(
    history_values: list[float],
    current: float,
    window: int = ZSCORE_WINDOW_DAYS,
) -> Optional[float]:
    """
    Z-score of `current` relative to the last `window` values in
    `history_values` (historical observations, NOT including current).
    Returns None when history is shorter than MIN_HISTORY_DAYS.
    """
    if len(history_values) < MIN_HISTORY_DAYS:
        return None
    recent = np.array(history_values[-window:], dtype=float)
    mean   = float(np.mean(recent))
    std    = float(np.std(recent, ddof=1))
    if std < 1e-10:
        return 0.0
    return round(float((current - mean) / std), 3)


# ---------------------------------------------------------------------------
# Regime classification
# ---------------------------------------------------------------------------

def classify_regime(risk_score: Optional[float]) -> tuple[str, float]:
    """
    Return (regime_label, regime_multiplier).

    regime_multiplier is output-only in v1; it does NOT modify momentum_score.
    """
    if risk_score is None:
        return "unknown", REGIME_MULTIPLIERS["unknown"]
    if risk_score > RISK_OFF_THRESHOLD:
        return "risk_off", REGIME_MULTIPLIERS["risk_off"]
    if risk_score < RISK_ON_THRESHOLD:
        return "risk_on", REGIME_MULTIPLIERS["risk_on"]
    return "neutral", REGIME_MULTIPLIERS["neutral"]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_risk_overlay() -> dict:
    """
    Full risk overlay pipeline:
      1. Fetch DVOL, HL funding/OI, BTC/HYPE RV
      2. Load + bootstrap history
      3. Compute ΔOI%, VRP, rolling z-scores, regime
      4. Persist snapshot to history
      5. Write output/risk_overlay.json

    Returns the overlay dict (identical to what is written to JSON).
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ------------------------------------------------------------------
    # 1. Fetch inputs
    # ------------------------------------------------------------------
    print("Risk Overlay 1/3  Fetching DVOL (Deribit public API)…", flush=True)
    dvol_btc = fetch_dvol_btc()

    print("Risk Overlay 2/3  Fetching HL funding rates & open interest…", flush=True)
    funding_stress, oi_total_usd = fetch_hl_market_data()

    print("Risk Overlay 3/3  Computing RV from daily candle cache…", flush=True)
    btc_rv   = compute_btc_rv()
    hype_rv  = compute_hype_rv()

    # ------------------------------------------------------------------
    # 2. Load history, bootstrap DVOL if first run
    # ------------------------------------------------------------------
    history = load_history()
    history = _bootstrap_dvol_history(history)

    # ------------------------------------------------------------------
    # 3. Derived: ΔOI% (true 24 h using timestamped history)
    # ------------------------------------------------------------------
    oi_24h_ago         = get_oi_24h_ago(history)
    oi_change_pct_24h: Optional[float] = None
    if oi_total_usd is not None and oi_24h_ago and oi_24h_ago > 0:
        oi_change_pct_24h = round(
            (oi_total_usd - oi_24h_ago) / oi_24h_ago * 100, 3
        )

    # ------------------------------------------------------------------
    # 4. Derived: VRP BTC  (dvol − rv, both in annualised % units)
    # ------------------------------------------------------------------
    vrp_btc: Optional[float] = None
    if dvol_btc is not None and btc_rv is not None:
        vrp_btc = round(dvol_btc - btc_rv, 2)

    # ------------------------------------------------------------------
    # 5. Rolling z-scores  (history does NOT yet include current snapshot)
    # ------------------------------------------------------------------
    hist_vrp     = get_series(history, "vrp_btc",           ZSCORE_WINDOW_DAYS)
    hist_funding = get_series(history, "funding_stress",     ZSCORE_WINDOW_DAYS)
    hist_oi_chg  = get_series(history, "oi_change_pct_24h",  ZSCORE_WINDOW_DAYS)

    vrp_z      = rolling_zscore(hist_vrp,     vrp_btc)           if vrp_btc            is not None else None
    funding_z  = rolling_zscore(hist_funding, funding_stress)    if funding_stress     is not None else None
    oi_z       = rolling_zscore(hist_oi_chg,  oi_change_pct_24h) if oi_change_pct_24h is not None else None

    # ------------------------------------------------------------------
    # 6. Composite risk_score = mean of available z-scores
    # ------------------------------------------------------------------
    available = [z for z in (vrp_z, funding_z, oi_z) if z is not None]
    risk_score: Optional[float] = (
        round(float(np.mean(available)), 3) if available else None
    )

    # ------------------------------------------------------------------
    # 7. Regime
    # ------------------------------------------------------------------
    regime, multiplier = classify_regime(risk_score)

    # ------------------------------------------------------------------
    # 8. Persist current snapshot to history (AFTER z-score computation)
    # ------------------------------------------------------------------
    snapshot: dict = {
        "ts":                 ts_now,
        "dvol_btc":           dvol_btc,
        "funding_stress":     funding_stress,
        "oi_total_usd":       oi_total_usd,
        "oi_change_pct_24h":  oi_change_pct_24h,
        "btc_rv_30d_ann":     btc_rv,
        "vrp_btc":            vrp_btc,
    }
    updated_history = append_snapshot(snapshot, history)
    save_history(updated_history)

    # ------------------------------------------------------------------
    # 9. Build output dict
    # ------------------------------------------------------------------
    n_days = history_day_count(updated_history)
    overlay: dict = {
        "enabled":           True,
        "timestamp_utc":     ts_now,
        "history_days":      n_days,
        "inputs": {
            "dvol_btc":          dvol_btc,
            "btc_rv_30d_ann":    btc_rv,
            "hype_rv_30d_ann":   hype_rv,         # contextual; not in VRP formula
            "funding_stress":    funding_stress,
            "oi_total_usd":      oi_total_usd,
            "oi_change_pct_24h": oi_change_pct_24h,
        },
        "vrp_btc":           vrp_btc,
        "vrp_z":             vrp_z,
        "funding_stress_z":  funding_z,
        "oi_stress_z":       oi_z,
        "risk_score":        risk_score,
        "risk_regime":       regime,
        "regime_multiplier": multiplier,
    }

    # ------------------------------------------------------------------
    # 10. Write output/risk_overlay.json
    # ------------------------------------------------------------------
    out_path = OUTPUT_DIR / "risk_overlay.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(_clean_for_json(overlay), fh, indent=2)

    _n_z = len(available)
    print(
        f"                   risk_overlay.json  "
        f"regime={regime}  score={risk_score}  "
        f"(z-scores available: {_n_z}/3  history: {n_days}d)",
        flush=True,
    )
    return overlay


def _clean_for_json(obj: object) -> object:
    """
    Recursively replace float NaN / ±Inf with None so json.dump never writes
    invalid JSON.  Python's json module serialises NaN as the bare token NaN
    (not quoted, not null) which is invalid per RFC 8259.
    """
    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_for_json(v) for v in obj]
    if isinstance(obj, float) and (obj != obj or abs(obj) == float("inf")):
        return None
    return obj
