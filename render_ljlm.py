#!/usr/bin/env python3
"""
Render static LJLM sounding products from a processed sounding JSON.

Outputs:
  diagnostics/latest_skewt.png
  diagnostics/latest_hodograph.png
  diagnostics/latest_products.json
  diagnostics/YYYY/MM/YYYYMMDD_TERM_skewt.png
  diagnostics/YYYY/MM/YYYYMMDD_TERM_hodograph.png

Designed for the JSON produced by extract_ljlm.py.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np

import metpy.calc as mpcalc
from metpy.plots import Hodograph, SkewT
from metpy.units import units


# ---------------------------------------------------------------------
# VISUAL SETTINGS
# ---------------------------------------------------------------------

BG = "#ffffff"
TEXT = "#172033"
MUTED = "#6b7280"
GRID = "#d9dee7"
TEMP = "#c73b32"
DEW = "#16836b"
PARCEL = "#3867d6"
WIND = "#243244"
INV = "#f2c66d"

DPI = 170


# ---------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------

def valid_number(value):
    try:
        return value is not None and math.isfinite(float(value))
    except Exception:
        return False


def load_profile(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def clean_levels(profile):
    rows = []
    for item in profile.get("levels", []):
        p = item.get("pressure_hpa")
        z = item.get("height_m")
        t = item.get("temperature_c")
        td = item.get("dewpoint_c")
        ws = item.get("wind_speed_ms")
        wd = item.get("wind_direction_deg")

        if not all(valid_number(x) for x in (p, z, t)):
            continue

        rows.append({
            "p": float(p),
            "z": float(z),
            "t": float(t),
            "td": float(td) if valid_number(td) else np.nan,
            "ws": float(ws) if valid_number(ws) else np.nan,
            "wd": float(wd) if valid_number(wd) else np.nan,
        })

    # Sort from highest pressure / lowest altitude upward.
    rows.sort(key=lambda x: (-x["p"], x["z"]))

    # Remove obvious duplicate pressure-height rows.
    cleaned = []
    seen = set()
    for row in rows:
