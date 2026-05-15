# Scripts

Standalone, cron-runnable entry points. Library code lives in `src/`.

## `track_wallets.py` — tracked-wallet overlay

Read-only Hyperliquid position monitor. Fetches positions for a fixed
watchlist (defined in `src/wallet_tracker/config.py`), diffs against a
local JSON snapshot under `data/wallet_state/`, and sends a Telegram
message for meaningful changes (open, close, flip, ≥20% resize, leverage
change).

**No signing. No private keys. No order placement.**

### Run locally (dry-run)

```bash
python scripts/track_wallets.py --dry-run
```

This fetches and logs changes to stdout but never calls Telegram. The
state file is still written, so the first invocation seeds the baseline.

### Deploy on DigitalOcean

#### One-time setup

```bash
# 1. Clone (or git pull) the repo
sudo mkdir -p /opt/hyperliquid-momentum
sudo chown "$USER" /opt/hyperliquid-momentum
git clone https://github.com/akiforge-lab/hyperliquid-momentum-scanner.git \
    /opt/hyperliquid-momentum
cd /opt/hyperliquid-momentum

# 2. Create venv and install deps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Configure Telegram credentials (NOT committed)
cp .env.example .env
$EDITOR .env   # fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID

# 4. First run — seeds state with no alerts
set -a; source .env; set +a
.venv/bin/python scripts/track_wallets.py

# 5. Verify state files exist
ls data/wallet_state/
```

#### Cron line (every 5 minutes)

The cron line uses a small wrapper because cron does not source `.env`:

```cron
*/5 * * * * /opt/hyperliquid-momentum/scripts/run_track_wallets.sh >> /var/log/track_wallets.log 2>&1
```

Create `scripts/run_track_wallets.sh` on the DO host (not in the repo):

```bash
#!/usr/bin/env bash
set -e
cd /opt/hyperliquid-momentum
set -a; source .env; set +a
.venv/bin/python scripts/track_wallets.py
```

```bash
chmod +x scripts/run_track_wallets.sh
crontab -e   # paste the cron line above
```

#### Updating code

```bash
cd /opt/hyperliquid-momentum
git pull
# data/wallet_state/ and .env are gitignored — pull won't disturb them
```

### What counts as a "meaningful change"

| Kind | Trigger |
|---|---|
| `OPEN` | Coin appeared and notional ≥ `MIN_NOTIONAL_USD` ($1,000) |
| `CLOSE` | Coin disappeared (or fell below dust threshold) |
| `FLIP` | Sign of `szi` reversed (long ↔ short) |
| `RESIZE` | Same side, ‖Δsize‖ / ‖size_old‖ ≥ `SIZE_CHANGE_PCT` (20%) |
| `LEVERAGE_CHANGE` | Leverage type or value changed |

Tune in `src/wallet_tracker/config.py`. State files use the full snapshot,
not deltas, so changing thresholds takes effect on the next run without
needing to re-seed.

### Relationship to GitHub Actions daily scan

Independent. The daily scan runs on GHA ephemeral runners and commits
results to `output/`. The wallet tracker runs on DO with persistent local
state in `data/wallet_state/` (gitignored). Neither touches the other's
state.
