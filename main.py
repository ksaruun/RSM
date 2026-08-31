"""
Swing Trade Screener - Main Entry Point
========================================

An agentic stock screener for the NIFTY 500 or NSE All Listed Equities
universe. Two strategies are available via --mode.

POSITIONAL mode (default) — multi-month holds in quality businesses
that have corrected. No golden cross, no PE-vs-median:
  1. Pullback: at least 15% (up to 40%) below the 52-week high
  2. Fundamental Quality: market cap >= Rs 5,000 Cr, ROE >= 15%, ROCE >= 15%,
     net margin >= 8%, operating margin >= 10%, current ratio >= 1.2,
     interest coverage >= 3x, positive free cash flow
  3. Debt Quality: D/E < 0.5, Promoter pledged shares < 5%
  4. Compounding Growth: sales & profit CAGR >= 7%

Usage:
  python main.py --token <KOTAK_NEO_ACCESS_TOKEN>
  python main.py --token <TOKEN> --consumer-key <KEY> --consumer-secret <SECRET>
  python main.py --no-kotak   # Run without Kotak Neo (yfinance only)

Architecture:
  - Kotak Neo API: Authentication, live quotes, real-time price enrichment
  - yfinance: Historical prices (DMA), quarterly financials, PE ratios,
              balance sheet (D/E), 52-week high/low, beta
  - NSE India: NIFTY 500 constituent list, promoter pledging data
"""
import argparse
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from data_fetcher import apply_yahoo_login
from kotak_client import KotakNeoClient
from screener import SwingTradeScreener
from utils import setup_logging


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Stock Screener - swing trades (golden cross + value) or "
            "positional trades (quality fundamentals on a pullback)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # With Kotak Neo token (for live market data enrichment)
  python main.py --token YOUR_ACCESS_TOKEN

  # Without Kotak Neo (uses yfinance only - fully functional)
  python main.py --no-kotak

  # Tighter pullback window: only 10-15% below 52W high
  python main.py --no-kotak --min-pullback 10 --max-pullback 15

  # Disable beta filter (include low-beta stocks too)
  python main.py --no-kotak --no-beta-filter

  # More aggressive: beta > 1.5, max D/E 0.3
  python main.py --no-kotak --min-beta 1.5 --max-de 0.3

  # Require 15% compound growth instead of default 7%
  python main.py --no-kotak --min-compound-growth 15

  # High threads for faster scan
  python main.py --no-kotak --threads 8

  # Scan NSE All Listed Equities (top 2000)
  python main.py --no-kotak --universe nse_all --threads 8

  # Scan only top 500 from NSE All (same as nifty500 in size, broader coverage)
  python main.py --no-kotak --universe nse_all --universe-size 500

  # POSITIONAL: quality stocks at least 15% off their 52-week high
  python main.py --no-kotak --mode positional --universe nse_all --universe-size 1000

  # POSITIONAL, stricter quality: ROE/ROCE >= 20%, large caps only
  python main.py --no-kotak --mode positional --min-roe 20 --min-roce 20 --min-market-cap 20000

  # POSITIONAL, deeper correction hunting (20-50% off the high)
  python main.py --no-kotak --mode positional --pos-min-pullback 20 --pos-max-pullback 50
        """,
    )

    auth_group = parser.add_argument_group("Authentication")
    auth_group.add_argument(
        "--token",
        type=str,
        default=None,
        help="Kotak Neo access token for live market data",
    )
    auth_group.add_argument(
        "--consumer-key",
        type=str,
        default="",
        help="Kotak Neo consumer key (optional)",
    )
    auth_group.add_argument(
        "--consumer-secret",
        type=str,
        default="",
        help="Kotak Neo consumer secret (optional)",
    )
    auth_group.add_argument(
        "--no-kotak",
        action="store_true",
        help="Run without Kotak Neo API (yfinance only mode)",
    )

    mode_group = parser.add_argument_group("Screener Mode")
    mode_group.add_argument(
        "--mode",
        type=str,
        default="positional",
        choices=["swing", "positional"],
        help=(
            "Screening strategy. 'positional' (default) = fundamentals-first "
            "(ROE/ROCE/margins/cash flow + classic Graham/Piotroski gates) on quality "
            f"stocks at least {config.POS_MIN_PULLBACK_PCT:.0f}%% off their 52-week high, "
            "for multi-month holds. 'swing' = golden cross + PE-vs-median + growth "
            "for short-horizon trades."
        ),
    )

    scan_group = parser.add_argument_group("Scan Settings")
    scan_group.add_argument(
        "--universe",
        type=str,
        default=None,
        choices=["nifty500", "nse_all"],
        help="Stock universe to scan: 'nifty500' (~500) or 'nse_all' (up to --universe-size, default: nifty500)",
    )
    scan_group.add_argument(
        "--universe-size",
        type=int,
        default=None,
        help=f"Max stocks when using --universe nse_all (default: {config.UNIVERSE_SIZE})",
    )
    scan_group.add_argument(
        "--threads",
        type=int,
        default=6,
        help="Number of concurrent threads for data fetching (default: 6)",
    )
    scan_group.add_argument(
        "--short-dma",
        type=int,
        default=None,
        help=f"Short DMA period (default: {config.SHORT_DMA_PERIOD})",
    )
    scan_group.add_argument(
        "--long-dma",
        type=int,
        default=None,
        help=f"Long DMA period (default: {config.LONG_DMA_PERIOD})",
    )

    debt_group = parser.add_argument_group("Debt Quality Filter")
    debt_group.add_argument(
        "--max-de",
        type=float,
        default=None,
        help=f"Max Debt-to-Equity ratio (default: {config.MAX_DEBT_TO_EQUITY})",
    )
    debt_group.add_argument(
        "--max-pledged",
        type=float,
        default=None,
        help=f"Max promoter pledged %% (default: {config.MAX_PLEDGED_PCT})",
    )

    pe_group = parser.add_argument_group("PE Valuation Filter")
    pe_group.add_argument(
        "--max-pe",
        type=float,
        default=None,
        help=f"Absolute PE cap (default: {config.MAX_PE_ABSOLUTE}, disabled)",
    )

    growth_group = parser.add_argument_group("Quarterly Growth Filter")
    growth_group.add_argument(
        "--min-compound-growth",
        type=float,
        default=None,
        help=f"Minimum compound annual growth rate (CAGR) for revenue and profit (default: {config.MIN_COMPOUND_GROWTH_PCT}%%)",
    )

    pos_group = parser.add_argument_group(
        "Positional Mode Filters (only used with --mode positional)"
    )
    pos_group.add_argument(
        "--pos-min-pullback",
        type=float,
        default=None,
        help=f"Min %% below 52W high (default: {config.POS_MIN_PULLBACK_PCT})",
    )
    pos_group.add_argument(
        "--pos-max-pullback",
        type=float,
        default=None,
        help=f"Max %% below 52W high (default: {config.POS_MAX_PULLBACK_PCT})",
    )
    pos_group.add_argument(
        "--min-market-cap",
        type=float,
        default=None,
        metavar="CR",
        help=f"Min market cap in Rs crore (default: {config.POS_MIN_MARKET_CAP_CR:,.0f})",
    )
    pos_group.add_argument(
        "--min-roe",
        type=float,
        default=None,
        help=f"Min Return on Equity %% (default: {config.POS_MIN_ROE_PCT})",
    )
    pos_group.add_argument(
        "--min-roce",
        type=float,
        default=None,
        help=f"Min Return on Capital Employed %% (default: {config.POS_MIN_ROCE_PCT})",
    )
    pos_group.add_argument(
        "--min-net-margin",
        type=float,
        default=None,
        help=f"Min net profit margin %% (default: {config.POS_MIN_NET_MARGIN_PCT})",
    )
    pos_group.add_argument(
        "--min-operating-margin",
        type=float,
        default=None,
        help=f"Min operating margin %% (default: {config.POS_MIN_OPERATING_MARGIN_PCT})",
    )
    pos_group.add_argument(
        "--min-current-ratio",
        type=float,
        default=None,
        help=f"Min current ratio (default: {config.POS_MIN_CURRENT_RATIO})",
    )
    pos_group.add_argument(
        "--min-interest-coverage",
        type=float,
        default=None,
        help=f"Min interest coverage EBIT/Interest (default: {config.POS_MIN_INTEREST_COVERAGE})",
    )
    pos_group.add_argument(
        "--allow-negative-fcf",
        action="store_true",
        help="Allow stocks with negative free cash flow (default: require positive FCF)",
    )

    yf_group = parser.add_argument_group("yfinance Reliability (rate limiting)")
    yf_group.add_argument(
        "--yf-max-retries",
        type=int,
        default=None,
        help=f"Retries per Yahoo request after the first try (default: {config.YF_MAX_RETRIES})",
    )
    yf_group.add_argument(
        "--yf-throttle",
        type=float,
        default=None,
        metavar="SEC",
        help=(
            f"Minimum seconds between Yahoo requests, enforced across all threads "
            f"(default: {config.YF_MIN_REQUEST_INTERVAL}). Raise to ~0.2 if you keep "
            f"hitting HTTP 429; 0 disables throttling."
        ),
    )
    yf_group.add_argument(
        "--no-adaptive-throttle",
        action="store_true",
        help=(
            "Disable adaptive backpressure. By default the request interval widens "
            "automatically when Yahoo throttles and eases back on success, so a long "
            "scan self-tunes instead of needing a manual re-run."
        ),
    )
    yf_group.add_argument(
        "--yf-cookie-t",
        type=str,
        default=None,
        help=(
            "Yahoo 'T' cookie from a logged-in finance.yahoo.com session. "
            "Affects subscription entitlement only - Yahoo rate-limits by IP, "
            "so this does NOT raise your quota. Prefer the YF_COOKIE_T env var."
        ),
    )
    yf_group.add_argument(
        "--yf-cookie-y",
        type=str,
        default=None,
        help="Yahoo 'Y' cookie (see --yf-cookie-t). Prefer the YF_COOKIE_Y env var.",
    )

    tolerance_group = parser.add_argument_group("Filter Tolerance")
    tolerance_group.add_argument(
        "--filter-tolerance",
        type=float,
        default=None,
        metavar="PCT",
        help=(
            f"Percentage-point tolerance applied to ALL filter boundaries — "
            f"a stock missing any threshold by <= this value is still included "
            f"(default: {config.FILTER_TOLERANCE_PCT}pp). "
            f"E.g. 0.5 widens CAGR >=7%% to >=6.5%%, pullback 10-30%% to 9.5-30.5%%."
        ),
    )

    pullback_group = parser.add_argument_group("52-Week High Pullback Filter")
    pullback_group.add_argument(
        "--no-pullback-filter",
        action="store_true",
        help="Disable the 52-week high pullback filter",
    )
    pullback_group.add_argument(
        "--min-pullback",
        type=float,
        default=None,
        help=f"Minimum %% below 52-week high (default: {config.MIN_PULLBACK_PCT})",
    )
    pullback_group.add_argument(
        "--max-pullback",
        type=float,
        default=None,
        help=f"Maximum %% below 52-week high (default: {config.MAX_PULLBACK_PCT})",
    )

    beta_group = parser.add_argument_group("Beta Filter")
    beta_group.add_argument(
        "--no-beta-filter",
        action="store_true",
        help="Disable the beta filter (include all beta stocks)",
    )
    beta_group.add_argument(
        "--min-beta",
        type=float,
        default=None,
        help=f"Minimum beta (default: {config.MIN_BETA})",
    )
    beta_group.add_argument(
        "--max-beta",
        type=float,
        default=None,
        help=f"Maximum beta cap (default: {config.MAX_BETA})",
    )

    ranking_group = parser.add_argument_group("Ranking Weights")
    ranking_group.add_argument(
        "--growth-weight",
        type=float,
        default=None,
        help=f"Weight for growth score 0-1 (default: {config.GROWTH_WEIGHT})",
    )
    ranking_group.add_argument(
        "--pe-weight",
        type=float,
        default=None,
        help=f"Weight for PE discount score 0-1 (default: {config.PE_WEIGHT})",
    )

    return parser.parse_args()


def main():
    args = parse_args()
    logger = setup_logging()

    is_positional = args.mode == "positional"
    logger.info("=" * 70)
    if is_positional:
        logger.info("  POSITIONAL TRADE SCREENER")
        logger.info("  Finding quality businesses trading at a discount to their highs")
    else:
        logger.info("  SWING TRADE SCREENER")
        logger.info("  Finding undervalued stocks with golden cross momentum")
    logger.info("=" * 70)

    # --- Apply CLI overrides to config ---
    if args.universe is not None:
        config.STOCK_UNIVERSE = args.universe
    if args.universe_size is not None:
        config.UNIVERSE_SIZE = args.universe_size

    if args.short_dma:
        config.SHORT_DMA_PERIOD = args.short_dma
    if args.long_dma:
        config.LONG_DMA_PERIOD = args.long_dma

    if args.max_de is not None:
        config.MAX_DEBT_TO_EQUITY = args.max_de
    if args.max_pledged is not None:
        config.MAX_PLEDGED_PCT = args.max_pledged
    if args.max_pe is not None:
        config.MAX_PE_ABSOLUTE = args.max_pe
    if args.min_compound_growth is not None:
        config.MIN_COMPOUND_GROWTH_PCT = args.min_compound_growth
    if args.filter_tolerance is not None:
        config.FILTER_TOLERANCE_PCT = args.filter_tolerance

    # --- yfinance reliability ---
    if args.yf_max_retries is not None:
        config.YF_MAX_RETRIES = args.yf_max_retries
    if args.yf_throttle is not None:
        config.YF_MIN_REQUEST_INTERVAL = args.yf_throttle
        # Keep the adaptive ceiling above the requested floor.
        config.YF_MAX_REQUEST_INTERVAL = max(
            config.YF_MAX_REQUEST_INTERVAL, args.yf_throttle
        )
    if args.no_adaptive_throttle:
        config.YF_ADAPTIVE_THROTTLE = False
    # Env vars are preferred so cookies never land in shell history or the repo.
    config.YF_COOKIE_T = args.yf_cookie_t or os.environ.get("YF_COOKIE_T") or config.YF_COOKIE_T
    config.YF_COOKIE_Y = args.yf_cookie_y or os.environ.get("YF_COOKIE_Y") or config.YF_COOKIE_Y

    # --- Positional mode overrides ---
    if args.pos_min_pullback is not None:
        config.POS_MIN_PULLBACK_PCT = args.pos_min_pullback
    if args.pos_max_pullback is not None:
        config.POS_MAX_PULLBACK_PCT = args.pos_max_pullback
    if args.min_market_cap is not None:
        config.POS_MIN_MARKET_CAP_CR = args.min_market_cap
    if args.min_roe is not None:
        config.POS_MIN_ROE_PCT = args.min_roe
    if args.min_roce is not None:
        config.POS_MIN_ROCE_PCT = args.min_roce
    if args.min_net_margin is not None:
        config.POS_MIN_NET_MARGIN_PCT = args.min_net_margin
    if args.min_operating_margin is not None:
        config.POS_MIN_OPERATING_MARGIN_PCT = args.min_operating_margin
    if args.min_current_ratio is not None:
        config.POS_MIN_CURRENT_RATIO = args.min_current_ratio
    if args.min_interest_coverage is not None:
        config.POS_MIN_INTEREST_COVERAGE = args.min_interest_coverage
    if args.allow_negative_fcf:
        config.POS_REQUIRE_POSITIVE_FCF = False

    if args.no_pullback_filter:
        config.ENABLE_PULLBACK_FILTER = False
    elif args.min_pullback is not None or args.max_pullback is not None:
        # Auto-enable pullback filter if user specifies thresholds
        config.ENABLE_PULLBACK_FILTER = True
    if args.min_pullback is not None:
        config.MIN_PULLBACK_PCT = args.min_pullback
    if args.max_pullback is not None:
        config.MAX_PULLBACK_PCT = args.max_pullback

    if args.no_beta_filter:
        config.ENABLE_BETA_FILTER = False
    elif args.min_beta is not None or args.max_beta is not None:
        # Auto-enable beta filter if user specifies beta thresholds
        config.ENABLE_BETA_FILTER = True
    if args.min_beta is not None:
        config.MIN_BETA = args.min_beta
    if args.max_beta is not None:
        config.MAX_BETA = args.max_beta

    if args.growth_weight is not None:
        config.GROWTH_WEIGHT = args.growth_weight
    if args.pe_weight is not None:
        config.PE_WEIGHT = args.pe_weight

    # Log active configuration
    universe_desc = (
        f"NSE All (top {config.UNIVERSE_SIZE})"
        if getattr(config, "STOCK_UNIVERSE", "nifty500") == "nse_all"
        else "NIFTY 500"
    )
    logger.info(f"  Mode: {args.mode.upper()}")
    logger.info(f"  Universe: {universe_desc}")

    if args.mode == "positional":
        logger.info(f"  Config: 52W Pullback={config.POS_MIN_PULLBACK_PCT:.0f}-{config.POS_MAX_PULLBACK_PCT:.0f}%"
                    f" | MCap>=Rs{config.POS_MIN_MARKET_CAP_CR:,.0f}Cr"
                    f" | ROE>={config.POS_MIN_ROE_PCT:.0f}%"
                    f" | ROCE>={config.POS_MIN_ROCE_PCT:.0f}%")
        logger.info(f"          NetMargin>={config.POS_MIN_NET_MARGIN_PCT:.0f}%"
                    f" | OpMargin>={config.POS_MIN_OPERATING_MARGIN_PCT:.0f}%"
                    f" | CurRatio>={config.POS_MIN_CURRENT_RATIO}"
                    f" | IntCov>={config.POS_MIN_INTEREST_COVERAGE}x"
                    f" | PositiveFCF={config.POS_REQUIRE_POSITIVE_FCF}")
        logger.info(f"          D/E<={config.POS_MAX_DEBT_TO_EQUITY} (banks up to {config.POS_BANK_MAX_DEBT_TO_EQUITY}x)"
                    f" | Pledged<={config.POS_MAX_PLEDGED_PCT}%"
                    f" | CAGR>={config.POS_MIN_COMPOUND_GROWTH_PCT:.0f}%")
        logger.info(f"          Banks/NBFCs/Insurance: ROA>={config.POS_BANK_MIN_ROA_PCT}%"
                    f" | ROE>={config.POS_BANK_MIN_ROE_PCT}%"
                    f" | NetM>={config.POS_BANK_MIN_NET_MARGIN_PCT}%"
                    f" | FCF/OpM/CurR/IntCov gates skipped")
        logger.info(f"          Classic gates: no annual loss (all)"
                    f" | OCF/NI>={config.POS_MIN_OCF_TO_NI} earnings-quality (non-banks)"
                    f" | Piotroski F-Score reported (gate>={config.POS_MIN_PIOTROSKI_SCORE})")
        logger.info(f"          Bluechip (MCap>=Rs{config.BLUECHIP_MCAP_CR:,.0f}Cr) pullback floor: "
                    f"{config.POS_BLUECHIP_MIN_PULLBACK_PCT:.0f}% (vs {config.POS_MIN_PULLBACK_PCT:.0f}% standard)")
        logger.info(f"  Ranking: Quality {config.POS_QUALITY_WEIGHT*100:.0f}%"
                    f" + Growth {config.POS_GROWTH_WEIGHT*100:.0f}%"
                    f" + FinHealth {config.POS_FIN_HEALTH_WEIGHT*100:.0f}%")
    else:
        logger.info(f"  Config: 52W Pullback={config.MIN_PULLBACK_PCT:.0f}-{config.MAX_PULLBACK_PCT:.0f}%"
                    f" (enabled={config.ENABLE_PULLBACK_FILTER})"
                    f" | Beta>={config.MIN_BETA} (enabled={config.ENABLE_BETA_FILTER})"
                    f" | D/E<={config.MAX_DEBT_TO_EQUITY}"
                    f" | Pledged<={config.MAX_PLEDGED_PCT}%")
        logger.info(f"  Ranking: Growth {config.GROWTH_WEIGHT*100:.0f}% + PE {config.PE_WEIGHT*100:.0f}%")

    logger.info(f"  Filter Tolerance: +/-{config.FILTER_TOLERANCE_PCT}pp on all thresholds")
    logger.info(
        f"  yfinance: retries={config.YF_MAX_RETRIES}"
        f" | throttle={config.YF_MIN_REQUEST_INTERVAL}s"
        f" | adaptive={config.YF_ADAPTIVE_THROTTLE}"
        f" (max {config.YF_MAX_REQUEST_INTERVAL}s)"
    )

    # Optional Yahoo login (entitlement only - does NOT raise rate limits)
    if config.YF_COOKIE_T and config.YF_COOKIE_Y:
        logger.info("  Yahoo login cookies supplied - verifying...")
        apply_yahoo_login(config.YF_COOKIE_T, config.YF_COOKIE_Y)

    # Initialize Kotak Neo client
    kotak = KotakNeoClient(
        access_token=args.token or "",
        consumer_key=args.consumer_key,
        consumer_secret=args.consumer_secret,
    )

    if args.no_kotak:
        logger.info("Running in yfinance-only mode (--no-kotak flag set).")
    elif args.token:
        logger.info("Initializing Kotak Neo API client...")
        if kotak.initialize():
            logger.info("Kotak Neo API: Connected. Live quotes will be available.")
        else:
            logger.warning(
                "Kotak Neo API: Could not connect. Continuing with yfinance only."
            )
    else:
        logger.info(
            "No Kotak Neo token provided. Running with yfinance only.\n"
            "  To use live Kotak Neo data, pass: --token YOUR_ACCESS_TOKEN"
        )

    # Run the screener — filters are built inside __init__ from the config
    # values we just patched from the CLI.
    screener = SwingTradeScreener(kotak, mode=args.mode)
    candidates = screener.run(max_workers=args.threads)

    # Final status
    label = "positional" if is_positional else "swing"
    results_file = config.POS_RESULTS_FILE if is_positional else config.RESULTS_FILE
    if candidates:
        logger.info(f"\nDone! Found {len(candidates)} {label} trade candidates.")
        logger.info(f"Full results at: {config.OUTPUT_DIR}/{results_file}")
    else:
        logger.info("\nDone. No candidates matched all criteria today.")

    return candidates


if __name__ == "__main__":
    main()
