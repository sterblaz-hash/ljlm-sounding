#!/usr/bin/env python3
"""
QC inspection for suspicious/extreme historical Ljubljana IGRA values.

Reads the archive already built by build_igra_archive.py:
    historical/igra_1996_2025_profiles.json.gz

Checks:
- top MUCAPE profiles
- top IVT profiles
- top q925 profiles (recomputed from 925-hPa dew point)
- detailed diagnostics for selected suspicious dates

No data are modified.
"""

from __future__ import annotations

import gzip
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
from metpy.calc import specific_humidity_from_dewpoint
from metpy.units import units


PROFILES_PATH = Path("historical/igra_1996_2025_profiles.json.gz")

CHECK_DATES = {
    "2018-12-22": "MUCAPE maximum from climatology QC",
    "2009-10-03": "q925 maximum from climatology QC",
    "2023-08-20": "IVT maximum from climatology QC",
}

TOP_N = 15


def finite(x):
    try:
        return x is not None and math.isfinite(float(x))
    except Exception:
        return False


def q_from_td_gkg(p_hpa, td_c):
    if not finite(p_hpa) or not finite(td_c):
        return None
    try:
        q = specific_humidity_from_dewpoint(
            float(p_hpa) * units.hPa,
            float(td_c) * units.degC,
        )
        return float(q.to("dimensionless").m) * 1000.0
    except Exception:
        return None


def standard_level(profile, pressure):
    return (
        profile
        .get("parameters", {})
        .get("standard_levels", {})
        .get(str(pressure), {})
    )


def q925(profile):
    sl = standard_level(profile, 925)
    td = sl.get("dewpoint_c")
    return q_from_td_gkg(925, td)


def profile_time(profile):
    return profile.get("time", {}).get("nominal_utc", "?")


def profile_date(profile):
    t = profile_time(profile)
    return t[:10] if isinstance(t, str) and len(t) >= 10 else "?"


def resolution(profile):
    return (
        profile
        .get("qc", {})
        .get("resolution", {})
        .get("class")
    )


def conv(profile, key):
    return (
        profile
        .get("parameters", {})
        .get("convective", {})
        .get(key)
    )


def ivt(profile, key):
    return (
        profile
        .get("parameters", {})
        .get("ivt", {})
        .get(key)
    )


def fmt(v, nd=2):
    if not finite(v):
        return "NA"
    return f"{float(v):.{nd}f}"


def print_standard_levels(profile):
    print("  Standard levels:")
    print("    p    z(m)     T      Td      q(g/kg)  wind(m/s)  dir")
    for p in (925, 850, 700, 500, 300):
        sl = standard_level(profile, p)
        q = q_from_td_gkg(p, sl.get("dewpoint_c"))
        print(
            f"    {p:<4} "
            f"{fmt(sl.get('height_m'),0):>7} "
            f"{fmt(sl.get('temperature_c'),1):>6} "
            f"{fmt(sl.get('dewpoint_c'),1):>7} "
            f"{fmt(q,2):>9} "
            f"{fmt(sl.get('wind_speed_ms'),1):>10} "
            f"{fmt(sl.get('wind_direction_deg'),0):>5}"
        )


def print_profile_detail(profile, reason):
    p = profile.get("parameters", {})
    c = p.get("convective", {})
    i = p.get("ivt", {})
    res = profile.get("qc", {}).get("resolution", {})

    print()
    print("=" * 86)
    print(profile_time(profile), " | ", reason)
    print("=" * 86)
    print("  Resolution:", res)
    print("  PWAT:", fmt(p.get("precipitable_water_mm")), "mm",
          "QC:", p.get("precipitable_water_qc"))
    print("  q925:", fmt(q925(profile)), "g/kg")
    print(
        "  CAPE:",
        "SB", fmt(c.get("sbcape_jkg"), 1),
        "ML", fmt(c.get("mlcape_jkg"), 1),
        "MU", fmt(c.get("mucape_jkg"), 1),
        "J/kg | QC:", c.get("qc_status")
    )
    print(
        "  CIN:",
        "SB", fmt(c.get("sbcin_jkg"), 1),
        "ML", fmt(c.get("mlcin_jkg"), 1),
        "MU", fmt(c.get("mucin_jkg"), 1),
        "J/kg"
    )
    print(
        "  IVT:",
        fmt(i.get("magnitude_kg_m_s"), 1), "kg m-1 s-1",
        "| bottom/top:", fmt(i.get("bottom_pressure_hpa"), 0),
        "/", fmt(i.get("top_pressure_hpa"), 0), "hPa",
        "| points:", i.get("integration_points"),
        "| status:", i.get("status")
    )
    print_standard_levels(profile)


def top_table(profiles, label, getter, qc_getter=None):
    rows = []

    for profile in profiles:
        value = getter(profile)
        if not finite(value):
            continue
        if qc_getter is not None and qc_getter(profile) != "ok":
            continue
        rows.append((float(value), profile))

    rows.sort(key=lambda x: x[0], reverse=True)

    print()
    print("=" * 86)
    print(f"TOP {min(TOP_N, len(rows))}: {label}")
    print("=" * 86)

    for rank, (value, profile) in enumerate(rows[:TOP_N], 1):
        sl925 = standard_level(profile, 925)
        print(
            f"{rank:>2}. {profile_time(profile):20} "
            f"{value:9.2f}  "
            f"res={str(resolution(profile)):11} "
            f"T925={fmt(sl925.get('temperature_c'),1):>5} "
            f"Td925={fmt(sl925.get('dewpoint_c'),1):>5} "
            f"q925={fmt(q925(profile),2):>6}"
        )

    return rows


def robust_outlier_context(rows, label):
    vals = np.asarray([v for v, _ in rows], dtype=float)
    if len(vals) < 20:
        return

    p95 = np.percentile(vals, 95)
    p99 = np.percentile(vals, 99)
    p999 = np.percentile(vals, 99.9)

    print(
        f"{label} distribution: "
        f"P95={p95:.2f}, P99={p99:.2f}, P99.9={p999:.2f}, max={vals.max():.2f}"
    )


def main():
    if not PROFILES_PATH.exists():
        raise SystemExit(
            f"Missing {PROFILES_PATH}. Run Build IGRA historical archive first."
        )

    with gzip.open(PROFILES_PATH, "rt", encoding="utf-8") as f:
        doc = json.load(f)

    profiles = doc.get("profiles", [])
    print("Profiles loaded:", len(profiles))

    mucape_rows = top_table(
        profiles,
        "MUCAPE (J/kg)",
        lambda p: conv(p, "mucape_jkg"),
        lambda p: (
            p.get("parameters", {})
             .get("convective", {})
             .get("qc_status")
        ),
    )

    ivt_rows = top_table(
        profiles,
        "IVT magnitude (kg m-1 s-1)",
        lambda p: ivt(p, "magnitude_kg_m_s"),
        lambda p: ivt(p, "status"),
    )

    q_rows = top_table(
        profiles,
        "q925 (g/kg, recomputed from Td925)",
        q925,
    )

    print()
    robust_outlier_context(mucape_rows, "MUCAPE")
    robust_outlier_context(ivt_rows, "IVT")
    robust_outlier_context(q_rows, "q925")

    for day, reason in CHECK_DATES.items():
        matches = [
            p for p in profiles
            if profile_date(p) == day
        ]

        if not matches:
            print()
            print(f"No profile found for {day} ({reason})")
            continue

        for profile in matches:
            print_profile_detail(profile, reason)

    print()
    print("=" * 86)
    print("QC interpretation guide")
    print("=" * 86)
    print(
        "- A huge MUCAPE value with very_coarse/insufficient resolution, "
        "large dew-point jumps or sparse low levels is suspicious."
    )
    print(
        "- q925 should be checked against Td925. An extreme q value is only "
        "credible if Td925 itself is credible and not produced across a huge interpolation gap."
    )
    print(
        "- Extreme IVT is more credible when status=ok, bottom pressure is near "
        "the surface, top pressure reaches <=400 hPa, and several integration points are used."
    )


if __name__ == "__main__":
    main()
