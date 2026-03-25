"""
Options Ollie — Data Fetching Layer
Abstracts data sources: yfinance now, MenthorQ when available.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from scipy.stats import norm
import warnings
warnings.filterwarnings('ignore')


class OptionsDataFetcher:
    """Fetches stock and options data. Swap to MenthorQ by subclassing."""

    def __init__(self, source: str = 'yfinance'):
        self.source = source
        self._cache: Dict[str, any] = {}

    def get_stock_info(self, symbol: str) -> Dict:
        """Get current price, volume, market cap, IV rank, etc."""
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        hist = ticker.history(period='1y')

        if hist.empty:
            return {}

        current_price = hist['Close'].iloc[-1]
        avg_volume = hist['Volume'].mean()

        # Calculate historical volatility (annualized)
        returns = np.log(hist['Close'] / hist['Close'].shift(1)).dropna()
        hv_30 = returns.tail(30).std() * np.sqrt(252) if len(returns) >= 30 else returns.std() * np.sqrt(252)
        hv_60 = returns.tail(60).std() * np.sqrt(252) if len(returns) >= 60 else hv_30

        # 52-week range
        high_52w = hist['High'].max()
        low_52w = hist['Low'].min()
        pct_from_high = (current_price - high_52w) / high_52w * 100
        pct_from_low = (current_price - low_52w) / low_52w * 100

        return {
            'symbol': symbol,
            'price': round(current_price, 2),
            'market_cap': info.get('marketCap', 0),
            'avg_volume': int(avg_volume),
            'hv_30': round(hv_30, 4),
            'hv_60': round(hv_60, 4),
            'high_52w': round(high_52w, 2),
            'low_52w': round(low_52w, 2),
            'pct_from_high': round(pct_from_high, 2),
            'pct_from_low': round(pct_from_low, 2),
            'sector': info.get('sector', 'N/A'),
            'name': info.get('shortName', symbol),
            'earnings_date': str(info.get('earningsTimestamp', 'N/A')),
        }

    def get_options_chain(self, symbol: str, min_dte: int = 20, max_dte: int = 50) -> pd.DataFrame:
        """
        Get full options chain filtered by DTE range.
        Returns DataFrame with puts and calls, enriched with Greeks estimates.
        """
        ticker = yf.Ticker(symbol)
        try:
            expirations = ticker.options
        except Exception:
            return pd.DataFrame()

        if not expirations:
            return pd.DataFrame()

        today = datetime.now().date()
        all_chains = []

        for exp_str in expirations:
            exp_date = datetime.strptime(exp_str, '%Y-%m-%d').date()
            dte = (exp_date - today).days

            if dte < min_dte or dte > max_dte:
                continue

            try:
                chain = ticker.option_chain(exp_str)
            except Exception:
                continue

            for opt_type, df in [('call', chain.calls), ('put', chain.puts)]:
                if df.empty:
                    continue

                df = df.copy()
                df['option_type'] = opt_type
                df['expiry'] = exp_str
                df['dte'] = dte
                df['symbol'] = symbol

                # Get current stock price for moneyness calculation
                hist = ticker.history(period='1d')
                if not hist.empty:
                    stock_price = hist['Close'].iloc[-1]
                    df['stock_price'] = stock_price
                    df['moneyness'] = (df['strike'] - stock_price) / stock_price * 100

                    # Estimate missing Greeks using Black-Scholes if needed
                    df = self._enrich_greeks(df, stock_price, dte)

                all_chains.append(df)

        if not all_chains:
            return pd.DataFrame()

        result = pd.concat(all_chains, ignore_index=True)

        # Liquidity filters
        result['bid_ask_spread'] = result['ask'] - result['bid']
        result['bid_ask_pct'] = np.where(
            result['ask'] > 0,
            result['bid_ask_spread'] / result['ask'],
            1.0
        )
        result['mid_price'] = (result['bid'] + result['ask']) / 2

        return result

    def _enrich_greeks(self, df: pd.DataFrame, stock_price: float, dte: int) -> pd.DataFrame:
        """Estimate Greeks using Black-Scholes when not provided by data source."""
        T = dte / 365.0
        r = 0.05  # Risk-free rate approximation

        for idx, row in df.iterrows():
            K = row['strike']
            sigma = row.get('impliedVolatility', 0.3)
            if sigma <= 0 or pd.isna(sigma):
                sigma = 0.3

            try:
                d1 = (np.log(stock_price / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
                d2 = d1 - sigma * np.sqrt(T)

                if row['option_type'] == 'call':
                    delta = norm.cdf(d1)
                    prob_itm = norm.cdf(d2)
                else:
                    delta = norm.cdf(d1) - 1
                    prob_itm = norm.cdf(-d2)

                gamma = norm.pdf(d1) / (stock_price * sigma * np.sqrt(T))
                theta = -(stock_price * norm.pdf(d1) * sigma) / (2 * np.sqrt(T)) / 365
                vega = stock_price * norm.pdf(d1) * np.sqrt(T) / 100

                df.at[idx, 'delta_est'] = round(delta, 4)
                df.at[idx, 'gamma_est'] = round(gamma, 6)
                df.at[idx, 'theta_est'] = round(theta, 4)
                df.at[idx, 'vega_est'] = round(vega, 4)
                df.at[idx, 'prob_itm'] = round(prob_itm, 4)
                df.at[idx, 'prob_otm'] = round(1 - prob_itm, 4)
            except (ZeroDivisionError, ValueError):
                df.at[idx, 'delta_est'] = 0
                df.at[idx, 'gamma_est'] = 0
                df.at[idx, 'theta_est'] = 0
                df.at[idx, 'vega_est'] = 0
                df.at[idx, 'prob_itm'] = 0
                df.at[idx, 'prob_otm'] = 1

        return df

    def get_earnings_calendar(self, symbols: List[str]) -> Dict[str, Optional[str]]:
        """Check upcoming earnings dates — avoid selling options through earnings."""
        earnings = {}
        for sym in symbols:
            try:
                ticker = yf.Ticker(sym)
                cal = ticker.calendar
                if cal is not None and not (isinstance(cal, pd.DataFrame) and cal.empty):
                    if isinstance(cal, dict):
                        ed = cal.get('Earnings Date', [None])
                        earnings[sym] = str(ed[0]) if ed else None
                    else:
                        earnings[sym] = None
                else:
                    earnings[sym] = None
            except Exception:
                earnings[sym] = None
        return earnings

    def get_iv_rank(self, symbol: str, lookback_days: int = 252) -> float:
        """
        Calculate IV Rank: where current IV sits relative to past year's range.
        IV Rank = (Current IV - 52w Low IV) / (52w High IV - 52w Low IV)
        Uses HV as proxy when real IV history isn't available.
        """
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period='1y')
        if hist.empty or len(hist) < 30:
            return 0.5

        # Use rolling 30-day HV as IV proxy
        returns = np.log(hist['Close'] / hist['Close'].shift(1)).dropna()
        rolling_vol = returns.rolling(window=30).std() * np.sqrt(252)
        rolling_vol = rolling_vol.dropna()

        if rolling_vol.empty:
            return 0.5

        current_vol = rolling_vol.iloc[-1]
        min_vol = rolling_vol.min()
        max_vol = rolling_vol.max()

        if max_vol == min_vol:
            return 0.5

        iv_rank = (current_vol - min_vol) / (max_vol - min_vol)
        return round(max(0, min(1, iv_rank)), 4)
