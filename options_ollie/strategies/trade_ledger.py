"""
Options Ollie — Trade Ledger
Records confirmed trades, tracks positions, and maintains full trade history.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional, Dict
from enum import Enum


class TradeType(str, Enum):
    CSP = 'csp'                          # Cash-secured put (sold)
    COVERED_CALL = 'covered_call'        # Covered call (sold)
    IRON_CONDOR = 'iron_condor'          # Iron condor (sold)
    BULL_PUT_SPREAD = 'bull_put_spread'
    BEAR_CALL_SPREAD = 'bear_call_spread'
    LONG_SHARES = 'long_shares'          # Share assignment or purchase
    PROTECTIVE_PUT = 'protective_put'    # Bought put hedge on existing shares


class TradeStatus(str, Enum):
    OPEN = 'open'
    CLOSED = 'closed'              # Closed for profit/loss
    ASSIGNED = 'assigned'          # Put assigned, now hold shares
    CALLED_AWAY = 'called_away'    # Shares called away via CC
    EXPIRED = 'expired'            # Expired worthless (full profit)
    ROLLED = 'rolled'              # Rolled to new expiry/strike


@dataclass
class Trade:
    """A single confirmed trade entry."""
    id: str                         # Unique trade ID (auto-generated)
    symbol: str
    trade_type: str                 # TradeType value
    status: str = 'open'           # TradeStatus value

    # Entry details
    entry_date: str = ''
    entry_price: float = 0.0       # Premium per contract (for options)
    quantity: int = 1              # Number of contracts (or shares for stock)

    # Option specifics
    strike: Optional[float] = None
    expiry: Optional[str] = None
    option_side: str = ''          # 'put', 'call', or '' for shares

    # Iron condor / spread legs
    short_put_strike: Optional[float] = None
    long_put_strike: Optional[float] = None
    short_call_strike: Optional[float] = None
    long_call_strike: Optional[float] = None

    # Financials
    premium_received: float = 0.0   # Total premium received (entry_price * quantity * 100)
    commission: float = 0.0
    collateral_required: float = 0.0

    # Exit details (filled when closed)
    exit_date: str = ''
    exit_price: float = 0.0
    realized_pnl: float = 0.0

    # Management
    notes: str = ''
    rolled_from: Optional[str] = None  # ID of trade this was rolled from
    rolled_to: Optional[str] = None    # ID of trade this was rolled to

    # Linked trades (wheel tracking)
    wheel_group: Optional[str] = None  # Groups CSP → shares → CC in a wheel cycle

    def days_held(self) -> int:
        if not self.entry_date:
            return 0
        entry = datetime.strptime(self.entry_date, '%Y-%m-%d')
        return (datetime.now() - entry).days

    def days_to_expiry(self) -> Optional[int]:
        if not self.expiry:
            return None
        exp = datetime.strptime(self.expiry, '%Y-%m-%d')
        return (exp - datetime.now()).days

    def is_options_trade(self) -> bool:
        return self.trade_type != TradeType.LONG_SHARES


class TradeLedger:
    """
    Persistent trade ledger. Records all confirmed trades and maintains history.
    Saves to JSON for portability.
    """

    def __init__(self, ledger_path: str = 'trade_ledger.json'):
        self.ledger_path = ledger_path
        self.trades: List[Trade] = []
        self._next_id = 1
        self.load()

    def load(self):
        """Load ledger from disk."""
        if os.path.exists(self.ledger_path):
            with open(self.ledger_path, 'r') as f:
                data = json.load(f)
                self.trades = [Trade(**t) for t in data.get('trades', [])]
                self._next_id = data.get('next_id', len(self.trades) + 1)

    def save(self):
        """Persist ledger to disk."""
        os.makedirs(os.path.dirname(self.ledger_path) or '.', exist_ok=True)
        data = {
            'trades': [asdict(t) for t in self.trades],
            'next_id': self._next_id,
            'last_updated': datetime.now().isoformat(),
        }
        with open(self.ledger_path, 'w') as f:
            json.dump(data, f, indent=2)

    def _gen_id(self) -> str:
        tid = f"OL-{self._next_id:04d}"
        self._next_id += 1
        return tid

    # ── Trade Entry Methods ──────────────────────────────────────────────

    def enter_csp(self, symbol: str, strike: float, expiry: str,
                  premium_per_contract: float, contracts: int = 1,
                  commission: float = 0.0, notes: str = '',
                  wheel_group: str = None, entry_date: str = None) -> Trade:
        """Record a cash-secured put sale."""
        trade = Trade(
            id=self._gen_id(),
            symbol=symbol.upper(),
            trade_type=TradeType.CSP,
            entry_date=entry_date or datetime.now().strftime('%Y-%m-%d'),
            entry_price=premium_per_contract,
            quantity=contracts,
            strike=strike,
            expiry=expiry,
            option_side='put',
            premium_received=round(premium_per_contract * contracts * 100, 2),
            collateral_required=round(strike * contracts * 100, 2),
            commission=commission,
            notes=notes,
            wheel_group=wheel_group or f"WHEEL-{symbol.upper()}-{self._next_id}",
        )
        self.trades.append(trade)
        self.save()
        return trade

    def enter_covered_call(self, symbol: str, strike: float, expiry: str,
                           premium_per_contract: float, contracts: int = 1,
                           commission: float = 0.0, notes: str = '',
                           wheel_group: str = None, entry_date: str = None) -> Trade:
        """Record a covered call sale."""
        trade = Trade(
            id=self._gen_id(),
            symbol=symbol.upper(),
            trade_type=TradeType.COVERED_CALL,
            entry_date=entry_date or datetime.now().strftime('%Y-%m-%d'),
            entry_price=premium_per_contract,
            quantity=contracts,
            strike=strike,
            expiry=expiry,
            option_side='call',
            premium_received=round(premium_per_contract * contracts * 100, 2),
            commission=commission,
            notes=notes,
            wheel_group=wheel_group,
        )
        self.trades.append(trade)
        self.save()
        return trade

    def enter_iron_condor(self, symbol: str, expiry: str,
                          short_put: float, long_put: float,
                          short_call: float, long_call: float,
                          net_credit_per_contract: float, contracts: int = 1,
                          commission: float = 0.0, notes: str = '') -> Trade:
        """Record an iron condor."""
        max_width = max(short_put - long_put, long_call - short_call)
        trade = Trade(
            id=self._gen_id(),
            symbol=symbol.upper(),
            trade_type=TradeType.IRON_CONDOR,
            entry_date=datetime.now().strftime('%Y-%m-%d'),
            entry_price=net_credit_per_contract,
            quantity=contracts,
            expiry=expiry,
            short_put_strike=short_put,
            long_put_strike=long_put,
            short_call_strike=short_call,
            long_call_strike=long_call,
            premium_received=round(net_credit_per_contract * contracts * 100, 2),
            collateral_required=round((max_width - net_credit_per_contract) * contracts * 100, 2),
            commission=commission,
            notes=notes,
        )
        self.trades.append(trade)
        self.save()
        return trade

    def enter_credit_spread(self, symbol: str, expiry: str,
                            short_strike: float, long_strike: float,
                            spread_type: str,  # 'bull_put' or 'bear_call'
                            net_credit_per_contract: float, contracts: int = 1,
                            commission: float = 0.0, notes: str = '') -> Trade:
        """Record a credit spread."""
        trade_type = TradeType.BULL_PUT_SPREAD if spread_type == 'bull_put' else TradeType.BEAR_CALL_SPREAD
        option_side = 'put' if spread_type == 'bull_put' else 'call'
        width = abs(short_strike - long_strike)

        trade = Trade(
            id=self._gen_id(),
            symbol=symbol.upper(),
            trade_type=trade_type,
            entry_date=datetime.now().strftime('%Y-%m-%d'),
            entry_price=net_credit_per_contract,
            quantity=contracts,
            strike=short_strike,
            expiry=expiry,
            option_side=option_side,
            short_put_strike=short_strike if option_side == 'put' else None,
            long_put_strike=long_strike if option_side == 'put' else None,
            short_call_strike=short_strike if option_side == 'call' else None,
            long_call_strike=long_strike if option_side == 'call' else None,
            premium_received=round(net_credit_per_contract * contracts * 100, 2),
            collateral_required=round((width - net_credit_per_contract) * contracts * 100, 2),
            commission=commission,
            notes=notes,
        )
        self.trades.append(trade)
        self.save()
        return trade

    def enter_protective_put(self, symbol: str, strike: float, expiry: str,
                             premium_per_contract: float, contracts: int = 1,
                             commission: float = 0.0, notes: str = '',
                             entry_date: str = None) -> Trade:
        """Record a protective put purchase (long put hedge on existing shares).
        Premium is a debit — premium_received is stored as a negative value.
        """
        trade = Trade(
            id=self._gen_id(),
            symbol=symbol.upper(),
            trade_type=TradeType.PROTECTIVE_PUT,
            entry_date=entry_date or datetime.now().strftime('%Y-%m-%d'),
            entry_price=premium_per_contract,
            quantity=contracts,
            strike=strike,
            expiry=expiry,
            option_side='put',
            premium_received=round(-premium_per_contract * contracts * 100, 2),  # debit
            collateral_required=0.0,
            commission=commission,
            notes=notes,
        )
        self.trades.append(trade)
        self.save()
        return trade

    def enter_shares(self, symbol: str, shares: int, cost_per_share: float,
                     notes: str = '', wheel_group: str = None) -> Trade:
        """Record a stock position (purchase or assignment)."""
        trade = Trade(
            id=self._gen_id(),
            symbol=symbol.upper(),
            trade_type=TradeType.LONG_SHARES,
            entry_date=datetime.now().strftime('%Y-%m-%d'),
            entry_price=cost_per_share,
            quantity=shares,
            collateral_required=round(cost_per_share * shares, 2),
            notes=notes,
            wheel_group=wheel_group,
        )
        self.trades.append(trade)
        self.save()
        return trade

    # ── Trade Exit / Update Methods ──────────────────────────────────────

    def close_trade(self, trade_id: str, exit_price: float,
                    status: str = 'closed', notes: str = '') -> Optional[Trade]:
        """Close a trade and calculate realized P&L."""
        trade = self.get_trade(trade_id)
        if not trade:
            return None

        trade.exit_date = datetime.now().strftime('%Y-%m-%d')
        trade.exit_price = exit_price
        trade.status = status

        if trade.is_options_trade():
            # For sold options: profit = premium received - cost to close
            cost_to_close = exit_price * trade.quantity * 100
            trade.realized_pnl = round(trade.premium_received - cost_to_close - trade.commission, 2)
        else:
            # For shares: profit = (exit - entry) * quantity
            trade.realized_pnl = round((exit_price - trade.entry_price) * trade.quantity - trade.commission, 2)

        if notes:
            trade.notes = f"{trade.notes} | {notes}" if trade.notes else notes

        self.save()
        return trade

    def expire_trade(self, trade_id: str) -> Optional[Trade]:
        """Mark option as expired worthless (full profit on sold options)."""
        return self.close_trade(trade_id, exit_price=0.0, status='expired',
                                notes='Expired worthless — full premium kept')

    def mark_assigned(self, trade_id: str) -> Optional[Trade]:
        """Mark a CSP as assigned. Creates a corresponding shares position."""
        trade = self.get_trade(trade_id)
        if not trade or trade.trade_type != TradeType.CSP:
            return None

        trade.status = TradeStatus.ASSIGNED
        trade.exit_date = datetime.now().strftime('%Y-%m-%d')
        trade.exit_price = 0.0
        trade.realized_pnl = trade.premium_received  # Premium still kept

        # Effective cost basis = strike - premium per share
        effective_cost = trade.strike - trade.entry_price
        shares_trade = self.enter_shares(
            symbol=trade.symbol,
            shares=trade.quantity * 100,
            cost_per_share=effective_cost,
            notes=f"Assigned from {trade.id} at ${trade.strike} strike. "
                  f"Effective cost basis: ${effective_cost:.2f} (strike ${trade.strike} - "
                  f"premium ${trade.entry_price})",
            wheel_group=trade.wheel_group,
        )

        trade.notes = f"{trade.notes} | Assigned → {shares_trade.id}" if trade.notes else f"Assigned → {shares_trade.id}"
        self.save()
        return trade

    def mark_called_away(self, trade_id: str) -> Optional[Trade]:
        """Mark a CC as exercised (shares called away)."""
        trade = self.get_trade(trade_id)
        if not trade or trade.trade_type != TradeType.COVERED_CALL:
            return None

        trade.status = TradeStatus.CALLED_AWAY
        trade.exit_date = datetime.now().strftime('%Y-%m-%d')
        trade.realized_pnl = trade.premium_received  # CC premium kept

        # Find and close the corresponding shares position
        shares_positions = [t for t in self.trades
                           if t.symbol == trade.symbol
                           and t.trade_type == TradeType.LONG_SHARES
                           and t.status == TradeStatus.OPEN]

        if shares_positions:
            sp = shares_positions[0]
            shares_pnl = (trade.strike - sp.entry_price) * min(sp.quantity, trade.quantity * 100)
            sp.status = TradeStatus.CLOSED
            sp.exit_date = datetime.now().strftime('%Y-%m-%d')
            sp.exit_price = trade.strike
            sp.realized_pnl = round(shares_pnl, 2)
            sp.notes = f"{sp.notes} | Called away via {trade.id} at ${trade.strike}" if sp.notes else f"Called away via {trade.id}"

        self.save()
        return trade

    def roll_trade(self, trade_id: str, close_price: float,
                   new_strike: float, new_expiry: str,
                   new_premium: float, notes: str = '') -> Optional[Trade]:
        """Roll a position: close existing, open new at different strike/expiry."""
        old_trade = self.get_trade(trade_id)
        if not old_trade:
            return None

        # Close the old trade
        old_trade.exit_date = datetime.now().strftime('%Y-%m-%d')
        old_trade.exit_price = close_price
        old_trade.status = TradeStatus.ROLLED

        cost_to_close = close_price * old_trade.quantity * 100
        old_trade.realized_pnl = round(old_trade.premium_received - cost_to_close, 2)

        # Open replacement
        if old_trade.trade_type == TradeType.CSP:
            new_trade = self.enter_csp(
                symbol=old_trade.symbol, strike=new_strike, expiry=new_expiry,
                premium_per_contract=new_premium, contracts=old_trade.quantity,
                notes=f"Rolled from {old_trade.id}. {notes}",
                wheel_group=old_trade.wheel_group,
            )
        elif old_trade.trade_type == TradeType.COVERED_CALL:
            new_trade = self.enter_covered_call(
                symbol=old_trade.symbol, strike=new_strike, expiry=new_expiry,
                premium_per_contract=new_premium, contracts=old_trade.quantity,
                notes=f"Rolled from {old_trade.id}. {notes}",
                wheel_group=old_trade.wheel_group,
            )
        else:
            self.save()
            return old_trade

        old_trade.rolled_to = new_trade.id
        new_trade.rolled_from = old_trade.id

        self.save()
        return new_trade

    def delete_trade(self, trade_id: str) -> bool:
        """Permanently remove a trade from the ledger. Returns True if found and deleted."""
        before = len(self.trades)
        self.trades = [t for t in self.trades if t.id != trade_id]
        if len(self.trades) < before:
            self.save()
            return True
        return False

    def update_trade(self, trade_id: str, fields: dict) -> Optional[Trade]:
        """Update arbitrary fields on a trade. Only known Trade fields are applied."""
        trade = self.get_trade(trade_id)
        if not trade:
            return None
        allowed = {
            'symbol', 'trade_type', 'status', 'entry_date', 'entry_price',
            'quantity', 'strike', 'expiry', 'option_side',
            'short_put_strike', 'long_put_strike', 'short_call_strike', 'long_call_strike',
            'premium_received', 'commission', 'collateral_required',
            'exit_date', 'exit_price', 'realized_pnl', 'notes',
        }
        for k, v in fields.items():
            if k in allowed:
                setattr(trade, k, v)
        self.save()
        return trade

    # ── Query Methods ────────────────────────────────────────────────────

    def get_trade(self, trade_id: str) -> Optional[Trade]:
        for t in self.trades:
            if t.id == trade_id:
                return t
        return None

    def open_trades(self, symbol: str = None) -> List[Trade]:
        return [t for t in self.trades
                if t.status == TradeStatus.OPEN
                and (symbol is None or t.symbol == symbol.upper())]

    def closed_trades(self, symbol: str = None) -> List[Trade]:
        return [t for t in self.trades
                if t.status != TradeStatus.OPEN
                and (symbol is None or t.symbol == symbol.upper())]

    def wheel_history(self, symbol: str) -> List[Trade]:
        """Get all trades in the wheel cycle for a symbol."""
        groups = set()
        for t in self.trades:
            if t.symbol == symbol.upper() and t.wheel_group:
                groups.add(t.wheel_group)
        return [t for t in self.trades if t.wheel_group in groups]

    def total_premium_collected(self, symbol: str = None) -> float:
        return sum(t.premium_received for t in self.trades
                   if t.is_options_trade()
                   and (symbol is None or t.symbol == symbol.upper()))

    def total_realized_pnl(self, symbol: str = None) -> float:
        return sum(t.realized_pnl for t in self.trades
                   if t.status != TradeStatus.OPEN
                   and (symbol is None or t.symbol == symbol.upper()))

    def summary(self) -> Dict:
        """Full ledger summary."""
        open_t = self.open_trades()
        closed_t = self.closed_trades()
        winners = [t for t in closed_t if t.realized_pnl > 0]

        return {
            'total_trades': len(self.trades),
            'open_positions': len(open_t),
            'closed_trades': len(closed_t),
            'win_rate': round(len(winners) / max(len(closed_t), 1) * 100, 1),
            'total_premium_collected': round(self.total_premium_collected(), 2),
            'total_realized_pnl': round(self.total_realized_pnl(), 2),
            'open_premium_at_risk': round(sum(t.premium_received for t in open_t if t.is_options_trade()), 2),
            'total_collateral_deployed': round(sum(t.collateral_required for t in open_t), 2),
        }
