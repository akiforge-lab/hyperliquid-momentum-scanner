# -*- coding: utf-8 -*-
"""
Configuration for the tracked-wallet overlay.

All thresholds are in code (not env) so changes are reviewable in git.
Runtime secrets (Telegram token / chat id) come from environment variables
and live only on the DO host -- never in this file.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Wallets to track (read-only -- never used to sign or trade)
# ---------------------------------------------------------------------------
TRACKED_WALLETS: list[str] = [
    "0x418aa6bf98a2b2bc93779f810330d88cde488888",
    "0x8def9f50456c6c4e37fa5d3d57f108ed23992dae",
]

# Human-friendly labels (optional).  Falls back to truncated address in messages.
WALLET_LABELS: dict[str, str] = {
    "0x418aa6bf98a2b2bc93779f810330d88cde488888": "58bro",
    "0x8def9f50456c6c4e37fa5d3d57f108ed23992dae": "Laurent",
}

# ---------------------------------------------------------------------------
# Change-detection thresholds
# ---------------------------------------------------------------------------
# Ignore positions whose notional value is below this -- avoids noisy dust alerts.
MIN_NOTIONAL_USD: float = 1_000.0

# Trigger a RESIZE notification when |size_new - size_old| / |size_old| >= this.
SIZE_CHANGE_PCT: float = 0.20   # 20 %

# Consecutive same-direction RESIZE events on the same coin within this many
# minutes are summarised as one running sequence ("A -> B in N min") instead
# of separate mechanical lines.  Purely a display aid -- detection is unchanged.
SEQUENCE_WINDOW_MIN: int = 30

# ---------------------------------------------------------------------------
# API + runtime
# ---------------------------------------------------------------------------
HL_INFO_URL: str = "https://api.hyperliquid.xyz/info"
API_TIMEOUT_SEC: int = 20

# Telegram (env-driven; both must be set for messages to be sent)
TELEGRAM_BOT_TOKEN_ENV: str = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV:   str = "TELEGRAM_CHAT_ID"
