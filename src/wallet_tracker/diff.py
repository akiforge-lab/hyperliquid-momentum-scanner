# -*- coding: utf-8 -*-
"""
Compute meaningful position changes between two snapshots.

Returns a list of change dicts; the caller renders them for Telegram.
"""
from __future__ import annotations

from typing import Optional

from .config import MIN_NOTIONAL_USD, SIZE_CHANGE_PCT


def _notional(pos: dict) -> float:
    v = pos.get("position_value")
    if v is None:
        # Fallback: |szi| * entry_px when position_value is absent.
        szi = pos.get("szi") or 0
        px  = pos.get("entry_px") or 0
        return abs(float(szi) * float(px))
    return abs(float(v))


def _side(pos: dict) -> str:
    return "LONG" if (pos.get("szi") or 0) > 0 else "SHORT"


def diff_positions(
    prev: Optional[dict[str, dict]],
    curr: dict[str, dict],
) -> list[dict]:
    """
    Compare prev (may be None on first run) and curr maps of coin->position.

    Change kinds:
      OPEN              new position (above dust threshold)
      CLOSE             previously-tracked position is gone or now dust
      FLIP              side reversed (long -> short or vice versa)
      RESIZE            same side, abs(delta_exposure) / abs(size_old) >= SIZE_CHANGE_PCT
                        (carries direction=INCREASE|REDUCE based on whether
                         absolute exposure grew or shrank; change_pct is signed)
      LEVERAGE_CHANGE   leverage value or type changed (same side)

    On the very first run (prev is None) no notifications fire -- we just
    persist the initial snapshot.  This avoids a flood of "OPEN" messages
    the first time the tracker starts.
    """
    if prev is None:
        return []

    changes: list[dict] = []
    prev_keys = set(prev.keys())
    curr_keys = set(curr.keys())

    # ------------------------------------------------------------------
    # Opens -- in curr, not in prev (and not dust)
    # ------------------------------------------------------------------
    for coin in curr_keys - prev_keys:
        p = curr[coin]
        if _notional(p) >= MIN_NOTIONAL_USD:
            changes.append({
                "kind":     "OPEN",
                "coin":     coin,
                "side":     _side(p),
                "szi":      p.get("szi"),
                "notional": _notional(p),
                "leverage": p.get("leverage_value"),
            })

    # ------------------------------------------------------------------
    # Closes -- in prev, not in curr
    # ------------------------------------------------------------------
    for coin in prev_keys - curr_keys:
        p = prev[coin]
        if _notional(p) >= MIN_NOTIONAL_USD:
            changes.append({
                "kind":          "CLOSE",
                "coin":          coin,
                "side":          _side(p),
                "prev_szi":      p.get("szi"),
                "prev_notional": _notional(p),
            })

    # ------------------------------------------------------------------
    # Updates -- present in both
    # ------------------------------------------------------------------
    for coin in prev_keys & curr_keys:
        old, new = prev[coin], curr[coin]
        old_szi  = float(old.get("szi") or 0)
        new_szi  = float(new.get("szi") or 0)
        old_side = _side(old)
        new_side = _side(new)

        old_dust = _notional(old) < MIN_NOTIONAL_USD
        new_dust = _notional(new) < MIN_NOTIONAL_USD

        # Effective close: was non-dust, now dust
        if not old_dust and new_dust:
            changes.append({
                "kind":          "CLOSE",
                "coin":          coin,
                "side":          old_side,
                "prev_szi":      old_szi,
                "prev_notional": _notional(old),
            })
            continue
        # Effective open: was dust, now non-dust
        if old_dust and not new_dust:
            changes.append({
                "kind":     "OPEN",
                "coin":     coin,
                "side":     new_side,
                "szi":      new_szi,
                "notional": _notional(new),
                "leverage": new.get("leverage_value"),
            })
            continue
        if old_dust and new_dust:
            continue  # both noise

        # Side flip
        if old_side != new_side:
            changes.append({
                "kind":         "FLIP",
                "coin":         coin,
                "old_side":     old_side,
                "new_side":     new_side,
                "old_szi":      old_szi,
                "new_szi":      new_szi,
                "old_notional": _notional(old),
                "new_notional": _notional(new),
            })
            continue

        # Resize -- same side; classify by change in *absolute* exposure.
        # Positive pct => exposure grew (INCREASE); negative => exposure
        # shrank (REDUCE / cover).  This is direction-aware so a short
        # going -10 -> -8 reads as a reduction, not a "+20%" increase.
        old_abs = abs(old_szi)
        new_abs = abs(new_szi)
        if old_abs > 0:
            signed_pct = (new_abs - old_abs) / old_abs
            if abs(signed_pct) >= SIZE_CHANGE_PCT:
                changes.append({
                    "kind":         "RESIZE",
                    "direction":    "INCREASE" if new_abs > old_abs else "REDUCE",
                    "coin":         coin,
                    "side":         new_side,
                    "old_szi":      old_szi,
                    "new_szi":      new_szi,
                    "change_pct":   signed_pct,
                    "old_notional": _notional(old),
                    "new_notional": _notional(new),
                })

        # Leverage change
        old_lev_v = old.get("leverage_value")
        new_lev_v = new.get("leverage_value")
        old_lev_t = old.get("leverage_type")
        new_lev_t = new.get("leverage_type")
        if (old_lev_v != new_lev_v) or (old_lev_t != new_lev_t):
            changes.append({
                "kind":         "LEVERAGE_CHANGE",
                "coin":         coin,
                "side":         new_side,
                "old_leverage": f"{old_lev_t}/{old_lev_v}",
                "new_leverage": f"{new_lev_t}/{new_lev_v}",
            })

    return changes
