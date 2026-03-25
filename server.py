#!/usr/bin/env python3
"""
Options Ollie — Local Web Server
Unified interface: scan for opportunities, click to log trades, monitor positions.

Usage:
    python server.py

Then open: http://localhost:5000
"""

import json, os, sys, time, threading
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string

sys.path.insert(0, os.path.dirname(__file__))

from options_ollie.config import OllieConfig, Position, FULL_WATCHLIST, WATCHLIST_ETFS
from options_ollie.data.fetcher import OptionsDataFetcher
from options_ollie.data.screener import OptionsScreener
from options_ollie.strategies.wheel import WheelManager
from options_ollie.strategies.trade_ledger import TradeLedger
from options_ollie.strategies.position_monitor import PositionMonitor

app = Flask(__name__)

# ── Custom JSON encoder: convert numpy/pandas scalar types to Python natives ──
class NumpySafeEncoder(json.JSONEncoder):
    def default(self, obj):
        try:
            import numpy as np
            if isinstance(obj, np.bool_):
                return bool(obj)
            if isinstance(obj, (np.integer,)):
                return int(obj)
            if isinstance(obj, (np.floating,)):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
        except ImportError:
            pass
        return super().default(obj)

app.json_encoder = NumpySafeEncoder
OUTPUT_DIR = os.path.dirname(__file__)
LEDGER_PATH = os.path.join(OUTPUT_DIR, 'data', 'trade_ledger.json')
os.makedirs(os.path.join(OUTPUT_DIR, 'data'), exist_ok=True)

def get_ledger():
    return TradeLedger(LEDGER_PATH)


# ─────────────────────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Options Ollie</title>
<style>
:root {
  --bg:#0f1117; --card:#1a1d27; --card2:#20243a; --border:#2a2d3a;
  --text:#e2e8f0; --muted:#8b95a5;
  --green:#22c55e; --red:#ef4444; --blue:#3b82f6;
  --orange:#f59e0b; --purple:#a855f7; --accent:#6366f1;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.5}
.topbar{background:var(--card);border-bottom:1px solid var(--border);padding:14px 28px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:100}
.topbar h1{font-size:20px;background:linear-gradient(135deg,var(--accent),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.topbar .sub{color:var(--muted);font-size:12px}
.wrap{max-width:1200px;margin:0 auto;padding:28px 20px}
.tabs{display:flex;gap:2px;margin-bottom:24px;border-bottom:1px solid var(--border)}
.tab{padding:10px 20px;background:transparent;color:var(--muted);border:none;cursor:pointer;font-size:14px;font-weight:500;border-bottom:2px solid transparent;transition:all .15s;margin-bottom:-1px}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-content{display:none}
.tab-content.active{display:block}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:22px;margin-bottom:18px}
.card h2{font-size:16px;margin-bottom:16px}
.rddt-box{background:linear-gradient(135deg,rgba(99,102,241,.08),rgba(168,85,247,.08));border:1px solid var(--accent);border-radius:12px;padding:22px;margin-bottom:20px}
.rddt-box h2{font-size:17px;margin-bottom:14px}
.rddt-action{background:rgba(34,197,94,.08);border:1px solid var(--green);border-radius:8px;padding:14px 16px;margin:12px 0;font-size:14px;line-height:1.6}
.rddt-note{background:rgba(245,158,11,.07);border-left:3px solid var(--orange);padding:10px 14px;margin:6px 0;border-radius:0 8px 8px 0;font-size:13px}
.stats-row{display:flex;gap:20px;flex-wrap:wrap;margin:12px 0}
.stat{text-align:center;min-width:80px}
.stat .lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.stat .val{font-size:22px;font-weight:700;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:9px 12px;color:var(--muted);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid var(--border);background:rgba(255,255,255,.02)}
td{padding:9px 12px;border-bottom:1px solid rgba(255,255,255,.04)}
tr:hover td{background:rgba(255,255,255,.02)}
.tbl-wrap{max-height:420px;overflow-y:auto;border-radius:8px}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:700}
.badge-green{background:rgba(34,197,94,.13);color:var(--green)}
.badge-blue{background:rgba(59,130,246,.13);color:var(--blue)}
.badge-orange{background:rgba(245,158,11,.13);color:var(--orange)}
.badge-red{background:rgba(239,68,68,.13);color:var(--red)}
.badge-purple{background:rgba(168,85,247,.13);color:var(--purple)}
.btn{padding:9px 18px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;border:none;transition:all .15s}
.btn-primary{background:var(--accent);color:#fff}.btn-primary:hover{background:#5457e8}
.btn-success{background:var(--green);color:#052e16}.btn-success:hover{background:#16a34a}
.btn-ghost{background:var(--card2);color:var(--text);border:1px solid var(--border)}.btn-ghost:hover{border-color:var(--accent)}
.btn-sm{padding:5px 12px;font-size:12px}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-group{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
.log-btn{background:rgba(99,102,241,.15);color:var(--accent);border:1px solid rgba(99,102,241,.3);padding:4px 10px;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;white-space:nowrap;transition:all .15s}
.log-btn:hover{background:rgba(99,102,241,.3)}
.pos{color:var(--green)}.neg{color:var(--red)}.neu{color:var(--blue)}.warn{color:var(--orange)}
.fg{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.fg3{grid-template-columns:1fr 1fr 1fr}
@media(max-width:640px){.fg,.fg3{grid-template-columns:1fr}}
.field{display:flex;flex-direction:column;gap:5px}
.field label{font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.4px}
.field input,.field select{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:9px 12px;border-radius:8px;font-size:14px;outline:none;transition:border-color .15s}
.field input:focus,.field select:focus{border-color:var(--accent)}
.field input::placeholder{color:var(--muted)}
.type-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px}
@media(max-width:540px){.type-grid{grid-template-columns:1fr 1fr}}
.type-btn{padding:12px 8px;border:2px solid var(--border);background:var(--bg);color:var(--muted);border-radius:10px;cursor:pointer;text-align:center;transition:all .15s;font-size:12px;font-weight:600;line-height:1.4}
.type-btn:hover{border-color:var(--accent);color:var(--text)}
.type-btn.selected{border-color:var(--accent);background:rgba(99,102,241,.12);color:var(--accent)}
.type-btn .ico{font-size:20px;display:block;margin-bottom:4px}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:200;display:none;align-items:center;justify-content:center;padding:20px}
.modal-overlay.open{display:flex}
.modal{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:28px;width:100%;max-width:560px;max-height:90vh;overflow-y:auto;position:relative}
.modal h3{font-size:17px;margin-bottom:4px}
.modal .sub-label{font-size:13px;color:var(--muted);margin-bottom:20px}
.modal-close{position:absolute;top:14px;right:18px;background:transparent;border:none;color:var(--muted);font-size:20px;cursor:pointer}
.modal-close:hover{color:var(--text)}
.preview-box{background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.3);border-radius:8px;padding:14px;margin:14px 0;font-size:13px;line-height:1.9}
.pos-card{background:var(--card2);border-radius:12px;padding:18px;border-left:4px solid var(--border);margin-bottom:12px}
.pos-card.urgent{border-left-color:var(--red);background:rgba(239,68,68,.05)}
.pos-card.action{border-left-color:var(--green);background:rgba(34,197,94,.05)}
.pos-card.watch{border-left-color:var(--orange);background:rgba(245,158,11,.05)}
.pos-header{display:flex;justify-content:space-between;align-items:flex-start}
.pos-title{font-size:15px;font-weight:700}
.pos-meta{font-size:12px;color:var(--muted);margin-top:3px}
.pos-stats{display:flex;gap:18px;flex-wrap:wrap;margin:12px 0}
.pos-stat .sl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.3px}
.pos-stat .sv{font-size:16px;font-weight:600;margin-top:2px}
.progress-bar{background:rgba(255,255,255,.05);border-radius:4px;height:6px;margin:8px 0 12px}
.progress-fill{height:100%;border-radius:4px;transition:width .3s}
.advice-hl{font-size:14px;font-weight:600;margin-bottom:5px}
.advice-dt{font-size:13px;color:var(--muted);line-height:1.6;margin-bottom:8px}
.action-list{list-style:none}
.action-list li{font-size:13px;padding:2px 0}
.action-list li::before{content:"• ";color:var(--accent)}
.summary-strip{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}
.sum-pill{background:var(--card2);border:1px solid var(--border);border-radius:8px;padding:10px 16px;text-align:center;flex:1;min-width:90px}
.sum-pill .val{font-size:20px;font-weight:700}
.sum-pill .lbl{font-size:11px;color:var(--muted);text-transform:uppercase}
.loader{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .8s linear infinite;vertical-align:middle;margin-right:5px}
@keyframes spin{to{transform:rotate(360deg)}}
.empty{text-align:center;padding:48px 20px;color:var(--muted)}
.empty .ico{font-size:44px;margin-bottom:10px}
.toast{position:fixed;bottom:24px;right:24px;padding:13px 18px;border-radius:10px;font-size:13px;font-weight:500;z-index:9999;transform:translateY(60px);opacity:0;transition:all .3s;max-width:360px;box-shadow:0 4px 20px rgba(0,0,0,.5)}
.toast.show{transform:translateY(0);opacity:1}
.toast.success{background:#14532d;border:1px solid var(--green);color:#86efac}
.toast.error{background:#450a0a;border:1px solid var(--red);color:#fca5a5}
.toast.info{background:#1e3a5f;border:1px solid var(--blue);color:#93c5fd}
.hidden{display:none!important}
.risk-section-hdr{font-size:14px;margin:20px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;display:flex;align-items:center;gap:8px}
.risk-section-hdr::after{content:'';flex:1;height:1px;background:var(--border)}
.scenario-below{background:rgba(239,68,68,.06)}
.rddt-roadmap{background:rgba(99,102,241,.06);border:1px solid rgba(99,102,241,.2);border-radius:8px;padding:12px 16px;margin-top:8px;font-size:13px}
.rddt-roadmap td,.rddt-roadmap th{padding:7px 10px}
.collar-credit{background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.2);border-radius:4px;padding:2px 8px;font-size:12px;font-weight:700;color:var(--green)}
.collar-debit{background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.2);border-radius:4px;padding:2px 8px;font-size:12px;font-weight:700;color:var(--red)}
.filter-bar{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.filter-bar input{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:7px 11px;border-radius:8px;font-size:13px;outline:none}
.filter-bar input:focus{border-color:var(--accent)}
</style>
</head>
<body>

<div class="topbar">
  <div>
    <h1>🎯 Options Ollie</h1>
    <div class="sub">Live Screener &amp; Trade Manager</div>
  </div>
  <div style="display:flex;align-items:center;gap:14px">
    <div id="clock" style="color:var(--muted);font-size:12px"></div>
    <button class="btn btn-success" id="scan-btn" onclick="runScan()">▶ Run Scan</button>
  </div>
</div>

<div class="wrap">
  <div class="tabs">
    <button class="tab active" onclick="showTab('scan')">📡 Scan &amp; Opportunities</button>
    <button class="tab" onclick="showTab('positions')">📊 My Positions</button>
    <button class="tab" onclick="showTab('log')">📝 Log a Trade</button>
    <button class="tab" onclick="showTab('history')">📋 History</button>
  </div>

  <!-- SCAN TAB -->
  <div id="tab-scan" class="tab-content active">
    <div id="scan-initial" class="empty">
      <div class="ico">📡</div>
      <p style="margin-bottom:16px;font-size:15px">Click <strong>Run Scan</strong> to fetch live market data and find today's best opportunities.</p>
      <p style="font-size:13px">Every result has a <strong style="color:var(--accent)">Log This Trade</strong> button — click it after you've placed the trade in your broker to start tracking it.</p>
    </div>
    <div id="scan-loading" class="hidden empty">
      <div class="ico">⏳</div>
      <p>Fetching live data from Yahoo Finance…<br><span style="font-size:13px;color:var(--muted)">Takes 30–60 seconds. Hang tight.</span></p>
    </div>
    <div id="scan-results" class="hidden">
      <div class="rddt-box" id="rddt-box"></div>
      <div class="card"><h2>🎡 Wheel Candidates — Cash-Secured Puts</h2>
        <div class="filter-bar"><input type="text" placeholder="Filter by symbol…" oninput="filterTbl('wheel-tbl',this.value)"></div>
        <div class="tbl-wrap"><div id="wheel-tbl-wrap"></div></div>
      </div>
      <div class="card"><h2>🦅 Iron Condor Opportunities</h2>
        <div class="tbl-wrap"><div id="condor-tbl-wrap"></div></div>
      </div>
      <div class="card"><h2>📐 Bull Put Spreads</h2>
        <div class="tbl-wrap"><div id="spread-tbl-wrap"></div></div>
      </div>
    </div>
  </div>

  <!-- POSITIONS TAB -->
  <div id="tab-positions" class="tab-content">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;flex-wrap:wrap;gap:10px">
      <h2 style="font-size:17px">Open Positions &amp; Advice</h2>
      <button class="btn btn-success" id="mon-btn" onclick="runMonitor()">▶ Refresh &amp; Get Advice</button>
    </div>
    <div id="mon-summary" class="summary-strip hidden"></div>
    <div id="mon-results"><div class="empty"><div class="ico">📊</div><p>Click <strong>Refresh &amp; Get Advice</strong> to check your open positions.</p></div></div>
  </div>

  <!-- LOG TAB -->
  <div id="tab-log" class="tab-content">
    <div class="card">
      <h2>📝 Log a Confirmed Trade</h2>
      <p style="color:var(--muted);font-size:13px;margin-bottom:18px">After placing a trade in your broker, record it here so Options Ollie can track and advise on it.</p>
      <div style="margin-bottom:6px;font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.4px">Trade Type</div>
      <div class="type-grid">
        <div class="type-btn selected" onclick="selType('csp',this)"><span class="ico">🟢</span>Cash-Secured Put<br><small style="font-weight:400;color:var(--muted)">(CSP)</small></div>
        <div class="type-btn" onclick="selType('cc',this)"><span class="ico">🔵</span>Covered Call<br><small style="font-weight:400;color:var(--muted)">(CC)</small></div>
        <div class="type-btn" onclick="selType('ic',this)"><span class="ico">🟠</span>Iron Condor<br><small style="font-weight:400;color:var(--muted)">(IC)</small></div>
        <div class="type-btn" onclick="selType('bull_put',this)"><span class="ico">📐</span>Bull Put Spread</div>
        <div class="type-btn" onclick="selType('bear_call',this)"><span class="ico">📐</span>Bear Call Spread</div>
        <div class="type-btn" onclick="selType('shares',this)"><span class="ico">📦</span>Long Shares</div>
      </div>
      <div class="fg" style="margin-bottom:14px">
        <div class="field"><label>Symbol</label><input type="text" id="f-sym" placeholder="e.g. RDDT" style="text-transform:uppercase"></div>
        <div class="field" id="f-ct-wrap"><label id="f-ct-lbl">Contracts</label><input type="number" id="f-ct" value="1" min="1"></div>
      </div>
      <div id="flds-single" class="fg fg3" style="margin-bottom:14px">
        <div class="field"><label id="f-sk-lbl">Strike</label><input type="number" id="f-sk" placeholder="85.00" step="0.5"></div>
        <div class="field"><label>Expiry</label><input type="date" id="f-exp"></div>
        <div class="field"><label id="f-pr-lbl">Premium / Contract ($)</label><input type="number" id="f-pr" placeholder="1.45" step="0.01"></div>
      </div>
      <div id="flds-ic" class="hidden" style="margin-bottom:14px">
        <div class="fg" style="margin-bottom:10px">
          <div class="field"><label>Short Put (sell)</label><input type="number" id="f-sp" placeholder="90" step="0.5"></div>
          <div class="field"><label>Long Put (buy)</label><input type="number" id="f-lp" placeholder="85" step="0.5"></div>
        </div>
        <div class="fg" style="margin-bottom:10px">
          <div class="field"><label>Short Call (sell)</label><input type="number" id="f-sc" placeholder="110" step="0.5"></div>
          <div class="field"><label>Long Call (buy)</label><input type="number" id="f-lc" placeholder="115" step="0.5"></div>
        </div>
        <div class="fg">
          <div class="field"><label>Expiry</label><input type="date" id="f-ic-exp"></div>
          <div class="field"><label>Net Credit / Contract ($)</label><input type="number" id="f-ic-cr" placeholder="2.30" step="0.01"></div>
        </div>
      </div>
      <div id="flds-spread" class="hidden" style="margin-bottom:14px">
        <div class="fg fg3" style="margin-bottom:10px">
          <div class="field"><label id="f-sps-lbl">Short Strike</label><input type="number" id="f-sps" step="0.5"></div>
          <div class="field"><label id="f-spl-lbl">Long Strike</label><input type="number" id="f-spl" step="0.5"></div>
          <div class="field"><label>Expiry</label><input type="date" id="f-sp-exp"></div>
        </div>
        <div class="fg">
          <div class="field"><label>Net Credit / Contract ($)</label><input type="number" id="f-sp-cr" step="0.01"></div><div></div>
        </div>
      </div>
      <div id="flds-shares" class="hidden fg" style="margin-bottom:14px">
        <div class="field"><label>Shares</label><input type="number" id="f-sh" placeholder="100" min="1"></div>
        <div class="field"><label>Cost per Share ($)</label><input type="number" id="f-shc" placeholder="87.50" step="0.01"></div>
      </div>
      <div class="fg" style="margin-bottom:0">
        <div class="field"><label>Commission ($)</label><input type="number" id="f-com" value="0" step="0.01"></div>
        <div class="field"><label>Notes</label><input type="text" id="f-notes" placeholder="optional"></div>
      </div>
      <div id="log-preview" class="hidden preview-box"></div>
      <div class="btn-group">
        <button class="btn btn-primary" onclick="previewLog()">Preview</button>
        <button class="btn btn-success hidden" id="confirm-btn" onclick="confirmLog()">✅ Confirm &amp; Save</button>
        <button class="btn btn-ghost" onclick="resetLog()">Clear</button>
      </div>
    </div>
  </div>

  <!-- HISTORY TAB -->
  <div id="tab-history" class="tab-content">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:18px">
      <h2 style="font-size:17px">Trade Ledger</h2>
      <button class="btn btn-ghost" onclick="loadHistory()">↻ Refresh</button>
    </div>
    <div id="history-wrap"><div class="empty"><div class="ico">📋</div><p>Loading…</p></div></div>
  </div>
</div>

<!-- LOG MODAL (pre-filled from scan results) -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <button class="modal-close" onclick="closeModal()">✕</button>
    <h3 id="m-title">Log This Trade</h3>
    <div class="sub-label" id="m-sub"></div>
    <div class="fg" style="margin-bottom:12px">
      <div class="field"><label>Symbol</label><input type="text" id="m-sym" style="text-transform:uppercase"></div>
      <div class="field" id="m-ct-wrap"><label>Contracts</label><input type="number" id="m-ct" value="1" min="1"></div>
    </div>
    <div id="m-flds-single" class="fg fg3" style="margin-bottom:12px">
      <div class="field"><label id="m-sk-lbl">Strike</label><input type="number" id="m-sk" step="0.5"></div>
      <div class="field"><label>Expiry</label><input type="date" id="m-exp"></div>
      <div class="field">
        <label id="m-pr-lbl">Actual Premium Received ($)</label>
        <input type="number" id="m-pr" step="0.01">
        <small id="m-pr-hint" style="color:var(--muted);font-size:11px;margin-top:3px;display:block"></small>
      </div>
    </div>
    <div id="m-flds-ic" class="hidden" style="margin-bottom:12px">
      <div class="fg" style="margin-bottom:8px">
        <div class="field"><label>Short Put</label><input type="number" id="m-sp" step="0.5"></div>
        <div class="field"><label>Long Put</label><input type="number" id="m-lp" step="0.5"></div>
      </div>
      <div class="fg" style="margin-bottom:8px">
        <div class="field"><label>Short Call</label><input type="number" id="m-sc" step="0.5"></div>
        <div class="field"><label>Long Call</label><input type="number" id="m-lc" step="0.5"></div>
      </div>
      <div class="fg">
        <div class="field"><label>Expiry</label><input type="date" id="m-ic-exp"></div>
        <div class="field"><label>Actual Net Credit / Contract ($)</label><input type="number" id="m-ic-cr" step="0.01"><small id="m-ic-hint" style="color:var(--muted);font-size:11px;margin-top:3px;display:block"></small></div>
      </div>
    </div>
    <div id="m-flds-spread" class="hidden" style="margin-bottom:12px">
      <div class="fg fg3" style="margin-bottom:8px">
        <div class="field"><label id="m-sps-lbl">Short Strike</label><input type="number" id="m-sps" step="0.5"></div>
        <div class="field"><label id="m-spl-lbl">Long Strike</label><input type="number" id="m-spl" step="0.5"></div>
        <div class="field"><label>Expiry</label><input type="date" id="m-sp-exp"></div>
      </div>
      <div class="field"><label>Actual Net Credit / Contract ($)</label><input type="number" id="m-sp-cr" step="0.01"><small id="m-sp-hint" style="color:var(--muted);font-size:11px;margin-top:3px;display:block"></small></div>
    </div>
    <div class="fg" style="margin-bottom:12px">
      <div class="field"><label>Commission ($)</label><input type="number" id="m-com" value="0" step="0.01"></div>
      <div class="field"><label>Notes</label><input type="text" id="m-notes" placeholder="optional"></div>
    </div>
    <div class="btn-group">
      <button class="btn btn-success" id="m-save-btn" onclick="saveModal()">✅ Confirm &amp; Save Trade</button>
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
    </div>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
let selTypeVal='csp', modalType='';

// clock
function tick(){const n=new Date();document.getElementById('clock').textContent=n.toLocaleDateString('en-AU',{weekday:'short',day:'2-digit',month:'short'})+' '+n.toLocaleTimeString('en-AU',{hour:'2-digit',minute:'2-digit'})}
tick();setInterval(tick,15000);

// tabs
function showTab(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
  document.querySelector(`.tab[onclick="showTab('${name}')"]`).classList.add('active');
  document.getElementById('tab-'+name).classList.add('active');
  if(name==='history')loadHistory();
  if(name==='positions'&&document.getElementById('mon-summary').classList.contains('hidden'))runMonitor();
}

// ── SCAN ──────────────────────────────────────────────────────────────
async function runScan(){
  const btn=document.getElementById('scan-btn');
  btn.disabled=true;btn.innerHTML='<span class="loader"></span> Scanning…';
  document.getElementById('scan-initial').classList.add('hidden');
  document.getElementById('scan-results').classList.add('hidden');
  document.getElementById('scan-loading').classList.remove('hidden');
  try{
    const r=await fetch('/api/scan');const data=await r.json();
    if(data.error){showToast('Scan error: '+data.error,'error');document.getElementById('scan-loading').classList.add('hidden');document.getElementById('scan-initial').classList.remove('hidden');return}
    renderScan(data);showToast('Scan complete!','success');
    document.getElementById('scan-loading').classList.add('hidden');
    document.getElementById('scan-results').classList.remove('hidden');
  }catch(e){showToast('Error: '+e.message,'error');document.getElementById('scan-loading').classList.add('hidden');document.getElementById('scan-initial').classList.remove('hidden')}
  finally{btn.disabled=false;btn.innerHTML='↻ Refresh Scan'}
}

function renderScan(data){
  renderRddt(data.rddt||{});
  renderWheelTbl(data.wheel_candidates||[]);
  renderCondorTbl(data.iron_condors||[]);
  renderSpreadTbl(data.credit_spreads||[]);
}

function renderRddt(r){
  const ivC=r.iv_rank>40?'pos':'warn';
  const recHtml=r.action?`<div class="rddt-action"><strong>Recommendation:</strong> ${r.action}<br><span style="color:var(--muted);font-size:13px">${r.reasoning||''}</span></div>`:'';
  const notesHtml=(r.risk_notes||[]).map(n=>`<div class="rddt-note">${n}</div>`).join('');

  // ── Covered call table ────────────────────────────────────────────────
  let ccHtml='';
  if((r.top_covered_calls||[]).length){
    const rows=r.top_covered_calls.map(c=>`<tr>
      <td><strong>$${c.strike}</strong></td><td>${c.expiry}</td><td>${c.dte}d</td>
      <td class="pos">$${(c.mid_price||0).toFixed(2)}</td>
      <td>$${(c.total_premium||0).toLocaleString('en-AU',{maximumFractionDigits:0})}</td>
      <td>${(c.prob_otm||0).toFixed(0)}%</td>
      <td class="pos">${(c.annualized_if_called||0).toFixed(1)}%</td>
      <td><button class="log-btn" onclick='openModal("cc",${JSON.stringify({symbol:r.symbol||"RDDT",strike:c.strike,expiry:c.expiry,mid_price:c.mid_price,contracts:r.contracts_available||2})})'>Log This Trade</button></td>
    </tr>`).join('');
    ccHtml=`<h3 style="font-size:14px;margin:18px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">📈 Covered Call Opportunities</h3>
    <div class="tbl-wrap"><table><thead><tr><th>Strike</th><th>Expiry</th><th>DTE</th><th>Mid</th><th>Total Premium</th><th>Prob OTM</th><th>Ann Ret</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  // ── Downside scenario table ───────────────────────────────────────────
  let scenarioHtml='';
  const ra=r.risk_analysis||{};
  if((ra.scenarios||[]).length){
    const sigma1Down=ra.one_sigma_down||0;
    const sigma1Up=ra.one_sigma_up||0;
    const rows=ra.scenarios.map(s=>{
      const cls=s.below_cost_basis?'neg':'warn';
      const basisFlag=s.below_cost_basis?'<span class="badge badge-red" style="font-size:10px;margin-left:4px">Below Basis</span>':'';
      return `<tr>
        <td><strong class="${cls}">-${s.drop_pct}%</strong></td>
        <td class="${cls}">$${(s.target_price||0).toFixed(2)}${basisFlag}</td>
        <td class="neg">-$${Math.abs(s.dollar_loss_from_now||0).toLocaleString('en-AU',{maximumFractionDigits:0})}</td>
        <td class="${cls}">${s.pnl_vs_cost_basis>=0?'+':''}\$${(s.pnl_vs_cost_basis||0).toLocaleString('en-AU',{maximumFractionDigits:0})}</td>
        <td class="${cls}">${s.pct_of_capital>=0?'+':''}${(s.pct_of_capital||0).toFixed(1)}% of capital</td>
      </tr>`;
    }).join('');
    const sigmaNote=sigma1Down?`<div style="font-size:12px;color:var(--muted);margin-top:8px">📊 1-sigma 30-day range: <span class="neg">$${sigma1Down.toFixed(2)}</span> ↔ <span class="pos">$${sigma1Up.toFixed(2)}</span> &nbsp;|&nbsp; 52-week range: <span class="neg">$${(ra['52w_low']||0).toFixed(2)}</span> – <span class="pos">$${(ra['52w_high']||0).toFixed(2)}</span></div>`:'';
    scenarioHtml=`<h3 style="font-size:14px;margin:20px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">⚠️ Downside Scenarios</h3>
    <div class="tbl-wrap"><table><thead><tr><th>Drop</th><th>Price</th><th>$ Loss from Now</th><th>P&L vs Cost Basis</th><th>Capital Impact</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${sigmaNote}`;
  }

  // ── Protective puts section ───────────────────────────────────────────
  let protHtml='';
  if((r.protective_strategies||[]).length){
    const rows=r.protective_strategies.map(p=>`<tr>
      <td><strong>$${p.strike}</strong></td>
      <td>${p.expiry||'—'}</td>
      <td>${p.dte||'—'}d</td>
      <td class="neg">$${(p.mid_price||0).toFixed(2)}/contract</td>
      <td class="neg">$${(p.total_cost||0).toLocaleString('en-AU',{maximumFractionDigits:0})} total</td>
      <td>$${(p.effective_floor||0).toFixed(2)}</td>
      <td class="warn">${(p.annualised_cost_pct||0).toFixed(1)}%/yr</td>
      <td style="color:var(--muted);font-size:12px">${p.verdict||''}</td>
      <td><button class="log-btn" onclick='openModal("protective_put",${JSON.stringify({symbol:r.symbol||"RDDT",strike:p.strike,expiry:p.expiry,mid_price:p.mid_price,contracts:r.contracts_available||2})})'>Log Trade</button></td>
    </tr>`).join('');
    protHtml=`<h3 style="font-size:14px;margin:20px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">🛡️ Protective Put Options</h3>
    <div class="tbl-wrap"><table><thead><tr><th>Strike</th><th>Expiry</th><th>DTE</th><th>Cost</th><th>Total (2 contracts)</th><th>Effective Floor</th><th>Ann. Cost</th><th>Verdict</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  // ── Collar strategies section ─────────────────────────────────────────
  let collarHtml='';
  if((r.collar_strategies||[]).length){
    const rows=r.collar_strategies.map(c=>{
      const credClass=c.net_credit>=0?'pos':'neg';
      const credLabel=c.net_credit>=0?`<span class="badge badge-green">Net Credit $${c.net_credit.toFixed(0)}</span>`:`<span class="badge badge-red">Net Debit $${Math.abs(c.net_credit).toFixed(0)}</span>`;
      return `<tr>
        <td>Sell $${c.cc_strike} CC / Buy $${c.put_strike} Put</td>
        <td>${c.expiry||'—'} (${c.dte||'—'}d)</td>
        <td class="pos">$${(c.cc_premium||0).toFixed(2)}</td>
        <td class="neg">$${(c.put_cost||0).toFixed(2)}</td>
        <td>${credLabel}</td>
        <td style="color:var(--muted);font-size:12px">${c.verdict||''}</td>
        <td><button class="log-btn" onclick='openModal("collar",${JSON.stringify({symbol:r.symbol||"RDDT",cc_strike:c.cc_strike,put_strike:c.put_strike,expiry:c.expiry,cc_premium:c.cc_premium,put_cost:c.put_cost,contracts:r.contracts_available||2})})'>Log Trade</button></td>
      </tr>`;
    }).join('');
    collarHtml=`<h3 style="font-size:14px;margin:20px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">🔗 Collar Strategies (Sell CC + Buy Put)</h3>
    <div class="tbl-wrap"><table><thead><tr><th>Structure</th><th>Expiry</th><th>CC Premium</th><th>Put Cost</th><th>Net</th><th>Verdict</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  // ── Cost basis roadmap ────────────────────────────────────────────────
  let roadmapHtml='';
  const cbr=r.cost_basis_roadmap||{};
  if(cbr.targets&&cbr.targets.length){
    const note=cbr.strategy_note||'';
    const monthlyEst=cbr.monthly_cc_income_estimate||0;
    const rows=cbr.targets.map(t=>`<tr>
      <td>Reduce basis by ${t.target_pct}%</td>
      <td>$${(t.target_basis||0).toFixed(2)}</td>
      <td class="neg">-$${(t.reduction_needed||0).toLocaleString('en-AU',{maximumFractionDigits:0})}</td>
      <td class="pos">${t.months_at_current_cc==='N/A'?'N/A (no CC income)':t.months_at_current_cc+' months'}</td>
    </tr>`).join('');
    roadmapHtml=`<h3 style="font-size:14px;margin:20px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">🗺️ Cost Basis Reduction Roadmap</h3>
    <div style="font-size:13px;color:var(--muted);margin-bottom:8px">Est. monthly CC income: <strong style="color:var(--text)">$${monthlyEst.toLocaleString('en-AU',{maximumFractionDigits:0})}</strong> — ${note}</div>
    <div class="tbl-wrap"><table><thead><tr><th>Target</th><th>New Basis</th><th>Income Needed</th><th>Time (at est. CC income)</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  document.getElementById('rddt-box').innerHTML=`
    <h2>🎯 RDDT — Your Active Position</h2>
    <div class="stats-row">
      <div class="stat"><div class="lbl">Price</div><div class="val">$${r.current_price||'—'}</div></div>
      <div class="stat"><div class="lbl">Shares</div><div class="val">${r.shares_held||200}</div></div>
      <div class="stat"><div class="lbl">IV Rank</div><div class="val ${ivC}">${r.iv_rank||'—'}%</div></div>
      <div class="stat"><div class="lbl">Contracts</div><div class="val">${r.contracts_available||2}</div></div>
      ${ra.position_value?`<div class="stat"><div class="lbl">Position Value</div><div class="val" style="font-size:16px">$${(ra.position_value||0).toLocaleString('en-AU',{maximumFractionDigits:0})}</div></div>`:''}
      ${ra.total_cost?`<div class="stat"><div class="lbl">Unrealized P&L</div><div class="val ${(ra.position_value-ra.total_cost)>=0?'pos':'neg'}" style="font-size:16px">${(ra.position_value-ra.total_cost)>=0?'+':''}$${((ra.position_value||0)-(ra.total_cost||0)).toLocaleString('en-AU',{maximumFractionDigits:0})}</div></div>`:''}
    </div>
    ${recHtml}${notesHtml}
    ${scenarioHtml}
    ${ccHtml}
    ${protHtml}
    ${collarHtml}
    ${roadmapHtml}`;
}

function renderWheelTbl(rows){
  if(!rows.length){document.getElementById('wheel-tbl-wrap').innerHTML='<p style="padding:16px;color:var(--muted)">No candidates found.</p>';return}
  const trs=rows.map(r=>`<tr>
    <td><strong>${r.symbol}</strong></td>
    <td>$${(r.stock_price||0).toFixed(2)}</td>
    <td><strong>$${r.strike}</strong></td>
    <td>${r.expiry}</td><td>${r.dte}d</td>
    <td class="pos">$${(r.mid_price||0).toFixed(2)}</td>
    <td class="pos">${r.annualized_return||'—'}%</td>
    <td>${(r.prob_otm||0).toFixed(0)}%</td>
    <td>${r.iv_rank||'—'}%</td>
    <td><span class="badge badge-${r.score>0.6?'green':r.score>0.4?'blue':'orange'}">${(r.score||0).toFixed(2)}</span></td>
    <td><button class="log-btn" onclick='openModal("csp",${JSON.stringify({symbol:r.symbol,strike:r.strike,expiry:r.expiry,mid_price:r.mid_price,contracts:1})})'>Log This Trade</button></td>
  </tr>`).join('');
  document.getElementById('wheel-tbl-wrap').innerHTML=`<table id="wheel-tbl"><thead><tr>
    <th>Symbol</th><th>Price</th><th>Strike</th><th>Expiry</th><th>DTE</th>
    <th>Mid</th><th>Ann Ret</th><th>Prob OTM</th><th>IV Rank</th><th>Score</th><th></th>
  </tr></thead><tbody>${trs}</tbody></table>`;
}

function renderCondorTbl(rows){
  if(!rows.length){document.getElementById('condor-tbl-wrap').innerHTML='<p style="padding:16px;color:var(--muted)">No iron condor setups found (IV may be too low today).</p>';return}
  const trs=rows.map(r=>`<tr>
    <td><strong>${r.symbol}</strong></td>
    <td>$${(r.stock_price||0).toFixed(2)}</td>
    <td>$${r.short_put}/$${r.short_call}</td>
    <td>$${r.long_put}/$${r.long_call}</td>
    <td>${r.expiry}</td><td>${r.dte}d</td>
    <td class="pos">$${(r.total_credit||0).toFixed(2)}</td>
    <td class="pos">${(r.return_on_risk_pct||0).toFixed(1)}%</td>
    <td>${(r.prob_profit_est||0).toFixed(0)}%</td>
    <td>${r.iv_rank||'—'}%</td>
    <td><button class="log-btn" onclick='openModal("ic",${JSON.stringify({symbol:r.symbol,short_put:r.short_put,long_put:r.long_put,short_call:r.short_call,long_call:r.long_call,expiry:r.expiry,credit:r.total_credit,contracts:1})})'>Log This Trade</button></td>
  </tr>`).join('');
  document.getElementById('condor-tbl-wrap').innerHTML=`<table><thead><tr>
    <th>Symbol</th><th>Price</th><th>Short Legs</th><th>Long Legs</th>
    <th>Expiry</th><th>DTE</th><th>Credit</th><th>RoR</th><th>Prob Profit</th><th>IV Rank</th><th></th>
  </tr></thead><tbody>${trs}</tbody></table>`;
}

function renderSpreadTbl(rows){
  if(!rows.length){document.getElementById('spread-tbl-wrap').innerHTML='<p style="padding:16px;color:var(--muted)">No credit spread opportunities found.</p>';return}
  const trs=rows.map(r=>`<tr>
    <td><strong>${r.symbol}</strong></td>
    <td>$${(r.stock_price||0).toFixed(2)}</td>
    <td>$${r.short_strike}/$${r.long_strike}</td>
    <td>${r.expiry}</td><td>${r.dte}d</td>
    <td class="pos">$${(r.net_credit||0).toFixed(2)}</td>
    <td class="pos">${(r.return_on_risk_pct||0).toFixed(1)}%</td>
    <td>${(r.prob_otm||0).toFixed(0)}%</td>
    <td>${r.iv_rank||'—'}%</td>
    <td><button class="log-btn" onclick='openModal("bull_put",${JSON.stringify({symbol:r.symbol,short_strike:r.short_strike,long_strike:r.long_strike,expiry:r.expiry,credit:r.net_credit,contracts:1})})'>Log This Trade</button></td>
  </tr>`).join('');
  document.getElementById('spread-tbl-wrap').innerHTML=`<table><thead><tr>
    <th>Symbol</th><th>Price</th><th>Short/Long Strike</th>
    <th>Expiry</th><th>DTE</th><th>Credit</th><th>RoR</th><th>Prob OTM</th><th>IV Rank</th><th></th>
  </tr></thead><tbody>${trs}</tbody></table>`;
}

function filterTbl(tblId,q){
  const tbl=document.getElementById(tblId);if(!tbl)return;
  tbl.querySelectorAll('tbody tr').forEach(r=>{r.style.display=r.cells[0]?.textContent.toLowerCase().includes(q.toLowerCase())?'':'none'});
}

// ── MODAL (pre-filled from scan) ───────────────────────────────────────
function openModal(type,prefill){
  modalType=type;
  ['m-flds-single','m-flds-ic','m-flds-spread'].forEach(id=>document.getElementById(id).classList.add('hidden'));
  document.getElementById('m-ct-wrap').classList.remove('hidden');
  const names={csp:'Cash-Secured Put',cc:'Covered Call',ic:'Iron Condor',bull_put:'Bull Put Spread',bear_call:'Bear Call Spread',protective_put:'Protective Put (Hedge)',collar:'Collar (CC + Protective Put)'};
  document.getElementById('m-title').textContent='Log This Trade — '+names[type];
  document.getElementById('m-sym').value=prefill.symbol||'';
  document.getElementById('m-ct').value=prefill.contracts||1;
  document.getElementById('m-com').value=0;
  document.getElementById('m-notes').value='';

  if(type==='csp'||type==='cc'||type==='protective_put'){
    document.getElementById('m-flds-single').classList.remove('hidden');
    document.getElementById('m-sk-lbl').textContent=type==='csp'?'Put Strike':type==='protective_put'?'Put Strike (floor)':'Call Strike';
    document.getElementById('m-pr-lbl').textContent=type==='protective_put'?'Actual Premium Paid / Contract ($)':'Actual Premium Received / Contract ($)';
    document.getElementById('m-sk').value=prefill.strike||'';
    document.getElementById('m-exp').value=prefill.expiry||'';
    document.getElementById('m-pr').value='';
    document.getElementById('m-pr-hint').textContent=prefill.mid_price?`Suggested mid: $${prefill.mid_price.toFixed(2)} — enter what you actually filled at`:'';
    document.getElementById('m-sub').textContent=`${prefill.symbol} ${type==='protective_put'?'Protective Put':'CC'} $${prefill.strike} exp ${prefill.expiry}`;
  } else if(type==='collar'){
    document.getElementById('m-flds-spread').classList.remove('hidden');
    document.getElementById('m-sps-lbl').textContent='Short Call Strike (CC sold)';
    document.getElementById('m-spl-lbl').textContent='Long Put Strike (protection bought)';
    document.getElementById('m-sps').value=prefill.cc_strike||'';
    document.getElementById('m-spl').value=prefill.put_strike||'';
    document.getElementById('m-sp-exp').value=prefill.expiry||'';
    document.getElementById('m-sp-cr').value='';
    const netGuide=prefill.cc_premium&&prefill.put_cost?`Suggested net: CC $${prefill.cc_premium.toFixed(2)} – Put $${prefill.put_cost.toFixed(2)} = $${(prefill.cc_premium-prefill.put_cost).toFixed(2)} net credit — enter actual net`:'';
    document.getElementById('m-sp-hint').textContent=netGuide;
    document.getElementById('m-sub').textContent=`${prefill.symbol} Collar: sell $${prefill.cc_strike}C / buy $${prefill.put_strike}P exp ${prefill.expiry}`;
  } else if(type==='ic'){
    document.getElementById('m-flds-ic').classList.remove('hidden');
    document.getElementById('m-sp').value=prefill.short_put||'';
    document.getElementById('m-lp').value=prefill.long_put||'';
    document.getElementById('m-sc').value=prefill.short_call||'';
    document.getElementById('m-lc').value=prefill.long_call||'';
    document.getElementById('m-ic-exp').value=prefill.expiry||'';
    document.getElementById('m-ic-cr').value='';
    document.getElementById('m-ic-hint').textContent=prefill.credit?`Suggested credit: $${prefill.credit.toFixed(2)} — enter actual fill`:'';
    document.getElementById('m-sub').textContent=`${prefill.symbol} IC $${prefill.short_put}P/$${prefill.short_call}C exp ${prefill.expiry}`;
  } else if(type==='bull_put'||type==='bear_call'){
    document.getElementById('m-flds-spread').classList.remove('hidden');
    document.getElementById('m-sps-lbl').textContent=type==='bull_put'?'Short Put Strike':'Short Call Strike';
    document.getElementById('m-spl-lbl').textContent=type==='bull_put'?'Long Put Strike':'Long Call Strike';
    document.getElementById('m-sps').value=prefill.short_strike||'';
    document.getElementById('m-spl').value=prefill.long_strike||'';
    document.getElementById('m-sp-exp').value=prefill.expiry||'';
    document.getElementById('m-sp-cr').value='';
    document.getElementById('m-sp-hint').textContent=prefill.credit?`Suggested credit: $${prefill.credit.toFixed(2)} — enter actual fill`:'';
    document.getElementById('m-sub').textContent=`${prefill.symbol} spread $${prefill.short_strike}/$${prefill.long_strike} exp ${prefill.expiry}`;
  }
  document.getElementById('modal').classList.add('open');
}

function closeModal(){document.getElementById('modal').classList.remove('open')}

async function saveModal(){
  const btn=document.getElementById('m-save-btn');btn.disabled=true;btn.innerHTML='<span class="loader"></span> Saving…';
  const payload={trade_type:modalType,symbol:document.getElementById('m-sym').value.trim().toUpperCase(),contracts:parseInt(document.getElementById('m-ct').value)||1,commission:parseFloat(document.getElementById('m-com').value)||0,notes:document.getElementById('m-notes').value.trim()};
  if(!payload.symbol){showToast('Symbol required','error');btn.disabled=false;btn.innerHTML='✅ Confirm &amp; Save Trade';return}
  if(modalType==='csp'||modalType==='cc'||modalType==='protective_put'){
    payload.strike=parseFloat(document.getElementById('m-sk').value);
    payload.expiry=document.getElementById('m-exp').value;
    payload.premium=parseFloat(document.getElementById('m-pr').value);
    if(!payload.strike||!payload.expiry||!payload.premium){showToast('Fill Strike, Expiry and Premium','error');btn.disabled=false;btn.innerHTML='✅ Confirm &amp; Save Trade';return}
  } else if(modalType==='ic'){
    payload.short_put=parseFloat(document.getElementById('m-sp').value);
    payload.long_put=parseFloat(document.getElementById('m-lp').value);
    payload.short_call=parseFloat(document.getElementById('m-sc').value);
    payload.long_call=parseFloat(document.getElementById('m-lc').value);
    payload.expiry=document.getElementById('m-ic-exp').value;
    payload.credit=parseFloat(document.getElementById('m-ic-cr').value);
  } else if(modalType==='bull_put'||modalType==='bear_call'){
    payload.short_strike=parseFloat(document.getElementById('m-sps').value);
    payload.long_strike=parseFloat(document.getElementById('m-spl').value);
    payload.expiry=document.getElementById('m-sp-exp').value;
    payload.credit=parseFloat(document.getElementById('m-sp-cr').value);
  }
  try{
    const r=await fetch('/api/log-trade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const res=await r.json();
    if(res.success){showToast(`✅ Trade saved! ${res.trade_id} — $${res.premium_received} premium`,'success');closeModal();setTimeout(()=>showTab('positions'),1200)}
    else showToast('Error: '+res.error,'error');
  }catch(e){showToast('Error: '+e.message,'error')}
  finally{btn.disabled=false;btn.innerHTML='✅ Confirm &amp; Save Trade'}
}

// ── MANUAL LOG ─────────────────────────────────────────────────────────
function selType(t,el){
  selTypeVal=t;
  document.querySelectorAll('.type-btn').forEach(b=>b.classList.remove('selected'));el.classList.add('selected');
  ['flds-single','flds-ic','flds-spread','flds-shares'].forEach(id=>document.getElementById(id).classList.add('hidden'));
  document.getElementById('f-ct-wrap').classList.remove('hidden');
  document.getElementById('log-preview').classList.add('hidden');
  document.getElementById('confirm-btn').classList.add('hidden');
  if(t==='csp'||t==='cc'){document.getElementById('flds-single').classList.remove('hidden');document.getElementById('f-sk-lbl').textContent=t==='csp'?'Put Strike':'Call Strike'}
  else if(t==='ic')document.getElementById('flds-ic').classList.remove('hidden');
  else if(t==='bull_put'||t==='bear_call'){document.getElementById('flds-spread').classList.remove('hidden');document.getElementById('f-sps-lbl').textContent=t==='bull_put'?'Short Put Strike':'Short Call Strike';document.getElementById('f-spl-lbl').textContent=t==='bull_put'?'Long Put Strike':'Long Call Strike'}
  else if(t==='shares'){document.getElementById('flds-shares').classList.remove('hidden');document.getElementById('f-ct-wrap').classList.add('hidden')}
}

function collectLog(){
  const sym=document.getElementById('f-sym').value.trim().toUpperCase();
  if(!sym){showToast('Enter a symbol','error');return null}
  const base={trade_type:selTypeVal,symbol:sym,contracts:parseInt(document.getElementById('f-ct').value)||1,commission:parseFloat(document.getElementById('f-com').value)||0,notes:document.getElementById('f-notes').value.trim()};
  if(selTypeVal==='csp'||selTypeVal==='cc'){
    const strike=parseFloat(document.getElementById('f-sk').value),expiry=document.getElementById('f-exp').value,premium=parseFloat(document.getElementById('f-pr').value);
    if(!strike||!expiry||!premium){showToast('Fill Strike, Expiry and Premium','error');return null}
    return{...base,strike,expiry,premium};
  }
  if(selTypeVal==='ic'){const sp=parseFloat(document.getElementById('f-sp').value),lp=parseFloat(document.getElementById('f-lp').value),sc=parseFloat(document.getElementById('f-sc').value),lc=parseFloat(document.getElementById('f-lc').value),expiry=document.getElementById('f-ic-exp').value,credit=parseFloat(document.getElementById('f-ic-cr').value);if(!sp||!lp||!sc||!lc||!expiry||!credit){showToast('Fill all IC fields','error');return null}return{...base,short_put:sp,long_put:lp,short_call:sc,long_call:lc,expiry,credit}}
  if(selTypeVal==='bull_put'||selTypeVal==='bear_call'){const sps=parseFloat(document.getElementById('f-sps').value),spl=parseFloat(document.getElementById('f-spl').value),expiry=document.getElementById('f-sp-exp').value,credit=parseFloat(document.getElementById('f-sp-cr').value);if(!sps||!spl||!expiry||!credit){showToast('Fill all spread fields','error');return null}return{...base,short_strike:sps,long_strike:spl,expiry,credit}}
  if(selTypeVal==='shares'){const shares=parseInt(document.getElementById('f-sh').value),cost=parseFloat(document.getElementById('f-shc').value);if(!shares||!cost){showToast('Enter shares and cost','error');return null}return{...base,shares,cost_per_share:cost}}
  return null;
}

function previewLog(){
  const d=collectLog();if(!d)return;
  const names={csp:'CSP',cc:'Covered Call',ic:'Iron Condor',bull_put:'Bull Put Spread',bear_call:'Bear Call Spread',shares:'Long Shares',protective_put:'Protective Put',collar:'Collar'};
  let lines=[`<strong>${names[d.trade_type]}</strong> — ${d.symbol}`];
  if(d.strike)lines.push(`Strike: <strong>$${d.strike}</strong>  |  Expiry: ${d.expiry}`);
  if(d.premium){const tot=(d.premium*(d.contracts||1)*100).toFixed(2);lines.push(`Premium: <strong>$${d.premium}/contract → $${tot} total received</strong>`)}
  if(d.credit){const tot=(d.credit*(d.contracts||1)*100).toFixed(2);lines.push(`Net credit: <strong>$${d.credit}/contract → $${tot} total received</strong>`)}
  if(d.short_put)lines.push(`Legs: $${d.short_put}P / $${d.short_call}C short  |  $${d.long_put}P / $${d.long_call}C long`);
  if(d.shares)lines.push(`${d.shares} shares @ $${d.cost_per_share} = $${(d.shares*d.cost_per_share).toFixed(2)}`);
  if(d.contracts&&d.trade_type!=='shares')lines.push(`Contracts: ${d.contracts}`);
  const box=document.getElementById('log-preview');box.innerHTML=lines.join('<br>');box.classList.remove('hidden');
  document.getElementById('confirm-btn').classList.remove('hidden');
}

async function confirmLog(){
  const d=collectLog();if(!d)return;
  const btn=document.getElementById('confirm-btn');btn.disabled=true;btn.innerHTML='<span class="loader"></span> Saving…';
  try{
    const r=await fetch('/api/log-trade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
    const res=await r.json();
    if(res.success){showToast(`✅ Trade saved! ${res.trade_id} — $${res.premium_received} premium`,'success');resetLog();setTimeout(()=>showTab('positions'),1200)}
    else showToast('Error: '+res.error,'error');
  }catch(e){showToast('Error: '+e.message,'error')}
  finally{btn.disabled=false;btn.innerHTML='✅ Confirm &amp; Save'}
}

function resetLog(){
  ['f-sym','f-sk','f-exp','f-pr','f-sp','f-lp','f-sc','f-lc','f-ic-exp','f-ic-cr','f-sps','f-spl','f-sp-exp','f-sp-cr','f-sh','f-shc','f-notes'].forEach(id=>{const el=document.getElementById(id);if(el)el.value=''});
  document.getElementById('f-ct').value=1;document.getElementById('f-com').value=0;
  document.getElementById('log-preview').classList.add('hidden');
  document.getElementById('confirm-btn').classList.add('hidden');
}

// ── MONITOR ────────────────────────────────────────────────────────────
async function runMonitor(){
  const btn=document.getElementById('mon-btn');btn.disabled=true;btn.innerHTML='<span class="loader"></span> Fetching…';
  document.getElementById('mon-results').innerHTML='<div class="empty"><div class="ico">⏳</div><p>Fetching live prices…</p></div>';
  document.getElementById('mon-summary').classList.add('hidden');
  try{
    const r=await fetch('/api/monitor');const data=await r.json();
    if(data.error){document.getElementById('mon-results').innerHTML=`<div class="empty"><p>Error: ${data.error}</p></div>`;return}
    if(!data.positions||!data.positions.length){
      document.getElementById('mon-results').innerHTML='<div class="empty"><div class="ico">📭</div><p>No open positions yet.<br>Run a scan and click <strong style="color:var(--accent)">Log This Trade</strong> after placing a trade in your broker.</p><button class="btn btn-primary" onclick="showTab(\'scan\')" style="margin-top:14px">📡 Go to Scan</button></div>';return
    }
    renderMonSummary(data);renderMonPositions(data.positions);showToast('Positions updated','success');
  }catch(e){showToast('Error: '+e.message,'error')}
  finally{btn.disabled=false;btn.innerHTML='▶ Refresh &amp; Get Advice'}
}

function renderMonSummary(r){
  const pnl=r.total_unrealized_pnl||0,pnlC=pnl>=0?'pos':'neg',pct=r.overall_pct_captured||0;
  const pills=[r.urgent_count?`<span class="badge badge-red">🚨 ${r.urgent_count} URGENT</span>`:'',r.action_count?`<span class="badge badge-green">✅ ${r.action_count} ACTION</span>`:'',r.watch_count?`<span class="badge badge-orange">👁 ${r.watch_count} WATCH</span>`:'',r.hold_count?`<span class="badge badge-blue">✓ ${r.hold_count} HOLD</span>`:''].filter(Boolean).join(' ');
  const s=document.getElementById('mon-summary');
  s.innerHTML=`<div class="sum-pill"><div class="val">${r.total_positions}</div><div class="lbl">Positions</div></div><div class="sum-pill"><div class="val">$${(r.total_premium_at_risk||0).toFixed(0)}</div><div class="lbl">Premium at Risk</div></div><div class="sum-pill"><div class="val ${pnlC}">${pnl>=0?'+':''}$${Math.abs(pnl).toFixed(0)}</div><div class="lbl">Unrealized P&L</div></div><div class="sum-pill"><div class="val ${pct>=50?'pos':pct>=25?'warn':'neu'}">${pct.toFixed(0)}%</div><div class="lbl">Captured</div></div><div class="sum-pill" style="display:flex;align-items:center;justify-content:center;gap:6px;flex-wrap:wrap">${pills||'<span style="color:var(--muted);font-size:12px">All good</span>'}</div>`;
  s.classList.remove('hidden');
}

function renderMonPositions(positions){
  const lo={URGENT:0,ACTION:1,WATCH:2,HOLD:3};
  positions.sort((a,b)=>(lo[a.advice_level]||9)-(lo[b.advice_level]||9));
  document.getElementById('mon-results').innerHTML=positions.map(posCard).join('');
}

function posCard(p){
  const lvl=(p.advice_level||'HOLD').toLowerCase();
  const ico={urgent:'🚨',action:'✅',watch:'👁',hold:'✓'}[lvl]||'•';
  const pnl=p.unrealized_pnl||0,pnlC=pnl>=0?'pos':'neg';
  const pct=p.pct_max_profit||0,pctC=pct>=50?'pos':pct>=25?'warn':'neu';
  const pctW=Math.max(0,Math.min(100,pct)),fillCol=pct>=50?'#22c55e':pct>=25?'#f59e0b':'#3b82f6';
  const dte=p.dte!=null?p.dte+'d':'—',dteC=p.dte!=null&&p.dte<=7?'neg':p.dte<=21?'warn':'neu';
  let strike='—';if(p.short_put_strike&&p.short_call_strike)strike=`$${p.short_put_strike}/$${p.short_call_strike}`;else if(p.strike)strike=`$${p.strike}`;
  let dist='';if(p.pct_to_short_put!=null){const c=p.pct_to_short_put<5?'neg':p.pct_to_short_put<10?'warn':'pos';dist+=`<span class="${c}">${p.pct_to_short_put.toFixed(1)}% above put</span> `}
  if(p.pct_to_short_call!=null){const c=p.pct_to_short_call<5?'neg':p.pct_to_short_call<10?'warn':'pos';dist+=`<span class="${c}">${p.pct_to_short_call.toFixed(1)}% below call</span>`}
  const acts=(p.advice_actions||[]).map(a=>`<li>${a}</li>`).join('');
  return`<div class="pos-card ${lvl}"><div class="pos-header"><div><div class="pos-title">${ico} ${p.symbol} — ${(p.trade_type||'').toUpperCase()} <span style="font-weight:400;font-size:12px;color:var(--muted)">[${p.trade_id}]</span></div><div class="pos-meta">Strike: ${strike} &nbsp;|&nbsp; Exp: ${p.expiry||'—'} &nbsp;|&nbsp; Stock: $${(p.current_price||0).toFixed(2)}${dist?' &nbsp;|&nbsp; '+dist:''}</div></div><div style="text-align:right"><div style="font-size:20px;font-weight:700" class="${pnlC}">${pnl>=0?'+':''}$${Math.abs(pnl).toFixed(0)}</div><div style="font-size:11px;color:var(--muted)">Unrealized P&L</div></div></div><div class="pos-stats"><div class="pos-stat"><div class="sl">Max Premium</div><div class="sv">$${(p.premium_received||0).toFixed(0)}</div></div><div class="pos-stat"><div class="sl">% Captured</div><div class="sv ${pctC}">${pct.toFixed(0)}%</div></div><div class="pos-stat"><div class="sl">DTE</div><div class="sv ${dteC}">${dte}</div></div><div class="pos-stat"><div class="sl">Entry</div><div class="sv" style="font-size:13px">${p.entry_date||'—'}</div></div></div><div class="progress-bar"><div class="progress-fill" style="width:${pctW}%;background:${fillCol}"></div></div><div class="advice-hl">${p.advice_headline||''}</div><div class="advice-dt">${p.advice_detail||''}</div>${acts?`<ul class="action-list">${acts}</ul>`:''}<div style="margin-top:12px"><button class="btn btn-ghost btn-sm" onclick="closePosition('${p.trade_id}','${p.symbol}')">Close / Expire Position</button></div></div>`;
}

function closePosition(id,sym){
  const px=prompt(`Close ${id} (${sym})\n\nEnter buy-back price per contract (enter 0 if expired worthless):`,'0.00');
  if(px===null)return;
  const price=parseFloat(px);if(isNaN(price)||price<0){showToast('Invalid price','error');return}
  fetch('/api/close-trade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trade_id:id,exit_price:price})}).then(r=>r.json()).then(res=>{if(res.success){showToast(`Closed! P&L: ${res.realized_pnl>=0?'+':''}$${res.realized_pnl.toFixed(2)}`,'success');runMonitor()}else showToast('Error: '+res.error,'error')});
}

// ── HISTORY ────────────────────────────────────────────────────────────
async function loadHistory(){
  try{
    const r=await fetch('/api/trades');const d=await r.json();
    if(!d.trades||!d.trades.length){document.getElementById('history-wrap').innerHTML='<div class="empty"><div class="ico">📋</div><p>No trades yet.</p></div>';return}
    const sc={open:'green',closed:'blue',expired:'blue',assigned:'orange',rolled:'orange',called_away:'orange'};
    const rows=d.trades.map(t=>{const pnlHtml=t.status!=='open'?`<span class="${t.realized_pnl>=0?'pos':'neg'}">${t.realized_pnl>=0?'+':''}$${(t.realized_pnl||0).toFixed(2)}</span>`:`<span class="neu">$${(t.premium_received||0).toFixed(2)} max</span>`;const sk=t.strike?`$${t.strike}`:t.short_put_strike?`$${t.short_put_strike}/$${t.short_call_strike}`:'—';return`<tr><td style="font-weight:600">${t.id}</td><td><strong>${t.symbol}</strong></td><td>${(t.trade_type||'').toUpperCase()}</td><td>${t.entry_date||'—'}</td><td>${t.expiry||'—'}</td><td>${sk}</td><td>$${(t.premium_received||0).toFixed(2)}</td><td>${pnlHtml}</td><td><span class="badge badge-${sc[t.status]||'blue'}">${t.status}</span></td></tr>`}).join('');
    const s=d.summary;
    document.getElementById('history-wrap').innerHTML=`<div class="card"><div style="overflow-x:auto"><table><thead><tr><th>ID</th><th>Symbol</th><th>Type</th><th>Entry</th><th>Expiry</th><th>Strike</th><th>Premium</th><th>P&L</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div><div style="margin-top:14px;font-size:13px;color:var(--muted)">${s.total_trades} trades &nbsp;|&nbsp; Win rate: ${s.win_rate}% &nbsp;|&nbsp; Realized P&L: <span class="${s.total_realized_pnl>=0?'pos':'neg'}">${s.total_realized_pnl>=0?'+':''}$${(s.total_realized_pnl||0).toFixed(2)}</span></div></div>`;
  }catch(e){document.getElementById('history-wrap').innerHTML=`<div class="empty"><p>Error: ${e.message}</p></div>`}
}

// ── TOAST ──────────────────────────────────────────────────────────────
function showToast(msg,type='info'){const t=document.getElementById('toast');t.textContent=msg;t.className=`toast ${type} show`;setTimeout(()=>t.classList.remove('show'),4000)}

window.addEventListener('load',()=>setTimeout(runScan,400));
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/api/scan')
def api_scan():
    try:
        config = OllieConfig()
        config.load_portfolio()
        fetcher = OptionsDataFetcher()
        screener = OptionsScreener(fetcher, config.risk)
        wheel_mgr = WheelManager(config, fetcher)

        rddt = wheel_mgr.recommend_rddt_action(shares=200, avg_cost=None)
        rddt['symbol'] = 'RDDT'

        wheel_df = screener.screen_wheel_candidates(symbols=FULL_WATCHLIST[:20], max_stock_price=200)
        ic_syms = WATCHLIST_ETFS[:6] + ['AAPL', 'MSFT', 'AMZN', 'GOOGL', 'META', 'NVDA']
        condor_df = screener.screen_iron_condors(symbols=ic_syms)
        spread_df = screener.screen_credit_spreads(symbols=FULL_WATCHLIST[:15], spread_type='put')

        return jsonify({
            'rddt': rddt,
            'wheel_candidates': [] if wheel_df.empty else wheel_df.head(30).to_dict('records'),
            'iron_condors': [] if condor_df.empty else condor_df.head(15).to_dict('records'),
            'credit_spreads': [] if spread_df.empty else spread_df.head(15).to_dict('records'),
        })
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/log-trade', methods=['POST'])
def api_log_trade():
    data = request.json
    try:
        ledger = get_ledger()
        t = data.get('trade_type')
        sym = data.get('symbol', '').upper()
        contracts = int(data.get('contracts', 1))
        commission = float(data.get('commission', 0))
        notes = data.get('notes', '')
        trade = None
        if t == 'csp':
            trade = ledger.enter_csp(sym, float(data['strike']), data['expiry'], float(data['premium']), contracts, commission, notes)
        elif t == 'cc':
            trade = ledger.enter_covered_call(sym, float(data['strike']), data['expiry'], float(data['premium']), contracts, commission, notes)
        elif t == 'ic':
            trade = ledger.enter_iron_condor(sym, data['expiry'], float(data['short_put']), float(data['long_put']), float(data['short_call']), float(data['long_call']), float(data['credit']), contracts, commission, notes)
        elif t in ('bull_put', 'bear_call'):
            trade = ledger.enter_credit_spread(sym, data['expiry'], float(data['short_strike']), float(data['long_strike']), t, float(data['credit']), contracts, commission, notes)
        elif t == 'shares':
            trade = ledger.enter_shares(sym, int(data['shares']), float(data['cost_per_share']), notes)
        else:
            return jsonify({'success': False, 'error': f'Unknown type: {t}'})
        return jsonify({'success': True, 'trade_id': trade.id, 'premium_received': round(trade.premium_received, 2)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/monitor')
def api_monitor():
    try:
        ledger = get_ledger()
        if not ledger.open_trades():
            return jsonify({'positions': [], 'total_positions': 0})
        monitor = PositionMonitor(ledger)
        snapshots = monitor.monitor_all()
        return jsonify(monitor.summary_report(snapshots))
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/close-trade', methods=['POST'])
def api_close_trade():
    data = request.json
    try:
        ledger = get_ledger()
        trade_id, exit_price = data.get('trade_id'), float(data.get('exit_price', 0))
        trade = ledger.expire_trade(trade_id) if exit_price == 0 else ledger.close_trade(trade_id, exit_price)
        if trade:
            return jsonify({'success': True, 'trade_id': trade.id, 'realized_pnl': round(trade.realized_pnl, 2), 'status': trade.status})
        return jsonify({'success': False, 'error': f'{trade_id} not found'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/trades')
def api_trades():
    try:
        ledger = get_ledger()
        from dataclasses import asdict
        trades = sorted(ledger.trades, key=lambda t: t.entry_date, reverse=True)
        return jsonify({'trades': [asdict(t) for t in trades], 'summary': ledger.summary()})
    except Exception as e:
        return jsonify({'error': str(e)})


# ─────────────────────────────────────────────────────────────────────────────

def open_browser(port):
    import time, webbrowser
    time.sleep(1.5)
    webbrowser.open(f'http://localhost:{port}')


if __name__ == '__main__':
    PORT = 5000
    print("""
╔══════════════════════════════════════════════════════╗
║                                                      ║
║   🎯  OPTIONS OLLIE — Web Interface                  ║
║                                                      ║
║   Open your browser to: http://localhost:5000        ║
║                                                      ║
║   Press Ctrl+C to stop the server                    ║
║                                                      ║
╚══════════════════════════════════════════════════════╝
""")
    threading.Thread(target=open_browser, args=(PORT,), daemon=True).start()
    app.run(host='0.0.0.0', port=PORT, debug=False)
