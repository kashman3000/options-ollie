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

# ── Recursively convert numpy/pandas scalars to plain Python types ────────────
try:
    import numpy as _np
    _NP_GENERIC = _np.generic      # base class for ALL numpy scalars (bool_, int64, float64 …)
    _NP_NDARRAY = _np.ndarray
except ImportError:
    _NP_GENERIC = _NP_NDARRAY = type(None)

import math as _math

def _sanitise(obj):
    """Walk any dict/list structure and convert numpy scalars to Python natives."""
    if isinstance(obj, dict):
        return {k: _sanitise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitise(v) for v in obj]
    # np.generic covers np.bool_, np.int64, np.float64 etc. in all numpy versions
    if isinstance(obj, _NP_GENERIC):
        return obj.item()          # .item() always returns the plain Python equivalent
    if isinstance(obj, _NP_NDARRAY):
        return obj.tolist()
    # Guard against NaN / Inf which JSON also can't handle
    if isinstance(obj, float) and not _math.isfinite(obj):
        return None
    return obj
OUTPUT_DIR = os.path.dirname(__file__)
LEDGER_PATH = os.path.join(OUTPUT_DIR, 'data', 'trade_ledger.json')
SCAN_CACHE_PATH = os.path.join(OUTPUT_DIR, 'latest_scan.json')
HOLDINGS_PATH = os.path.join(OUTPUT_DIR, 'data', 'my_holdings.json')
CONFIG_PATH = os.path.join(OUTPUT_DIR, 'data', 'config.json')
os.makedirs(os.path.join(OUTPUT_DIR, 'data'), exist_ok=True)

# ── Holdings helpers ──────────────────────────────────────────────────────────
DEFAULT_HOLDINGS = [{"symbol": "RDDT", "shares": 200, "avg_cost": None, "exchange": "NASDAQ", "notes": ""}]

def load_holdings():
    if os.path.exists(HOLDINGS_PATH):
        try:
            with open(HOLDINGS_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return DEFAULT_HOLDINGS

def save_holdings(holdings):
    with open(HOLDINGS_PATH, 'w') as f:
        json.dump(holdings, f, indent=2)

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(cfg):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)

def get_ledger():
    return TradeLedger(LEDGER_PATH)


def get_wheel_cycle_summary(symbol: str) -> dict:
    """
    Read the trade ledger and compute wheel cycle stats for one symbol.
    Returns a dict ready to embed in the recommendation dict.
    """
    from datetime import date, datetime as _dt
    symbol = symbol.upper()
    ledger = get_ledger()

    all_trades = [t for t in ledger.trades if t.symbol == symbol]
    if not all_trades:
        return {'has_data': False, 'completed_cycles': 0, 'phase': 'NONE',
                'total_premium_this_cycle': 0, 'capital_deployed': 0,
                'annualised_yield': None, 'days_in_cycle': 0, 'open_trades': []}

    # ── Detect current phase from open trades ─────────────────────────────
    open_trades = [t for t in all_trades if t.status == 'open']
    open_csps = [t for t in open_trades if t.trade_type == 'csp']
    open_ccs  = [t for t in open_trades if t.trade_type == 'covered_call']
    open_shares = [t for t in open_trades if t.trade_type == 'long_shares']

    if open_csps:
        phase = 'CSP'
    elif open_ccs:
        phase = 'CC'
    elif open_shares:
        phase = 'SHARES'
    else:
        phase = 'READY'   # no open positions — ready to sell next CSP

    # ── Count completed wheel cycles ──────────────────────────────────────
    # A completed cycle = at least one closed/expired/called_away CC per wheel_group
    wheel_groups = set(t.wheel_group for t in all_trades if t.wheel_group)
    completed_cycles = 0
    for wg in wheel_groups:
        grp = [t for t in all_trades if t.wheel_group == wg]
        has_closed_cc = any(
            t.trade_type == 'covered_call' and t.status in ('closed', 'expired', 'called_away', 'rolled')
            for t in grp
        )
        if has_closed_cc:
            completed_cycles += 1

    # ── Premium collected in current open cycle ───────────────────────────
    # Find the earliest open trade's entry date as cycle start
    open_entry_dates = [t.entry_date for t in open_trades if t.entry_date]
    if open_entry_dates:
        cycle_start_str = min(open_entry_dates)
        try:
            cycle_start = _dt.strptime(cycle_start_str, '%Y-%m-%d').date()
            days_in_cycle = (date.today() - cycle_start).days or 1
        except Exception:
            days_in_cycle = 1
    else:
        days_in_cycle = 1

    # Premium = sum of all options premium received across all active wheel groups
    active_groups = set(t.wheel_group for t in open_trades if t.wheel_group)
    total_premium_this_cycle = sum(
        t.premium_received for t in all_trades
        if t.wheel_group in active_groups and t.is_options_trade()
    )

    # ── Capital deployed = shares × effective cost ────────────────────────
    capital_deployed = 0.0
    for t in open_shares:
        capital_deployed += t.entry_price * t.quantity
    # If in CSP phase, capital deployed = collateral required
    if not capital_deployed:
        for t in open_csps:
            capital_deployed += t.collateral_required

    # ── Annualised yield on deployed capital ──────────────────────────────
    annualised_yield = None
    if capital_deployed > 0 and days_in_cycle > 0:
        annualised_yield = round(
            (total_premium_this_cycle / capital_deployed) * (365 / days_in_cycle) * 100, 1
        )

    # ── Open CC positions with roll analysis ──────────────────────────────
    open_cc_list = []
    for t in open_ccs:
        dte = None
        if t.expiry:
            try:
                exp = _dt.strptime(t.expiry, '%Y-%m-%d').date()
                dte = (exp - date.today()).days
            except Exception:
                pass
        pct_captured = None
        if t.premium_received > 0 and dte is not None:
            # rough theta-based estimate: at 7 DTE ~70% of value decayed
            # We don't have live market price, so use DTE-based proxy
            original_dte = t.days_held() + (dte or 0)
            if original_dte > 0:
                pct_elapsed = t.days_held() / original_dte
                pct_captured = round(min(pct_elapsed * 100, 99), 0)
        open_cc_list.append({
            'trade_id': t.id,
            'strike': t.strike,
            'expiry': t.expiry,
            'dte': dte,
            'premium_received': t.premium_received,
            'pct_captured': pct_captured,
            'entry_date': t.entry_date,
        })

    # ── Open protective put positions ─────────────────────────────────────
    open_protective_puts = []
    for t in open_trades:
        if t.trade_type == 'protective_put':
            dte = None
            if t.expiry:
                try:
                    exp = _dt.strptime(t.expiry, '%Y-%m-%d').date()
                    dte = (exp - date.today()).days
                except Exception:
                    pass
            open_protective_puts.append({
                'trade_id': t.id,
                'strike': t.strike,
                'expiry': t.expiry,
                'dte': dte,
                'quantity': t.quantity,
                'entry_price': t.entry_price,
                'total_cost': abs(t.premium_received or 0),
            })

    return {
        'has_data': True,
        'phase': phase,
        'completed_cycles': completed_cycles,
        'total_premium_this_cycle': round(total_premium_this_cycle, 2),
        'capital_deployed': round(capital_deployed, 2),
        'annualised_yield': annualised_yield,
        'days_in_cycle': days_in_cycle,
        'open_trades': open_cc_list,             # open CC positions for rolling calc
        'open_protective_puts': open_protective_puts,  # existing put protection
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Options Ollie</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.26.0/plotly.min.js" crossorigin="anonymous"></script>
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
.rddt-action.protect{background:rgba(239,68,68,.08);border-color:var(--red)}
.rddt-action.hold{background:rgba(245,158,11,.07);border-color:var(--orange)}
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
/* Market Structure */
.oi-range-bar{position:relative;height:28px;background:var(--card);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin:10px 0}
.oi-range-fill{position:absolute;top:0;bottom:0;background:rgba(99,102,241,.15);border-left:2px solid rgba(99,102,241,.4);border-right:2px solid rgba(99,102,241,.4)}
.oi-range-price{position:absolute;top:0;bottom:0;width:2px;background:var(--accent);transform:translateX(-50%)}
.oi-range-label{position:absolute;top:5px;font-size:10px;color:var(--muted)}
.oi-level-row{display:flex;align-items:center;gap:8px;font-size:12px;padding:2px 0}
.oi-bar-put{height:8px;background:rgba(34,197,94,.5);border-radius:2px}
.oi-bar-call{height:8px;background:rgba(239,68,68,.5);border-radius:2px}
.oi-badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.oi-badge-support{background:rgba(34,197,94,.15);border:1px solid rgba(34,197,94,.3);color:var(--green)}
.oi-badge-resist{background:rgba(239,68,68,.15);border:1px solid rgba(239,68,68,.3);color:var(--red)}
.oi-badge-flip{background:rgba(250,204,21,.12);border:1px solid rgba(250,204,21,.3);color:#facc15}
.oi-coaching{font-size:13px;color:var(--muted);line-height:1.6;background:rgba(99,102,241,.05);border:1px solid rgba(99,102,241,.15);border-radius:8px;padding:12px 16px;margin-top:10px}
/* Next Best Action hero card */
.nba-card{border-radius:12px;padding:18px 20px;margin-bottom:16px;border:1px solid}
.nba-tag{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px}
.nba-headline{font-size:19px;font-weight:700;margin:4px 0 2px;line-height:1.3}
.nba-conf-bar{display:flex;align-items:center;gap:8px;margin:8px 0 12px}
.nba-conf-track{flex:none;width:100px;height:5px;background:rgba(255,255,255,.08);border-radius:3px;overflow:hidden}
.nba-conf-fill{height:100%;border-radius:3px}
.signal-pills{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
.signal-pill{display:inline-flex;align-items:center;gap:3px;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;cursor:default;border:1px solid}
.sp-green{background:rgba(34,197,94,.12);border-color:rgba(34,197,94,.35);color:var(--green)}
.sp-red{background:rgba(239,68,68,.12);border-color:rgba(239,68,68,.35);color:var(--red)}
.sp-orange{background:rgba(245,158,11,.12);border-color:rgba(245,158,11,.35);color:var(--orange)}
.sp-blue{background:rgba(59,130,246,.12);border-color:rgba(59,130,246,.35);color:var(--blue)}
.sp-grey{background:rgba(255,255,255,.06);border-color:var(--border);color:var(--muted)}
.nba-reasoning{font-size:13px;line-height:1.7;margin:10px 0 8px}
.nba-edu{font-size:12px;color:var(--muted);line-height:1.65;background:rgba(255,255,255,.025);border-radius:8px;padding:10px 14px;margin-top:10px;border:1px solid rgba(255,255,255,.06)}
.nba-gemini-badge{display:inline-flex;align-items:center;gap:4px;font-size:10px;color:var(--muted);margin-top:8px;opacity:.7}
/* Advisory layout — tabbed supporting analysis */
.analysis-tabs-bar{display:flex;gap:2px;border-bottom:1px solid var(--border);margin:20px 0 0;padding:0}
.analysis-tab{background:none;border:none;color:var(--muted);font-size:12px;font-weight:600;padding:8px 16px;cursor:pointer;border-bottom:2px solid transparent;transition:all .15s;letter-spacing:.2px}
.analysis-tab:hover{color:var(--text);background:rgba(255,255,255,.03)}
.analysis-tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.analysis-tab .tab-count{font-size:10px;font-weight:400;color:var(--muted);margin-left:4px}
.analysis-panel{display:none;padding:16px 0 8px;animation:fadePanel .2s}
.analysis-panel.active{display:block}
@keyframes fadePanel{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
/* Coaching annotations above tables */
.coaching-annotation{font-size:13px;line-height:1.7;color:var(--text);background:linear-gradient(135deg,rgba(99,102,241,.06),rgba(168,85,247,.04));border:1px solid rgba(99,102,241,.18);border-radius:10px;padding:12px 16px;margin-bottom:14px}
.coaching-annotation .coach-label{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.8px;color:var(--accent);margin-bottom:4px}
/* Risk narrative replaces raw scenario table */
.risk-narrative{font-size:13px;line-height:1.7;color:var(--text);background:rgba(239,68,68,.04);border:1px solid rgba(239,68,68,.15);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.risk-narrative .rn-label{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.8px;color:var(--red);margin-bottom:6px}
.risk-narrative .rn-scenario-mini{display:flex;gap:12px;flex-wrap:wrap;margin-top:10px;padding-top:10px;border-top:1px solid rgba(239,68,68,.1)}
.risk-narrative .rn-scenario-mini .rn-pill{font-size:11px;padding:3px 10px;border-radius:12px;background:rgba(239,68,68,.08);color:var(--red);font-weight:600}
.risk-narrative .rn-scenario-mini .rn-pill.safe{background:rgba(34,197,94,.08);color:var(--green)}
/* Portfolio briefing banner at top of scan results */
.briefing-panel{background:linear-gradient(135deg,rgba(99,102,241,.08),rgba(59,130,246,.06));border:1px solid rgba(99,102,241,.25);border-radius:12px;padding:18px 22px;margin-bottom:20px}
.briefing-panel .bp-label{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1px;color:var(--accent);margin-bottom:8px;display:flex;align-items:center;gap:6px}
.briefing-panel .bp-text{font-size:14px;line-height:1.75;color:var(--text)}
.briefing-panel .bp-loading{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px}
/* Ollie's Pick card */
.pick-card{background:linear-gradient(135deg,rgba(34,197,94,.07),rgba(99,102,241,.05));border:1px solid rgba(34,197,94,.3);border-radius:14px;padding:20px 22px;margin-bottom:4px}
.pick-card .pick-header{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.pick-card .pick-badge{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1px;background:var(--green);color:#000;padding:3px 9px;border-radius:20px}
.pick-card .pick-trade{font-size:18px;font-weight:700;color:var(--text);flex:1}
.pick-card .pick-meta{font-size:12px;color:var(--muted)}
.pick-body{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:700px){.pick-body{grid-template-columns:1fr}}
.pick-scores{display:flex;flex-direction:column;gap:8px}
.pick-score-row{display:flex;align-items:center;gap:10px}
.pick-score-label{font-size:11px;color:var(--muted);width:110px;flex-shrink:0}
.pick-score-bar-bg{flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden}
.pick-score-bar-fill{height:100%;border-radius:3px;transition:width .5s ease}
.pick-score-val{font-size:11px;font-weight:700;width:34px;text-align:right;flex-shrink:0}
.pick-narrative{font-size:13px;line-height:1.75;color:var(--text)}
.pick-narrative .pick-coach-label{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.8px;color:var(--green);margin-bottom:8px}
.pick-why{margin-top:12px}
.pick-why summary{font-size:11px;font-weight:700;color:var(--muted);cursor:pointer;text-transform:uppercase;letter-spacing:.6px}
.pick-why-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
@media(max-width:700px){.pick-why-grid{grid-template-columns:1fr}}
.pick-why-item{background:var(--card2);border-radius:8px;padding:10px 12px}
.pick-why-item .wi-title{font-size:11px;font-weight:700;color:var(--accent);margin-bottom:3px}
.pick-why-item .wi-body{font-size:11px;color:var(--muted);line-height:1.6}
/* Price chart panel */
.chart-section-hdr{font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;display:flex;align-items:center;gap:10px;margin:6px 0 10px}
.chart-section-hdr::after{content:'';flex:1;height:1px;background:var(--border)}
.chart-section-sub{font-size:11px;font-weight:400;text-transform:none;letter-spacing:0;color:var(--muted);opacity:.7}
.chart-wrap{border-radius:10px;overflow:hidden;background:rgba(255,255,255,.015);border:1px solid var(--border);margin-bottom:16px;min-height:60px}
.chart-loading{display:flex;align-items:center;justify-content:center;gap:8px;height:80px;color:var(--muted);font-size:13px}
/* F1 — Wheel cycle card */
.wheel-cycle-card{background:linear-gradient(135deg,rgba(99,102,241,.07),rgba(168,85,247,.06));border:1px solid rgba(99,102,241,.25);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.wheel-cycle-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.wheel-cycle-title{font-size:13px;font-weight:700;color:var(--text)}
.wheel-phase-badge{font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px}
.phase-csp{background:rgba(245,158,11,.15);color:var(--orange)}
.phase-cc{background:rgba(34,197,94,.13);color:var(--green)}
.phase-shares{background:rgba(59,130,246,.13);color:var(--blue)}
.phase-ready{background:rgba(99,102,241,.13);color:var(--accent)}
.wheel-stats{display:flex;gap:16px;flex-wrap:wrap}
.wheel-stat{text-align:center;min-width:70px}
.wheel-stat .wsl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.wheel-stat .wsv{font-size:17px;font-weight:700;margin-top:2px}
/* F2 — Income dashboard */
.income-bar-wrap{display:flex;align-items:flex-end;gap:6px;height:120px;margin:12px 0}
.income-bar-col{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;cursor:default}
.income-bar{width:100%;border-radius:4px 4px 0 0;min-height:4px;transition:height .4s;background:linear-gradient(180deg,var(--accent),var(--purple))}
.income-bar-lbl{font-size:9px;color:var(--muted);text-align:center;white-space:nowrap}
.income-bar-val{font-size:10px;font-weight:700;color:var(--text)}
.income-target-bar{background:rgba(255,255,255,.05);border-radius:6px;height:10px;overflow:hidden;margin:6px 0}
.income-target-fill{height:100%;border-radius:6px;background:linear-gradient(90deg,var(--green),var(--accent));transition:width .5s}
/* F3 — Roll calculator */
.roll-card{background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.25);border-radius:10px;padding:12px 16px;margin-bottom:12px}
.roll-card-hdr{font-size:12px;font-weight:700;color:var(--orange);margin-bottom:8px;text-transform:uppercase;letter-spacing:.4px}
.roll-rec{font-size:14px;font-weight:700;margin-bottom:4px}
.roll-detail{font-size:12px;color:var(--muted);line-height:1.6}
/* F4 — Earnings badge */
.earnings-banner{display:flex;align-items:center;gap:8px;background:rgba(239,68,68,.09);border:1px solid rgba(239,68,68,.3);border-radius:8px;padding:8px 14px;margin-bottom:12px;font-size:13px;font-weight:600}
.earnings-banner.warn{background:rgba(245,158,11,.08);border-color:rgba(245,158,11,.3);color:var(--orange)}
.earnings-banner.safe{background:rgba(34,197,94,.07);border-color:rgba(34,197,94,.25);color:var(--green)}
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
    <div id="sync-badge" style="font-size:11px;color:var(--muted);display:none">Last synced: <span id="sync-age"></span></div>
    <button class="btn btn-ghost" style="font-size:12px;padding:6px 12px" onclick="openSettings()" title="Settings — Gemini API key">⚙️</button>
    <button class="btn btn-success" id="scan-btn" onclick="runScan()">▶ Run Scan</button>
  </div>
</div>

<div class="wrap">
  <div class="tabs">
    <button class="tab active" onclick="showTab('scan')">📡 Scan &amp; Opportunities</button>
    <button class="tab" onclick="showTab('positions')">📊 My Positions</button>
    <button class="tab" onclick="showTab('income')">💰 Income</button>
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
      <!-- Holdings management bar -->
      <div class="card" style="padding:14px 18px;margin-bottom:16px">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
          <div style="font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">📂 My Holdings</div>
          <button class="btn btn-primary" style="font-size:12px;padding:6px 14px" onclick="openAddHolding()">＋ Add Stock</button>
        </div>
        <div id="holdings-chips" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px"></div>
      </div>
      <!-- Portfolio briefing (Gemini-powered) -->
      <div id="portfolio-briefing"></div>
      <!-- Per-holding analysis cards (one rddt-box per holding) -->
      <div id="holdings-cards"></div>
      <!-- Market screener sections -->
      <div class="card"><h2>🎡 Wheel Candidates — Cash-Secured Puts</h2>
        <div id="wheel-pick-card"></div>
        <div class="filter-bar" style="margin-top:12px"><input type="text" placeholder="Filter by symbol…" oninput="filterTbl('wheel-tbl',this.value)"></div>
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
      <div id="flds-entry-date" class="hidden" style="margin-bottom:14px">
        <div class="field"><label>Entry Date <span style="color:var(--muted);font-weight:400">(leave blank for today)</span></label><input type="date" id="f-entry-date"></div>
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

  <!-- INCOME TAB (F2) -->
  <div id="tab-income" class="tab-content">
    <div id="income-wrap">
      <div class="empty"><div class="ico">💰</div><p>Loading income data…</p></div>
    </div>
  </div>

  <!-- HISTORY TAB -->
  <div id="tab-history" class="tab-content">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;flex-wrap:wrap;gap:10px">
      <h2 style="font-size:17px">Trade Ledger</h2>
      <div style="display:flex;gap:8px">
        <button class="btn btn-success" onclick="showTab('log')">＋ Add Trade</button>
        <button class="btn btn-ghost" onclick="loadHistory()">↻ Refresh</button>
      </div>
    </div>
    <div id="history-wrap"><div class="empty"><div class="ico">📋</div><p>Loading…</p></div></div>
  </div>
</div>

<!-- ADD HOLDING MODAL -->
<div class="modal-overlay" id="holding-modal">
  <div class="modal" style="max-width:420px">
    <button class="modal-close" onclick="closeAddHolding()">✕</button>
    <h3>Add Stock to My Holdings</h3>
    <div class="sub-label">Works for US stocks (NASDAQ/NYSE) and ASX stocks (e.g. ANZ.AX)</div>
    <div class="fg" style="margin-bottom:12px">
      <div class="field"><label>Symbol</label><input type="text" id="h-sym" placeholder="e.g. AAPL or ANZ.AX" style="text-transform:uppercase" oninput="this.value=this.value.toUpperCase()"></div>
      <div class="field"><label>Shares</label><input type="number" id="h-shares" value="100" min="1"></div>
      <div class="field"><label>Avg Cost (optional)</label><input type="number" id="h-cost" placeholder="Leave blank = use live price" step="0.01"></div>
      <div class="field"><label>Notes (optional)</label><input type="text" id="h-notes" placeholder="e.g. Long-term hold"></div>
    </div>
    <div style="display:flex;gap:10px;margin-top:8px">
      <button class="btn btn-success" onclick="saveAddHolding()">＋ Add &amp; Scan</button>
      <button class="btn btn-ghost" onclick="closeAddHolding()">Cancel</button>
    </div>
    <div id="h-err" style="color:var(--red);font-size:13px;margin-top:8px"></div>
  </div>
</div>

<!-- SETTINGS MODAL -->
<div class="modal-overlay" id="settings-modal">
  <div class="modal" style="max-width:460px">
    <button class="modal-close" onclick="closeSettings()">✕</button>
    <h3>⚙️ Settings</h3>
    <div class="sub-label">Configure Options Ollie integrations</div>
    <div style="margin-bottom:18px">
      <div style="font-size:13px;font-weight:600;margin-bottom:8px;color:var(--text)">🤖 Gemini AI Key <span style="font-size:11px;color:var(--muted);font-weight:400">(optional — enhances coaching narratives)</span></div>
      <div class="field"><label>Google Gemini API Key</label>
        <input type="password" id="s-gemini-key" placeholder="AIza… (leave blank to use built-in coaching)">
      </div>
      <div id="s-key-status" style="font-size:12px;color:var(--muted);margin-top:6px"></div>
      <div style="font-size:12px;color:var(--muted);margin-top:6px">
        Get a free key at <strong>aistudio.google.com</strong>. When set, the Next Best Action coaching will use Gemini 2.0 Flash for richer, more personalised trade guidance.
      </div>
    </div>
    <div style="display:flex;gap:10px;margin-top:8px">
      <button class="btn btn-success" onclick="saveSettings()">💾 Save</button>
      <button class="btn btn-ghost" onclick="clearGeminiKey()" style="font-size:12px">Clear Key</button>
      <button class="btn btn-ghost" onclick="closeSettings()">Cancel</button>
    </div>
    <div id="s-err" style="color:var(--red);font-size:13px;margin-top:8px"></div>
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
    <div id="m-flds-entry-date" class="hidden" style="margin-bottom:12px">
      <div class="field"><label>Entry Date <span style="color:var(--muted);font-weight:400">(leave blank for today)</span></label><input type="date" id="m-entry-date"></div>
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

<!-- EDIT TRADE MODAL -->
<div class="modal-overlay" id="edit-trade-modal">
  <div class="modal" style="max-width:520px">
    <button class="modal-close" onclick="closeEditTrade()">✕</button>
    <h3 id="et-title">Edit Trade</h3>
    <input type="hidden" id="et-id">
    <div class="fg" style="margin-bottom:10px">
      <div class="field"><label>Symbol</label><input type="text" id="et-sym" style="text-transform:uppercase" oninput="this.value=this.value.toUpperCase()"></div>
      <div class="field"><label>Trade Type</label>
        <select id="et-type">
          <option value="csp">CSP (Cash-Secured Put)</option>
          <option value="cc">Covered Call</option>
          <option value="ic">Iron Condor</option>
          <option value="bull_put">Bull Put Spread</option>
          <option value="bear_call">Bear Call Spread</option>
          <option value="protective_put">Protective Put</option>
          <option value="shares">Long Shares</option>
        </select>
      </div>
    </div>
    <div class="fg fg3" style="margin-bottom:10px">
      <div class="field"><label>Entry Date</label><input type="date" id="et-date"></div>
      <div class="field"><label>Strike ($)</label><input type="number" id="et-strike" step="0.5" placeholder="optional"></div>
      <div class="field"><label>Expiry</label><input type="date" id="et-expiry"></div>
    </div>
    <div class="fg fg3" style="margin-bottom:10px">
      <div class="field"><label>Premium / Contract ($)</label><input type="number" id="et-premium" step="0.01"></div>
      <div class="field"><label>Contracts / Shares</label><input type="number" id="et-contracts" min="1"></div>
      <div class="field"><label>Total Premium Received ($)</label><input type="number" id="et-prem-received" step="0.01"></div>
    </div>
    <div class="fg" style="margin-bottom:10px">
      <div class="field"><label>Commission ($)</label><input type="number" id="et-commission" step="0.01" value="0"></div>
      <div class="field"><label>Status</label>
        <select id="et-status" onchange="toggleEditExitFields()">
          <option value="open">Open</option>
          <option value="closed">Closed</option>
          <option value="expired">Expired</option>
          <option value="assigned">Assigned</option>
          <option value="called_away">Called Away</option>
          <option value="rolled">Rolled</option>
        </select>
      </div>
    </div>
    <div id="et-exit-wrap" style="display:none">
      <div class="fg" style="margin-bottom:10px">
        <div class="field"><label>Exit Price ($)</label><input type="number" id="et-exit-price" step="0.01" placeholder="0 if expired worthless"></div>
        <div class="field"><label>Realized P&L ($)</label><input type="number" id="et-realized-pnl" step="0.01" placeholder="override auto-calc"></div>
      </div>
    </div>
    <div class="field" style="margin-bottom:14px"><label>Notes</label><input type="text" id="et-notes" placeholder="optional"></div>
    <div class="btn-group">
      <button class="btn btn-success" id="et-save-btn" onclick="saveEditTrade()">💾 Save Changes</button>
      <button class="btn btn-ghost" onclick="closeEditTrade()">Cancel</button>
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
  if(name==='income')loadIncome();
  if(name==='history')loadHistory();
  if(name==='positions'&&document.getElementById('mon-summary').classList.contains('hidden'))runMonitor();
}

// ── Per-holding analysis tabs ─────────────────────────────────────────
function showAnalysisTab(sym, tabName){
  const wrap=document.getElementById('atabs-'+sym);
  if(!wrap)return;
  wrap.querySelectorAll('.analysis-tab').forEach(t=>t.classList.remove('active'));
  wrap.querySelectorAll('.analysis-panel').forEach(p=>p.classList.remove('active'));
  const btn=wrap.querySelector(`.analysis-tab[data-tab="${tabName}"]`);
  const panel=wrap.querySelector(`#ap-${sym}-${tabName}`);
  if(btn)btn.classList.add('active');
  if(panel)panel.classList.add('active');
}

// ── SYNC BADGE ─────────────────────────────────────────────────────────
function updateSyncBadge(ts){
  if(!ts)return;
  const badge=document.getElementById('sync-badge');
  const ageEl=document.getElementById('sync-age');
  const diff=Math.round((Date.now()-new Date(ts).getTime())/1000);
  let label;
  if(diff<60)label=diff+'s ago';
  else if(diff<3600)label=Math.round(diff/60)+'m ago';
  else if(diff<86400)label=Math.round(diff/3600)+'h ago';
  else label=Math.round(diff/86400)+'d ago';
  ageEl.textContent=label;
  badge.style.display='block';
}

// ── SCAN ──────────────────────────────────────────────────────────────
async function loadCachedScan(){
  try{
    const r=await fetch('/api/cached');const data=await r.json();
    if(data.error)return; // no cache yet
    renderScan(data);
    updateSyncBadge(data.scan_timestamp);
    document.getElementById('scan-initial').classList.add('hidden');
    document.getElementById('scan-results').classList.remove('hidden');
    showToast('Loaded cached data — click Refresh Scan for live prices','info');
  }catch(e){}
}

async function runScan(){
  const btn=document.getElementById('scan-btn');
  btn.disabled=true;btn.innerHTML='<span class="loader"></span> Scanning…';
  document.getElementById('scan-initial').classList.add('hidden');
  document.getElementById('scan-results').classList.add('hidden');
  document.getElementById('scan-loading').classList.remove('hidden');
  try{
    const r=await fetch('/api/scan');const data=await r.json();
    if(data.error){showToast('Scan error: '+data.error,'error');document.getElementById('scan-loading').classList.add('hidden');document.getElementById('scan-initial').classList.remove('hidden');return}
    renderScan(data);
    updateSyncBadge(data.scan_timestamp);
    showToast('Scan complete!','success');
    document.getElementById('scan-loading').classList.add('hidden');
    document.getElementById('scan-results').classList.remove('hidden');
  }catch(e){showToast('Error: '+e.message,'error');document.getElementById('scan-loading').classList.add('hidden');document.getElementById('scan-initial').classList.remove('hidden')}
  finally{btn.disabled=false;btn.innerHTML='↻ Refresh Scan'}
}

function renderScan(data){
  // Render holdings chips bar
  renderHoldingsChips(data.holdings||[]);
  // Render one card per holding
  const cardsEl=document.getElementById('holdings-cards');
  if(!cardsEl)return;
  const holdings=data.holdings||[];
  if(!holdings.length){cardsEl.innerHTML='';return}
  cardsEl.innerHTML=holdings.map(h=>{
    if(h.error)return `<div class="rddt-box" style="border-color:var(--red)"><h2>⚠️ ${h.symbol||'?'}</h2><p style="color:var(--red)">${h.error}</p></div>`;
    const id='hcard-'+h.symbol.replace(/\./g,'_');
    const cardHtml=buildHoldingCardHtml(h);
    return `<div class="rddt-box" id="${id}">${cardHtml}</div>`;
  }).join('');
  const wc=data.wheel_candidates||[];
  renderWheelPick(wc);
  renderWheelTbl(wc);
  renderCondorTbl(data.iron_condors||[]);
  renderSpreadTbl(data.credit_spreads||[]);
  // Load charts for each holding (deferred so cards render first)
  (data.holdings||[]).forEach(h=>{
    if(h.symbol&&!h.error) setTimeout(()=>loadChart(h.symbol, h), 50);
  });
  // Load AI portfolio briefing (async, non-blocking)
  loadPortfolioBriefing(holdings);
}

async function loadPortfolioBriefing(holdings){
  const el=document.getElementById('portfolio-briefing');
  if(!el||!holdings||!holdings.length)return;
  el.innerHTML=`<div class="briefing-panel"><div class="bp-loading"><span class="loader"></span> Generating portfolio briefing…</div></div>`;
  try{
    const r=await fetch('/api/briefing',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({holdings})});
    const d=await r.json();
    if(d.briefing){
      el.innerHTML=`<div class="briefing-panel">
        <div class="bp-label">☀️ Ollie's Morning Briefing</div>
        <div class="bp-text">${d.briefing}</div>
      </div>`;
    } else {
      el.innerHTML='';  // No Gemini key configured — hide silently
    }
  }catch(e){el.innerHTML=''}
}

function renderHoldingsChips(holdings){
  const el=document.getElementById('holdings-chips');
  if(!el)return;
  el.innerHTML=holdings.map(h=>{
    const sym=h.symbol||'?';
    const pnl=h.unrealized_pnl||0;
    const pnlColor=pnl>=0?'var(--green)':'var(--red)';
    const pnlStr=(pnl>=0?'+':'')+Math.round(pnl).toLocaleString('en-AU');
    return `<div style="display:flex;align-items:center;gap:6px;background:var(--bg);border:1px solid var(--border);border-radius:20px;padding:4px 12px;font-size:12px">
      <strong>${sym}</strong>
      <span style="color:var(--muted)">${h.shares_held||0} shares</span>
      ${h.unrealized_pnl!=null?`<span style="color:${pnlColor};font-weight:700">${pnlStr}</span>`:''}
      <button onclick="removeHolding('${sym}')" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px;padding:0 0 0 4px" title="Remove">×</button>
    </div>`;
  }).join('');
}

// ── F1: Wheel Cycle Summary Card ──────────────────────────────────────────
function buildWheelCycleHtml(r){
  const wc=r.wheel_cycle||{};
  if(!wc.has_data)return'';
  const phase=wc.phase||'NONE';
  const phaseLabels={CSP:'🟡 CSP Phase',CC:'🟢 CC Phase',SHARES:'🔵 Shares Held',READY:'🟣 Ready for Next Cycle',NONE:'—'};
  const phaseClass={CSP:'phase-csp',CC:'phase-cc',SHARES:'phase-shares',READY:'phase-ready',NONE:''};
  const phaseLbl=phaseLabels[phase]||phase;
  const phaseCls=phaseClass[phase]||'';
  const premium=wc.total_premium_this_cycle||0;
  const capital=wc.capital_deployed||0;
  const cycles=wc.completed_cycles||0;
  const yld=wc.annualised_yield;
  const yldHtml=yld!=null?`<span class="pos">${yld}%</span>`:'<span class="neu">—</span>';
  // Open CC positions for roll calc (F3)
  const openCCs=wc.open_trades||[];
  let rollHtml=buildRollCalcHtml(openCCs, r);
  return `<div class="wheel-cycle-card">
    <div class="wheel-cycle-hdr">
      <span class="wheel-cycle-title">🔄 Wheel Cycle</span>
      <span class="wheel-phase-badge ${phaseCls}">${phaseLbl}</span>
    </div>
    <div class="wheel-stats">
      <div class="wheel-stat"><div class="wsl">Completed Cycles</div><div class="wsv">${cycles}</div></div>
      <div class="wheel-stat"><div class="wsl">Premium Collected</div><div class="wsv pos">$${premium.toFixed(0)}</div></div>
      ${capital>0?`<div class="wheel-stat"><div class="wsl">Capital Deployed</div><div class="wsv">$${capital.toLocaleString('en-AU',{maximumFractionDigits:0})}</div></div>`:''}
      ${yld!=null?`<div class="wheel-stat"><div class="wsl">Ann. Yield</div><div class="wsv">${yldHtml}</div></div>`:''}
    </div>
  </div>${rollHtml}`;
}

// ── F3: Rolling Calculator ─────────────────────────────────────────────────
function buildRollCalcHtml(openCCs, r){
  if(!openCCs||!openCCs.length)return'';
  const price=r.current_price||0;
  let html='';
  for(const pos of openCCs){
    const strike=pos.strike||0;const dte=pos.dte;const premium=pos.premium_received||0;
    const pct_captured=pos.pct_captured||0;
    let action='',detail='',cls='warn';
    const pct_otm=strike>0&&price>0?(strike-price)/price*100:null;
    if(dte!=null&&dte<=5){
      action='⚡ CLOSE NOW';detail=`Only ${dte} DTE left — option is nearly expired. Buy back for a few cents and pocket the remaining theta.`;cls='pos';
    } else if(pct_captured>=80){
      action='✅ BUY BACK (50% rule)';detail=`Estimated ${pct_captured.toFixed(0)}% of theta has decayed. Buy back early to free up capital and repeat the trade — don't wait for expiry.`;cls='pos';
    } else if(pct_otm!=null&&pct_otm<2&&dte!=null&&dte>7){
      action='⚠️ CONSIDER ROLLING';detail=`Stock is within ${Math.abs(pct_otm).toFixed(1)}% of your $${strike} strike with ${dte} DTE. Roll up-and-out to a higher strike or later expiry for a net credit before you get assigned.`;cls='warn';
    } else if(price>=strike){
      action='🚨 BLOWN THROUGH — ASSESS';detail=`Stock ($${price.toFixed(2)}) has moved above your $${strike} strike. Either roll up-and-out for a credit, or let it be called away (still profitable if strike > your cost basis).`;cls='neg';
    } else {
      const daysLeft=dte!=null?`${dte} DTE`:'?';
      action='⏳ HOLD';detail=`$${strike} strike is ${pct_otm!=null?Math.abs(pct_otm).toFixed(1)+'% OTM':'safely away'} with ${daysLeft}. Theta is still decaying — no action needed yet.`;cls='pos';
    }
    html+=`<div class="roll-card">
      <div class="roll-card-hdr">🎲 Should I Roll? — $${strike} Call exp ${pos.expiry||'?'}</div>
      <div class="roll-rec ${cls==='pos'?'pos':cls==='neg'?'neg':'warn'}">${action}</div>
      <div class="roll-detail">${detail}</div>
      ${premium?`<div style="font-size:11px;color:var(--muted);margin-top:6px">Premium received: <strong>$${premium.toFixed(0)}</strong> · Est. captured: <strong>${pct_captured.toFixed(0)}%</strong></div>`:''}
    </div>`;
  }
  return html;
}

// ── F4: Earnings Badge ─────────────────────────────────────────────────────
function buildEarningsBannerHtml(r){
  const ed=r.earnings_date;const days=r.earnings_days_away;
  if(!ed||days==null)return'';
  if(days<=14){
    return`<div class="earnings-banner"><span>⚠️ EARNINGS in ${days} days (${ed})</span><span style="font-size:11px;font-weight:400;margin-left:4px">— avoid selling new options through this date</span></div>`;
  } else if(days<=21){
    return`<div class="earnings-banner warn"><span>📅 Earnings in ${days} days (${ed})</span><span style="font-size:11px;font-weight:400;margin-left:4px">— select expiry before earnings date</span></div>`;
  } else {
    return`<div class="earnings-banner safe"><span>✅ Next earnings: ${ed} (${days}d away)</span><span style="font-size:11px;font-weight:400;margin-left:4px">— safe to sell options before that date</span></div>`;
  }
}

// ── F2: Income Dashboard ───────────────────────────────────────────────────
let _incomeFetched=false;
async function loadIncome(){
  if(_incomeFetched)return;
  const wrap=document.getElementById('income-wrap');
  if(!wrap)return;
  wrap.innerHTML='<div class="empty"><div class="ico">⏳</div><p>Loading income data…</p></div>';
  try{
    const res=await fetch('/api/income');
    const data=await res.json();
    if(data.error){wrap.innerHTML=`<div class="empty"><p>Error: ${data.error}</p></div>`;return}
    renderIncome(data);
    _incomeFetched=true;
  }catch(e){wrap.innerHTML=`<div class="empty"><p>Error: ${e.message}</p></div>`}
}

function renderIncome(d){
  const wrap=document.getElementById('income-wrap');
  if(!wrap)return;
  const monthly=d.monthly||[];
  const maxPremium=Math.max(...monthly.map(m=>m.premium||0),1);
  const s=d.summary||{};
  const tickers=d.best_tickers||[];

  // Bar chart
  const barsHtml=monthly.map(m=>{
    const h=Math.max(Math.round((m.premium/maxPremium)*100),2);
    const pct=maxPremium>0?Math.round(m.premium/maxPremium*100):0;
    return`<div class="income-bar-col" title="${m.label}: $${m.premium.toFixed(0)}">
      <div class="income-bar-val" style="font-size:10px">$${m.premium>0?m.premium.toFixed(0):'—'}</div>
      <div class="income-bar" style="height:${h}px"></div>
      <div class="income-bar-lbl">${m.label}</div>
    </div>`;
  }).join('');

  // Target progress
  const cur=d.current_month_income||0;const tgt=d.target_per_month||500;const tPct=d.target_pct||0;
  const yld=d.annualised_yield;
  const tickerRows=tickers.map(t=>`<tr><td><strong>${t.symbol}</strong></td><td class="pos">$${t.premium.toFixed(0)}</td></tr>`).join('');

  wrap.innerHTML=`
  <div class="summary-strip">
    <div class="sum-pill"><div class="val pos">$${(s.total_premium_collected||0).toFixed(0)}</div><div class="lbl">Total Premium</div></div>
    <div class="sum-pill"><div class="val">${s.win_rate||0}%</div><div class="lbl">Win Rate</div></div>
    <div class="sum-pill"><div class="val">${s.closed_trades||0}</div><div class="lbl">Closed Trades</div></div>
    ${yld!=null?`<div class="sum-pill"><div class="val pos">${yld}%</div><div class="lbl">Est. Ann. Yield</div></div>`:''}
  </div>
  <div class="card">
    <h2>📊 Monthly Premium Income (Last 6 Months)</h2>
    <div class="income-bar-wrap">${barsHtml}</div>
    <div style="margin-top:10px;font-size:13px;font-weight:600">This month: <span class="pos">$${cur.toFixed(0)}</span> of <span style="color:var(--muted)">$${tgt.toFixed(0)}</span> target</div>
    <div class="income-target-bar"><div class="income-target-fill" style="width:${tPct}%"></div></div>
    <div style="font-size:11px;color:var(--muted)">${tPct}% of monthly target achieved</div>
  </div>
  ${tickers.length?`<div class="card"><h2>🏆 Best Tickers by Income</h2><div class="tbl-wrap"><table><thead><tr><th>Symbol</th><th>Total Premium</th></tr></thead><tbody>${tickerRows}</tbody></table></div></div>`:''}
  <div class="card"><h2>📈 P&L Summary</h2>
    <div class="stats-row">
      <div class="stat"><div class="lbl">Realized P&L</div><div class="val ${(s.total_realized_pnl||0)>=0?'pos':'neg'}">${(s.total_realized_pnl||0)>=0?'+':''}$${Math.abs(s.total_realized_pnl||0).toFixed(0)}</div></div>
      <div class="stat"><div class="lbl">Open Positions</div><div class="val">${s.open_positions||0}</div></div>
      <div class="stat"><div class="lbl">Collateral Deployed</div><div class="val">$${(s.total_collateral_deployed||0).toLocaleString('en-AU',{maximumFractionDigits:0})}</div></div>
      <div class="stat"><div class="lbl">Premium at Risk</div><div class="val warn">$${(s.open_premium_at_risk||0).toFixed(0)}</div></div>
    </div>
    <div style="font-size:12px;color:var(--muted);margin-top:10px">Income data is from your logged trades. Log trades in the "Log a Trade" tab to populate this dashboard.</div>
  </div>`;
}

// ── Next Best Action card ─────────────────────────────────────────────────
function buildNbaCardHtml(nba, r){
  if(!nba||!nba.headline)return'';
  const color=nba.color||'blue';
  const bgMap={green:'rgba(34,197,94,.07)',red:'rgba(239,68,68,.07)',orange:'rgba(245,158,11,.06)',blue:'rgba(59,130,246,.06)',grey:'rgba(255,255,255,.03)'};
  const borderMap={green:'rgba(34,197,94,.35)',red:'rgba(239,68,68,.35)',orange:'rgba(245,158,11,.35)',blue:'rgba(59,130,246,.35)',grey:'var(--border)'};
  const textMap={green:'var(--green)',red:'var(--red)',orange:'var(--orange)',blue:'var(--blue)',grey:'var(--muted)'};
  const confColor=nba.confidence>=75?'var(--green)':nba.confidence>=55?'var(--orange)':'var(--red)';
  const confLabel=nba.confidence>=80?'HIGH':nba.confidence>=60?'MEDIUM':'LOW';
  const signalPills=(nba.signals||[]).map(s=>`<span class="signal-pill sp-${s.color||'blue'}" title="${(s.tooltip||'').replace(/'/g,'&#39;')}">${s.label}: <strong>${s.value}</strong></span>`).join('');
  // Log button for specific trade
  let tradeBtn='';
  const t=nba.specific_trade||{};
  if(t.type==='covered_call'&&t.strike){
    const prefill=JSON.stringify({symbol:r.symbol,strike:t.strike,expiry:t.expiry,mid_price:t.mid_price||0,contracts:r.contracts_available||1});
    tradeBtn=`<button class="log-btn" style="margin-top:12px;padding:5px 14px;font-size:12px" onclick='openModal("cc",${prefill})'>📋 Log This Trade →</button>`;
  } else if(t.type==='protective_put'&&t.strike){
    const prefill=JSON.stringify({symbol:r.symbol,strike:t.strike,expiry:t.expiry,mid_price:t.mid_price||0,contracts:r.contracts_available||1});
    tradeBtn=`<button class="log-btn" style="margin-top:12px;padding:5px 14px;font-size:12px" onclick='openModal("protective_put",${prefill})'>📋 Log This Trade →</button>`;
  } else if(t.type==='collar'&&t.cc_strike){
    const prefill=JSON.stringify({symbol:r.symbol,cc_strike:t.cc_strike,put_strike:t.put_strike,expiry:t.expiry,cc_premium:t.cc_premium||0,put_cost:t.put_cost||0,contracts:r.contracts_available||1});
    tradeBtn=`<button class="log-btn" style="margin-top:12px;padding:5px 14px;font-size:12px" onclick='openModal("collar",${prefill})'>📋 Log This Trade →</button>`;
  }
  const eduHtml=nba.education?`<div class="nba-edu">${nba.education}</div>`:'';
  return `<div class="nba-card" style="background:${bgMap[color]};border-color:${borderMap[color]}">
  <div class="nba-tag" style="color:${textMap[color]}">🎯 NEXT BEST ACTION</div>
  <div class="nba-headline" style="color:${textMap[color]}">${nba.icon||'•'} ${nba.headline}</div>
  <div class="nba-conf-bar">
    <div class="nba-conf-track"><div class="nba-conf-fill" style="width:${nba.confidence}%;background:${confColor}"></div></div>
    <span style="font-size:11px;font-weight:700;color:${confColor}">${confLabel} CONFIDENCE (${nba.confidence}%)</span>
  </div>
  <div class="signal-pills">${signalPills}</div>
  <div class="nba-reasoning">${nba.reasoning||''}</div>
  ${eduHtml}
  ${tradeBtn}
</div>`;
}

// Build the inner HTML for a single holding card — advisory layout
// NBA hero at top, chart below, supporting analysis in collapsible tabs
function buildHoldingCardHtml(r){
  const sym=r.symbol||'';
  const isAsx=r.is_asx||false;
  const ivC=(r.iv_rank||0)>40?'pos':'warn';
  const ra=r.risk_analysis||{};
  const nba=r.next_best_action||{};
  const nbaHtml=buildNbaCardHtml(nba, r);
  const wheelHtml=buildWheelCycleHtml(r);   // F1
  const earningsBannerHtml=buildEarningsBannerHtml(r);  // F4
  const actionClass=r.action&&r.action.startsWith('BUY PROTECTION')?'protect':r.action&&r.action.startsWith('HOLD')?'hold':'';
  const recHtml=r.action?`<div class="rddt-action ${actionClass}" style="margin-top:0"><strong>Detail:</strong> ${r.action}<br><span style="color:var(--muted);font-size:13px">${r.reasoning||''}</span></div>`:'';
  const notesHtml=(r.risk_notes||[]).map(n=>`<div class="rddt-note">${n}</div>`).join('');

  // ── Build sub-sections (same data, just reorganised) ──────────────────
  let ccHtml='';
  const ccCoaching=nba.cc_coaching||'';
  if((r.top_covered_calls||[]).length){
    const ivLabel=(r.top_covered_calls[0].iv_rank_label)||'';
    const ivTier=(r.top_covered_calls[0].iv_rank_tier)||'';
    const ivTierColor={'Low':'var(--muted)','Below Avg':'var(--orange)','Average':'var(--blue)','Elevated':'var(--green)','High':'var(--green)'}[ivTier]||'var(--muted)';
    const rows=r.top_covered_calls.map(c=>{
      const popVal=c.pop||0;
      const ptVal=c.prob_touch||0;
      const popColor=popVal>=75?'pos':popVal>=60?'warn':'neg';
      const ptColor=ptVal<=40?'pos':ptVal<=60?'warn':'neg';
      return `<tr>
        <td><strong>$${c.strike}</strong></td><td>${c.expiry}</td><td>${c.dte}d</td>
        <td class="pos">$${(c.mid_price||0).toFixed(2)}</td>
        <td class="pos">$${(c.total_premium||0).toFixed(0)}</td>
        <td>${(c.prob_otm||0).toFixed(0)}%</td>
        <td class="${popColor}" title="Prob of Profit"><strong>${popVal.toFixed(0)}%</strong></td>
        <td class="${ptColor}" title="Prob Touch">${ptVal.toFixed(0)}%</td>
        <td>${(c.upside_to_strike_pct||0).toFixed(1)}%</td>
        <td>${(c.annualized_if_called||0).toFixed(1)}%</td>
        <td><button class="log-btn" onclick='openModal("cc",${JSON.stringify({symbol:sym,strike:c.strike,expiry:c.expiry,mid_price:c.mid_price,contracts:r.contracts_available||1})})'>Log</button></td>
      </tr>`;
    }).join('');
    const coachBlock=ccCoaching?`<div class="coaching-annotation"><div class="coach-label">Ollie's take</div>${ccCoaching}</div>`:'';
    ccHtml=`${coachBlock}<div style="font-size:12px;color:var(--muted);margin-bottom:6px">IV: <span style="color:${ivTierColor};font-weight:600">${ivTier}</span> (${r.iv_rank||'—'}%) — ${ivLabel}</div>
    <div class="tbl-wrap"><table><thead><tr>
      <th>Strike</th><th>Expiry</th><th>DTE</th><th>Mid</th><th>Total $</th><th>Prob OTM</th><th>POP ↑</th><th>P.Touch ↓</th><th>Upside</th><th>Ann Ret</th><th></th>
    </tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  // ── Risk narrative (replaces raw scenario table with coached version) ──
  let riskHtml='';
  const riskNarrative=nba.risk_narrative||'';
  if((ra.scenarios||[]).length){
    const pills=ra.scenarios.map(s=>{
      const cls=s.below_cost_basis?'rn-pill':'rn-pill safe';
      return `<span class="${cls}">-${s.drop_pct}% → $${s.target_price} (${s.below_cost_basis?'-$'+Math.abs(s.dollar_loss_from_now||0).toLocaleString('en-AU',{maximumFractionDigits:0}):'+$'+(s.pnl_vs_cost_basis||0).toLocaleString('en-AU',{maximumFractionDigits:0})+' vs basis'})</span>`;
    }).join('');
    const narrativeBlock=riskNarrative
      ?`<div class="risk-narrative"><div class="rn-label">Risk Assessment</div>${riskNarrative}<div class="rn-scenario-mini">${pills}</div></div>`
      :`<div class="risk-narrative"><div class="rn-label">Downside Scenarios</div><div class="rn-scenario-mini">${pills}</div></div>`;
    // Keep full table in a collapsed detail for power users
    const fullRows=ra.scenarios.map(s=>`<tr class="${s.below_cost_basis?'scenario-below':''}">
      <td>-${s.drop_pct}%</td><td>$${s.target_price}</td>
      <td class="neg">-$${Math.abs(s.dollar_loss_from_now||0).toLocaleString('en-AU',{maximumFractionDigits:0})}</td>
      <td class="${s.pnl_vs_cost_basis>=0?'pos':'neg'}">${s.pnl_vs_cost_basis>=0?'+':''}$${(s.pnl_vs_cost_basis||0).toLocaleString('en-AU',{maximumFractionDigits:0})}</td>
      <td class="${s.pct_of_capital>=0?'pos':'neg'}">${s.pct_of_capital>=0?'+':''}${s.pct_of_capital}%</td>
      ${s.below_cost_basis?'<td><span class="badge badge-red" style="font-size:10px">Below Basis</span></td>':'<td></td>'}
    </tr>`).join('');
    riskHtml=`${narrativeBlock}<details style="margin-top:6px"><summary style="font-size:11px;color:var(--muted);cursor:pointer">▸ Full scenario table</summary>
    <div class="tbl-wrap" style="margin-top:8px"><table><thead><tr><th>Drop</th><th>Price</th><th>Loss vs Now</th><th>vs Cost Basis</th><th>% Capital</th><th></th></tr></thead><tbody>${fullRows}</tbody></table></div></details>`;
  }

  // ── Protection & collars (combined into one tab) ──────────────────────
  let protCollarHtml='';
  if(!isAsx){
    if((r.protective_strategies||[]).length){
      const rows=r.protective_strategies.map(p=>`<tr>
        <td>-${p.floor_pct}%</td><td>$${p.strike}</td><td>${p.expiry}</td><td>${p.dte}d</td>
        <td>$${(p.mid_price||0).toFixed(2)}</td>
        <td>$${(p.total_cost||0).toLocaleString('en-AU',{maximumFractionDigits:0})}</td>
        <td>${p.cost_pct_of_position||p.annualised_cost_pct}%</td>
        <td style="font-size:12px;color:var(--muted)">${p.verdict||''}</td>
        <td><button class="log-btn" onclick='openModal("protective_put",${JSON.stringify({symbol:sym,strike:p.strike,expiry:p.expiry,mid_price:p.mid_price,contracts:r.contracts_available||1})})'>Log</button></td>
      </tr>`).join('');
      protCollarHtml+=`<h4 style="font-size:13px;margin:0 0 8px;color:var(--muted)">🛡 Protective Puts</h4>
      <div class="tbl-wrap"><table><thead><tr><th>Floor</th><th>Strike</th><th>Expiry</th><th>DTE</th><th>Mid</th><th>Total</th><th>Cost %</th><th>Verdict</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }
    if((r.collar_strategies||[]).length){
      const rows=r.collar_strategies.map(c=>`<tr>
        <td style="font-size:12px">${c.structure||''}</td><td>${c.expiry}</td><td>${c.dte}d</td>
        <td class="pos">$${(c.cc_premium||0).toFixed(2)}</td>
        <td class="neg">$${(c.put_cost||0).toFixed(2)}</td>
        <td><span class="${c.net_credit>=0?'collar-credit':'collar-debit'}">${c.net_credit>=0?'Credit':'Debit'} $${Math.abs(c.net_credit||0).toFixed(0)}</span></td>
        <td style="font-size:12px;color:var(--muted)">${c.verdict||''}</td>
        <td><button class="log-btn" onclick='openModal("collar",${JSON.stringify({symbol:sym,cc_strike:c.cc_strike,put_strike:c.put_strike,expiry:c.expiry,cc_premium:c.cc_premium,put_cost:c.put_cost,contracts:r.contracts_available||1})})'>Log</button></td>
      </tr>`).join('');
      protCollarHtml+=`<h4 style="font-size:13px;margin:16px 0 8px;color:var(--muted)">🔗 Collar Strategies</h4>
      <div class="tbl-wrap"><table><thead><tr><th>Structure</th><th>Expiry</th><th>DTE</th><th>CC Prem</th><th>Put Cost</th><th>Net</th><th>Verdict</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }
  }

  // ── Market structure ──────────────────────────────────────────────────
  let mktHtml='';
  if(!isAsx){
    const ms=r.market_structure||{};
    if(ms.coaching){
      const pw=ms.put_wall,cw=ms.call_wall,gf=ms.gamma_flip,price=r.current_price||0;
      let badgesHtml='';
      if(pw){const pct=Math.abs(pw.pct_from_price||0),k=(pw.oi/1000).toFixed(1);badgesHtml+=`<span class="oi-badge oi-badge-support">🛡 Put Wall $${pw.strike.toFixed(0)} (${pct}% below · ${k}K OI)</span> `;}
      if(cw){const pct=Math.abs(cw.pct_from_price||0),k=(cw.oi/1000).toFixed(1);badgesHtml+=`<span class="oi-badge oi-badge-resist">🧱 Call Wall $${cw.strike.toFixed(0)} (${pct}% above · ${k}K OI)</span> `;}
      if(gf){const flipPct=(ms.gamma_flip_pct_from_price||0),dir=ms.price_relative_to_flip==='above'?'above':'below',gexLabel=ms.gex_positive?'Pos GEX — dampened':'Neg GEX — amplified';badgesHtml+=`<span class="oi-badge oi-badge-flip">⚡ Gamma Flip $${gf.toFixed(0)} (price ${dir} · ${gexLabel})</span>`;}
      let rangeBarHtml='';
      if(pw&&cw&&price>0){const lo=Math.min(pw.strike,cw.strike,price)*0.9,hi=Math.max(pw.strike,cw.strike,price)*1.1,span=hi-lo,putPct=((pw.strike-lo)/span*100).toFixed(1),callPct=((cw.strike-lo)/span*100).toFixed(1),pricePct=((price-lo)/span*100).toFixed(1),fillWidth=(callPct-putPct).toFixed(1);rangeBarHtml=`<div style="margin:12px 0 4px;font-size:11px;color:var(--muted)">OI RANGE VISUALISER</div><div class="oi-range-bar"><div class="oi-range-fill" style="left:${putPct}%;width:${fillWidth}%"></div><div class="oi-range-price" style="left:${pricePct}%"></div><span class="oi-range-label" style="left:${putPct}%">$${pw.strike.toFixed(0)}</span><span class="oi-range-label" style="left:${Math.min(parseFloat(callPct),85)}%">$${cw.strike.toFixed(0)}</span></div><div style="font-size:11px;color:var(--muted);display:flex;gap:16px;margin-bottom:8px"><span>▐ filled = OI zone</span><span style="color:var(--accent)">│ = $${price}</span></div>`;}
      let oiTableHtml='';
      const topPuts=ms.top_put_strikes||[],topCalls=ms.top_call_strikes||[];
      if(topPuts.length||topCalls.length){const maxP=Math.max(...topPuts.map(p=>p.put_oi||0),1),maxC=Math.max(...topCalls.map(c=>c.call_oi||0),1);const putRows=topPuts.map(p=>`<tr><td>$${p.strike.toFixed(0)}</td><td>${(p.put_oi/1000).toFixed(1)}K</td><td><div class="oi-bar-put" style="width:${Math.round(p.put_oi/maxP*120)}px"></div></td><td style="font-size:11px;color:var(--muted)">${p.strike<=price?'Support':'—'}</td></tr>`).join('');const callRows=topCalls.map(c=>`<tr><td>$${c.strike.toFixed(0)}</td><td>${(c.call_oi/1000).toFixed(1)}K</td><td><div class="oi-bar-call" style="width:${Math.round(c.call_oi/maxC*120)}px"></div></td><td style="font-size:11px;color:var(--muted)">${c.strike>=price?'Resist.':'—'}</td></tr>`).join('');oiTableHtml=`<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:12px"><div><div style="font-size:11px;font-weight:700;color:var(--green);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">🛡 Top Put OI</div><table style="width:100%"><thead><tr><th>Strike</th><th>OI</th><th>Strength</th><th></th></tr></thead><tbody>${putRows}</tbody></table></div><div><div style="font-size:11px;font-weight:700;color:var(--red);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">🧱 Top Call OI</div><table style="width:100%"><thead><tr><th>Strike</th><th>OI</th><th>Strength</th><th></th></tr></thead><tbody>${callRows}</tbody></table></div></div>`;}
      const dataNoteHtml=ms.data_note?`<div style="font-size:11px;color:var(--muted);margin-top:10px;opacity:.7">ℹ️ ${ms.data_note}</div>`:'';
      mktHtml=`<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">${badgesHtml}</div>${rangeBarHtml}<div class="oi-coaching">${ms.coaching}</div>${oiTableHtml}${dataNoteHtml}`;
    }
  }

  // ── Roadmap ───────────────────────────────────────────────────────────
  let roadmapHtml='';
  if(!isAsx){
    const cbr=r.cost_basis_roadmap||{};
    if(cbr.targets&&cbr.targets.length){
      const note=cbr.strategy_note||'';const monthlyEst=cbr.monthly_cc_income_estimate||0;
      const rows=cbr.targets.map(t=>`<tr>
        <td>Reduce basis by ${t.target_pct}%</td>
        <td>$${(t.target_basis||0).toFixed(2)}</td>
        <td class="neg">-$${(t.reduction_needed||0).toLocaleString('en-AU',{maximumFractionDigits:0})}</td>
        <td class="pos">${t.months_at_current_cc===null?'N/A':t.months_at_current_cc+' months'}</td>
      </tr>`).join('');
      roadmapHtml=`<div style="font-size:13px;color:var(--muted);margin-bottom:8px">Est. monthly CC income: <strong style="color:var(--text)">$${monthlyEst.toLocaleString('en-AU',{maximumFractionDigits:0})}</strong> — ${note}</div>
      <div class="tbl-wrap"><table><thead><tr><th>Target</th><th>New Basis</th><th>Income Needed</th><th>Time</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }
  }

  // ── Assemble: determine which analysis tab to open by default ────────
  const pnl=r.unrealized_pnl||0;const pnlColor=pnl>=0?'pos':'neg';
  const safeId=sym.replace(/\./g,'_');
  const chartSubLabel=isAsx?'price history':'price history · IV cone · OI walls';
  const nbaType=nba.action_type||'';
  // Default tab: match what the NBA recommends
  const defaultTab=(nbaType==='BUY_PROTECTION'||nbaType==='COLLAR')?'risk':
                   (nbaType==='HOLD_WAIT')?'structure':'opportunities';

  // Count items for tab badges
  const ccCount=(r.top_covered_calls||[]).length;
  const protCount=(r.protective_strategies||[]).length+(r.collar_strategies||[]).length;

  return `<h2>${sym}${isAsx?' <span style="font-size:11px;color:var(--muted);font-weight:400">ASX</span>':''}</h2>
  <div class="stats-row" style="margin-bottom:14px">
    <div class="stat"><div class="lbl">Price</div><div class="val">$${r.current_price||'—'}</div></div>
    <div class="stat"><div class="lbl">Shares</div><div class="val">${r.shares_held||0}</div></div>
    ${!isAsx?`<div class="stat"><div class="lbl">IV Rank</div><div class="val ${ivC}">${r.iv_rank||'—'}%</div></div>`:''}
    <div class="stat"><div class="lbl">Value</div><div class="val">$${(ra.position_value||0).toLocaleString('en-AU',{maximumFractionDigits:0})}</div></div>
    <div class="stat"><div class="lbl">P&L</div><div class="val ${pnlColor}">${pnl>=0?'+':''}$${Math.abs(pnl).toLocaleString('en-AU',{maximumFractionDigits:0})}</div></div>
    ${r.cost_basis?`<div class="stat"><div class="lbl">Avg Cost</div><div class="val">$${r.cost_basis}</div></div>`:''}
  </div>
  ${earningsBannerHtml}
  ${nbaHtml}
  ${wheelHtml}
  ${notesHtml}
  <div class="chart-section-hdr">📊 Market Structure <span class="chart-section-sub">${chartSubLabel}</span></div>
  <div class="chart-wrap" id="chart-${safeId}" style="height:390px"></div>
  <div id="atabs-${safeId}">
    <div class="analysis-tabs-bar">
      <button class="analysis-tab ${defaultTab==='opportunities'?'active':''}" data-tab="opportunities" onclick="showAnalysisTab('${safeId}','opportunities')">📈 Opportunities${ccCount?` <span class="tab-count">(${ccCount})</span>`:''}</button>
      <button class="analysis-tab ${defaultTab==='risk'?'active':''}" data-tab="risk" onclick="showAnalysisTab('${safeId}','risk')">📉 Risk${protCount?` <span class="tab-count">(${protCount})</span>`:''}</button>
      <button class="analysis-tab ${defaultTab==='structure'?'active':''}" data-tab="structure" onclick="showAnalysisTab('${safeId}','structure')">📐 Structure</button>
      ${roadmapHtml?`<button class="analysis-tab" data-tab="roadmap" onclick="showAnalysisTab('${safeId}','roadmap')">🗺️ Roadmap</button>`:''}
    </div>
    <div id="ap-${safeId}-opportunities" class="analysis-panel ${defaultTab==='opportunities'?'active':''}">${ccHtml||'<div style="color:var(--muted);font-size:13px;padding:10px 0">No covered call opportunities at current IV levels.</div>'}</div>
    <div id="ap-${safeId}-risk" class="analysis-panel ${defaultTab==='risk'?'active':''}">${riskHtml}${protCollarHtml}</div>
    <div id="ap-${safeId}-structure" class="analysis-panel ${defaultTab==='structure'?'active':''}">${mktHtml||'<div style="color:var(--muted);font-size:13px;padding:10px 0">Market structure data loads with the chart above.</div>'}</div>
    ${roadmapHtml?`<div id="ap-${safeId}-roadmap" class="analysis-panel">${roadmapHtml}</div>`:''}
  </div>
  <details style="margin-top:10px"><summary style="font-size:12px;color:var(--muted);cursor:pointer;user-select:none">▸ Show engine reasoning</summary>${recHtml}</details>`;
}

// ── HOLDINGS MANAGEMENT ─────────────────────────────────────────────────────
function openAddHolding(){document.getElementById('holding-modal').classList.add('open');document.getElementById('h-sym').focus();}
function closeAddHolding(){document.getElementById('holding-modal').classList.remove('open');document.getElementById('h-err').textContent='';}

async function saveAddHolding(){
  const sym=document.getElementById('h-sym').value.trim().toUpperCase();
  const shares=parseInt(document.getElementById('h-shares').value)||100;
  const cost=parseFloat(document.getElementById('h-cost').value)||null;
  const notes=document.getElementById('h-notes').value.trim();
  if(!sym){document.getElementById('h-err').textContent='Please enter a symbol';return}
  const r=await fetch('/api/holdings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym,shares,avg_cost:cost,notes})});
  const data=await r.json();
  if(!data.success){document.getElementById('h-err').textContent=data.error||'Error';return}
  closeAddHolding();
  showToast(`${sym} added — running scan…`,'success');
  runScan();
}

async function removeHolding(sym){
  if(!confirm(`Remove ${sym} from your holdings?`))return;
  await fetch('/api/holdings/'+sym,{method:'DELETE'});
  showToast(`${sym} removed`,'info');
  runScan();
}

// ── SETTINGS ─────────────────────────────────────────────────────────────
async function openSettings(){
  document.getElementById('settings-modal').classList.add('open');
  document.getElementById('s-err').textContent='';
  // Load current key status
  try{
    const r=await fetch('/api/config');const d=await r.json();
    if(d.gemini_key_set){
      document.getElementById('s-key-status').textContent=`✅ Key saved (${d.gemini_key_masked}) — re-enter to update`;
      document.getElementById('s-gemini-key').placeholder='Enter new key to update, or leave blank';
    } else {
      document.getElementById('s-key-status').textContent='No key saved yet — enter key below to enable Gemini coaching';
    }
  }catch(e){}
}
function closeSettings(){document.getElementById('settings-modal').classList.remove('open')}
async function saveSettings(){
  const key=document.getElementById('s-gemini-key').value.trim();
  const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({gemini_key:key})});
  const d=await r.json();
  if(d.success){
    closeSettings();
    showToast(key?'✅ Gemini key saved — next scan will use AI coaching':'Key cleared','success');
  } else {
    document.getElementById('s-err').textContent='Failed to save';
  }
}
async function clearGeminiKey(){
  if(!confirm('Clear the saved Gemini API key?'))return;
  await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({gemini_key:''})});
  document.getElementById('s-gemini-key').value='';
  document.getElementById('s-key-status').textContent='Key cleared';
  showToast('Gemini key cleared','info');
}

// ── PRICE CHART (candlestick + IV cone + OI walls) ──────────────────────────

async function loadChart(sym, holdingData) {
  const safeId = sym.replace(/\./g,'_');
  const el = document.getElementById('chart-' + safeId);
  if (!el) return;
  el.innerHTML = '<div class="chart-loading"><span class="loader"></span> Loading wall map…</div>';
  try {
    const r = await fetch('/api/chart/' + encodeURIComponent(sym));
    const d = await r.json();
    if (d.error) { el.innerHTML = `<div class="chart-loading" style="color:var(--red)">${d.error}</div>`; return; }
    renderChart(el, d, holdingData || {});
  } catch(e) {
    el.innerHTML = `<div class="chart-loading" style="color:var(--red)">Chart unavailable: ${e.message}</div>`;
  }
}

function renderChart(el, d, r) {
  // r = holding data from scan (contains NBA recommendation, etc.)
  if (!window.Plotly) { el.innerHTML = '<div class="chart-loading">Plotly not loaded</div>'; return; }
  el.innerHTML = '';  // clear loading spinner before Plotly renders

  const DARK = {
    paper: 'transparent', plot: 'rgba(255,255,255,0.01)',
    grid: 'rgba(255,255,255,0.04)', line: 'rgba(255,255,255,0.07)',
    text: '#8b95a5', tick: '#8b95a5',
  };

  const traces = [];
  const shapes = [];
  const annotations = [];
  const price = d.current_price || 0;
  const lastDate = d.last_candle_date || (d.candles.length ? d.candles[d.candles.length-1].date : null);

  // ── 1. Candlestick (5-day anchor to current price) ────────────────────────
  if (d.candles && d.candles.length) {
    traces.push({
      type: 'candlestick',
      x:     d.candles.map(c => c.date),
      open:  d.candles.map(c => c.open),
      high:  d.candles.map(c => c.high),
      low:   d.candles.map(c => c.low),
      close: d.candles.map(c => c.close),
      name: d.symbol,
      increasing: { line: { color: '#22c55e' }, fillcolor: 'rgba(34,197,94,0.65)' },
      decreasing: { line: { color: '#ef4444' }, fillcolor: 'rgba(239,68,68,0.65)' },
      whiskerwidth: 0.5,
      yaxis: 'y', xaxis: 'x',
      showlegend: false,
    });
  }

  // ── 2. IV Probability Cone ────────────────────────────────────────────────
  const cp = (!d.is_asx && d.cone_points) ? d.cone_points : [];
  if (cp.length && lastDate) {
    const coneX  = [lastDate, ...cp.map(p => p.date)];
    const upper2 = [price,   ...cp.map(p => p.upper_2)];
    const lower2 = [price,   ...cp.map(p => p.lower_2)];
    const upper1 = [price,   ...cp.map(p => p.upper_1)];
    const lower1 = [price,   ...cp.map(p => p.lower_1)];

    // ±2σ fill
    traces.push({
      type: 'scatter',
      x: [...coneX, ...coneX.slice().reverse()],
      y: [...upper2, ...lower2.slice().reverse()],
      fill: 'toself', fillcolor: 'rgba(99,102,241,0.05)',
      line: { width: 0 }, hoverinfo: 'skip',
      name: '±2σ (95%)', legendrank: 12,
      yaxis: 'y', xaxis: 'x',
    });
    // ±1σ fill
    traces.push({
      type: 'scatter',
      x: [...coneX, ...coneX.slice().reverse()],
      y: [...upper1, ...lower1.slice().reverse()],
      fill: 'toself', fillcolor: 'rgba(99,102,241,0.12)',
      line: { width: 0 }, hoverinfo: 'skip',
      name: '±1σ (68%)', legendrank: 11,
      yaxis: 'y', xaxis: 'x',
    });
    // ±1σ boundary lines
    traces.push({ type:'scatter', x:coneX, y:upper1, mode:'lines',
      line:{ color:'rgba(99,102,241,0.5)', width:1, dash:'dot' },
      name:'+1σ', showlegend:false, hoverinfo:'skip', yaxis:'y', xaxis:'x' });
    traces.push({ type:'scatter', x:coneX, y:lower1, mode:'lines',
      line:{ color:'rgba(99,102,241,0.5)', width:1, dash:'dot' },
      name:'-1σ', showlegend:false, hoverinfo:'skip', yaxis:'y', xaxis:'x' });

    // Dollar-range labels at each cone expiry (below upper_1 line)
    cp.forEach(p => {
      const range = (p.upper_1 - p.lower_1);
      annotations.push({
        x: p.date, y: p.upper_1,
        xref: 'x', yref: 'y',
        text: `±$${(range/2).toFixed(0)}`,
        showarrow: false,
        font: { size: 9, color: 'rgba(139,149,165,0.85)' },
        xanchor: 'center', yanchor: 'bottom', yshift: 3,
        bgcolor: 'rgba(15,17,23,0.7)',
      });
    });

    // Hover points for full cone detail
    traces.push({
      type: 'scatter',
      x: cp.map(p => p.date), y: cp.map(p => p.upper_1),
      mode: 'markers', marker: { color: 'rgba(0,0,0,0)', size: 14 },
      text: cp.map(p =>
        `<b>Exp ${p.date}</b> (${p.dte}d)<br>` +
        `ATM IV: ${p.iv}%<br>` +
        `±1σ: $${p.lower_1} – $${p.upper_1}<br>` +
        `±2σ: $${p.lower_2} – $${p.upper_2}`),
      hovertemplate: '%{text}<extra>IV Cone</extra>',
      showlegend: false, yaxis: 'y', xaxis: 'x',
    });
  }

  // ── 3. OI Put/Call Walls — horizontal bands, OI-proportional thickness ────
  // Stale walls are rendered at reduced opacity with a "(stale)" note
  const oi_alpha = d.oi_stale ? 0.45 : 0.85;
  const band_alpha_fill = d.oi_stale ? 0.06 : 0.13;
  const walls = (!d.is_asx && d.oi_walls) ? d.oi_walls : [];

  if (walls.length) {
    const allOI   = walls.map(w => w.oi);
    const maxOI   = Math.max(...allOI, 1);
    const priceRange = price * 0.15;  // 15% of current price = full-width band height

    const callWalls = walls.filter(w => w.type === 'call').sort((a,b) => a.date.localeCompare(b.date));
    const putWalls  = walls.filter(w => w.type === 'put').sort((a,b)  => a.date.localeCompare(b.date));

    // Helper: draw one wall as a horizontal band + label
    function drawWall(w, isCall) {
      const bandHalf = priceRange * (0.006 + 0.018 * (w.oi / maxOI));  // 0.6–2.4% of price
      const fillColor = isCall
        ? `rgba(34,197,94,${band_alpha_fill})`
        : `rgba(239,68,68,${band_alpha_fill})`;
      const lineColor = isCall
        ? `rgba(34,197,94,${oi_alpha})`
        : `rgba(239,68,68,${oi_alpha})`;
      const midColor  = isCall ? '#22c55e' : '#ef4444';

      // Filled band rectangle
      shapes.push({
        type: 'rect',
        x0: lastDate, x1: w.date,
        y0: w.strike - bandHalf, y1: w.strike + bandHalf,
        xref: 'x', yref: 'y',
        fillcolor: fillColor,
        line: { width: 0 },
        layer: 'below',
      });
      // Centre line of the band
      shapes.push({
        type: 'line',
        x0: lastDate, x1: w.date,
        y0: w.strike,  y1: w.strike,
        xref: 'x', yref: 'y',
        line: { color: lineColor, width: 1.5 },
      });
      // Vertical tick at expiry
      shapes.push({
        type: 'line',
        x0: w.date, x1: w.date,
        y0: w.strike - bandHalf * 2.5, y1: w.strike + bandHalf * 2.5,
        xref: 'x', yref: 'y',
        line: { color: lineColor, width: 2 },
      });
      // Label at expiry end
      const staleNote = d.oi_stale ? ' ⏱' : '';
      const oiK = w.oi >= 1000 ? `${(w.oi/1000).toFixed(1)}K` : `${w.oi}`;
      annotations.push({
        x: w.date, y: w.strike,
        xref: 'x', yref: 'y',
        text: `${isCall ? 'CALL' : 'PUT'} $${w.strike} | ${oiK} OI${staleNote}`,
        showarrow: false,
        font: { size: 10, color: midColor },
        xanchor: 'left', xshift: 7,
        bgcolor: 'rgba(15,17,23,0.82)',
        borderpad: 2,
      });
    }

    callWalls.forEach(w => drawWall(w, true));
    putWalls.forEach(w  => drawWall(w, false));

    // Connecting line across expiries (shows how positioning shifts)
    if (callWalls.length > 1) {
      traces.push({
        type: 'scatter',
        x: callWalls.map(w => w.date), y: callWalls.map(w => w.strike),
        mode: 'lines',
        line: { color: `rgba(34,197,94,${oi_alpha * 0.5})`, width: 1, dash: 'dot' },
        name: 'Call Wall drift', showlegend: false, hoverinfo: 'skip',
        yaxis: 'y', xaxis: 'x',
      });
    }
    if (putWalls.length > 1) {
      traces.push({
        type: 'scatter',
        x: putWalls.map(w => w.date), y: putWalls.map(w => w.strike),
        mode: 'lines',
        line: { color: `rgba(239,68,68,${oi_alpha * 0.5})`, width: 1, dash: 'dot' },
        name: 'Put Wall drift', showlegend: false, hoverinfo: 'skip',
        yaxis: 'y', xaxis: 'x',
      });
    }
  }

  // ── 4. Open Positions — your CC/CSP plotted directly on the chart ─────────
  const openPositions = d.open_positions || [];
  openPositions.forEach(pos => {
    const isCC  = pos.type === 'covered_call';
    const isCSP = pos.type === 'csp';
    const pctDist = price > 0 ? Math.abs(pos.strike - price) / price * 100 : 99;
    const threatened = (isCC && pos.strike <= price * 1.05) || (isCSP && pos.strike >= price * 0.95);
    const posColor = threatened ? '#f59e0b' : (isCC ? '#a855f7' : '#3b82f6');
    const label = (isCC ? 'CC' : 'CSP') + ` $${pos.strike} ${pos.expiry||''}`;

    // Horizontal line from today to expiry
    if (lastDate && pos.expiry) {
      shapes.push({
        type: 'line',
        x0: lastDate, x1: pos.expiry,
        y0: pos.strike, y1: pos.strike,
        xref: 'x', yref: 'y',
        line: { color: posColor, width: 2, dash: threatened ? 'solid' : 'dashdot' },
      });
      // Vertical expiry tick
      shapes.push({
        type: 'line',
        x0: pos.expiry, x1: pos.expiry,
        y0: 0, y1: 1, xref: 'x', yref: 'paper',
        line: { color: posColor + '66', width: 1, dash: 'dot' },
      });
      // Intersection label
      annotations.push({
        x: pos.expiry, y: pos.strike,
        xref: 'x', yref: 'y',
        text: `● Your ${label}${threatened ? ' ⚠️' : ''}`,
        showarrow: false,
        font: { size: 10, color: posColor },
        xanchor: 'right', xshift: -6,
        bgcolor: 'rgba(15,17,23,0.85)', borderpad: 2,
      });
    }
  });

  // ── 5. NBA Recommended Strike ─────────────────────────────────────────────
  // Pulled from the holding data passed into renderChart
  const nba = (r && r.next_best_action) ? r.next_best_action : {};
  const nbaTrade = nba.specific_trade || {};
  const nbaStrike = nbaTrade.strike;
  const nbaExpiry = nbaTrade.expiry;
  if (nbaStrike && nba.action_type === 'SELL_CC' && lastDate) {
    const nbaEnd = nbaExpiry || cp[cp.length - 1]?.date || lastDate;
    shapes.push({
      type: 'line',
      x0: lastDate, x1: nbaEnd,
      y0: nbaStrike, y1: nbaStrike,
      xref: 'x', yref: 'y',
      line: { color: 'rgba(34,197,94,0.55)', width: 1.5, dash: 'dot' },
    });
    annotations.push({
      x: lastDate, y: nbaStrike,
      xref: 'x', yref: 'y',
      text: `◎ Suggested CC $${nbaStrike}`,
      showarrow: false,
      font: { size: 9, color: 'rgba(34,197,94,0.75)' },
      xanchor: 'left', xshift: 6,
      bgcolor: 'rgba(15,17,23,0.75)',
    });
  }

  // ── Layout: Shared shapes / annotations ───────────────────────────────────

  // "Today" vertical divider
  if (lastDate) {
    shapes.push({
      type: 'line', x0: lastDate, x1: lastDate, y0: 0, y1: 1, yref: 'paper',
      line: { color: 'rgba(255,255,255,0.2)', width: 1.5, dash: 'dot' },
    });
    annotations.push({
      x: lastDate, y: 0.98, xref: 'x', yref: 'paper',
      text: '▶ Today', showarrow: false,
      font: { size: 9, color: 'rgba(255,255,255,0.4)' },
      xanchor: 'left', xshift: 4,
    });
  }

  // Current price reference line
  if (price) {
    shapes.push({
      type: 'line', x0: 0, x1: 1, y0: price, y1: price,
      xref: 'paper', yref: 'y',
      line: { color: 'rgba(255,255,255,0.14)', width: 1 },
    });
    annotations.push({
      x: 0, y: price, xref: 'paper', yref: 'y',
      text: `$${price.toFixed(2)}`,
      showarrow: false,
      font: { size: 9, color: 'rgba(255,255,255,0.4)' },
      xanchor: 'right', xshift: -4,
    });
  }

  // OI data freshness note
  const oiNote = d.oi_stale
    ? `⏱ OI from ${d.oi_as_of} (stale — after hours)`
    : (d.native_oi_available === false ? 'OI data unavailable'
       : `OI as of ${d.oi_as_of || '—'}`);
  const oiNoteColor = d.oi_stale ? 'rgba(245,158,11,0.6)' : 'rgba(255,255,255,0.25)';
  annotations.push({
    x: 1, y: 1, xref: 'paper', yref: 'paper',
    text: oiNote, showarrow: false,
    font: { size: 9, color: oiNoteColor },
    xanchor: 'right', yanchor: 'top',
  });

  const layout = {
    paper_bgcolor: 'transparent',
    plot_bgcolor:  DARK.plot,
    font: { color: DARK.text, size: 11 },
    margin: { t: 16, r: 14, b: 36, l: 62 },
    xaxis: {
      type: 'date',
      gridcolor: DARK.grid,
      linecolor: DARK.line,
      tickfont:  { color: DARK.tick, size: 10 },
      rangeslider: { visible: false },
      showgrid: true,
    },
    yaxis: {
      gridcolor: DARK.grid,
      linecolor: DARK.line,
      tickfont:  { color: DARK.tick, size: 10 },
      tickprefix: '$',
      showgrid: true,
    },
    legend: {
      x: 0.01, y: 0.99, xanchor: 'left', yanchor: 'top',
      bgcolor: 'rgba(0,0,0,0.25)',
      bordercolor: 'rgba(255,255,255,0.07)', borderwidth: 1,
      font: { size: 10 }, orientation: 'h',
    },
    shapes,
    annotations,
    hovermode: 'closest',
    hoverlabel: { bgcolor: '#1a1d27', bordercolor: '#2a2d3a', font: { color: '#e2e8f0', size: 12 } },
  };

  Plotly.newPlot(el, traces, layout, { displayModeBar: false, responsive: true });
}

// renderRddt kept as no-op stub — rendering is now done by buildHoldingCardHtml via renderScan
function renderRddt(r){
  // legacy no-op
  const ivC=r.iv_rank>40?'pos':'warn';
  const actionClass=r.action&&r.action.startsWith('BUY PROTECTION')?'protect':r.action&&r.action.startsWith('HOLD')?'hold':'';
  const recHtml=r.action?`<div class="rddt-action ${actionClass}"><strong>Recommendation:</strong> ${r.action}<br><span style="color:var(--muted);font-size:13px">${r.reasoning||''}</span></div>`:'';
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
      <td><strong>$${(p.effective_floor||0).toFixed(2)}</strong></td>
      <td class="warn">${(p.cost_pct_of_position||p.annualised_cost_pct||0).toFixed(1)}% of position</td>
      <td style="color:var(--muted);font-size:12px">${p.verdict||''}</td>
      <td><button class="log-btn" onclick='openModal("protective_put",${JSON.stringify({symbol:r.symbol||"RDDT",strike:p.strike,expiry:p.expiry,mid_price:p.mid_price,contracts:r.contracts_available||2})})'>Log Trade</button></td>
    </tr>`).join('');
    protHtml=`<h3 style="font-size:14px;margin:20px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">🛡️ Protective Put Options</h3>
    <div class="tbl-wrap"><table><thead><tr><th>Strike</th><th>Expiry</th><th>DTE</th><th>Cost/Contract</th><th>Total (2 contracts)</th><th>Effective Floor</th><th>Cost</th><th>Verdict</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
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

  // ── Market Structure (OI analysis) ────────────────────────────────────
  let mktHtml='';
  const ms=r.market_structure||{};
  if(ms.coaching){
    const pw=ms.put_wall;
    const cw=ms.call_wall;
    const gf=ms.gamma_flip;
    const price=r.current_price||0;

    // Key levels badges
    let badgesHtml='';
    if(pw){
      const pct=Math.abs(pw.pct_from_price||0);
      const k=(pw.oi/1000).toFixed(1);
      badgesHtml+=`<span class="oi-badge oi-badge-support">🛡 Put Wall $${pw.strike.toFixed(0)} (${pct}% below · ${k}K OI)</span> `;
    }
    if(cw){
      const pct=Math.abs(cw.pct_from_price||0);
      const k=(cw.oi/1000).toFixed(1);
      badgesHtml+=`<span class="oi-badge oi-badge-resist">🧱 Call Wall $${cw.strike.toFixed(0)} (${pct}% above · ${k}K OI)</span> `;
    }
    if(gf){
      const flipPct=(ms.gamma_flip_pct_from_price||0);
      const dir=ms.price_relative_to_flip==='above'?'above':'below';
      const gexLabel=ms.gex_positive?'Pos GEX — dampened':'Neg GEX — amplified';
      badgesHtml+=`<span class="oi-badge oi-badge-flip">⚡ Gamma Flip $${gf.toFixed(0)} (price ${dir} · ${gexLabel})</span>`;
    }

    // Mini visual range bar
    let rangeBarHtml='';
    if(pw&&cw&&price>0){
      const lo=Math.min(pw.strike,cw.strike,price)*0.9;
      const hi=Math.max(pw.strike,cw.strike,price)*1.1;
      const span=hi-lo;
      const putPct=((pw.strike-lo)/span*100).toFixed(1);
      const callPct=((cw.strike-lo)/span*100).toFixed(1);
      const pricePct=((price-lo)/span*100).toFixed(1);
      const fillWidth=(callPct-putPct).toFixed(1);
      rangeBarHtml=`
      <div style="margin:12px 0 4px;font-size:11px;color:var(--muted)">OI RANGE VISUALISER (support → resistance)</div>
      <div class="oi-range-bar">
        <div class="oi-range-fill" style="left:${putPct}%;width:${fillWidth}%"></div>
        <div class="oi-range-price" style="left:${pricePct}%"></div>
        <span class="oi-range-label" style="left:${putPct}%">$${pw.strike.toFixed(0)}</span>
        <span class="oi-range-label" style="left:${Math.min(parseFloat(callPct),85)}%">$${cw.strike.toFixed(0)}</span>
      </div>
      <div style="font-size:11px;color:var(--muted);display:flex;gap:16px;margin-bottom:8px">
        <span>▐ Range filled = OI-defined probable zone</span>
        <span style="color:var(--accent)">│ = current price ($${price})</span>
      </div>`;
    }

    // Top put & call OI strikes table
    let oiTableHtml='';
    const topPuts=ms.top_put_strikes||[];
    const topCalls=ms.top_call_strikes||[];
    if(topPuts.length||topCalls.length){
      const maxPutOI=Math.max(...topPuts.map(p=>p.put_oi||0),1);
      const maxCallOI=Math.max(...topCalls.map(c=>c.call_oi||0),1);
      const putRows=topPuts.map(p=>{
        const barW=Math.round((p.put_oi/maxPutOI)*120);
        const k=(p.put_oi/1000).toFixed(1);
        return `<tr><td>$${p.strike.toFixed(0)}</td><td>${k}K</td><td><div class="oi-bar-put" style="width:${barW}px"></div></td><td style="font-size:11px;color:var(--muted)">${p.strike<=price?'Support':'—'}</td></tr>`;
      }).join('');
      const callRows=topCalls.map(c=>{
        const barW=Math.round((c.call_oi/maxCallOI)*120);
        const k=(c.call_oi/1000).toFixed(1);
        return `<tr><td>$${c.strike.toFixed(0)}</td><td>${k}K</td><td><div class="oi-bar-call" style="width:${barW}px"></div></td><td style="font-size:11px;color:var(--muted)">${c.strike>=price?'Resist.':'—'}</td></tr>`;
      }).join('');
      oiTableHtml=`
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:12px">
        <div>
          <div style="font-size:11px;font-weight:700;color:var(--green);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">🛡 Top Put OI (Support)</div>
          <table style="width:100%"><thead><tr><th>Strike</th><th>OI</th><th>Strength</th><th></th></tr></thead><tbody>${putRows}</tbody></table>
        </div>
        <div>
          <div style="font-size:11px;font-weight:700;color:var(--red);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">🧱 Top Call OI (Resistance)</div>
          <table style="width:100%"><thead><tr><th>Strike</th><th>OI</th><th>Strength</th><th></th></tr></thead><tbody>${callRows}</tbody></table>
        </div>
      </div>`;
    }

    const dataNoteHtml=ms.data_note?`<div style="font-size:11px;color:var(--muted);margin-top:10px;opacity:.7">ℹ️ ${ms.data_note}</div>`:'';
    mktHtml=`
    <h3 style="font-size:14px;margin:24px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">📐 Market Structure — OI Analysis</h3>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">${badgesHtml}</div>
    ${rangeBarHtml}
    ${oiTableHtml}
    <div class="oi-coaching">${ms.coaching}</div>
    ${dataNoteHtml}`;
  }

  // no-op in new multi-stock UI
}


function scoreColor(v){
  if(v>=80) return 'var(--green)';
  if(v>=60) return 'var(--blue)';
  if(v>=40) return 'var(--orange)';
  return 'var(--red)';
}

function renderWheelPick(rows){
  const el=document.getElementById('wheel-pick-card');
  if(!el) return;
  if(!rows||!rows.length){el.innerHTML='';return;}
  const p=rows[0]; // already sorted by score desc

  const scoreTotal=Math.round((p.score||0)*100);

  const dims=[
    {key:'s_iv',   label:'IV Quality',      tip:'Are we selling when volatility is genuinely elevated? Combines 3-month IV rank + VRP (IV minus realised vol). Tastytrade target: IV rank > 50%.'},
    {key:'s_ev',   label:'Expected Return',  tip:'Risk-adjusted annualised return: Ann. Return × POP. Rewards high-income trades that also have good statistical backing. Target: > 25% risk-adjusted.'},
    {key:'s_pop',  label:'Prob of Profit',   tip:'Statistical probability of the trade expiring profitably at the chosen strike and DTE. Derived from Black-Scholes. Target: > 70%.'},
    {key:'s_dte',  label:'DTE Quality',      tip:'How close to the 30–45 day theta sweet spot. Tastytrade research shows this window captures the steepest theta decay curve. Penalty for < 21d (gamma risk) or > 60d (capital tied up too long).'},
    {key:'s_liq',  label:'Liquidity',        tip:'Weighted combination of bid-ask spread tightness (65%) and open interest depth (35%). Tight spreads reduce fill slippage — the hidden tax on options trades.'},
    {key:'s_delta',label:'Strike Selection', tip:'Quality of the chosen delta. 0.20–0.30 is the professional sweet spot: high enough premium to be meaningful, low enough probability of assignment to sleep well.'},
  ];

  const bars=dims.map(d=>{
    const v=p[d.key]||0;
    return `<div class="pick-score-row" title="${d.tip}">
      <span class="pick-score-label">${d.label}</span>
      <div class="pick-score-bar-bg">
        <div class="pick-score-bar-fill" style="width:${v}%;background:${scoreColor(v)}"></div>
      </div>
      <span class="pick-score-val" style="color:${scoreColor(v)}">${v.toFixed(0)}</span>
    </div>`;
  }).join('');

  const vrop = p.vrop || 0;
  let vrpNote;
  if(vrop >= 15)
    vrpNote = `VRP is a strong <strong>+${vrop.toFixed(1)}pp</strong> (IV ${p.current_iv}% vs HV30 ${p.hv30}%) — the market is significantly over-pricing risk, giving you a clear statistical edge as the seller.`;
  else if(vrop >= 5)
    vrpNote = `VRP is a positive <strong>+${vrop.toFixed(1)}pp</strong> (IV ${p.current_iv}% vs HV30 ${p.hv30}%) — implied vol is above realised, so selling has a meaningful edge.`;
  else if(vrop >= 0)
    vrpNote = `VRP is near-neutral at <strong>+${vrop.toFixed(1)}pp</strong> (IV ${p.current_iv}% vs HV30 ${p.hv30}%) — no strong vol edge, but return and probability metrics still make this the strongest available setup.`;
  else
    vrpNote = `⚠️ VRP is <strong>${vrop.toFixed(1)}pp</strong> (IV ${p.current_iv}% vs HV30 ${p.hv30}%) — IV is below realised vol, meaning the market is under-pricing risk. The selling edge is limited; size conservatively.`;

  const narrative=`<div class="pick-coach-label">🤖 Why Ollie picked this</div>
<p>You collect <strong>$${premium.toFixed(0)}</strong> upfront. With a ${p.pop}% probability of profit,
the statistically expected gain is <strong>~$${ev.toFixed(0)} per contract</strong> — or roughly
<strong>$${evPerDay.toFixed(0)}/day</strong> for the ${p.dte} days your capital is committed.
That's ${p.annualized_return}% annualised on $${(p.capital_required||0).toLocaleString()} at risk.
The $${p.strike} strike (${p.delta.toFixed(2)} delta) sits in the tastytrade sweet spot — far enough OTM
to have a ${p.prob_otm ? p.prob_otm.toFixed(0) : '~80'}% chance of expiring worthless, close enough to
collect meaningful premium. ${vrpNote}</p>`;

  const why=`<details class="pick-why">
<summary>📚 Why these 6 factors matter</summary>
<div class="pick-why-grid">
  <div class="pick-why-item"><div class="wi-title">IV Quality (25%)</div><div class="wi-body">Selling options when IV is elevated vs recent history means you collect more premium for the same risk. The VRP (IV − realised vol) tells you if the market is over-pricing fear — your edge as a seller.</div></div>
  <div class="pick-why-item"><div class="wi-title">Expected Return (25%)</div><div class="wi-body">Raw annualised return is meaningless without adjusting for probability. A 100% return trade with a 10% POP has terrible expected value. We multiply return × POP to get the true risk-adjusted yield.</div></div>
  <div class="pick-why-item"><div class="wi-title">Prob of Profit (20%)</div><div class="wi-body">The statistical probability of the trade expiring profitable. At ≥70% POP you're collecting premium from the majority of outcomes — this is the core edge of premium selling over time.</div></div>
  <div class="pick-why-item"><div class="wi-title">DTE Quality (15%)</div><div class="wi-body">Tastytrade research shows theta decay accelerates sharply in the final 30–45 days. Inside 21 DTE, gamma risk spikes and small moves hurt fast. Beyond 60 DTE, capital is tied up too long for low incremental gain.</div></div>
  <div class="pick-why-item"><div class="wi-title">Liquidity (10%)</div><div class="wi-body">A wide bid-ask spread is a hidden tax you pay on every trade. If the spread is $0.50, you're immediately down $50/contract at fill. Tight spreads + high OI mean you can enter and exit cleanly.</div></div>
  <div class="pick-why-item"><div class="wi-title">Strike Selection (5%)</div><div class="wi-body">The 0.20–0.30 delta range (70–80% chance of expiring worthless) is the professional sweet spot. Below 0.15 delta: premium too thin to bother. Above 0.35 delta: assignment risk becomes the dominant concern.</div></div>
</div></details>`;

  const premium = p.premium_100 || 0;
  const capitalReq = p.capital_required || 0;
  const popFrac = (p.pop || 0) / 100;
  const ev = premium * popFrac;                          // expected value per contract
  const evPerDay = p.dte > 0 ? ev / p.dte : 0;          // expected $ per day tied up
  const yieldPct = capitalReq > 0 ? (premium / capitalReq * 100).toFixed(2) : '—';
  const evColor = ev >= 150 ? 'var(--green)' : ev >= 75 ? 'var(--blue)' : 'var(--orange)';

  el.innerHTML=`<div class="pick-card">
  <div class="pick-header">
    <span class="pick-badge">🏆 Ollie's Pick</span>
    <span class="pick-trade">Sell $${p.strike} Put on ${p.symbol} — exp ${p.expiry}</span>
    <span style="background:rgba(34,197,94,.15);border:1px solid rgba(34,197,94,.4);color:var(--green);font-size:15px;font-weight:800;padding:4px 12px;border-radius:8px;white-space:nowrap">$${premium.toFixed(0)} <span style="font-size:11px;font-weight:500;opacity:.8">collected</span></span>
    <span style="background:rgba(99,102,241,.12);border:1px solid rgba(99,102,241,.3);color:${evColor};font-size:15px;font-weight:800;padding:4px 12px;border-radius:8px;white-space:nowrap" title="Expected Value = Premium × Probability of Profit. The statistically weighted dollar gain you can expect on average across many trades like this one.">~$${ev.toFixed(0)} <span style="font-size:11px;font-weight:500;opacity:.8">exp. value</span></span>
    <span class="pick-meta">Score: ${scoreTotal}/100 &nbsp;·&nbsp; ${yieldPct}% yield &nbsp;·&nbsp; ~$${evPerDay.toFixed(0)}/day &nbsp;·&nbsp; ${p.earnings_days != null ? `✅ Earnings ${p.earnings_days}d away` : '✅ No earnings in window'} &nbsp;·&nbsp; ${p.exdiv_in_window ? `⚠️ Ex-div ${p.exdiv_days}d (~$${(p.dividend_amount||0).toFixed(2)} drop)` : p.exdiv_days != null ? `✅ Ex-div ${p.exdiv_days}d away` : '✅ No ex-div in window'} &nbsp;·&nbsp; ${p.sector||'—'}</span>
  </div>
  <div class="pick-body">
    <div class="pick-scores">${bars}</div>
    <div class="pick-narrative">${narrative}</div>
  </div>
  ${why}
</div>`;
}

function renderWheelTbl(rows){
  if(!rows.length){document.getElementById('wheel-tbl-wrap').innerHTML='<p style="padding:16px;color:var(--muted)">No candidates found.</p>';return}
  const trs=rows.map(r=>{
    const popVal=r.pop||0;
    const ptVal=r.prob_touch||0;
    const popColor=popVal>=75?'pos':popVal>=60?'warn':'neg';
    const ptColor=ptVal<=40?'pos':ptVal<=60?'warn':'neg';
    const ivTierColor={'Low':'var(--muted)','Below Avg':'var(--orange)','Average':'var(--blue)','Elevated':'var(--green)','High':'var(--green)'}[r.iv_rank_tier||'']||'var(--muted)';
    return `<tr>
      <td><strong>${r.symbol}</strong></td>
      <td>$${(r.stock_price||0).toFixed(2)}</td>
      <td><strong>$${r.strike}</strong></td>
      <td>${r.expiry}</td><td>${r.dte}d</td>
      <td class="pos">$${(r.mid_price||0).toFixed(2)}</td>
      <td class="pos">${r.annualized_return||'—'}%</td>
      <td>${(r.prob_otm||0).toFixed(0)}%</td>
      <td class="${popColor}" title="${r.iv_rank_label||''}"><strong>${popVal.toFixed(0)}%</strong></td>
      <td class="${ptColor}">${ptVal.toFixed(0)}%</td>
      <td><span style="font-size:11px;color:${ivTierColor}">${r.iv_rank_tier||'—'}</span> ${r.iv_rank||'—'}%</td>
      <td><span class="badge badge-${r.score>0.6?'green':r.score>0.4?'blue':'orange'}">${(r.score||0).toFixed(2)}</span></td>
      <td><button class="log-btn" onclick='openModal("csp",${JSON.stringify({symbol:r.symbol,strike:r.strike,expiry:r.expiry,mid_price:r.mid_price,contracts:1})})'>Log This Trade</button></td>
    </tr>`;
  }).join('');
  document.getElementById('wheel-tbl-wrap').innerHTML=`<table id="wheel-tbl"><thead><tr>
    <th>Symbol</th><th>Price</th><th>Strike</th><th>Expiry</th><th>DTE</th>
    <th>Mid</th><th>Ann Ret</th>
    <th title="Prob OTM — stock expires below strike, you keep all premium">Prob OTM</th>
    <th title="Prob of Profit — stock stays above breakeven (strike minus premium) at expiry">POP ↑</th>
    <th title="~2× Prob ITM — rough chance strike is touched before expiry. Lower = safer.">P.Touch ↓</th>
    <th>IV Rank</th><th>Score</th><th></th>
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
    document.getElementById('m-flds-entry-date').classList.remove('hidden');
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
    payload.entry_date=document.getElementById('m-entry-date').value||null;
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
  if(t==='csp'||t==='cc'){document.getElementById('flds-single').classList.remove('hidden');document.getElementById('flds-entry-date').classList.remove('hidden');document.getElementById('f-sk-lbl').textContent=t==='csp'?'Put Strike':'Call Strike'}
  else if(t==='ic')document.getElementById('flds-ic').classList.remove('hidden');
  else if(t==='bull_put'||t==='bear_call'){document.getElementById('flds-spread').classList.remove('hidden');document.getElementById('f-sps-lbl').textContent=t==='bull_put'?'Short Put Strike':'Short Call Strike';document.getElementById('f-spl-lbl').textContent=t==='bull_put'?'Long Put Strike':'Long Call Strike'}
  else if(t==='shares'){document.getElementById('flds-shares').classList.remove('hidden');document.getElementById('f-ct-wrap').classList.add('hidden')}
}

function collectLog(){
  const sym=document.getElementById('f-sym').value.trim().toUpperCase();
  if(!sym){showToast('Enter a symbol','error');return null}
  const base={trade_type:selTypeVal,symbol:sym,contracts:parseInt(document.getElementById('f-ct').value)||1,commission:parseFloat(document.getElementById('f-com').value)||0,notes:document.getElementById('f-notes').value.trim()};
  if(selTypeVal==='csp'||selTypeVal==='cc'){
    const strike=parseFloat(document.getElementById('f-sk').value),expiry=document.getElementById('f-exp').value,premium=parseFloat(document.getElementById('f-pr').value),entry_date=document.getElementById('f-entry-date').value||null;
    if(!strike||!expiry||!premium){showToast('Fill Strike, Expiry and Premium','error');return null}
    return{...base,strike,expiry,premium,entry_date};
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
  ['f-sym','f-sk','f-exp','f-pr','f-entry-date','f-sp','f-lp','f-sc','f-lc','f-ic-exp','f-ic-cr','f-sps','f-spl','f-sp-exp','f-sp-cr','f-sh','f-shc','f-notes'].forEach(id=>{const el=document.getElementById(id);if(el)el.value=''});
  document.getElementById('flds-entry-date').classList.add('hidden');
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
    if(!d.trades||!d.trades.length){document.getElementById('history-wrap').innerHTML='<div class="empty"><div class="ico">📋</div><p>No trades yet. <button class="btn btn-success" style="margin-left:8px" onclick="showTab(\'log\')">＋ Add Trade</button></p></div>';return}
    const sc={open:'green',closed:'blue',expired:'blue',assigned:'orange',rolled:'orange',called_away:'orange'};
    const rows=d.trades.map(t=>{
      const pnlHtml=t.status!=='open'?`<span class="${t.realized_pnl>=0?'pos':'neg'}">${t.realized_pnl>=0?'+':''}$${(t.realized_pnl||0).toFixed(2)}</span>`:`<span class="neu">$${(t.premium_received||0).toFixed(2)} max</span>`;
      const sk=t.strike?`$${t.strike}`:t.short_put_strike?`$${t.short_put_strike}/$${t.short_call_strike}`:'—';
      const actions=`<td style="white-space:nowrap"><button class="btn btn-ghost" style="padding:3px 8px;font-size:12px" onclick="openEditTrade(${JSON.stringify(t).replace(/"/g,'&quot;')})">✏️ Edit</button> <button class="btn" style="padding:3px 8px;font-size:12px;background:var(--red);color:#fff;border-color:var(--red)" onclick="deleteTrade('${t.id}','${t.symbol}')">🗑 Delete</button></td>`;
      return`<tr><td style="font-weight:600">${t.id}</td><td><strong>${t.symbol}</strong></td><td>${(t.trade_type||'').toUpperCase()}</td><td>${t.entry_date||'—'}</td><td>${t.expiry||'—'}</td><td>${sk}</td><td>$${(t.premium_received||0).toFixed(2)}</td><td>${pnlHtml}</td><td><span class="badge badge-${sc[t.status]||'blue'}">${t.status}</span></td>${actions}</tr>`;
    }).join('');
    const s=d.summary;
    document.getElementById('history-wrap').innerHTML=`<div class="card"><div style="overflow-x:auto"><table><thead><tr><th>ID</th><th>Symbol</th><th>Type</th><th>Entry</th><th>Expiry</th><th>Strike</th><th>Premium</th><th>P&L</th><th>Status</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div><div style="margin-top:14px;font-size:13px;color:var(--muted)">${s.total_trades} trades &nbsp;|&nbsp; Win rate: ${s.win_rate}% &nbsp;|&nbsp; Realized P&L: <span class="${s.total_realized_pnl>=0?'pos':'neg'}">${s.total_realized_pnl>=0?'+':''}$${(s.total_realized_pnl||0).toFixed(2)}</span></div></div>`;
  }catch(e){document.getElementById('history-wrap').innerHTML=`<div class="empty"><p>Error: ${e.message}</p></div>`}
}

async function deleteTrade(id,sym){
  if(!confirm(`Delete trade ${id} (${sym})?\n\nThis cannot be undone.`))return;
  try{
    const r=await fetch('/api/delete-trade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trade_id:id})});
    const res=await r.json();
    if(res.success){showToast(`Deleted ${id}`,'success');loadHistory()}
    else showToast('Error: '+res.error,'error');
  }catch(e){showToast('Error: '+e.message,'error')}
}

function openEditTrade(t){
  document.getElementById('et-id').value=t.id;
  document.getElementById('et-sym').value=t.symbol||'';
  document.getElementById('et-type').value=t.trade_type||'csp';
  document.getElementById('et-date').value=t.entry_date||'';
  document.getElementById('et-strike').value=t.strike||'';
  document.getElementById('et-expiry').value=t.expiry||'';
  document.getElementById('et-premium').value=t.entry_price||'';
  document.getElementById('et-contracts').value=t.quantity||1;
  document.getElementById('et-prem-received').value=t.premium_received||'';
  document.getElementById('et-commission').value=t.commission||0;
  document.getElementById('et-status').value=t.status||'open';
  document.getElementById('et-exit-price').value=t.exit_price||'';
  document.getElementById('et-realized-pnl').value=t.realized_pnl||'';
  document.getElementById('et-notes').value=t.notes||'';
  document.getElementById('et-title').textContent=`Edit Trade — ${t.id}`;
  toggleEditExitFields();
  document.getElementById('edit-trade-modal').classList.add('open');
}

function toggleEditExitFields(){
  const s=document.getElementById('et-status').value;
  const show=s!=='open';
  document.getElementById('et-exit-wrap').style.display=show?'':'none';
}

function closeEditTrade(){document.getElementById('edit-trade-modal').classList.remove('open')}

async function saveEditTrade(){
  const btn=document.getElementById('et-save-btn');btn.disabled=true;btn.innerHTML='<span class="loader"></span> Saving…';
  const payload={
    trade_id:document.getElementById('et-id').value,
    symbol:document.getElementById('et-sym').value.trim().toUpperCase(),
    trade_type:document.getElementById('et-type').value,
    entry_date:document.getElementById('et-date').value,
    strike:document.getElementById('et-strike').value||null,
    expiry:document.getElementById('et-expiry').value||null,
    entry_price:document.getElementById('et-premium').value||null,
    quantity:document.getElementById('et-contracts').value,
    premium_received:document.getElementById('et-prem-received').value||null,
    commission:document.getElementById('et-commission').value||0,
    status:document.getElementById('et-status').value,
    exit_price:document.getElementById('et-exit-price').value||null,
    realized_pnl:document.getElementById('et-realized-pnl').value||null,
    notes:document.getElementById('et-notes').value.trim(),
  };
  try{
    const r=await fetch('/api/edit-trade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const res=await r.json();
    if(res.success){showToast('Trade updated ✅','success');closeEditTrade();loadHistory()}
    else showToast('Error: '+res.error,'error');
  }catch(e){showToast('Error: '+e.message,'error')}
  finally{btn.disabled=false;btn.innerHTML='💾 Save Changes'}
}

// ── TOAST ──────────────────────────────────────────────────────────────
function showToast(msg,type='info'){const t=document.getElementById('toast');t.textContent=msg;t.className=`toast ${type} show`;setTimeout(()=>t.classList.remove('show'),4000)}

window.addEventListener('load',async()=>{
  // Try to load cached data instantly, fall back to fresh scan
  await loadCachedScan();
});
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
        # Attach Gemini key if configured (used by intelligence engine)
        app_config = load_config()
        wheel_mgr._gemini_key = app_config.get('gemini_key', '') or ''

        # ── Analyse every holding the user owns ──────────────────────────────
        holdings = load_holdings()
        holdings_results = []
        for h in holdings:
            sym = h.get('symbol', '').upper().strip()
            if not sym:
                continue
            try:
                # Fetch wheel_cycle BEFORE recommend_action so the NBA engine
                # can see existing protective puts inside _score_signals()
                wheel_cycle = get_wheel_cycle_summary(sym)
                rec = wheel_mgr.recommend_action(
                    symbol=sym,
                    shares=int(h.get('shares', 100)),
                    avg_cost=h.get('avg_cost') or None,
                    extra_data={'wheel_cycle': wheel_cycle},
                )
                rec['_holding_notes'] = h.get('notes', '')
                # Keep for template rendering (already set via extra_data but be explicit)
                rec['wheel_cycle'] = wheel_cycle
                holdings_results.append(rec)
            except Exception as he:
                holdings_results.append({
                    'symbol': sym, 'error': str(he),
                    'shares_held': h.get('shares', 0),
                })

        # ── Market screener (unchanged) ──────────────────────────────────────
        wheel_df = screener.screen_wheel_candidates(symbols=FULL_WATCHLIST[:20], max_stock_price=200)
        ic_syms = WATCHLIST_ETFS[:6] + ['AAPL', 'MSFT', 'AMZN', 'GOOGL', 'META', 'NVDA']
        condor_df = screener.screen_iron_condors(symbols=ic_syms)
        spread_df = screener.screen_credit_spreads(symbols=FULL_WATCHLIST[:15], spread_type='put')

        result = _sanitise({
            'holdings': holdings_results,
            # Keep 'rddt' key for any legacy JS that still uses it
            'rddt': next((r for r in holdings_results if r.get('symbol') == 'RDDT'), {}),
            'wheel_candidates': [] if wheel_df.empty else wheel_df.head(30).to_dict('records'),
            'iron_condors': [] if condor_df.empty else condor_df.head(15).to_dict('records'),
            'credit_spreads': [] if spread_df.empty else spread_df.head(15).to_dict('records'),
            'scan_timestamp': datetime.utcnow().isoformat() + 'Z',
        })

        # ── Cache to disk ────────────────────────────────────────────────────
        try:
            with open(SCAN_CACHE_PATH, 'w') as f:
                json.dump(result, f)
        except Exception:
            pass

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/briefing', methods=['POST'])
def api_briefing():
    """
    Generate a cross-holding portfolio briefing using Gemini.
    Called AFTER scan completes — accepts the holdings array and returns
    a 4-5 sentence portfolio-level advisory.
    """
    cfg = load_config()
    gemini_key = cfg.get('gemini_key', '') or ''
    if not gemini_key or len(gemini_key) < 10:
        return jsonify({'briefing': '', 'source': 'none'})

    data = request.json or {}
    holdings = data.get('holdings', [])
    if not holdings:
        return jsonify({'briefing': '', 'source': 'none'})

    # Build a compact summary of all holdings for the prompt
    summaries = []
    for h in holdings:
        if h.get('error'):
            continue
        sym = h.get('symbol', '?')
        nba = h.get('next_best_action', {}) or {}
        ms = h.get('market_structure', {}) or {}
        iv = h.get('iv_rank', 0) or 0
        price = h.get('current_price', 0) or 0
        pnl = h.get('unrealized_pnl', 0) or 0
        shares = h.get('shares_held', 0) or 0
        wc = h.get('wheel_cycle', {}) or {}
        ed = h.get('earnings_date', '')
        ed_days = h.get('earnings_days_away', 999)
        gex = 'Pos GEX' if ms.get('gex_positive') else 'Neg GEX'
        pw = ms.get('put_wall', {})
        cw = ms.get('call_wall', {})
        summaries.append(
            f"{sym}: ${price:.2f}, {shares} shares, IV Rank {iv:.0f}%, "
            f"P&L {'+'if pnl>=0 else ''}${pnl:,.0f}, "
            f"NBA={nba.get('action_type','?')} ({nba.get('confidence',0)}% conf), "
            f"{gex}, "
            f"Put wall ${pw.get('strike','?')}, Call wall ${cw.get('strike','?')}, "
            f"Wheel phase: {wc.get('phase','?')}, "
            f"{'Earnings in '+str(ed_days)+'d ('+ed+')' if ed_days < 60 else 'No near earnings'}"
        )

    portfolio_summary = '\n'.join(summaries)

    prompt = (
        "You are Options Ollie, a friendly but expert options trading advisor. "
        "The trader has loaded their portfolio for a morning review. "
        "Write a 4-5 sentence morning briefing that:\n"
        "1. Gives an overall portfolio health check (P&L, risk posture)\n"
        "2. Highlights the single most important action to take today, across ALL holdings\n"
        "3. Flags any risks (earnings approaching, negative GEX, positions near cost basis)\n"
        "4. Ends with one confident, encouraging takeaway\n\n"
        "Be concise, specific (use tickers and dollar amounts), and conversational. "
        "No disclaimers. No bullet points — write flowing prose.\n\n"
        f"Portfolio:\n{portfolio_summary}"
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_key}"
    import urllib.request, urllib.error
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 400}
    }
    try:
        req_data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=req_data, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode('utf-8'))
            candidates = body.get('candidates', [])
            if candidates:
                parts = candidates[0].get('content', {}).get('parts', [])
                if parts:
                    text = parts[0].get('text', '').strip()
                    return jsonify({'briefing': text, 'source': 'gemini'})
    except Exception:
        pass

    return jsonify({'briefing': '', 'source': 'error'})


@app.route('/api/cached')
def api_cached():
    """Return the last saved scan result (instant load on startup)."""
    if not os.path.exists(SCAN_CACHE_PATH):
        return jsonify({'error': 'no_cache'})
    try:
        with open(SCAN_CACHE_PATH) as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/holdings', methods=['GET'])
def api_get_holdings():
    return jsonify(load_holdings())


@app.route('/api/holdings', methods=['POST'])
def api_add_holding():
    data = request.json or {}
    sym = data.get('symbol', '').upper().strip()
    if not sym:
        return jsonify({'success': False, 'error': 'Symbol required'})
    holdings = load_holdings()
    # Don't duplicate
    if any(h['symbol'] == sym for h in holdings):
        return jsonify({'success': False, 'error': f'{sym} already in your holdings'})
    holdings.append({
        'symbol': sym,
        'shares': int(data.get('shares', 100)),
        'avg_cost': float(data['avg_cost']) if data.get('avg_cost') else None,
        'exchange': data.get('exchange', 'NASDAQ'),
        'notes': data.get('notes', ''),
    })
    save_holdings(holdings)
    return jsonify({'success': True, 'holdings': holdings})


@app.route('/api/holdings/<symbol>', methods=['DELETE'])
def api_delete_holding(symbol):
    holdings = load_holdings()
    updated = [h for h in holdings if h['symbol'] != symbol.upper()]
    save_holdings(updated)
    return jsonify({'success': True, 'holdings': updated})


@app.route('/api/holdings/<symbol>', methods=['PUT'])
def api_update_holding(symbol):
    data = request.json or {}
    holdings = load_holdings()
    for h in holdings:
        if h['symbol'] == symbol.upper():
            if 'shares' in data:
                h['shares'] = int(data['shares'])
            if 'avg_cost' in data:
                h['avg_cost'] = float(data['avg_cost']) if data['avg_cost'] else None
            if 'notes' in data:
                h['notes'] = data['notes']
            break
    save_holdings(holdings)
    return jsonify({'success': True, 'holdings': holdings})


@app.route('/api/config', methods=['GET'])
def api_get_config():
    cfg = load_config()
    # Mask key — never send full key to browser
    key = cfg.get('gemini_key', '')
    masked = ('*' * (len(key) - 4) + key[-4:]) if len(key) > 4 else ('*' * len(key))
    return jsonify({'gemini_key_set': bool(key), 'gemini_key_masked': masked})


@app.route('/api/config', methods=['POST'])
def api_save_config():
    data = request.json or {}
    cfg = load_config()
    if 'gemini_key' in data:
        cfg['gemini_key'] = data['gemini_key'].strip()
    save_config(cfg)
    return jsonify({'success': True})


@app.route('/api/chart/<symbol>')
def api_chart(symbol):
    """
    Return enriched chart data:
      - 30 trading days of OHLCV candles
      - IV-implied expected move cone (up to 60 DTE)
      - OI put/call walls (with stale-data caching for after-hours)
      - 21 EMA + 50 SMA (computed from 70 calendar days of data)
      - Volume profile (24-bin POC)
      - Open CC/CSP positions from the trade ledger
      - Cone accuracy metric (% of daily moves inside ±1σ)
    """
    import math as _m
    from datetime import date, timedelta
    import yfinance as yf
    import concurrent.futures as _cf

    symbol = symbol.upper().strip()
    today = date.today()
    today_str = today.isoformat()
    oi_cache_file = os.path.join(OUTPUT_DIR, 'data', f'oi_{symbol}.json')

    try:
        ticker = yf.Ticker(symbol)

        # ── Candles: 5 trading days only — just an anchor to current price ────
        # The chart's job is to show the FUTURE (walls, cone, positions), not the past.
        hist = ticker.history(period='8d', interval='1d')
        if hist.empty:
            return jsonify({'error': f'No price history for {symbol}'})

        current_price = float(hist['Close'].iloc[-1])
        last_candle_date = hist.index[-1].strftime('%Y-%m-%d')

        candles = []
        for idx, row in hist.iterrows():
            vol = row.get('Volume', 0)
            candles.append({
                'date': idx.strftime('%Y-%m-%d'),
                'open':  round(float(row['Open']),  4),
                'high':  round(float(row['High']),  4),
                'low':   round(float(row['Low']),   4),
                'close': round(float(row['Close']), 4),
                'volume': int(vol) if vol and not _m.isnan(float(vol)) else 0,
            })
        candles = candles[-5:]   # strictly 5 trading days

        # ── Open positions from ledger (plotted directly on the chart) ────────
        open_positions_chart = []
        try:
            ledger_data = get_ledger()
            for t in ledger_data.trades:
                if t.symbol != symbol or t.status != 'open':
                    continue
                if t.trade_type in ('covered_call', 'csp') and t.strike and t.expiry:
                    open_positions_chart.append({
                        'type':    t.trade_type,
                        'strike':  round(float(t.strike), 2),
                        'expiry':  t.expiry,
                        'premium': round(float(t.premium_received or 0), 2),
                        'id':      t.id,
                    })
        except Exception:
            pass

        is_asx = symbol.endswith('.AX')
        if is_asx:
            return jsonify(_sanitise({
                'symbol': symbol, 'current_price': current_price,
                'candles': candles, 'cone_points': [], 'oi_walls': [],
                'oi_stale': False, 'oi_as_of': today_str, 'is_asx': True,
                'last_candle_date': last_candle_date,
                'open_positions': open_positions_chart,
            }))

        # ── Options data: IV cone + OI walls ─────────────────────────────────
        cutoff = today + timedelta(days=61)
        try:
            all_exps = list(ticker.options or [])
        except Exception:
            all_exps = []

        expirations = []
        for exp in all_exps:
            try:
                exp_date = date.fromisoformat(exp)
                if today < exp_date <= cutoff:
                    expirations.append(exp)
            except Exception:
                continue

        cone_points = []
        oi_walls    = []
        atm_iv_first = None

        def _native_oi(val):
            """openInterest only — no volume fallback (volume proxy causes phantom walls)."""
            if val is None:
                return 0
            try:
                fval = float(val)
                return 0 if _m.isnan(fval) else int(fval)
            except (TypeError, ValueError):
                return 0

        native_oi_found = False

        for exp in expirations[:8]:
            try:
                exp_date = date.fromisoformat(exp)
                dte = (exp_date - today).days
                if dte < 1:
                    continue

                with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                    _fut = _ex.submit(ticker.option_chain, exp)
                    try:
                        chain = _fut.result(timeout=8)
                    except _cf.TimeoutError:
                        continue
                calls = chain.calls.copy()
                puts  = chain.puts.copy()

                atm_iv = None
                if not calls.empty:
                    calls['_dist'] = (calls['strike'] - current_price).abs()
                    atm_row = calls.loc[calls['_dist'].idxmin()]
                    iv_val = float(atm_row.get('impliedVolatility', 0) or 0)
                    if iv_val > 0.01:
                        atm_iv = iv_val
                        if atm_iv_first is None:
                            atm_iv_first = atm_iv

                if atm_iv:
                    t_frac = dte / 365.0
                    m1 = current_price * atm_iv * _m.sqrt(t_frac)
                    m2 = current_price * atm_iv * 2 * _m.sqrt(t_frac)
                    cone_points.append({
                        'date':    exp, 'dte': dte,
                        'iv':      round(atm_iv * 100, 1),
                        'upper_1': round(current_price + m1, 2),
                        'lower_1': round(current_price - m1, 2),
                        'upper_2': round(current_price + m2, 2),
                        'lower_2': round(current_price - m2, 2),
                    })

                # OI walls — OTM only (puts below price, calls above)
                if not calls.empty:
                    otm_calls = calls[calls['strike'] > current_price].copy()
                    if not otm_calls.empty:
                        otm_calls['_oi'] = otm_calls['openInterest'].apply(_native_oi)
                        call_oi = int(otm_calls['_oi'].max())
                        if call_oi > 0:
                            native_oi_found = True
                            best_call = otm_calls.loc[otm_calls['_oi'].idxmax()]
                            oi_walls.append({'type': 'call', 'date': exp,
                                             'strike': round(float(best_call['strike']), 2), 'oi': call_oi})

                if not puts.empty:
                    otm_puts = puts[puts['strike'] < current_price].copy()
                    if not otm_puts.empty:
                        otm_puts['_oi'] = otm_puts['openInterest'].apply(_native_oi)
                        put_oi = int(otm_puts['_oi'].max())
                        if put_oi > 0:
                            native_oi_found = True
                            best_put = otm_puts.loc[otm_puts['_oi'].idxmax()]
                            oi_walls.append({'type': 'put', 'date': exp,
                                             'strike': round(float(best_put['strike']), 2), 'oi': put_oi})

            except Exception:
                continue

        # ── OI wall caching: persist live data; serve stale after-hours ───────
        oi_stale = False
        oi_as_of = today_str
        if native_oi_found and oi_walls:
            try:
                with open(oi_cache_file, 'w') as f:
                    json.dump({'walls': oi_walls, 'date': today_str}, f)
            except Exception:
                pass
        elif not native_oi_found and os.path.exists(oi_cache_file):
            try:
                with open(oi_cache_file) as f:
                    cached = json.load(f)
                oi_walls  = cached.get('walls', [])
                oi_as_of  = cached.get('date', today_str)
                oi_stale  = True
            except Exception:
                pass

        return jsonify(_sanitise({
            'symbol':              symbol,
            'current_price':       current_price,
            'candles':             candles,
            'cone_points':         cone_points,
            'oi_walls':            oi_walls,
            'oi_stale':            oi_stale,
            'oi_as_of':            oi_as_of,
            'last_candle_date':    last_candle_date,
            'is_asx':              False,
            'native_oi_available': native_oi_found,
            'open_positions':      open_positions_chart,
        }))

    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/income')
def api_income():
    """
    F2 — Income Dashboard.
    Returns monthly premium income (last 6 months), win rate, best tickers,
    annualised yield, and monthly target tracking — all from the trade ledger.
    """
    from datetime import date, datetime as _dt
    from collections import defaultdict
    try:
        ledger = get_ledger()
        today = date.today()

        # ── Monthly income (last 6 calendar months) ───────────────────────
        months = []
        for i in range(5, -1, -1):
            # go back i months from current month
            m = today.month - i
            y = today.year
            while m <= 0:
                m += 12
                y -= 1
            months.append((y, m))

        monthly_data = {}
        for y, m in months:
            key = f"{y}-{m:02d}"
            monthly_data[key] = {
                'label': _dt(y, m, 1).strftime('%b %Y'),
                'premium': 0.0,
                'trades': 0,
                'winners': 0,
                'losers': 0,
            }

        # Walk all closed/expired/assigned options trades
        closed_options = [
            t for t in ledger.trades
            if t.is_options_trade() and t.status in ('closed', 'expired', 'assigned', 'rolled', 'called_away')
        ]

        for t in closed_options:
            close_dt_str = t.exit_date or t.entry_date
            if not close_dt_str:
                continue
            try:
                close_dt = _dt.strptime(close_dt_str, '%Y-%m-%d').date()
            except Exception:
                continue
            key = f"{close_dt.year}-{close_dt.month:02d}"
            if key in monthly_data:
                monthly_data[key]['premium'] += t.premium_received
                monthly_data[key]['trades'] += 1
                if t.realized_pnl > 0:
                    monthly_data[key]['winners'] += 1
                else:
                    monthly_data[key]['losers'] += 1

        # Round premiums
        for v in monthly_data.values():
            v['premium'] = round(v['premium'], 2)

        # ── Overall stats ─────────────────────────────────────────────────
        summary = ledger.summary()

        # ── Best tickers by total premium ─────────────────────────────────
        ticker_income = defaultdict(float)
        for t in ledger.trades:
            if t.is_options_trade() and t.premium_received > 0:
                ticker_income[t.symbol] += t.premium_received

        best_tickers = sorted(
            [{'symbol': sym, 'premium': round(p, 2)} for sym, p in ticker_income.items()],
            key=lambda x: x['premium'], reverse=True
        )[:5]

        # ── Monthly target tracking ($500/month default) ──────────────────
        target_per_month = 500.0
        current_month_key = f"{today.year}-{today.month:02d}"
        current_month_income = monthly_data.get(current_month_key, {}).get('premium', 0.0)
        target_pct = round(min(current_month_income / target_per_month * 100, 100), 1)

        # ── Annualised yield (portfolio-level) ────────────────────────────
        open_collateral = summary.get('total_collateral_deployed', 0)
        total_12mo_premium = sum(
            v['premium'] for v in monthly_data.values()
        ) * 2   # 6 months × 2 = rough 12mo
        annualised_yield = None
        if open_collateral > 0:
            annualised_yield = round(total_12mo_premium / open_collateral * 100, 1)

        return jsonify(_sanitise({
            'monthly': list(monthly_data.values()),
            'summary': summary,
            'best_tickers': best_tickers,
            'target_per_month': target_per_month,
            'current_month_income': round(current_month_income, 2),
            'target_pct': target_pct,
            'annualised_yield': annualised_yield,
        }))
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
        entry_date = data.get('entry_date') or None
        if t == 'csp':
            trade = ledger.enter_csp(sym, float(data['strike']), data['expiry'], float(data['premium']), contracts, commission, notes, entry_date=entry_date)
        elif t == 'cc':
            trade = ledger.enter_covered_call(sym, float(data['strike']), data['expiry'], float(data['premium']), contracts, commission, notes, entry_date=entry_date)
        elif t == 'ic':
            trade = ledger.enter_iron_condor(sym, data['expiry'], float(data['short_put']), float(data['long_put']), float(data['short_call']), float(data['long_call']), float(data['credit']), contracts, commission, notes)
        elif t in ('bull_put', 'bear_call'):
            trade = ledger.enter_credit_spread(sym, data['expiry'], float(data['short_strike']), float(data['long_strike']), t, float(data['credit']), contracts, commission, notes)
        elif t == 'shares':
            trade = ledger.enter_shares(sym, int(data['shares']), float(data['cost_per_share']), notes)
        elif t == 'protective_put':
            trade = ledger.enter_protective_put(sym, float(data['strike']), data['expiry'], float(data['premium']), contracts, commission, notes, entry_date=entry_date)
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
        return jsonify(_sanitise(monitor.summary_report(snapshots)))
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


@app.route('/api/delete-trade', methods=['POST'])
def api_delete_trade():
    data = request.json
    try:
        ledger = get_ledger()
        trade_id = data.get('trade_id', '')
        deleted = ledger.delete_trade(trade_id)
        if deleted:
            return jsonify({'success': True, 'trade_id': trade_id})
        return jsonify({'success': False, 'error': f'{trade_id} not found'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/edit-trade', methods=['POST'])
def api_edit_trade():
    data = request.json
    try:
        ledger = get_ledger()
        trade_id = data.pop('trade_id', '')
        # Coerce numeric fields so they arrive as the right types
        for num_field in ('entry_price', 'quantity', 'strike', 'premium_received',
                          'commission', 'collateral_required', 'exit_price', 'realized_pnl'):
            if num_field in data and data[num_field] is not None and data[num_field] != '':
                data[num_field] = float(data[num_field])
            elif num_field in data:
                data.pop(num_field)  # drop blank/None optionals
        if 'quantity' in data:
            data['quantity'] = int(data['quantity'])
        trade = ledger.update_trade(trade_id, data)
        if trade:
            from dataclasses import asdict
            return jsonify({'success': True, 'trade': asdict(trade)})
        return jsonify({'success': False, 'error': f'{trade_id} not found'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


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
