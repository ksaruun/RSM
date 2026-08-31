# Stock Screener (Swing + Positional)

Run the stock screener at `ResearchSM/` to find trade candidates from the NIFTY 500
or the full NSE listed-equity universe. Two strategies are available via `--mode`.

## When to use

Invoke this skill when the user asks about:
- Finding swing trade or positional trade stocks / candidates
- Screening NIFTY 500 / NSE 500 / NSE 1000 / NSE 2000 for opportunities
- Running the stock screener / market scanner
- Stocks with a golden cross, undervalued PE, or growth momentum (**swing**)
- Fundamentally strong / quality stocks that are down X% from their 52-week high (**positional**)
- Stocks filtered by ROE, ROCE, margins, cash flow, debt, or promoter pledging

## Choosing a mode

| | **positional** (default) | **swing** (`--mode swing`) |
|---|---|---|
| Horizon | Multi-month hold | Short-term trade |
| Golden cross | Not used | Required |
| PE vs 3yr median | Not used | Required |
| Pullback from 52W high | 15-40% (bluechips 10%) | 10-30% |
| Core thesis | Quality business on sale | Cheap vs own history + turning up |
| Market cap floor | ₹5,000 Cr | None |
| Ranking | 40% Quality + 35% Growth + 25% Fin-Health | 60% Growth + 40% PE Discount |
| Output CSV | `output/positional_candidates.csv` | `output/swing_trade_candidates.csv` |

Pick **positional** whenever the user emphasises *fundamentally good / quality*
stocks or asks for stocks *down at least N%* from their highs.

## Filters

### Swing mode
1. **Golden Cross** — 50 DMA crossed above the 200 DMA within the last 15 trading
   days (with a meaningful spread at the crossing point), **or** has stayed above
   with no death cross in the last 2 quarters. Also requires price ≥ 200 DMA, which
   rejects failed breakouts.
2. **PE Valuation** — current trailing PE below the 3-year median PE.
3. **Compound Growth** — revenue and profit CAGR over the last 4 quarters ≥ 7%.
4. **Debt Quality** — D/E < 0.5, promoter pledged shares < 5%.
5. **Price Momentum** — 10-30% below the 52-week high; beta filter off by default.

### Positional mode
1. **Pullback** — at least 15% (up to 40%) below the 52-week high.
   *Bluechips (MCap ≥ ₹1L Cr) need only be 10% off* (`POS_BLUECHIP_MIN_PULLBACK_PCT`).
2. **Fundamental Quality** — market cap ≥ ₹5,000 Cr, ROE ≥ 15%, ROCE ≥ 15%,
   net margin ≥ 8%, operating margin ≥ 10%, current ratio ≥ 1.2,
   interest coverage ≥ 3x, positive free cash flow.
3. **Debt Quality** — D/E < 0.5, promoter pledged shares < 5%.
4. **Compound Growth** — revenue and profit CAGR ≥ 7% (bluechips accept 3
   quarters when yfinance only has 3, via `BLUECHIP_MIN_QUARTERS`).

Plus two time-tested **classic quality gates** (Graham / Piotroski):
- **Earnings consistency** — reject if any annual net loss in the available
  years (~4). Applies to every stock, including banks.
- **Earnings quality (accruals)** — operating cash flow must back reported
  profit: OCF/NI ≥ 0.8 (`POS_MIN_OCF_TO_NI`). *Skipped for banks/NBFCs.*

And **report-only** signals (shown in the table/CSV, not gated by default):
Piotroski **F-Score** (`FScr` col, x/9), Graham Number (`pe_x_pb`, `graham_ok`),
`debt_to_ebitda`, dividend yield/payout. Turn F-Score into a gate by setting
`POS_MIN_PIOTROSKI_SCORE` > 0 (5+ decent, 7+ strong).

### Bank / NBFC / insurance handling
Financial-sector stocks (detected via `FINANCIAL_SECTOR_KEYWORDS` on sector/
industry) use a **bank-adjusted profile**, marked `[B]` in the table:
- ROCE → **ROA ≥ 1%** (capital-employed is ill-defined for lenders)
- D/E ceiling raised to **10x** (`POS_BANK_MAX_DEBT_TO_EQUITY`)
- ROE ≥ 12%, net margin ≥ 10% (`POS_BANK_MIN_*`)
- **Skipped:** FCF, operating margin, current ratio, interest coverage, and the
  OCF/NI earnings-quality gate — all inapplicable to deposit-funded businesses.

All numeric thresholds honour a **±0.5pp tolerance** (`--filter-tolerance`) so a
stock that misses a bar by a hair is still included.

## Running the screener

Always `cd C:\Users\Arun_KumarSingh\TGIF\ResearchSM` first. Use `--no-kotak`
unless the user supplies a Kotak Neo token.

```bash
# Positional, NIFTY 500 (DEFAULT — no --mode needed)
python main.py --no-kotak --threads 8

# Positional, NSE 1000
python main.py --no-kotak --universe nse_all --universe-size 1000 --threads 8

# Positional, stricter quality and large caps only
python main.py --no-kotak --min-roe 20 --min-roce 20 --min-market-cap 20000

# Positional, deeper corrections (20-50% off the high)
python main.py --no-kotak --pos-min-pullback 20 --pos-max-pullback 50

# Swing, NIFTY 500
python main.py --no-kotak --mode swing --threads 8

# Swing, NSE 1000 with a PE cap
python main.py --no-kotak --mode swing --universe nse_all --universe-size 1000 --max-pe 30 --threads 8
```

### Universe selection
- `--universe nifty500` (default) — ~500 index constituents
- `--universe nse_all --universe-size N` — NSE EQ-series list (~2,300 stocks),
  capped at N and taken **alphabetically**, so "NSE 1000" is an alphabetical
  slice, not the 1000 largest companies.

### Useful flags
| Flag | Purpose |
|---|---|
| `--mode {swing,positional}` | Strategy |
| `--universe`, `--universe-size` | Stock universe |
| `--threads N` | Concurrency (8 is safe) |
| `--max-pe` | Absolute PE cap (swing) |
| `--min-compound-growth` | CAGR bar, default 7 |
| `--filter-tolerance` | Tolerance in pp, default 0.5; use 0 for strict |
| `--pos-min-pullback`, `--pos-max-pullback` | Positional pullback band |
| `--min-market-cap CR` | Positional size floor in ₹ crore |
| `--min-roe`, `--min-roce` | Positional return bars |
| `--min-net-margin`, `--min-operating-margin` | Positional margin bars |
| `--min-current-ratio`, `--min-interest-coverage` | Positional balance-sheet bars |
| `--allow-negative-fcf` | Drop the positive-FCF requirement |

Run `python main.py --help` for the full list.

## Output

Console prints a ranked table; full data goes to CSV in `ResearchSM/output/`.
Only the last 2 CSVs and logs are retained automatically.

- **Swing** → `swing_trade_candidates.csv`: price, DMAs, crossover date,
  `current_pe`, `median_pe_3yr`, `pe_discount_pct`, `revenue_cagr_pct`,
  `profit_cagr_pct`, `debt_to_equity`, `pledged_pct`, pullback, scores.
- **Positional** → `positional_candidates.csv`: `market_cap_cr`, `roe_pct`,
  `roce_pct`, `roa_pct`, `net_margin_pct`, `operating_margin_pct`,
  `current_ratio`, `interest_coverage`, `free_cashflow`, `price_to_book`,
  `is_financial_sector`, `piotroski_score`/`piotroski_max`, `ocf_to_ni`,
  `has_annual_loss`, `debt_to_ebitda`, `pe_x_pb`, `graham_ok`,
  `dividend_yield_pct`, `payout_ratio_pct`, `revenue_cagr_pct`,
  `profit_cagr_pct`, `debt_to_equity`, `pledged_pct`, pullback,
  `quality_score`, `growth_score`, `health_score`, `composite_score`.

## Steps for the agent

1. `cd C:\Users\Arun_KumarSingh\TGIF\ResearchSM`
2. Choose the mode from the user's intent. **Positional is the default** — no
   `--mode` flag needed. Only add `--mode swing` for short-horizon/technical runs.
3. Run `python main.py` with `--no-kotak` plus the relevant flags. Run it in the
   background and poll, since a 1000-stock scan takes ~2-3 minutes.
4. Present the ranked candidates as a markdown table, and state the filter funnel
   (how many passed each stage) so the user can see how selective the run was.
5. If the user only wants to re-filter an existing run (e.g. "show me these with
   PE < 50"), read the CSV in `output/` with pandas instead of re-running the scan.

## Rate limiting

Yahoo rate-limits by IP (`HTTP 429` → `401 Invalid Crumb` → `info` silently
returns `{}`). The fetcher handles this automatically: retries with exponential
backoff, cookie/crumb reset, empty-payload detection, a global request throttle
that **adapts** upward under pressure and eases back on success, plus
statement-based fallbacks for PE, market cap, ROE, margins, current ratio and FCF.

Every run reports `yfinance retries: N | crumb resets: N | gave up: N`.
**Always check `gave up`** — if it is non-zero, tell the user results may be
incomplete and suggest `--yf-throttle 0.2` with fewer `--threads`.

Relevant flags: `--yf-max-retries`, `--yf-throttle SEC`,
`--no-adaptive-throttle`, `--yf-cookie-t` / `--yf-cookie-y` (Yahoo login;
entitlement only, does **not** raise rate limits).

## Troubleshooting

- **Everything returns 0 / `Passed PE Filter: 0`** — heavy Yahoo throttling.
  Lower `--threads`, raise `--yf-throttle`, or wait a few minutes.
- **Scan is slow** — check the `adaptive throttle: peaked at X` line; a high
  peak means the scan deliberately slowed to keep data complete.
- **Too few candidates** — relax with `--filter-tolerance 1`, lower
  `--min-roe`/`--min-roce`, drop `--min-market-cap`, or widen the pullback band.
