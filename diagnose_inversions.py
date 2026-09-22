#!/usr/bin/env python3
"""
LJLM inversion / stability diagnostic v2.

Reads data/latest.json only. Does not modify the sounding archive.

Goals:
- high-sensitivity inversion detection from surface to 700 hPa
- selective detection from 700 to 300 hPa
- theta and theta-e diagnostics
- candidate cap assessment in the lower troposphere
"""

import json
import math
from pathlib import Path

import numpy as np
import metpy.calc as mpcalc
from metpy.units import units

INPUT_FILE = Path("data/latest.json")

GRID_STEP_M = 10.0
SMOOTH_WINDOW_M = 50.0

# High-sensitivity lower-troposphere thresholds.
LOWER_BOUND_HPA = 700.0
LOWER_MIN_DEPTH_M = 20.0
LOWER_MIN_DELTA_T_C = 0.10

# More selective middle-troposphere thresholds.
UPPER_BOUND_HPA = 300.0
MID_MIN_DEPTH_M = 50.0
MID_MIN_DELTA_T_C = 0.50

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


def prepare_grid(levels):
    rows = []
    for level in levels:
        z = level.get("height_m")
        p = level.get("pressure_hpa")
        t = level.get("temperature_c")
        td = level.get("dewpoint_c")
        if not all(valid_number(x) for x in (z, p, t)):
            continue
        rows.append((
            float(z), float(p), float(t),
            float(td) if valid_number(td) else np.nan
        ))

    rows.sort(key=lambda x: x[0])

    clean = []
    seen = set()
    for row in rows:
        key = round(row[0], 1)
        if key in seen:
            continue
        seen.add(key)
        clean.append(row)

    if len(clean) < 10:
        raise RuntimeError("Too few valid p/T/z levels.")

    z = np.array([x[0] for x in clean])
    p = np.array([x[1] for x in clean])
    t = np.array([x[2] for x in clean])
    td = np.array([x[3] for x in clean])

    station_z = z[0]

    # Only diagnose classical tropospheric inversions down to 300 hPa.
    valid_300 = np.where(p >= UPPER_BOUND_HPA)[0]
    if len(valid_300) < 2:
        raise RuntimeError("Profile does not reach 300 hPa.")

    top_z = z[valid_300[-1]]

    grid_z = np.arange(station_z, top_z + GRID_STEP_M, GRID_STEP_M)
    grid_t = np.interp(grid_z, z, t)
    grid_logp = np.interp(grid_z, z, np.log(p))
    grid_p = np.exp(grid_logp)

    # Dewpoint: interpolate only through finite observations.
    finite_td = np.isfinite(td)
    if finite_td.sum() >= 2:
        grid_td = np.interp(
            grid_z, z[finite_td], td[finite_td]
        )
    else:
        grid_td = np.full_like(grid_z, np.nan)

    window_points = max(1, round(SMOOTH_WINDOW_M / GRID_STEP_M))
    smooth_t = moving_average(grid_t, window_points)
    smooth_td = moving_average(grid_td, window_points)

    theta = (
        (smooth_t + 273.15)
        * (1000.0 / grid_p) ** KAPPA
    )

    theta_e = np.full_like(theta, np.nan)
    for i in range(len(grid_z)):
        if not valid_number(smooth_td[i]):
            continue
        try:
            value = mpcalc.equivalent_potential_temperature(
                grid_p[i] * units.hPa,
                smooth_t[i] * units.degC,
                smooth_td[i] * units.degC,
            )
            theta_e[i] = float(value.to("kelvin").magnitude)
        except Exception:
            pass

    return station_z, grid_z, grid_p, smooth_t, smooth_td, theta, theta_e


def raw_segments(t):
    dt = np.diff(t)
    result = []
    start = None

    for i, delta in enumerate(dt):
        if delta > 0:
            if start is None:
                start = i
        elif start is not None:
            result.append([start, i])
            start = None

    if start is not None:
        result.append([start, len(t) - 1])

    return result


def merge_segments(segments, z, t):
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


def metrics(seg, station_z, z, p, t, td, theta, theta_e):
    i0, i1 = seg
    depth = float(z[i1] - z[i0])
    delta_t = float(t[i1] - t[i0])

    layer = {
        "base_msl_m": float(z[i0]),
        "top_msl_m": float(z[i1]),
        "base_agl_m": float(z[i0] - station_z),
        "top_agl_m": float(z[i1] - station_z),
        "base_pressure_hpa": float(p[i0]),
        "top_pressure_hpa": float(p[i1]),
        "depth_m": depth,
        "base_temp_c": float(t[i0]),
        "top_temp_c": float(t[i1]),
        "delta_t_c": delta_t,
        "gradient_c_per_km": (
            1000.0 * delta_t / depth if depth > 0 else None
        ),
        "base_theta_k": float(theta[i0]),
        "top_theta_k": float(theta[i1]),
        "delta_theta_k": float(theta[i1] - theta[i0]),
    }

    if np.isfinite(theta_e[i0]) and np.isfinite(theta_e[i1]):
        layer.update({
            "base_theta_e_k": float(theta_e[i0]),
            "top_theta_e_k": float(theta_e[i1]),
            "delta_theta_e_k": float(theta_e[i1] - theta_e[i0]),
        })

    # Moisture change across the layer is useful when evaluating a cap.
    if np.isfinite(td[i0]) and np.isfinite(td[i1]):
        layer["base_dewpoint_c"] = float(td[i0])
        layer["top_dewpoint_c"] = float(td[i1])
        layer["delta_dewpoint_c"] = float(td[i1] - td[i0])

    return layer


def keep_layer(layer):
    base_p = layer["base_pressure_hpa"]
    top_p = layer["top_pressure_hpa"]
    depth = layer["depth_m"]
    delta_t = layer["delta_t_c"]

    # Any inversion that begins at/above 700 hPa but still lies below 300 hPa:
    # selective mode.
    if base_p < LOWER_BOUND_HPA:
        if top_p < UPPER_BOUND_HPA:
            return False, "above_300"
        return (
            depth >= MID_MIN_DEPTH_M
            and delta_t >= MID_MIN_DELTA_T_C
        ), "700-300 hPa selective"

    # Surface through 700 hPa: high sensitivity.
    return (
        depth >= LOWER_MIN_DEPTH_M
        and delta_t >= LOWER_MIN_DELTA_T_C
    ), "surface-700 hPa high sensitivity"


def cap_score(layer):
    """
    Diagnostic, not a final physical definition of a cap.
    We flag lower-tropospheric elevated inversions that may inhibit
    parcel ascent. CIN remains the more direct parcel diagnostic.
    """
    if layer["base_agl_m"] <= 50:
        return "surface inversion"

    if layer["base_pressure_hpa"] < LOWER_BOUND_HPA:
        return "not lower-tropospheric cap candidate"

    dt = layer["delta_t_c"]
    dtheta = layer["delta_theta_k"]
    depth = layer["depth_m"]

    if dt >= 1.0 or dtheta >= 2.0:
        return "notable cap candidate"
    if dt >= 0.3 or dtheta >= 1.0:
        return "weak cap candidate"
    return "very weak cap candidate"


def main():
    if not INPUT_FILE.exists():
        raise SystemExit("data/latest.json not found.")

    with INPUT_FILE.open("r", encoding="utf-8") as f:
        sounding = json.load(f)

    (
        station_z, z, p, t, td, theta, theta_e
    ) = prepare_grid(sounding.get("levels", []))

    segments = merge_segments(raw_segments(t), z, t)

    kept = []
    rejected = 0

    for seg in segments:
        layer = metrics(
            seg, station_z, z, p, t, td, theta, theta_e
        )
        keep, regime = keep_layer(layer)
        if not keep:
            rejected += 1
            continue
        layer["regime"] = regime
        layer["cap_assessment"] = cap_score(layer)
        kept.append(layer)

    print()
    print("LJLM INVERSION / STABILITY DIAGNOSTIC v2")
    print("=" * 78)
    print("Launch:", sounding.get("launch_time"))
    print("Nominal:", sounding.get("nominal_date"), sounding.get("term"), "UTC")
    print(f"Station height: {station_z:.0f} m MSL")
    print(f"Grid {GRID_STEP_M:.0f} m | T/Td smoothing ~{SMOOTH_WINDOW_M:.0f} m")
    print(
        "Surface-700 hPa:",
        f"depth >= {LOWER_MIN_DEPTH_M:.0f} m,",
        f"Delta T >= {LOWER_MIN_DELTA_T_C:.2f} C"
    )
    print(
        "700-300 hPa:",
        f"depth >= {MID_MIN_DEPTH_M:.0f} m,",
        f"Delta T >= {MID_MIN_DELTA_T_C:.2f} C"
    )
    print("-" * 78)

    for n, layer in enumerate(kept, 1):
        print()
        print(
            f"#{n:02d} | {layer['regime']} | "
            f"{layer['cap_assessment']}"
        )
        print(
            f"  {layer['base_pressure_hpa']:.1f} -> "
            f"{layer['top_pressure_hpa']:.1f} hPa | "
            f"{layer['base_agl_m']:.0f} -> "
            f"{layer['top_agl_m']:.0f} m AGL"
        )
        print(
            f"  depth {layer['depth_m']:.0f} m | "
            f"T {layer['base_temp_c']:.2f} -> "
            f"{layer['top_temp_c']:.2f} C | "
            f"Delta T +{layer['delta_t_c']:.2f} C | "
            f"+{layer['gradient_c_per_km']:.2f} C/km"
        )
        print(
            f"  theta {layer['base_theta_k']:.2f} -> "
            f"{layer['top_theta_k']:.2f} K | "
            f"Delta theta +{layer['delta_theta_k']:.2f} K"
        )

        if "delta_theta_e_k" in layer:
            sign = "+" if layer["delta_theta_e_k"] >= 0 else ""
            print(
                f"  theta-e {layer['base_theta_e_k']:.2f} -> "
                f"{layer['top_theta_e_k']:.2f} K | "
                f"Delta theta-e {sign}"
                f"{layer['delta_theta_e_k']:.2f} K"
            )

        if "delta_dewpoint_c" in layer:
            sign = "+" if layer["delta_dewpoint_c"] >= 0 else ""
            print(
                f"  Td {layer['base_dewpoint_c']:.2f} -> "
                f"{layer['top_dewpoint_c']:.2f} C | "
                f"Delta Td {sign}{layer['delta_dewpoint_c']:.2f} C"
            )

    print()
    print("=" * 78)
    print("Retained inversion layers:", len(kept))
    print("Rejected weak/upper candidates:", rejected)
    print()
    print(
        "CAP labels are diagnostic only. They should be interpreted "
        "together with CIN, parcel origin and the full T/Td/theta-e profile."
    )


if __name__ == "__main__":
    main()
