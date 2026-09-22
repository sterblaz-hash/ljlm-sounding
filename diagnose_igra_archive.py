#!/usr/bin/env python3
"""
Diagnose the historical IGRA archive for Ljubljana/Bežigrad (SIM00014015).

Purpose
-------
This is deliberately a diagnostic first pass. It downloads the public
IGRA station archive, parses the native fixed-width format, deduplicates
near-duplicate launches, and reports how vertical resolution and variable
availability change through time. It does NOT modify data/ or climatology/.

The output is intended to help choose resolution-aware historical inversion
criteria before building the full 1996-2025 profile archive.
"""

from __future__ import annotations

import csv
import io
import math
import statistics
import urllib.request
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

STATION = "SIM00014015"
URL = f"https://www.ncei.noaa.gov/pub/data/igra/data/data-por/{STATION}-data.txt.zip"
OUTDIR = Path("diagnostics")
YEAR_START = 1996
YEAR_END = 2025

MISSING = {-9999, -8888}


def as_int(s: str) -> Optional[int]:
    try:
        v = int(s)
    except (TypeError, ValueError):
        return None
    return None if v in MISSING else v


def pct(values, q):
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    x = (len(vals) - 1) * q
    lo = math.floor(x)
    hi = math.ceil(x)
    if lo == hi:
        return vals[lo]
    return vals[lo] * (hi - x) + vals[hi] * (x - lo)


def fmt(v, nd=1):
    return "NA" if v is None else f"{v:.{nd}f}"


@dataclass
class Level:
    pressure_hpa: Optional[float]
    height_m: Optional[float]
    temperature_c: Optional[float]
    dewpoint_c: Optional[float]
    wind_dir_deg: Optional[float]
    wind_speed_ms: Optional[float]


@dataclass
class Profile:
    year: int
    month: int
    day: int
    hour: int
    reltime: Optional[int]
    numlev: int
    levels: list[Level]

    @property
    def nominal_key(self):
        return self.year, self.month, self.day, self.hour

    @property
    def date_key(self):
        return self.year, self.month, self.day


def parse_header(line: str):
    # IGRA v2 header fields:
    # 1-11 ID; 13-16 YEAR; 18-19 MONTH; 21-22 DAY; 24-25 HOUR;
    # 27-30 RELTIME; 32-36 NUMLEV.
    return {
        "id": line[1:12].strip(),
        "year": as_int(line[13:17]),
        "month": as_int(line[18:20]),
        "day": as_int(line[21:23]),
        "hour": as_int(line[24:26]),
        "reltime": as_int(line[27:31]),
        "numlev": as_int(line[32:36]) or 0,
    }


def parse_level(line: str) -> Level:
    # IGRA v2 level fields. Values use scale factors:
    # pressure Pa -> hPa /100; geopotential height m;
    # temperature/dewpoint depression tenths C; wind speed tenths m/s.
    p = as_int(line[9:15])
    z = as_int(line[16:21])
    t = as_int(line[22:27])
    dpd = as_int(line[34:39])
    wd = as_int(line[40:45])
    ws = as_int(line[46:51])

    tc = t / 10.0 if t is not None else None
    tdc = None
    if tc is not None and dpd is not None:
        tdc = tc - dpd / 10.0

    return Level(
        pressure_hpa=p / 100.0 if p is not None else None,
        height_m=float(z) if z is not None else None,
        temperature_c=tc,
        dewpoint_c=tdc,
        wind_dir_deg=float(wd) if wd is not None else None,
        wind_speed_ms=ws / 10.0 if ws is not None else None,
    )


def download_text() -> str:
    print("Downloading:", URL)
    req = urllib.request.Request(URL, headers={"User-Agent": "ljlm-sounding/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        payload = r.read()
    print(f"Downloaded: {len(payload)/1024/1024:.1f} MB compressed")

    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
        if not names:
            raise RuntimeError("No .txt file found inside IGRA ZIP")
        with zf.open(names[0]) as f:
            return f.read().decode("ascii", errors="replace")


def parse_profiles(text: str) -> list[Profile]:
    lines = text.splitlines()
    profiles = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("#"):
            i += 1
            continue

        h = parse_header(line)
        n = h["numlev"]
        raw_levels = lines[i + 1:i + 1 + n]
        i += 1 + n

        if h["id"] != STATION or h["year"] is None:
            continue
        if not (YEAR_START <= h["year"] <= YEAR_END):
            continue

        levels = [parse_level(x) for x in raw_levels]
        profiles.append(Profile(
            year=h["year"], month=h["month"], day=h["day"], hour=h["hour"] or 0,
            reltime=h["reltime"], numlev=n, levels=levels
        ))
    return profiles


def quality_score(p: Profile):
    usable = sum(
        1 for x in p.levels
        if x.pressure_hpa is not None and x.height_m is not None
        and x.temperature_c is not None
    )
    dew = sum(1 for x in p.levels if x.dewpoint_c is not None)
    wind = sum(
        1 for x in p.levels
        if x.wind_dir_deg is not None and x.wind_speed_ms is not None
    )
    return usable, dew, wind, p.numlev


def deduplicate(profiles: list[Profile]) -> tuple[list[Profile], int]:
    # Prefer one representative profile per nominal launch timestamp.
    # If duplicates exist, retain the profile with the richest usable data.
    groups = defaultdict(list)
    for p in profiles:
        groups[p.nominal_key].append(p)

    out = []
    removed = 0
    for group in groups.values():
        group.sort(key=quality_score, reverse=True)
        out.append(group[0])
        removed += len(group) - 1

    out.sort(key=lambda p: p.nominal_key)
    return out, removed


def low_level_metrics(p: Profile):
    # For inversion design, examine valid T+z levels from the lowest valid
    # observation upward to 700 hPa. Do not interpolate new levels here.
    rows = [
        x for x in p.levels
        if x.pressure_hpa is not None
        and x.height_m is not None
        and x.temperature_c is not None
        and x.pressure_hpa >= 700.0
    ]
    rows.sort(key=lambda x: x.height_m)

    if len(rows) < 2:
        return None

    z0 = rows[0].height_m
    agl = [(x, x.height_m - z0) for x in rows if x.height_m >= z0]

    dz = []
    for (a, _), (b, _) in zip(agl[:-1], agl[1:]):
        d = b.height_m - a.height_m
        if 0 < d <= 2000:
            dz.append(d)

    def count_below(limit):
        return sum(1 for _, h in agl if 0 <= h <= limit)

    return {
        "n_700": len(rows),
        "n_0_500": count_below(500),
        "n_0_1000": count_below(1000),
        "dz_median": statistics.median(dz) if dz else None,
        "dz_p25": pct(dz, 0.25),
        "dz_p75": pct(dz, 0.75),
        "dz_min": min(dz) if dz else None,
    }


def availability(p: Profile):
    n = max(len(p.levels), 1)
    return {
        "t": sum(x.temperature_c is not None for x in p.levels) / n,
        "td": sum(x.dewpoint_c is not None for x in p.levels) / n,
        "wind": sum(
            x.wind_speed_ms is not None and x.wind_dir_deg is not None
            for x in p.levels
        ) / n,
    }


def period_label(year):
    if year <= 2000:
        return "1996-2000"
    if year <= 2005:
        return "2001-2005"
    if year <= 2010:
        return "2006-2010"
    if year <= 2015:
        return "2011-2015"
    if year <= 2020:
        return "2016-2020"
    return "2021-2025"


def summarize(profiles: list[Profile], removed: int):
    OUTDIR.mkdir(exist_ok=True)

    by_year = defaultdict(list)
    by_period = defaultdict(list)
    for p in profiles:
        by_year[p.year].append(p)
        by_period[period_label(p.year)].append(p)

    yearly_rows = []
    for year in range(YEAR_START, YEAR_END + 1):
        ps = by_year.get(year, [])
        mets = [low_level_metrics(p) for p in ps]
        mets = [m for m in mets if m]
        av = [availability(p) for p in ps]

        yearly_rows.append({
            "year": year,
            "profiles": len(ps),
            "median_levels": fmt(statistics.median([p.numlev for p in ps]) if ps else None),
            "median_lowlevel_dz_m": fmt(statistics.median([m["dz_median"] for m in mets if m["dz_median"] is not None]) if mets else None),
            "median_levels_0_500m": fmt(statistics.median([m["n_0_500"] for m in mets]) if mets else None),
            "median_levels_0_1000m": fmt(statistics.median([m["n_0_1000"] for m in mets]) if mets else None),
            "T_availability_pct": fmt(100 * statistics.mean([a["t"] for a in av]) if av else None),
            "Td_availability_pct": fmt(100 * statistics.mean([a["td"] for a in av]) if av else None),
            "wind_availability_pct": fmt(100 * statistics.mean([a["wind"] for a in av]) if av else None),
        })

    csv_path = OUTDIR / "igra_resolution_by_year.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=yearly_rows[0].keys())
        w.writeheader()
        w.writerows(yearly_rows)

    print("\n" + "=" * 72)
    print("IGRA LJUBLJANA DIAGNOSTIC")
    print("=" * 72)
    print(f"Station: {STATION}")
    print(f"Period: {YEAR_START}-{YEAR_END}")
    print(f"Profiles after nominal-time deduplication: {len(profiles)}")
    print(f"Duplicate profiles removed: {removed}")
    print(f"Yearly CSV: {csv_path}")

    hours = Counter(p.hour for p in profiles)
    print("\nNominal UTC hours:")
    print("  " + ", ".join(f"{h:02d}: {n}" for h, n in sorted(hours.items())))

    print("\nResolution / availability by period")
    print("-" * 72)
    for label in ("1996-2000", "2001-2005", "2006-2010",
                  "2011-2015", "2016-2020", "2021-2025"):
        ps = by_period.get(label, [])
        mets = [low_level_metrics(p) for p in ps]
        mets = [m for m in mets if m]
        av = [availability(p) for p in ps]

        dz = [m["dz_median"] for m in mets if m["dz_median"] is not None]
        n500 = [m["n_0_500"] for m in mets]
        n1000 = [m["n_0_1000"] for m in mets]

        print(
            f"{label}: profiles={len(ps):4d} | "
            f"median dz<700={fmt(statistics.median(dz) if dz else None)} m "
            f"(P25={fmt(pct(dz, .25))}, P75={fmt(pct(dz, .75))}) | "
            f"N 0-500m={fmt(statistics.median(n500) if n500 else None)} | "
            f"N 0-1000m={fmt(statistics.median(n1000) if n1000 else None)} | "
            f"T={fmt(100*statistics.mean(a['t'] for a in av) if av else None)}% "
            f"Td={fmt(100*statistics.mean(a['td'] for a in av) if av else None)}% "
            f"wind={fmt(100*statistics.mean(a['wind'] for a in av) if av else None)}%"
        )

    # Show a few profiles from each era so we can see the actual vertical grid.
    sample_path = OUTDIR / "igra_lowlevel_samples.txt"
    with sample_path.open("w", encoding="utf-8") as f:
        for label in ("1996-2000", "2001-2005", "2006-2010",
                      "2011-2015", "2016-2020", "2021-2025"):
            ps = by_period.get(label, [])
            if not ps:
                continue
            picks = [ps[0], ps[len(ps)//2], ps[-1]]
            f.write(f"\n===== {label} =====\n")
            for p in picks:
                f.write(
                    f"\n{p.year:04d}-{p.month:02d}-{p.day:02d} "
                    f"{p.hour:02d} UTC reltime={p.reltime} levels={p.numlev}\n"
                )
                valid = [
                    x for x in p.levels
                    if x.pressure_hpa is not None
                    and x.height_m is not None
                    and x.temperature_c is not None
                    and x.pressure_hpa >= 700
                ]
                valid.sort(key=lambda x: x.height_m)
                for x in valid[:40]:
                    f.write(
                        f"  p={x.pressure_hpa:7.1f} hPa "
                        f"z={x.height_m:7.0f} m "
                        f"T={x.temperature_c:6.1f} C "
                        f"Td={fmt(x.dewpoint_c):>6} C "
                        f"ws={fmt(x.wind_speed_ms):>6} m/s\n"
                    )

    print(f"Low-level samples: {sample_path}")
    print("\nNo historical archive files were modified.")
    print("Next decision: resolution-aware inversion criteria.")


def main():
    text = download_text()
    raw = parse_profiles(text)
    print(f"Parsed profiles {YEAR_START}-{YEAR_END}: {len(raw)}")
    profiles, removed = deduplicate(raw)
    summarize(profiles, removed)


if __name__ == "__main__":
    main()
