# 🦉 Options Ollie

A local web-based options trading assistant designed for covered call income generation and downside risk management on an active stock portfolio.

---

## What it does

Options Ollie runs on your own machine as a local web server and gives you a daily dashboard to:

- **Scan for opportunities** — screens a watchlist of stocks for cash-secured puts (wheel strategy), iron condors, and credit spreads ranked by quality score
- **Manage your RDDT position** — deep analysis of your Reddit Inc. (RDDT) covered call candidates alongside full downside risk modelling including scenario tables, protective put pricing, collar strategies, and a cost-basis reduction roadmap
- **Log trades with one click** — every scan result has a "Log This Trade" button that pre-fills the trade entry modal with symbol, strike, expiry and suggested mid-price
- **Monitor open positions** — live P&L, % of max profit captured, days to expiry, distance-to-strike alerts, and rules-based advice (HOLD / WATCH / ACTION / URGENT)
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

The launcher installs dependencies automatically (`flask`, `yfinance`, `scipy`) and starts the server.

3. Open your browser and go to: **http://localhost:5000**

### Manual start

```bash
pip install flask yfinance scipy
python server.py
```

---

## Project structure

```
Options Ollie/
├── server.py                     # Flask web server + all API endpoints
├── Start Options Ollie.command   # Mac one-click launcher
├── Start Options Ollie.bat       # Windows one-click launcher
├── requirements.txt              # Python dependencies
├── data/
│   └── trade_ledger.json         # Persistent trade storage (auto-created, gitignored)
└── options_ollie/
    ├── config.py                 # Portfolio config, watchlists, risk settings
    ├── data/
    │   ├── fetcher.py            # Yahoo Finance data fetching + Greeks estimation
    │   └── screener.py           # Wheel, iron condor and spread screeners
    ├── strategies/
    │   ├── wheel.py              # RDDT covered call + risk analysis engine
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
| Bear Call Spread | Sell a call, buy a higher call — defined risk income |
| Protective Put | Buy a put to floor downside on a long stock position |
| Collar | Sell a CC + buy a protective put — near-zero-cost hedge |

---

## Position management rules

Ollie applies a rules-based advisory engine to all open positions:

- **50% profit rule** — flags to close when 50% of max credit has been captured
- **21 DTE roll zone** — prompts to consider rolling when ≤21 days remain
- **2× loss stop-loss** — urgent alert when unrealised loss reaches 2× premium received
- **Strike proximity alert** — warns when stock is within 7% of a short strike

---

## Risk analysis (RDDT position)

The RDDT card shows a comprehensive risk view for the active 200-share position:

- **Downside scenarios** — dollar P&L at –5%, –10%, –15%, –20%, –30% drops with cost-basis breach flagged
- **1-sigma range** — typical 30-day move based on 30-day historical volatility
- **Protective put options** — put strikes at 5/10/15/20% floors with annualised cost %
- **Collar strategies** — CC + put combinations showing net credit/debit
- **Cost-basis roadmap** — months of CC selling needed to reduce basis by 5/10/15%

---

## Configuration

Edit `options_ollie/config.py` to change:
- Your RDDT share count and cost basis
- Watchlist symbols
- Risk preferences (min IV rank, min probability OTM, DTE targets)

---

## Telegram notifications (optional)

Add your Telegram bot token and chat ID to `config.py` to receive a daily position digest. Leave blank to disable.

---

## Version history

| Version | Notes |
|---|---|
| v1.0.0 | Initial release — unified web dashboard, trade logging, position monitoring, RDDT risk analysis |

---

*Built for personal use. Not financial advice. Always do your own research before trading options.*
