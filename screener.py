"""
Screener: Orchestrates all filters and produces the final candidate list.

Two modes are supported.

SWING mode (default) — short-horizon, technical + value:
  a. Golden Cross Filter (50 DMA crossed above 200 DMA recently or sustained)
  b. PE Valuation Filter (Current PE < 3yr Median PE)
  c. Quarterly Growth Filter (4Q compound Sales & Profit CAGR)
  d. Debt Quality Filter (D/E < 0.5, Pledged < 5%)
  e. Price Momentum Filter (52W pullback 10-30%, Beta)
  Ranked by: 60% Growth + 40% PE Discount

POSITIONAL mode (--mode positional) — multi-month, fundamentals-first:
  a. Price Momentum Filter (52W pullback 15-40%, no beta constraint)
  b. Fundamental Quality Filter (market cap floor, ROE, ROCE, net &
     operating margins, current ratio, interest coverage, positive FCF)
  c. Debt Quality Filter (D/E, Pledged)
  d. Quarterly Growth Filter (compound Sales & Profit CAGR)
  No golden cross and no PE-vs-median check — see config.py for rationale.
  Ranked by: 40% Quality + 35% Growth + 25% Financial Health

Both modes share universe loading, threading, enrichment and output.
"""
import glob
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
from tqdm import tqdm

import config
from data_fetcher import DataFetcher
from filters import (
    GoldenCrossFilter,
    PEValuationFilter,
    QuarterlyGrowthFilter,
    DebtQualityFilter,
    PriceMomentumFilter,
    FundamentalQualityFilter,
)
from kotak_client import KotakNeoClient
from stock_universe import get_stock_universe
from utils import format_large_number

logger = logging.getLogger("SwingScreener.Screener")


class SwingTradeScreener:
    """
    Main screener that orchestrates the entire agentic screening flow.

    Args:
        kotak_client: Authenticated (or stub) Kotak Neo client.
        mode: "swing" (default) or "positional". See module docstring.
    """

    def __init__(self, kotak_client: KotakNeoClient, mode: str = "swing"):
        self.kotak = kotak_client
        self.mode = (mode or "swing").lower().strip()
        self.data_fetcher = DataFetcher()

        if self.mode == "positional":
            # Fundamentals-first pipeline: no golden cross, no PE-vs-median.
            self.momentum_filter = PriceMomentumFilter(
                enable_pullback=True,
                min_pullback_pct=config.POS_MIN_PULLBACK_PCT,
                max_pullback_pct=config.POS_MAX_PULLBACK_PCT,
                enable_beta=False,
                bluechip_min_pullback_pct=config.POS_BLUECHIP_MIN_PULLBACK_PCT,
            )
            self.quality_filter = FundamentalQualityFilter()
            self.debt_filter = DebtQualityFilter(
                max_de=config.POS_MAX_DEBT_TO_EQUITY,
                max_pledged=config.POS_MAX_PLEDGED_PCT,
            )
            self.growth_filter = QuarterlyGrowthFilter(
                min_compound_growth_pct=config.POS_MIN_COMPOUND_GROWTH_PCT
            )
            self.golden_cross = None
            self.pe_filter = None
        else:
            self.golden_cross    = GoldenCrossFilter()
            self.pe_filter       = PEValuationFilter(max_pe_absolute=config.MAX_PE_ABSOLUTE)
            self.growth_filter   = QuarterlyGrowthFilter(
                min_compound_growth_pct=config.MIN_COMPOUND_GROWTH_PCT
            )
            self.debt_filter     = DebtQualityFilter()
            self.momentum_filter = PriceMomentumFilter()
            self.quality_filter  = None

        # Stats
        self.stats = {
            "total": 0,
            "passed_golden_cross": 0,
            "passed_pe": 0,
            "passed_growth": 0,
            "passed_debt": 0,
            "passed_momentum": 0,
            "passed_quality": 0,
            "errors": 0,
        }

    def screen_single_stock(self, stock: dict) -> dict | None:
        """Dispatch to the pipeline for the active mode."""
        if self.mode == "positional":
            return self._screen_positional(stock)
        return self._screen_swing(stock)

    def _screen_positional(self, stock: dict) -> dict | None:
        """
        Positional pipeline — fundamentals first, fail-fast ordering.

        Order is chosen so the cheapest, highest-rejection checks run first:
        the pullback test uses only the cached ``info`` dict, while the growth
        test needs full quarterly statements.
        """
        symbol = stock["symbol"]

        try:
            # --- Stage 1: 52-week pullback (must be >= 15% off the high) ---
            momentum_data = self.data_fetcher.get_price_momentum_data(symbol)
            momentum_result = self.momentum_filter.apply(symbol, momentum_data)
            if momentum_result is None:
                return None

            self.stats["passed_momentum"] += 1

            # --- Stage 2: Fundamental quality (the core of this mode) ---
            # Pass sector/industry from the universe so bank-detection works
            # even when yfinance's info is empty or rate-limited.
            sector   = stock.get("sector", "") or ""
            industry = stock.get("industry", "") or ""
            quality_data = self.data_fetcher.get_fundamental_quality_data(
                symbol, sector=sector, industry=industry
            )
            quality_result = self.quality_filter.apply(symbol, quality_data)
            if quality_result is None:
                return None

            self.stats["passed_quality"] += 1

            # --- Stage 3: Debt quality (D/E + promoter pledging) ---
            debt_data = self.data_fetcher.get_debt_quality_data(symbol)
            # Forward the bank-detection flag set by the quality filter.
            debt_result = self.debt_filter.apply(
                symbol, debt_data,
                sector=quality_data.get("sector", sector),
                industry=quality_data.get("industry", industry),
            )
            if debt_result is None:
                return None

            self.stats["passed_debt"] += 1

            # --- Stage 4: Compounding growth ---
            # Pass market cap so the filter can apply the bluechip relaxation
            # (fewer quarters accepted for MCap >= config.BLUECHIP_MCAP_CR).
            quarterly_fin = self.data_fetcher.get_quarterly_financials(symbol)
            growth_result = self.growth_filter.apply(
                symbol, quarterly_fin,
                market_cap_cr=quality_result.get("market_cap_cr"),
            )
            if growth_result is None:
                return None

            self.stats["passed_growth"] += 1

            current_price = momentum_data.get("current_price")

            combined = {
                "symbol": symbol,
                "company_name": stock.get("company_name", ""),
                "industry": stock.get("industry", ""),
                "sector": stock.get("sector", ""),
                "is_financial_sector": quality_result.get("is_financial_sector", False),
                "current_price": round(current_price, 2) if current_price else None,
                # Quality metrics
                "market_cap_cr": quality_result["market_cap_cr"],
                "roe_pct": quality_result["roe_pct"],
                "roce_pct": quality_result["roce_pct"],
                "roa_pct": quality_result["roa_pct"],
                "net_margin_pct": quality_result["net_margin_pct"],
                "operating_margin_pct": quality_result["operating_margin_pct"],
                "gross_margin_pct": quality_result["gross_margin_pct"],
                "current_ratio": quality_result["current_ratio"],
                "interest_coverage": quality_result["interest_coverage"],
                "free_cashflow": quality_result["free_cashflow"],
                "price_to_book": quality_result["price_to_book"],
                "peg_ratio": quality_result["peg_ratio"],
                # Classic quality signals (Graham / Piotroski)
                "piotroski_score": quality_result.get("piotroski_score"),
                "piotroski_max": quality_result.get("piotroski_max"),
                "ocf_to_ni": quality_result.get("ocf_to_ni"),
                "annual_periods": quality_result.get("annual_periods"),
                "has_annual_loss": quality_result.get("has_annual_loss"),
                "debt_to_ebitda": quality_result.get("debt_to_ebitda"),
                "pe_x_pb": quality_result.get("pe_x_pb"),
                "graham_ok": quality_result.get("graham_ok"),
                "dividend_yield_pct": quality_result.get("dividend_yield_pct"),
                "payout_ratio_pct": quality_result.get("payout_ratio_pct"),
                # Growth
                "revenue_cagr_pct": growth_result["revenue_cagr_pct"],
                "profit_cagr_pct": growth_result["profit_cagr_pct"],
                "avg_revenue_growth_pct": growth_result["avg_revenue_growth_pct"],
                "avg_profit_growth_pct": growth_result["avg_profit_growth_pct"],
                "latest_quarterly_revenue": growth_result["latest_quarterly_revenue"],
                "latest_quarterly_profit": growth_result["latest_quarterly_profit"],
                # Debt
                "debt_to_equity": debt_result["debt_to_equity"],
                "pledged_pct": debt_result["pledged_pct"],
                # Price position
                "week52_high": momentum_result["week52_high"],
                "week52_low": momentum_result["week52_low"],
                "pullback_from_52w_high_pct": momentum_result["pullback_from_52w_high_pct"],
                "beta": momentum_result["beta"],
            }

            logger.info(f"*** CANDIDATE FOUND: {symbol} - {stock.get('company_name', '')} ***")
            return combined

        except Exception as e:
            logger.debug(f"Error screening {symbol}: {e}")
            self.stats["errors"] += 1
            return None

    def _screen_swing(self, stock: dict) -> dict | None:
        """
        Run all swing-mode filters on a single stock. Returns the combined
        result if it passes all filters, or None.
        """
        symbol = stock["symbol"]

        try:
            # --- Stage 1: Golden Cross Filter ---
            price_data = self.data_fetcher.get_historical_prices(symbol)
            if price_data is None:
                return None

            golden_result = self.golden_cross.apply(symbol, price_data)
            if golden_result is None:
                return None

            self.stats["passed_golden_cross"] += 1

            # --- Stage 2: PE Valuation Filter ---
            pe_data = self.data_fetcher.get_pe_ratio_data(symbol)
            pe_result = self.pe_filter.apply(symbol, pe_data)
            if pe_result is None:
                return None

            self.stats["passed_pe"] += 1

            # --- Stage 3: Quarterly Growth Filter ---
            # Fetch market cap from info for bluechip relaxation.
            quarterly_fin = self.data_fetcher.get_quarterly_financials(symbol)
            _mcap_raw = self.data_fetcher.get_info(symbol).get("marketCap")
            _mcap_cr = float(_mcap_raw) / 1e7 if _mcap_raw else None
            growth_result = self.growth_filter.apply(
                symbol, quarterly_fin, market_cap_cr=_mcap_cr
            )
            if growth_result is None:
                return None

            self.stats["passed_growth"] += 1

            # --- Stage 4: Debt Quality Filter ---
            debt_data = self.data_fetcher.get_debt_quality_data(symbol)
            debt_result = self.debt_filter.apply(symbol, debt_data)
            if debt_result is None:
                return None

            self.stats["passed_debt"] += 1

            # --- Stage 5: Price Momentum Filter (52W pullback + Beta) ---
            momentum_data   = self.data_fetcher.get_price_momentum_data(symbol)
            momentum_result = self.momentum_filter.apply(symbol, momentum_data)
            if momentum_result is None:
                return None

            self.stats["passed_momentum"] += 1

            # --- Combine results ---
            combined = {
                "symbol": symbol,
                "company_name": stock.get("company_name", ""),
                "industry": stock.get("industry", ""),
                # Golden Cross data
                "current_price": golden_result["current_price"],
                "dma_50": golden_result["dma_50"],
                "dma_200": golden_result["dma_200"],
                "dma_spread_pct": golden_result["dma_spread_pct"],
                "crossover_date": golden_result["crossover_date"],
                "days_since_crossover": golden_result["days_since_crossover"],
                "is_fresh_crossover": golden_result["is_fresh_crossover"],
                "price_above_both_dma": golden_result["price_above_both_dma"],
                # PE data
                "current_pe": pe_result["current_pe"],
                "median_pe_3yr": pe_result["median_pe_3yr"],
                "pe_discount_pct": pe_result["pe_discount_pct"],
                # Growth data
                "avg_revenue_growth_pct": growth_result["avg_revenue_growth_pct"],
                "avg_profit_growth_pct": growth_result["avg_profit_growth_pct"],
                "revenue_cagr_pct": growth_result["revenue_cagr_pct"],
                "profit_cagr_pct": growth_result["profit_cagr_pct"],
                "revenue_growth_4q": growth_result["revenue_growth_4q"],
                "profit_growth_4q": growth_result["profit_growth_4q"],
                "latest_quarterly_revenue": growth_result["latest_quarterly_revenue"],
                "latest_quarterly_profit": growth_result["latest_quarterly_profit"],
                # Debt quality data
                "debt_to_equity": debt_result["debt_to_equity"],
                "pledged_pct": debt_result["pledged_pct"],
                # Price momentum data
                "week52_high": momentum_result["week52_high"],
                "week52_low": momentum_result["week52_low"],
                "pullback_from_52w_high_pct": momentum_result["pullback_from_52w_high_pct"],
                "beta": momentum_result["beta"],
            }

            logger.info(f"*** CANDIDATE FOUND: {symbol} - {stock.get('company_name', '')} ***")
            return combined

        except Exception as e:
            logger.debug(f"Error screening {symbol}: {e}")
            self.stats["errors"] += 1
            return None

    def run(self, max_workers: int = 4) -> list[dict]:
        """
        Run the full screening pipeline on the NIFTY 500 universe.

        Uses multithreading for parallel data fetching.
        Returns list of stocks passing all filters.
        """
        title = (
            "POSITIONAL TRADE SCREENER" if self.mode == "positional"
            else "SWING TRADE SCREENER"
        )
        logger.info("=" * 70)
        logger.info(f"  {title} - Starting Scan")
        logger.info("=" * 70)

        # Step 1: Load universe
        universe_mode = getattr(config, "STOCK_UNIVERSE", "nifty500").lower()
        universe_label = (
            f"NSE All Equities (top {config.UNIVERSE_SIZE})"
            if universe_mode == "nse_all"
            else "NIFTY 500"
        )
        logger.info(f"[Step 1/4] Loading stock universe: {universe_label}...")
        stocks = get_stock_universe()
        self.stats["total"] = len(stocks)
        logger.info(f"Loaded {len(stocks)} stocks.")

        # Step 2: Screen each stock
        logger.info("[Step 2/4] Screening stocks through filters...")
        if self.mode == "positional":
            logger.info(
                f"  Filters: Pullback {config.POS_MIN_PULLBACK_PCT:.0f}-{config.POS_MAX_PULLBACK_PCT:.0f}% | "
                f"MCap>=Rs{config.POS_MIN_MARKET_CAP_CR:,.0f}Cr | "
                f"ROE>={config.POS_MIN_ROE_PCT:.0f}% & ROCE>={config.POS_MIN_ROCE_PCT:.0f}% | "
                f"NetM>={config.POS_MIN_NET_MARGIN_PCT:.0f}% & OpM>={config.POS_MIN_OPERATING_MARGIN_PCT:.0f}%"
            )
            logger.info(
                f"           CurRatio>={config.POS_MIN_CURRENT_RATIO} | "
                f"IntCov>={config.POS_MIN_INTEREST_COVERAGE}x | "
                f"FCF>0:{config.POS_REQUIRE_POSITIVE_FCF} | "
                f"D/E<{config.POS_MAX_DEBT_TO_EQUITY} & Pledge<{config.POS_MAX_PLEDGED_PCT}% | "
                f"CAGR>={config.POS_MIN_COMPOUND_GROWTH_PCT:.0f}%"
            )
            logger.info("           NO golden cross | NO PE-vs-median check")
            logger.info(
                f"           Bluechip (MCap>=Rs{config.BLUECHIP_MCAP_CR:,.0f}Cr): "
                f"min quarters relaxed to {config.BLUECHIP_MIN_QUARTERS} (standard={config.MIN_QUARTERS})"
            )
        else:
            pullback_label = (
                f"Pullback {config.MIN_PULLBACK_PCT:.0f}-{config.MAX_PULLBACK_PCT:.0f}%"
                if config.ENABLE_PULLBACK_FILTER else "Pullback:OFF"
            )
            beta_label = (
                f"Beta>{config.MIN_BETA}"
                if config.ENABLE_BETA_FILTER else "Beta:OFF"
            )
            logger.info(
                f"  Filters: Golden Cross | PE < 3yr Median | 4Q Growth | "
                f"D/E<{config.MAX_DEBT_TO_EQUITY} & Pledge<{config.MAX_PLEDGED_PCT}% | "
                f"{pullback_label} | {beta_label}"
            )
        logger.info(f"  Processing with {max_workers} threads...")

        candidates = []

        # Use ThreadPoolExecutor for parallel processing
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_stock = {
                executor.submit(self.screen_single_stock, stock): stock
                for stock in stocks
            }

            progress = tqdm(
                as_completed(future_to_stock),
                total=len(stocks),
                desc="Screening",
                unit="stock",
            )

            for future in progress:
                stock = future_to_stock[future]
                try:
                    result = future.result(timeout=60)
                    if result:
                        candidates.append(result)
                        progress.set_postfix({"found": len(candidates)})
                except Exception as e:
                    logger.debug(f"Thread error for {stock['symbol']}: {e}")
                    self.stats["errors"] += 1

        # Step 3: Enrich with live Kotak Neo data
        logger.info(f"\n[Step 3/4] Enriching {len(candidates)} candidates with live data...")
        if self.kotak.is_authenticated:
            candidates = self.kotak.enrich_with_live_data(candidates)

        # Step 4: Rank, sort and output
        logger.info("[Step 4/4] Ranking, sorting and saving results...")
        if self.mode == "positional":
            candidates = self._rank_positional(candidates)
        else:
            candidates = self._rank_candidates(candidates)

        self._save_results(candidates)
        self._cleanup_old_files(keep_count=2)  # Keep only last 2 CSVs and logs
        self._print_summary(candidates)

        return candidates

    def _rank_candidates(self, candidates: list[dict]) -> list[dict]:
        """
        Rank candidates using a composite score:
          Composite Score = 60% × Growth Score + 40% × PE Discount Score

        Growth Score:
          Average of normalised revenue-growth and profit-growth ranks
          (capped at 200% to prevent outliers dominating).

        PE Discount Score:
          Normalised rank of pe_discount_pct (0–100 scale).

        Both sub-scores are normalised using min-max scaling across the
        candidate pool so every dimension sits on a 0–100 scale.
        """
        import math

        GROWTH_WEIGHT = config.GROWTH_WEIGHT
        PE_WEIGHT     = config.PE_WEIGHT
        GROWTH_CAP    = config.GROWTH_CAP

        if not candidates:
            return candidates

        def clamp(val, lo, hi):
            return max(lo, min(hi, val))

        def minmax_norm(values: list[float]) -> list[float]:
            """Scale a list of floats to [0, 100]."""
            lo, hi = min(values), max(values)
            if hi == lo:
                return [50.0] * len(values)
            return [((v - lo) / (hi - lo)) * 100 for v in values]

        # Extract raw metrics (use CAGR for ranking)
        rev_growths = [
            clamp(c.get("revenue_cagr_pct", 0) or 0, -100, GROWTH_CAP)
            for c in candidates
        ]
        prof_growths = [
            clamp(c.get("profit_cagr_pct", 0) or 0, -500, GROWTH_CAP)
            for c in candidates
        ]
        pe_discounts = [c.get("pe_discount_pct", 0) or 0 for c in candidates]

        # Normalise each dimension to 0–100
        rev_scores   = minmax_norm(rev_growths)
        prof_scores  = minmax_norm(prof_growths)
        pe_scores    = minmax_norm(pe_discounts)

        # Build composite score and attach to each candidate
        for i, c in enumerate(candidates):
            growth_score = (rev_scores[i] + prof_scores[i]) / 2.0
            pe_score     = pe_scores[i]
            composite    = GROWTH_WEIGHT * growth_score + PE_WEIGHT * pe_score

            c["growth_score"]    = round(growth_score, 2)
            c["pe_score"]        = round(pe_score, 2)
            c["composite_score"] = round(composite, 2)

        # Sort descending by composite score
        candidates.sort(key=lambda x: x["composite_score"], reverse=True)

        # Assign rank
        for rank, c in enumerate(candidates, start=1):
            c["rank"] = rank

        logger.info(
            f"Ranking applied: Growth weight=60%, PE weight=40%. "
            f"Top pick: {candidates[0]['symbol']} (score={candidates[0]['composite_score']:.1f})"
        )
        return candidates

    def _rank_positional(self, candidates: list[dict]) -> list[dict]:
        """
        Rank positional candidates on business quality rather than valuation:

          Composite = 40% × Quality + 35% × Growth + 25% × Financial Health

          Quality          — ROE, ROCE, net margin
          Growth           — revenue CAGR, profit CAGR (outliers capped)
          Financial Health — low D/E (inverted), current ratio, interest coverage

        Every sub-metric is min-max normalised to 0-100 across the candidate
        pool so no single dimension dominates purely because of its units.
        """
        if not candidates:
            return candidates

        QUALITY_W = config.POS_QUALITY_WEIGHT
        GROWTH_W  = config.POS_GROWTH_WEIGHT
        HEALTH_W  = config.POS_FIN_HEALTH_WEIGHT
        CAP       = config.GROWTH_CAP

        def clamp(val, lo, hi):
            return max(lo, min(hi, val))

        def minmax_norm(values: list[float]) -> list[float]:
            lo, hi = min(values), max(values)
            if hi == lo:
                return [50.0] * len(values)
            return [((v - lo) / (hi - lo)) * 100 for v in values]

        def col(key, default=0.0, lo=-1e9, hi=1e9):
            return [clamp(c.get(key) or default, lo, hi) for c in candidates]

        # --- Quality dimension ---
        roe_scores  = minmax_norm(col("roe_pct", 0.0, -100, 200))
        roce_scores = minmax_norm(col("roce_pct", 0.0, -100, 200))
        marg_scores = minmax_norm(col("net_margin_pct", 0.0, -100, 100))

        # --- Growth dimension ---
        rev_scores  = minmax_norm(col("revenue_cagr_pct", 0.0, -100, CAP))
        prof_scores = minmax_norm(col("profit_cagr_pct", 0.0, -500, CAP))

        # --- Financial health dimension ---
        # D/E is inverted: lower debt is better, so negate before normalising.
        de_inverted = [-(c.get("debt_to_equity") or 0.0) for c in candidates]
        de_scores   = minmax_norm(de_inverted)
        cr_scores   = minmax_norm(col("current_ratio", 1.0, 0, 10))
        # Interest coverage is capped at 50x — beyond that it is effectively
        # "no debt" and shouldn't skew the scale.
        ic_scores   = minmax_norm(col("interest_coverage", 0.0, 0, 50))

        for i, c in enumerate(candidates):
            quality_score = (roe_scores[i] + roce_scores[i] + marg_scores[i]) / 3.0
            growth_score  = (rev_scores[i] + prof_scores[i]) / 2.0
            health_score  = (de_scores[i] + cr_scores[i] + ic_scores[i]) / 3.0

            composite = (
                QUALITY_W * quality_score
                + GROWTH_W * growth_score
                + HEALTH_W * health_score
            )

            c["quality_score"]   = round(quality_score, 2)
            c["growth_score"]    = round(growth_score, 2)
            c["health_score"]    = round(health_score, 2)
            c["composite_score"] = round(composite, 2)

        candidates.sort(key=lambda x: x["composite_score"], reverse=True)
        for rank, c in enumerate(candidates, start=1):
            c["rank"] = rank

        logger.info(
            f"Ranking applied: Quality={QUALITY_W*100:.0f}% Growth={GROWTH_W*100:.0f}% "
            f"Health={HEALTH_W*100:.0f}%. "
            f"Top pick: {candidates[0]['symbol']} (score={candidates[0]['composite_score']:.1f})"
        )
        return candidates

    def _cleanup_old_files(self, keep_count: int = 2):
        """
        Keep only the last N CSV and log files in the output directory.
        Deletes older files to prevent clutter.
        """
        output_dir = config.OUTPUT_DIR
        if not os.path.exists(output_dir):
            return

        # Cleanup timestamped CSV files (keep last N)
        prefix = "positional_candidates" if self.mode == "positional" else "swing_candidates"
        csv_pattern = os.path.join(output_dir, f"{prefix}_*.csv")
        csv_files = sorted(glob.glob(csv_pattern), key=os.path.getmtime, reverse=True)

        for old_csv in csv_files[keep_count:]:
            try:
                os.remove(old_csv)
                logger.debug(f"Deleted old CSV: {os.path.basename(old_csv)}")
            except Exception as e:
                logger.debug(f"Failed to delete {old_csv}: {e}")

        # Cleanup log files (keep last N)
        log_pattern = os.path.join(output_dir, "*.log")
        log_files = sorted(glob.glob(log_pattern), key=os.path.getmtime, reverse=True)

        for old_log in log_files[keep_count:]:
            try:
                os.remove(old_log)
                logger.debug(f"Deleted old log: {os.path.basename(old_log)}")
            except Exception as e:
                logger.debug(f"Failed to delete {old_log}: {e}")

    def _save_results(self, candidates: list[dict]):
        """Save results to CSV with rank and score columns first."""
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)

        is_positional = self.mode == "positional"
        results_file = config.POS_RESULTS_FILE if is_positional else config.RESULTS_FILE
        ts_prefix    = "positional_candidates" if is_positional else "swing_candidates"
        output_path  = os.path.join(config.OUTPUT_DIR, results_file)

        if candidates:
            df = pd.DataFrame(candidates)
            # Reorder so rank/score cols appear first
            if is_positional:
                priority_cols = ["rank", "composite_score", "quality_score",
                                 "growth_score", "health_score"]
            else:
                priority_cols = ["rank", "composite_score", "growth_score", "pe_score"]
            priority_cols = [c for c in priority_cols if c in df.columns]
            other_cols = [c for c in df.columns if c not in priority_cols]
            df = df[priority_cols + other_cols]
            df.to_csv(output_path, index=False)
            logger.info(f"Results saved to: {output_path}")

            # Also save a timestamped copy
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            ts_path = os.path.join(config.OUTPUT_DIR, f"{ts_prefix}_{ts}.csv")
            df.to_csv(ts_path, index=False)
        else:
            logger.info("No candidates found. No file saved.")

    def _print_summary(self, candidates: list[dict]):
        """Print a summary of the screening results."""
        logger.info("\n" + "=" * 70)
        logger.info("  SCREENING RESULTS SUMMARY")
        logger.info("=" * 70)
        logger.info(f"  Total stocks scanned:        {self.stats['total']}")

        if self.mode == "positional":
            logger.info(f"  Passed 52W Pullback:         {self.stats['passed_momentum']}")
            logger.info(f"  Passed Fundamental Quality:  {self.stats['passed_quality']}")
            logger.info(f"  Passed Debt Quality:         {self.stats['passed_debt']}")
            logger.info(f"  Passed Compound Growth:      {self.stats['passed_growth']}")
        else:
            logger.info(f"  Passed Golden Cross:         {self.stats['passed_golden_cross']}")
            logger.info(f"  Passed PE Filter:            {self.stats['passed_pe']}")
            logger.info(f"  Passed Quarterly Growth:     {self.stats['passed_growth']}")
            logger.info(f"  Passed Debt Quality:         {self.stats['passed_debt']}")
            logger.info(f"  Passed Price Momentum:       {self.stats['passed_momentum']}")

        logger.info(f"  Errors/Skipped:              {self.stats['errors']}")
        logger.info(f"  FINAL CANDIDATES:            {len(candidates)}")

        # Surface throttling so a degraded run is obvious instead of silent.
        rs = getattr(self.data_fetcher, "retry_stats", None)
        if rs and any(rs.values()):
            logger.info("-" * 70)
            logger.info(
                f"  yfinance retries: {rs.get('retries', 0)} | "
                f"crumb resets: {rs.get('crumb_resets', 0)} | "
                f"gave up: {rs.get('gave_up', 0)}"
            )
            limiter = getattr(self.data_fetcher, "_limiter", None)
            if limiter is not None and getattr(limiter, "peak_interval", 0) > 0:
                logger.info(
                    f"  adaptive throttle: peaked at {limiter.peak_interval:.2f}s/request, "
                    f"ended at {limiter.interval:.2f}s"
                )
            if rs.get("gave_up", 0) > 0:
                logger.warning(
                    f"  {rs['gave_up']} fetches exhausted all retries - results may be "
                    f"incomplete. Try --yf-throttle 0.2 with fewer --threads, "
                    f"or re-run later."
                )
        logger.info("=" * 70)

        if not candidates:
            logger.info("\n  No stocks passed all filters.")
            logger.info("  Consider relaxing the criteria or widening the universe.")
            return

        if self.mode == "positional":
            self._print_positional_table(candidates)
            return

        # Print detailed table
        logger.info("\n  SWING TRADE CANDIDATES  [Rank = 60% Growth + 40% PE Discount]")
        logger.info("-" * 185)
        header = (
            f"  {'Rk':>2} {'Symbol':<14} {'Company':<20} {'Price':>9} {'52WHigh':>9} "
            f"{'Pb%':>5} {'Beta':>5} {'XD':>3} {'PE':>7} {'MedPE':>7} {'PEDsc%':>6} "
            f"{'D/E':>5} {'Pld%':>4} {'RevCAGR%':>8} {'PrfCAGR%':>8} "
            f"{'GrScr':>6} {'PEScr':>6} {'Score':>6}"
        )
        logger.info(header)
        logger.info("-" * 185)

        for c in candidates:
            pb  = c.get("pullback_from_52w_high_pct")
            bt  = c.get("beta")
            w52 = c.get("week52_high")
            line = (
                f"  {c['rank']:>2} {c['symbol']:<14} {c.get('company_name', '')[:19]:<20} "
                f"{c['current_price']:>9.2f} {w52 if w52 else 0:>9.2f} "
                f"{pb if pb else 0:>4.1f}% {bt if bt else 0:>5.2f} "
                f"{c.get('days_since_crossover', ''):>3} "
                f"{c['current_pe']:>7.2f} {c['median_pe_3yr']:>7.2f} "
                f"{c['pe_discount_pct']:>5.1f}% "
                f"{c['debt_to_equity']:>5.2f} {c.get('pledged_pct', 0):>3.1f}% "
                f"{c['revenue_cagr_pct']:>7.1f}% "
                f"{c['profit_cagr_pct']:>7.1f}% "
                f"{c['growth_score']:>6.1f} {c['pe_score']:>6.1f} {c['composite_score']:>6.1f}"
            )
            logger.info(line)

        logger.info("-" * 185)
        logger.info(f"  Pb% = % below 52W High | Beta = market sensitivity (>1 = more volatile)")
        logger.info(f"  RevCAGR% = Revenue CAGR (4Q) | PrfCAGR% = Profit CAGR (4Q)")
        logger.info(f"  Scoring: Growth Score = avg(Rev CAGR rank, Profit CAGR rank) | PE Score = PE discount rank")
        logger.info(f"  Weights: Growth {config.GROWTH_WEIGHT*100:.0f}% + PE Discount {config.PE_WEIGHT*100:.0f}% | All scores normalised 0-100")
        logger.info(f"\n  Results saved to: {config.OUTPUT_DIR}/{config.RESULTS_FILE}")

    def _print_positional_table(self, candidates: list[dict]):
        """Print the positional-mode results table (quality-focused columns)."""
        qw = config.POS_QUALITY_WEIGHT * 100
        gw = config.POS_GROWTH_WEIGHT * 100
        hw = config.POS_FIN_HEALTH_WEIGHT * 100

        logger.info(
            f"\n  POSITIONAL TRADE CANDIDATES  "
            f"[Rank = {qw:.0f}% Quality + {gw:.0f}% Growth + {hw:.0f}% Fin-Health]"
        )
        logger.info("-" * 205)
        header = (
            f"  {'Rk':>2} {'Symbol':<13} {'Company':<22} {'Price':>9} {'MCap(Cr)':>9} "
            f"{'Pb%':>5} {'ROE%':>6} {'ROCE%':>6} {'NetM%':>6} {'OpM%':>6} "
            f"{'D/E':>5} {'CurR':>5} {'IntCov':>7} {'P/B':>6} "
            f"{'FScr':>5} {'Div%':>5} "
            f"{'RevCAGR%':>8} {'PrfCAGR%':>8} {'Qual':>5} {'Grw':>5} {'Hlth':>5} {'Score':>6}"
        )
        logger.info(header)
        logger.info("-" * 205)

        for c in candidates:
            is_fin = c.get("is_financial_sector", False)
            # For financial stocks show ROA where ROCE would normally appear.
            roce   = c.get("roa_pct") if is_fin else c.get("roce_pct")
            opm    = c.get("operating_margin_pct")
            curr   = c.get("current_ratio")
            icov   = c.get("interest_coverage")
            pb     = c.get("price_to_book")
            mcap   = c.get("market_cap_cr") or 0
            fscr   = c.get("piotroski_score")
            fmax   = c.get("piotroski_max")
            div    = c.get("dividend_yield_pct")
            # Mark financial-sector stocks with [B] so they stand out.
            sym_label = f"{c['symbol']}[B]" if is_fin else c['symbol']

            line = (
                f"  {c['rank']:>2} {sym_label:<13} {c.get('company_name','')[:21]:<22} "
                f"{c.get('current_price') or 0:>9.2f} {mcap:>9,.0f} "
                f"{c.get('pullback_from_52w_high_pct') or 0:>4.1f}% "
                f"{c.get('roe_pct') or 0:>6.1f} "
                f"{(f'{roce:.1f}' if roce is not None else '-'):>6} "
                f"{c.get('net_margin_pct') or 0:>6.1f} "
                f"{(f'{opm:.1f}' if opm is not None else '-'):>6} "
                f"{c.get('debt_to_equity') or 0:>5.2f} "
                f"{(f'{curr:.2f}' if curr is not None else '-'):>5} "
                f"{(f'{icov:.1f}x' if icov is not None else '-'):>7} "
                f"{(f'{pb:.2f}' if pb is not None else '-'):>6} "
                f"{(f'{fscr}/{fmax}' if fscr is not None else '-'):>5} "
                f"{(f'{div:.1f}' if div is not None else '-'):>5} "
                f"{c.get('revenue_cagr_pct') or 0:>8.1f} "
                f"{c.get('profit_cagr_pct') or 0:>8.1f} "
                f"{c.get('quality_score') or 0:>5.1f} "
                f"{c.get('growth_score') or 0:>5.1f} "
                f"{c.get('health_score') or 0:>5.1f} "
                f"{c.get('composite_score') or 0:>6.1f}"
            )
            logger.info(line)

        logger.info("-" * 205)
        logger.info("  Pb% = % below 52W High | MCap in Rs crore | IntCov = EBIT / Interest Expense")
        logger.info("  FScr = Piotroski F-Score (passed/testable of 9) | Div% = dividend yield")
        logger.info("  [B] = Bank/NBFC/Insurance/AMC - ROCE col shows ROA; FCF/OpM/CurR/IntCov & earnings-quality gates skipped; D/E up to 10x")
        logger.info("  Hard gates added: no annual loss (all) + OCF>=NI earnings quality (non-banks)")
        logger.info("  Quality = avg(ROE, ROCE/ROA, Net Margin ranks) | Grw = avg(Rev CAGR, Profit CAGR ranks)")
        logger.info("  Hlth = avg(low D/E, Current Ratio, Interest Coverage ranks) | All scores 0-100")
        logger.info(f"\n  Results saved to: {config.OUTPUT_DIR}/{config.POS_RESULTS_FILE}")
