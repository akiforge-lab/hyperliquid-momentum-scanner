# Hyperliquid Momentum Scanner

Scans the full Hyperliquid perpetual-futures universe and ranks every asset
by a log-price regression momentum score, identifying top LONG and SHORT
candidates. Results are displayed in a live browser dashboard.

---

## Quick start

```bash
pip install -r requirements.txt

# Start the live dashboard (persistent server, Preview-compatible)
python app.py

# Or run the scanner once from the CLI
python main.py
```

Open **http://localhost:5000** in your browser (or use the Claude Code Preview
panel) to see the dashboard. Click **Run Scan** to fetch fresh data.

---

## How it works

| Step | What happens |
|------|--------------|
| 1 | Fetches every coin in the Hyperliquid perp universe via `POST /info {"type":"meta"}` |
| 2 | Downloads up to 220 daily candles per asset, trying **Hyperliquid first**, then **Yahoo Finance** as fallback |
| 3 | Filters out assets with fewer than **100** daily closes |
| 4 | Computes 90-day log-price regression **slope**, **R²**, **Momentum Score** (slope × R²), and **100-day MA** |
| 5 | Classifies **LONG** / **SHORT** / **NEUTRAL** signals |
| 6 | Writes CSV/JSON output files and serves results in the web dashboard |

---

## Price-source mapping

```
Coin type              Primary source            Fallback source
─────────────────────  ───────────────────────   ──────────────────────────
Standard crypto perps  Hyperliquid candleSnapshot  (none needed)
xyz: stocks (future)   Hyperliquid candleSnapshot  Yahoo Finance ticker
xyz: commodities       Hyperliquid candleSnapshot  Yahoo Finance futures (GC=F etc.)
xyz: indices           Hyperliquid candleSnapshot  Yahoo Finance index (^GSPC etc.)
Delisted crypto        Hyperliquid candleSnapshot  Yahoo Finance (COIN-USD)
```

### xyz: asset handling

When Hyperliquid adds tokenised-stock or commodity markets (e.g. `xyz:AAPL`,
`xyz:GOLD`, `xyz:SPX`), the scanner:

1. Tries the Hyperliquid `candleSnapshot` API with the exact coin name
   (`xyz:AAPL`) — this is the best source because it uses Hyperliquid's own
   oracle price.
2. If that returns no data, falls back to Yahoo Finance:
   - `xyz:AAPL` → `AAPL` (stock)
   - `xyz:GOLD` → `GC=F` (Gold futures)
   - `xyz:SPX`  → `^GSPC` (S&P 500)
   - `xyz:OIL`  → `CL=F` (WTI crude)

Coins that cannot be mapped at all are recorded in `output/missing_symbols.csv`
with a clear reason — they never crash the scan.

---

## Momentum metrics

| Column | Description |
|--------|-------------|
| `slope_ann_pct` | Annualised log-regression slope (252 × daily_slope × 100). Positive = uptrend. |
| `r2` | R² of the 90-day regression (0–1). Higher = smoother trend. |
| `momentum_score` | `slope_ann_pct × r2`. Combines trend magnitude and quality. |
| `ma100` | Simple 100-day moving average of closing price. |
| `source` | `hyperliquid` or `yahoo:<symbol>` — which source provided the price data. |

**Signal rules:**

| Signal | Condition |
|--------|-----------|
| LONG   | slope > 0 AND R² > 0.5 AND price > 100-DMA |
| SHORT  | slope < 0 AND R² > 0.5 AND price < 100-DMA |
| NEUTRAL | Everything else |

---

## Running the dashboard

```bash
python app.py               # default: http://localhost:5000
python app.py --port 8080   # custom port
```

The dashboard:
- Loads the last scan results from `output/` on startup
- **Run Scan** button triggers a fresh scan (uses cached candles; add
  `{"no_cache": true}` to the POST body to force re-fetch)
- Shows scan timestamp, summary cards, top 10 LONG, top 10 SHORT, and all
  missing/excluded symbols

---

## CLI flags

```bash
python main.py              # run scan using cached candle data
python main.py --no-cache   # delete cache and re-fetch everything from APIs
python main.py --verbose    # enable DEBUG logging
```

---

## Output files

| File | Description |
|------|-------------|
| `output/all_rankings.csv` | All assets that passed the ≥100-close filter, sorted by momentum score |
| `output/top_longs.csv` | Top 10 LONG candidates |
| `output/top_shorts.csv` | Top 10 SHORT candidates |
| `output/missing_symbols.csv` | Assets excluded due to no price data or insufficient history |
| `output/scan_summary.json` | Machine-readable scan metadata |
| `data/universe.json` | Raw universe API response |
| `data/candles/<COIN>.json` | Cached daily close data per coin |

---

## scan_summary.json fields

```json
{
  "scan_timestamp":                "2026-03-11T10:29:48Z",
  "universe_size":                 229,
  "assets_with_data":              210,
  "assets_sufficient_history":     197,
  "assets_excluded_no_data":       19,
  "assets_excluded_short_history": 13,
  "assets_computed":               197,
  "long_candidates":               5,
  "short_candidates":              153
}
```

---

## Project structure

```
hyperliquid-momentum/
├── app.py                    # Flask dashboard server (persistent)
├── main.py                   # CLI entry point + importable run_scan()
├── requirements.txt
├── README.md
├── src/
│   ├── price_sources.py      # HL candleSnapshot + Yahoo Finance fallback
│   ├── fetch_prices.py       # Per-coin fetching with caching
│   ├── compute_momentum.py   # Regression, R², momentum score, 100-DMA
│   └── output.py             # CSV/JSON writers + console printer
├── data/
│   ├── universe.json         # Cached universe API response
│   └── candles/              # Per-coin daily close cache (v2 format)
└── output/                   # Generated results
```

---

## Notes

- Candle data is cached in `data/candles/`. Repeat runs are near-instant.
  Use `--no-cache` (CLI) or `{"no_cache": true}` (API) to force re-fetch.
- The scanner is read-only — it never places orders.
- This is a research tool, not financial advice.
