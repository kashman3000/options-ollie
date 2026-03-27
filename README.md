# 🦉 Options Ollie

A local web-based options trading assistant for covered call income generation, downside risk management, and wheel strategy candidate selection.

---

## What it does

Options Ollie runs on your own machine as a local web server and gives you a daily dashboard to:

- **Scan for opportunities** — screens a watchlist of stocks for cash-secured puts (wheel strategy), iron condors, and credit spreads ranked by a 6-dimension professional scoring model
- **Get Ollie's Pick** — a featured best-trade recommendation with expected dollar value, score breakdown, VRP analysis, earnings and ex-dividend checks
- **Manage your holdings** — deep NBA (Next Best Action) analysis per holding with tabbed panels covering covered calls, risk scenarios, OI walls, and wheel roadmap
- **Collar & protection awareness** — detects open covered calls and protective puts from your ledger; recommends Hold (Collar Active) when both legs are open
- **Log trades with one click** — every scan result has a "Log This Trade" button that pre-fills the trade entry modal
- **Monitor open positions** — live P&L, % of max profit captured, days to expiry, distance-to-strike alerts
- **Portfolio briefing** — AI-generated cross-holding narrative via Gemini 2.0 Flash
- **Track history** — full trade ledger with win rate and realised P&L summary

---

## Getting started

### Requirements

- Python 3.9+
- Internet connection (fetches live options data via Yahoo Finance)

### Installation

1. Clone this repo or download the folder
2. Double-click the launcher for your OS:
   - **Mac:** `Start Options Ollie.command`
   - **Windows:** `Start Options Ollie.bat`

The launcher installs dependencies automatically and starts the server.

3. Open your browser and go to: **http://localhost:5000**

### Manual start

```bash
pip install -r requirements.txt
python server.py
```

---

## Project structure

```
Options Ollie/
├── server.py                     # Flask web server + all API endpoints + full UI
├── Start Options Ollie.command   # Mac one-click launcher
├── Start Options Ollie.bat       # Windows one-click launcher
├── requirements.txt              # Python dependencies
├── data/
│   └── trade_ledger.json         # Persistent trade storage (auto-created, gitignored)
└── options_ollie/
    ├── config.py                 # Portfolio config, watchlists, risk settings
    ├── data/
    │   ├── fetcher.py            # Yahoo Finance data fetching, Greeks, IV rank + VRP
    │   └── screener.py           # Wheel, iron condor and spread screeners (Ollie Score)
    ├── strategies/
    │   ├── wheel.py              # Holding analysis engine (CC, risk, collar, earnings, ex-div)
    │   ├── intelligence.py       # NBA scoring engine + Gemini coaching
    │   ├── trade_ledger.py       # Trade entry, closing, P&L tracking
    │   └── position_monitor.py   # Live position monitoring + advice engine
    ├── dashboard/
    │   └── generator.py          # Static HTML dashboard generator
    └── notifications/
        └── telegram.py           # Telegram daily digest (optional)
```

---

## Options strategies supported

| Strategy | Description |
|---|---|
| Covered Call (CC) | Sell calls against long stock to generate income |
| Cash-Secured Put (CSP) | Sell puts to acquire stock at a lower basis |
| Iron Condor (IC) | Sell a put spread + call spread for range-bound stocks |
| Bull Put Spread | Sell a put, buy a lower put — defined risk income |
| Protective Put | Buy a put to floor downside on a long stock position |
| Collar | Sell a CC + buy a protective put — near-zero-cost hedge |

---

## Ollie's Pick — 6-dimension scoring model

The wheel candidate screener uses a professional framework based on tastytrade and theta-selling research:

| Dimension | Weight | What it measures |
|---|---|---|
| IV Quality | 25% | 3-month IV rank × VRP multiplier (IV − HV30). Rewards selling when vol is genuinely elevated. |
| Expected Return | 25% | Annualised return × POP — risk-adjusted yield, not raw return. |
| Prob of Profit | 20% | Black-Scholes derived probability of the trade expiring profitably. |
| DTE Quality | 15% | Closeness to the 30–45 day theta sweet spot (peak theta decay window). |
| Liquidity | 10% | Bid-ask spread tightness (65%) + open interest depth (35%). |
| Strike Selection | 5% | 0.20–0.30 delta sweet spot — meaningful premium, manageable assignment risk. |

### Automatic filters applied before scoring

- **VRP gate** — candidates with Volatility Risk Premium < −5pp are dropped (selling has no statistical edge)
- **Earnings blackout** — any stock with earnings ≤21 days away is excluded entirely
- **Ex-dividend awareness** — if ex-div falls within the option DTE, POP is adjusted downward for the expected stock price drop on the ex-date

### Pick card shows

- Dollar premium collected per contract
- Expected value (premium × POP) — the statistically weighted dollar gain
- Expected value per day — for comparing trades of different lengths
- Earnings safety status
- Ex-dividend status (with warning if it falls inside the trade window)
- VRP commentary — honest about whether the selling environment is strong, neutral, or weak

---

## Next Best Action (NBA) engine

Per holding, the NBA engine scores across 9 signal categories and picks the optimal action:

| Signal | What it checks |
|---|---|
| IV Quality + VRP | 3-month IV rank × Volatility Risk Premium |
| Position vs Cost Basis | % above/below your average cost |
| Best POP available | Probability of profit on best CC candidate |
| GEX Regime | Dealer gamma exposure — predicts whether moves amplify or dampen |
| Risk Scenarios | How many of 5 downside scenarios breach cost basis |
| Collar Quality | Whether a net-credit collar is available |
| Wheel Cycle Phase | CSP / CC / Shares / Ready phase detection from ledger |
| Open CC + Put Awareness | Suppresses selling what you already own; detects collar state |
| Earnings Blackout | Hard downgrade within 14 days; soft warning within 21 days |
| Ex-Dividend | Early assignment risk on open CCs; price drop risk on new CSPs |

**Action types:** `SELL_CC` · `BUY_PROTECTION` · `COLLAR` · `HOLD_WAIT` · `PREPARE_ASSIGNMENT` · `ASX_HOLD`

Collar detection: if both an open CC and open protective put are found in the ledger, all action scores except HOLD_WAIT are hard-suppressed and the headline reads **🔒 Collar Active — Hold both legs**.

---

## IV Rank and VRP

Options Ollie uses **real ATM implied volatility** extracted from the live options chain (not historical vol as a proxy). IV rank is computed over a **63-day (3-month) window** rather than 52 weeks — this avoids post-IPO volatility spikes inflating the range on newer stocks.

The **Volatility Risk Premium (VRP = IV − HV30)** is shown as a signal on every holding card:
- VRP > +15pp → strong selling edge (market over-pricing fear)
- VRP +5 to +15pp → moderate selling edge
- VRP −5 to +5pp → neutral
- VRP < −5pp → negative edge (candidates excluded from screener)

---

## Position management rules

- **50% profit rule** — flag to close when 50% of max credit captured
- **21 DTE roll zone** — prompt to consider rolling when ≤21 days remain
- **2× loss stop-loss** — urgent alert when unrealised loss reaches 2× premium received
- **Strike proximity alert** — warns when stock is within 7% of a short strike

---

## Configuration

Edit `options_ollie/config.py` to change:
- Your holdings (symbol, share count, cost basis)
- Watchlist symbols
- Risk preferences (min IV rank, min probability OTM, DTE targets)
- Gemini API key (for AI coaching and portfolio briefing)

---

## Telegram notifications (optional)

Add your Telegram bot token and chat ID to `config.py` to receive a daily position digest. Leave blank to disable.

---

## Version history

| Version | Notes |
|---|---|
| v1.0.0 | Initial release — unified web dashboard, trade logging, position monitoring, multi-holding support |
| v2.0.0 | Advisory UI overhaul — NBA hero card, tabbed analysis panels, AI portfolio briefing, 6-dimension Ollie's Pick with VRP/earnings/ex-div filters, real ATM IV rank (3-month window), collar detection, duplicate position suppression, expected value dollar display |

---

*Built for personal use. Not financial advice. Always do your own research before trading options.*
