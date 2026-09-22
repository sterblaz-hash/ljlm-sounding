#!/usr/bin/env python3
"""
Diagnostic inversion detector for the newest Ljubljana (LJLM) sounding.

Reads:
    data/latest.json

Does NOT modify the archive. It prints candidate temperature inversions
so thresholds can be meteorologically validated before integration into
extract_ljlm.py.
"""

import json
import math
from pathlib import Path

import numpy as np

INPUT_FILE = Path("data/latest.json")

# Diagnostic settings: intentionally permissive.
GRID_STEP_M = 10.0
SMOOTH_WINDOW_M = 50.0
MAX_HEIGHT_AGL_M = 12000.0
MIN_LAYER_DEPTH_M = 20.0
MIN_DELTA_T_C = 0.10
MERGE_GAP_M = 40.0
MERGE_COOLING_C = 0.15

RD = 287.05
CP = 1004.0
KAPPA = RD / CP


def valid_number(value):
    try:
        return (
            value is not None
            and math.isfinite(float(value))
            and abs(float(value)) < 1e20
        )
    except Exception:
        return False


def moving_average(values, window_points):
    if window_points <= 1:
        return values.copy()

    if window_points % 2 == 0:
        window_points += 1

    half = window_points // 2
    padded = np.pad(values, (half, half), mode="edge")
    kernel = np.ones(window_points, dtype=float) / window_points
    return np.convolve(padded, kernel, mode="valid")


def interpolate_profile(levels):
    rows = []

    for level in levels:
        z = level.get("height_m")
        p = level.get("pressure_hpa")
        t = level.get("temperature_c")

        if not all(valid_number(x) for x in (z, p, t)):
            continue

        rows.append((float(z), float(p), float(t)))

    rows.sort(key=lambda x: x[0])

    # Remove duplicate heights by retaining the first occurrence.
    clean = []
    seen = set()

    for row in rows:
        key = round(row[0], 1)
        if key in seen:
            continue
        seen.add(key)
        clean.append(row)

    if len(clean) < 10:
        raise RuntimeError("Too few valid T/p/z levels.")

    z = np.array([x[0] for x in clean], dtype=float)
    p = np.array([x[1] for x in clean], dtype=float)
    t = np.array([x[2] for x in clean], dtype=float)

    station_z = z[0]
    max_z = min(z[-1], station_z + MAX_HEIGHT_AGL_M)

    grid_z = np.arange(
        station_z,
        max_z + GRID_STEP_M,
        GRID_STEP_M
    )

    grid_t = np.interp(grid_z, z, t)

    # Pressure interpolation in log(p).
    grid_logp = np.interp(
        grid_z,
        z,
        np.log(p)
    )
    grid_p = np.exp(grid_logp)

    window_points = max(
        1,
        round(SMOOTH_WINDOW_M / GRID_STEP_M)
    )

    smooth_t = moving_average(
        grid_t,
        window_points
    )

    theta = (
        (smooth_t + 273.15)
        * (1000.0 / grid_p) ** KAPPA
    )

    return (
        station_z,
        grid_z,
        grid_p,
        smooth_t,
        theta
    )


def raw_inversion_segments(grid_z, smooth_t):
    dt = np.diff(smooth_t)

    segments = []
    start = None

    for i, delta in enumerate(dt):
        if delta > 0:
            if start is None:
                start = i
        else:
            if start is not None:
                segments.append([start, i])
                start = None

    if start is not None:
        segments.append([start, len(grid_z) - 1])

    return segments


def segment_metrics(segment, station_z, z, p, t, theta):
    i0, i1 = segment

    base_z = float(z[i0])
    top_z = float(z[i1])

    depth = top_z - base_z
    delta_t = float(t[i1] - t[i0])
    delta_theta = float(theta[i1] - theta[i0])

    return {
        "i0": i0,
        "i1": i1,
        "base_msl_m": base_z,
        "top_msl_m": top_z,
        "base_agl_m": base_z - station_z,
        "top_agl_m": top_z - station_z,
        "base_pressure_hpa": float(p[i0]),
        "top_pressure_hpa": float(p[i1]),
        "depth_m": depth,
        "base_temp_c": float(t[i0]),
        "top_temp_c": float(t[i1]),
        "delta_t_c": delta_t,
        "gradient_c_per_km": (
            1000.0 * delta_t / depth
            if depth > 0 else None
        ),
        "base_theta_k": float(theta[i0]),
        "top_theta_k": float(theta[i1]),
        "delta_theta_k": delta_theta,
    }


def merge_segments(segments, station_z, z, p, t, theta):
    if not segments:
        return []

    merged = [segments[0][:]]

    for current in segments[1:]:
        previous = merged[-1]

        prev_end = previous[1]
        cur_start = current[0]

        gap_depth = z[cur_start] - z[prev_end]
        gap_cooling = t[prev_end] - t[cur_start]

        if (
            gap_depth <= MERGE_GAP_M
            and gap_cooling <= MERGE_COOLING_C
        ):
            previous[1] = current[1]
        else:
            merged.append(current[:])

    return merged


def classify_layer(layer):
    base_agl = layer["base_agl_m"]
    delta_t = layer["delta_t_c"]
    depth = layer["depth_m"]

    if base_agl <= 50:
        kind = "surface"
    else:
        kind = "elevated"

    # Diagnostic significance only; these are not yet final thresholds.
    if delta_t >= 2.0 and depth >= 100:
        significance = "strong_candidate"
    elif delta_t >= 0.5 and depth >= 50:
        significance = "clear_candidate"
    else:
        significance = "weak_candidate"

    return kind, significance


def main():
    if not INPUT_FILE.exists():
        raise SystemExit(
            "data/latest.json not found. Run the sounding update first."
        )

    with INPUT_FILE.open("r", encoding="utf-8") as f:
        sounding = json.load(f)

    levels = sounding.get("levels", [])

    (
        station_z,
        grid_z,
        grid_p,
        smooth_t,
        theta,
    ) = interpolate_profile(levels)

    segments = raw_inversion_segments(
        grid_z,
        smooth_t
    )

    segments = merge_segments(
        segments,
        station_z,
        grid_z,
        grid_p,
        smooth_t,
        theta
    )

    candidates = []

    for segment in segments:
        layer = segment_metrics(
            segment,
            station_z,
            grid_z,
            grid_p,
            smooth_t,
            theta
        )

        if layer["depth_m"] < MIN_LAYER_DEPTH_M:
            continue

        if layer["delta_t_c"] < MIN_DELTA_T_C:
            continue

        kind, significance = classify_layer(layer)
        layer["type"] = kind
        layer["diagnostic_class"] = significance
        candidates.append(layer)

    print()
    print("LJLM INVERSION DIAGNOSTIC")
    print("=" * 72)
    print("Launch:", sounding.get("launch_time"))
    print(
        "Nominal:",
        sounding.get("nominal_date"),
        sounding.get("term"),
        "UTC"
    )
    print(f"Station height used: {station_z:.0f} m MSL")
    print(
        f"Grid: {GRID_STEP_M:.0f} m | "
        f"T smoothing: ~{SMOOTH_WINDOW_M:.0f} m | "
        f"search top: {MAX_HEIGHT_AGL_M/1000:.1f} km AGL"
    )
    print(
        "Diagnostic thresholds:",
        f"depth >= {MIN_LAYER_DEPTH_M:.0f} m,",
        f"Delta T >= {MIN_DELTA_T_C:.2f} C"
    )
    print(
        "Merge:",
        f"gap <= {MERGE_GAP_M:.0f} m and",
        f"intervening cooling <= {MERGE_COOLING_C:.2f} C"
    )
    print("-" * 72)

    if not candidates:
        print("No inversion candidates found.")
        return

    for number, layer in enumerate(candidates, start=1):
        print()
        print(
            f"#{number:02d} "
            f"{layer['type'].upper()} | "
            f"{layer['diagnostic_class']}"
        )
        print(
            f"  base/top MSL: "
            f"{layer['base_msl_m']:.0f} / "
            f"{layer['top_msl_m']:.0f} m"
        )
        print(
            f"  base/top AGL: "
            f"{layer['base_agl_m']:.0f} / "
            f"{layer['top_agl_m']:.0f} m"
        )
        print(
            f"  pressure: "
            f"{layer['base_pressure_hpa']:.1f} -> "
            f"{layer['top_pressure_hpa']:.1f} hPa"
        )
        print(
            f"  depth: {layer['depth_m']:.0f} m"
        )
        print(
            f"  T: {layer['base_temp_c']:.2f} -> "
            f"{layer['top_temp_c']:.2f} C | "
            f"Delta T = +{layer['delta_t_c']:.2f} C"
        )
        print(
            f"  inversion gradient: "
            f"+{layer['gradient_c_per_km']:.2f} C/km"
        )
        print(
            f"  theta: {layer['base_theta_k']:.2f} -> "
            f"{layer['top_theta_k']:.2f} K | "
            f"Delta theta = +{layer['delta_theta_k']:.2f} K"
        )

    print()
    print("=" * 72)
    print("Candidate count:", len(candidates))
    print()
    print(
        "NOTE: This is a deliberately permissive diagnostic. "
        "Do not treat weak candidates as final inversion layers yet."
    )


if __name__ == "__main__":
    main()
