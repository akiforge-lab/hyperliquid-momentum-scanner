# -*- coding: utf-8 -*-
"""
Centralised path definitions for the momentum scanner.

All modules import from here.  To relocate any directory, change it once.
"""
from pathlib import Path

OUTPUT_DIR       = Path("output")
DAILY_CACHE_DIR  = Path("data/candles")
HOURLY_CACHE_DIR = Path("data/candles_1h")
DATA_DIR         = Path("data")
