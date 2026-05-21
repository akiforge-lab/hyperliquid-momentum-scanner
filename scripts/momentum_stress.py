#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
momentum_stress.py -- intraday momentum stress overlay.

Read-only.  No signing.  No private keys.  No order placement.

The daily GitHub Actions scan produces one *structural* ranking snapshot
(output/top_longs.csv, output/top_shorts.csv).  This script is the cheap
*live stress* layer that runs intraday on the DO host, independent of and
without re-triggering the expensive GitHub Actions scan.

For the top-N strongest signals in the latest ranking it:
  1. reads the structural ranking from GitHub raw (always current),
  2. fetches live mid prices from Hyperliquid's public `allMids` endpoint,
  3. compares the live price with the ranking's reference price, and
  4. sends a concise Telegram alert ONLY on a meaningful move:
       - a high-score LONG whose price is dumping  -> crowded-long unwind
       - a high-score SHORT whose price is pumping  -> short squeeze / invalidation
       - (optional) a large move that confirms the signal direction.

Per-coin cooldown suppresses repeat spam.  The first run seeds state
silently (no flood on deployment).

Configuration -- env vars / repo-root .env (gitignored), all optional:
  TG_BOT_TOKEN        Telegram bot token
  TG_CHAT_ID          Telegram chat / user id
  MS_TOP_N            signals to monitor          (default: 15)
  MS_DROP_PCT         LONG wrong-way drop %       (default: 8.0)
  MS_RISE_PCT         SHORT wrong-way rise %      (default: 8.0)
  MS_ALIGN_PCT        aligned-move % (0 disables) (default: 12.0)
  MS_COOLDOWN_HOURS   per-coin alert cooldown     (default: 6)

Credentials are read from the environment only -- never hardcoded.
Runtime state and logs live under the gitignored data/ tree.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import pathlib
import sys
import datetime

import requests

# ── paths ───────────────────────────────────────────────────────────────────
SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT   = SCRIPTS_DIR.parent
DATA_DIR    = REPO_ROOT / "data"
STATE_FILE  = DATA_DIR / "momentum_stress" / "state.json"
LOG_FILE    = DATA_DIR / "logs" / "momentum_stress.log"
# Credentials come from the process env or the repo-root .env (gitignored).
# An explicit override path may be supplied via HL_ENV_FILE (cron wrapper).
ENV_FILE    = pathlib.Path(os.environ.get("HL_ENV_FILE", REPO_ROOT / ".env"))

GITHUB_BASE = (
    "https://raw.githubusercontent.com"
    "/akiforge-lab/hyperliquid-momentum-scanner/main/output"
)
LONGS_URL   = f"{GITHUB_BASE}/top_longs.csv"
SHORTS_URL  = f"{GITHUB_BASE}/top_shorts.csv"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"   # public, read-only
HTTP_TIMEOUT = 20

# ── defaults (overridable via env) ──────────────────────────────────────────
DEFAULT_TOP_N          = 15
DEFAULT_DROP_PCT       = 8.0
DEFAULT_RISE_PCT       = 8.0
DEFAULT_ALIGN_PCT      = 12.0   # set MS_ALIGN_PCT=0 to disable aligned alerts
DEFAULT_COOLDOWN_HOURS = 6

# ── logging ─────────────────────────────────────────────────────────────────
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("momentum_stress")


# ── env / credentials ───────────────────────────────────────────────────────
def _load_env() -> None:
    """Populate os.environ from .env files (process env always wins)."""
    for p in (REPO_ROOT / ".env", ENV_FILE):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ── time helpers ────────────────────────────────────────────────────────────
def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


# ── data fetch ──────────────────────────────────────────────────────────────
def _fetch_csv(url: str) -> list[dict] | None:
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return list(csv.DictReader(io.StringIO(r.text)))
    except Exception as exc:
        log.warning("CSV fetch failed %s: %s", url, exc)
        return None


def load_signals(top_n: int) -> list[dict] | None:
    """
    Return the top-N strongest signals tradeable on Hyperliquid, each:
      {coin, signal, score, ref_price}
    Non-HL proxies (xyz: / yahoo:) are skipped -- they have no live HL mid.
    Returns None if both ranking files fail to fetch.
    """
    longs, shorts = _fetch_csv(LONGS_URL), _fetch_csv(SHORTS_URL)
    if longs is None and shorts is None:
        return None

    out: list[dict] = []
    for row in (longs or []) + (shorts or []):
        coin   = (row.get("coin") or "").strip()
        source = (row.get("source") or "").strip().lower()
        signal = (row.get("signal") or "").strip().upper()
        # Only real Hyperliquid perps -- xyz:/yahoo: proxies have no HL mid.
        if not coin or source != "hyperliquid" or signal not in ("LONG", "SHORT"):
            continue
        try:
            score = float(row.get("momentum_score") or 0)
            ref   = float(row.get("latest_price") or 0)
        except (TypeError, ValueError):
            continue
        if ref <= 0:
            continue
        out.append({"coin": coin, "signal": signal, "score": score, "ref_price": ref})

    out.sort(key=lambda s: -abs(s["score"]))
    return out[:top_n]


def fetch_mids() -> dict[str, float] | None:
    """Live mid prices for all HL perps via the public `allMids` endpoint."""
    try:
        r = requests.post(HL_INFO_URL, json={"type": "allMids"}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        raw = r.json()
    except Exception as exc:
        log.warning("allMids fetch failed: %s", exc)
        return None
    mids: dict[str, float] = {}
    for coin, px in (raw or {}).items():
        try:
            mids[coin] = float(px)
        except (TypeError, ValueError):
            continue
    return mids


# ── state ───────────────────────────────────────────────────────────────────
def load_state() -> dict | None:
    """Return persisted state, or None on the first run."""
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception as exc:
        log.warning("state load error: %s -- treating as empty", exc)
        return {}


def save_state(last_alert: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(
        {"updated_at": _iso(_now()), "last_alert": last_alert}, indent=2
    ))


# ── evaluation ──────────────────────────────────────────────────────────────
def evaluate(signals: list[dict], mids: dict[str, float],
             drop_pct: float, rise_pct: float, align_pct: float) -> list[dict]:
    """
    Classify each monitored signal against its live price move.
    Returns a list of alert dicts: {coin, signal, score, move_pct, kind, note}.
    kind is "WRONG-WAY" or "ALIGNED".
    """
    alerts: list[dict] = []
    for s in signals:
        coin, sig = s["coin"], s["signal"]
        live = mids.get(coin)
        if live is None or live <= 0:
            continue
        move = (live - s["ref_price"]) / s["ref_price"] * 100.0

        kind = note = None
        if sig == "LONG" and move <= -drop_pct:
            kind, note = "WRONG-WAY", "possible crowded-long unwind / weakening"
        elif sig == "SHORT" and move >= rise_pct:
            kind, note = "WRONG-WAY", "possible short squeeze / signal invalidation"
        elif align_pct > 0 and sig == "LONG" and move >= align_pct:
            kind, note = "ALIGNED", "LONG signal confirming -- up strongly"
        elif align_pct > 0 and sig == "SHORT" and move <= -align_pct:
            kind, note = "ALIGNED", "SHORT signal confirming -- down strongly"

        if kind:
            alerts.append({
                "coin": coin, "signal": sig, "score": s["score"],
                "move_pct": move, "kind": kind, "note": note,
            })
    return alerts


def _format(alerts: list[dict]) -> str:
    """Concise Telegram message. Wrong-way first, then aligned."""
    wrong   = [a for a in alerts if a["kind"] == "WRONG-WAY"]
    aligned = [a for a in alerts if a["kind"] == "ALIGNED"]
    lines = [f"<b>[MOMENTUM STRESS]</b> intraday overlay -- {len(alerts)} alert(s)"]

    def _row(a: dict) -> str:
        return (
            f"{a['kind']}: <b>{a['coin']}</b> {a['signal']} "
            f"(score {a['score']:+.0f}) price {a['move_pct']:+.1f}% since scan "
            f"-> {a['note']}."
        )

    if wrong:
        lines.append("")
        lines += [_row(a) for a in wrong]
    if aligned:
        lines.append("")
        lines += [_row(a) for a in aligned]
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            log.error("Telegram HTTP %d: %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as exc:
        log.error("Telegram send failed: %s", exc)
        return False


# ── main ────────────────────────────────────────────────────────────────────
def main() -> int:
    _load_env()
    top_n     = _env_int("MS_TOP_N", DEFAULT_TOP_N)
    drop_pct  = _env_float("MS_DROP_PCT", DEFAULT_DROP_PCT)
    rise_pct  = _env_float("MS_RISE_PCT", DEFAULT_RISE_PCT)
    align_pct = _env_float("MS_ALIGN_PCT", DEFAULT_ALIGN_PCT)
    cooldown  = _env_int("MS_COOLDOWN_HOURS", DEFAULT_COOLDOWN_HOURS)

    signals = load_signals(top_n)
    if not signals:
        log.warning("No tradeable signals available -- skipping run.")
        return 0
    mids = fetch_mids()
    if not mids:
        log.warning("No live prices available -- skipping run.")
        return 0

    alerts = evaluate(signals, mids, drop_pct, rise_pct, align_pct)
    log.info("Monitored %d signal(s); %d raw alert(s).", len(signals), len(alerts))

    state = load_state()
    if state is None:
        # First run: seed cooldown for anything already stressed, send nothing.
        seeded = {a["coin"]: _iso(_now()) for a in alerts}
        save_state(seeded)
        log.info("First run: seeded state (%d coin(s)), no notification sent.",
                 len(seeded))
        return 0

    last_alert: dict = dict(state.get("last_alert", {}))
    cutoff = _now() - datetime.timedelta(hours=cooldown)
    fresh: list[dict] = []
    for a in alerts:
        prev = _parse_iso(last_alert.get(a["coin"]))
        if prev and prev >= cutoff:
            continue  # still in cooldown
        fresh.append(a)
        last_alert[a["coin"]] = _iso(_now())

    # Drop cooldown entries older than the window so state stays small.
    last_alert = {c: t for c, t in last_alert.items()
                  if (_parse_iso(t) or cutoff) >= cutoff}

    if not fresh:
        log.info("No fresh alerts (cooldown suppressed %d).", len(alerts))
        save_state(last_alert)
        return 0

    token   = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    msg = _format(fresh)
    if token and chat_id:
        if send_telegram(token, chat_id, msg):
            log.info("Telegram sent (%d alert(s)).", len(fresh))
        else:
            # Don't persist -- retry on next run.
            return 1
    else:
        log.warning("TG_BOT_TOKEN / TG_CHAT_ID not set -- alert not sent:\n%s", msg)

    save_state(last_alert)
    return 0


if __name__ == "__main__":
    sys.exit(main())
