# Stock Screener (ResearchSM)

An agentic stock screener for the **NIFTY 500** or the full **NSE listed-equity**
universe. Three strategies are available via `--mode`:

- **`positional`** (default) — multi-month holds: high-quality businesses that
  have corrected at least 15% from their 52-week high (10% for bluechips).
- **`momentum`** — trend rides: stocks making fresh 52-week / 20-day highs with
  positive QoQ results, in trending/emerging sectors.
- **`swing`** — short-horizon trades: a confirmed golden cross in a stock that
  is cheap relative to its own valuation history.

## Modes at a glance

| | **positional** (default) | **momentum** (`--mode momentum`) | **swing** (`--mode swing`) |
|---|---|---|---|
| Horizon | Multi-month hold | Weeks-to-months trend ride | Short-term trade |
| Price setup | 15-40% off 52W high (dip) | Near 52W high + fresh 20D high | Golden cross, price ≥ 200DMA |
| Results check | ROE/ROCE/margins/FCF + CAGR | QoQ revenue OR profit up | PE < 3yr median + CAGR |
| Sector logic | — | Trending/emerging (hybrid) | — |
| Market cap floor | ₹5,000 Cr | ₹1,000 Cr | None |
| Core thesis | Quality business on sale | Chase strength, not value | Cheap vs own history + turning up |
| Ranking | 40% Quality + 35% Growth + 25% Fin-Health | 45% Price + 30% Results + 25% Sector | 60% Growth + 40% PE Discount |
| Output | `output/positional_candidates.csv` | `output/momentum_candidates.csv` | `output/swing_trade_candidates.csv` |

## Filters

### Swing mode

| # | Filter | Criteria | Data Source |
|---|--------|----------|-------------|
| 1 | **Golden Cross** | 50 DMA crossed above 200 DMA within the last 15 trading days with a meaningful spread at the crossing point, **or** has stayed above with no death cross in the last 2 quarters. Also requires price ≥ 200 DMA (rejects failed breakouts). | yfinance (historical prices) |
| 2 | **PE Valuation** | Current trailing PE < 3-year median PE | yfinance valuation measures / statements |
| 3 | **Compound Growth** | Revenue **and** profit CAGR over last 4 quarters ≥ 7% | yfinance quarterly income stmt |
| 4 | **Debt Quality** | D/E < 0.5, promoter pledged < 5% | yfinance balance sheet + NSE India |
| 5 | **Price Momentum** | 10-30% below 52-week high (beta filter off by default) | yfinance |

### Positional mode

| # | Filter | Criteria | Data Source |
|---|--------|----------|-------------|
| 1 | **Pullback** | At least 15% (up to 40%) below the 52-week high. Bluechips (MCap ≥ ₹1L Cr) need only be 10% off. | yfinance |
| 2 | **Fundamental Quality** | Market cap ≥ ₹5,000 Cr, ROE ≥ 15%, ROCE ≥ 15%, net margin ≥ 8%, operating margin ≥ 10%, current ratio ≥ 1.2, interest coverage ≥ 3x, positive free cash flow | yfinance info + income stmt / balance sheet / cash flow |
| 3 | **Debt Quality** | D/E < 0.5, promoter pledged < 5% (banks up to 10x D/E) | yfinance + NSE India |
| 4 | **Compound Growth** | Revenue **and** profit CAGR ≥ 7%. Bluechips accept 3 quarters. | yfinance quarterly income stmt |
| 5 | **Earnings Consistency** *(Graham)* | No annual net loss across available years (~4). All stocks incl. banks. | yfinance annual income stmt |
| 6 | **Earnings Quality** *(Piotroski)* | Operating cash flow / Net income ≥ 0.8 — profit must be cash-backed. Skipped for banks/NBFCs. | yfinance cash flow stmt |

**Report-only** (shown in CSV/table, not gated by default): Piotroski F-Score (0–9), Graham Number (P/E × P/B), Debt/EBITDA, dividend yield.

**Bank / NBFC / Insurance**: auto-detected stocks marked `[B]`. Use ROA ≥ 1% instead of ROCE, D/E up to 10x, ROE ≥ 12%, net margin ≥ 10%. FCF, operating margin, current ratio, interest coverage, and the OCF/NI gate are all skipped.

### Momentum mode

| # | Filter | Criteria | Data Source |
|---|--------|----------|-------------|
| 1 | **Breakout / new high** | Current close within 2% of the 52-week high AND at a fresh 20-day closing high | yfinance daily prices |
| 2 | **QoQ Results** | Latest quarter revenue OR net profit up vs the previous quarter; latest profit positive | yfinance quarterly income stmt |
| 3 | **Size guard** | Market cap ≥ ₹1,000 Cr (set 0 to disable) | yfinance info |

**Ranking** = 45% Price momentum + 30% QoQ results + 25% Sector strength. Sector strength is **hybrid**: data-driven sector 3-month momentum plus a curated trending-theme bonus (defence, renewables, EV, semiconductors, railways, capital goods, power, fintech...). Trending stocks are flagged `*`; sectors are a **ranking boost, not a hard filter**.

Stocks must pass **all filters** for the active mode.

### Filter tolerance

Every numeric threshold honours a **±0.5 percentage-point tolerance**
(`--filter-tolerance`, `config.FILTER_TOLERANCE_PCT`) so a stock that misses a
bar by a hair is still included. For example CAGR ≥ 7% accepts ≥ 6.5%, and the
pullback band 10-30% widens to 9.5-30.5%. Use `--filter-tolerance 0` for strict.

### Derived metrics

`ROCE` and `Interest Coverage` are not exposed by yfinance and are computed:

```
ROCE              = EBIT / (Total Assets - Current Liabilities)
Interest Coverage = EBIT / Interest Expense
```

yfinance's `info` dict is unreliable for NSE tickers (ROE, current ratio and free
cash flow are absent for ~90% of symbols, and it degrades further under rate
limiting). Anything missing from `info` is therefore recomputed from the annual
statements — including PE and market cap — which keeps the screener working when
Yahoo returns `HTTP 429 / Invalid Crumb`.

## yfinance Reliability

Yahoo's unofficial API rate-limits **by IP**. Once tripped it returns `HTTP 429`,
then `401 Invalid Crumb`, and `Ticker.info` starts returning `{}` **without
raising** — which silently guts the screener rather than failing loudly.
yfinance itself has no backoff, so `data_fetcher.py` adds:

| Mechanism | What it does |
|---|---|
| **Retry with backoff** | Up to `YF_MAX_RETRIES` attempts, exponential delay + jitter so parallel workers don't resynchronise into the limiter. |
| **Empty-payload detection** | A response missing every expected field is treated as throttling and retried — this is what catches the silent `{}` failure mode. |
| **Crumb/cookie reset** | On a 401, clears the cached cookie+crumb on yfinance's `YfData` singleton so it re-negotiates. Without this every later call keeps failing with the same stale pair. |
| **Global throttle** | A shared minimum interval between requests across all threads. |
| **Adaptive backpressure** | The interval widens on each throttle signal (up to `YF_MAX_REQUEST_INTERVAL`) and eases back after a streak of successes, so a long scan self-tunes. |
| **Per-symbol `info` cache** | Five call sites need `info`; it is fetched once per symbol behind a lock. |
| **Permanent-vs-transient triage** | Genuine "no data for this ticker" errors return immediately instead of burning retries. |

Measured effect on an identical 1000-stock positional scan:

| | Fixed throttle | Adaptive |
|---|---|---|
| Retries | 400 | **24** |
| Crumb resets | 100 | **6** |
| Failed fetches | 84 | **6** |
| Candidates found | 23 | **24** |

Tuning: `--yf-max-retries`, `--yf-throttle SEC`, `--no-adaptive-throttle`.

### Yahoo login (optional, rarely useful)

yfinance ≥1.7 accepts the `T` and `Y` cookies from a browser logged into
finance.yahoo.com. **This governs subscription entitlement, not rate limits** —
throttling is per-IP, so logging in will *not* raise your quota. It is wired up
only for entitlement-gated fields.

```bash
# Preferred: environment variables, so cookies stay out of shell history
$env:YF_COOKIE_T = "..."; $env:YF_COOKIE_Y = "..."
python main.py --no-kotak           # positional is the default

# Or explicitly
python main.py --no-kotak --yf-cookie-t "..." --yf-cookie-y "..."
```

Obtain them from DevTools → Application/Storage → Cookies →
`https://finance.yahoo.com` → copy the `T` and `Y` values.

## Architecture

```
main.py                      # Entry point (CLI, mode selection)
config.py                    # All configurable parameters (both modes)
stock_universe.py            # NIFTY 500 / NSE all-equities list fetcher
kotak_client.py              # Kotak Neo API wrapper (live quotes, enrichment)
data_fetcher.py              # yfinance prices/financials + NSE pledging
screener.py                  # Orchestrator: both pipelines, ranking, output
filters/
  golden_cross.py            # 50/200 DMA crossover (fresh or sustained)
  pe_valuation.py            # Current PE vs 3-year median PE
  quarterly_growth.py        # 4-quarter compound sales & profit CAGR
  debt_quality.py            # D/E ratio and promoter pledging
  price_momentum.py          # 52-week pullback band + optional beta
  fundamental_quality.py     # ROE, ROCE, margins, liquidity, coverage, FCF
  price_breakout.py          # Momentum: near 52W high + fresh 20-day high
  quarterly_momentum.py      # Momentum: QoQ revenue/profit results check
utils.py                     # Logging, helpers
output/                      # Results CSVs and logs (last 2 retained)
```

## Quick Start

```bash
pip install -r requirements.txt
```

```bash
# Positional, NIFTY 500 (DEFAULT — no --mode needed)
python main.py --no-kotak --threads 8

# Positional, NSE 1000
python main.py --no-kotak --universe nse_all --universe-size 1000 --threads 8

# Positional, stricter quality and large caps only
python main.py --no-kotak --min-roe 20 --min-roce 20 --min-market-cap 20000

# Positional, deeper corrections (20-50% off the high)
python main.py --no-kotak --pos-min-pullback 20 --pos-max-pullback 50

# Momentum, NIFTY 500 (fresh highs + QoQ results + hot sectors)
python main.py --no-kotak --mode momentum --threads 8

# Momentum, stricter (both rev+profit up QoQ and beating year-ago)
python main.py --no-kotak --mode momentum --mom-qoq-mode both_and_yoy

# Swing, NIFTY 500
python main.py --no-kotak --mode swing --threads 8

# Swing, NSE 1000 with a PE cap
python main.py --no-kotak --mode swing --universe nse_all --universe-size 1000 --max-pe 30 --threads 8

# With Kotak Neo token (adds live quote enrichment of final candidates)
python main.py --token YOUR_KOTAK_NEO_ACCESS_TOKEN --threads 8
```

### Universe selection

- `--universe nifty500` (default) — ~500 index constituents.
- `--universe nse_all --universe-size N` — NSE EQ-series list (~2,300 stocks),
  capped at N **alphabetically**. So "NSE 1000" is an alphabetical slice, not
  the 1000 largest companies.

## CLI Options

Run `python main.py --help` for the authoritative list.

```
Screener Mode:
  --mode {swing,positional,momentum}   Strategy (default: positional)

Scan Settings:
  --universe {nifty500,nse_all}
  --universe-size N           Cap when using nse_all (default: 2000)
  --threads N                 Concurrent threads (default: 6)
  --short-dma N               Short DMA period (default: 50)
  --long-dma N                Long DMA period (default: 200)

Shared Filters:
  --max-de N                  Max Debt-to-Equity (default: 0.5)
  --max-pledged N             Max promoter pledged % (default: 5.0)
  --max-pe N                  Absolute PE cap (swing, default: off)
  --min-compound-growth N     Min revenue/profit CAGR % (default: 7)
  --filter-tolerance PCT      Tolerance in pp (default: 0.5; 0 = strict)

Positional Mode Filters:
  --pos-min-pullback N        Min % below 52W high (default: 15)
  --pos-max-pullback N        Max % below 52W high (default: 40)
  --min-market-cap CR         Min market cap in ₹ crore (default: 5000)
  --min-roe N                 Min Return on Equity % (default: 15)
  --min-roce N                Min Return on Capital Employed % (default: 15)
  --min-net-margin N          Min net profit margin % (default: 8)
  --min-operating-margin N    Min operating margin % (default: 10)
  --min-current-ratio N       Min current ratio (default: 1.2)
  --min-interest-coverage N   Min EBIT/Interest (default: 3.0)
  --allow-negative-fcf        Drop the positive free-cash-flow requirement

Momentum Mode Filters:
  --mom-near-high PCT         Max % below 52W high for "near high" (default: 2)
  --mom-breakout-days N       Fresh N-day high lookback (default: 20)
  --mom-qoq-mode {either,both,both_and_yoy}   QoQ results rule (default: either)
  --mom-min-market-cap CR     Min market cap floor, 0 disables (default: 1000)

Swing Mode Filters:
  --no-pullback-filter        Disable the 52W pullback filter
  --min-pullback N            Min % below 52W high (default: 10)
  --max-pullback N            Max % below 52W high (default: 30)
  --no-beta-filter            Disable beta filter (off by default anyway)
  --min-beta N / --max-beta N Beta bounds

Ranking Weights:
  --growth-weight N           Swing growth weight 0-1 (default: 0.6)
  --pe-weight N               Swing PE weight 0-1 (default: 0.4)
```

## Output

The console prints a ranked table; full data goes to CSV in `output/`. Only the
last 2 CSVs and logs are retained automatically.

**Swing** (`swing_trade_candidates.csv`): `symbol`, `company_name`,
`current_price`, `dma_50`, `dma_200`, `crossover_date`, `days_since_crossover`,
`current_pe`, `median_pe_3yr`, `pe_discount_pct`, `revenue_cagr_pct`,
`profit_cagr_pct`, `debt_to_equity`, `pledged_pct`, `week52_high`,
`pullback_from_52w_high_pct`, `beta`, `growth_score`, `pe_score`,
`composite_score`, `rank`.

**Positional** (`positional_candidates.csv`): `symbol`, `company_name`,
`current_price`, `market_cap_cr`, `is_financial_sector`,
`roe_pct`, `roce_pct`, `roa_pct`, `net_margin_pct`, `operating_margin_pct`,
`gross_margin_pct`, `current_ratio`, `interest_coverage`, `free_cashflow`,
`price_to_book`, `peg_ratio`,
`piotroski_score`, `piotroski_max`, `ocf_to_ni`, `has_annual_loss`,
`annual_periods`, `debt_to_ebitda`, `pe_x_pb`, `graham_ok`,
`dividend_yield_pct`, `payout_ratio_pct`,
`revenue_cagr_pct`, `profit_cagr_pct`, `avg_revenue_growth_pct`,
`avg_profit_growth_pct`, `debt_to_equity`, `pledged_pct`,
`week52_high`, `pullback_from_52w_high_pct`, `beta`,
`quality_score`, `growth_score`, `health_score`, `composite_score`, `rank`.

**Momentum** (`momentum_candidates.csv`): `symbol`, `company_name`, `sector`,
`industry`, `current_price`, `market_cap_cr`, `high_52w`, `high_20d`,
`dist_from_52w_high_pct`, `ret_3m_pct`, `ret_6m_pct`, `above_200dma_pct`,
`qoq_revenue_pct`, `qoq_profit_pct`, `yoy_revenue_pct`, `yoy_profit_pct`,
`latest_quarterly_revenue`, `latest_quarterly_profit`, `sector_momentum_pct`,
`is_trending_sector`, `price_momentum_score`, `results_score`, `sector_score`,
`composite_score`, `rank`.

## Ranking

**Swing:** `60% × Growth + 40% × PE Discount`, where Growth is the average of
normalised revenue-CAGR and profit-CAGR ranks.

**Momentum:** `45% × Price Momentum + 30% × QoQ Results + 25% × Sector Strength`
- Price Momentum — average of normalised 3M return, 6M return and proximity to 52W high
- QoQ Results — average of normalised QoQ revenue and profit change
- Sector Strength — data-driven sector 3M momentum + curated trending-theme bonus

**Positional:** `40% × Quality + 35% × Growth + 25% × Financial Health`
- Quality — average of normalised ROE, ROCE and net-margin ranks
- Growth — average of normalised revenue-CAGR and profit-CAGR ranks
- Financial Health — average of normalised low-D/E, current-ratio and
  interest-coverage ranks

All sub-scores are min-max normalised to 0-100 across the candidate pool, so no
dimension dominates because of its units. Growth outliers are capped
(`config.GROWTH_CAP`) so a single 20,000% swing does not flatten the scale.

## Configuration

Edit `config.py` to change defaults for moving averages, PE history, growth
thresholds, debt limits, pullback bands, filter tolerance, the full positional
quality block, ranking weights, universe, and output paths.

## Data Sources

| Component | Source | Notes |
|-----------|--------|-------|
| Stock Universe | NSE India (CSV/API) | NIFTY 500 or all EQ-series equities |
| Historical Prices | yfinance | 3+ years daily OHLCV |
| PE Ratios | yfinance valuation measures, statements fallback | Quarterly trailing PE |
| Quarterly Financials | yfinance quarterly income stmt | Revenue, Net Income |
| Quality Metrics | yfinance info + income stmt / balance sheet / cash flow | ROE, ROCE, margins, liquidity, coverage, FCF |
| Pledged % | NSE India API | Promoter pledged shares |
| Live Quotes | Kotak Neo API | LTP, volume, change (optional) |

> **Note:** Kotak Neo does not provide historical data. It is used for
> authentication validation and real-time price enrichment of final candidates.

## Troubleshooting

- **`gave up: N` in the summary** — that many fetches exhausted their retries,
  so results may be incomplete. Re-run with `--yf-throttle 0.2` and fewer
  `--threads`, or wait a few minutes for the IP limit to reset.
- **Everything returns 0 candidates / `Passed PE Filter: 0`** — heavy Yahoo rate
  limiting. The retry layer and statement fallbacks normally absorb this; if it
  persists, lower `--threads`, raise `--yf-throttle`, or shrink the universe.
- **Scan feels slow** — check the `adaptive throttle: peaked at X` line. A high
  peak means Yahoo was throttling hard and the scan deliberately slowed to keep
  data complete. `--no-adaptive-throttle` restores speed at the cost of accuracy.
- **Too few candidates** — relax with `--filter-tolerance 1`, lower
  `--min-roe`/`--min-roce`, drop `--min-market-cap`, or widen the pullback band.

## Devin Skill

Registered as a Devin skill (`.devin/skills/swing-screener/`). Trigger it with:
- "Find fundamentally good stocks that are down 15% from their highs" (positional)
- "Find stocks making new highs with strong results in hot sectors" (momentum)
- "Find swing trade candidates" (swing)
- "Run the positional screener on NSE 1000"
