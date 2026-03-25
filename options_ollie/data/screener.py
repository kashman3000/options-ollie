"""
Options Ollie — Options Screener
Scans the watchlist and ranks candidates by strategy suitability.
"""

import pandas as pd
import numpy as np
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
from .fetcher import OptionsDataFetcher
from ..config import RiskProfile, FULL_WATCHLIST


class OptionsScreener:
    """Screens options universe for high-probability trade setups."""

    def __init__(self, fetcher: OptionsDataFetcher, risk: RiskProfile):
        self.fetcher = fetcher
        self.risk = risk

    def screen_wheel_candidates(self, symbols: List[str] = None,
                                 max_stock_price: float = 200.0) -> pd.DataFrame:
        """
        Find best cash-secured put candidates for the wheel strategy.

        Criteria:
        - Stock price affordable for 100-share lots
        - High IV rank (selling premium when IV is elevated)
        - 70%+ probability OTM at target delta
        - Good liquidity (volume, open interest, tight spreads)
        - Avoid earnings within DTE window
        """
        symbols = symbols or FULL_WATCHLIST
        candidates = []

        for sym in symbols:
            try:
                info = self.fetcher.get_stock_info(sym)
                if not info or info['price'] > max_stock_price or info['price'] < 5:
                    continue

                iv_rank = self.fetcher.get_iv_rank(sym)
                chain = self.fetcher.get_options_chain(
                    sym,
                    min_dte=self.risk.min_days_to_expiry,
                    max_dte=self.risk.max_days_to_expiry
                )

                if chain.empty:
                    continue

                # Filter to OTM puts only
                puts = chain[
                    (chain['option_type'] == 'put') &
                    (chain['strike'] < info['price']) &
                    (chain['openInterest'] >= self.risk.min_open_interest) &
                    (chain['volume'] >= self.risk.min_volume) &
                    (chain['bid_ask_pct'] <= self.risk.max_bid_ask_spread_pct) &
                    (chain['prob_otm'] >= self.risk.min_probability_of_profit) &
                    (abs(chain['delta_est']) <= self.risk.target_delta_csp + 0.10) &
                    (abs(chain['delta_est']) >= self.risk.target_delta_csp - 0.10)
                ].copy()

                if puts.empty:
                    continue

                # Score each contract
                for _, put in puts.iterrows():
                    capital_required = put['strike'] * 100
                    premium = put['mid_price'] * 100
                    annualized_return = (premium / capital_required) * (365 / put['dte'])

                    # Composite score: higher is better
                    # Weights: premium yield (40%), probability (30%), IV rank (20%), liquidity (10%)
                    score = (
                        0.40 * min(annualized_return / 0.30, 1.0) +   # Normalize to 30% target
                        0.30 * put['prob_otm'] +
                        0.20 * iv_rank +
                        0.10 * min(put['openInterest'] / 5000, 1.0)
                    )

                    candidates.append({
                        'symbol': sym,
                        'name': info['name'],
                        'stock_price': info['price'],
                        'strike': put['strike'],
                        'expiry': put['expiry'],
                        'dte': put['dte'],
                        'bid': put['bid'],
                        'ask': put['ask'],
                        'mid_price': put['mid_price'],
                        'premium_100': round(premium, 2),
                        'capital_required': round(capital_required, 2),
                        'annualized_return': round(annualized_return * 100, 2),
                        'delta': round(abs(put['delta_est']), 4),
                        'prob_otm': round(put['prob_otm'] * 100, 2),
                        'iv_rank': round(iv_rank * 100, 2),
                        'open_interest': int(put['openInterest']),
                        'volume': int(put['volume']),
                        'bid_ask_pct': round(put['bid_ask_pct'] * 100, 2),
                        'score': round(score, 4),
                        'strategy': 'CSP',
                        'sector': info.get('sector', 'N/A'),
                    })

            except Exception as e:
                continue

        if not candidates:
            return pd.DataFrame()

        df = pd.DataFrame(candidates)
        df = df.sort_values('score', ascending=False).reset_index(drop=True)
        return df

    def screen_covered_call_candidates(self, symbol: str, shares: int,
                                        avg_cost: float = None) -> pd.DataFrame:
        """
        Find best covered call strikes for shares you already own.
        For the wheel: sell calls above your cost basis.
        """
        info = self.fetcher.get_stock_info(symbol)
        if not info:
            return pd.DataFrame()

        cost_basis = avg_cost or info['price']
        chain = self.fetcher.get_options_chain(
            symbol,
            min_dte=self.risk.min_days_to_expiry,
            max_dte=self.risk.max_days_to_expiry
        )

        if chain.empty:
            return pd.DataFrame()

        num_contracts = shares // 100

        # Filter OTM calls above cost basis
        calls = chain[
            (chain['option_type'] == 'call') &
            (chain['strike'] > cost_basis) &
            (chain['openInterest'] >= self.risk.min_open_interest // 2) &  # Slightly relaxed
            (chain['bid'] > 0.10) &
            (abs(chain['delta_est']) <= self.risk.target_delta_cc + 0.10) &
            (abs(chain['delta_est']) >= max(self.risk.target_delta_cc - 0.10, 0.05))
        ].copy()

        if calls.empty:
            # Fallback: relax filters
            calls = chain[
                (chain['option_type'] == 'call') &
                (chain['strike'] > info['price']) &
                (chain['bid'] > 0.05)
            ].copy()

        if calls.empty:
            return pd.DataFrame()

        results = []
        for _, call in calls.iterrows():
            premium = call['mid_price'] * 100 * num_contracts
            upside_to_strike = (call['strike'] - info['price']) / info['price'] * 100
            total_return = (call['mid_price'] + call['strike'] - info['price']) / info['price']
            annualized = total_return * (365 / call['dte'])

            results.append({
                'symbol': symbol,
                'stock_price': info['price'],
                'cost_basis': cost_basis,
                'strike': call['strike'],
                'expiry': call['expiry'],
                'dte': call['dte'],
                'bid': call['bid'],
                'ask': call['ask'],
                'mid_price': call['mid_price'],
                'contracts': num_contracts,
                'total_premium': round(premium, 2),
                'upside_to_strike_pct': round(upside_to_strike, 2),
                'annualized_if_called': round(annualized * 100, 2),
                'delta': round(abs(call['delta_est']), 4),
                'prob_otm': round(call.get('prob_otm', 0) * 100, 2),
                'open_interest': int(call['openInterest']),
                'strategy': 'CC',
            })

        df = pd.DataFrame(results)
        df = df.sort_values('total_premium', ascending=False).reset_index(drop=True)
        return df

    def screen_iron_condors(self, symbols: List[str] = None,
                            min_credit: float = 0.50) -> pd.DataFrame:
        """
        Find iron condor opportunities: sell OTM put spread + OTM call spread.
        Best on range-bound, high-IV underlyings.
        """
        symbols = symbols or ['SPY', 'QQQ', 'IWM', 'AAPL', 'MSFT', 'AMZN', 'GOOGL', 'META']
        candidates = []

        for sym in symbols:
            try:
                info = self.fetcher.get_stock_info(sym)
                if not info:
                    continue

                iv_rank = self.fetcher.get_iv_rank(sym)
                if iv_rank < 0.30:  # Only sell condors when IV is elevated
                    continue

                chain = self.fetcher.get_options_chain(
                    sym,
                    min_dte=self.risk.min_days_to_expiry,
                    max_dte=self.risk.max_days_to_expiry
                )
                if chain.empty:
                    continue

                price = info['price']
                expiries = chain['expiry'].unique()

                for exp in expiries:
                    exp_chain = chain[chain['expiry'] == exp]
                    puts = exp_chain[exp_chain['option_type'] == 'put'].sort_values('strike')
                    calls = exp_chain[exp_chain['option_type'] == 'call'].sort_values('strike')

                    if puts.empty or calls.empty:
                        continue

                    # Short put: ~15-20 delta (higher prob OTM)
                    short_puts = puts[
                        (abs(puts['delta_est']) >= 0.12) &
                        (abs(puts['delta_est']) <= 0.22) &
                        (puts['bid'] > 0.10)
                    ]

                    # Short call: ~15-20 delta
                    short_calls = calls[
                        (abs(calls['delta_est']) >= 0.12) &
                        (abs(calls['delta_est']) <= 0.22) &
                        (calls['bid'] > 0.10)
                    ]

                    if short_puts.empty or short_calls.empty:
                        continue

                    # Pick the best short strikes
                    sp = short_puts.iloc[len(short_puts) // 2]  # Middle of range
                    sc = short_calls.iloc[len(short_calls) // 2]

                    # Long put: 5 delta below short put (wing)
                    wing_width = max(price * 0.03, 2.0)  # ~3% or $2 minimum
                    long_put_strike = sp['strike'] - wing_width
                    long_puts = puts[
                        (puts['strike'] <= long_put_strike + 1) &
                        (puts['strike'] >= long_put_strike - 1)
                    ]

                    # Long call: 5 delta above short call
                    long_call_strike = sc['strike'] + wing_width
                    long_calls = calls[
                        (calls['strike'] <= long_call_strike + 1) &
                        (calls['strike'] >= long_call_strike - 1)
                    ]

                    if long_puts.empty or long_calls.empty:
                        continue

                    lp = long_puts.iloc[-1]  # Closest to target
                    lc = long_calls.iloc[0]

                    # Calculate credit and risk
                    put_spread_credit = sp['mid_price'] - lp['mid_price']
                    call_spread_credit = sc['mid_price'] - lc['mid_price']
                    total_credit = put_spread_credit + call_spread_credit

                    if total_credit < min_credit:
                        continue

                    put_spread_width = sp['strike'] - lp['strike']
                    call_spread_width = lc['strike'] - sc['strike']
                    max_risk = max(put_spread_width, call_spread_width) * 100 - total_credit * 100

                    if max_risk <= 0:
                        continue

                    pop_estimate = min(sp.get('prob_otm', 0.8), sc.get('prob_otm', 0.8))
                    return_on_risk = total_credit * 100 / max_risk
                    annualized_ror = return_on_risk * (365 / sp['dte'])

                    candidates.append({
                        'symbol': sym,
                        'stock_price': price,
                        'expiry': exp,
                        'dte': int(sp['dte']),
                        'short_put': sp['strike'],
                        'long_put': lp['strike'],
                        'short_call': sc['strike'],
                        'long_call': lc['strike'],
                        'put_credit': round(put_spread_credit, 2),
                        'call_credit': round(call_spread_credit, 2),
                        'total_credit': round(total_credit, 2),
                        'total_credit_100': round(total_credit * 100, 2),
                        'max_risk': round(max_risk, 2),
                        'return_on_risk_pct': round(return_on_risk * 100, 2),
                        'annualized_ror_pct': round(annualized_ror * 100, 2),
                        'prob_profit_est': round(pop_estimate * 100, 2),
                        'iv_rank': round(iv_rank * 100, 2),
                        'strategy': 'IC',
                    })

            except Exception as e:
                continue

        if not candidates:
            return pd.DataFrame()

        df = pd.DataFrame(candidates)
        df = df.sort_values('return_on_risk_pct', ascending=False).reset_index(drop=True)
        return df

    def screen_credit_spreads(self, symbols: List[str] = None,
                               spread_type: str = 'put') -> pd.DataFrame:
        """
        Screen for bull put spreads (default) or bear call spreads.
        Simpler than iron condors — one directional bias.
        """
        symbols = symbols or FULL_WATCHLIST[:15]  # Top 15 for speed
        candidates = []

        for sym in symbols:
            try:
                info = self.fetcher.get_stock_info(sym)
                if not info:
                    continue

                iv_rank = self.fetcher.get_iv_rank(sym)
                chain = self.fetcher.get_options_chain(
                    sym,
                    min_dte=self.risk.min_days_to_expiry,
                    max_dte=self.risk.max_days_to_expiry
                )
                if chain.empty:
                    continue

                price = info['price']
                opt_type = 'put' if spread_type == 'put' else 'call'
                options = chain[chain['option_type'] == opt_type].copy()

                if options.empty:
                    continue

                for exp in options['expiry'].unique():
                    exp_opts = options[options['expiry'] == exp].sort_values('strike')

                    if spread_type == 'put':
                        # Bull put spread: sell higher put, buy lower put
                        short_opts = exp_opts[
                            (abs(exp_opts['delta_est']) >= 0.15) &
                            (abs(exp_opts['delta_est']) <= 0.30) &
                            (exp_opts['bid'] > 0.10)
                        ]
                    else:
                        # Bear call spread: sell lower call, buy higher call
                        short_opts = exp_opts[
                            (abs(exp_opts['delta_est']) >= 0.15) &
                            (abs(exp_opts['delta_est']) <= 0.30) &
                            (exp_opts['bid'] > 0.10)
                        ]

                    if short_opts.empty:
                        continue

                    short = short_opts.iloc[len(short_opts) // 2]
                    width = max(price * 0.025, 1.0)

                    if spread_type == 'put':
                        long_strike = short['strike'] - width
                        long_opts = exp_opts[
                            (exp_opts['strike'] <= long_strike + 0.5) &
                            (exp_opts['strike'] >= long_strike - 0.5)
                        ]
                    else:
                        long_strike = short['strike'] + width
                        long_opts = exp_opts[
                            (exp_opts['strike'] <= long_strike + 0.5) &
                            (exp_opts['strike'] >= long_strike - 0.5)
                        ]

                    if long_opts.empty:
                        continue

                    long = long_opts.iloc[0]
                    credit = short['mid_price'] - long['mid_price']
                    spread_width = abs(short['strike'] - long['strike'])
                    max_risk = (spread_width - credit) * 100

                    if credit <= 0.10 or max_risk <= 0:
                        continue

                    ror = credit * 100 / max_risk

                    candidates.append({
                        'symbol': sym,
                        'stock_price': price,
                        'strategy': 'Bull Put Spread' if spread_type == 'put' else 'Bear Call Spread',
                        'expiry': exp,
                        'dte': int(short['dte']),
                        'short_strike': short['strike'],
                        'long_strike': long['strike'],
                        'credit': round(credit, 2),
                        'credit_100': round(credit * 100, 2),
                        'max_risk': round(max_risk, 2),
                        'return_on_risk_pct': round(ror * 100, 2),
                        'prob_otm': round(short.get('prob_otm', 0.75) * 100, 2),
                        'iv_rank': round(iv_rank * 100, 2),
                        'delta': round(abs(short['delta_est']), 4),
                    })

            except Exception:
                continue

        if not candidates:
            return pd.DataFrame()

        df = pd.DataFrame(candidates)
        df = df.sort_values('return_on_risk_pct', ascending=False).reset_index(drop=True)
        return df
