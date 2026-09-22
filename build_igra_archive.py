#!/usr/bin/env python3
"""
Build a historical Ljubljana (SIM00014015 / LJLM) radiosonde archive from NOAA IGRA v2
and, in the same run, build an expanded daily climatology.

Adds:
- observed-level theta / theta-e
- standard levels: 925, 850, 700, 500, 300 hPa
- freezing level
- lapse rates
- PWAT
- wind at standard levels
- bulk shear: 0-1, 0-3, 0-6 km
- IVT magnitude/direction/components
- Lifted Index
- SBCAPE/SBCIN, MLCAPE/MLCIN, MUCAPE/MUCIN
- per-parameter QC/status
- resolution-aware observed stable layers
- daily climatology using +/-15 calendar days, with P1/P10/P25/P50/P75/P90/P99
  and 10-day circular smoothing of percentile curves.

Important:
IGRA reported-level profiles are often vertically coarse. Fine inversion climatology is
therefore intentionally NOT treated as equivalent to the high-resolution DWD BUFR
inversion algorithm. CAPE/CIN, shear and IVT are only published when conservative
coverage/QC criteria are met.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import os
import re
import statistics
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from metpy.calc import (
    bulk_shear,
    dewpoint_from_relative_humidity,
    equivalent_potential_temperature,
    lifted_index,
    mixed_layer_cape_cin,
    most_unstable_cape_cin,
    parcel_profile,
    precipitable_water,
    specific_humidity_from_dewpoint,
    surface_based_cape_cin,
)
from metpy.units import units


STATION_ID = "SIM00014015"
URL = "https://www.ncei.noaa.gov/pub/data/igra/data/data-por/SIM00014015-data.txt.zip"
START_YEAR = 1996
END_YEAR = 2025

OUTDIR = Path("historical")
SUMMARY_PATH = OUTDIR / "igra_1996_2025_summary.json"
PROFILES_PATH = OUTDIR / "igra_1996_2025_profiles.json.gz"
REPORT_PATH = OUTDIR / "igra_build_report.json"
CLIM_PATH = Path("climatology") / "daily_climatology.json"

STANDARD_LEVELS = (925, 850, 700, 500, 300)
CLIM_PERCENTILES = (1, 10, 25, 50, 75, 90, 99)
WINDOW_DAYS = 15
SMOOTH_DAYS = 10

MISSING = {-9999, -8888}
EARTH_R = 6371000.0
G = 9.80665


def finite(x):
    try:
        return x is not None and math.isfinite(float(x))
    except Exception:
        return False


def clean_num(x, scale=1.0):
    try:
        v = int(x)
    except Exception:
        return None
    if v in MISSING:
        return None
    return v / scale


def percentile_rank(values, x):
    a = np.asarray([v for v in values if finite(v)], dtype=float)
    if not len(a) or not finite(x):
        return None
    return round(float(100.0 * np.mean(a <= float(x))), 1)


def pct(values, q):
    a = np.asarray([v for v in values if finite(v)], dtype=float)
    if not len(a):
        return None
    return round(float(np.percentile(a, q)), 3)


def circular_smooth(values, width=10):
    vals = np.asarray([np.nan if v is None else float(v) for v in values], dtype=float)
    n = len(vals)
    out = []
    # centered-ish 10-day window; circular over year
    left = (width - 1) // 2
    right = width - left - 1
    for i in range(n):
        inds = [(i + j) % n for j in range(-left, right + 1)]
        chunk = vals[inds]
        good = chunk[np.isfinite(chunk)]
        out.append(None if not len(good) else round(float(np.mean(good)), 3))
    return out


def doy365(month, day):
    # map leap day to Feb 28 bucket; dates after Feb 29 are normalized to non-leap DOY
    if month == 2 and day == 29:
        month, day = 2, 28
    return datetime(2001, month, day).timetuple().tm_yday


def circular_doy_distance(a, b):
    d = abs(a - b)
    return min(d, 365 - d)


def download_igra():
    print("Downloading:", URL)
    with urllib.request.urlopen(URL, timeout=120) as r:
        data = r.read()
    print(f"Downloaded: {len(data)/1024/1024:.1f} MB compressed")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if not names:
            raise RuntimeError("Empty IGRA zip")
        return z.read(names[0]).decode("ascii", errors="replace").splitlines()


def parse_header(line):
    # IGRA2 station-data header fixed-width format.
    # Positions follow NOAA IGRA v2 documentation.
    try:
        return {
            "id": line[1:12].strip(),
            "year": int(line[13:17]),
            "month": int(line[18:20]),
            "day": int(line[21:23]),
            "hour": int(line[24:26]),
            "reltime": clean_num(line[27:31]),
            "numlev": int(line[32:36]),
            "p_src": line[37:45].strip() or None,
            "np_src": line[46:54].strip() or None,
            "lat": clean_num(line[55:62], 10000.0),
            "lon": clean_num(line[63:71], 10000.0),
        }
    except Exception as e:
        raise ValueError(f"Bad header: {line[:80]!r}: {e}")


def parse_level(line):
    # IGRA2 level record:
    # LVLTYP1(0), LVLTYP2(1), ETIME(3:8), PRESS(9:15), PFLAG(15),
    # GPH(16:21), ZFLAG(21), TEMP(22:27), TFLAG(27),
    # RH(28:33), DPDP(34:39), WDIR(40:45), WSPD(46:51)
    p = clean_num(line[9:15], 100.0)       # Pa -> hPa
    z = clean_num(line[16:21], 1.0)
    t = clean_num(line[22:27], 10.0)
    rh = clean_num(line[28:33], 10.0)
    dpdp = clean_num(line[34:39], 10.0)
    wd = clean_num(line[40:45], 1.0)
    ws = clean_num(line[46:51], 10.0)

    td = None
    if finite(t) and finite(dpdp):
        td = float(t) - float(dpdp)
    elif finite(t) and finite(rh) and 0 < rh <= 100:
        try:
            td = float(
                dewpoint_from_relative_humidity(
                    float(t) * units.degC, (float(rh) / 100.0) * units.dimensionless
                ).to("degC").m
            )
        except Exception:
            td = None

    return {
        "pressure_hpa": p,
        "height_m": z,
        "temperature_c": t,
        "dewpoint_c": td,
        "relative_humidity_pct": rh,
        "wind_speed_ms": ws,
        "wind_direction_deg": wd,
    }


def parse_profiles(lines):
    profiles = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("#"):
            i += 1
            continue
        h = parse_header(line)
        n = h["numlev"]
        raw_levels = []
        for j in range(i + 1, min(i + 1 + n, len(lines))):
            if lines[j].startswith("#"):
                break
            try:
                raw_levels.append(parse_level(lines[j]))
            except Exception:
                pass
        i += 1 + n

        if h["id"] != STATION_ID or not (START_YEAR <= h["year"] <= END_YEAR):
            continue

        try:
            dt = datetime(h["year"], h["month"], h["day"], h["hour"], tzinfo=timezone.utc)
        except Exception:
            continue

        # Keep plausible pressure/height observations and sort bottom-up by pressure.
        levels = [
            x for x in raw_levels
            if finite(x["pressure_hpa"]) and 5 <= x["pressure_hpa"] <= 1100
        ]
        levels.sort(key=lambda x: x["pressure_hpa"], reverse=True)

        profiles.append({"header": h, "nominal_dt": dt, "levels": levels})

    # Conservative nominal-timestamp deduplication; prefer profile with more levels.
    best = {}
    duplicates = 0
    for p in profiles:
        key = p["nominal_dt"].isoformat()
        score = len(p["levels"])
        if key not in best or score > len(best[key]["levels"]):
            if key in best:
                duplicates += 1
            best[key] = p
        else:
            duplicates += 1
    return [best[k] for k in sorted(best)], duplicates


def add_theta(levels):
    for l in levels:
        l["potential_temperature_k"] = None
        l["equivalent_potential_temperature_k"] = None
        p, t, td = l["pressure_hpa"], l["temperature_c"], l["dewpoint_c"]
        if finite(p) and finite(t):
            try:
                # dry potential temperature
                tk = float(t) + 273.15
                l["potential_temperature_k"] = round(
                    tk * (1000.0 / float(p)) ** 0.2854, 2
                )
            except Exception:
                pass
        if finite(p) and finite(t) and finite(td):
            try:
                th_e = equivalent_potential_temperature(
                    float(p) * units.hPa,
                    float(t) * units.degC,
                    float(td) * units.degC,
                ).to("kelvin").m
                l["equivalent_potential_temperature_k"] = round(float(th_e), 2)
            except Exception:
                pass


def log_interp(levels, target_p, key, max_gap_hpa=250):
    pts = sorted(
        [(float(l["pressure_hpa"]), float(l[key])) for l in levels
         if finite(l.get("pressure_hpa")) and finite(l.get(key))],
        reverse=True,
    )
    for p, v in pts:
        if abs(p - target_p) < 0.05:
            return v
    for (p1, v1), (p2, v2) in zip(pts[:-1], pts[1:]):
        if p1 >= target_p >= p2 and (p1 - p2) <= max_gap_hpa:
            f = (math.log(target_p) - math.log(p1)) / (math.log(p2) - math.log(p1))
            return v1 + f * (v2 - v1)
    return None


def standard_levels(levels):
    out = {}
    keys = [
        "height_m", "temperature_c", "dewpoint_c", "relative_humidity_pct",
        "potential_temperature_k", "equivalent_potential_temperature_k"
    ]
    for p in STANDARD_LEVELS:
        d = {"pressure_hpa": p}
        for key in keys:
            v = log_interp(levels, p, key)
            d[key] = None if v is None else round(float(v), 2)

        # Wind must be interpolated vectorially, not by direction angle.
        uv = wind_at_pressure(levels, p)
        if uv:
            u, v = uv
            spd = math.hypot(u, v)
            direction = (math.degrees(math.atan2(-u, -v)) + 360) % 360
            d["wind_speed_ms"] = round(spd, 2)
            d["wind_direction_deg"] = round(direction, 1)
            d["u_ms"] = round(u, 2)
            d["v_ms"] = round(v, 2)
        else:
            d["wind_speed_ms"] = d["wind_direction_deg"] = None
            d["u_ms"] = d["v_ms"] = None
        out[str(p)] = d
    return out


def uv_from_speed_dir(speed, direction):
    rad = math.radians(float(direction))
    return -float(speed) * math.sin(rad), -float(speed) * math.cos(rad)


def wind_at_pressure(levels, target_p, max_gap_hpa=250):
    pts = []
    for l in levels:
        if finite(l.get("pressure_hpa")) and finite(l.get("wind_speed_ms")) and finite(l.get("wind_direction_deg")):
            u, v = uv_from_speed_dir(l["wind_speed_ms"], l["wind_direction_deg"])
            pts.append((float(l["pressure_hpa"]), u, v))
    pts.sort(reverse=True)
    for p, u, v in pts:
        if abs(p - target_p) < 0.05:
            return u, v
    for (p1, u1, v1), (p2, u2, v2) in zip(pts[:-1], pts[1:]):
        if p1 >= target_p >= p2 and (p1 - p2) <= max_gap_hpa:
            f = (math.log(target_p) - math.log(p1)) / (math.log(p2) - math.log(p1))
            return u1 + f * (u2 - u1), v1 + f * (v2 - v1)
    return None


def interp_by_height(levels, target_z, key):
    pts = sorted(
        [(float(l["height_m"]), float(l[key])) for l in levels
         if finite(l.get("height_m")) and finite(l.get(key))]
    )
    for z, v in pts:
        if abs(z - target_z) < 1:
            return v
    for (z1, v1), (z2, v2) in zip(pts[:-1], pts[1:]):
        if z1 <= target_z <= z2 and (z2 - z1) <= 2000:
            f = (target_z - z1) / (z2 - z1)
            return v1 + f * (v2 - v1)
    return None


def wind_at_height(levels, target_z):
    pts = []
    for l in levels:
        if finite(l.get("height_m")) and finite(l.get("wind_speed_ms")) and finite(l.get("wind_direction_deg")):
            u, v = uv_from_speed_dir(l["wind_speed_ms"], l["wind_direction_deg"])
            pts.append((float(l["height_m"]), u, v))
    pts.sort()
    for z, u, v in pts:
        if abs(z - target_z) < 1:
            return u, v
    for (z1, u1, v1), (z2, u2, v2) in zip(pts[:-1], pts[1:]):
        if z1 <= target_z <= z2 and (z2 - z1) <= 2000:
            f = (target_z - z1) / (z2 - z1)
            return u1 + f * (u2 - u1), v1 + f * (v2 - v1)
    return None


def resolution_metadata(levels):
    valid = sorted(
        [l for l in levels if finite(l.get("height_m")) and finite(l.get("pressure_hpa")) and l["pressure_hpa"] >= 700],
        key=lambda x: x["height_m"],
    )
    dz = [
        valid[i + 1]["height_m"] - valid[i]["height_m"]
        for i in range(len(valid) - 1)
        if 0 < valid[i + 1]["height_m"] - valid[i]["height_m"] < 5000
    ]
    med = statistics.median(dz) if dz else None
    if med is None:
        cls = "insufficient"
    elif med <= 250:
        cls = "moderate"
    elif med <= 750:
        cls = "coarse"
    else:
        cls = "very_coarse"

    if valid:
        z0 = min(x["height_m"] for x in valid)
        n500 = sum(1 for x in valid if x["height_m"] <= z0 + 500)
        n1000 = sum(1 for x in valid if x["height_m"] <= z0 + 1000)
    else:
        n500 = n1000 = 0

    return {
        "class": cls,
        "median_spacing_below_700_hpa_m": None if med is None else round(float(med), 1),
        "levels_in_first_500m": n500,
        "levels_in_first_1000m": n1000,
        "note": "IGRA reported-level resolution; not equivalent to high-resolution BUFR."
    }


def observed_stable_layers(levels):
    # Only adjacent OBSERVED IGRA levels. This is deliberately not the DWD inversion algorithm.
    arr = sorted(
        [l for l in levels if finite(l.get("height_m")) and finite(l.get("pressure_hpa")) and finite(l.get("temperature_c"))],
        key=lambda x: x["height_m"],
    )
    layers = []
    for a, b in zip(arr[:-1], arr[1:]):
        if min(a["pressure_hpa"], b["pressure_hpa"]) < 300:
            continue
        dz = b["height_m"] - a["height_m"]
        if not (100 <= dz <= 2000):
            continue
        dt = b["temperature_c"] - a["temperature_c"]
        if dt < 0.5:
            continue
        item = {
            "bottom_pressure_hpa": round(a["pressure_hpa"], 1),
            "top_pressure_hpa": round(b["pressure_hpa"], 1),
            "bottom_height_m": round(a["height_m"], 1),
            "top_height_m": round(b["height_m"], 1),
            "depth_m": round(dz, 1),
            "delta_temperature_c": round(dt, 2),
            "temperature_gradient_c_per_km": round(dt / dz * 1000.0, 2),
        }
        for name, key in [
            ("delta_theta_k", "potential_temperature_k"),
            ("delta_theta_e_k", "equivalent_potential_temperature_k"),
            ("delta_dewpoint_c", "dewpoint_c"),
        ]:
            item[name] = (
                round(b[key] - a[key], 2)
                if finite(a.get(key)) and finite(b.get(key)) else None
            )
        layers.append(item)
    return {
        "is_equivalent_to_high_resolution_inversion_algorithm": False,
        "method": "adjacent observed IGRA levels; p>=300 hPa; depth 100-2000 m; dT>=0.5 C",
        "count": len(layers),
        "layers": layers,
    }


def freezing_level(levels):
    pts = sorted(
        [(float(l["height_m"]), float(l["temperature_c"])) for l in levels
         if finite(l.get("height_m")) and finite(l.get("temperature_c"))]
    )
    if not pts:
        return None
    for (z1, t1), (z2, t2) in zip(pts[:-1], pts[1:]):
        if t1 == 0:
            return round(z1, 1)
        if t1 > 0 >= t2 and z2 > z1:
            f = t1 / (t1 - t2)
            return round(z1 + f * (z2 - z1), 1)
    return None


def calc_pwat(levels):
    rows = [
        l for l in levels
        if finite(l.get("pressure_hpa")) and finite(l.get("dewpoint_c"))
    ]
    rows.sort(key=lambda x: x["pressure_hpa"], reverse=True)
    if len(rows) < 3:
        return None, "insufficient_points"
    if rows[0]["pressure_hpa"] < 850 or rows[-1]["pressure_hpa"] > 500:
        return None, "insufficient_vertical_coverage"
    try:
        p = np.array([x["pressure_hpa"] for x in rows]) * units.hPa
        td = np.array([x["dewpoint_c"] for x in rows]) * units.degC
        val = precipitable_water(p, td).to("millimeter").m
        return round(abs(float(val)), 2), "ok"
    except Exception as e:
        return None, f"error:{type(e).__name__}"


def calc_shear(levels, depth_m):
    rows = [
        l for l in levels if all(finite(l.get(k)) for k in
        ("height_m", "pressure_hpa", "wind_speed_ms", "wind_direction_deg"))
    ]
    if len(rows) < 2:
        return {"magnitude_ms": None, "status": "insufficient_points"}
    rows.sort(key=lambda x: x["height_m"])
    z0 = rows[0]["height_m"]
    if rows[-1]["height_m"] < z0 + depth_m:
        return {"magnitude_ms": None, "status": "insufficient_vertical_coverage"}
    base = wind_at_height(rows, z0)
    top = wind_at_height(rows, z0 + depth_m)
    if not base or not top:
        return {"magnitude_ms": None, "status": "interpolation_gap_too_large"}
    du, dv = top[0] - base[0], top[1] - base[1]
    return {
        "u_shear_ms": round(du, 2),
        "v_shear_ms": round(dv, 2),
        "magnitude_ms": round(math.hypot(du, dv), 2),
        "status": "ok",
    }


def calc_ivt(levels):
    rows = []
    for l in levels:
        if all(finite(l.get(k)) for k in
               ("pressure_hpa", "temperature_c", "dewpoint_c", "wind_speed_ms", "wind_direction_deg")):
            p = float(l["pressure_hpa"])
            if 300 <= p <= 1050:
                u, v = uv_from_speed_dir(l["wind_speed_ms"], l["wind_direction_deg"])
                try:
                    q = float(
                        specific_humidity_from_dewpoint(
                            p * units.hPa, float(l["dewpoint_c"]) * units.degC
                        ).to("dimensionless").m
                    )
                    rows.append((p, q, u, v))
                except Exception:
                    pass
    rows.sort(reverse=True)
    if len(rows) < 4:
        return {"magnitude_kg_m_s": None, "status": "insufficient_points"}
    pmax, pmin = rows[0][0], rows[-1][0]
    if pmax < 850 or pmin > 400:
        return {"magnitude_kg_m_s": None, "status": "insufficient_vertical_coverage"}

    p_pa = np.array([r[0] * 100.0 for r in rows])
    qu = np.array([r[1] * r[2] for r in rows])
    qv = np.array([r[1] * r[3] for r in rows])
    # Pressure decreases with index; integrate in increasing pressure and divide by g.
    iu = float(np.trapezoid(qu[::-1], p_pa[::-1]) / G)
    iv = float(np.trapezoid(qv[::-1], p_pa[::-1]) / G)
    mag = math.hypot(iu, iv)
    # Direction TOWARD which moisture is transported, consistent with current sounding output.
    toward = (math.degrees(math.atan2(iu, iv)) + 360) % 360
    return {
        "u_kg_m_s": round(iu, 2),
        "v_kg_m_s": round(iv, 2),
        "magnitude_kg_m_s": round(mag, 2),
        "toward_direction_deg": round(toward, 1),
        "bottom_pressure_hpa": round(pmax, 1),
        "top_pressure_hpa": round(pmin, 1),
        "status": "ok",
    }


def thermo_arrays(levels):
    rows = [
        l for l in levels if all(finite(l.get(k)) for k in
        ("pressure_hpa", "temperature_c", "dewpoint_c"))
    ]
    rows.sort(key=lambda x: x["pressure_hpa"], reverse=True)
    # Remove duplicate pressure values.
    seen = set()
    unique = []
    for r in rows:
        pr = round(float(r["pressure_hpa"]), 2)
        if pr not in seen:
            unique.append(r)
            seen.add(pr)
    return unique


def cape_qc(rows):
    if len(rows) < 6:
        return False, "insufficient_points"
    pbot, ptop = rows[0]["pressure_hpa"], rows[-1]["pressure_hpa"]
    if pbot < 900:
        return False, "surface_layer_missing"
    if ptop > 500:
        return False, "insufficient_vertical_coverage"
    # Require at least 2 thermodynamic levels in lowest 1 km when heights exist.
    hrows = [r for r in rows if finite(r.get("height_m"))]
    if hrows:
        z0 = min(r["height_m"] for r in hrows)
        nlow = sum(1 for r in hrows if r["height_m"] <= z0 + 1000)
        if nlow < 2:
            return False, "low_level_resolution_too_coarse"
    return True, "ok"


def calc_convective(levels):
    rows = thermo_arrays(levels)
    good, status = cape_qc(rows)
    out = {
        "qc_status": status,
        "sbcape_jkg": None, "sbcin_jkg": None,
        "mlcape_jkg": None, "mlcin_jkg": None,
        "mucape_jkg": None, "mucin_jkg": None,
        "lifted_index_c": None,
    }
    if not good:
        return out

    try:
        p = np.array([r["pressure_hpa"] for r in rows]) * units.hPa
        t = np.array([r["temperature_c"] for r in rows]) * units.degC
        td = np.array([r["dewpoint_c"] for r in rows]) * units.degC

        sbcape, sbcin = surface_based_cape_cin(p, t, td)
        mlcape, mlcin = mixed_layer_cape_cin(p, t, td, depth=100 * units.hPa)
        mucape, mucin = most_unstable_cape_cin(p, t, td, depth=300 * units.hPa)

        def capeval(q):
            v = float(q.to("joule / kilogram").m)
            return round(max(0.0, v), 2)

        def cinval(q):
            return round(float(q.to("joule / kilogram").m), 2)

        out.update({
            "sbcape_jkg": capeval(sbcape), "sbcin_jkg": cinval(sbcin),
            "mlcape_jkg": capeval(mlcape), "mlcin_jkg": cinval(mlcin),
            "mucape_jkg": capeval(mucape), "mucin_jkg": cinval(mucin),
        })

        # Classical surface-parcel LI at 500 hPa. MetPy returns array-like quantity.
        prof = parcel_profile(p, t[0], td[0])
        li = lifted_index(p, t, prof)
        liv = np.asarray(li.to("delta_degC").m).reshape(-1)
        if len(liv) and np.isfinite(liv[0]):
            out["lifted_index_c"] = round(float(liv[0]), 2)
    except Exception as e:
        out["qc_status"] = f"error:{type(e).__name__}"
    return out


def lapse_rate(std, p1, p2):
    a, b = std.get(str(p1), {}), std.get(str(p2), {})
    if not all(finite(x) for x in
               (a.get("temperature_c"), b.get("temperature_c"), a.get("height_m"), b.get("height_m"))):
        return None
    dz = b["height_m"] - a["height_m"]
    if dz <= 0:
        return None
    return round((a["temperature_c"] - b["temperature_c"]) / dz * 1000.0, 3)


def build_profile(p, idx):
    h = p["header"]
    levels = p["levels"]
    add_theta(levels)
    std = standard_levels(levels)
    res = resolution_metadata(levels)
    pwat, pwat_status = calc_pwat(levels)
    conv = calc_convective(levels)

    nominal = p["nominal_dt"]
    pid = f"IGRA_{nominal:%Y%m%d_%H}_{idx:05d}"

    return {
        "schema_version": "igra-historical-v2",
        "profile_id": pid,
        "station": {
            "wmo": "14015",
            "igra_id": STATION_ID,
            "name": "Ljubljana/Bežigrad",
            "latitude": h["lat"],
            "longitude": h["lon"],
        },
        "time": {
            "nominal_utc": nominal.isoformat().replace("+00:00", "Z"),
            "year": h["year"], "month": h["month"], "day": h["day"], "hour": h["hour"],
            "release_time_hhmm_utc": int(h["reltime"]) if finite(h["reltime"]) else None,
        },
        "source": {
            "dataset": "NOAA IGRA v2",
            "station_id": STATION_ID,
            "format": "IGRA fixed-width",
            "profile_resolution": "reported_levels",
            "future_preferred_source": "original_BUFR_if_available",
            "superseded_by_high_resolution_bufr": False,
        },
        "qc": {
            "resolution": res,
            "historical_boundary_layer_comparability": "limited",
            "note": (
                "Historical IGRA profiles are mostly reported/significant levels and are not "
                "time- or resolution-equivalent to current 00/12 UTC high-resolution DWD BUFR."
            ),
        },
        "parameters": {
            "standard_levels": std,
            "precipitable_water_mm": pwat,
            "precipitable_water_qc": pwat_status,
            "freezing_level_m": freezing_level(levels),
            "lapse_rate_850_500_c_per_km": lapse_rate(std, 850, 500),
            "lapse_rate_700_500_c_per_km": lapse_rate(std, 700, 500),
            "shear_0_1km": calc_shear(levels, 1000),
            "shear_0_3km": calc_shear(levels, 3000),
            "shear_0_6km": calc_shear(levels, 6000),
            "ivt": calc_ivt(levels),
            "convective": conv,
        },
        "observed_stable_layers": observed_stable_layers(levels),
        "levels": levels,
    }


def compact_summary(profile):
    p = profile["parameters"]
    std = p["standard_levels"]
    def sl(level, key):
        return std.get(str(level), {}).get(key)

    return {
        "profile_id": profile["profile_id"],
        "nominal_utc": profile["time"]["nominal_utc"],
        "release_time_hhmm_utc": profile["time"]["release_time_hhmm_utc"],
        "resolution": profile["qc"]["resolution"],
        "source": profile["source"],
        "t925_c": sl(925, "temperature_c"),
        "t850_c": sl(850, "temperature_c"),
        "t700_c": sl(700, "temperature_c"),
        "t500_c": sl(500, "temperature_c"),
        "t300_c": sl(300, "temperature_c"),
        "theta925_k": sl(925, "potential_temperature_k"),
        "theta850_k": sl(850, "potential_temperature_k"),
        "theta700_k": sl(700, "potential_temperature_k"),
        "thetae925_k": sl(925, "equivalent_potential_temperature_k"),
        "thetae850_k": sl(850, "equivalent_potential_temperature_k"),
        "thetae700_k": sl(700, "equivalent_potential_temperature_k"),
        "wind925_ms": sl(925, "wind_speed_ms"),
        "wind850_ms": sl(850, "wind_speed_ms"),
        "wind700_ms": sl(700, "wind_speed_ms"),
        "wind500_ms": sl(500, "wind_speed_ms"),
        "wind300_ms": sl(300, "wind_speed_ms"),
        "wind925_dir": sl(925, "wind_direction_deg"),
        "wind850_dir": sl(850, "wind_direction_deg"),
        "wind700_dir": sl(700, "wind_direction_deg"),
        "wind500_dir": sl(500, "wind_direction_deg"),
        "wind300_dir": sl(300, "wind_direction_deg"),
        "pwat_mm": p["precipitable_water_mm"],
        "pwat_qc": p["precipitable_water_qc"],
        "freezing_level_m": p["freezing_level_m"],
        "lapse_850_500": p["lapse_rate_850_500_c_per_km"],
        "lapse_700_500": p["lapse_rate_700_500_c_per_km"],
        "shear_0_1km_ms": p["shear_0_1km"].get("magnitude_ms"),
        "shear_0_1km_qc": p["shear_0_1km"].get("status"),
        "shear_0_3km_ms": p["shear_0_3km"].get("magnitude_ms"),
        "shear_0_3km_qc": p["shear_0_3km"].get("status"),
        "shear_0_6km_ms": p["shear_0_6km"].get("magnitude_ms"),
        "shear_0_6km_qc": p["shear_0_6km"].get("status"),
        "ivt_kg_m_s": p["ivt"].get("magnitude_kg_m_s"),
        "ivt_toward_deg": p["ivt"].get("toward_direction_deg"),
        "ivt_qc": p["ivt"].get("status"),
        "sbcape_jkg": p["convective"].get("sbcape_jkg"),
        "sbcin_jkg": p["convective"].get("sbcin_jkg"),
        "mlcape_jkg": p["convective"].get("mlcape_jkg"),
        "mlcin_jkg": p["convective"].get("mlcin_jkg"),
        "mucape_jkg": p["convective"].get("mucape_jkg"),
        "mucin_jkg": p["convective"].get("mucin_jkg"),
        "lifted_index_c": p["convective"].get("lifted_index_c"),
        "convective_qc": p["convective"].get("qc_status"),
        # Keep only count in compact summary; detailed layers remain in gz profile archive.
        "observed_stable_layer_count": profile["observed_stable_layers"]["count"],
    }


CLIM_FIELDS = [
    "t925_c", "t850_c", "t700_c", "t500_c", "t300_c",
    "theta925_k", "theta850_k", "theta700_k",
    "thetae925_k", "thetae850_k", "thetae700_k",
    "wind925_ms", "wind850_ms", "wind700_ms", "wind500_ms", "wind300_ms",
    "pwat_mm", "freezing_level_m", "lapse_850_500", "lapse_700_500",
    "shear_0_1km_ms", "shear_0_3km_ms", "shear_0_6km_ms",
    "ivt_kg_m_s",
    "sbcape_jkg", "sbcin_jkg", "mlcape_jkg", "mlcin_jkg",
    "mucape_jkg", "mucin_jkg", "lifted_index_c",
]


def valid_for_clim(rec, field):
    v = rec.get(field)
    if not finite(v):
        return False
    if field == "pwat_mm" and rec.get("pwat_qc") != "ok":
        return False
    if field.startswith("shear_"):
        qcfield = field.replace("_ms", "_qc")
        if rec.get(qcfield) != "ok":
            return False
    if field == "ivt_kg_m_s" and rec.get("ivt_qc") != "ok":
        return False
    if field in {"sbcape_jkg","sbcin_jkg","mlcape_jkg","mlcin_jkg","mucape_jkg","mucin_jkg","lifted_index_c"}:
        if rec.get("convective_qc") != "ok":
            return False
    return True


def build_climatology(records):
    dated = []
    for r in records:
        dt = datetime.fromisoformat(r["nominal_utc"].replace("Z", "+00:00"))
        dated.append((doy365(dt.month, dt.day), r))

    days = []
    raw_series = {field: {q: [] for q in CLIM_PERCENTILES} for field in CLIM_FIELDS}

    for doy in range(1, 366):
        day = {"doy": doy, "parameters": {}}
        for field in CLIM_FIELDS:
            vals = [
                float(r[field]) for rdoy, r in dated
                if circular_doy_distance(rdoy, doy) <= WINDOW_DAYS and valid_for_clim(r, field)
            ]
            stats = {"n": len(vals)}
            for q in CLIM_PERCENTILES:
                val = pct(vals, q)
                stats[f"p{q}"] = val
                raw_series[field][q].append(val)
            day["parameters"][field] = stats
        days.append(day)

    # 10-day smoothed percentile curves; N stays raw.
    for field in CLIM_FIELDS:
        for q in CLIM_PERCENTILES:
            sm = circular_smooth(raw_series[field][q], SMOOTH_DAYS)
            for i, val in enumerate(sm):
                days[i]["parameters"][field][f"p{q}_smoothed"] = val

    return {
        "schema_version": "ljlm-daily-climatology-v2",
        "station": {"wmo": "14015", "igra_id": STATION_ID, "name": "Ljubljana/Bežigrad"},
        "period": f"{START_YEAR}-{END_YEAR}",
        "method": {
            "calendar_window_days_each_side": WINDOW_DAYS,
            "window_total_nominal_days": 2 * WINDOW_DAYS + 1,
            "percentiles": list(CLIM_PERCENTILES),
            "percentile_curve_smoothing_days": SMOOTH_DAYS,
            "smoothing": "circular moving mean; raw empirical percentiles retained",
            "sample_size_n": "stored separately for every parameter/day",
            "caveat": (
                "Historical launch times and IGRA vertical resolution differ from current DWD BUFR. "
                "Boundary-layer, CAPE/CIN and fine-layer comparisons require extra caution."
            ),
        },
        "parameters": CLIM_FIELDS,
        "days": days,
    }


def main():
    OUTDIR.mkdir(parents=True, exist_ok=True)
    CLIM_PATH.parent.mkdir(parents=True, exist_ok=True)

    lines = download_igra()
    parsed, duplicates = parse_profiles(lines)
    print(f"Parsed profiles {START_YEAR}-{END_YEAR}: {len(parsed)}")
    print(f"Duplicate profiles removed: {duplicates}")

    full_profiles = []
    summaries = []
    errors = []
    resolution = Counter()

    for idx, p in enumerate(parsed, 1):
        try:
            prof = build_profile(p, idx)
            full_profiles.append(prof)
            summaries.append(compact_summary(prof))
            resolution[prof["qc"]["resolution"]["class"]] += 1
        except Exception as e:
            errors.append({
                "nominal_utc": p["nominal_dt"].isoformat(),
                "error": f"{type(e).__name__}: {e}",
            })
        if idx % 500 == 0:
            print(f"Processed {idx}/{len(parsed)}")

    summary_doc = {
        "schema_version": "igra-historical-summary-v2",
        "station": {"wmo": "14015", "igra_id": STATION_ID, "name": "Ljubljana/Bežigrad"},
        "period": f"{START_YEAR}-{END_YEAR}",
        "profile_count": len(summaries),
        "duplicate_profiles_removed": duplicates,
        "resolution_classes": dict(resolution),
        "profiles": summaries,
    }

    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(summary_doc, f, ensure_ascii=False, separators=(",", ":"))

    with gzip.open(PROFILES_PATH, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump({
            "schema_version": "igra-historical-profiles-v2",
            "profile_count": len(full_profiles),
            "profiles": full_profiles,
        }, f, ensure_ascii=False, separators=(",", ":"))

    climatology = build_climatology(summaries)
    with open(CLIM_PATH, "w", encoding="utf-8") as f:
        json.dump(climatology, f, ensure_ascii=False, separators=(",", ":"))

    report = {
        "built": len(full_profiles),
        "errors": len(errors),
        "error_details": errors[:100],
        "duplicates_removed": duplicates,
        "resolution_classes": dict(resolution),
        "outputs": {
            "summary": str(SUMMARY_PATH),
            "profiles": str(PROFILES_PATH),
            "climatology": str(CLIM_PATH),
        },
        "qc_counts": {
            "pwat_ok": sum(r.get("pwat_qc") == "ok" for r in summaries),
            "shear_0_1km_ok": sum(r.get("shear_0_1km_qc") == "ok" for r in summaries),
            "shear_0_3km_ok": sum(r.get("shear_0_3km_qc") == "ok" for r in summaries),
            "shear_0_6km_ok": sum(r.get("shear_0_6km_qc") == "ok" for r in summaries),
            "ivt_ok": sum(r.get("ivt_qc") == "ok" for r in summaries),
            "convective_ok": sum(r.get("convective_qc") == "ok" for r in summaries),
        },
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("=" * 72)
    print("IGRA HISTORICAL ARCHIVE + EXPANDED CLIMATOLOGY BUILT")
    print("=" * 72)
    print("Built:", len(full_profiles))
    print("Errors:", len(errors))
    print("Resolution classes:", dict(resolution))
    print("QC counts:", report["qc_counts"])
    print(f"Summary:     {SUMMARY_PATH} ({SUMMARY_PATH.stat().st_size/1024/1024:.2f} MB)")
    print(f"Profiles:    {PROFILES_PATH} ({PROFILES_PATH.stat().st_size/1024/1024:.2f} MB)")
    print(f"Climatology: {CLIM_PATH} ({CLIM_PATH.stat().st_size/1024/1024:.2f} MB)")
    print(f"Report:      {REPORT_PATH}")

    if errors:
        raise SystemExit(f"Build completed with {len(errors)} profile errors; inspect report.")


if __name__ == "__main__":
    main()
