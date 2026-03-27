"""
Options Ollie — Open Interest Structure Analysis
Identifies put/call walls, gamma flip level, and probable price range
using market microstructure derived from options OI data.

Key concepts:
  Put Wall   — strike with the highest put OI cluster.  Dealers who sold these puts
                hedged by buying shares as price fell → acts as a price floor / support.
  Call Wall  — strike with the highest call OI cluster.  Dealers short these calls sold
                shares as price rose → acts as resistance / ceiling.
  Gamma Flip — the strike at which net dealer gamma exposure (GEX) crosses zero.
                Above flip: positive GEX (moves dampened, mean-reversion tendency).
                Below flip: negative GEX (moves amplified, trending tendency).

GEX formula (SpotGamma convention, simplified):
  Net GEX at strike K = (call_OI_K × call_gamma_K - put_OI_K × put_gamma_K) × 100 × spot
  Positive total GEX → dealers long gamma → stabilising.
  Negative total GEX → dealers short gamma → destabilising.
"""

import numpy as np
from scipy.stats import norm
from typing import Dict, List, Optional, Tuple
from datetime import datetime


# ──────────────────────────────────────────────────────────────────────────────
# Black-Scholes helpers (duplicated here so module is self-contained)
# ──────────────────────────────────────────────────────────────────────────────

def _bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Return Black-Scholes gamma (identical for calls and puts)."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    try:
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        return float(norm.pdf(d1) / (S * sigma * np.sqrt(T)))
    except (ZeroDivisionError, ValueError):
        return 0.0


def _bs_iv_bisect(price: float, S: float, K: float, T: float, r: float,
                  opt_type: str = 'call', fallback: float = 0.50) -> float:
    """Back-solve implied vol via bisection (60 iterations, tolerance 0.001)."""
    if T <= 0 or price <= 0:
        return fallback
    lo, hi = 0.01, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        d1 = (np.log(S / K) + (r + 0.5 * mid ** 2) * T) / (mid * np.sqrt(T))
        d2 = d1 - mid * np.sqrt(T)
        if opt_type == 'call':
            theo = S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
        else:
            theo = K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
        if abs(theo - price) < 0.001:
            return round(mid, 4)
        if theo < price:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 4)


# ──────────────────────────────────────────────────────────────────────────────
# Main analysis function
# ──────────────────────────────────────────────────────────────────────────────

def analyze_oi_structure(
    symbol: str,
    fetcher,
    current_price: float,
    hv_30: float = 0.50,
    max_dte: int = 90,
    r: float = 0.05,
) -> Dict:
    """
    Build a market-structure picture from the full options OI landscape.

    Parameters
    ----------
    symbol        : ticker, e.g. 'RDDT'
    fetcher       : OptionsDataFetcher instance
    current_price : live stock price
    hv_30         : 30-day historical vol (used as sigma fallback)
    max_dte       : include all expirations up to this many days out
    r             : risk-free rate

    Returns
    -------
    dict with keys:
      put_wall, call_wall, gamma_flip, total_gex, gex_positive,
      oi_levels (list), coaching (str), range_low, range_high,
      price_relative_to_flip (str), top_put_strikes, top_call_strikes
    """
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    try:
        expirations = ticker.options
    except Exception:
        return _empty_result("Could not fetch options chain")

    today = datetime.now().date()

    # Aggregate OI across all near-term expirations
    # strike → {call_oi, put_oi, call_gamma_x_oi, put_gamma_x_oi}
    strike_map: Dict[float, Dict] = {}
    native_oi_found = False  # tracks whether real OI (non-volume) was available

    for exp_str in expirations:
        try:
            exp_date = datetime.strptime(exp_str, '%Y-%m-%d').date()
        except ValueError:
            continue
        dte = (exp_date - today).days
        if dte < 5 or dte > max_dte:
            continue

        T = dte / 365.0

        try:
            chain = ticker.option_chain(exp_str)
        except Exception:
            continue

        for opt_type, df in [('call', chain.calls), ('put', chain.puts)]:
            if df.empty:
                continue

            for _, row in df.iterrows():
                strike = float(row.get('strike', 0) or 0)
                if strike <= 0:
                    continue

                # yfinance frequently returns OI=0 outside market hours.
                # Fall back to today's volume as a liquidity/interest proxy.
                import math as _math
                _oi_raw = row.get('openInterest', 0)
                _vol_raw = row.get('volume', 0)
                raw_oi = 0 if (_oi_raw is None or (isinstance(_oi_raw, float) and _math.isnan(_oi_raw))) else int(_oi_raw)
                vol = 0 if (_vol_raw is None or (isinstance(_vol_raw, float) and _math.isnan(_vol_raw))) else int(_vol_raw)
                if raw_oi <= 0 and vol <= 0:
                    continue
                if raw_oi > 0:
                    native_oi_found = True
                # Use whichever is larger; volume is a same-day proxy, OI is cumulative
                oi = max(raw_oi, vol)

                # Get best available price for IV estimation
                last_px = float(row.get('lastPrice', 0) or 0)
                mid_px = (float(row.get('bid', 0) or 0) + float(row.get('ask', 0) or 0)) / 2
                price_for_iv = mid_px if mid_px > 0 else last_px

                # Get sigma: validate yfinance IV, back-solve if unreliable
                yf_sigma = float(row.get('impliedVolatility', 0) or 0)
                sigma = None
                if yf_sigma > 0.05 and price_for_iv > 0 and T > 0:
                    # Quick sanity check — does yfinance IV produce a sane price?
                    d1 = (np.log(current_price / strike) + (r + 0.5 * yf_sigma**2) * T) / (yf_sigma * np.sqrt(T))
                    d2 = d1 - yf_sigma * np.sqrt(T)
                    if opt_type == 'call':
                        theo = current_price * norm.cdf(d1) - strike * np.exp(-r * T) * norm.cdf(d2)
                    else:
                        theo = strike * np.exp(-r * T) * norm.cdf(-d2) - current_price * norm.cdf(-d1)
                    if theo > 0 and price_for_iv > 0 and abs(theo - price_for_iv) / price_for_iv < 0.50:
                        sigma = yf_sigma

                if sigma is None:
                    if price_for_iv > 0 and T > 0:
                        sigma = _bs_iv_bisect(price_for_iv, current_price, strike, T, r, opt_type)
                    else:
                        sigma = hv_30 if hv_30 > 0 else 0.50

                gamma = _bs_gamma(current_price, strike, T, r, sigma)
                # GEX contribution: OI × gamma × 100 × spot  (calls positive, puts negative)
                gex_contrib = oi * gamma * 100 * current_price
                if opt_type == 'put':
                    gex_contrib = -gex_contrib

                if strike not in strike_map:
                    strike_map[strike] = {
                        'call_oi': 0, 'put_oi': 0,
                        'call_gex': 0.0, 'put_gex': 0.0,
                    }

                if opt_type == 'call':
                    strike_map[strike]['call_oi'] += oi
                    strike_map[strike]['call_gex'] += gex_contrib
                else:
                    strike_map[strike]['put_oi'] += oi
                    strike_map[strike]['put_gex'] += gex_contrib  # already negated above

    if not strike_map:
        return _empty_result("No OI data found in the options chain")

    # ── Build sorted levels list ─────────────────────────────────────────────
    levels = []
    for strike, d in sorted(strike_map.items()):
        net_gex = d['call_gex'] + d['put_gex']
        levels.append({
            'strike': float(strike),
            'call_oi': int(d['call_oi']),
            'put_oi': int(d['put_oi']),
            'total_oi': int(d['call_oi'] + d['put_oi']),
            'net_gex': round(net_gex / 1e6, 3),  # in $M for readability
        })

    # ── Put wall & call wall ─────────────────────────────────────────────────
    # Focus on strikes within a reasonable range of current price (50% band)
    nearby = [l for l in levels if 0.5 * current_price <= l['strike'] <= 1.5 * current_price]
    if not nearby:
        nearby = levels

    # Put wall: highest put OI below current price (support)
    puts_below = [l for l in nearby if l['strike'] <= current_price and l['put_oi'] > 0]
    call_above = [l for l in nearby if l['strike'] >= current_price and l['call_oi'] > 0]

    put_wall = max(puts_below, key=lambda x: x['put_oi']) if puts_below else None
    call_wall = max(call_above, key=lambda x: x['call_oi']) if call_above else None

    # ── Gamma flip level ─────────────────────────────────────────────────────
    # Sort all nearby strikes and find where cumulative GEX changes sign
    nearby_sorted = sorted(nearby, key=lambda x: x['strike'])
    cumulative_gex = 0.0
    gamma_flip = None
    last_sign = None
    for level in nearby_sorted:
        cumulative_gex += level['net_gex']
        current_sign = 1 if cumulative_gex >= 0 else -1
        if last_sign is not None and current_sign != last_sign:
            gamma_flip = level['strike']
        last_sign = current_sign

    total_gex = sum(l['net_gex'] for l in nearby_sorted)
    gex_positive = total_gex >= 0

    # Price relative to gamma flip
    if gamma_flip:
        if current_price > gamma_flip:
            price_rel = 'above'
        else:
            price_rel = 'below'
    else:
        price_rel = 'at'

    # ── Top 3 put strikes and call strikes for detail display ────────────────
    top_puts = sorted(puts_below, key=lambda x: x['put_oi'], reverse=True)[:3]
    top_calls = sorted(call_above, key=lambda x: x['call_oi'], reverse=True)[:3]

    # ── Inferred price range ─────────────────────────────────────────────────
    range_low = put_wall['strike'] if put_wall else round(current_price * 0.85, 0)
    range_high = call_wall['strike'] if call_wall else round(current_price * 1.15, 0)

    # ── Coaching text ─────────────────────────────────────────────────────────
    coaching = _build_coaching(
        current_price=current_price,
        put_wall=put_wall,
        call_wall=call_wall,
        gamma_flip=gamma_flip,
        price_rel=price_rel,
        gex_positive=gex_positive,
        total_gex=total_gex,
    )

    used_volume_proxy = not native_oi_found

    def _safe_levels(lst):
        """Strip numpy types from a list of level dicts."""
        out = []
        for d in lst:
            out.append({k: (float(v) if hasattr(v, 'item') else v) for k, v in d.items()})
        return out

    return {
        'put_wall': {
            'strike': float(put_wall['strike']),
            'oi': int(put_wall['put_oi']),
            'pct_from_price': float(round((put_wall['strike'] - current_price) / current_price * 100, 1)),
        } if put_wall else None,
        'call_wall': {
            'strike': float(call_wall['strike']),
            'oi': int(call_wall['call_oi']),
            'pct_from_price': float(round((call_wall['strike'] - current_price) / current_price * 100, 1)),
        } if call_wall else None,
        'gamma_flip': float(round(gamma_flip, 2)) if gamma_flip else None,
        'gamma_flip_pct_from_price': (
            float(round((gamma_flip - current_price) / current_price * 100, 1)) if gamma_flip else None
        ),
        'total_gex': float(round(total_gex, 2)),
        'gex_positive': bool(gex_positive),
        'price_relative_to_flip': price_rel,
        'range_low': float(range_low),
        'range_high': float(range_high),
        'top_put_strikes': _safe_levels(top_puts),
        'top_call_strikes': _safe_levels(top_calls),
        'oi_levels': _safe_levels(nearby_sorted),
        'coaching': coaching,
        'data_note': (
            'Volume used as OI proxy (yfinance returns OI=0 outside market hours — '
            'levels shown reflect today\'s trading activity)'
            if used_volume_proxy else
            'Open Interest data from options chain'
        ),
    }


def _build_coaching(
    current_price: float,
    put_wall,
    call_wall,
    gamma_flip,
    price_rel: str,
    gex_positive: bool,
    total_gex: float,
) -> str:
    lines = []

    # --- Range assessment ---
    if put_wall and call_wall:
        width_pct = round((call_wall['strike'] - put_wall['strike']) / current_price * 100, 0)
        lines.append(
            f"OI structure defines a range of ${put_wall['strike']:.0f}–${call_wall['strike']:.0f} "
            f"({width_pct:.0f}% wide) through the near-term expiries. "
        )

    # --- Put wall interpretation ---
    if put_wall:
        contracts_k = round(put_wall.get('put_oi', put_wall.get('oi', 0)) / 1000, 1)
        pct = abs(round((put_wall['strike'] - current_price) / current_price * 100, 1))
        lines.append(
            f"Put wall: ${put_wall['strike']:.0f} ({pct:.0f}% below) carries {contracts_k}K contracts of open interest — "
            f"dealers who sold these puts hedged by buying shares on the way down, "
            f"creating a natural support floor. Watch for a bounce if price reaches this level."
        )

    # --- Call wall interpretation ---
    if call_wall:
        contracts_k = round(call_wall.get('call_oi', call_wall.get('oi', 0)) / 1000, 1)
        pct = abs(round((call_wall['strike'] - current_price) / current_price * 100, 1))
        lines.append(
            f"Call wall: ${call_wall['strike']:.0f} ({pct:.0f}% above) carries {contracts_k}K contracts — "
            f"dealers short these calls hedged by selling shares as price rose, "
            f"creating resistance. Breaking through this level with volume signals a real move."
        )

    # --- Gamma flip / GEX environment ---
    if gamma_flip:
        if price_rel == 'above':
            lines.append(
                f"Gamma flip level: ~${gamma_flip:.0f}. Price is currently ABOVE the flip — "
                f"market makers are net long gamma here. This dampens volatility: "
                f"expect mean-reversion, tight ranges, and 'pinning' near key strikes near expiry. "
                f"Good environment to sell premium (covered calls)."
            )
        else:
            lines.append(
                f"Gamma flip level: ~${gamma_flip:.0f}. Price is currently BELOW the flip — "
                f"market makers are net short gamma here. This amplifies moves in either direction: "
                f"small catalysts can produce outsized price swings. "
                f"Prioritise protection (collars/puts) before adding short premium."
            )
    elif gex_positive:
        lines.append(
            "Net dealer gamma exposure is positive — "
            "the market is in a dampened, mean-reverting regime. "
            "Favourable for selling covered calls."
        )
    else:
        lines.append(
            "Net dealer gamma exposure is negative — "
            "the market is in an amplified, trending regime. "
            "Price moves may be exaggerated. Keep protective puts or collars in place."
        )

    # --- Probability framing ---
    if put_wall and call_wall:
        range_width = call_wall['strike'] - put_wall['strike']
        cushion_below = current_price - put_wall['strike']
        cushion_above = call_wall['strike'] - current_price
        if cushion_below < range_width * 0.25:
            lines.append(
                f"⚠️ Price is close to the put wall (${current_price - put_wall['strike']:.0f} gap). "
                f"A break below ${put_wall['strike']:.0f} with conviction would remove key dealer support "
                f"and could accelerate selling. Consider increasing protective put coverage."
            )
        elif cushion_above < range_width * 0.25:
            lines.append(
                f"Price is near the call wall (${call_wall['strike'] - current_price:.0f} gap). "
                f"A strong move through ${call_wall['strike']:.0f} could trigger a short squeeze "
                f"as dealers cover short hedges. A good level to consider taking CC profits early."
            )
        else:
            lines.append(
                f"Price sits comfortably inside the OI range "
                f"(${cushion_below:.0f} from put wall, ${cushion_above:.0f} from call wall). "
                f"Statistically, this is the highest-probability outcome through near-term expiry — "
                f"price drifts within the range while time decay works in your favour."
            )

    return " ".join(lines)


def _empty_result(reason: str) -> Dict:
    return {
        'put_wall': None,
        'call_wall': None,
        'gamma_flip': None,
        'gamma_flip_pct_from_price': None,
        'total_gex': 0,
        'gex_positive': True,
        'price_relative_to_flip': 'unknown',
        'range_low': None,
        'range_high': None,
        'top_put_strikes': [],
        'top_call_strikes': [],
        'oi_levels': [],
        'coaching': f"Market structure analysis unavailable: {reason}",
    }
