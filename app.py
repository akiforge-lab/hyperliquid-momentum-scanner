# -*- coding: utf-8 -*-
"""
Hyperliquid Momentum Dashboard - persistent Flask web server.

Run:  python app.py
Open: http://localhost:5000
"""
import json
import logging
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    format="%(asctime)s  %(levelname)-7s  %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
    stream=sys.stderr,
)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

from src.paths import OUTPUT_DIR
_scan_lock       = threading.Lock()
_pairs_scan_lock = threading.Lock()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# HTML Dashboard
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HL Momentum Scanner</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg:       #f0f2f5;
      --surface:  #ffffff;
      --border:   #e0e3ea;
      --text:     #1a1d2e;
      --muted:    #6b7280;
      --green:    #16a34a;
      --green-bg: #f0fdf4;
      --red:      #dc2626;
      --red-bg:   #fef2f2;
      --blue:     #2563eb;
      --blue-bg:  #eff6ff;
      --radius:   8px;
      --shadow:   0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.05);
    }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      font-size: 14px;
      line-height: 1.5;
    }

    /* ---- Header ---- */
    header {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 14px 24px;
      display: flex;
      align-items: center;
      gap: 16px;
      position: sticky;
      top: 0;
      z-index: 10;
      box-shadow: var(--shadow);
    }
    header h1 { font-size: 17px; font-weight: 700; flex: 1; }
    header h1 span { color: var(--blue); }
    #timestamp { font-size: 12px; color: var(--muted); }
    #scan-btn {
      padding: 7px 18px;
      background: var(--blue);
      color: #fff;
      border: none;
      border-radius: var(--radius);
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 8px;
      transition: opacity .15s;
    }
    #scan-btn:disabled { opacity: .55; cursor: not-allowed; }
    #scan-btn svg { animation: spin 1s linear infinite; display: none; }
    #scan-btn.loading svg { display: block; }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* ---- Main ---- */
    main { max-width: 1280px; margin: 0 auto; padding: 20px 24px 40px; }

    /* ---- Empty state ---- */
    #empty {
      text-align: center;
      padding: 80px 20px;
      color: var(--muted);
    }
    #empty h2 { font-size: 22px; margin-bottom: 8px; color: var(--text); }
    #empty p  { margin-bottom: 20px; }

    /* ---- Summary cards ---- */
    #cards {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 14px 16px;
      box-shadow: var(--shadow);
    }
    .card .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
    .card .value { font-size: 26px; font-weight: 700; margin-top: 2px; }
    .card.green .value { color: var(--green); }
    .card.red   .value { color: var(--red);   }
    .card.blue  .value { color: var(--blue);  }

    /* ---- Tables layout ---- */
    #tables {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-bottom: 16px;
    }
    @media (max-width: 900px) { #tables { grid-template-columns: 1fr; } }

    /* ---- Panel ---- */
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .panel-header {
      padding: 12px 16px;
      font-weight: 700;
      font-size: 13px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .panel-header.long  { background: var(--green-bg); color: var(--green); }
    .panel-header.short { background: var(--red-bg);   color: var(--red);   }
    .panel-header.miss  { background: var(--blue-bg);  color: var(--blue);  }

    /* ---- Data table ---- */
    .tbl { width: 100%; border-collapse: collapse; font-size: 12.5px; }
    .tbl th {
      text-align: left;
      padding: 8px 12px;
      background: #f8f9fb;
      border-bottom: 1px solid var(--border);
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .04em;
      white-space: nowrap;
    }
    .tbl td {
      padding: 7px 12px;
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
    }
    .tbl tr:last-child td { border-bottom: none; }
    .tbl tr:hover td { background: #f8f9fb; }
    .coin  { font-weight: 700; font-family: monospace; font-size: 13px; }
    .long  { color: var(--green); font-weight: 600; }
    .short { color: var(--red);   font-weight: 600; }
    .num   { text-align: right; font-variant-numeric: tabular-nums; }
    .src   { font-size: 11px; color: var(--muted); }
    .empty-row td { text-align: center; color: var(--muted); padding: 24px; }

    /* ---- Missing panel (full width) ---- */
    #missing-panel { margin-bottom: 16px; }

    /* ---- xyz: panel ---- */
    .panel-header.xyz { background: #fdf4ff; color: #7c3aed; }
    .badge {
      display: inline-block;
      padding: 1px 7px;
      border-radius: 12px;
      font-size: 11px;
      font-weight: 600;
    }
    .badge-ok      { background: #f0fdf4; color: #16a34a; }
    .badge-warn    { background: #fffbeb; color: #b45309; }
    .badge-err     { background: #fef2f2; color: #dc2626; }
    .badge-neutral { background: #f1f5f9; color: #475569; }

    /* ---- Tab navigation ---- */
    .tab-nav {
      display: flex;
      gap: 0;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 0 24px;
      position: sticky;
      top: 57px;
      z-index: 9;
      box-shadow: 0 1px 0 var(--border);
    }
    .tab-btn {
      padding: 10px 20px;
      background: none;
      border: none;
      border-bottom: 3px solid transparent;
      font-size: 13px;
      font-weight: 600;
      color: var(--muted);
      cursor: pointer;
      transition: color .15s, border-color .15s;
      margin-bottom: -1px;
    }
    .tab-btn:hover  { color: var(--text); }
    .tab-btn.active { color: var(--blue); border-bottom-color: var(--blue); }

    /* ---- Tab panels ---- */
    .tab-panel          { display: none; }
    .tab-panel.active   { display: block; }

    /* ---- Pairs-specific ---- */
    .pair-coin { font-weight: 700; font-family: monospace; font-size: 12px; }
    .badge-long-spread  { background: #f0fdf4; color: #16a34a; font-weight: 700; }
    .badge-short-spread { background: #fef2f2; color: #dc2626; font-weight: 700; }
    .badge-watch        { background: #fffbeb; color: #b45309; }
    .badge-invalid      { background: #f1f5f9; color: #9ca3af; }
  </style>
</head>
<body>

<header>
  <h1><span>Hyperliquid</span> Momentum Scanner</h1>
  <span id="timestamp"></span>
  <span id="auto-scan-status" style="font-size:11px;color:var(--muted);margin-left:4px"></span>
  <span id="scan-mode" style="font-size:11px;padding:3px 8px;border-radius:4px;display:none"></span>
  <button id="scan-btn" onclick="runScan()">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
      <path d="M21 12a9 9 0 1 1-6.219-8.56"/>
    </svg>
    Run Scan
  </button>
  <!-- pairs-scan-btn removed: Mean Reversion tab disabled, Pair Momentum replaces it -->
  <button id="pair-momentum-scan-btn" onclick="runPairMomScan()" style="display:none;padding:7px 18px;background:var(--blue);color:#fff;border:none;border-radius:var(--radius);font-size:13px;font-weight:600;cursor:pointer;align-items:center;gap:8px;transition:opacity .15s">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
      <path d="M21 12a9 9 0 1 1-6.219-8.56"/>
    </svg>
    Scan Pair Momentum
  </button>
</header>

<nav class="tab-nav">
  <button class="tab-btn active" data-tab="momentum"      onclick="switchTab('momentum')">Momentum</button>
  <button class="tab-btn"        data-tab="pair-momentum" onclick="switchTab('pair-momentum')">Pair Momentum</button>
</nav>

<main>

<!-- ===================== MOMENTUM TAB ===================== -->
<div class="tab-panel active" id="tab-momentum">
  <div id="empty" style="display:none">
    <h2>No scan data yet</h2>
    <p>Click <strong>Run Scan</strong> above to fetch live data and compute momentum scores.</p>
    <p style="font-size:12px;color:var(--muted)">First run fetches ~220 days of candles for every asset (~30-60 s).</p>
  </div>

  <div id="content" style="display:none">
    <!-- Summary cards -->
    <div id="cards">
      <div class="card blue">
        <div class="label">Universe</div>
        <div class="value" id="c-universe">-</div>
      </div>
      <div class="card">
        <div class="label">With Data</div>
        <div class="value" id="c-with-data">-</div>
      </div>
      <div class="card">
        <div class="label">Computed</div>
        <div class="value" id="c-computed">-</div>
      </div>
      <div class="card green">
        <div class="label">LONG</div>
        <div class="value" id="c-long">-</div>
      </div>
      <div class="card red">
        <div class="label">SHORT</div>
        <div class="value" id="c-short">-</div>
      </div>
      <div class="card">
        <div class="label">Missing</div>
        <div class="value" id="c-missing">-</div>
      </div>
    </div>

    <!-- Unified momentum rankings -->
    <div class="panel">
      <div class="panel-header">MOMENTUM RANKINGS</div>
      <table class="tbl" id="mom-table">
        <thead><tr>
          <th>#</th><th>Coin</th><th>Direction</th>
          <th class="num">Strength</th><th class="num">Trend %</th><th class="num">R&#178;</th>
        </tr></thead>
        <tbody id="mom-body"></tbody>
      </table>
    </div>

    <!-- Missing symbols -->
    <div class="panel" id="missing-panel">
      <div class="panel-header miss">&#9888; MISSING / EXCLUDED SYMBOLS</div>
      <!-- no_price_data: collapsible summary row -->
      <div style="padding:8px 16px 6px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px">
        <span style="font-size:12px;color:var(--muted)">no_price_data: <strong id="no-price-count">-</strong></span>
        <button id="no-price-toggle" onclick="toggleNoPrice()"
          style="font-size:11px;padding:1px 8px;border:1px solid var(--border);border-radius:3px;background:var(--panel);color:var(--muted);cursor:pointer">
          show &#9660;
        </button>
        <span style="font-size:11px;color:var(--muted)">(inactive/zombie perps — no candle data on HL)</span>
      </div>
      <div id="no-price-section" style="display:none">
        <table class="tbl">
          <thead><tr><th>Coin</th><th>Category</th><th>Reason</th></tr></thead>
          <tbody id="no-price-body"></tbody>
        </table>
      </div>
      <!-- insufficient_history: always visible -->
      <table class="tbl">
        <thead><tr>
          <th>Coin</th><th>Category</th><th>Reason</th>
        </tr></thead>
        <tbody id="missing-body"></tbody>
      </table>
    </div>

    <!-- xyz: score rankings -->
    <div class="panel" id="xyz-rankings-panel">
      <div class="panel-header xyz">&#127850; XYZ SCORE RANKINGS</div>
      <div style="padding:8px 16px 6px;display:flex;gap:24px;flex-wrap:wrap;border-bottom:1px solid var(--border)">
        <span style="font-size:12px;color:var(--muted)">Universe: <strong id="xr-total">-</strong></span>
        <span style="font-size:12px;color:var(--muted)">With data: <strong id="xr-data">-</strong></span>
        <span style="font-size:12px;color:#16a34a">Computed: <strong id="xr-computed">-</strong></span>
      </div>
      <table class="tbl">
        <thead><tr>
          <th>#</th><th>Coin</th><th>Direction</th>
          <th class="num">Strength</th><th class="num">Trend %</th><th class="num">R&#178;</th>
        </tr></thead>
        <tbody id="xyz-rankings-body"></tbody>
      </table>
    </div>

    <!-- xyz: debug section -->
    <div class="panel" id="xyz-panel">
      <div class="panel-header xyz">&#127850; xyz: TOKENISED REAL-WORLD ASSETS</div>
      <div style="padding:10px 16px 6px;display:flex;gap:24px;flex-wrap:wrap;border-bottom:1px solid var(--border)">
        <span style="font-size:12px;color:var(--muted)">Total: <strong id="xyz-total">-</strong></span>
        <span style="font-size:12px;color:var(--muted)">With data: <strong id="xyz-data">-</strong></span>
        <span style="font-size:12px;color:var(--muted)">Sufficient history: <strong id="xyz-suff">-</strong></span>
        <span style="font-size:12px;color:#16a34a">In rankings: <strong id="xyz-ranked">-</strong></span>
        <span style="font-size:12px;color:#dc2626">Missing/excluded: <strong id="xyz-miss">-</strong></span>
      </div>
      <table class="tbl">
        <thead><tr>
          <th>HL Symbol</th><th>Type</th><th>HL OK</th>
          <th>Yahoo</th><th>Closes</th><th>Stage</th><th>Reason</th>
        </tr></thead>
        <tbody id="xyz-body"></tbody>
      </table>
    </div>
  </div>
</div><!-- /tab-momentum -->

<!-- ===================== PAIRS TAB ===================== -->
<div class="tab-panel" id="tab-pairs">

  <div id="pairs-empty" style="display:none;text-align:center;padding:80px 20px;color:var(--muted)">
    <h2 style="font-size:22px;margin-bottom:8px;color:var(--text)">No pairs data yet</h2>
    <p style="margin-bottom:20px">Click <strong>Scan Pairs</strong> above to compute pair metrics.</p>
    <p style="font-size:12px">Fetches fresh 1h candles from Hyperliquid (&sim;5&ndash;15 s for 22 symbols).</p>
  </div>

  <div id="pairs-content" style="display:none">
    <div style="max-width:1280px;margin:0 auto;padding:20px 24px 40px">

      <!-- Summary cards -->
      <div id="pairs-cards" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-bottom:20px">
        <div class="card blue"><div class="label">Configured</div><div class="value" id="pc-configured">-</div></div>
        <div class="card"><div class="label">Computed</div><div class="value" id="pc-computed">-</div></div>
        <div class="card green"><div class="label">Long Spread</div><div class="value" id="pc-long">-</div></div>
        <div class="card red"><div class="label">Short Spread</div><div class="value" id="pc-short">-</div></div>
        <div class="card"><div class="label">Watch</div><div class="value" id="pc-watch">-</div></div>
        <div class="card"><div class="label">Skipped</div><div class="value" id="pc-skipped">-</div></div>
      </div>
      <!-- Discovery stats row -->
      <div id="discovery-stats" style="display:flex;gap:16px;flex-wrap:wrap;padding:8px 12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:12px;font-size:12px">
        <span style="color:var(--muted);font-weight:600">Discovery:</span>
        <span>Candidates screened: <strong id="pd-total">-</strong></span>
        <span style="color:#7c3aed">Auto-selected: <strong id="pd-auto">-</strong></span>
        <span style="color:var(--blue)">Pinned: <strong id="pd-pinned">-</strong></span>
        <span style="color:var(--muted)">Excluded: <strong id="pd-excl">-</strong></span>
        <span style="margin-left:auto;font-size:11px;color:var(--muted)">Min corr<sub>168h</sub>=0.40 &bull; top 15 auto pairs</span>
      </div>

      <!-- Filter bar + timeframe label -->
      <div style="margin-bottom:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span style="font-size:12px;color:var(--muted);font-weight:600">Filter:</span>
        <button class="tab-btn active" id="pf-all"  style="padding:5px 14px;border:1px solid var(--border);border-radius:var(--radius);font-size:12px" onclick="filterPairs('all')">All</button>
        <button class="tab-btn"       id="pf-mr"   style="padding:5px 14px;border:1px solid var(--border);border-radius:var(--radius);font-size:12px" onclick="filterPairs('mr')">Mean Reversion</button>
        <button class="tab-btn"       id="pf-momentum"  style="padding:5px 14px;border:1px solid var(--border);border-radius:var(--radius);font-size:12px" onclick="filterPairs('momentum')">Relative Momentum</button>
        <span style="font-size:11px;padding:3px 8px;border-radius:4px;background:#ede9fe;color:#7c3aed;border:1px solid #c4b5fd;margin-left:4px">&#128337; 1h candles &bull; z=120h &bull; corr=168h</span>
        <span id="pairs-mode" style="font-size:11px;padding:3px 8px;border-radius:4px;display:none"></span>
        <span id="pairs-timestamp" style="font-size:12px;color:var(--muted);margin-left:auto"></span>
      </div>

      <!-- Active signals table -->
      <div class="panel" style="margin-bottom:16px">
        <div class="panel-header" style="background:#eff6ff;color:var(--blue)">&#9670; ACTIVE PAIR SIGNALS</div>
        <table class="tbl">
          <thead><tr>
            <th>#</th><th>Trade</th>
            <th class="num">Z-Score</th><th class="num">Z-Thresh</th>
            <th class="num">HL (h)</th><th class="num">Corr 72h</th><th class="num">Corr 168h</th>
            <th class="num">Beta</th><th class="num">Score</th><th>Momentum</th>
          </tr></thead>
          <tbody id="pairs-signals-body"></tbody>
        </table>
      </div>

      <!-- All pairs metrics table -->
      <div class="panel" style="margin-bottom:16px">
        <div class="panel-header" style="background:#f8f9fb;color:var(--text)">ALL PAIRS &mdash; METRICS (1h candles)</div>
        <table class="tbl">
          <thead><tr>
            <th>#</th><th>Pair (Long&#8593;-Short&#8595;)</th><th>Src</th><th class="num">Z-Score</th>
            <th class="num">Spread</th><th class="num">HL (h)</th>
            <th class="num">Corr 168h</th><th class="num">Beta</th>
            <th class="num">Slope A%</th><th class="num">Slope B%</th>
            <th class="num">Bars</th><th>Signal</th>
          </tr></thead>
          <tbody id="pairs-metrics-body"></tbody>
        </table>
      </div>

      <!-- Skipped pairs -->
      <div class="panel" id="pairs-skipped-panel" style="margin-bottom:16px">
        <div class="panel-header miss">&#9888; SKIPPED / INVALID PAIRS</div>
        <table class="tbl">
          <thead><tr><th>Pair</th><th>Reason</th></tr></thead>
          <tbody id="pairs-skipped-body"></tbody>
        </table>
      </div>

      <!-- Discovery universe panel -->
      <div class="panel" id="pairs-discovery-panel">
        <div class="panel-header" style="background:#fdf4ff;color:#7c3aed">
          &#128270; PAIR DISCOVERY UNIVERSE &mdash; ALL CANDIDATES
        </div>
        <div style="padding:8px 14px;font-size:11px;color:var(--muted);border-bottom:1px solid var(--border)">
          All C(n,2) combinations screened. Sorted by discovery score.
          Status: <span style="color:#7c3aed">&#9670; discovered</span> &nbsp;
                  <span style="color:var(--blue)">&#9670; pinned</span> &nbsp;
                  <span style="color:var(--muted)">&#9671; candidate</span> &nbsp;
                  <span style="color:#9ca3af">&#215; excluded</span>
        </div>
        <table class="tbl">
          <thead><tr>
            <th>Pair</th><th>Status</th>
            <th class="num">Score</th><th class="num">Corr 168h</th>
            <th class="num">HL (h)</th><th class="num">Bars</th>
            <th>Excluded reason</th>
          </tr></thead>
          <tbody id="discovery-body"></tbody>
        </table>
      </div>

    </div>
  </div>
</div><!-- /tab-pairs -->

<!-- ===================== PAIR MOMENTUM TAB ===================== -->
<div class="tab-panel" id="tab-pair-momentum">

  <div id="pm-empty" style="display:none;text-align:center;padding:80px 20px;color:var(--muted)">
    <h2 style="font-size:22px;margin-bottom:8px;color:var(--text)">No Pair Momentum data yet</h2>
    <p style="margin-bottom:20px">Click <strong>Scan Pair Momentum</strong> above to compute scores.</p>
    <p style="font-size:12px">Scores all C(n,2) pairs from the discovery universe using the same log-regression model as the single-asset scanner.</p>
  </div>

  <div id="pm-content" style="display:none">
    <div style="max-width:1280px;margin:0 auto;padding:20px 24px 40px">

      <!-- Summary cards -->
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-bottom:20px">
        <div class="card blue"><div class="label">Pairs Scored</div><div class="value" id="pm-count">-</div></div>
        <div class="card"><div class="label">Diversified</div><div class="value" id="pm-div-count">-</div></div>
        <div class="card"><div class="label">Window</div><div class="value" id="pm-window">-</div></div>
        <div class="card"><div class="label">Timeframe</div><div class="value" id="pm-tf">-</div></div>
      </div>

      <div style="margin-bottom:8px;font-size:11px;color:var(--muted)">
        Strongest relative momentum pairs &bull; daily candles &bull; 100-bar regression window &bull; top <span id="pm-top-n">-</span>
        &nbsp;<span style="padding:2px 6px;background:#ede9fe;color:#7c3aed;border-radius:3px;border:1px solid #c4b5fd">1d candles</span>
        <span style="margin-left:6px;color:#9ca3af">Strength&nbsp;=&nbsp;annualized log-ratio slope &times; R&sup2;</span>
      </div>

      <!-- View toggle -->
      <div style="margin-bottom:10px;display:flex;gap:8px;align-items:center">
        <span style="font-size:11px;color:var(--muted)">View:</span>
        <button id="pm-btn-div" onclick="setPmView('diversified')"
          style="padding:3px 12px;border-radius:4px;border:1px solid #3b82f6;background:#3b82f6;color:#fff;font-size:12px;cursor:pointer">
          Diversified
        </button>
        <button id="pm-btn-raw" onclick="setPmView('raw')"
          style="padding:3px 12px;border-radius:4px;border:1px solid #d1d5db;background:#fff;color:#374151;font-size:12px;cursor:pointer">
          Raw (all <span id="pm-raw-btn-n">-</span>)
        </button>
        <span id="pm-view-hint" style="font-size:11px;color:var(--muted)"></span>
      </div>

      <!-- Results table -->
      <div class="panel">
        <div class="panel-header" style="background:#eff6ff;color:var(--blue)" id="pm-panel-header">&#9670; PAIR MOMENTUM RANKINGS</div>
        <table class="tbl">
          <thead><tr>
            <th>#</th><th>Trade</th>
            <th class="num">Strength</th>
            <th class="num">Trend %</th>
            <th class="num">R&sup2;</th>
            <th class="num">Bars</th>
            <th>TF</th>
          </tr></thead>
          <tbody id="pm-body"></tbody>
        </table>
      </div>

    </div>
  </div>

</div><!-- /tab-pair-momentum -->

</main>

<script>
const fmt  = (n, d=4)  => n == null ? '-' : (+n).toLocaleString('en', {minimumFractionDigits:d, maximumFractionDigits:d});
const fmt2 = (n)       => n == null ? '-' : (+n).toLocaleString('en', {minimumFractionDigits:2, maximumFractionDigits:2});
const fmtP = (n)       => n == null ? '-' : (+n).toFixed(4);

function tradeRow(row, i) {
  const score    = +(row.momentum_score ?? 0);
  const isLong   = score >= 0;
  const dir      = isLong ? 'Long' : 'Short';
  const dirClass = isLong ? 'long' : 'short';
  const strength = Math.abs(score);
  const trend    = row.slope_ann_pct != null ? Math.abs(+row.slope_ann_pct) : null;
  return `<tr>
    <td>${i}</td>
    <td class="coin">${row.coin}</td>
    <td class="${dirClass}" style="font-weight:600">${dir}</td>
    <td class="num long">${fmt2(strength)}</td>
    <td class="num ${isLong ? 'long' : 'short'}">${trend != null ? fmt2(trend) + '%' : '-'}</td>
    <td class="num">${fmtP(row.r2)}</td>
  </tr>`;
}

function render(data) {
  const s = data.summary || {};
  document.getElementById('c-universe').textContent  = s.universe_size  ?? '-';
  document.getElementById('c-with-data').textContent = s.assets_with_data ?? '-';
  document.getElementById('c-computed').textContent  = s.assets_computed  ?? '-';
  document.getElementById('c-long').textContent      = s.long_candidates  ?? '-';
  document.getElementById('c-short').textContent     = s.short_candidates ?? '-';
  document.getElementById('c-missing').textContent   =
    ((s.assets_excluded_no_data ?? 0) + (s.assets_excluded_short_history ?? 0));

  const ts = s.scan_timestamp
    ? new Date(s.scan_timestamp).toLocaleString()
    : '';
  document.getElementById('timestamp').textContent = ts ? 'Last scan: ' + ts : '';

  const modeEl = document.getElementById('scan-mode');
  if (s.scan_timestamp) {
    const cached = s.used_cache;
    modeEl.style.display     = '';
    modeEl.textContent       = cached ? 'cached prices' : 'live prices';
    modeEl.style.background  = cached ? '#fffbeb' : '#f0fdf4';
    modeEl.style.color       = cached ? '#b45309' : '#16a34a';
    modeEl.style.border      = cached ? '1px solid #fde68a' : '1px solid #bbf7d0';
  }

  // Unified momentum table — merge longs+shorts, sort by |score| descending
  const momBody = document.getElementById('mom-body');
  const allRows = [...(data.longs || []), ...(data.shorts || [])]
    .sort((a, b) => Math.abs(+b.momentum_score) - Math.abs(+a.momentum_score));
  momBody.innerHTML = allRows.length
    ? allRows.map((r, i) => tradeRow(r, i+1)).join('')
    : '<tr class="empty-row"><td colspan="6">No momentum data — run a scan</td></tr>';

  // Missing table — split no_price_data (collapsible) vs insufficient_history (always shown)
  const missing = (data.missing || []);
  const noPrice  = missing.filter(r => r.category === 'no_price_data');
  const insuffHist = missing.filter(r => r.category !== 'no_price_data');
  const mkRow = r => `<tr>
      <td class="coin">${r.coin}</td>
      <td class="src">${r.category || ''}</td>
      <td style="color:var(--muted);font-size:12px">${r.reason || ''}</td>
    </tr>`;
  document.getElementById('no-price-count').textContent = noPrice.length;
  document.getElementById('no-price-body').innerHTML = noPrice.length
    ? noPrice.map(mkRow).join('') : '';
  const missingBody = document.getElementById('missing-body');
  missingBody.innerHTML = insuffHist.length
    ? insuffHist.map(mkRow).join('')
    : '<tr class="empty-row"><td colspan="3">No insufficient-history symbols</td></tr>';

  // xyz: score rankings table
  const xyzRankRows = [...(data.xyz_rankings || [])]
    .sort((a, b) => Math.abs(+b.momentum_score) - Math.abs(+a.momentum_score));
  document.getElementById('xr-total').textContent    = s.xyz_total    ?? '-';
  document.getElementById('xr-data').textContent     = s.xyz_with_price_data ?? '-';
  document.getElementById('xr-computed').textContent = s.xyz_in_rankings ?? '-';
  const xyzRankBody = document.getElementById('xyz-rankings-body');
  xyzRankBody.innerHTML = xyzRankRows.length
    ? xyzRankRows.map((r, i) => {
        const score  = +(r.momentum_score ?? 0);
        const isLong = score >= 0;
        const dir    = isLong ? 'Long' : 'Short';
        const dirCls = isLong ? 'long' : 'short';
        const strength = Math.abs(score);
        const trend    = r.slope_ann_pct != null ? Math.abs(+r.slope_ann_pct) : null;
        return `<tr>
          <td>${i + 1}</td>
          <td class="coin" style="font-size:12px">${r.coin}</td>
          <td class="${dirCls}" style="font-weight:600">${dir}</td>
          <td class="num long">${fmt2(strength)}</td>
          <td class="num ${isLong ? 'long' : 'short'}">${trend != null ? fmt2(trend) + '%' : '-'}</td>
          <td class="num">${fmtP(r.r2)}</td>
        </tr>`;
      }).join('')
    : '<tr class="empty-row"><td colspan="6">No xyz: coins in rankings &mdash; run a scan</td></tr>';

  // xyz: debug section
  const xyzRows = (data.xyz_debug || []);
  document.getElementById('xyz-total').textContent  = s.xyz_total ?? xyzRows.length;
  document.getElementById('xyz-data').textContent   = s.xyz_with_price_data ?? '-';
  document.getElementById('xyz-suff').textContent   = s.xyz_with_sufficient_history ?? '-';
  document.getElementById('xyz-ranked').textContent = s.xyz_in_rankings ?? '-';
  document.getElementById('xyz-miss').textContent   = s.xyz_missing_count ?? '-';

  const stageBadge = (stage) => {
    if (stage === 'in_rankings')   return `<span class="badge badge-ok">in rankings</span>`;
    if (stage === 'insufficient_history_after_fetch')
      return `<span class="badge badge-warn">insuff. history</span>`;
    if (stage === 'unsupported_index_proxy')
      return `<span class="badge badge-warn">index proxy</span>`;
    if (stage === 'yahoo_no_data') return `<span class="badge badge-err">no yahoo data</span>`;
    if (stage === 'yahoo_mapping_missing')
      return `<span class="badge badge-err">no mapping</span>`;
    return `<span class="badge badge-neutral">${stage || '-'}</span>`;
  };
  const bool2icon = (v) => v ? '<span style="color:#16a34a">&#10003;</span>'
                              : '<span style="color:#dc2626">&#10007;</span>';

  const xyzBody = document.getElementById('xyz-body');
  xyzBody.innerHTML = xyzRows.length
    ? xyzRows.map(r => `<tr>
        <td class="coin" style="font-size:12px">${r.hyperliquid_symbol}</td>
        <td class="src">${r.asset_type_guess || ''}</td>
        <td style="text-align:center">${bool2icon(r.hyperliquid_fetch_ok)}</td>
        <td class="src">${r.yahoo_symbol_used || ''}</td>
        <td class="num">${r.number_of_closes ?? 0}</td>
        <td>${stageBadge(r.failure_stage)}</td>
        <td style="color:var(--muted);font-size:11px;max-width:280px;white-space:normal">${r.failure_reason || ''}</td>
      </tr>`).join('')
    : '<tr class="empty-row"><td colspan="7">No xyz: data (run a scan first)</td></tr>';

  document.getElementById('empty').style.display   = 'none';
  document.getElementById('content').style.display = '';
}

async function loadData() {
  try {
    const resp = await fetch('/api/data');
    if (!resp.ok) throw new Error(resp.statusText);
    const data = await resp.json();
    if (data.no_data) {
      document.getElementById('empty').style.display   = '';
      document.getElementById('content').style.display = 'none';
    } else {
      render(data);
    }
  } catch (e) {
    console.error('loadData failed', e);
  }
}

async function runScan() {
  const btn = document.getElementById('scan-btn');
  btn.disabled = true;
  btn.classList.add('loading');
  btn.childNodes[1].textContent = ' Scanning...';
  try {
    const resp = await fetch('/api/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ no_cache: true, strict_source: true }),
    });
    const data = await resp.json();
    if (data.error) { alert('Scan error: ' + data.error); return; }
    await loadData();
  } catch (e) {
    alert('Scan failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.classList.remove('loading');
    btn.childNodes[1].textContent = ' Run Scan';
  }
}

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------
let pairsLoaded = false;
let pairsData   = null;
let pairsFilter = 'all';
let pairMomLoaded = false;

function switchTab(name) {
  document.querySelectorAll('.tab-btn[data-tab]').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p =>
    p.classList.toggle('active', p.id === 'tab-' + name));

  // Show the correct scan button
  document.getElementById('scan-btn').style.display               = name === 'momentum'      ? '' : 'none';
  document.getElementById('pair-momentum-scan-btn').style.display = name === 'pair-momentum' ? '' : 'none';

  if (name === 'pair-momentum' && !pairMomLoaded) loadPairMomData();
}

// ---------------------------------------------------------------------------
// Pairs helpers
// ---------------------------------------------------------------------------
function zscoreClass(z) {
  if (z == null) return '';
  return z <= -2 ? 'long' : z >= 2 ? 'short' : '';
}

function signalBadge(sig) {
  if (sig === 'LONG_SPREAD')  return '<span class="badge badge-long-spread">LONG SPREAD</span>';
  if (sig === 'SHORT_SPREAD') return '<span class="badge badge-short-spread">SHORT SPREAD</span>';
  if (sig === 'WATCH')        return '<span class="badge badge-warn">WATCH</span>';
  if (sig === 'NEUTRAL')      return '<span class="badge badge-neutral">NEUTRAL</span>';
  if (sig === 'INVALID')      return '<span class="badge badge-invalid">INVALID</span>';
  return `<span class="badge badge-neutral">${sig || '-'}</span>`;
}

function momBadge(sig) {
  if (!sig || sig === 'NEUTRAL') return '<span style="color:var(--muted);font-size:11px">-</span>';
  if (sig.startsWith('CONFIRMS')) return `<span style="color:var(--green);font-size:11px">${sig.replace('CONFIRMS_', '')}</span>`;
  if (sig === 'CONFLICTS') return `<span style="color:var(--red);font-size:11px">CONFLICTS</span>`;
  return `<span style="font-size:11px">${sig}</span>`;
}

function renderPairsSignals(rows) {
  const body = document.getElementById('pairs-signals-body');
  if (!rows || rows.length === 0) {
    body.innerHTML = '<tr class="empty-row"><td colspan="10">No active signals</td></tr>';
    return;
  }
  body.innerHTML = rows.map((r, i) => {
    const tradeLabel  = r.trade_label  ?? r.display_pair ?? r.pair_id ?? '-';
    const displayPair = r.display_pair ?? r.pair_id ?? '';
    const pairHint    = displayPair ? `<span style="font-size:0.72em;color:var(--muted);display:block;font-weight:400">${displayPair}</span>` : '';
    return `<tr>
    <td>${i + 1}</td>
    <td class="pair-coin" style="font-size:0.95em">${tradeLabel}${pairHint}</td>
    <td class="num ${zscoreClass(r.zscore)}">${r.zscore != null ? (+r.zscore).toFixed(3) : '-'}</td>
    <td class="num" style="color:var(--muted)">${r.zscore_threshold != null ? '\xb1' + (+r.zscore_threshold).toFixed(1) : '-'}</td>
    <td class="num">${r.half_life_hours != null ? (+r.half_life_hours).toFixed(1) + 'h' : '-'}</td>
    <td class="num">${r.corr_72  != null ? (+r.corr_72).toFixed(3)  : '-'}</td>
    <td class="num">${r.corr_168 != null ? (+r.corr_168).toFixed(3) : '-'}</td>
    <td class="num">${r.beta != null ? (+r.beta).toFixed(3) : '-'}</td>
    <td class="num">${r.final_pair_score != null ? (+r.final_pair_score).toFixed(3) : '-'}</td>
    <td>${momBadge(r.mom_signal)}</td>
  </tr>`;
  }).join('');
}

function renderPairsMetrics(rows) {
  const body = document.getElementById('pairs-metrics-body');
  if (!rows || rows.length === 0) {
    body.innerHTML = '<tr class="empty-row"><td colspan="12">No pair metrics computed</td></tr>';
    return;
  }
  body.innerHTML = rows.map((r, i) => {
    const displayPair = r.display_pair ?? r.pair_id ?? '-';
    const longLeg  = r.long_leg  ?? r.leg_a ?? '';
    const shortLeg = r.short_leg ?? r.leg_b ?? '';
    const legHint  = longLeg && shortLeg ? `<span style="font-size:0.72em;color:var(--muted);display:block">&#8593;${longLeg} &nbsp;&#8595;${shortLeg}</span>` : '';
    return `<tr>
    <td>${i + 1}</td>
    <td class="pair-coin">${displayPair}${legHint}</td>
    <td>${sourceBadge(r.source)}</td>
    <td class="num ${zscoreClass(r.zscore)}">${r.zscore != null ? (+r.zscore).toFixed(3) : '-'}</td>
    <td class="num">${r.spread_current != null ? (+r.spread_current).toFixed(4) : '-'}</td>
    <td class="num">${r.half_life_hours != null ? (+r.half_life_hours).toFixed(1) + 'h' : '-'}</td>
    <td class="num">${r.corr_168 != null ? (+r.corr_168).toFixed(3) : '-'}</td>
    <td class="num">${r.beta != null ? (+r.beta).toFixed(3) : '-'}</td>
    <td class="num ${r.slope_a_ann_pct > 0 ? 'long' : 'short'}">${r.slope_a_ann_pct != null ? (+r.slope_a_ann_pct).toFixed(1) + '%' : '-'}</td>
    <td class="num ${r.slope_b_ann_pct > 0 ? 'long' : 'short'}">${r.slope_b_ann_pct != null ? (+r.slope_b_ann_pct).toFixed(1) + '%' : '-'}</td>
    <td class="num">${r.n_aligned ?? '-'}</td>
    <td>${signalBadge(r.mr_signal)}</td>
  </tr>`;
  }).join('');
}

function applyPairsFilter(filter) {
  if (!pairsData) return;
  pairsFilter = filter;

  // Update filter button states
  ['all', 'mr', 'momentum'].forEach(f => {
    document.getElementById('pf-' + f).classList.toggle('active', f === filter);
  });

  let metrics = pairsData.metrics || [];
  let signals = pairsData.signals || [];

  if (filter === 'mr') {
    const mrActive = new Set(['LONG_SPREAD', 'SHORT_SPREAD', 'WATCH']);
    metrics = metrics.filter(r => mrActive.has(r.mr_signal));
    signals = signals.filter(r => mrActive.has(r.mr_signal));
  } else if (filter === 'momentum') {
    const momActive = new Set(['CONFIRMS_LONG_SPREAD', 'CONFIRMS_SHORT_SPREAD', 'CONFLICTS']);
    metrics = metrics.filter(r => momActive.has(r.mom_signal));
    signals = signals.filter(r => momActive.has(r.mom_signal));
  }

  renderPairsSignals(signals);
  renderPairsMetrics(metrics);
}

function filterPairs(f) { applyPairsFilter(f); }

function sourceBadge(src) {
  if (src === 'pinned')     return '<span class="badge" style="background:#eff6ff;color:var(--blue)">&#128204; pinned</span>';
  if (src === 'discovered') return '<span class="badge" style="background:#fdf4ff;color:#7c3aed">&#10022; auto</span>';
  return '<span class="badge badge-neutral">' + (src || 'auto') + '</span>';
}

function discStatusBadge(status) {
  if (status === 'discovered')                return '<span style="color:#7c3aed;font-weight:600">&#9670; discovered</span>';
  if (status === 'pinned')                    return '<span style="color:var(--blue);font-weight:600">&#9670; pinned</span>';
  if (status === 'candidate')                 return '<span style="color:var(--muted)">&#9671; candidate</span>';
  if (status === 'excluded_low_correlation')  return '<span style="color:#9ca3af">&#215; low corr</span>';
  if (status === 'excluded_insufficient_data')return '<span style="color:#9ca3af">&#215; no data</span>';
  if (status === 'excluded_denylist')         return '<span style="color:#9ca3af">&#215; denylist</span>';
  return `<span style="color:#9ca3af">&#215; ${status ?? ''}</span>`;
}

function renderDiscovery(rows) {
  // Update discovery stats bar
  const byStatus = {};
  (rows || []).forEach(r => { byStatus[r.status] = (byStatus[r.status] || 0) + 1; });
  document.getElementById('pd-total').textContent  = (rows || []).length || '-';
  document.getElementById('pd-auto').textContent   = byStatus['discovered'] ?? '-';
  document.getElementById('pd-pinned').textContent = byStatus['pinned']     ?? '-';
  const excl = Object.entries(byStatus)
    .filter(([k]) => k.startsWith('excluded'))
    .reduce((a,[,v]) => a + v, 0);
  document.getElementById('pd-excl').textContent   = excl || '-';

  const body = document.getElementById('discovery-body');
  if (!rows || rows.length === 0) {
    body.innerHTML = '<tr class="empty-row"><td colspan="7">No discovery data (run a pairs scan first)</td></tr>';
    return;
  }
  // Sort: discovered first, then pinned, then candidates, then excluded
  const ORDER = {discovered:0, pinned:1, candidate:2};
  const sorted = [...rows].sort((a, b) => {
    const oa = ORDER[a.status] ?? 3;
    const ob = ORDER[b.status] ?? 3;
    if (oa !== ob) return oa - ob;
    const sa = a.discovery_score ?? -1;
    const sb = b.discovery_score ?? -1;
    return sb - sa;
  });
  body.innerHTML = sorted.map(r => `<tr>
    <td class="pair-coin">${r.pair_id ?? '-'}</td>
    <td>${discStatusBadge(r.status)}</td>
    <td class="num">${r.discovery_score != null ? (+r.discovery_score).toFixed(3) : '-'}</td>
    <td class="num">${r.corr_168 != null ? (+r.corr_168).toFixed(3) : '-'}</td>
    <td class="num">${r.half_life_hours != null ? (+r.half_life_hours).toFixed(1) + 'h' : '-'}</td>
    <td class="num">${r.n_aligned ?? '-'}</td>
    <td style="color:var(--muted);font-size:11px">${r.exclusion_reason || ''}</td>
  </tr>`).join('');
}

function renderPairs(data) {
  const s = data.summary || {};

  document.getElementById('pc-configured').textContent = s.pairs_configured ?? '-';
  document.getElementById('pc-computed').textContent   = s.pairs_computed    ?? '-';
  document.getElementById('pc-long').textContent       = s.long_spread       ?? '-';
  document.getElementById('pc-short').textContent      = s.short_spread      ?? '-';
  document.getElementById('pc-watch').textContent      = s.watch             ?? '-';
  document.getElementById('pc-skipped').textContent    = s.pairs_skipped     ?? '-';

  const ts = s.scan_timestamp ? new Date(s.scan_timestamp).toLocaleString() : '';
  document.getElementById('pairs-timestamp').textContent = ts ? 'Last scan: ' + ts : '';

  const modeEl = document.getElementById('pairs-mode');
  if (s.scan_timestamp) {
    modeEl.style.display    = '';
    modeEl.textContent      = s.used_cache ? 'cached prices' : 'live prices';
    modeEl.style.background = s.used_cache ? '#fffbeb' : '#f0fdf4';
    modeEl.style.color      = s.used_cache ? '#b45309' : '#16a34a';
    modeEl.style.border     = s.used_cache ? '1px solid #fde68a' : '1px solid #bbf7d0';
  }

  // Skipped pairs
  const skipped = s.skipped_pairs || [];
  const sb = document.getElementById('pairs-skipped-body');
  sb.innerHTML = skipped.length
    ? skipped.map(r => `<tr><td class="pair-coin">${r.pair_id}</td><td style="color:var(--muted);font-size:12px">${r.reason}</td></tr>`).join('')
    : '<tr class="empty-row"><td colspan="2">All pairs computed successfully</td></tr>';

  document.getElementById('pairs-empty').style.display   = 'none';
  document.getElementById('pairs-content').style.display = '';

  renderDiscovery(data.discovery || []);
  applyPairsFilter(pairsFilter);
}

async function loadPairsData() {
  try {
    const resp = await fetch('/api/pairs');
    if (!resp.ok) throw new Error(resp.statusText);
    const data = await resp.json();
    if (data.no_data) {
      document.getElementById('pairs-empty').style.display   = '';
      document.getElementById('pairs-content').style.display = 'none';
    } else {
      pairsData   = data;
      pairsLoaded = true;
      renderPairs(data);
    }
  } catch (e) {
    console.error('loadPairsData failed', e);
  }
}

async function runPairsScan() {
  const btn = document.getElementById('pairs-scan-btn');
  btn.disabled = true;
  const origText = btn.childNodes[1]?.textContent || ' Scan Pairs';
  if (btn.childNodes[1]) btn.childNodes[1].textContent = ' Scanning...';
  try {
    const resp = await fetch('/api/pairs/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ no_cache: true, strict_source: false }),
    });
    const data = await resp.json();
    if (data.error) { alert('Pairs scan error: ' + data.error); return; }
    pairsLoaded = false;
    await loadPairsData();
  } catch (e) {
    alert('Pairs scan failed: ' + e.message);
  } finally {
    btn.disabled = false;
    if (btn.childNodes[1]) btn.childNodes[1].textContent = origText;
  }
}

// ---------------------------------------------------------------------------
// Pair Momentum tab
// ---------------------------------------------------------------------------
let _pmData    = null;   // full API response cached after first load
let _pmView    = 'diversified';  // 'diversified' | 'raw'

function toggleNoPrice() {
  const sec = document.getElementById('no-price-section');
  const btn = document.getElementById('no-price-toggle');
  const hidden = sec.style.display === 'none';
  sec.style.display = hidden ? 'block' : 'none';
  btn.innerHTML = hidden ? 'hide &#9650;' : 'show &#9660;';
}

function setPmView(view) {
  _pmView = view;
  // Toggle button styles
  const btnDiv = document.getElementById('pm-btn-div');
  const btnRaw = document.getElementById('pm-btn-raw');
  const active   = 'padding:3px 12px;border-radius:4px;border:1px solid #3b82f6;background:#3b82f6;color:#fff;font-size:12px;cursor:pointer';
  const inactive = 'padding:3px 12px;border-radius:4px;border:1px solid #d1d5db;background:#fff;color:#374151;font-size:12px;cursor:pointer';
  btnDiv.style.cssText = view === 'diversified' ? active : inactive;
  btnRaw.style.cssText = view === 'raw'         ? active : inactive;
  if (_pmData) renderPairMom(_pmData);
}

function renderPairMom(data) {
  _pmData = data;
  const summary = data.summary || {};
  document.getElementById('pm-count').textContent     = summary.pairs_computed  ?? '-';
  document.getElementById('pm-div-count').textContent = summary.diversified_pairs != null
    ? summary.diversified_pairs + ' pairs'
    : '-';
  document.getElementById('pm-window').textContent = summary.slope_window_bars
    ? summary.slope_window_bars + ' bars' : '-';
  document.getElementById('pm-tf').textContent = summary.timeframe ?? '-';

  const isDiversified = _pmView === 'diversified';
  let rows = (isDiversified ? (data.diversified_rows || []) : (data.rows || []));

  const topN = summary.top_n ?? '?';
  document.getElementById('pm-top-n').textContent    = topN;
  document.getElementById('pm-raw-btn-n').textContent = topN;

  const hint = isDiversified
    ? 'each symbol appears at most once'
    : `all top ${topN} by raw |score|`;
  document.getElementById('pm-view-hint').innerHTML = hint;

  const header = document.getElementById('pm-panel-header');
  header.textContent = isDiversified
    ? '\u25C6 PAIR MOMENTUM RANKINGS \u2014 Diversified (' + rows.length + ')'
    : `\u25C6 PAIR MOMENTUM RANKINGS \u2014 Raw (Top ${topN})`;

  const body = document.getElementById('pm-body');
  if (!rows.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="7">No results</td></tr>';
    return;
  }
  rows = [...rows].sort((a, b) => Math.abs(b.momentum_score) - Math.abs(a.momentum_score));
  body.innerHTML = rows.map((r, i) => {
    const tradeLabel  = r.trade_label  ?? r.display_pair ?? r.pair_id ?? '-';
    const displayPair = r.display_pair ?? r.pair_id ?? '';
    const pairHint    = displayPair ? `<span style="font-size:0.72em;color:var(--muted);display:block;font-weight:400">${displayPair}</span>` : '';
    const strength = r.momentum_score != null ? Math.abs(+r.momentum_score) : null;
    const trend    = r.slope_ann_pct  != null ? Math.abs(+r.slope_ann_pct)  : null;
    return `<tr>
    <td>${i + 1}</td>
    <td class="pair-coin" style="font-size:0.95em">${tradeLabel}${pairHint}</td>
    <td class="num long">${strength != null ? strength.toFixed(2) : '-'}</td>
    <td class="num long">${trend    != null ? trend.toFixed(1) + '%' : '-'}</td>
    <td class="num">${r.r2 != null ? (+r.r2).toFixed(3) : '-'}</td>
    <td class="num">${r.bars_used ?? '-'}</td>
    <td style="color:var(--muted);font-size:11px">${r.timeframe ?? '-'}</td>
  </tr>`;
  }).join('');
}

async function loadPairMomData() {
  try {
    const resp = await fetch('/api/pair-momentum');
    if (!resp.ok) throw new Error(resp.statusText);
    const data = await resp.json();
    if (data.no_data) {
      document.getElementById('pm-empty').style.display   = '';
      document.getElementById('pm-content').style.display = 'none';
    } else {
      pairMomLoaded = true;
      document.getElementById('pm-empty').style.display   = 'none';
      document.getElementById('pm-content').style.display = '';
      renderPairMom(data);
    }
  } catch (e) {
    console.error('loadPairMomData failed', e);
  }
}

async function runPairMomScan() {
  const btn = document.getElementById('pair-momentum-scan-btn');
  btn.disabled = true;
  const origText = btn.childNodes[1]?.textContent || ' Scan Pair Momentum';
  if (btn.childNodes[1]) btn.childNodes[1].textContent = ' Scanning...';
  try {
    const resp = await fetch('/api/pair-momentum/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ no_cache: true }),
    });
    const data = await resp.json();
    if (data.error) { alert('Pair momentum scan error: ' + data.error); return; }
    pairMomLoaded = false;
    await loadPairMomData();
  } catch (e) {
    alert('Pair momentum scan failed: ' + e.message);
  } finally {
    btn.disabled = false;
    if (btn.childNodes[1]) btn.childNodes[1].textContent = origText;
  }
}

// ---------------------------------------------------------------------------
// Auto-scan
// ---------------------------------------------------------------------------
const AUTO_SCAN_MS   = 6 * 60 * 60 * 1000;  // full scan every 6 hours
const AUTO_POLL_MS   = 60 * 1000;            // refresh display every 1 min

let _nextScanAt      = null;
let _autoScanRunning = false;

function _fmtCountdown(ms) {
  if (ms <= 0) return 'now';
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function _updateAutoStatus(label) {
  const el = document.getElementById('auto-scan-status');
  if (el) el.textContent = label;
}

async function _triggerAutoScan() {
  if (_autoScanRunning) return;
  _autoScanRunning = true;
  _updateAutoStatus('⟳ scanning…');
  const opts = { method: 'POST', headers: {'Content-Type': 'application/json'},
                 body: JSON.stringify({no_cache: false}) };
  try {
    await fetch('/api/scan', opts);               // waits until scan finishes
    await fetch('/api/pair-momentum/scan', opts); // then pair momentum
    await loadData();
    if (pairMomLoaded) await loadPairMomData();   // preserves _pmView state
  } catch(e) {
    console.error('Auto-scan failed', e);
  } finally {
    _autoScanRunning = false;
  }
}

function _scheduleNextScan() {
  _nextScanAt = Date.now() + AUTO_SCAN_MS;
  setTimeout(async () => {
    await _triggerAutoScan();
    _scheduleNextScan();
  }, AUTO_SCAN_MS);
}

function _startAutoScan() {
  _scheduleNextScan();
  // Lightweight display poll — re-reads output files, doesn't run scan
  setInterval(async () => {
    if (!_autoScanRunning) {
      await loadData();
      if (pairMomLoaded) await loadPairMomData();
    }
    const remaining = _nextScanAt - Date.now();
    _updateAutoStatus(`next scan in ${_fmtCountdown(remaining)}`);
  }, AUTO_POLL_MS);
}

// ---------------------------------------------------------------------------
// Initial load
// ---------------------------------------------------------------------------
loadData();
_startAutoScan();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict | list | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _read_csv_rows(path: Path) -> list[dict]:
    try:
        import math
        import pandas as pd
        df = pd.read_csv(path)
        records = df.to_dict(orient="records")
        # Replace float NaN/inf with None so Flask jsonify produces valid JSON.
        # pandas `where` doesn't reliably handle all column dtypes, so we fix
        # at the record level instead.
        for row in records:
            for k, v in row.items():
                if isinstance(v, float) and not math.isfinite(v):
                    row[k] = None
        return records
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/data")
def api_data():
    summary_path = OUTPUT_DIR / "scan_summary.json"
    if not summary_path.exists():
        return jsonify({"no_data": True})

    summary      = _read_json(summary_path) or {}
    longs        = _read_csv_rows(OUTPUT_DIR / "top_longs.csv")
    shorts       = _read_csv_rows(OUTPUT_DIR / "top_shorts.csv")
    missing      = _read_csv_rows(OUTPUT_DIR / "missing_symbols.csv")
    xyz_debug    = _read_csv_rows(OUTPUT_DIR / "xyz_debug.csv")
    xyz_rankings = _read_csv_rows(OUTPUT_DIR / "xyz_rankings.csv")

    return jsonify({
        "no_data":      False,
        "summary":      summary,
        "longs":        longs,
        "shorts":       shorts,
        "missing":      missing,
        "xyz_debug":    xyz_debug,
        "xyz_rankings": xyz_rankings,
    })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    if not _scan_lock.acquire(blocking=False):
        return jsonify({"error": "A scan is already running. Please wait."}), 409

    try:
        # Import here so the web server starts fast even if deps are slow
        from main import run_scan
        import logging
        logging.getLogger().setLevel(logging.INFO)
        body = request.json if request.is_json else {}
        no_cache     = body.get("no_cache", False)
        strict_source = body.get("strict_source", False)
        summary = run_scan(no_cache=no_cache, strict_source=strict_source)
        return jsonify({"ok": True, "summary": summary})
    except Exception as exc:
        logging.exception("Scan failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        _scan_lock.release()


# DEPRECATED: Mean Reversion Pairs tab removed from UI (2026-03-18).
# Routes kept for backward-compatibility / CLI use; not linked from dashboard.
@app.route("/api/pairs")
def api_pairs():
    summary_path = OUTPUT_DIR / "pair_scan_summary.json"
    if not summary_path.exists():
        return jsonify({"no_data": True})

    summary   = _read_json(summary_path) or {}
    signals   = _read_csv_rows(OUTPUT_DIR / "pair_signals.csv")
    metrics   = _read_csv_rows(OUTPUT_DIR / "pair_metrics.csv")
    discovery = _read_csv_rows(OUTPUT_DIR / "pair_universe_discovered.csv")

    return jsonify({
        "no_data":   False,
        "summary":   summary,
        "signals":   signals,
        "metrics":   metrics,
        "discovery": discovery,
    })


# DEPRECATED: see note above.
@app.route("/api/pairs/scan", methods=["POST"])
def api_pairs_scan():
    if not _pairs_scan_lock.acquire(blocking=False):
        return jsonify({"error": "A pairs scan is already running. Please wait."}), 409

    try:
        from main import run_pairs_scan
        import logging
        logging.getLogger().setLevel(logging.INFO)
        body = request.json if request.is_json else {}
        no_cache      = body.get("no_cache", False)
        strict_source = body.get("strict_source", False)
        summary = run_pairs_scan(no_cache=no_cache, strict_source=strict_source)
        return jsonify({"ok": True, "summary": summary})
    except Exception as exc:
        logging.exception("Pairs scan failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        _pairs_scan_lock.release()


@app.route("/api/pair-momentum")
def api_pair_momentum():
    summary_path = OUTPUT_DIR / "pair_momentum_summary.json"
    if not summary_path.exists():
        return jsonify({"no_data": True})
    summary          = _read_json(summary_path) or {}
    rows             = _read_csv_rows(OUTPUT_DIR / "pair_momentum.csv")
    diversified_rows = _read_csv_rows(OUTPUT_DIR / "pair_momentum_diversified.csv")
    return jsonify({
        "no_data":          False,
        "summary":          summary,
        "rows":             rows,
        "diversified_rows": diversified_rows,
    })


_pair_momentum_scan_lock = threading.Lock()


@app.route("/api/pair-momentum/scan", methods=["POST"])
def api_pair_momentum_scan():
    if not _pair_momentum_scan_lock.acquire(blocking=False):
        return jsonify({"error": "A pair momentum scan is already running. Please wait."}), 409
    try:
        from main import run_pair_momentum_scan
        import logging
        logging.getLogger().setLevel(logging.INFO)
        body     = request.json if request.is_json else {}
        no_cache = body.get("no_cache", False)
        summary  = run_pair_momentum_scan(no_cache=no_cache)
        return jsonify({"ok": True, "summary": summary})
    except Exception as exc:
        logging.exception("Pair momentum scan failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        _pair_momentum_scan_lock.release()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Hyperliquid Momentum Dashboard")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    print(f"Dashboard running at http://localhost:{args.port}")
    print("Press Ctrl+C to stop.")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
