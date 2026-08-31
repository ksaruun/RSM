"""
Fundamental Quality Filter — the core screen for positional (multi-month) trades.

Where swing mode asks "is this cheap versus its own history and turning up?",
positional mode asks "is this a genuinely high-quality business?" The answer is
built from four pillars:

  1. Returns on capital   — ROE and ROCE show whether management compounds
                            shareholder money at an attractive rate.
  2. Margins              — net and operating margins show pricing power and
                            operating efficiency.
  3. Balance-sheet health — current ratio (liquidity) and interest coverage
                            (ability to service debt) show survivability.
  4. Cash generation      — positive free cash flow proves the accounting
                            profit is real.

A size floor is also applied: micro-caps are excluded because multi-month
holds need liquidity and reliable disclosure.

Every threshold honours ``config.FILTER_TOLERANCE_PCT`` so a stock that misses
a bar by a hair is still admitted.

BANK / NBFC / INSURANCE SECTOR HANDLING
---------------------------------------
Financial-sector companies have fundamentally different economics.  Their
business *is* leverage, so several standard quality gates are inapplicable:

  • FCF             — deposit inflows look like operating outflows; skipped.
  • Operating margin — banks report NIM, not operating margin; skipped.
  • Current ratio   — deposits make this meaningless; skipped.
  • D/E             — banks operate at 8-10x leverage by design; the threshold
                      is raised to POS_BANK_MAX_DEBT_TO_EQUITY (default 10x).
  • ROCE → ROA      — capital-employed is ill-defined for a bank; ROA ≥ 1%
                      replaces ROCE as the capital-efficiency gate.
  • Interest coverage — banks earn interest, so the ratio flips; skipped.

Detection is automatic via keyword matching on ``industry`` / ``sector``
(see config.FINANCIAL_SECTOR_KEYWORDS).  The symbol-level ``sector`` and
``industry`` fields must be passed in via ``quality_data['sector']`` and
``quality_data['industry']``.
"""
import logging
import math
from typing import Optional

import config

logger = logging.getLogger("SwingScreener.FundamentalQuality")

# ---------------------------------------------------------------------------
# Sector detection helper
# ---------------------------------------------------------------------------

def is_financial_sector(sector: str = "", industry: str = "") -> bool:
    """
    Return True if the stock belongs to the bank/NBFC/insurance/AMC universe.

    Matches any keyword in ``config.FINANCIAL_SECTOR_KEYWORDS`` against the
    yfinance ``sector`` or ``industry`` strings (case-insensitive substring).
    """
    combined = f"{sector} {industry}".lower()
    return any(kw in combined for kw in config.FINANCIAL_SECTOR_KEYWORDS)


class FundamentalQualityFilter:
    """Filters for high-quality businesses suitable for positional holds."""

    def __init__(
        self,
        min_market_cap_cr: float = None,
        min_roe_pct: float = None,
        min_roce_pct: float = None,
        min_net_margin_pct: float = None,
        min_operating_margin_pct: float = None,
        min_current_ratio: float = None,
        min_interest_coverage: float = None,
        require_positive_fcf: bool = None,
    ):
        self.min_market_cap_cr        = min_market_cap_cr        if min_market_cap_cr        is not None else config.POS_MIN_MARKET_CAP_CR
        self.min_roe_pct              = min_roe_pct              if min_roe_pct              is not None else config.POS_MIN_ROE_PCT
        self.min_roce_pct             = min_roce_pct             if min_roce_pct             is not None else config.POS_MIN_ROCE_PCT
        self.min_net_margin_pct       = min_net_margin_pct       if min_net_margin_pct       is not None else config.POS_MIN_NET_MARGIN_PCT
        self.min_operating_margin_pct = min_operating_margin_pct if min_operating_margin_pct is not None else config.POS_MIN_OPERATING_MARGIN_PCT
        self.min_current_ratio        = min_current_ratio        if min_current_ratio        is not None else config.POS_MIN_CURRENT_RATIO
        self.min_interest_coverage    = min_interest_coverage    if min_interest_coverage    is not None else config.POS_MIN_INTEREST_COVERAGE
        self.require_positive_fcf     = require_positive_fcf     if require_positive_fcf     is not None else config.POS_REQUIRE_POSITIVE_FCF

    def apply(self, symbol: str, quality_data: dict) -> Optional[dict]:
        """
        Check a stock against every quality bar.

        Args:
            symbol: Stock symbol
            quality_data: Dict from DataFetcher.get_fundamental_quality_data().
                          Must include optional keys ``sector`` and ``industry``
                          so the filter can auto-detect financial-sector stocks.

        Returns:
            Dict of quality metrics if the stock passes, else None.
        """
        if not quality_data:
            return None

        tol = config.FILTER_TOLERANCE_PCT  # percentage points

        def _valid(v):
            return v is not None and not math.isnan(v)

        # --- Detect financial sector ---
        sector   = quality_data.get("sector", "") or ""
        industry = quality_data.get("industry", "") or ""
        is_fin   = is_financial_sector(sector, industry)

        if is_fin:
            logger.debug(
                f"{symbol}: Financial sector detected "
                f"(sector='{sector}', industry='{industry}') — "
                "applying bank-adjusted quality profile."
            )

        market_cap_cr     = quality_data.get("market_cap_cr")
        roe               = quality_data.get("roe_pct")
        roa               = quality_data.get("roa_pct")
        roce              = quality_data.get("roce_pct")
        net_margin        = quality_data.get("net_margin_pct")
        operating_margin  = quality_data.get("operating_margin_pct")
        current_ratio     = quality_data.get("current_ratio")
        interest_coverage = quality_data.get("interest_coverage")
        free_cashflow     = quality_data.get("free_cashflow")

        # ── 1. Size floor ────────────────────────────────────────────────
        # Required: a missing market cap means we cannot verify liquidity.
        if not _valid(market_cap_cr):
            logger.debug(f"{symbol}: Market cap unavailable")
            return None
        mcap_floor = self.min_market_cap_cr * (1 - tol / 100)
        if market_cap_cr < mcap_floor:
            logger.debug(
                f"{symbol}: Market cap ₹{market_cap_cr:,.0f}Cr < floor ₹{mcap_floor:,.0f}Cr"
            )
            return None

        # ── 2. Return on Equity ──────────────────────────────────────────
        if not _valid(roe):
            logger.debug(f"{symbol}: ROE unavailable")
            return None
        roe_floor = (config.POS_BANK_MIN_ROE_PCT if is_fin else self.min_roe_pct) - tol
        if roe < roe_floor:
            logger.debug(f"{symbol}: ROE {roe:.1f}% < floor {roe_floor:.1f}%")
            return None

        # ── 3. Capital efficiency ────────────────────────────────────────
        if is_fin:
            # Banks: use ROA >= POS_BANK_MIN_ROA_PCT instead of ROCE
            roa_floor = config.POS_BANK_MIN_ROA_PCT - tol
            if _valid(roa):
                if roa < roa_floor:
                    logger.debug(
                        f"{symbol}: ROA {roa:.2f}% < floor {roa_floor:.2f}% "
                        "(financial sector — using ROA instead of ROCE)"
                    )
                    return None
            else:
                logger.debug(
                    f"{symbol}: ROA unavailable for financial sector stock — "
                    "skipping capital-efficiency gate"
                )
        else:
            # Non-financial: use ROCE (falls back to ROE when ROCE unavailable)
            if _valid(roce):
                if roce < self.min_roce_pct - tol:
                    logger.debug(f"{symbol}: ROCE {roce:.1f}% < {self.min_roce_pct - tol:.1f}%")
                    return None
            else:
                logger.debug(f"{symbol}: ROCE unavailable — falling back to ROE")

        # ── 4. Net profit margin ─────────────────────────────────────────
        if not _valid(net_margin):
            logger.debug(f"{symbol}: Net margin unavailable")
            return None
        nm_floor = (config.POS_BANK_MIN_NET_MARGIN_PCT if is_fin else self.min_net_margin_pct) - tol
        if net_margin < nm_floor:
            logger.debug(
                f"{symbol}: Net margin {net_margin:.1f}% < floor {nm_floor:.1f}%"
            )
            return None

        # ── 5. Operating margin ──────────────────────────────────────────
        # Skipped entirely for financial-sector stocks — banks report NIM,
        # not operating margin in the manufacturing sense.
        if not is_fin and _valid(operating_margin):
            if operating_margin < self.min_operating_margin_pct - tol:
                logger.debug(
                    f"{symbol}: Operating margin {operating_margin:.1f}% < "
                    f"{self.min_operating_margin_pct - tol:.1f}%"
                )
                return None

        # ── 6. Current ratio (liquidity) ─────────────────────────────────
        # Skipped for financial-sector stocks (deposits make it meaningless).
        # For non-financial, a missing value is also not a rejection.
        if not is_fin and _valid(current_ratio):
            cr_floor = self.min_current_ratio * (1 - tol / 100)
            if current_ratio < cr_floor:
                logger.debug(
                    f"{symbol}: Current ratio {current_ratio:.2f} < floor {cr_floor:.2f}"
                )
                return None

        # ── 7. Interest coverage (debt serviceability) ───────────────────
        # Skipped for financial-sector stocks — they *earn* interest, so the
        # ratio is conceptually inverted and not meaningful.
        if not is_fin and _valid(interest_coverage):
            ic_floor = self.min_interest_coverage * (1 - tol / 100)
            if interest_coverage < ic_floor:
                logger.debug(
                    f"{symbol}: Interest coverage {interest_coverage:.1f}x < floor {ic_floor:.1f}x"
                )
                return None

        # ── 8. Free cash flow ────────────────────────────────────────────
        # Skipped for financial-sector stocks — deposit inflows register as
        # operating outflows, so FCF is always misleadingly negative for banks.
        if not is_fin and self.require_positive_fcf and _valid(free_cashflow):
            if free_cashflow <= 0:
                logger.debug(f"{symbol}: Negative free cash flow ({free_cashflow:,.0f})")
                return None

        # ── 9. Earnings consistency (Graham) — HARD GATE, all incl. banks ─
        # No annual net loss across the available reporting years. A company
        # that has posted a loss is not the "steady compounder" positional
        # trades depend on. When history is unavailable we do not reject.
        has_annual_loss = quality_data.get("has_annual_loss")
        annual_periods  = quality_data.get("annual_periods", 0)
        if config.POS_REQUIRE_EARNINGS_CONSISTENCY and has_annual_loss:
            logger.debug(
                f"{symbol}: Failed earnings-consistency — annual net loss in "
                f"{annual_periods} yrs of history"
            )
            return None

        # ── 10. Earnings quality / accruals (Piotroski) — non-banks only ──
        # Operating cash flow must reasonably back reported profit. Skipped for
        # financial-sector stocks (OCF is not a meaningful concept for lenders).
        ocf_to_ni = quality_data.get("ocf_to_ni")
        if not is_fin and _valid(ocf_to_ni):
            ocf_floor = config.POS_MIN_OCF_TO_NI * (1 - tol / 100)
            if ocf_to_ni < ocf_floor:
                logger.debug(
                    f"{symbol}: Failed earnings-quality — OCF/NI {ocf_to_ni:.2f} "
                    f"< floor {ocf_floor:.2f} (profit not cash-backed)"
                )
                return None

        # ── 11. Piotroski F-Score — report-only unless a floor is configured ─
        piotroski_score = quality_data.get("piotroski_score")
        piotroski_max   = quality_data.get("piotroski_max")
        if (config.POS_MIN_PIOTROSKI_SCORE > 0 and piotroski_score is not None):
            if piotroski_score < config.POS_MIN_PIOTROSKI_SCORE:
                logger.debug(
                    f"{symbol}: Piotroski F-Score {piotroski_score}/{piotroski_max} "
                    f"< floor {config.POS_MIN_PIOTROSKI_SCORE}"
                )
                return None

        result = {
            "symbol": symbol,
            "is_financial_sector": is_fin,
            "market_cap_cr": round(market_cap_cr, 1),
            "roe_pct": round(roe, 2),
            "roce_pct": round(roce, 2) if _valid(roce) else None,
            "roa_pct": round(roa, 2) if _valid(roa) else None,
            "net_margin_pct": round(net_margin, 2),
            "operating_margin_pct": round(operating_margin, 2) if _valid(operating_margin) else None,
            "gross_margin_pct": round(quality_data["gross_margin_pct"], 2) if _valid(quality_data.get("gross_margin_pct")) else None,
            "current_ratio": round(current_ratio, 2) if _valid(current_ratio) else None,
            "interest_coverage": round(interest_coverage, 2) if _valid(interest_coverage) else None,
            "free_cashflow": free_cashflow,
            "price_to_book": round(quality_data["price_to_book"], 2) if _valid(quality_data.get("price_to_book")) else None,
            "peg_ratio": round(quality_data["peg_ratio"], 2) if _valid(quality_data.get("peg_ratio")) else None,
            # --- Classic quality signals (reported for ranking / review) ---
            "ocf_to_ni": round(ocf_to_ni, 2) if _valid(ocf_to_ni) else None,
            "has_annual_loss": has_annual_loss,
            "annual_periods": annual_periods,
            "piotroski_score": piotroski_score,
            "piotroski_max": piotroski_max,
            "debt_to_ebitda": round(quality_data["debt_to_ebitda"], 2) if _valid(quality_data.get("debt_to_ebitda")) else None,
            "pe_x_pb": round(quality_data["pe_x_pb"], 2) if _valid(quality_data.get("pe_x_pb")) else None,
            "graham_ok": quality_data.get("graham_ok"),
            "dividend_yield_pct": round(quality_data["dividend_yield_pct"], 2) if _valid(quality_data.get("dividend_yield_pct")) else None,
            "payout_ratio_pct": round(quality_data["payout_ratio_pct"], 2) if _valid(quality_data.get("payout_ratio_pct")) else None,
        }

        profile = "BANK" if is_fin else "STANDARD"
        logger.debug(
            f"{symbol}: QUALITY PASS [{profile}] | MCap=₹{market_cap_cr:,.0f}Cr "
            f"ROE={roe:.1f}% "
            + (f"ROA={roa:.2f}%" if is_fin and _valid(roa) else f"ROCE={roce if _valid(roce) else 0:.1f}%")
            + f" NetM={net_margin:.1f}%"
        )

        return result
