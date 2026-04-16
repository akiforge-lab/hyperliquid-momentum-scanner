# -*- coding: utf-8 -*-
"""
Risk overlay configuration — all thresholds in one place for easy tuning.

Run order: the risk overlay is collected BEFORE the main momentum scan so
that the current regime is visible alongside trade signals.  The overlay
never modifies momentum_score; regime_multiplier is output-only in v1.

Enable with:  python main.py --risk-overlay [...]
"""

# ---------------------------------------------------------------------------
# External API endpoints
# ---------------------------------------------------------------------------
DERIBIT_DVOL_URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
HL_API_URL       = "https://api.hyperliquid.xyz/info"
API_TIMEOUT      = 20  # seconds per request

# ---------------------------------------------------------------------------
# Rolling windows
# ---------------------------------------------------------------------------
RV_WINDOW_DAYS      = 30   # days of log-returns for realized volatility
ZSCORE_WINDOW_DAYS  = 90   # max lookback for rolling z-score normalisation
MIN_HISTORY_DAYS    = 14   # below this, z-scores return None; regime = "unknown"
OI_LOOKBACK_HOURS   = 24   # target age of history entry for ΔOI% calculation
OI_TOLERANCE_HOURS  = 4    # accept history entry within ±N hours of target

# ---------------------------------------------------------------------------
# Regime thresholds  (applied to composite risk_score)
# ---------------------------------------------------------------------------
RISK_OFF_THRESHOLD = 1.5   # risk_score > this → risk_off
RISK_ON_THRESHOLD  = -1.0  # risk_score < this → risk_on

# regime_multiplier is output-only in v1 (not applied to momentum_score)
REGIME_MULTIPLIERS: dict[str, float] = {
    "risk_off": 0.5,
    "neutral":  1.0,
    "risk_on":  1.1,
    "unknown":  1.0,
}

# ---------------------------------------------------------------------------
# Annotation thresholds  (informational flags in output, not regime logic)
# ---------------------------------------------------------------------------
FUNDING_EXTREME_ABS   = 0.005   # |funding| > 0.5% per 8h → flag as extreme
OI_DROP_EXTREME_PCT   = -5.0    # ΔOI > −5% in 24h → flag as stressed
VRP_PREMIUM_THRESHOLD = 15.0    # dvol_btc − btc_rv > 15 pts → vol premium
