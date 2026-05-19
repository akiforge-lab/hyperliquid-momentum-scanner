#!/usr/bin/env python3
"""
hl_notify.py — Telegram notifier for hyperliquid-momentum-scanner signals.

Fetches the latest individual-asset rankings from GitHub, compares with
persisted local state, and sends a Telegram message only on meaningful
signal lifecycle events (in priority order):

  1. strengthened       — existing in-set signal materially improved
  2. reentered_stronger — coin previously dropped from the set comes back with
                          materially higher score in the same direction
  3. flip               — direction reversed (LONG ↔ SHORT)
  4. new                — coin newly entered the set with no recent history
  5. dropped            — coin left the published top set (conservative: may be
                          rank slippage rather than signal death)

First run: silently writes initial state without sending any message
(prevents a startup flood of all currently-active signals).

Configuration — set as env vars or in the repo-root .env (gitignored):
  TG_BOT_TOKEN   Telegram bot token
  TG_CHAT_ID     Telegram chat / user ID to deliver to
Credentials are read from the environment / .env only — never hardcoded.
Runtime state, cache and logs live under the gitignored data/ tree.

Optional tuning (env vars, sensible defaults for Hyperliquid):
  HL_MIN_SCORE   Min |momentum_score| to qualify  (default: 100)
  HL_MIN_R2      Min R²            to qualify  (default: 0.70)
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
import datetime

# ── paths ──────────────────────────────────────────────────────────────────────

GITHUB_BASE = (
    "https://raw.githubusercontent.com"
    "/akiforge-lab/hyperliquid-momentum-scanner/main/output"
)
LONGS_URL  = f"{GITHUB_BASE}/top_longs.csv"
SHORTS_URL = f"{GITHUB_BASE}/top_shorts.csv"

# Repo-runnable layout: this file lives in <repo>/scripts/, so the repo root
# is its parent's parent.  All runtime artefacts live under the gitignored
# data/ tree so nothing per-host is ever committed.
SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT   = SCRIPTS_DIR.parent
DATA_DIR    = REPO_ROOT / "data"
STATE_FILE  = DATA_DIR / "notifier_state" / ".notifier_state.json"
LOG_FILE    = DATA_DIR / "logs" / "hl_notify.log"
# Credentials come from the process env or the repo-root .env (gitignored).
# An explicit override path may be supplied via HL_ENV_FILE (used by the
# local cron wrapper) so no host-specific path is hardcoded here.
ENV_FILE    = pathlib.Path(os.environ.get("HL_ENV_FILE", REPO_ROOT / ".env"))

# ── defaults ───────────────────────────────────────────────────────────────────

DEFAULT_MIN_SCORE = 100.0
DEFAULT_MIN_R2    = 0.70
EXCLUDED_COINS    = frozenset({"FXS"})

# ── strengthened event thresholds ─────────────────────────────────────────────
# Absolute rank / absolute score is the PRIMARY driver of a "strengthened"
# event.  Rank *movement* is only a SECONDARY acceleration signal — a big jump
# is meaningless if the coin still ends up at a weak absolute rank (it usually
# just means other coins left the set, not genuine strength).
#
# A coin is "strengthened" if ANY of these hold:
#   (a) top-tier persistence — it is currently in the absolute top tier
#       (rank ≤ TOP_TIER_RANK) and its score improved even modestly.  This is
#       the strongest, most reliable signal: a coin that is *and stays* at the
#       top while still improving.
#   (b) absolute score surge — score increased by ≥ STRENGTHEN_SCORE_ONLY,
#       regardless of where it sits in the ranking.
#   (c) rank acceleration (secondary) — rank improved by
#       ≥ STRENGTHEN_MIN_RANK_GAIN AND score improved by
#       ≥ STRENGTHEN_MIN_SCORE_GAIN AND the coin actually lands within a real
#       absolute band (rank ≤ MOVEMENT_MAX_RANK).  The absolute-band guard is
#       what keeps "large move from a weak rank" from notifying.
#
# Rationale against current data (scores ~115–370, tight cluster at ranks 7–10):
#   Top-tier coins carry the signal, so they need only a small score gain.
#   80-pt score-only gain catches a coin surging even if its rank held.
#   The rank path keeps its old thresholds but is now band-limited.
TOP_TIER_RANK                 = 5   # absolute top tier (primary signal)
STRENGTHEN_TOPTIER_SCORE_GAIN = 15  # modest score gain suffices when top-tier
STRENGTHEN_SCORE_ONLY         = 80  # score gain alone, large enough to notify
STRENGTHEN_MIN_RANK_GAIN      = 3   # positions moved up (rank number decreased)
STRENGTHEN_MIN_SCORE_GAIN     = 30  # absolute |momentum_score| increase
MOVEMENT_MAX_RANK             = 10  # rank-acceleration path only within this band

# ── re-entry memory ───────────────────────────────────────────────────────────
# When a coin drops from the qualifying set we remember its last-known signal
# data for HISTORY_RETENTION_DAYS.  If it re-enters the set in the same direction
# with score >= last_known + REENTRY_MIN_SCORE_GAIN, classify as
# "reentered_stronger" rather than an ordinary "new" entry — that's where the
# real lifecycle story is (left and came back materially stronger).
HISTORY_RETENTION_DAYS = 14
REENTRY_MIN_SCORE_GAIN = 20

# ── logging ────────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── env / credentials ─────────────────────────────────────────────────────────

def _load_env() -> None:
    # Process env always wins (lines below only set keys not already set).
    # Sources: repo-root .env, then the optional HL_ENV_FILE override.
    for p in (REPO_ROOT / ".env", ENV_FILE):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"{name} is not set. Add it to {ENV_FILE} or export it."
        )
    return val


# ── GitHub fetch ───────────────────────────────────────────────────────────────

def _fetch_csv(url: str) -> tuple[list[dict] | None, str | None]:
    """
    Fetch a CSV from url.
    Returns (rows, snapshot_ts) where snapshot_ts is a compact UTC string.
    Prefers Last-Modified; falls back to Date − Source-Age (GitHub CDN
    serves Date + Source-Age instead of Last-Modified on raw content URLs).
    Returns (None, None) on any network/parse error.
    """
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            raw         = r.read().decode("utf-8")
            snapshot_ts = _extract_snapshot_ts(r.headers)
        return list(csv.DictReader(io.StringIO(raw))), snapshot_ts
    except Exception as exc:
        log.warning("fetch failed %s: %s", url, exc)
        return None, None


def _extract_snapshot_ts(headers) -> str | None:
    """
    Derive a compact UTC timestamp from HTTP response headers.
    Priority: Last-Modified → Date − Source-Age → Date.
    Returns a string like '22 Apr 14:03 UTC', or None on failure.
    """
    import email.utils

    def _parse_rfc1123(s: str | None) -> datetime.datetime | None:
        if not s:
            return None
        try:
            return email.utils.parsedate_to_datetime(s)
        except Exception:
            return None

    last_mod = _parse_rfc1123(headers.get("Last-Modified"))
    if last_mod:
        return last_mod.strftime("%-d %b %H:%M UTC")

    date_dt = _parse_rfc1123(headers.get("Date"))
    if date_dt:
        try:
            age_secs = int(headers.get("Source-Age") or headers.get("Age") or 0)
        except (ValueError, TypeError):
            age_secs = 0
        ts = date_dt - datetime.timedelta(seconds=age_secs)
        return ts.strftime("%-d %b %H:%M UTC")

    return None


def load_rankings(min_score: float, min_r2: float) -> tuple[dict[str, dict] | None, str | None]:
    """
    Fetch top_longs + top_shorts from GitHub and return
    (qualifying_dict, snapshot_ts).

    qualifying_dict is {coin: {signal, momentum_score, r2, slope_ann_pct}}.
    Returns (None, None) if both fetches fail (data unavailable, skip run).
    snapshot_ts is the Last-Modified timestamp of the fetched files, or None.
    """
    longs_rows,  longs_ts  = _fetch_csv(LONGS_URL)
    shorts_rows, shorts_ts = _fetch_csv(SHORTS_URL)
    snapshot_ts = longs_ts or shorts_ts

    if longs_rows is None and shorts_rows is None:
        log.warning("Both longs and shorts fetch failed — skipping run")
        return None, None

    rows = (longs_rows or []) + (shorts_rows or [])

    # Collect qualifying rows first, then rank by score so rank is stable.
    candidates: list[tuple[float, str, dict]] = []
    for row in rows:
        coin = (row.get("coin") or "").strip().upper()
        if not coin or coin in EXCLUDED_COINS:
            continue
        try:
            score     = abs(float(row.get("momentum_score") or 0))
            r2        = float(row.get("r2") or 0)
            sig       = (row.get("signal") or "NEUTRAL").strip().upper()
            slope_pct = float(row.get("slope_ann_pct") or 0)
        except (ValueError, TypeError):
            continue
        if score >= min_score and r2 >= min_r2 and sig in ("LONG", "SHORT"):
            candidates.append((score, coin, {
                "signal":         sig,
                "momentum_score": round(score, 2),
                "r2":             round(r2, 4),
                "slope_ann_pct":  round(slope_pct, 2),
            }))

    candidates.sort(key=lambda t: -t[0])
    qualified: dict[str, dict] = {
        coin: {**data, "rank": rank}
        for rank, (_, coin, data) in enumerate(candidates, 1)
    }

    log.info(
        "Fetched %d qualifying signals (min_score=%.0f, min_r2=%.2f, snapshot=%s)",
        len(qualified), min_score, min_r2, snapshot_ts or "unknown",
    )
    return qualified, snapshot_ts


# ── state persistence ─────────────────────────────────────────────────────────

def load_state() -> tuple[dict, dict] | None:
    """
    Load persisted state.
    Returns (signals, history) on subsequent runs.
    Returns None if the state file does not exist (first run).
    Returns ({}, {}) if file is present but unreadable.
    """
    if not STATE_FILE.exists():
        return None
    try:
        obj = json.loads(STATE_FILE.read_text())
        return obj.get("signals", {}), obj.get("history", {})
    except Exception as exc:
        log.warning("state load error: %s — treating as empty", exc)
        return {}, {}


def _prune_history(history: dict) -> dict:
    """Drop history entries older than HISTORY_RETENTION_DAYS."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=HISTORY_RETENTION_DAYS
    )
    pruned: dict[str, dict] = {}
    for coin, data in history.items():
        ts = _parse_iso(data.get("last_seen_at"))
        if ts and ts >= cutoff:
            pruned[coin] = data
    return pruned


def save_state(
    signals: dict,
    history: dict,
    alerted_coins: set[str] | None = None,
) -> None:
    """
    Persist signal + history state.  alerted_coins entries get an alerted_at
    timestamp stamped onto their signal record (for future cooldown use).
    History is pruned to HISTORY_RETENTION_DAYS at every save.
    """
    now = _now_iso()
    stored: dict[str, dict] = {}
    for coin, data in signals.items():
        entry = dict(data)
        if alerted_coins and coin in alerted_coins:
            entry["alerted_at"] = now
        stored[coin] = entry
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "updated_at": now,
        "signals":    stored,
        "history":    _prune_history(history),
    }, indent=2))


# ── event detection ────────────────────────────────────────────────────────────

def compute_events(prev: dict, curr: dict, history: dict | None = None) -> list[dict]:
    """
    Compare prev and curr qualifying signal dicts, consulting history for
    coins that recently dropped.

    Returns list of event dicts with type in
    {"strengthened", "reentered_stronger", "flip", "new", "dropped"}.

    Priority rationale:
      strengthened       — in-set signal materially improved
      reentered_stronger — previously-dropped coin re-enters with stronger signal
      flip               — direction changed; takes precedence over strengthening
      new                — first sighting (no recent history)
      dropped            — coin left the published top set (conservative)
    """
    history = history or {}
    events: list[dict] = []
    prev_coins = set(prev)
    curr_coins = set(curr)

    # ── New entrants — check history for a recent appearance first. ──────────
    for coin in sorted(curr_coins - prev_coins):
        c    = curr[coin]
        hist = history.get(coin)
        reentry_event = None
        if hist and hist.get("signal") == c["signal"]:
            score_gain = c["momentum_score"] - hist.get("momentum_score", 0)
            if score_gain >= REENTRY_MIN_SCORE_GAIN:
                reentry_event = {
                    "type":          "reentered_stronger",
                    "coin":          coin,
                    "prev_rank":     hist.get("rank"),
                    "prev_score":    hist.get("momentum_score"),
                    "rank_gain":     (hist.get("rank") - c["rank"])
                                      if (hist.get("rank") and c.get("rank")) else 0,
                    "score_gain":    round(score_gain, 2),
                    "last_seen_at":  hist.get("last_seen_at"),
                    **c,
                }
        events.append(reentry_event or {"type": "new", "coin": coin, **c})

    for coin in sorted(prev_coins - curr_coins):
        events.append({"type": "dropped", "coin": coin, **prev[coin]})

    for coin in sorted(prev_coins & curr_coins):
        p, c = prev[coin], curr[coin]

        # Flip takes priority — a direction change overrides any strength comparison.
        if p["signal"] != c["signal"]:
            events.append({
                "type":        "flip",
                "coin":        coin,
                "prev_signal": p["signal"],
                **c,
            })
            continue

        # Strengthened: coin held direction but improved materially.
        score_gain = c["momentum_score"] - p["momentum_score"]
        # prev may lack "rank" if state was written before rank tracking was added;
        # fall back to None and skip the combined (rank + score) gate in that case.
        prev_rank = p.get("rank")
        curr_rank = c.get("rank")
        rank_gain = (prev_rank - curr_rank) if (prev_rank and curr_rank) else 0

        # Primary: absolute rank / absolute score.
        in_top_tier   = bool(curr_rank) and curr_rank <= TOP_TIER_RANK
        toptier_gate  = in_top_tier and score_gain >= STRENGTHEN_TOPTIER_SCORE_GAIN
        score_gate    = score_gain >= STRENGTHEN_SCORE_ONLY

        # Secondary: rank acceleration, but only if it lands in a real
        # absolute band — a big jump that still ends weak does not count.
        movement_gate = (
            rank_gain  >= STRENGTHEN_MIN_RANK_GAIN
            and score_gain >= STRENGTHEN_MIN_SCORE_GAIN
            and bool(curr_rank) and curr_rank <= MOVEMENT_MAX_RANK
        )

        if toptier_gate or score_gate or movement_gate:
            events.append({
                "type":       "strengthened",
                "coin":       coin,
                "prev_rank":  prev_rank,
                "prev_score": p["momentum_score"],
                "rank_gain":  rank_gain,
                "score_gain": round(score_gain, 2),
                **c,
            })

    return events


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(token: str, chat_id: str, text: str) -> None:
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "HTML",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Telegram HTTP {exc.code}: {body[:200]}") from exc
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram API error: {resp}")


def format_message(events: list[dict], snapshot_ts: str | None = None) -> str:
    strengthened = [e for e in events if e["type"] == "strengthened"]
    reentered    = [e for e in events if e["type"] == "reentered_stronger"]
    flips        = [e for e in events if e["type"] == "flip"]
    new_sigs     = [e for e in events if e["type"] == "new"]
    dropped      = [e for e in events if e["type"] == "dropped"]

    # Prefer the file's own Last-Modified timestamp; fall back to current time.
    ts_line    = f"<i>snapshot {snapshot_ts}</i>" if snapshot_ts else \
                 f"<i>{_now_iso()[:16].replace('T', ' ')} UTC</i>"
    parts      = []
    ts_emitted = False

    def _ts() -> list[str]:
        nonlocal ts_emitted
        if ts_emitted:
            return []
        ts_emitted = True
        return [ts_line, ""]

    # ── 1. Strengthened (highest priority) ───────────────────────────────────
    # Absolute rank is the primary ordering key (lower rank = stronger);
    # score is the tiebreak and the fallback when rank is absent.
    def _rank_key(e: dict):
        return (e.get("rank") or 9_999, -e["momentum_score"])

    if strengthened:
        by_score = sorted(strengthened, key=_rank_key)
        count    = len(by_score)
        lines    = [f"<b>📈 Signal{'s' if count > 1 else ''} strengthened — {count}</b>"]
        lines   += _ts()
        for e in by_score:
            emoji     = "🟢" if e["signal"] == "LONG" else "🔴"
            rank_part = ""
            if e.get("rank"):
                moved = e.get("prev_rank") and e["prev_rank"] != e["rank"]
                rank_part = f"  #{e['rank']} (was #{e['prev_rank']})" if moved \
                            else f"  #{e['rank']}"
            score_delta = f" (+{e['score_gain']:.1f})" if e.get("score_gain", 0) > 0 else ""
            lines.append(f"{emoji} <b>{e['coin']}</b>  {e['signal']}{rank_part}")
            lines.append(
                f"   score {e['momentum_score']:.1f}{score_delta}"
                f"  R² {e['r2']:.3f}"
            )
        parts.append("\n".join(lines))

    # ── 1b. Re-entered stronger ──────────────────────────────────────────────
    if reentered:
        by_score = sorted(reentered, key=_rank_key)
        count    = len(by_score)
        lines    = [f"<b>🔁 Re-entered stronger — {count}</b>"]
        lines   += _ts()
        for e in by_score:
            emoji     = "🟢" if e["signal"] == "LONG" else "🔴"
            rank_part = ""
            if e.get("rank"):
                if e.get("prev_rank") and e["prev_rank"] != e["rank"]:
                    rank_part = f"  #{e['rank']} (was #{e['prev_rank']})"
                else:
                    rank_part = f"  #{e['rank']}"
            score_delta = f" (+{e['score_gain']:.1f} vs last)" if e.get("score_gain", 0) > 0 else ""
            ago_days    = _days_ago(e.get("last_seen_at"))
            ago_part    = f"  last seen {ago_days}d ago" if ago_days is not None else ""
            lines.append(f"{emoji} <b>{e['coin']}</b>  {e['signal']}{rank_part}")
            lines.append(
                f"   score {e['momentum_score']:.1f}{score_delta}"
                f"  R² {e['r2']:.3f}{ago_part}"
            )
        parts.append("\n".join(lines))

    # ── 2. Flips ──────────────────────────────────────────────────────────────
    if flips:
        by_score = sorted(flips, key=lambda e: -e["momentum_score"])
        count    = len(by_score)
        lines    = [f"<b>🔄 Signal flip{'s' if count > 1 else ''} — {count}</b>"]
        lines   += _ts()
        for e in by_score:
            old_e = "🟢" if e["prev_signal"] == "LONG" else "🔴"
            new_e = "🟢" if e["signal"] == "LONG" else "🔴"
            rank_part = f"  #{e['rank']}" if e.get("rank") else ""
            lines.append(
                f"{old_e}→{new_e} <b>{e['coin']}</b>"
                f"  {e['prev_signal']}→{e['signal']}{rank_part}"
            )
            lines.append(f"   score {e['momentum_score']:.1f}  R² {e['r2']:.3f}")
        parts.append("\n".join(lines))

    # ── 3. New signals ────────────────────────────────────────────────────────
    if new_sigs:
        by_score = sorted(new_sigs, key=_rank_key)
        count    = len(by_score)
        lines    = [f"<b>📌 New signal{'s' if count > 1 else ''} — {count}</b>"]
        lines   += _ts()
        for e in by_score:
            emoji     = "🟢" if e["signal"] == "LONG" else "🔴"
            rank_part = f"  #{e['rank']}" if e.get("rank") else ""
            lines.append(f"{emoji} <b>{e['coin']}</b>  {e['signal']}{rank_part}")
            lines.append(
                f"   score {e['momentum_score']:.1f}"
                f"  slope {e['slope_ann_pct']:+.1f}%"
                f"  R² {e['r2']:.3f}"
            )
        parts.append("\n".join(lines))

    # ── 4. Dropped (lowest priority, conservative) ───────────────────────────
    if dropped:
        by_score = sorted(dropped, key=lambda e: -e["momentum_score"])
        count    = len(by_score)
        lines    = [f"<b>⬇️ Dropped from qualifying set — {count}</b>"]
        lines   += _ts()
        for e in by_score:
            emoji     = "🟢" if e["signal"] == "LONG" else "🔴"
            rank_part = f"  (was #{e['rank']})" if e.get("rank") else ""
            lines.append(
                f"{emoji} <b>{e['coin']}</b>  {e['signal']}{rank_part}"
                f"  (no longer in published top set)"
            )
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


# ── utilities ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _parse_iso(s: str | None) -> datetime.datetime | None:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _days_ago(iso_ts: str | None) -> int | None:
    """Return integer days since the given UTC ISO timestamp, or None."""
    ts = _parse_iso(iso_ts)
    if not ts:
        return None
    delta = datetime.datetime.now(datetime.timezone.utc) - ts
    return max(0, int(delta.total_seconds() // 86400))


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_env()

    min_score = float(os.environ.get("HL_MIN_SCORE", DEFAULT_MIN_SCORE))
    min_r2    = float(os.environ.get("HL_MIN_R2",    DEFAULT_MIN_R2))

    try:
        token   = _require_env("TG_BOT_TOKEN")
        chat_id = _require_env("TG_CHAT_ID")
    except RuntimeError as exc:
        log.error("Config error: %s", exc)
        sys.exit(1)

    curr, snapshot_ts = load_rankings(min_score, min_r2)
    if curr is None:
        # Both fetches failed — don't touch state, try again next cron run.
        sys.exit(0)

    state = load_state()

    if state is None:
        # First run — seed state silently to avoid startup flood.
        log.info(
            "First run: seeding state with %d signal(s), no notification sent.",
            len(curr),
        )
        save_state(curr, history={})
        return

    prev, history = state
    events = compute_events(prev, curr, history)

    # Build the next history: prune-by-retention happens inside save_state.
    # 1. Carry forward all current history entries (they'll be pruned on save).
    # 2. Record any coin that just dropped (with its last-known data + timestamp).
    # 3. Remove any coin that just re-entered the qualifying set (it's in signals now).
    next_history: dict[str, dict] = dict(history)
    now_ts = _now_iso()
    for e in events:
        if e["type"] == "dropped":
            next_history[e["coin"]] = {
                "signal":         prev[e["coin"]]["signal"],
                "momentum_score": prev[e["coin"]]["momentum_score"],
                "r2":             prev[e["coin"]].get("r2"),
                "slope_ann_pct":  prev[e["coin"]].get("slope_ann_pct"),
                "rank":           prev[e["coin"]].get("rank"),
                "last_seen_at":   now_ts,
            }
        elif e["coin"] in next_history and e["coin"] in curr:
            # New, reentered_stronger, flip, strengthened — coin is back in-set.
            next_history.pop(e["coin"], None)

    if not events:
        log.info("No changes (tracking %d signal(s))", len(curr))
        save_state(curr, next_history)
        return

    event_summary = ", ".join(f"{e['type']}:{e['coin']}" for e in events)
    log.info("%d event(s): %s", len(events), event_summary)

    msg = format_message(events, snapshot_ts)
    try:
        send_telegram(token, chat_id, msg)
        log.info("Telegram message sent (%d chars)", len(msg))
    except Exception as exc:
        log.error("Telegram send failed: %s", exc)
        # Don't save state — retry comparison on next run.
        sys.exit(1)

    # Stamp alerted_at on coins that triggered an actionable event.
    # "dropped" coins are not in curr so they won't appear in signals anyway.
    alerted_coins = {e["coin"] for e in events if e["type"] != "dropped"}
    save_state(curr, next_history, alerted_coins=alerted_coins or None)


if __name__ == "__main__":
    main()
