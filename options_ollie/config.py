"""
Options Ollie — Configuration
Central config for risk parameters, watchlists, API keys, and strategy settings.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional
import json
import os

# ─── Risk Profile ────────────────────────────────────────────────────────────

@dataclass
class RiskProfile:
    """Moderate risk profile: capital preservation with steady income."""
    min_probability_of_profit: float = 0.70      # 70%+ POP required
    max_portfolio_allocation_pct: float = 0.05    # Max 5% of portfolio per position
    max_total_options_exposure_pct: float = 0.50   # Max 50% of portfolio in options
    target_monthly_return_pct: float = 0.02        # 2% monthly target
    min_days_to_expiry: int = 20                   # Minimum 20 DTE
    max_days_to_expiry: int = 50                   # Maximum 50 DTE (sweet spot 30-45)
    target_delta_csp: float = 0.25                 # Cash-secured puts: ~25 delta
    target_delta_cc: float = 0.25                  # Covered calls: ~25 delta
    min_open_interest: int = 100                   # Minimum open interest for liquidity
    min_volume: int = 50                           # Minimum daily volume
    max_bid_ask_spread_pct: float = 0.05           # Max 5% bid-ask spread
    max_positions: int = 10                        # Max concurrent positions
    stop_loss_pct: float = 2.0                     # Close at 2x premium received
    profit_target_pct: float = 0.50                # Close at 50% profit

# ─── Portfolio State ─────────────────────────────────────────────────────────

@dataclass
class Position:
    """Tracks a single options or stock position."""
    symbol: str
    position_type: str        # 'shares', 'csp', 'covered_call', 'iron_condor', 'credit_spread'
    quantity: int
    entry_price: float
    entry_date: str
    expiry_date: Optional[str] = None
    strike: Optional[float] = None
    strike_put: Optional[float] = None   # For iron condors
    strike_call: Optional[float] = None  # For iron condors
    premium_received: float = 0.0
    status: str = 'open'                 # 'open', 'closed', 'assigned', 'expired'
    notes: str = ''

@dataclass
class Portfolio:
    """Current portfolio state."""
    cash: float = 50000.0
    positions: List[Position] = field(default_factory=list)
    trade_history: List[Dict] = field(default_factory=list)

    def total_value(self, current_prices: Dict[str, float] = None) -> float:
        val = self.cash
        if current_prices:
            for p in self.positions:
                if p.position_type == 'shares' and p.symbol in current_prices:
                    val += p.quantity * current_prices[p.symbol]
        return val

    def shares_held(self, symbol: str) -> int:
        return sum(p.quantity for p in self.positions
                   if p.symbol == symbol and p.position_type == 'shares' and p.status == 'open')

    def open_options(self, symbol: str = None) -> List[Position]:
        return [p for p in self.positions
                if p.status == 'open'
                and p.position_type != 'shares'
                and (symbol is None or p.symbol == symbol)]

    def to_dict(self) -> dict:
        return {
            'cash': self.cash,
            'positions': [vars(p) for p in self.positions],
            'trade_history': self.trade_history
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'Portfolio':
        port = cls(cash=d.get('cash', 50000))
        port.positions = [Position(**p) for p in d.get('positions', [])]
        port.trade_history = d.get('trade_history', [])
        return port

# ─── Watchlist & Screening ───────────────────────────────────────────────────

# Universe of liquid, options-friendly tickers to screen
WATCHLIST_STOCKS = [
    # Tech
    'AAPL', 'MSFT', 'NVDA', 'AMD', 'GOOGL', 'META', 'AMZN', 'TSLA', 'NFLX', 'CRM',
    # Reddit (your current holding)
    'RDDT',
    # Financials
    'JPM', 'BAC', 'GS', 'V', 'MA',
    # Consumer
    'WMT', 'COST', 'HD', 'MCD', 'SBUX',
    # Energy
    'XOM', 'CVX', 'SLB',
    # Healthcare
    'JNJ', 'UNH', 'PFE', 'ABBV',
    # Industrial
    'CAT', 'DE', 'BA',
]

WATCHLIST_ETFS = [
    'SPY', 'QQQ', 'IWM', 'DIA',     # Index
    'GLD', 'SLV',                     # Precious metals
    'USO', 'XLE',                     # Energy
    'TLT', 'HYG',                     # Bonds
    'XLF', 'XLK', 'XLV',             # Sector
    'ARKK', 'SOXX',                   # Thematic
]

FULL_WATCHLIST = WATCHLIST_STOCKS + WATCHLIST_ETFS

# ─── Telegram Config ─────────────────────────────────────────────────────────

@dataclass
class TelegramConfig:
    bot_token: str = ''         # Set via env var OLLIE_TELEGRAM_TOKEN
    chat_id: str = ''           # Set via env var OLLIE_TELEGRAM_CHAT_ID
    enabled: bool = False

    def __post_init__(self):
        self.bot_token = os.environ.get('OLLIE_TELEGRAM_TOKEN', self.bot_token)
        self.chat_id = os.environ.get('OLLIE_TELEGRAM_CHAT_ID', self.chat_id)
        self.enabled = bool(self.bot_token and self.chat_id)

# ─── MenthorQ Config (Future) ────────────────────────────────────────────────

@dataclass
class MenthorQConfig:
    api_key: str = ''           # Set via env var MENTHORQ_API_KEY
    base_url: str = 'https://api.menthorq.com/v1'
    enabled: bool = False

    def __post_init__(self):
        self.api_key = os.environ.get('MENTHORQ_API_KEY', self.api_key)
        self.enabled = bool(self.api_key)

# ─── Master Config ───────────────────────────────────────────────────────────

@dataclass
class OllieConfig:
    risk: RiskProfile = field(default_factory=RiskProfile)
    portfolio: Portfolio = field(default_factory=Portfolio)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    menthorq: MenthorQConfig = field(default_factory=MenthorQConfig)
    data_dir: str = 'data'
    portfolio_file: str = 'portfolio.json'

    def save_portfolio(self):
        os.makedirs(self.data_dir, exist_ok=True)
        path = os.path.join(self.data_dir, self.portfolio_file)
        with open(path, 'w') as f:
            json.dump(self.portfolio.to_dict(), f, indent=2)

    def load_portfolio(self):
        path = os.path.join(self.data_dir, self.portfolio_file)
        if os.path.exists(path):
            with open(path) as f:
                self.portfolio = Portfolio.from_dict(json.load(f))

# Default config instance
DEFAULT_CONFIG = OllieConfig()
