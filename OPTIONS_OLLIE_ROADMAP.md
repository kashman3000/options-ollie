# Options Ollie — Improvement Roadmap

*Audit conducted: 2026-03-30*

---

## Bugs Fixed This Session

| Fix | File | Description |
|-----|------|-------------|
| ✅ `primaryExch` → `primaryExchange` | server.py:2803/2808 | IBKR sync crash on stock positions |
| ✅ 100% profit display | position_monitor.py | When yfinance fails, sqrt-time fallback now used before P&L calc |
| ✅ Protective put P&L formula | position_monitor.py | Was double-counting cost; fixed to `current_value + premium_received` |
| ✅ Protective put advice | position_monitor.py | Was getting short-option "roll zone" advice; now has its own advice block |

---

## Phase 1 — Fix the Silent Breaks ⬜

> Quick wins. All in server.py JavaScript + Python. High impact, lower effort.

1. ⬜ Fix `overall_pct_captured` in Positions summary bar — exclude protective puts (negative `premium_received` skews the aggregate %)
2. ⬜ Unify "% captured" to always use sqrt-of-time decay model — remove linear time-elapsed version from `buildRollCalcHtml()` so both tabs show the same number
3. ⬜ Add **Protective Put** to Log a Trade tab type grid
4. ⬜ Add **Collar** to Log a Trade tab type grid with proper two-leg fields
5. ⬜ Fix Income tab: remove `_incomeFetched` guard, add ↻ Refresh button, auto-refresh after logging/closing
6. ⬜ Auto-refresh History tab after logging a trade within the same session

---

## Phase 2 — Fix the Close/Roll Workflow ⬜

> Biggest daily-use pain point. Position monitor gives good advice but then leaves you stranded.

7. ⬜ Replace `prompt()` close dialog with inline panel: exit price, commission, status (closed / expired / rolled / called away), notes
8. ⬜ Add **Roll This Position** button: marks old trade as "rolled", pre-populates Log form with same symbol/type/contracts
9. ⬜ Add IBKR sync button to Positions tab header (currently only on Scan tab)

---

## Phase 3 — Navigation & Daily-Use Structure ⬜

> Most days you don't want to scan — you want to check positions. Tabs should reflect that.

10. ⬜ Reorder tabs: **Positions → Scan → Income → Log → History**
11. ⬜ Live badge counts on Positions tab label (e.g. "📊 Positions 🚨1" for urgent)
12. ⬜ "Last updated X ago" timestamp on Positions tab + optional auto-refresh toggle

---

## Phase 4 — Consolidate the Two Advice Systems ⬜

> Currently: scan tab's NBA card + roll calculator AND position monitor both advise on the same position, and they can contradict each other.

13. ⬜ Per-holding scan cards: if an active logged position exists for that symbol/type, show position monitor advice instead of re-computing NBA card
14. ⬜ Roll calculator in scan tab: hide when position monitor already has a monitored entry for that CC (monitor is more accurate)

---

## Phase 5 — Settings & Config Polish ⬜

15. ⬜ Add monthly income target to Settings modal (hardcoded at $500 in Python)
16. ⬜ Add IBKR host/port/client ID to Settings modal (hardcoded at 127.0.0.1:4001)
17. ⬜ Update scan loading message from "Yahoo Finance" → reflects IBKR for ASX

---

## Key Files Reference

| Phase | Files |
|-------|-------|
| 1 | `server.py` — JS: `buildRollCalcHtml`, `renderMonSummary`, Log tab HTML, `_incomeFetched`, `loadHistory`; Python: `get_wheel_cycle_summary()` |
| 2 | `server.py` — JS: `posCard`, `closePosition`, log/confirm flow |
| 3 | `server.py` — HTML tab order, `showTab()`, `runMonitor()` |
| 4 | `server.py` — `buildHoldingCardHtml`, `buildRollCalcHtml`; `options_ollie/strategies/position_monitor.py` |
| 5 | `server.py` — Settings modal HTML, `load_config`/`save_config`, scan endpoint |

---

## Root Cause of Most Issues

The app grew tab-by-tab. Scan tab and Positions tab both ended up advising on the same positions using different data sources and different calculations. The fix isn't just patching individual bugs — it's making one system the authority (position monitor for open positions, scan for new opportunities) and linking them together.
