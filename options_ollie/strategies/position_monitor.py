"""
Options Ollie — Position Monitor
Monitors all open confirmed trades daily, calculates live P&L,
and generates actionable management advice to maximise profits.

Advice is generated using proven options management rules:
  - 50% profit rule: Close at 50% of max premium captured
  - 21 DTE roll rule: Consider rolling when DTE drops under 21
  - 2x loss rule: Cut or roll if current loss > 2x premium received
  - Strike defence: Alert when underlying approaches the short strike
  - Expiry urgency: Urgent close/roll advice inside 7 DTE
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional, Dict, Any

import yfinance as yf
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq

from .trade_ledger import TradeLedger, Trade, TradeType, TradeStatus


# ── Advice severity levels ───────────────────────────────────────────────────

ADVICE_HOLD   = 'HOLD'
ADVICE_WATCH  = 'WATCH'
ADVICE_ACTION = 'ACTION'
ADVICE_URGENT = 'URGENT'


@dataclass
class PositionSnapshot:
    """Live snapshot of a single open position."""
    trade_id: str
    symbol: str
    trade_type: str
    entry_date: str
    expiry: Optional[str]
    strike: Optional[float]
    short_put_strike: Optional[float]
    short_call_strike: Optional[float]
    contracts: int
    premium_received: float          # Total premium collected at entry

    # Live market data
    current_price: float = 0.0      # Current stock price
    current_option_price: float = 0.0  # Current mid price of the option
    unrealized_pnl: float = 0.0     # Positive = profit, negative = loss
    pct_max_profit: float = 0.0     # 0–100%: how much of max premium kept
    dte: Optional[int] = None

    # Greeks (estimated)
    delta: Optional[float] = None
    theta: Optional[float] = None
    iv: Optional[float] = None

    # Distance metrics
    pct_to_short_put: Optional[float] = None   # % gap from price → short put
    pct_to_short_call: Optional[float] = None  # % gap from price → short call

    # Advice
    advice_level: str = ADVICE_HOLD
    advice_headline: str = ''
    advice_detail: str = ''
    advice_actions: List[str] = field(default_factory=list)


class PositionMonitor:
    """
    Loads open trades from the ledger, enriches them with live market data,
    and generates management recommendations.
    """

    def __init__(self, ledger: TradeLedger):
        self.ledger = ledger

    def monitor_all(self) -> List[PositionSnapshot]:
        """
        Run a full monitor pass on all open trades.
        Returns a list of PositionSnapshot objects — one per trade.
        """
        open_trades = self.ledger.open_trades()
        snapshots = []
        for trade in open_trades:
            snap = self._build_snapshot(trade)
            snap = self._generate_advice(snap, trade)
            snapshots.append(snap)
        return snapshots

    def monitor_one(self, trade_id: str) -> Optional[PositionSnapshot]:
        """Monitor a single trade by ID."""
        trade = self.ledger.get_trade(trade_id)
        if not trade or trade.status != TradeStatus.OPEN:
            return None
        snap = self._build_snapshot(trade)
        snap = self._generate_advice(snap, trade)
        return snap

    # ── Internal: build snapshot ─────────────────────────────────────────

    def _build_snapshot(self, trade: Trade) -> PositionSnapshot:
        snap = PositionSnapshot(
            trade_id=trade.id,
            symbol=trade.symbol,
            trade_type=trade.trade_type,
            entry_date=trade.entry_date,
            expiry=trade.expiry,
            strike=trade.strike,
            short_put_strike=trade.short_put_strike,
            short_call_strike=trade.short_call_strike,
            contracts=trade.quantity,
            premium_received=trade.premium_received,
        )

        # ── DTE ──────────────────────────────────────────────────────────
        if trade.expiry:
            try:
                exp = datetime.strptime(trade.expiry, '%Y-%m-%d')
                snap.dte = max((exp - datetime.now()).days, 0)
            except ValueError:
                pass

        # ── Live stock price ─────────────────────────────────────────────
        try:
            ticker = yf.Ticker(trade.symbol)
            hist = ticker.history(period='2d')
            if not hist.empty:
                snap.current_price = round(float(hist['Close'].iloc[-1]), 2)
        except Exception:
            pass

        # ── Live option price (for options positions) ─────────────────────
        if trade.trade_type != TradeType.LONG_SHARES and trade.expiry and snap.current_price:
            # Calculate actual entry DTE from trade dates so the time-decay
            # fallback uses the real holding period, not a hardcoded 45-day guess.
            entry_dte = None
            if trade.entry_date and trade.expiry:
                try:
                    entry_dt = datetime.strptime(trade.entry_date, '%Y-%m-%d')
                    expiry_dt = datetime.strptime(trade.expiry, '%Y-%m-%d')
                    entry_dte = max((expiry_dt - entry_dt).days, 1)
                except ValueError:
                    pass

            snap.current_option_price = self._fetch_option_mid(
                symbol=trade.symbol,
                expiry=trade.expiry,
                strike=trade.strike or trade.short_put_strike,
                option_type='put' if trade.option_side in ('put', '') else 'call',
                trade_type=trade.trade_type,
                short_call_strike=trade.short_call_strike,
                short_put_strike=trade.short_put_strike,
                current_price=snap.current_price,
                dte=snap.dte,
                entry_price=trade.entry_price,
                entry_dte=entry_dte,
            )

        # ── Unrealized P&L ────────────────────────────────────────────────
        if trade.trade_type == TradeType.LONG_SHARES:
            if snap.current_price and trade.entry_price:
                snap.unrealized_pnl = round(
                    (snap.current_price - trade.entry_price) * trade.quantity, 2
                )
        else:
            # Sold option: profit = premium received - current cost to close
            cost_to_close = snap.current_option_price * trade.quantity * 100
            snap.unrealized_pnl = round(trade.premium_received - cost_to_close, 2)

            # % of max profit captured
            if trade.premium_received > 0:
                snap.pct_max_profit = round(
                    snap.unrealized_pnl / trade.premium_received * 100, 1
                )

        # ── Distance to strike(s) ─────────────────────────────────────────
        if snap.current_price:
            if trade.short_put_strike:
                snap.pct_to_short_put = round(
                    (snap.current_price - trade.short_put_strike) / snap.current_price * 100, 1
                )
            elif trade.strike and trade.option_side == 'put':
                snap.pct_to_short_put = round(
                    (snap.current_price - trade.strike) / snap.current_price * 100, 1
                )

            if trade.short_call_strike:
                snap.pct_to_short_call = round(
                    (trade.short_call_strike - snap.current_price) / snap.current_price * 100, 1
                )
            elif trade.strike and trade.option_side == 'call':
                snap.pct_to_short_call = round(
                    (trade.strike - snap.current_price) / snap.current_price * 100, 1
                )

        return snap

    def _fetch_option_mid(self, symbol: str, expiry: str, strike: Optional[float],
                          option_type: str, trade_type: str,
                          short_call_strike: Optional[float],
                          short_put_strike: Optional[float],
                          current_price: float, dte: Optional[int],
                          entry_price: float,
                          entry_dte: Optional[int] = None) -> float:
        """
        Try to fetch the current mid price of the option from yfinance.
        Falls back to a time-decay estimate if the chain isn't available.
        """
        try:
            ticker = yf.Ticker(symbol)
            chain = ticker.option_chain(expiry)

            if trade_type in (TradeType.CSP, TradeType.BULL_PUT_SPREAD):
                df = chain.puts
                target_strike = short_put_strike or strike
                is_call = False
            elif trade_type in (TradeType.COVERED_CALL, TradeType.BEAR_CALL_SPREAD):
                df = chain.calls
                target_strike = short_call_strike or strike
                is_call = True
            elif trade_type == TradeType.IRON_CONDOR:
                # Live bid/ask first
                put_mid = self._chain_bid_ask_mid(chain.puts, short_put_strike)
                call_mid = self._chain_bid_ask_mid(chain.calls, short_call_strike)
                if round(put_mid + call_mid, 2) > 0:
                    return round(put_mid + call_mid, 2)
                # BS with cross-expiry IV
                bs_put = self._bs_from_chain(chain.puts, short_put_strike, current_price, dte, False, symbol)
                bs_call = self._bs_from_chain(chain.calls, short_call_strike, current_price, dte, True, symbol)
                if round(bs_put + bs_call, 2) > 0:
                    return round(bs_put + bs_call, 2)
                # lastPrice fallback
                lp = self._chain_last_price(chain.puts, short_put_strike)
                lc = self._chain_last_price(chain.calls, short_call_strike)
                if round(lp + lc, 2) > 0:
                    return round(lp + lc, 2)
                return self._estimate_current_price(entry_price, dte, entry_dte)
            else:
                df = chain.puts if option_type == 'put' else chain.calls
                target_strike = strike
                is_call = (option_type == 'call')

            # 1. Live bid/ask mid — only available when market is open
            mid = self._chain_bid_ask_mid(df, target_strike)
            if mid > 0:
                return mid

            # 2. Black-Scholes with cross-expiry implied vol — more accurate than
            #    a stale lastPrice when the stock has moved since the last trade.
            bs_price = self._bs_from_chain(df, target_strike, current_price, dte, is_call, symbol)
            if bs_price > 0:
                return bs_price

            # 3. lastPrice — stale but better than nothing
            last = self._chain_last_price(df, target_strike)
            if last > 0:
                return last

            # 4. Last resort: sqrt-of-time decay model
            return self._estimate_current_price(entry_price, dte, entry_dte)

        except Exception:
            # Fallback: estimate using simple time-decay model
            return self._estimate_current_price(entry_price, dte, entry_dte)

    def _chain_mid(self, df, strike: Optional[float]) -> float:
        """Legacy combined helper — returns bid/ask mid, then lastPrice."""
        mid = self._chain_bid_ask_mid(df, strike)
        return mid if mid > 0 else self._chain_last_price(df, strike)

    def _chain_bid_ask_mid(self, df, strike: Optional[float]) -> float:
        """Return (bid+ask)/2 only. Returns 0.0 when market is closed."""
        if df is None or df.empty or strike is None:
            return 0.0
        try:
            row = df.iloc[(df['strike'] - strike).abs().argsort()[:1]]
            bid = float(row['bid'].iloc[0])
            ask = float(row['ask'].iloc[0])
            mid = round((bid + ask) / 2, 2)
            return mid if mid > 0 else 0.0
        except Exception:
            return 0.0

    def _chain_last_price(self, df, strike: Optional[float]) -> float:
        """Return lastPrice for the nearest strike. May be stale outside market hours."""
        if df is None or df.empty or strike is None:
            return 0.0
        try:
            row = df.iloc[(df['strike'] - strike).abs().argsort()[:1]]
            if 'lastPrice' in row.columns:
                last = float(row['lastPrice'].iloc[0])
                return round(last, 2) if last > 0 else 0.0
            return 0.0
        except Exception:
            return 0.0

    def _bs_from_chain(self, df, strike: Optional[float], current_price: float,
                       dte: Optional[int], is_call: bool,
                       symbol: Optional[str] = None) -> float:
        """
        Estimate option price using Black-Scholes.

        Volatility priority:
          1. Chain impliedVolatility — if it looks realistic (> 10%)
          2. 30-day historical volatility from price history — always live,
             used when chain IV is stale/zero (common outside market hours)
        """
        if df is None or df.empty or strike is None or not current_price or not dte or dte <= 0:
            return 0.0
        try:
            row = df.iloc[(df['strike'] - strike).abs().argsort()[:1]]
            iv = float(row['impliedVolatility'].iloc[0]) if 'impliedVolatility' in row.columns else 0.0

            # Chain IV below 10% on an individual stock is almost certainly stale data.
            # Back-solve a better IV from high-volume near-ATM options instead.
            if np.isnan(iv) or iv < 0.10:
                iv = self._get_vol_estimate(symbol, current_price, is_call) if symbol else 0.0

            if iv <= 0:
                return 0.0

            S = current_price
            K = float(row['strike'].iloc[0])
            T = dte / 365.0
            r = 0.05
            d1 = (np.log(S / K) + (r + 0.5 * iv ** 2) * T) / (iv * np.sqrt(T))
            d2 = d1 - iv * np.sqrt(T)
            if is_call:
                price = S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
            else:
                price = K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
            return round(max(float(price), 0.01), 2)
        except Exception:
            return 0.0

    def _get_vol_estimate(self, symbol: str, current_price: float,
                          is_call: bool = True) -> float:
        """
        Best available volatility estimate for Black-Scholes pricing.

        Strategy:
        1. Back-solve IV from near-ATM options on the nearest liquid expiry
           (7–45 DTE), preferring the same option side (calls for call pricing)
           to avoid put/call skew distortion.
        2. Fall back to 30-day historical volatility if no liquid data found.
        """
        def bs_price_fn(S, K, T, r, sig, call):
            if sig <= 0 or T <= 0: return 0.0
            d1 = (np.log(S / K) + (r + 0.5 * sig ** 2) * T) / (sig * np.sqrt(T))
            d2 = d1 - sig * np.sqrt(T)
            if call:
                return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
            return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

        try:
            ticker = yf.Ticker(symbol)
            today = datetime.now()
            nearby = [
                e for e in ticker.options
                if 7 <= (datetime.strptime(e, '%Y-%m-%d') - today).days <= 45
            ]
            if not nearby:
                raise ValueError("no nearby expiries")

            r = 0.05
            S = current_price
            ivs = []

            for exp in nearby[:3]:
                T = (datetime.strptime(exp, '%Y-%m-%d') - today).days / 365.0
                if T <= 0:
                    continue
                chain = ticker.option_chain(exp)

                # Prefer same side as the option being priced (less skew distortion).
                # OTM only: calls above current price, puts below current price.
                if is_call:
                    candidates = chain.calls[
                        chain.calls['strike'].between(current_price * 0.98, current_price * 1.08)
                        & (chain.calls['lastPrice'] > 0.10)
                        & (chain.calls['volume'] > 0)
                    ]
                    side_is_call = True
                else:
                    candidates = chain.puts[
                        chain.puts['strike'].between(current_price * 0.92, current_price * 1.02)
                        & (chain.puts['lastPrice'] > 0.10)
                        & (chain.puts['volume'] > 0)
                    ]
                    side_is_call = False

                for _, row in candidates.iterrows():
                    K = float(row['strike'])
                    last = float(row['lastPrice'])
                    try:
                        iv = brentq(
                            lambda sig: bs_price_fn(S, K, T, r, sig, side_is_call) - last,
                            0.05, 5.0, xtol=1e-4
                        )
                        ivs.append(iv)
                    except Exception:
                        continue

                if ivs:
                    break

            if ivs:
                return round(float(np.median(ivs)), 4)

        except Exception:
            pass

        # ── Fallback: 30-day historical volatility ─────────────────────
        try:
            hist = yf.Ticker(symbol).history(period='35d')
            if len(hist) < 5:
                return 0.0
            closes = hist['Close'].tail(31)
            log_returns = np.log(closes / closes.shift(1)).dropna()
            hv = float(log_returns.std() * np.sqrt(252))
            return round(hv, 4) if hv > 0 else 0.0
        except Exception:
            return 0.0

    def _estimate_current_price(self, entry_price: float, dte: Optional[int],
                                entry_dte: Optional[int] = None) -> float:
        """
        Rough time-decay estimate when live chain data isn't available.
        Uses sqrt-of-time model (theta decays faster near expiry).

        entry_dte: the DTE at the time the trade was opened (calculated from
                   trade.entry_date and trade.expiry). Falls back to 45 if unknown.
        """
        if not entry_price or dte is None or dte <= 0:
            return 0.0
        baseline_dte = entry_dte if (entry_dte and entry_dte > 0) else 45
        # Clamp: current DTE can't exceed the baseline
        effective_dte = min(dte, baseline_dte)
        ratio = (effective_dte / baseline_dte) ** 0.5
        return round(entry_price * ratio, 2)

    # ── Internal: generate advice ─────────────────────────────────────────

    def _generate_advice(self, snap: PositionSnapshot, trade: Trade) -> PositionSnapshot:
        """
        Apply management rules and populate advice fields.
        Rules are applied in priority order — highest priority wins headline.
        """
        actions = []
        level = ADVICE_HOLD
        headline = ''
        detail = ''

        dte = snap.dte
        pct = snap.pct_max_profit
        premium = snap.premium_received
        current_cost = snap.current_option_price * snap.contracts * 100

        # ── For shares ────────────────────────────────────────────────────
        if trade.trade_type == TradeType.LONG_SHARES:
            if snap.current_price and trade.entry_price:
                gain_pct = (snap.current_price - trade.entry_price) / trade.entry_price * 100
                if gain_pct >= 10:
                    level = ADVICE_ACTION
                    headline = f"Up {gain_pct:.1f}% — consider selling covered calls"
                    detail = ("Your shares are in strong profit. This is an ideal time to sell "
                              "covered calls above your cost basis to generate income while holding.")
                    actions.append("Sell a covered call 5-10% OTM, 30-45 DTE")
                elif gain_pct <= -10:
                    level = ADVICE_WATCH
                    headline = f"Down {abs(gain_pct):.1f}% — monitor for CC opportunity"
                    detail = ("Shares are below your cost basis. Avoid selling covered calls below "
                              "your cost basis. Consider selling ATM or slightly OTM calls to reduce cost basis.")
                    actions.append("Sell ATM covered call to reduce cost basis")
                else:
                    level = ADVICE_HOLD
                    headline = f"Position healthy — gain {gain_pct:+.1f}%"
                    detail = "Hold and look for an opportunity to sell covered calls when IV is elevated."
                    actions.append("Sell covered call on IV spike, 30-45 DTE")
            snap.advice_level = level
            snap.advice_headline = headline
            snap.advice_detail = detail
            snap.advice_actions = actions
            return snap

        # ── For options trades ────────────────────────────────────────────

        # Rule 1: URGENT — very close to expiry
        if dte is not None and dte <= 5:
            level = ADVICE_URGENT
            headline = f"⚠️ {dte} DTE — Act now: close or let expire"
            if pct >= 70:
                detail = (f"You've captured {pct:.0f}% of max profit. With only {dte} days left, "
                          "there is very little premium left to gain. Close the position to free up "
                          "capital and eliminate any tail risk.")
                actions.append(f"Close position — lock in {pct:.0f}% of max profit")
                actions.append("Deploy capital into a new 30-45 DTE trade")
            elif pct < 0:
                loss = abs(snap.unrealized_pnl)
                detail = (f"Position is at a loss of ${loss:.0f} with only {dte} days left. "
                          "Rolling is risky this close to expiry. Evaluate whether to take the loss "
                          "now or let the position play out if it's still OTM.")
                if snap.pct_to_short_put and snap.pct_to_short_put > 3:
                    actions.append("Still OTM — consider holding to expiry")
                else:
                    actions.append("Close now to avoid assignment risk")
            else:
                detail = (f"Only {dte} DTE remaining. The position is profitable at {pct:.0f}% of max. "
                          "Close to realise gains and avoid last-minute gamma risk.")
                actions.append(f"Close for profit — {pct:.0f}% of max captured")

        # Rule 2: ACTION — 50% profit rule (the gold standard)
        elif pct >= 50 and (dte is None or dte > 5):
            level = ADVICE_ACTION
            profit = snap.unrealized_pnl
            headline = f"✅ 50% rule triggered — ${profit:.0f} profit locked"
            detail = (f"You've captured {pct:.1f}% of your maximum possible profit on this position "
                      f"(${profit:.0f} of ${premium:.0f} max). The 50% rule says: close now. "
                      "You eliminate the remaining risk for half the potential reward — "
                      "a statistically superior exit strategy.")
            actions.append(f"Close position — take ${profit:.0f} profit")
            actions.append("Open a new 30-45 DTE trade to re-deploy capital")
            if dte and dte > 21:
                actions.append(f"Still {dte} DTE left — capital can be recycled immediately")

        # Rule 3: ACTION — 2x loss rule
        elif premium > 0 and current_cost > premium * 2:
            level = ADVICE_ACTION
            loss = abs(snap.unrealized_pnl)
            headline = f"🚨 2x loss rule — position down ${loss:.0f}"
            detail = (f"The cost to close is now ${current_cost:.0f}, which is more than 2x the "
                      f"${premium:.0f} premium you received. This is the standard stop-loss trigger. "
                      "Rolling may recover some loss; closing limits further damage.")
            if dte and dte > 14:
                actions.append(f"Roll out 4-6 weeks and down/up to a better strike")
                actions.append("Collect additional credit on the roll to offset loss")
            else:
                actions.append("Close the position — limit further loss")
            actions.append("Post-mortem: review IV rank and strike selection at entry")

        # Rule 4: WATCH — entering the 21 DTE danger zone
        elif dte is not None and dte <= 21 and pct < 50:
            level = ADVICE_WATCH
            headline = f"👁 {dte} DTE — Approaching roll zone"
            detail = (f"Under 21 days to expiry with only {pct:.0f}% of max profit captured. "
                      "Gamma risk is rising — small moves in the underlying have bigger impacts now. "
                      "Start preparing to roll if the position doesn't reach 50% profit soon.")
            actions.append(f"Roll forward 4-6 weeks if {50 - pct:.0f}% profit not captured in next 3 days")
            actions.append("Look at same-strike, next expiry — collect additional credit if possible")
            if snap.pct_to_short_put and snap.pct_to_short_put < 5:
                actions.append(f"⚠️ Stock only {snap.pct_to_short_put:.1f}% above your put strike — roll defensively")
            if snap.pct_to_short_call and snap.pct_to_short_call < 5:
                actions.append(f"⚠️ Stock only {snap.pct_to_short_call:.1f}% below your call strike — roll defensively")

        # Rule 5: WATCH — strike defence (more than 21 DTE but stock getting close)
        elif self._strike_threatened(snap):
            level = ADVICE_WATCH
            if snap.pct_to_short_put is not None and snap.pct_to_short_put < 7:
                headline = f"👁 Stock approaching put strike ({snap.pct_to_short_put:.1f}% above)"
                detail = (f"{snap.symbol} is only {snap.pct_to_short_put:.1f}% above your ${snap.short_put_strike or snap.strike} "
                          f"put strike. If the trend continues, consider rolling down and out to "
                          "a lower strike at a further expiry to avoid assignment.")
                actions.append("Monitor daily — set price alert at your strike")
                actions.append("Prepare to roll: sell next month, same or lower strike")
            elif snap.pct_to_short_call is not None and snap.pct_to_short_call < 7:
                headline = f"👁 Stock approaching call strike ({snap.pct_to_short_call:.1f}% below)"
                detail = (f"{snap.symbol} is only {snap.pct_to_short_call:.1f}% below your ${snap.short_call_strike or snap.strike} "
                          f"call strike. If the momentum continues, consider rolling up and out to "
                          "avoid having shares called away below your target exit price.")
                actions.append("Monitor daily — set price alert at your strike")
                actions.append("Prepare to roll up: buy current call, sell higher strike next month")

        # Rule 6: HOLD — all good, let theta work
        else:
            level = ADVICE_HOLD
            dte_str = f"{dte} DTE" if dte is not None else "unknown DTE"
            headline = f"✓ On track — {pct:.0f}% captured, {dte_str}"
            detail = (f"Position is performing well. You've captured {pct:.0f}% of the ${premium:.0f} "
                      f"maximum premium with {dte_str} remaining. Let time decay (theta) do its work. "
                      "No action required — check again tomorrow.")
            actions.append(f"Target: close at 50% profit (${premium * 0.5:.0f})")
            if dte and dte > 21:
                actions.append(f"Next review: around {dte - 7} DTE")

        snap.advice_level = level
        snap.advice_headline = headline
        snap.advice_detail = detail
        snap.advice_actions = actions
        return snap

    def _strike_threatened(self, snap: PositionSnapshot) -> bool:
        """True if the stock is within 7% of either short strike."""
        if snap.pct_to_short_put is not None and 0 <= snap.pct_to_short_put < 7:
            return True
        if snap.pct_to_short_call is not None and 0 <= snap.pct_to_short_call < 7:
            return True
        return False

    def summary_report(self, snapshots: List[PositionSnapshot]) -> Dict[str, Any]:
        """
        Aggregate summary of all monitored positions — for dashboard / Telegram.
        """
        total_premium = sum(s.premium_received for s in snapshots)
        total_unrealized = sum(s.unrealized_pnl for s in snapshots)
        urgents = [s for s in snapshots if s.advice_level == ADVICE_URGENT]
        actions = [s for s in snapshots if s.advice_level == ADVICE_ACTION]
        watches = [s for s in snapshots if s.advice_level == ADVICE_WATCH]
        holds   = [s for s in snapshots if s.advice_level == ADVICE_HOLD]

        return {
            'as_of': datetime.now().isoformat(),
            'total_positions': len(snapshots),
            'total_premium_at_risk': round(total_premium, 2),
            'total_unrealized_pnl': round(total_unrealized, 2),
            'overall_pct_captured': round(total_unrealized / total_premium * 100, 1) if total_premium else 0,
            'urgent_count': len(urgents),
            'action_count': len(actions),
            'watch_count': len(watches),
            'hold_count': len(holds),
            'positions': [asdict(s) for s in snapshots],
        }
