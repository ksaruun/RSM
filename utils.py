"""
Utility functions for the Swing Trade Screener.
"""
import logging
import os
import sys
from datetime import datetime

import config


def setup_logging(level=logging.INFO):
    """Configure logging for the screener."""
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    log_file = os.path.join(
        config.OUTPUT_DIR,
        f"screener_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
    )

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    return logging.getLogger("SwingScreener")


def nse_to_yfinance(symbol: str) -> str:
    """Convert NSE symbol to yfinance ticker format (append .NS)."""
    cleaned = symbol.strip().upper()
    if not cleaned.endswith(".NS") and not cleaned.endswith(".BO"):
        return f"{cleaned}.NS"
    return cleaned


def safe_float(value, default=None):
    """Safely convert a value to float."""
    try:
        if value is None:
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def format_large_number(num):
    """Format large numbers in Indian convention (Cr, L)."""
    if num is None:
        return "N/A"
    abs_num = abs(num)
    if abs_num >= 1e7:
        return f"{'−' if num < 0 else ''}₹{abs_num/1e7:.2f} Cr"
    elif abs_num >= 1e5:
        return f"{'−' if num < 0 else ''}₹{abs_num/1e5:.2f} L"
    else:
        return f"{'−' if num < 0 else ''}₹{abs_num:,.0f}"
