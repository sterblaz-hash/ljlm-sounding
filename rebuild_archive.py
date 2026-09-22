#!/usr/bin/env python3
"""
Rebuild all archived LJLM sounding JSON files from the already stored high-resolution
profile levels.

This script does NOT download BUFR data. It uses the calculation functions from
extract_ljlm.py so the archive and the live extractor always use the same algorithms.

Recomputed:
- RH (when T/Td are available)
- potential temperature theta at every level
- equivalent potential temperature theta-e at every level
- standard pressure levels
- all MetPy diagnostics
- climatology comparison
- inversion diagnostics
- previous-term / previous-day comparisons

Preserved:
- launch metadata
- source/source_file
- trajectory
- QC fields that came from BUFR
- original high-resolution profile levels (with derived level fields refreshed)
"""

import copy
import glob
import json
import math
import os
import re
from datetime import date

import metpy.calc as mpcalc
from metpy.units import units

import extract_ljlm as core

# NumPy 2.x removed np.trapz. The live extractor is also updated to use
# np.trapezoid, but keep this compatibility alias so an archive rebuild
# cannot lose IVT if GitHub checks out an older extractor revision.
if not hasattr(core.np, "trapz") and hasattr(core.np, "trapezoid"):
    core.np.trapz = core.np.trapezoid


def valid(value):
    return core.valid_number(value)


def refresh_level_thermodynamics(levels):
    """Refresh RH, theta and theta-e directly from p/T/Td for every archived level."""
    refreshed = []

    for old in levels:
        row = copy.deepcopy(old)

        # Normalize old archive schemas before passing levels to the current
        # extractor helpers. Some earliest JSON records omitted keys entirely
        # when a value was unavailable; current helpers expect the keys to
        # exist and allow their value to be None.
        for key in (
            "pressure_hpa",
            "height_m",
            "temperature_c",
            "dewpoint_c",
            "relative_humidity_pct",
            "wind_speed_ms",
            "wind_direction_deg",
            "potential_temperature_k",
            "equivalent_potential_temperature_k",
        ):
            row.setdefault(key, None)

        p = row.get("pressure_hpa")
        t = row.get("temperature_c")
        td = row.get("dewpoint_c")

        rh = None
        theta = None
        theta_e = None

        if valid(p) and valid(t):
            try:
                theta = float(
                    mpcalc.potential_temperature(
                        float(p) * units.hPa,
                        float(t) * units.degC
                    ).to("kelvin").magnitude
                )
            except Exception:
                theta = None

        if valid(p) and valid(t) and valid(td):
            try:
                rh = float(
                    mpcalc.relative_humidity_from_dewpoint(
                        float(t) * units.degC,
                        float(td) * units.degC
                    ).to("dimensionless").magnitude
                ) * 100.0
            except Exception:
                rh = None

            try:
                theta_e = float(
                    mpcalc.equivalent_potential_temperature(
                        float(p) * units.hPa,
                        float(t) * units.degC,
                        float(td) * units.degC
                    ).to("kelvin").magnitude
                )
            except Exception:
                theta_e = None

        row["relative_humidity_pct"] = (
            round(rh, 1)
            if rh is not None and math.isfinite(rh)
            else None
        )
        row["potential_temperature_k"] = (
            round(theta, 2)
            if theta is not None and math.isfinite(theta)
            else None
        )
        row["equivalent_potential_temperature_k"] = (
            round(theta_e, 2)
            if theta_e is not None and math.isfinite(theta_e)
            else None
        )

        refreshed.append(row)

    refreshed.sort(
        key=lambda x: (
            -(float(x["pressure_hpa"]))
            if valid(x.get("pressure_hpa"))
            else float("inf")
        )
    )
    return refreshed


def standard_levels(levels):
    result = {}

    for pressure in (850, 700, 500):
        result[f"t{pressure}"] = core.value_at_pressure(
            levels, "temperature_c", pressure
        )
        result[f"td{pressure}"] = core.value_at_pressure(
            levels, "dewpoint_c", pressure
        )
        result[f"rh{pressure}"] = core.value_at_pressure(
            levels, "relative_humidity_pct", pressure
        )

    return result


def archive_files():
    files = glob.glob("data/*/*/*.json")
    files = [
        p for p in files
        if os.path.basename(p) != "latest.json"
    ]

    def key(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d.get("launch_time", "")
        except Exception:
            return ""

    return sorted(files, key=key)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def rebuild_one(path):
    profile = read_json(path)

    levels = profile.get("levels")
    if not isinstance(levels, list) or len(levels) < 10:
        raise ValueError("missing/too-short levels array")

    nominal_text = profile.get("nominal_date")
    term = profile.get("term")

    # Older archive files may pre-date the nominal_date/term schema.
    # Recover these fields from the canonical archive filename first,
    # e.g. data/2026/09/20260920_00.json.
    if not nominal_text or not term:
        match = re.search(
            r"(\d{8})_(00|12|special_\d{4})\.json$",
            path
        )

        if match:
            nominal_date = date.fromisoformat(
                f"{match.group(1)[0:4]}-"
                f"{match.group(1)[4:6]}-"
                f"{match.group(1)[6:8]}"
            )
            term = match.group(2)
            nominal_text = nominal_date.isoformat()

            profile["nominal_date"] = nominal_text
            profile["term"] = term
            profile["sounding_id"] = (
                nominal_date.strftime("%Y%m%d") + "_" + term
            )
        else:
            raise ValueError(
                "missing nominal_date or term and filename cannot recover them"
            )
    else:
        nominal_date = date.fromisoformat(nominal_text)

    profile["levels"] = refresh_level_thermodynamics(levels)

    std = standard_levels(profile["levels"])
    metpy = core.calculate_metpy_parameters(profile["levels"])
    climatology = core.calculate_climatology_comparison(
        std, metpy, nominal_date
    )
    inversions = core.calculate_inversions(profile["levels"])

    profile["parameters"] = {
        "standard_levels": std,
        "metpy": metpy,
        "climatology": climatology,
        "inversions": inversions,
    }

    # This is intentionally calculated after all earlier archive files have
    # already been rebuilt, because comparisons read those files from disk.
    profile["parameters"]["change"] = core.calculate_previous_comparisons(
        profile, nominal_date, term
    )

    write_json(path, profile)
    return profile


def main():
    paths = archive_files()

    if not paths:
        raise SystemExit("No archived soundings found under data/YYYY/MM/*.json")

    print("=" * 72)
    print("LJLM ARCHIVE REBUILD")
    print("=" * 72)
    print("Archive files:", len(paths))
    print("BUFR download: NO")
    print("Algorithms: imported from extract_ljlm.py")
    print()

    rebuilt = []
    failed = []

    for i, path in enumerate(paths, start=1):
        try:
            profile = rebuild_one(path)
            rebuilt.append((path, profile))
            inv = (
                profile.get("parameters", {})
                .get("inversions", {})
                .get("summary", {})
                .get("total_count")
            )
            print(
                f"[{i:03d}/{len(paths):03d}] OK  {path} | "
                f"{profile.get('launch_time')} | inversions={inv}"
            )
        except Exception as exc:
            failed.append((path, str(exc)))
            print(f"[{i:03d}/{len(paths):03d}] ERROR {path}: {exc}")

    if failed:
        print()
        print("FAILED FILES:")
        for path, error in failed:
            print(" -", path, ":", error)
        raise SystemExit(1)

    # A second chronological pass refreshes comparisons once every profile
    # in the archive has the new schema.
    print()
    print("Refreshing archive comparisons...")

    for path, _ in rebuilt:
        profile = read_json(path)
        nominal_date = date.fromisoformat(profile["nominal_date"])
        term = profile["term"]
        profile["parameters"]["change"] = core.calculate_previous_comparisons(
            profile, nominal_date, term
        )
        write_json(path, profile)

    # latest.json must be the newest archived launch, not whichever file happened
    # to be processed last by pathname.
    newest_path = max(
        paths,
        key=lambda p: read_json(p).get("launch_time", "")
    )
    newest = read_json(newest_path)
    write_json(core.OUTPUT_LATEST, newest)

    print()
    print("=" * 72)
    print("REBUILD COMPLETE")
    print("Rebuilt:", len(rebuilt))
    print("Newest:", newest.get("sounding_id"), newest.get("launch_time"))
    print(
        "Latest inversions:",
        newest.get("parameters", {})
              .get("inversions", {})
              .get("summary", {})
              .get("total_count")
    )

    # Final schema checks.
    sample_levels = newest.get("levels", [])
    has_theta = any(
        valid(x.get("potential_temperature_k"))
        for x in sample_levels
    )
    has_theta_e = any(
        valid(x.get("equivalent_potential_temperature_k"))
        for x in sample_levels
    )
    has_inversions = isinstance(
        newest.get("parameters", {}).get("inversions"), dict
    )

    print("Latest has theta:", has_theta)
    print("Latest has theta-e:", has_theta_e)
    print("Latest has inversions:", has_inversions)

    if not (has_theta and has_theta_e and has_inversions):
        raise SystemExit("Final schema validation failed")


if __name__ == "__main__":
    main()
