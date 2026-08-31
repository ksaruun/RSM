"""
Configuration for the Swing Trade Screener.
All parameters can be overridden via CLI flags (see main.py --help).
"""

# --- Moving Average Settings ---
SHORT_DMA_PERIOD = 50      # Short-term moving average (days)
LONG_DMA_PERIOD = 200      # Long-term moving average (days)

# --- PE Valuation Settings ---
PE_HISTORY_YEARS = 3       # Years of PE history for median calculation
MAX_PE_ABSOLUTE = None     # Absolute PE cap (e.g. 35). If set, rejects stocks
                           # with current PE above this value regardless of
                           # historical median. Set to None to disable.

# --- Quarterly Growth Settings ---
MIN_QUARTERS = 4           # Minimum quarters of positive growth required
MIN_COMPOUND_GROWTH_PCT = 7.0  # Minimum compound annual growth rate (CAGR) for revenue and profit

# --- Bluechip (large-cap) relaxation for the growth filter ---
# Very large companies (e.g. HDFC Bank, Reliance) sometimes have only 3 quarters
# of data in yfinance due to reporting lags or API gaps. Rather than rejecting a
# ₹1L+ Cr company over a missing quarter, we relax the minimum to 3 quarters and
# compute CAGR from whatever is available.  The CAGR floor itself is unchanged.
BLUECHIP_MCAP_CR = 100_000.0   # ₹ 1 lakh crore — "bluechip" threshold
BLUECHIP_MIN_QUARTERS = 3      # Accept 3+ quarters for bluechips (vs 4 normally)

# --- Debt Quality Settings ---
MAX_DEBT_TO_EQUITY = 0.5   # Maximum D/E ratio (e.g. 0.5 = 50%)
MAX_PLEDGED_PCT = 5.0      # Maximum promoter pledged percentage

# --- 52-Week High Pullback Settings ---
# Stock must have pulled back from its 52-week high by at least MIN_PULLBACK_PCT
# but no more than MAX_PULLBACK_PCT (the "sweet spot" entry zone).
ENABLE_PULLBACK_FILTER = True   # Set False to disable this filter
MIN_PULLBACK_PCT = 10.0         # Minimum % below 52-week high (default 10%)
MAX_PULLBACK_PCT = 30.0         # Maximum % below 52-week high (default 30%)
                                # Use --min-pullback 10 --max-pullback 15 for a
                                # tighter "10-15% off peak" entry zone.

# --- Beta (Volatility/Momentum) Settings ---
# NOTE: yfinance computes beta vs S&P 500, not Nifty 50. Most Indian stocks
# show beta < 1 against the S&P even if they are volatile vs the domestic index.
# Beta filter is therefore DISABLED by default to avoid over-filtering NSE stocks.
# Enable it manually if you want to screen against yfinance's reported values,
# or when using a beta source calibrated against Nifty.
ENABLE_BETA_FILTER = False  # Set True to enable beta filter
MIN_BETA = 1.0              # Minimum beta (>1 means more volatile than market)
MAX_BETA = 3.0              # Maximum beta cap (avoids extremely erratic stocks)

# --- Filter Tolerance ---
# A stock that misses any numerical threshold by at most this many percentage
# points (or ratio units for D/E) is still included.
# Examples with FILTER_TOLERANCE_PCT = 0.5:
#   • CAGR ≥ 7%  → accepts ≥ 6.5%
#   • Pullback   → band widens by 0.5pp on each side (e.g. 9.5%–30.5%)
#   • D/E ≤ 0.5  → accepts ≤ 0.505  (0.5 % of the max added)
#   • Pledged ≤ 5%  → accepts ≤ 5.5%
#   • PE vs median  → accepts up to 0.5% above median
#   • Price vs 200 DMA  → accepts up to 0.5% below 200 DMA
FILTER_TOLERANCE_PCT = 0.5

# --- Data Fetching ---
HISTORICAL_PERIOD_YEARS = 3  # Years of historical price data to fetch
YFINANCE_BATCH_SIZE = 20     # Number of tickers to process concurrently
REQUEST_DELAY = 0.3          # Seconds between API calls to avoid rate limiting

# ============================================================================
#  YFINANCE RESILIENCE
# ============================================================================
# Yahoo's unofficial API rate-limits by IP. When it trips you get HTTP 429
# followed by 401 "Invalid Crumb", and `Ticker.info` starts returning {} —
# which silently guts the screener. yfinance itself has no backoff, so the
# retry/throttle layer lives in data_fetcher.py.

YF_MAX_RETRIES = 4           # Retry attempts per fetch after the first try
YF_RETRY_BASE_DELAY = 1.5    # Seconds; grows exponentially (1.5, 3, 6, 12...)
YF_RETRY_MAX_DELAY = 20.0    # Cap on any single backoff sleep
YF_RETRY_JITTER = 0.4        # Random 0..N seconds added, de-synchronises threads

# Minimum seconds between outbound yfinance requests, enforced globally across
# all worker threads. 0 disables throttling. Raising this trades scan speed for
# a much lower chance of tripping the limiter — worth it above ~8 threads.
YF_MIN_REQUEST_INTERVAL = 0.05

# Adaptive backpressure: when Yahoo starts throttling, automatically widen the
# request interval instead of burning retries at a pace Yahoo has already
# rejected, then ease back off once requests succeed again. This lets a long
# scan self-heal rather than needing a manual re-run with different flags.
YF_ADAPTIVE_THROTTLE = True
YF_MAX_REQUEST_INTERVAL = 1.20   # Ceiling for the adaptive interval (seconds)
YF_THROTTLE_GROWTH = 2.0         # Multiplier applied on each rate-limit hit
YF_THROTTLE_DECAY = 0.85         # Multiplier applied after a run of successes
YF_THROTTLE_RECOVER_AFTER = 25   # Consecutive successes before easing off

# --- Optional Yahoo Finance login ---
# yfinance >=1.7 accepts the `T` and `Y` cookies from a browser session that is
# logged into finance.yahoo.com. This governs SUBSCRIPTION ENTITLEMENT (premium
# data), NOT rate limits — throttling is per-IP, so logging in will not raise
# your quota. Provided only because it can help with entitlement-gated fields.
#
# To use: log in at finance.yahoo.com, open DevTools > Application/Storage >
# Cookies > https://finance.yahoo.com, copy the `T` and `Y` values, then set
# environment variables YF_COOKIE_T and YF_COOKIE_Y (preferred, keeps secrets
# out of the repo) or pass --yf-cookie-t / --yf-cookie-y.
YF_COOKIE_T = None
YF_COOKIE_Y = None

# --- NSE/BSE 500 ---
NIFTY500_URL = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500"
NIFTY500_CSV_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"

# --- Stock Universe ---
# "nifty500"   -> NIFTY 500 index constituents (~500 stocks)
# "nse_all"    -> NSE All Listed Equities EQ series (~2500 stocks, capped at UNIVERSE_SIZE)
STOCK_UNIVERSE = "nifty500"
UNIVERSE_SIZE = 2000          # Max stocks to scan when using "nse_all"
NSE_EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

# --- Kotak Neo API ---
KOTAK_NEO_CONSUMER_KEY = ""
KOTAK_NEO_CONSUMER_SECRET = ""
KOTAK_NEO_ENVIRONMENT = "prod"  # "prod" or "uat"

# --- Ranking Weights ---
# Composite Score = GROWTH_WEIGHT * growth_score + PE_WEIGHT * pe_discount_score
# Both sub-scores are min-max normalised to 0-100 across the candidate pool.
GROWTH_WEIGHT = 0.60   # Weight given to combined revenue+profit growth score
PE_WEIGHT = 0.40       # Weight given to PE discount vs 3yr median
GROWTH_CAP = 200.0     # Cap on growth % to prevent outliers dominating ranking

# ============================================================================
#  POSITIONAL TRADE MODE  (--mode positional)
# ============================================================================
# A fundamentals-first screen for multi-month holds. Unlike swing mode it:
#   • does NOT require a golden cross (a quality stock 15-40% off its high
#     normally has its 50 DMA below the 200 DMA — demanding a golden cross
#     would contradict the "buy quality on a dip" thesis)
#   • does NOT use the PE-vs-3yr-median check; quality metrics carry the load
#     and the 15%+ pullback supplies the discount
#   • demands high returns on capital, healthy margins and a strong balance
#     sheet, plus a mid-cap-or-larger size floor.

# --- Pullback band: buy quality only after a real correction ---
POS_MIN_PULLBACK_PCT = 15.0    # Must be at least 15% below the 52-week high
POS_MAX_PULLBACK_PCT = 40.0    # Beyond this the thesis is usually broken
# Bluechips (MCap >= BLUECHIP_MCAP_CR) are steadier and rarely correct as hard,
# so a shallower dip is a valid entry — they need only be this far off the high.
POS_BLUECHIP_MIN_PULLBACK_PCT = 10.0

# --- Size floor (₹ crore) — avoids micro-caps for multi-month holds ---
POS_MIN_MARKET_CAP_CR = 5000.0

# --- Return ratios ---
POS_MIN_ROE_PCT  = 15.0        # Return on Equity
POS_MIN_ROCE_PCT = 15.0        # Return on Capital Employed (EBIT / cap employed)

# --- Profitability margins ---
POS_MIN_NET_MARGIN_PCT       = 8.0
POS_MIN_OPERATING_MARGIN_PCT = 10.0

# --- Balance-sheet strength ---
POS_MAX_DEBT_TO_EQUITY    = 0.5
POS_MIN_CURRENT_RATIO     = 1.2
POS_MIN_INTEREST_COVERAGE = 3.0    # EBIT / Interest Expense
POS_REQUIRE_POSITIVE_FCF  = True   # Free cash flow must be positive

# --- Compounding growth (same CAGR engine as swing mode) ---
POS_MIN_COMPOUND_GROWTH_PCT = 7.0

# --- Promoter pledging ---
POS_MAX_PLEDGED_PCT = 5.0

# ============================================================================
#  CLASSIC QUALITY GATES  (Graham / Piotroski, ~1934-2000)
# ============================================================================
# Time-tested "quality vs value-trap" tests added to the positional screen.
# Two are HARD gates; the rest are computed and reported for ranking/review.

# --- HARD GATE 1: Earnings consistency (Graham "earnings stability") ---
# Graham required no annual loss over 10 years. yfinance typically exposes ~4
# annual periods, so we require NO annual net loss across all available years.
# Applies to every stock, including banks (a loss-making lender is a red flag).
POS_REQUIRE_EARNINGS_CONSISTENCY = True

# --- HARD GATE 2: Earnings quality / accruals (Piotroski signal #4) ---
# Operating cash flow should back up reported profit — "owner earnings" in
# Buffett's language. We require OCF / Net Income >= this ratio.
# Piotroski uses strict CFO > NI; 0.8 leaves slack for genuine working-capital
# build in fast growers. SKIPPED for banks/NBFCs (OCF is not meaningful there).
POS_MIN_OCF_TO_NI = 0.8

# --- REPORT-ONLY: Graham Number valuation sanity ---
# Graham: P/E (<=15) x P/B (<=1.5) should not exceed 22.5. Not gated by default
# (premium quality compounders routinely exceed it), but computed & flagged.
GRAHAM_MAX_PE_X_PB = 22.5

# --- REPORT-ONLY: Piotroski F-Score ---
# 0-9 fundamental-momentum score. Reported for every stock; can be turned into
# a gate by raising this above 0.
POS_MIN_PIOTROSKI_SCORE = 0   # 0 = report only (no gate). 5+ = decent, 7+ = strong.

# ============================================================================
#  BANK / NBFC / INSURANCE / EXCHANGE SECTOR OVERRIDES
# ============================================================================
# Financial-sector companies (banks, NBFCs, insurance, AMCs, stock exchanges)
# have fundamentally different economics — their business model is leverage
# itself, so many "normal" quality gates are either inapplicable or need
# completely different thresholds.
#
# These params apply automatically whenever a stock's industry/sector is
# detected as financial (see fundamental_quality.py for the keyword list).
# They can also be individually overridden via CLI flags (future work).
#
#   FCF             — banks don't produce "free cash flow" in the industrial
#                     sense (deposit inflows appear as operating outflows), so
#                     the FCF > 0 gate is SKIPPED entirely.
#   Operating margin — banks report NIM / NII, which yfinance can't compute
#                     as a margin; gate SKIPPED.
#   Current ratio   — deposits make this meaningless for banks; SKIPPED.
#   D/E ceiling     — banks operate at 8–10x leverage by design; a hard cap
#                     of 10x is still protective against true overleveraging.
#   ROCE → ROA      — EBIT / capital-employed is meaningless for banks;
#                     ROA >= 1% (for banks) / 2% (for light-touch financials)
#                     is the conventional capital-efficiency test instead.
#   Interest coverage — banks earn interest; the ratio flips; SKIPPED.
#   ROE             — stays, but financials often run higher; min raised.

# Industries/sectors that trigger the financial-sector profile (case-insensitive
# substring match on yfinance's ``industry`` or ``sector`` field).
FINANCIAL_SECTOR_KEYWORDS = [
    "bank", "nbfc", "insurance", "asset management", "brokerage",
    "capital markets", "financial services", "mortgage", "microfinance",
    "diversified financial", "credit services",
]

# D/E ceiling for banks/NBFCs (10x = 1000% debtToEquity in yfinance notation)
POS_BANK_MAX_DEBT_TO_EQUITY = 10.0

# ROE floor for financial-sector stocks (often runs 12-20% for good banks)
POS_BANK_MIN_ROE_PCT = 12.0

# Minimum Return on Assets for financial-sector stocks
# Good private banks target > 1.5%; 1.0% is the floor for quality NBFCs.
POS_BANK_MIN_ROA_PCT = 1.0

# Net margin floor — banks can clear 20%+ but 10% is a reasonable floor
POS_BANK_MIN_NET_MARGIN_PCT = 10.0

# --- Positional ranking weights (should sum to 1.0) ---
# Composite = quality × W1 + growth × W2 + financial health × W3
POS_QUALITY_WEIGHT    = 0.40   # ROE, ROCE, net margin
POS_GROWTH_WEIGHT     = 0.35   # revenue & profit CAGR
POS_FIN_HEALTH_WEIGHT = 0.25   # low D/E, current ratio, interest coverage

# --- Output ---
OUTPUT_DIR = "output"
RESULTS_FILE = "swing_trade_candidates.csv"
POS_RESULTS_FILE = "positional_candidates.csv"
