#!/usr/bin/env python3
"""
Build the compact historical IGRA archive for Ljubljana/Bežigrad (SIM00014015).

Design goals
------------
* 1996-2025 historical reference archive.
* Preserve source/provenance so a profile can later be superseded by an
  original high-resolution BUFR without changing the public data model.
* Do NOT pretend that coarse IGRA data resolve today's thin inversions.
* Store robust standard-level and column diagnostics plus resolution metadata.
* Keep a compact summary for web/climatology work and optional compact profiles
  containing only observed IGRA levels (no invented fine vertical grid).

Outputs
-------
historical/igra_1996_2025_summary.json
historical/igra_1996_2025_profiles.json.gz
historical/igra_build_report.json
"""

from __future__ import annotations

import gzip
import io
import json
import math
import statistics
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from metpy.calc import (
    equivalent_potential_temperature,
    potential_temperature,
    precipitable_water,
)
from metpy.units import units

STATION = "SIM00014015"
URL = f"https://www.ncei.noaa.gov/pub/data/igra/data/data-por/{STATION}-data.txt.zip"
YEAR_START = 1996
YEAR_END = 2025
OUTDIR = Path("historical")
MISSING = {-9999, -8888}
STANDARD_PRESSURES = (925.0, 850.0, 700.0, 500.0, 300.0)


def finite(x):
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


def r(x, n=2):
    return round(float(x), n) if finite(x) else None


def as_int(s):
    try:
        v = int(s)
    except (TypeError, ValueError):
        return None
    return None if v in MISSING else v


def parse_header(line):
    return {
        "id": line[1:12].strip(),
        "year": as_int(line[13:17]),
        "month": as_int(line[18:20]),
        "day": as_int(line[21:23]),
        "hour": as_int(line[24:26]),
        "reltime": as_int(line[27:31]),
        "numlev": as_int(line[32:36]) or 0,
    }


def parse_level(line):
    p = as_int(line[9:15])
    z = as_int(line[16:21])
    t = as_int(line[22:27])
    dpd = as_int(line[34:39])
    wd = as_int(line[40:45])
    ws = as_int(line[46:51])

    tc = t / 10.0 if t is not None else None
    td = tc - dpd / 10.0 if tc is not None and dpd is not None else None

    return {
        "pressure_hpa": p / 100.0 if p is not None else None,
        "height_m": float(z) if z is not None else None,
        "temperature_c": tc,
        "dewpoint_c": td,
        "wind_direction_deg": float(wd) if wd is not None else None,
        "wind_speed_ms": ws / 10.0 if ws is not None else None,
    }


def download_profiles():
    print("Downloading:", URL)
    req = urllib.request.Request(URL, headers={"User-Agent": "ljlm-sounding/1.0"})
    with urllib.request.urlopen(req, timeout=120) as response:
        payload = response.read()
    print(f"Downloaded: {len(payload)/1024/1024:.1f} MB compressed")

    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
        if not names:
            raise RuntimeError("No IGRA TXT inside ZIP")
        with zf.open(names[0]) as f:
            lines = f.read().decode("ascii", errors="replace").splitlines()

    profiles = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("#"):
            i += 1
            continue
        h = parse_header(lines[i])
        n = h["numlev"]
        raw = lines[i + 1:i + 1 + n]
        i += 1 + n

        if h["id"] != STATION or h["year"] is None:
            continue
        if not YEAR_START <= h["year"] <= YEAR_END:
            continue

        profiles.append({
            **h,
            "levels": [parse_level(x) for x in raw],
        })
    return profiles


def profile_score(p):
    levels = p["levels"]
    thermo = sum(
        finite(x["pressure_hpa"]) and finite(x["height_m"]) and finite(x["temperature_c"])
        for x in levels
    )
    dew = sum(finite(x["dewpoint_c"]) for x in levels)
    wind = sum(
        finite(x["wind_speed_ms"]) and finite(x["wind_direction_deg"]) for x in levels
    )
    return thermo, dew, wind, len(levels)


def deduplicate(profiles):
    # Conservative nominal-time deduplication. In the current 1996-2025 archive
    # this may remove none; keeping the logic makes future source revisions safe.
    groups = defaultdict(list)
    for p in profiles:
        key = (p["year"], p["month"], p["day"], p["hour"])
        groups[key].append(p)

    out = []
    removed = 0
    for group in groups.values():
        group.sort(key=profile_score, reverse=True)
        out.append(group[0])
        removed += len(group) - 1
    out.sort(key=lambda p: (p["year"], p["month"], p["day"], p["hour"]))
    return out, removed


def log_interp(levels, field, target_hpa, max_gap_hpa=250):
    rows = sorted(
        [
            (x["pressure_hpa"], x.get(field))
            for x in levels
            if finite(x["pressure_hpa"]) and finite(x.get(field))
        ],
        reverse=True,
    )
    for p, v in rows:
        if abs(p - target_hpa) < 0.05:
            return v

    for (p1, v1), (p2, v2) in zip(rows[:-1], rows[1:]):
        if p1 >= target_hpa >= p2 and p1 != p2:
            if p1 - p2 > max_gap_hpa:
                return None
            x = (math.log(target_hpa) - math.log(p1)) / (math.log(p2) - math.log(p1))
            return v1 + x * (v2 - v1)
    return None


def add_theta(level):
    p, t, td = level["pressure_hpa"], level["temperature_c"], level["dewpoint_c"]
    level["potential_temperature_k"] = None
    level["equivalent_potential_temperature_k"] = None
    if finite(p) and finite(t):
        try:
            th = potential_temperature(p * units.hPa, t * units.degC)
            level["potential_temperature_k"] = r(th.to("kelvin").m, 2)
        except Exception:
            pass
    if finite(p) and finite(t) and finite(td):
        try:
            the = equivalent_potential_temperature(
                p * units.hPa, t * units.degC, td * units.degC
            )
            level["equivalent_potential_temperature_k"] = r(the.to("kelvin").m, 2)
        except Exception:
            pass


def resolution_metrics(levels):
    rows = [
        x for x in levels
        if finite(x["pressure_hpa"]) and finite(x["height_m"])
        and finite(x["temperature_c"]) and x["pressure_hpa"] >= 700
    ]
    rows.sort(key=lambda x: x["height_m"])
    if not rows:
        return {
            "lowest_valid_height_m": None,
            "levels_surface_to_700": 0,
            "levels_first_500m": 0,
            "levels_first_1000m": 0,
            "median_spacing_surface_to_700_m": None,
            "inversion_resolution_class": "insufficient",
        }

    z0 = rows[0]["height_m"]
    dz = [
        b["height_m"] - a["height_m"]
        for a, b in zip(rows[:-1], rows[1:])
        if 0 < b["height_m"] - a["height_m"] <= 2000
    ]
    med = statistics.median(dz) if dz else None
    n500 = sum(0 <= x["height_m"] - z0 <= 500 for x in rows)
    n1000 = sum(0 <= x["height_m"] - z0 <= 1000 for x in rows)

    if med is None:
        cls = "insufficient"
    elif med <= 250:
        cls = "moderate"
    elif med <= 750:
        cls = "coarse"
    else:
        cls = "very_coarse"

    return {
        "lowest_valid_height_m": r(z0, 0),
        "levels_surface_to_700": len(rows),
        "levels_first_500m": n500,
        "levels_first_1000m": n1000,
        "median_spacing_surface_to_700_m": r(med, 1),
        "inversion_resolution_class": cls,
    }


def robust_stable_layers(levels):
    """
    Resolution-aware historical diagnostic.

    This is NOT the high-resolution DWD inversion algorithm. It reports only
    observed adjacent-layer warming where both endpoints are actual IGRA
    observations. No fine interpolation and no "cap" label.

    To reduce false precision:
      * require dz >= 100 m,
      * require dT >= 0.5 C,
      * accept only dz <= 2000 m,
      * report the raw layer depth and endpoint pressures.
    """
    rows = [
        x for x in levels
        if finite(x["pressure_hpa"]) and finite(x["height_m"])
        and finite(x["temperature_c"]) and x["pressure_hpa"] >= 300
    ]
    rows.sort(key=lambda x: x["height_m"])
    found = []

    for a, b in zip(rows[:-1], rows[1:]):
        dz = b["height_m"] - a["height_m"]
        dt = b["temperature_c"] - a["temperature_c"]
        if not (100 <= dz <= 2000 and dt >= 0.5):
            continue

        layer = {
            "bottom_pressure_hpa": r(a["pressure_hpa"], 1),
            "top_pressure_hpa": r(b["pressure_hpa"], 1),
            "bottom_height_m": r(a["height_m"], 0),
            "top_height_m": r(b["height_m"], 0),
            "depth_m": r(dz, 0),
            "delta_temperature_c": r(dt, 2),
            "temperature_gradient_c_per_km": r(1000 * dt / dz, 2),
            "delta_theta_k": None,
            "delta_theta_e_k": None,
            "delta_dewpoint_c": None,
        }
        if finite(a.get("potential_temperature_k")) and finite(b.get("potential_temperature_k")):
            layer["delta_theta_k"] = r(
                b["potential_temperature_k"] - a["potential_temperature_k"], 2
            )
        if finite(a.get("equivalent_potential_temperature_k")) and finite(b.get("equivalent_potential_temperature_k")):
            layer["delta_theta_e_k"] = r(
                b["equivalent_potential_temperature_k"] -
                a["equivalent_potential_temperature_k"], 2
            )
        if finite(a["dewpoint_c"]) and finite(b["dewpoint_c"]):
            layer["delta_dewpoint_c"] = r(b["dewpoint_c"] - a["dewpoint_c"], 2)
        found.append(layer)

    return found


def freezing_level(levels):
    rows = sorted(
        [
            x for x in levels
            if finite(x["height_m"]) and finite(x["temperature_c"])
        ],
        key=lambda x: x["height_m"],
    )
    for a, b in zip(rows[:-1], rows[1:]):
        ta, tb = a["temperature_c"], b["temperature_c"]
        if ta == 0:
            return a["height_m"]
        if ta > 0 >= tb and b["height_m"] > a["height_m"]:
            f = (0 - ta) / (tb - ta)
            return a["height_m"] + f * (b["height_m"] - a["height_m"])
    return None


def pwat(levels):
    rows = sorted(
        [
            x for x in levels
            if finite(x["pressure_hpa"]) and finite(x["dewpoint_c"])
        ],
        key=lambda x: x["pressure_hpa"],
        reverse=True,
    )
    if len(rows) < 3:
        return None
    # Require meaningful column coverage; avoid publishing truncated PWAT.
    if rows[0]["pressure_hpa"] < 850 or rows[-1]["pressure_hpa"] > 500:
        return None
    try:
        p = np.array([x["pressure_hpa"] for x in rows]) * units.hPa
        td = np.array([x["dewpoint_c"] for x in rows]) * units.degC
        val = precipitable_water(p, td)
        return r(val.to("millimeter").m, 2)
    except Exception:
        return None


def lapse_rate(levels, p_bottom, p_top):
    t1 = log_interp(levels, "temperature_c", p_bottom)
    t2 = log_interp(levels, "temperature_c", p_top)
    z1 = log_interp(levels, "height_m", p_bottom)
    z2 = log_interp(levels, "height_m", p_top)
    if not all(finite(x) for x in (t1, t2, z1, z2)) or z2 <= z1:
        return None
    return 1000 * (t1 - t2) / (z2 - z1)


def build_record(p, idx):
    levels = [dict(x) for x in p["levels"]]
    for x in levels:
        add_theta(x)

    nominal = f"{p['year']:04d}-{p['month']:02d}-{p['day']:02d}T{p['hour']:02d}:00:00Z"
    sid = f"IGRA_{p['year']:04d}{p['month']:02d}{p['day']:02d}_{p['hour']:02d}_{idx:05d}"

    std = {}
    for pressure in STANDARD_PRESSURES:
        key = str(int(pressure))
        std[key] = {
            "temperature_c": r(log_interp(levels, "temperature_c", pressure), 2),
            "dewpoint_c": r(log_interp(levels, "dewpoint_c", pressure), 2),
            "height_m": r(log_interp(levels, "height_m", pressure), 0),
            "potential_temperature_k": r(
                log_interp(levels, "potential_temperature_k", pressure), 2
            ),
            "equivalent_potential_temperature_k": r(
                log_interp(levels, "equivalent_potential_temperature_k", pressure), 2
            ),
        }

    stable = robust_stable_layers(levels)
    resolution = resolution_metrics(levels)

    summary = {
        "profile_id": sid,
        "source": {
            "dataset": "NOAA IGRA v2",
            "station_id": STATION,
            "format": "IGRA fixed-width",
            "profile_resolution": "reported_levels",
            "future_preferred_source": "original_BUFR_if_available",
            "superseded_by_high_resolution_bufr": False,
        },
        "nominal_time": nominal,
        "release_time_hhmm_utc": p["reltime"],
        "level_count": len(levels),
        "resolution": resolution,
        "standard_levels": std,
        "parameters": {
            "pwat_mm": pwat(levels),
            "freezing_level_m": r(freezing_level(levels), 0),
            "lapse_rate_850_500_c_per_km": r(lapse_rate(levels, 850, 500), 2),
            "lapse_rate_700_500_c_per_km": r(lapse_rate(levels, 700, 500), 2),
            "observed_stable_layers": {
                "method": "adjacent_observed_IGRA_levels_only",
                "is_equivalent_to_high_resolution_inversion_algorithm": False,
                "count": len(stable),
                "layers": stable,
            },
        },
        "qc": {
            "historical_boundary_layer_comparability": "limited",
            "note": (
                "IGRA vertical resolution and historical launch times differ from "
                "current DWD 00/12 UTC profiles. Thin inversions are not comparable."
            ),
        },
    }

    compact_levels = []
    for x in levels:
        compact_levels.append({
            "pressure_hpa": r(x["pressure_hpa"], 1),
            "height_m": r(x["height_m"], 0),
            "temperature_c": r(x["temperature_c"], 2),
            "dewpoint_c": r(x["dewpoint_c"], 2),
            "potential_temperature_k": r(x["potential_temperature_k"], 2),
            "equivalent_potential_temperature_k": r(
                x["equivalent_potential_temperature_k"], 2
            ),
            "wind_speed_ms": r(x["wind_speed_ms"], 2),
            "wind_direction_deg": r(x["wind_direction_deg"], 1),
        })

    detail = {
        "profile_id": sid,
        "source": summary["source"],
        "nominal_time": nominal,
        "release_time_hhmm_utc": p["reltime"],
        "resolution": resolution,
        "levels": compact_levels,
        "parameters": summary["parameters"],
        "qc": summary["qc"],
    }
    return summary, detail


def main():
    OUTDIR.mkdir(exist_ok=True)
    raw = download_profiles()
    profiles, removed = deduplicate(raw)

    print(f"Profiles parsed: {len(raw)}")
    print(f"Profiles retained: {len(profiles)}")
    print(f"Duplicates removed: {removed}")

    summaries = []
    details = []
    errors = []

    for i, p in enumerate(profiles, 1):
        try:
            s, d = build_record(p, i)
            summaries.append(s)
            details.append(d)
        except Exception as e:
            errors.append({
                "date": f"{p['year']:04d}-{p['month']:02d}-{p['day']:02d}",
                "hour": p["hour"],
                "error": repr(e),
            })
        if i % 500 == 0 or i == len(profiles):
            print(f"[{i:05d}/{len(profiles):05d}] built={len(summaries)} errors={len(errors)}")

    summary_doc = {
        "schema_version": 1,
        "station": STATION,
        "period": {"start": YEAR_START, "end": YEAR_END},
        "profile_count": len(summaries),
        "provenance_policy": {
            "baseline_source": "NOAA IGRA v2",
            "replacement_policy": (
                "If original high-resolution BUFR becomes available, retain IGRA "
                "provenance and allow a BUFR-derived profile to become the preferred "
                "representation for the same launch."
            ),
        },
        "profiles": summaries,
    }

    summary_path = OUTDIR / "igra_1996_2025_summary.json"
    summary_path.write_text(
        json.dumps(summary_doc, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    profiles_path = OUTDIR / "igra_1996_2025_profiles.json.gz"
    with gzip.open(profiles_path, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(
            {
                "schema_version": 1,
                "station": STATION,
                "profiles": details,
            },
            f,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    classes = defaultdict(int)
    for s in summaries:
        classes[s["resolution"]["inversion_resolution_class"]] += 1

    report = {
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_url": URL,
        "parsed_profiles": len(raw),
        "retained_profiles": len(profiles),
        "duplicates_removed": removed,
        "successfully_built": len(summaries),
        "errors": errors,
        "resolution_classes": dict(classes),
        "output_bytes": {
            "summary_json": summary_path.stat().st_size,
            "profiles_json_gz": profiles_path.stat().st_size,
        },
    }
    report_path = OUTDIR / "igra_build_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n" + "=" * 72)
    print("IGRA HISTORICAL ARCHIVE BUILT")
    print("=" * 72)
    print("Built:", len(summaries))
    print("Errors:", len(errors))
    print("Resolution classes:", dict(classes))
    print(f"Summary:  {summary_path} ({summary_path.stat().st_size/1024/1024:.2f} MB)")
    print(f"Profiles: {profiles_path} ({profiles_path.stat().st_size/1024/1024:.2f} MB)")
    print("Report:  ", report_path)

    if errors:
        raise SystemExit("Build completed with profile errors; inspect report before commit.")


if __name__ == "__main__":
    main()
