"""
Options Ollie — Next Best Action Intelligence Engine

Synthesises all available signals (IV rank, OI structure, Greeks, P&L,
cost basis proximity) into a single prioritised recommendation with
plain-English coaching and optional Gemini-powered narrative.

Scoring model (additive):
  Each signal contributes points to one or more action buckets:
    SELL_CC, BUY_PROTECTION, COLLAR, HOLD_WAIT, ASX_HOLD
  The highest-scoring bucket wins.  Signals are surfaced to the UI as badges.
"""

import urllib.request
import urllib.error
import json
import math
from typing import Dict, List, Optional, Tuple


# ──────────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────────

def next_best_action(rec: Dict, gemini_key: str = None) -> Dict:
    """
    Given a full recommendation dict from WheelManager.recommend_action(),
    return a 'next_best_action' dict suitable for rendering the hero card.

    Parameters
    ----------
    rec        : The recommendation dict from wheel.py
    gemini_key : Optional Google Gemini API key for enhanced narrative

    Returns
    -------
    {
        action_type   : str   — SELL_CC | BUY_PROTECTION | COLLAR | HOLD_WAIT | ASX_HOLD
        confidence    : int   — 0–100
        headline      : str   — Short one-liner e.g. "Sell Covered Call"
        icon          : str   — Emoji
        color         : str   — green | orange | red | blue | grey
        signals       : list  — [{label, value, color, tooltip}]
        reasoning     : str   — 3-5 sentence plain-English explanation
        education     : str   — Why this strategy works in these conditions
        specific_trade: dict  — Best specific trade to place right now (or {})
        score_breakdown: dict — Raw scores for debugging / transparency
    }
    """
    is_asx = rec.get('is_asx', False)
    # Only fall back to ASX_HOLD if we genuinely have no options data.
    # If IBKR provided a chain, run the normal scoring engine.
    if is_asx and not rec.get('top_covered_calls') and not rec.get('protective_strategies'):
        return _asx_result(rec)

    signals, score_breakdown = _score_signals(rec)
    action_type = _pick_action_type(score_breakdown, rec)
    confidence = _calc_confidence(score_breakdown, action_type)
    specific_trade = _pick_specific_trade(action_type, rec)
    headline, icon, color = _headline(action_type, rec, specific_trade)
    reasoning = _build_reasoning(action_type, rec, signals, specific_trade)
    education = _education_blurb(action_type, rec)

    # Template-based section coaching (always available)
    cc_coaching = _build_cc_coaching(rec, specific_trade, action_type)
    risk_narrative = _build_risk_narrative(rec, specific_trade, action_type)

    # Optionally enhance ALL coaching text with Gemini in one call
    if gemini_key and len(gemini_key) > 10:
        try:
            enhanced = _gemini_enhance_structured(
                reasoning, cc_coaching, risk_narrative,
                action_type, rec, signals, gemini_key
            )
            if enhanced:
                reasoning = enhanced.get('reasoning', reasoning)
                cc_coaching = enhanced.get('cc_coaching', cc_coaching)
                risk_narrative = enhanced.get('risk_narrative', risk_narrative)
        except Exception:
            pass  # graceful degradation — template coaching still used

    return {
        'action_type': action_type,
        'confidence': confidence,
        'headline': headline,
        'icon': icon,
        'color': color,
        'signals': signals,
        'reasoning': reasoning,
        'education': education,
        'specific_trade': specific_trade,
        'score_breakdown': score_breakdown,
        'cc_coaching': cc_coaching,
        'risk_narrative': risk_narrative,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Signal scoring
# ──────────────────────────────────────────────────────────────────────────────

def _score_signals(rec: Dict) -> Tuple[List[Dict], Dict]:
    """
    Score every available signal.  Returns:
      - signals: list of UI badge dicts
      - score_breakdown: {action_type: score}
    """
    scores = {'SELL_CC': 0, 'BUY_PROTECTION': 0, 'COLLAR': 0, 'HOLD_WAIT': 0, 'PREPARE_ASSIGNMENT': 0}
    signals = []

    iv_rank = rec.get('iv_rank', 0) or 0           # 0-100
    current_price = rec.get('current_price', 0) or 0
    cost_basis = rec.get('cost_basis', current_price) or current_price
    contracts = rec.get('contracts_available', 0) or 0
    ms = rec.get('market_structure', {}) or {}
    top_ccs = rec.get('top_covered_calls', []) or []
    protective = rec.get('protective_strategies', []) or []
    collars = rec.get('collar_strategies', []) or []
    ra = rec.get('risk_analysis', {}) or {}

    # ── Signal 1: IV Rank (3-month percentile) ───────────────────────────────
    vrop      = rec.get('vrop', None)        # IV - HV30, in percentage points
    current_iv = rec.get('current_iv', 0) or 0
    hv30       = rec.get('hv30', 0) or 0

    if iv_rank >= 60:
        scores['SELL_CC'] += 35
        scores['COLLAR'] += 10
        sig = ('IV Rank', f'{iv_rank:.0f}%',  'green',
               f'High IV — in the top 40% of its 3-month range ({current_iv:.0f}% IV). Premium is rich. Best time to sell options.')
    elif iv_rank >= 40:
        scores['SELL_CC'] += 22
        scores['COLLAR'] += 8
        sig = ('IV Rank', f'{iv_rank:.0f}%', 'green',
               f'Elevated IV ({current_iv:.0f}%) — above-average premium available over the past 3 months.')
    elif iv_rank >= 25:
        scores['SELL_CC'] += 12
        scores['COLLAR'] += 5
        scores['HOLD_WAIT'] += 5
        sig = ('IV Rank', f'{iv_rank:.0f}%', 'blue',
               f'Average IV ({current_iv:.0f}%) — moderate premium. Covered calls still viable.')
    elif iv_rank >= 10:
        scores['SELL_CC'] += 5
        scores['BUY_PROTECTION'] += 12
        scores['HOLD_WAIT'] += 10
        sig = ('IV Rank', f'{iv_rank:.0f}%', 'orange',
               f'Below-average IV ({current_iv:.0f}%) — thin premiums vs recent range, but cheap to buy protection.')
    else:
        scores['BUY_PROTECTION'] += 20
        scores['HOLD_WAIT'] += 15
        scores['SELL_CC'] -= 5
        sig = ('IV Rank', f'{iv_rank:.0f}%', 'red',
               f'Low IV ({current_iv:.0f}%) — premiums compressed vs recent range. Protect position or wait for vol.')
    signals.append({'label': sig[0], 'value': sig[1], 'color': sig[2], 'tooltip': sig[3]})

    # ── Signal 1b: Volatility Risk Premium ───────────────────────────────────
    # VRP = IV - HV30. Positive = market pricing in more fear than realised.
    # This is the real signal for whether selling premium is attractive —
    # independent of IV rank (which only measures IV vs its own recent range).
    if vrop is not None and hv30 > 0:
        if vrop >= 15:
            scores['SELL_CC'] += 18
            scores['COLLAR'] += 6
            signals.append({'label': 'VRP', 'value': f'+{vrop:.1f}pp',  'color': 'green',
                             'tooltip': f'IV ({current_iv:.0f}%) is {vrop:.1f}pp above realised vol (HV30 {hv30:.0f}%). '
                                        f'The market is over-pricing risk — selling premium has a strong statistical edge.'})
        elif vrop >= 5:
            scores['SELL_CC'] += 8
            signals.append({'label': 'VRP', 'value': f'+{vrop:.1f}pp', 'color': 'blue',
                             'tooltip': f'IV ({current_iv:.0f}%) is modestly above HV30 ({hv30:.0f}%). '
                                        f'Positive volatility risk premium — selling is slightly favoured.'})
        elif vrop >= -5:
            signals.append({'label': 'VRP', 'value': f'{vrop:+.1f}pp', 'color': 'orange',
                             'tooltip': f'IV ({current_iv:.0f}%) is roughly in line with HV30 ({hv30:.0f}%). '
                                        f'No strong edge for buyers or sellers right now.'})
        else:
            scores['BUY_PROTECTION'] += 10
            scores['SELL_CC'] -= 8
            signals.append({'label': 'VRP', 'value': f'{vrop:+.1f}pp', 'color': 'red',
                             'tooltip': f'IV ({current_iv:.0f}%) is BELOW HV30 ({hv30:.0f}%). '
                                        f'The market is under-pricing risk — selling premium has negative edge. Consider buying protection instead.'})

    # ── Signal 2: Position vs Cost Basis ─────────────────────────────────────
    if cost_basis > 0:
        pct_from_basis = (current_price - cost_basis) / cost_basis * 100
    else:
        pct_from_basis = 0

    if pct_from_basis >= 15:
        scores['SELL_CC'] += 28
        sig = ('vs Basis', f'+{pct_from_basis:.0f}%', 'green',
               f'Well above cost basis — selling CCs carries no risk of loss if called away.')
    elif pct_from_basis >= 5:
        scores['SELL_CC'] += 18
        sig = ('vs Basis', f'+{pct_from_basis:.0f}%', 'green',
               'Comfortably above cost basis — selling CCs above strike is safe.')
    elif pct_from_basis >= -2:
        scores['SELL_CC'] += 5
        scores['BUY_PROTECTION'] += 12
        scores['COLLAR'] += 15
        sig = ('vs Basis', f'{pct_from_basis:+.0f}%', 'orange',
               'Near breakeven — a collar protects downside while keeping upside open.')
    elif pct_from_basis >= -10:
        scores['SELL_CC'] -= 10
        scores['BUY_PROTECTION'] += 22
        scores['COLLAR'] += 20
        sig = ('vs Basis', f'{pct_from_basis:+.0f}%', 'red',
               'Below cost basis — selling CCs here would lock in a loss if called away. Protect first.')
    else:
        scores['SELL_CC'] -= 25
        scores['BUY_PROTECTION'] += 28
        scores['COLLAR'] += 15
        scores['HOLD_WAIT'] += 10
        sig = ('vs Basis', f'{pct_from_basis:+.0f}%', 'red',
               f'Significantly below cost basis ({pct_from_basis:.0f}%). Protection or patience required.')
    signals.append({'label': sig[0], 'value': sig[1], 'color': sig[2], 'tooltip': sig[3]})

    # ── Signal 3: GEX Regime ─────────────────────────────────────────────────
    if ms and ms.get('coaching'):
        gex_positive = ms.get('gex_positive', True)
        gamma_flip = ms.get('gamma_flip')
        price_rel = ms.get('price_relative_to_flip', 'unknown')

        if gex_positive:
            scores['SELL_CC'] += 18
            scores['COLLAR'] += 5
            sig = ('GEX', 'Positive', 'green',
                   'Positive dealer gamma — moves are dampened, mean-reversion favours CC sellers.')
        else:
            scores['BUY_PROTECTION'] += 22
            scores['COLLAR'] += 18
            scores['SELL_CC'] -= 8
            sig = ('GEX', 'Negative', 'red',
                   'Negative dealer gamma — moves are amplified. Higher risk for naked premium sellers.')
        signals.append({'label': sig[0], 'value': sig[1], 'color': sig[2], 'tooltip': sig[3]})

        # Sub-signal: put wall proximity
        put_wall = ms.get('put_wall')
        if put_wall and current_price > 0:
            pw_pct = abs(put_wall.get('pct_from_price', 0) or 0)
            if pw_pct <= 5:
                scores['SELL_CC'] += 10
                signals.append({'label': 'Put Wall', 'value': f"${put_wall['strike']:.0f} ({pw_pct:.0f}% below)",
                                 'color': 'green', 'tooltip': 'Strong OI support floor nearby — downside buffered.'})
            elif pw_pct <= 10:
                scores['SELL_CC'] += 5
                signals.append({'label': 'Put Wall', 'value': f"${put_wall['strike']:.0f} ({pw_pct:.0f}% below)",
                                 'color': 'blue', 'tooltip': 'Moderate OI support floor below current price.'})

        # Sub-signal: call wall above strike
        call_wall = ms.get('call_wall')
        if call_wall and current_price > 0:
            cw_pct = abs(call_wall.get('pct_from_price', 0) or 0)
            if cw_pct <= 5:
                scores['HOLD_WAIT'] += 8  # resistance very close — stock may struggle
                signals.append({'label': 'Call Wall', 'value': f"${call_wall['strike']:.0f} ({cw_pct:.0f}% above)",
                                 'color': 'orange',
                                 'tooltip': 'Heavy call OI resistance very close above — stock may stall here. Good CC target strike.'})
            elif cw_pct <= 15:
                scores['SELL_CC'] += 5
                signals.append({'label': 'Call Wall', 'value': f"${call_wall['strike']:.0f} ({cw_pct:.0f}% above)",
                                 'color': 'blue',
                                 'tooltip': 'Call wall resistance above — use as target for CC strike.'})

    # ── Signal 4: Best CC Quality ─────────────────────────────────────────────
    if top_ccs:
        best = top_ccs[0]
        pop = best.get('pop', 0) or 0
        prob_touch = best.get('prob_touch', 0) or 0
        ann_ret = best.get('annualized_if_called', 0) or 0

        if pop >= 75:
            scores['SELL_CC'] += 25
            sig = ('Best POP', f'{pop:.0f}%', 'green',
                   f'High Probability of Profit — {pop:.0f}% chance this CC expires worthless and you keep all premium.')
        elif pop >= 60:
            scores['SELL_CC'] += 15
            sig = ('Best POP', f'{pop:.0f}%', 'blue',
                   f'Good POP — {pop:.0f}% chance of full profit. Acceptable risk.')
        else:
            scores['SELL_CC'] += 5
            sig = ('Best POP', f'{pop:.0f}%', 'orange',
                   f'Marginal POP ({pop:.0f}%). Strike may be too close — consider wider.')
        signals.append({'label': sig[0], 'value': sig[1], 'color': sig[2], 'tooltip': sig[3]})

        pt_color = 'green' if prob_touch <= 35 else ('orange' if prob_touch <= 55 else 'red')
        signals.append({
            'label': 'Prob Touch', 'value': f'{prob_touch:.0f}%', 'color': pt_color,
            'tooltip': f'Chance the strike is touched before expiry (≈2×ProbITM). '
                       f'{"Low — tight stops can hold." if prob_touch <= 35 else "High — set stop-loss wide or avoid tight risk." if prob_touch > 55 else "Moderate."}'
        })

        if ann_ret >= 20:
            scores['SELL_CC'] += 10
            signals.append({'label': 'Ann Return', 'value': f'{ann_ret:.0f}%', 'color': 'green',
                             'tooltip': f'Annualised return if called away: {ann_ret:.0f}%. Excellent yield.'})
        elif ann_ret >= 12:
            scores['SELL_CC'] += 5
            signals.append({'label': 'Ann Return', 'value': f'{ann_ret:.0f}%', 'color': 'blue',
                             'tooltip': f'Annualised return if called away: {ann_ret:.0f}%. Solid income.'})

    elif contracts < 1:
        scores['SELL_CC'] -= 50  # can't sell — not enough shares
        signals.append({'label': 'Contracts', 'value': '0 lots', 'color': 'red',
                         'tooltip': 'Need at least 100 shares to sell 1 covered call contract.'})

    # ── Signal 5: Downside scenario risk ─────────────────────────────────────
    scenarios = ra.get('scenarios', []) or []
    breach_count = sum(1 for s in scenarios if s.get('below_cost_basis', False))
    if breach_count == 0:
        scores['SELL_CC'] += 8
        signals.append({'label': 'Risk Scenarios', 'value': '0/5 breach basis', 'color': 'green',
                         'tooltip': 'None of the 5 downside scenarios breach your cost basis. Low risk profile.'})
    elif breach_count <= 2:
        signals.append({'label': 'Risk Scenarios', 'value': f'{breach_count}/5 breach basis', 'color': 'orange',
                         'tooltip': f'{breach_count} of 5 downside scenarios breach your cost basis. Manage position carefully.'})
    else:
        scores['BUY_PROTECTION'] += 12
        scores['SELL_CC'] -= 5
        signals.append({'label': 'Risk Scenarios', 'value': f'{breach_count}/5 breach basis', 'color': 'red',
                         'tooltip': f'{breach_count} of 5 downside scenarios would result in a loss vs cost basis. Consider protecting the position.'})

    # ── Signal 6: Collar quality ──────────────────────────────────────────────
    if collars:
        best_collar = collars[0]
        net_credit = best_collar.get('net_credit', 0) or 0
        if net_credit > 0:
            scores['COLLAR'] += 15
            signals.append({'label': 'Collar', 'value': f'+${net_credit:.0f} credit', 'color': 'green',
                             'tooltip': 'A net-credit collar is available — earns income AND defines risk. Efficient hedge.'})

    # ── Signal 7: Wheel cycle phase (F1) ─────────────────────────────────────
    wc = rec.get('wheel_cycle', {}) or {}
    if wc.get('has_data'):
        phase = wc.get('phase', 'NONE')
        if phase == 'CSP':
            # In CSP phase — check if price is approaching strike
            open_trades = wc.get('open_trades', []) or []
            for ot in open_trades:
                strike = ot.get('strike')
                if strike and current_price and strike > 0:
                    pct_to_strike = (strike - current_price) / current_price * 100
                    if pct_to_strike <= 3:
                        # Price within 3% of put strike — high assignment risk
                        scores['PREPARE_ASSIGNMENT'] += 80
                        signals.append({'label': 'Wheel Phase', 'value': 'CSP → Near Strike', 'color': 'orange',
                                        'tooltip': f'In CSP phase, stock is within {pct_to_strike:.1f}% of your ${strike} put strike. Prepare your CC ladder for if assigned.'})
                    elif pct_to_strike <= 8:
                        scores['PREPARE_ASSIGNMENT'] += 40
                        scores['HOLD_WAIT'] += 15
                        signals.append({'label': 'Wheel Phase', 'value': f'CSP — {pct_to_strike:.0f}% buffer', 'color': 'blue',
                                        'tooltip': f'CSP has a {pct_to_strike:.0f}% buffer to the ${strike} strike. Monitor closely as stock approaches.'})
                    else:
                        signals.append({'label': 'Wheel Phase', 'value': 'CSP Active', 'color': 'green',
                                        'tooltip': f'Selling put phase. Strike ${strike} has good distance from current price.'})
        elif phase == 'CC':
            signals.append({'label': 'Wheel Phase', 'value': 'CC Active', 'color': 'blue',
                            'tooltip': 'In covered call phase of the wheel. Premium is being collected against your shares.'})
        elif phase == 'SHARES':
            scores['SELL_CC'] += 10
            signals.append({'label': 'Wheel Phase', 'value': 'Shares — Sell CC', 'color': 'orange',
                            'tooltip': 'You hold shares but have no CC open. Next wheel step is to sell a covered call.'})
        elif phase == 'READY':
            signals.append({'label': 'Wheel Phase', 'value': 'Ready — New Cycle', 'color': 'green',
                            'tooltip': f'{wc.get("completed_cycles", 0)} completed wheel cycles. Ready to start the next.'})

    # ── Signal 8: Open CC + protective put awareness ──────────────────────────
    # Detect existing covered calls and protective puts from the ledger.
    # Cases:
    #   CC open only   → suppress SELL_CC (already sold), nudge HOLD
    #   Puts open only → suppress BUY_PROTECTION (already bought), nudge HOLD
    #   Both (collar)  → hard-suppress both, strong HOLD — position is fully managed
    open_puts = wc.get('open_protective_puts', []) if wc.get('has_data') else []
    active_puts = [p for p in (open_puts or []) if (p.get('dte') or 0) > 0]

    open_cc_trades = wc.get('open_trades', []) if wc.get('has_data') else []
    active_ccs = [c for c in (open_cc_trades or []) if (c.get('dte') or 0) > 0]

    has_active_puts = bool(active_puts)
    has_active_ccs = bool(active_ccs)

    if has_active_ccs and has_active_puts:
        # ── Collar: both legs open — this is a managed position, just hold ──
        scores['SELL_CC'] -= 999
        scores['BUY_PROTECTION'] -= 999
        scores['COLLAR'] -= 999          # don't suggest opening another collar either
        scores['HOLD_WAIT'] += 60
        for c in active_ccs:
            signals.append({'label': '✅ CC Open', 'value': f'${c.get("strike")} Call ({c.get("dte")}d left)', 'color': 'blue',
                             'tooltip': f'You already have a covered call at ${c.get("strike")} expiring in {c.get("dte")} days. No new CC needed.'})
        for p in active_puts:
            signals.append({'label': '✅ Put Open', 'value': f'${p.get("strike")} Put ({p.get("dte")}d left)', 'color': 'green',
                             'tooltip': f'You already hold {p.get("quantity", 1)}× ${p.get("strike")} put(s) expiring in {p.get("dte")} days.'})
        signals.append({'label': '🔒 Collar Active', 'value': 'Fully Hedged', 'color': 'green',
                         'tooltip': 'Both a covered call and protective put are open. The position is collared — upside capped, downside floored. Hold both legs and monitor for roll opportunities.'})

    elif has_active_ccs:
        # ── CC open, no puts — suppress selling another CC ────────────────
        scores['SELL_CC'] -= 999
        scores['HOLD_WAIT'] += 20
        for c in active_ccs:
            signals.append({'label': '✅ CC Open', 'value': f'${c.get("strike")} Call ({c.get("dte")}d left)', 'color': 'blue',
                             'tooltip': f'You already have a covered call at ${c.get("strike")} expiring in {c.get("dte")} days. No new CC can be sold until this expires or is rolled.'})

    elif has_active_puts:
        # ── Puts open, no CC — suppress buying more puts ──────────────────
        scores['BUY_PROTECTION'] -= 999
        scores['HOLD_WAIT'] += 20
        for p in active_puts:
            signals.append({'label': '✅ Protected', 'value': f'${p.get("strike")} Put ({p.get("dte")}d left)', 'color': 'green',
                             'tooltip': f'You already hold {p.get("quantity", 1)}× ${p.get("strike")} put(s) expiring in {p.get("dte")} days. Downside is covered — no additional protection needed.'})

    # ── Signal 9: Earnings Blackout (F4) ─────────────────────────────────────
    earnings_date = rec.get('earnings_date')
    earnings_days = rec.get('earnings_days_away')
    best_cc_expiry = (top_ccs[0].get('expiry', '') if top_ccs else '')
    earnings_within_best_cc = False
    if earnings_date and earnings_days is not None and best_cc_expiry:
        # Check if earnings fall within the window of the recommended CC expiry
        try:
            from datetime import date as _ed
            ed = _ed.fromisoformat(earnings_date)
            exp_d = _ed.fromisoformat(best_cc_expiry)
            earnings_within_best_cc = ed <= exp_d
        except Exception:
            pass

    if earnings_days is not None and earnings_days <= 14:
        # Hard downgrade — earnings within 14 days is dangerous for short options
        scores['SELL_CC'] -= 40
        scores['HOLD_WAIT'] += 30
        signals.append({'label': '⚠️ Earnings', 'value': f'{earnings_days}d away', 'color': 'red',
                        'tooltip': f'Earnings in {earnings_days} days ({earnings_date}). IV will spike before/collapse after. Do NOT sell new short options through an earnings event — risk of massive move.'})
    elif earnings_days is not None and earnings_days <= 21:
        scores['SELL_CC'] -= 15
        scores['HOLD_WAIT'] += 15
        signals.append({'label': 'Earnings', 'value': f'{earnings_days}d away', 'color': 'orange',
                        'tooltip': f'Earnings in {earnings_days} days ({earnings_date}). Avoid selling options that expire through the earnings date. Select a strike that expires before.'})
    elif earnings_days is not None:
        signals.append({'label': 'Earnings', 'value': f'{earnings_days}d away', 'color': 'green',
                        'tooltip': f'Next earnings: {earnings_date} — {earnings_days} days away. Safe to sell options expiring well before this date.'})

    # ── Signal 10: Ex-Dividend / Early Assignment Risk ────────────────────────
    # For CC holders: if ex-div falls inside the open CC's DTE, the call buyer
    # may exercise early to capture the dividend — stripping your remaining theta.
    # For CSP / new CC: if ex-div falls inside the recommended expiry window,
    # the stock will drop ~dividend on that date, nudging it toward the strike.
    exdiv_date   = rec.get('exdiv_date')
    exdiv_days   = rec.get('exdiv_days_away')
    dividend_amt = rec.get('dividend_amount', 0) or 0

    if exdiv_days is not None and exdiv_days >= 0:
        # Check if ex-div is within the best CC expiry window
        exdiv_in_cc_window = False
        if best_cc_expiry and exdiv_date:
            try:
                from datetime import date as _exd
                exdiv_d = _exd.fromisoformat(str(exdiv_date))
                exp_d   = _exd.fromisoformat(best_cc_expiry)
                exdiv_in_cc_window = exdiv_d <= exp_d
            except Exception:
                pass

        if exdiv_in_cc_window and active_ccs:
            # CC is open AND ex-div is before expiry — early assignment risk
            scores['SELL_CC'] -= 20
            scores['HOLD_WAIT'] += 15
            signals.append({'label': '⚠️ Ex-Div Risk', 'value': f'{exdiv_days}d away',
                             'color': 'orange',
                             'tooltip': f'Ex-dividend in {exdiv_days} days (${dividend_amt:.2f}/share). '
                                        f'Your open CC expires after the ex-date — the call buyer may exercise '
                                        f'early to capture the dividend, causing unexpected assignment and '
                                        f'cutting off your remaining theta income.'})
        elif exdiv_days <= 30:
            signals.append({'label': 'Ex-Div', 'value': f'{exdiv_days}d away',
                             'color': 'blue' if exdiv_days > 14 else 'orange',
                             'tooltip': f'Ex-dividend date is {exdiv_days} days away (${dividend_amt:.2f}/share). '
                                        f'Stock will drop by ~${dividend_amt:.2f} on this date. '
                                        f'{"Ensure any CC you sell expires before the ex-date to avoid early assignment risk." if exdiv_days <= 14 else "Monitor as it approaches."}'})

    return signals, scores


# ──────────────────────────────────────────────────────────────────────────────
# Decision logic
# ──────────────────────────────────────────────────────────────────────────────

def _pick_action_type(scores: Dict, rec: Dict) -> str:
    """Select the winning action type from the score breakdown."""
    # Hard overrides
    contracts = rec.get('contracts_available', 0) or 0
    if contracts < 1 and not rec.get('top_covered_calls'):
        # Can't sell any option — eliminate CC
        scores['SELL_CC'] = -999

    # ASX_HOLD only if genuinely no options data (IB Gateway not connected)
    if rec.get('is_asx', False) and not rec.get('top_covered_calls') and not rec.get('protective_strategies'):
        return 'ASX_HOLD'

    # PREPARE_ASSIGNMENT only wins if it was explicitly triggered by wheel cycle signal
    # (i.e. score >= 80). Otherwise suppress it so it doesn't win on noise.
    if scores.get('PREPARE_ASSIGNMENT', 0) < 80:
        scores['PREPARE_ASSIGNMENT'] = -999

    best = max(scores, key=scores.get)

    # If best is SELL_CC but there are no actual candidates → fallback
    if best == 'SELL_CC' and not rec.get('top_covered_calls'):
        scores['SELL_CC'] = -999
        best = max(scores, key=scores.get)

    return best


def _calc_confidence(scores: Dict, winner: str) -> int:
    """
    Convert score gap between winner and runner-up into a 0-100 confidence.
    Gap of 20+ → 85-100, 10-20 → 65-84, 5-10 → 50-64, <5 → 40-50
    """
    valid = {k: v for k, v in scores.items() if v > -900}
    if not valid:
        return 50
    sorted_scores = sorted(valid.values(), reverse=True)
    winner_score = valid.get(winner, 0)
    runner_up = sorted_scores[1] if len(sorted_scores) > 1 else 0
    gap = winner_score - runner_up

    if gap >= 30:
        conf = min(95, 85 + (gap - 30) // 3)
    elif gap >= 18:
        conf = 75 + (gap - 18) // 2
    elif gap >= 8:
        conf = 62 + (gap - 8)
    else:
        conf = max(40, 55 + gap)

    return int(conf)


def _pick_specific_trade(action_type: str, rec: Dict) -> Dict:
    """Return the best specific trade dict for the chosen action."""
    if action_type == 'SELL_CC':
        ccs = rec.get('top_covered_calls', []) or []
        if ccs:
            return {'type': 'covered_call', **ccs[0]}
    elif action_type == 'BUY_PROTECTION':
        prots = rec.get('protective_strategies', []) or []
        # Prefer 10-15% floor protection
        best = next((p for p in prots if p.get('floor_pct') in (10, 15)), None)
        if best is None and prots:
            best = prots[0]
        if best:
            return {'type': 'protective_put', **best}
    elif action_type == 'COLLAR':
        collars = rec.get('collar_strategies', []) or []
        if collars:
            return {'type': 'collar', **collars[0]}
    return {}


def _headline(action_type: str, rec: Dict, trade: Dict) -> Tuple[str, str, str]:
    """Return (headline text, emoji, color)."""
    sym = rec.get('symbol', '')
    contracts = rec.get('contracts_available', 1) or 1

    if action_type == 'SELL_CC':
        if trade:
            return (
                f"Sell {contracts}× {sym} ${trade.get('strike', '?')} Call"
                f" exp {trade.get('expiry', '?')}",
                '✅', 'green'
            )
        return f"Sell Covered Call on {sym}", '✅', 'green'

    elif action_type == 'BUY_PROTECTION':
        if trade:
            return (
                f"Buy {contracts}× {sym} ${trade.get('strike', '?')} Put"
                f" exp {trade.get('expiry', '?')}",
                '🛡', 'red'
            )
        return f"Buy Protective Put on {sym}", '🛡', 'red'

    elif action_type == 'COLLAR':
        if trade:
            return (
                f"Collar: Sell ${trade.get('cc_strike', '?')} Call / "
                f"Buy ${trade.get('put_strike', '?')} Put exp {trade.get('expiry', '?')}",
                '🔗', 'orange'
            )
        return f"Collar Strategy on {sym}", '🔗', 'orange'

    elif action_type == 'HOLD_WAIT':
        # Check if we're in a collar (CC + puts both open) for a better headline
        wc = rec.get('wheel_cycle', {}) or {}
        open_puts = wc.get('open_protective_puts', []) or []
        open_ccs  = wc.get('open_trades', []) or []
        active_puts = [p for p in open_puts if (p.get('dte') or 0) > 0]
        active_ccs  = [c for c in open_ccs  if (c.get('dte') or 0) > 0]
        if active_puts and active_ccs:
            cc = active_ccs[0]
            put = active_puts[0]
            return (
                f"Collar Active — Hold both legs on {sym} "
                f"(${cc.get('strike')} Call / ${put.get('strike')} Put)",
                '🔒', 'green'
            )
        return f"Hold & Wait — monitor for better IV or entry", '⏳', 'blue'

    elif action_type == 'PREPARE_ASSIGNMENT':
        wc = rec.get('wheel_cycle', {}) or {}
        open_trades = wc.get('open_trades', []) or []
        strike_str = f"${open_trades[0]['strike']}" if open_trades and open_trades[0].get('strike') else ''
        return (
            f"Prepare for Assignment — Ready CC at {strike_str}" if strike_str
            else f"Prepare for Assignment on {sym}",
            '🎯', 'orange'
        )

    elif action_type == 'ASX_HOLD':
        return f"Hold {sym} — monitor via your ASX broker", '📊', 'blue'

    return "Review Position", '📋', 'grey'


# ──────────────────────────────────────────────────────────────────────────────
# Plain-English coaching
# ──────────────────────────────────────────────────────────────────────────────

def _build_reasoning(action_type: str, rec: Dict, signals: List[Dict], trade: Dict) -> str:
    """2-4 sentence plain-English explanation of WHY this action was chosen."""
    sym = rec.get('symbol', 'this stock')
    iv_rank = rec.get('iv_rank', 0) or 0
    contracts = rec.get('contracts_available', 1) or 1
    current_price = rec.get('current_price', 0) or 0
    cost_basis = rec.get('cost_basis', current_price) or current_price
    pct_above = (current_price - cost_basis) / cost_basis * 100 if cost_basis else 0
    ms = rec.get('market_structure', {}) or {}
    gex_positive = ms.get('gex_positive', True)
    put_wall = ms.get('put_wall')
    call_wall = ms.get('call_wall')

    if action_type == 'SELL_CC':
        pop = trade.get('pop', 0) if trade else 0
        prob_touch = trade.get('prob_touch', 0) if trade else 0
        total_premium = trade.get('total_premium', 0) if trade else 0
        strike = trade.get('strike', '?') if trade else '?'
        expiry = trade.get('expiry', '?') if trade else '?'
        ann_ret = trade.get('annualized_if_called', 0) if trade else 0

        lines = []
        if iv_rank >= 40:
            lines.append(
                f"IV Rank is {iv_rank:.0f}%, meaning premium is elevated — now is a good time to be a seller."
            )
        else:
            lines.append(
                f"Even at IV Rank {iv_rank:.0f}%, a covered call on {sym} offers a positive-expectancy trade."
            )

        if pct_above > 5:
            lines.append(
                f"{sym} is {pct_above:.0f}% above your cost basis, so even if the shares are called away at ${strike} "
                f"you'd lock in a profit on both the stock and the premium."
            )
        else:
            lines.append(
                f"The ${strike} strike is above your cost basis, giving you a cushion — "
                f"if called away, you'd still profit."
            )

        if pop:
            defend_note = "meaning you likely won't need to defend it" if prob_touch <= 45 else "so set a stop-loss at 2× the premium received"
            lines.append(
                f"This strike has a {pop:.0f}% Probability of Profit (the breakeven is strike + premium received), "
                f"and a {prob_touch:.0f}% chance of being touched before expiry — "
                f"{defend_note}."
            )

        if total_premium:
            lines.append(
                f"Collecting ~${total_premium:.0f} in premium would reduce your effective cost basis. "
                f"At {ann_ret:.0f}% annualised, this is meaningful income generation from shares you already own."
            )

        if gex_positive and ms.get('coaching'):
            lines.append(
                "The OI structure confirms positive dealer gamma — price moves are likely to be contained, "
                "which reduces the chance of a sudden spike through your strike."
            )

        return ' '.join(lines[:4])  # max 4 sentences

    elif action_type == 'BUY_PROTECTION':
        strike = trade.get('strike', '?') if trade else '?'
        mid = trade.get('mid_price', 0) if trade else 0
        floor = trade.get('effective_floor', 0) if trade else 0
        cost_pct = trade.get('cost_pct_of_position', 0) if trade else 0

        lines = []
        if iv_rank < 25:
            lines.append(
                f"IV Rank is only {iv_rank:.0f}% — premiums are compressed. "
                f"While this makes CCs less attractive to sell, it makes protective puts cheaper to buy. "
                f"This is the ideal window to lock in downside protection."
            )
        else:
            lines.append(
                f"With {sym} near your cost basis and downside risk in play, "
                f"a protective put is the most efficient way to limit losses without selling the position."
            )

        if not gex_positive and ms.get('coaching'):
            lines.append(
                f"The gamma flip analysis shows negative dealer GEX — market moves are amplified right now, "
                f"meaning a drop could accelerate faster than usual. This raises the urgency of protection."
            )

        if trade:
            lines.append(
                f"Buying the ${strike} put (exp {trade.get('expiry', '?')}) for ~${mid:.2f}/contract "
                f"(~${trade.get('total_cost', 0):.0f} total) floors your downside at ~${floor:.2f} — "
                f"costing only {cost_pct:.1f}% of your position value for that coverage window."
            )

        lines.append(
            f"Once you have the put, your maximum loss is capped. "
            f"If IV rises later, you can layer in a covered call above to turn this into a zero-cost collar."
        )
        return ' '.join(lines[:4])

    elif action_type == 'COLLAR':
        lines = []
        best_collar = trade if trade else {}
        net_credit = best_collar.get('net_credit', 0)
        lines.append(
            f"{sym} is near or below your cost basis — this is the collar zone. "
            f"A collar (sell a call, buy a put with same expiry) defines both your upside cap and downside floor, "
            f"often for little or no net cost."
        )
        if net_credit and net_credit > 0:
            lines.append(
                f"The best collar here actually earns a net credit of ${net_credit:.0f} — "
                f"meaning you get paid to protect the position."
            )
        lines.append(
            f"With negative P&L or slim margin above cost basis, the priority is protecting what you have. "
            f"A collar lets you participate in any recovery while capping catastrophic loss."
        )
        return ' '.join(lines[:3])

    elif action_type == 'HOLD_WAIT':
        vrop      = rec.get('vrop', None)
        current_iv = rec.get('current_iv', 0) or 0
        hv30       = rec.get('hv30', 0) or 0
        # Use VRP to give a more precise reason when available
        if vrop is not None and hv30 > 0:
            vrp_context = (
                f"IV is {current_iv:.0f}% vs realised vol of {hv30:.0f}% (VRP {vrop:+.1f}pp). "
                + ("Selling edge is thin — wait for IV rank to expand before opening new premium positions."
                   if vrop < 10 else
                   "While VRP is positive, the overall signal mix favours holding your current structure.")
            )
        else:
            vrp_context = (
                f"IV Rank is {iv_rank:.0f}% — premiums are below their recent range. "
                f"Wait for volatility to expand before opening new positions."
            )
        lines = [
            vrp_context,
            f"The better play is to monitor and let the position breathe until a clearer signal emerges.",
        ]
        if put_wall:
            pw_pct = abs(put_wall.get('pct_from_price', 0) or 0)
            lines.append(
                f"The put wall at ${put_wall['strike']:.0f} is {pw_pct:.0f}% below — "
                f"there's dealer support below that level, so a mild pullback is unlikely to accelerate."
            )
        return ' '.join(lines[:3])

    if action_type == 'PREPARE_ASSIGNMENT':
        sym = rec.get('symbol', 'this stock')
        wc = rec.get('wheel_cycle', {}) or {}
        open_trades = wc.get('open_trades', []) or []
        ot = open_trades[0] if open_trades else {}
        strike = ot.get('strike', '?')
        dte = ot.get('dte', '?')
        expiry = ot.get('expiry', '?')
        top_ccs = rec.get('top_covered_calls', []) or []
        cc_str = ''
        if top_ccs:
            c = top_ccs[0]
            cc_str = (f" When assigned, immediately sell the ${c.get('strike')} call expiring "
                      f"{c.get('expiry')} for ~${c.get('total_premium', 0):.0f} total premium.")
        pct_to_strike = ''
        if strike != '?' and current_price:
            try:
                pct = (float(strike) - current_price) / current_price * 100
                pct_to_strike = f" ({abs(pct):.1f}% {'above' if pct > 0 else 'below'} current price)"
            except Exception:
                pass
        return (
            f"{sym} is approaching your CSP strike of ${strike}{pct_to_strike} with {dte} days to expiry. "
            f"Assignment on {expiry} is increasingly probable. This is the wheel working as planned — "
            f"you'll receive 100 shares at ${strike} minus the premium you already collected, "
            f"which is your effective break-even entry.{cc_str}"
        )

    return rec.get('reasoning', 'No specific action identified — review all signals above.')


def _education_blurb(action_type: str, rec: Dict) -> str:
    """A brief explanation of the strategy mechanics for the education section."""
    iv_rank = rec.get('iv_rank', 0) or 0

    if action_type == 'SELL_CC':
        return (
            "📚 How a Covered Call works: You sell the right to buy your shares at the strike price "
            "by expiry. In exchange, you collect premium upfront — this is yours to keep regardless of outcome. "
            "If the stock stays below strike, the option expires worthless and you sell again next month. "
            "If it rises above strike, your shares get called away at strike (plus you keep the premium). "
            "Key rules: close at 50% profit, set a 2× stop-loss, and never sell through an earnings date."
        )
    elif action_type == 'BUY_PROTECTION':
        return (
            "📚 How a Protective Put works: Buying a put option gives you the right to sell your shares "
            "at the strike price, no matter how far the stock falls. Think of it as insurance — you pay "
            "a premium upfront, but your downside is capped at (strike - premium paid). "
            f"Low IV environments (like now at {iv_rank:.0f}% rank) make puts cheaper — "
            "this is when protection gives the best value. Once IV expands, you can sell a call to "
            "offset the cost, creating a zero-cost collar."
        )
    elif action_type == 'COLLAR':
        return (
            "📚 How a Collar works: You simultaneously sell an out-of-the-money call (to collect premium) "
            "and buy an out-of-the-money put (to cap your downside). The call premium helps offset the put cost. "
            "A 'zero-cost collar' is when these exactly offset — you get downside protection for free "
            "by giving up upside above the call strike. Ideal when you're near your cost basis and want "
            "to stay in the position through uncertainty without risking a large loss."
        )
    elif action_type == 'HOLD_WAIT':
        return (
            "📚 Why timing matters: Selling options when IV is low means collecting small premiums while "
            "giving away the same amount of risk. The odds don't work in your favour. "
            "IV Rank measures where current IV sits vs the past 12 months — below 25% means premiums "
            "are in the bottom quarter of what's been available. Patience here typically pays off: "
            "when volatility expands, you'll collect far more per contract for the same risk."
        )
    elif action_type == 'PREPARE_ASSIGNMENT':
        wc = rec.get('wheel_cycle', {}) or {}
        open_trades = wc.get('open_trades', []) or []
        strike = open_trades[0].get('strike', '?') if open_trades else '?'
        sym = rec.get('symbol', 'this stock')
        top_ccs = rec.get('top_covered_calls', []) or []
        best_cc_str = ''
        if top_ccs:
            c = top_ccs[0]
            best_cc_str = f" The best first CC target is ${c.get('strike')} exp {c.get('expiry')} (~${c.get('total_premium', 0):.0f} premium)."
        return (
            f"📚 Wheel Assignment Strategy: Your CSP at ${strike} is now close to the money — "
            f"assignment is likely if {sym} stays near or below ${strike} by expiry. "
            f"This is NOT a loss — it's the wheel working as designed. Once assigned, you'll own 100 shares "
            f"at an effective cost of ${strike} minus the premium you already collected. "
            f"Your next move is to immediately sell a covered call above your effective cost basis.{best_cc_str} "
            f"You keep the CC premium on top of the CSP premium, lowering your effective entry further."
        )
    return ""


# ──────────────────────────────────────────────────────────────────────────────
# ASX fallback
# ──────────────────────────────────────────────────────────────────────────────

def _asx_result(rec: Dict) -> Dict:
    sym = rec.get('symbol', '')
    price = rec.get('current_price', 0) or 0
    pnl = rec.get('unrealized_pnl', 0) or 0
    pnl_str = f"{'▲' if pnl >= 0 else '▼'} ${abs(pnl):,.0f}"

    # Surface the actual IBKR error if wheel.py captured one
    ibkr_error = rec.get('ibkr_error', '')
    if ibkr_error:
        reasoning = (
            f"{sym} is an ASX-listed stock. IBKR chain fetch failed: {ibkr_error}. "
            f"Ensure IB Gateway is running on port 4001 with API access enabled and try again."
        )
    else:
        reasoning = (
            f"{sym} is an ASX-listed stock. No options data was returned from IB Gateway. "
            f"Make sure IB Gateway is running on port 4001 with API access enabled. "
            f"Once connected, full covered call, protective put, and collar analysis will appear here."
        )

    return {
        'action_type': 'ASX_HOLD',
        'confidence': 70,
        'headline': f"Hold {sym} — awaiting IBKR options data",
        'icon': '📊',
        'color': 'blue',
        'signals': [
            {'label': 'Exchange', 'value': 'ASX', 'color': 'blue',
             'tooltip': 'ASX-listed stock. Options data sourced via IB Gateway.'},
            {'label': 'Unrealized P&L', 'value': pnl_str,
             'color': 'green' if pnl >= 0 else 'red',
             'tooltip': 'Mark-to-market gain/loss vs your cost basis.'},
            {'label': 'Price', 'value': f'${price:.2f}', 'color': 'blue',
             'tooltip': 'Current price from yfinance (15-min delayed for ASX).'},
            {'label': 'IBKR Status', 'value': 'Error' if ibkr_error else 'No data',
             'color': 'red' if ibkr_error else 'orange',
             'tooltip': ibkr_error or 'No options chain returned — check IB Gateway is running'},
        ],
        'reasoning': reasoning,
        'education': (
            "ASX Options via IBKR: Options Ollie connects to IB Gateway to pull live ASX options chains. "
            "Ensure IB Gateway is open and logged in, then refresh this position."
        ),
        'specific_trade': {},
        'score_breakdown': {},
    }


# ──────────────────────────────────────────────────────────────────────────────
# Gemini narrative enhancement
# ──────────────────────────────────────────────────────────────────────────────

def _build_cc_coaching(rec: Dict, trade: Dict, action_type: str) -> str:
    """Template-based one-sentence coaching annotation for the CC table."""
    top_ccs = rec.get('top_covered_calls', []) or []
    if not top_ccs:
        return ''
    best = top_ccs[0]
    strike = best.get('strike', '?')
    pop = best.get('pop', 0)
    prob_touch = best.get('prob_touch', 0)
    ann_ret = best.get('annualized_if_called', 0)
    total = best.get('total_premium', 0)
    ms = rec.get('market_structure', {}) or {}
    call_wall = ms.get('call_wall')

    parts = [f"The ${strike} strike stands out — {pop:.0f}% probability of profit"]
    if total:
        parts.append(f"collecting ~${total:.0f} total")
    if ann_ret:
        parts.append(f"{ann_ret:.0f}% annualised if called")
    line = ', '.join(parts) + '.'
    if call_wall and call_wall.get('strike'):
        cw = call_wall['strike']
        if abs(cw - float(strike)) / float(strike) < 0.05:
            line += f" This strike aligns with the ${cw:.0f} call wall — dealer resistance acts as a ceiling."
        elif float(strike) < cw:
            line += f" Well below the ${cw:.0f} call wall — structural room for the stock to move before touching your strike."
    if prob_touch > 60:
        line += " Prob Touch is elevated — consider setting a 2× premium stop-loss."
    return line


def _build_risk_narrative(rec: Dict, trade: Dict, action_type: str) -> str:
    """Template-based coached risk assessment replacing raw scenario table."""
    ra = rec.get('risk_analysis', {}) or {}
    scenarios = ra.get('scenarios', []) or []
    if not scenarios:
        return ''
    sym = rec.get('symbol', 'this stock')
    current_price = rec.get('current_price', 0) or 0
    cost_basis = rec.get('cost_basis', current_price) or current_price
    ms = rec.get('market_structure', {}) or {}
    gex_positive = ms.get('gex_positive', True)
    put_wall = ms.get('put_wall')
    protective = rec.get('protective_strategies', []) or []

    # Find first scenario that breaks cost basis
    breach = next((s for s in scenarios if s.get('below_cost_basis')), None)

    lines = []
    if breach:
        lines.append(
            f"A {breach['drop_pct']}% drop to ${breach['target_price']} would put you "
            f"${abs(breach.get('dollar_loss_from_now', 0)):,.0f} underwater from here."
        )
    else:
        lines.append(
            f"Even in a 20% pullback, {sym} stays above your cost basis — you're in a strong position."
        )

    if put_wall:
        pw_strike = put_wall.get('strike', 0)
        pw_pct = abs(put_wall.get('pct_from_price', 0) or 0)
        lines.append(
            f"The ${pw_strike:.0f} put wall ({pw_pct:.0f}% below) provides dealer-driven support."
        )

    if not gex_positive:
        lines.append("Negative GEX means moves could accelerate — protection has more urgency here.")
    elif gex_positive:
        lines.append("Positive GEX dampens moves — a sudden gap down is less likely in this regime.")

    if protective:
        best_prot = protective[0]
        lines.append(
            f"If you want a floor: the ${best_prot.get('strike', '?')} put costs "
            f"~${best_prot.get('total_cost', 0):,.0f} ({best_prot.get('cost_pct_of_position', '?')}% of position) "
            f"for {best_prot.get('dte', '?')}d of cover."
        )

    return ' '.join(lines[:4])


def _gemini_enhance_structured(
    reasoning: str,
    cc_coaching: str,
    risk_narrative: str,
    action_type: str,
    rec: Dict,
    signals: List[Dict],
    api_key: str,
) -> Optional[Dict]:
    """
    Single Gemini call that enhances reasoning, CC coaching, and risk narrative.
    Returns dict with keys: reasoning, cc_coaching, risk_narrative — or None on failure.
    """
    sym = rec.get('symbol', 'the stock')
    iv_rank = rec.get('iv_rank', 0) or 0
    price = rec.get('current_price', 0) or 0
    signal_summary = ', '.join(
        f"{s['label']} = {s['value']}" for s in signals[:6]
    )

    # Summarise any open protective puts so Gemini can reference them
    wc_ctx = rec.get('wheel_cycle', {}) or {}
    open_puts_ctx = wc_ctx.get('open_protective_puts', []) or []
    active_puts_ctx = [p for p in open_puts_ctx if (p.get('dte') or 0) > 0]
    puts_note = ''
    if active_puts_ctx:
        put_descs = ', '.join(
            f"{p.get('quantity', 1)}× ${p.get('strike')} put ({p.get('dte')}d left)"
            for p in active_puts_ctx
        )
        puts_note = f" They already hold open protective puts: {put_descs}."

    prompt = (
        f"You are Options Ollie, a friendly but expert options trading coach. "
        f"A trader owns {rec.get('shares_held', '?')} shares of {sym} at ${price:.2f}.{puts_note} "
        f"The system recommends: {action_type}. "
        f"Key signals: {signal_summary}. IV Rank: {iv_rank:.0f}%.\n\n"
        f"Rewrite the following three sections in a warm, confident, coaching voice. "
        f"Use dollar amounts and percentages from the data. No jargon without explanation. "
        f"No disclaimers. Be direct and specific.\n\n"
        f"SECTION 1 — Main Recommendation (3-4 sentences):\n{reasoning}\n\n"
        f"SECTION 2 — Covered Call Annotation (1-2 sentences, which strike to pick and why):\n{cc_coaching}\n\n"
        f"SECTION 3 — Risk Assessment (2-3 sentences, what could go wrong and whether to hedge):\n{risk_narrative}\n\n"
        f"Respond in EXACTLY this format (three sections separated by ----):\n"
        f"[reasoning text]\n----\n[cc coaching text]\n----\n[risk narrative text]"
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 600}
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode('utf-8'))
            candidates = body.get('candidates', [])
            if candidates:
                parts = candidates[0].get('content', {}).get('parts', [])
                if parts:
                    text = parts[0].get('text', '').strip()
                    sections = [s.strip() for s in text.split('----')]
                    if len(sections) >= 3:
                        return {
                            'reasoning': sections[0],
                            'cc_coaching': sections[1],
                            'risk_narrative': sections[2],
                        }
                    elif len(sections) >= 1:
                        return {'reasoning': sections[0]}
    except (urllib.error.URLError, json.JSONDecodeError, KeyError):
        pass

    return None
