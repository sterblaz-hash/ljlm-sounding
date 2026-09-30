#!/usr/bin/env python3
"""
A/B source test for LJLM pseudo-soundings:
  A) Open-Meteo Single Runs API, ICON-D2 pressure levels
  B) DWD Open Data, ICON-D2 native model levels (regular lat/lon GRIB2)

The script is intentionally standalone and does not modify the production archive.
It reads data/latest.json by default, derives the verifying 00/12 UTC time,
and compares both sources for the same ICON-D2 run / lead time.

Outputs:
  test/icon_d2_sources/openmeteo_profile.json
  test/icon_d2_sources/dwd_native_profile.json
  test/icon_d2_sources/source_comparison.json
  test/icon_d2_sources/source_comparison.csv
  test/icon_d2_sources/source_comparison.png

Requirements are already expected in the LJLM repository:
  numpy, matplotlib, metpy, eccodes
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

import numpy as np
import matplotlib.pyplot as plt

from icon_d2_native import (
    utc_iso,
    parse_utc,
    fetch_json,
    safe_float,
    round_or_none,
    q_from_dewpoint,
    dewpoint_from_q,
    theta_thetae,
    uv_from_speed_dir,
    speed_dir_from_uv,
    fetch_dwd_native_profile,
)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

OPEN_METEO_ENDPOINT = "https://single-runs-api.open-meteo.com/v1/forecast"

OPEN_METEO_LEVELS = [
    1000, 975, 950, 925, 900, 850, 800, 700, 600, 500,
    400, 300, 250, 200, 150, 100, 70, 50, 30,
]

STANDARD_LEVELS = [925, 850, 700, 500, 300, 250, 200]


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# -----------------------------------------------------------------------------
# Sounding / timing
# -----------------------------------------------------------------------------

def derive_valid_time(obs: dict[str, Any]) -> datetime:
    """Return nominal verifying time (00 or 12 UTC) for the observed sounding."""
    nominal_date = obs.get("nominal_date")
    term = str(obs.get("term", ""))

    if nominal_date and term in {"00", "12"}:
        return datetime.fromisoformat(f"{nominal_date}T{term}:00:00+00:00")

    launch = parse_utc(obs["launch_time"])

    # LJLM operational convention used by extract_ljlm.py:
    # ~23:30 UTC launch -> following 00 UTC; ~11:30 -> 12 UTC.
    if launch.hour >= 21:
        d = (launch + timedelta(days=1)).date()
        return datetime(d.year, d.month, d.day, 0, tzinfo=timezone.utc)
    if 9 <= launch.hour <= 14:
        d = launch.date()
        return datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc)

    # Fallback: nearest 00/12 UTC.
    candidates = []
    for offset in (-1, 0, 1):
        d = (launch + timedelta(days=offset)).date()
        for hour in (0, 12):
            candidates.append(datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc))
    return min(candidates, key=lambda x: abs((x - launch).total_seconds()))


def derive_run(valid_time: datetime, lead_hours: int) -> datetime:
    run = valid_time - timedelta(hours=lead_hours)
    if run.hour not in {0, 3, 6, 9, 12, 15, 18, 21} or run.minute != 0:
        raise ValueError(
            f"Derived ICON-D2 run {utc_iso(run)} is not a 3-hourly cycle. "
            "Use a lead that maps the 00/12 UTC valid time to an ICON-D2 cycle."
        )
    return run


# -----------------------------------------------------------------------------
# Open-Meteo ICON-D2 pressure-level profile
# -----------------------------------------------------------------------------

def fetch_openmeteo_profile(
    latitude: float,
    longitude: float,
    run: datetime,
    valid_time: datetime,
) -> dict[str, Any]:
    variables: list[str] = []
    for p in OPEN_METEO_LEVELS:
        variables.extend([
            f"temperature_{p}hPa",
            f"relative_humidity_{p}hPa",
            f"dew_point_{p}hPa",
            f"wind_speed_{p}hPa",
            f"wind_direction_{p}hPa",
            f"geopotential_height_{p}hPa",
        ])

    params = {
        "latitude": f"{latitude:.5f}",
        "longitude": f"{longitude:.5f}",
        "hourly": ",".join(variables),
        "wind_speed_unit": "ms",
        "models": "icon_d2",
        "run": run.strftime("%Y-%m-%dT%H:%M"),
        "timezone": "UTC",
        "elevation": "nan",
        "cell_selection": "nearest",
    }
    url = OPEN_METEO_ENDPOINT + "?" + urlencode(params)

    t0 = time.perf_counter()
    data = fetch_json(url, timeout=120)
    elapsed = time.perf_counter() - t0

    if data.get("error"):
        raise RuntimeError(f"Open-Meteo error: {data.get('reason')}")

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    target = valid_time.strftime("%Y-%m-%dT%H:%M")
    if target not in times:
        raise RuntimeError(
            f"Open-Meteo run does not contain valid time {target}. "
            f"Available time range: {times[0] if times else '?'} .. {times[-1] if times else '?'}"
        )
    idx = times.index(target)

    levels = []
    for p in OPEN_METEO_LEVELS:
        def at(name: str) -> float | None:
            arr = hourly.get(name)
            if not arr or idx >= len(arr):
                return None
            return safe_float(arr[idx])

        t_c = at(f"temperature_{p}hPa")
        td_c = at(f"dew_point_{p}hPa")
        rh = at(f"relative_humidity_{p}hPa")
        ws = at(f"wind_speed_{p}hPa")
        wd = at(f"wind_direction_{p}hPa")
        z = at(f"geopotential_height_{p}hPa")

        # Exclude missing pressure surfaces. Keep below-ground values if the API
        # explicitly supplies them; the comparison metadata flags them later.
        if t_c is None and td_c is None and ws is None and z is None:
            continue

        q = q_from_dewpoint(p, td_c) if td_c is not None else None
        theta, thetae = theta_thetae(p, t_c, td_c) if t_c is not None else (None, None)
        u = v = None
        if ws is not None and wd is not None:
            u, v = uv_from_speed_dir(ws, wd)

        levels.append({
            "pressure_hpa": float(p),
            "height_m": round_or_none(z, 1),
            "temperature_c": round_or_none(t_c, 2),
            "dewpoint_c": round_or_none(td_c, 2),
            "relative_humidity_pct": round_or_none(rh, 1),
            "specific_humidity_gkg": round_or_none(q * 1000.0 if q is not None else None, 3),
            "potential_temperature_k": round_or_none(theta, 2),
            "equivalent_potential_temperature_k": round_or_none(thetae, 2),
            "u_ms": round_or_none(u, 3),
            "v_ms": round_or_none(v, 3),
            "wind_speed_ms": round_or_none(ws, 2),
            "wind_direction_deg": round_or_none(wd, 1),
        })

    z500 = next((x["height_m"] for x in levels if x["pressure_hpa"] == 500), None)
    z1000 = next((x["height_m"] for x in levels if x["pressure_hpa"] == 1000), None)
    thickness = None
    if z500 is not None and z1000 is not None:
        thickness = z500 - z1000

    return {
        "source": "open-meteo",
        "model": "icon_d2",
        "run_time": utc_iso(run),
        "valid_time": utc_iso(valid_time),
        "lead_hours": int((valid_time - run).total_seconds() / 3600),
        "requested_latitude": latitude,
        "requested_longitude": longitude,
        "grid_latitude": data.get("latitude"),
        "grid_longitude": data.get("longitude"),
        "grid_elevation_m": data.get("elevation"),
        "pressure_levels_count": len(levels),
        "fetch_seconds": round(elapsed, 2),
        "z500_m": round_or_none(z500, 1),
        "thickness_1000_500_m": round_or_none(thickness, 1),
        "levels": levels,
    }


# -----------------------------------------------------------------------------
# Interpolation and source comparison
# -----------------------------------------------------------------------------

def interp_logp(levels: list[dict[str, Any]], pressure_hpa: float, field: str) -> float | None:
    pairs = []
    for row in levels:
        p = safe_float(row.get("pressure_hpa"))
        v = safe_float(row.get(field))
        if p is not None and v is not None and p > 0:
            pairs.append((p, v))
    if len(pairs) < 2:
        return None

    pairs.sort(key=lambda x: x[0])
    ps = np.asarray([x[0] for x in pairs], dtype=float)
    vs = np.asarray([x[1] for x in pairs], dtype=float)
    if pressure_hpa < ps.min() or pressure_hpa > ps.max():
        return None
    return float(np.interp(math.log(pressure_hpa), np.log(ps), vs))


def row_at_pressure(levels: list[dict[str, Any]], pressure_hpa: int) -> dict[str, Any] | None:
    for row in levels:
        if abs(float(row.get("pressure_hpa", -9999)) - pressure_hpa) < 0.01:
            return row
    return None


def compare_sources(openmeteo: dict[str, Any], dwd: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for p in STANDARD_LEVELS:
        om = row_at_pressure(openmeteo["levels"], p)
        if not om:
            continue

        dwd_t = interp_logp(dwd["levels"], p, "temperature_c")
        dwd_q = interp_logp(dwd["levels"], p, "specific_humidity_gkg")
        dwd_u = interp_logp(dwd["levels"], p, "u_ms")
        dwd_v = interp_logp(dwd["levels"], p, "v_ms")
        dwd_td = dewpoint_from_q(p, dwd_q / 1000.0) if dwd_q is not None else None
        dwd_ws = dwd_wd = None
        if dwd_u is not None and dwd_v is not None:
            dwd_ws, dwd_wd = speed_dir_from_uv(dwd_u, dwd_v)

        om_t = safe_float(om.get("temperature_c"))
        om_td = safe_float(om.get("dewpoint_c"))
        om_q = safe_float(om.get("specific_humidity_gkg"))
        om_ws = safe_float(om.get("wind_speed_ms"))

        rows.append({
            "pressure_hpa": p,
            "openmeteo_t_c": round_or_none(om_t, 2),
            "dwd_native_t_c": round_or_none(dwd_t, 2),
            "delta_t_openmeteo_minus_dwd_c": round_or_none(om_t - dwd_t if om_t is not None and dwd_t is not None else None, 2),
            "openmeteo_td_c": round_or_none(om_td, 2),
            "dwd_native_td_c": round_or_none(dwd_td, 2),
            "delta_td_openmeteo_minus_dwd_c": round_or_none(om_td - dwd_td if om_td is not None and dwd_td is not None else None, 2),
            "openmeteo_q_gkg": round_or_none(om_q, 3),
            "dwd_native_q_gkg": round_or_none(dwd_q, 3),
            "delta_q_openmeteo_minus_dwd_gkg": round_or_none(om_q - dwd_q if om_q is not None and dwd_q is not None else None, 3),
            "openmeteo_wind_ms": round_or_none(om_ws, 2),
            "dwd_native_wind_ms": round_or_none(dwd_ws, 2),
            "delta_wind_openmeteo_minus_dwd_ms": round_or_none(om_ws - dwd_ws if om_ws is not None and dwd_ws is not None else None, 2),
            "dwd_native_wind_direction_deg": round_or_none(dwd_wd, 1),
        })

    def rmse(key: str) -> float | None:
        vals = [safe_float(x.get(key)) for x in rows]
        vals = [x for x in vals if x is not None]
        if not vals:
            return None
        return math.sqrt(sum(x * x for x in vals) / len(vals))

    return {
        "model": "icon_d2",
        "run_time": openmeteo["run_time"],
        "valid_time": openmeteo["valid_time"],
        "lead_hours": openmeteo["lead_hours"],
        "source_timing": {
            "openmeteo_fetch_seconds": openmeteo.get("fetch_seconds"),
            "dwd_native_fetch_seconds": dwd.get("fetch_seconds"),
            "dwd_native_download_mb": dwd.get("compressed_download_mb"),
        },
        "grid_points": {
            "requested": [openmeteo.get("requested_latitude"), openmeteo.get("requested_longitude")],
            "openmeteo": [openmeteo.get("grid_latitude"), openmeteo.get("grid_longitude")],
            "dwd_native": [dwd.get("grid_latitude"), dwd.get("grid_longitude")],
        },
        "vertical_resolution": {
            "openmeteo_pressure_levels": openmeteo.get("pressure_levels_count"),
            "dwd_native_levels_to_test_top": dwd.get("native_levels_count"),
        },
        "synoptic": {
            "openmeteo_z500_m": openmeteo.get("z500_m"),
            "dwd_z500_m": dwd.get("z500_m"),
            "openmeteo_thickness_1000_500_m": openmeteo.get("thickness_1000_500_m"),
            "dwd_thickness_1000_500_m": dwd.get("thickness_1000_500_m"),
        },
        "rmse_standard_levels": {
            "temperature_c": round_or_none(rmse("delta_t_openmeteo_minus_dwd_c"), 3),
            "dewpoint_c": round_or_none(rmse("delta_td_openmeteo_minus_dwd_c"), 3),
            "specific_humidity_gkg": round_or_none(rmse("delta_q_openmeteo_minus_dwd_gkg"), 4),
            "wind_speed_ms": round_or_none(rmse("delta_wind_openmeteo_minus_dwd_ms"), 3),
        },
        "standard_levels": rows,
    }


def save_comparison_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def make_plot(path: Path, om: dict[str, Any], dwd: dict[str, Any], comp: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    om_levels = [x for x in om["levels"] if x.get("pressure_hpa") is not None]
    dwd_levels = [x for x in dwd["levels"] if x.get("pressure_hpa") is not None]

    fig, axes = plt.subplots(1, 3, figsize=(13, 7), constrained_layout=True)

    # T/Td profile
    ax = axes[0]
    ax.plot([x.get("temperature_c") for x in om_levels], [x["pressure_hpa"] for x in om_levels], label="Open-Meteo T", linewidth=2)
    ax.plot([x.get("dewpoint_c") for x in om_levels], [x["pressure_hpa"] for x in om_levels], label="Open-Meteo Td", linewidth=1.8)
    ax.plot([x.get("temperature_c") for x in dwd_levels], [x["pressure_hpa"] for x in dwd_levels], label="DWD native T", linewidth=1.6)
    ax.plot([x.get("dewpoint_c") for x in dwd_levels], [x["pressure_hpa"] for x in dwd_levels], label="DWD native Td", linewidth=1.4)
    ax.set_yscale("log")
    ax.invert_yaxis()
    ax.set_ylim(1000, 180)
    ax.set_yticks([1000, 925, 850, 700, 500, 300, 200])
    ax.get_yaxis().set_major_formatter(plt.ScalarFormatter())
    ax.set_xlabel("Temperature / dew point (°C)")
    ax.set_ylabel("Pressure (hPa)")
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8)
    ax.set_title("Thermodynamic profile")

    # q / wind
    ax = axes[1]
    ax.plot([x.get("specific_humidity_gkg") for x in om_levels], [x["pressure_hpa"] for x in om_levels], label="Open-Meteo q")
    ax.plot([x.get("specific_humidity_gkg") for x in dwd_levels], [x["pressure_hpa"] for x in dwd_levels], label="DWD native q")
    ax.set_yscale("log")
    ax.invert_yaxis()
    ax.set_ylim(1000, 180)
    ax.set_yticks([1000, 925, 850, 700, 500, 300, 200])
    ax.get_yaxis().set_major_formatter(plt.ScalarFormatter())
    ax.set_xlabel("Specific humidity (g/kg)")
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8, loc="upper right")
    ax2 = ax.twiny()
    ax2.plot([x.get("wind_speed_ms") for x in om_levels], [x["pressure_hpa"] for x in om_levels], linestyle="--", label="Open-Meteo wind")
    ax2.plot([x.get("wind_speed_ms") for x in dwd_levels], [x["pressure_hpa"] for x in dwd_levels], linestyle=":", label="DWD native wind")
    ax2.set_xlabel("Wind speed (m/s)")
    ax2.legend(fontsize=8, loc="lower right")
    ax.set_title("Moisture and wind")

    # deltas
    ax = axes[2]
    rows = comp.get("standard_levels", [])
    p = [x["pressure_hpa"] for x in rows]
    ax.plot([x.get("delta_t_openmeteo_minus_dwd_c") for x in rows], p, marker="o", label="ΔT (°C)")
    ax.plot([x.get("delta_td_openmeteo_minus_dwd_c") for x in rows], p, marker="o", label="ΔTd (°C)")
    ax.plot([x.get("delta_wind_openmeteo_minus_dwd_ms") for x in rows], p, marker="o", label="Δwind (m/s)")
    ax.axvline(0, linewidth=1)
    ax.set_yscale("log")
    ax.invert_yaxis()
    ax.set_ylim(1000, 180)
    ax.set_yticks([925, 850, 700, 500, 300, 200])
    ax.get_yaxis().set_major_formatter(plt.ScalarFormatter())
    ax.set_xlabel("Open-Meteo − DWD native")
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8)
    ax.set_title("Standard-level differences")

    fig.suptitle(
        f"LJLM ICON-D2 source test | run {om['run_time']} | valid {om['valid_time']} | +{om['lead_hours']} h",
        fontsize=12,
    )
    fig.savefig(path, dpi=150)
    plt.close(fig)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare Open-Meteo and DWD-native ICON-D2 pseudo-sounding sources")
    p.add_argument("--obs", default="data/latest.json", help="Observed LJLM latest/archive JSON")
    p.add_argument("--lead", type=int, default=0, help="Forecast lead in hours; default 0")
    p.add_argument("--run", default=None, help="Override model run, e.g. 2026-09-28T00:00")
    p.add_argument("--valid", default=None, help="Override valid time, e.g. 2026-09-28T00:00")
    p.add_argument("--top-hpa", type=float, default=150.0, help="DWD native top pressure to retain; default 150 hPa")
    p.add_argument("--workers", type=int, default=6, help="Concurrent DWD downloads; default 6")
    p.add_argument("--output-dir", default="test/icon_d2_sources")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    obs_path = Path(args.obs)
    if not obs_path.exists():
        print(f"ERROR: observed sounding not found: {obs_path}", file=sys.stderr)
        return 2

    obs = json.loads(obs_path.read_text(encoding="utf-8"))
    latitude = float(obs.get("latitude"))
    longitude = float(obs.get("longitude"))

    valid_time = parse_utc(args.valid) if args.valid else derive_valid_time(obs)
    run = parse_utc(args.run) if args.run else derive_run(valid_time, args.lead)
    lead = int((valid_time - run).total_seconds() / 3600)

    print("=== LJLM ICON-D2 SOURCE TEST ===")
    print(f"OBS:      {obs_path}")
    print(f"Point:    {latitude:.5f}, {longitude:.5f}")
    print(f"Run:      {utc_iso(run)}")
    print(f"Valid:    {utc_iso(valid_time)}")
    print(f"Lead:     +{lead} h")
    print(f"DWD top:  {args.top_hpa:.0f} hPa")
    print()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        print("[1/3] Open-Meteo ICON-D2 pressure-level profile ...")
        om = fetch_openmeteo_profile(latitude, longitude, run, valid_time)
        save_json(out / "openmeteo_profile.json", om)
        print(f"      {om['pressure_levels_count']} levels, {om['fetch_seconds']} s")

        print("[2/3] DWD Open Data native ICON-D2 model-level profile ...")
        dwd = fetch_dwd_native_profile(
            latitude, longitude, run, valid_time,
            top_hpa=args.top_hpa,
            workers=args.workers,
        )
        save_json(out / "dwd_native_profile.json", dwd)
        print(
            f"      {dwd['native_levels_count']} native levels, "
            f"{dwd['compressed_download_mb']} MB, {dwd['fetch_seconds']} s"
        )

        print("[3/3] Comparison ...")
        comp = compare_sources(om, dwd)
        save_json(out / "source_comparison.json", comp)
        save_comparison_csv(out / "source_comparison.csv", comp["standard_levels"])
        make_plot(out / "source_comparison.png", om, dwd, comp)

        print()
        print("=== SUMMARY ===")
        vr = comp["vertical_resolution"]
        print(f"Open-Meteo levels: {vr['openmeteo_pressure_levels']}")
        print(f"DWD native levels: {vr['dwd_native_levels_to_test_top']}")
        print("RMSE Open-Meteo vs DWD-native at standard levels:")
        for k, v in comp["rmse_standard_levels"].items():
            print(f"  {k:24s}: {v}")
        print("Synoptic:")
        for k, v in comp["synoptic"].items():
            print(f"  {k:36s}: {v}")
        print()
        print(f"Outputs: {out}")
        return 0

    except (HTTPError, URLError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
