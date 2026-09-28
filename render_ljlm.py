#!/usr/bin/env python3
"""Render improved static LJLM sounding graphics from archived JSON.

Phase 1 renderer
----------------
Creates a cleaner operational set of sounding graphics:
- operational Skew-T (surface to ~200 hPa)
- low-level T/Td profile (surface to 700 hPa)
- theta / theta-e profile (0–8 km AGL)
- hodograph (0–12 km AGL)

It also supports backfilling archived JSON soundings without re-downloading
raw BUFR data.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

import metpy.calc as mpcalc
from metpy.plots import Hodograph, SkewT
from metpy.units import units

# -----------------------------------------------------------------------------
# STYLE
# -----------------------------------------------------------------------------

BG = "#FFFFFF"
INK = "#10223F"
MUTED = "#66758C"
GRID = "#DCE5EF"
GRID_MAJOR = "#B9C8D9"
TEMP = "#D83A3A"
DEW = "#168A45"
THETA = "#B653C6"
THETAE = "#1769E0"
PARCEL = "#1769E0"
INV_FILL = "#F5C469"
CAPE_FILL = "#F6B36B"
CIN_FILL = "#A7C8FF"
BOUNDARY_FILL = "#EAF2FF"
RM_COLOR = "#845EC2"
LM_COLOR = "#C34A36"
MEAN_COLOR = "#334E68"

HODO_SEGMENTS = (
    (0, 1, "#1769E0", "0–1 km"),
    (1, 3, "#20A44B", "1–3 km"),
    (3, 6, "#F59E0B", "3–6 km"),
    (6, 12, "#E63946", "6–12 km"),
)

STANDARD_PRESSURES = (1000, 925, 850, 700, 500, 300, 250, 200, 150, 100)
BARB_PRESSURES = (
    1000, 950, 925, 900, 850, 800, 750, 700, 650, 600,
    550, 500, 450, 400, 350, 300, 250, 200,
)
LOWLEVEL_PRESSURES = (1000, 950, 925, 900, 850, 800, 750, 700)

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 9.5,
    "axes.titlesize": 14,
    "axes.titleweight": "semibold",
    "axes.labelsize": 10,
    "axes.labelcolor": INK,
    "axes.edgecolor": "#91A4B8",
    "xtick.color": "#38506B",
    "ytick.color": "#38506B",
    "text.color": INK,
    "figure.facecolor": BG,
    "savefig.facecolor": BG,
})


# -----------------------------------------------------------------------------
# DATA HELPERS
# -----------------------------------------------------------------------------

@dataclass
class Profile:
    p_hpa: np.ndarray
    z_m: np.ndarray
    t_c: np.ndarray
    td_c: np.ndarray
    ws_ms: np.ndarray
    wd_deg: np.ndarray


def _finite(value) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _utc_text(value: str | None, fmt: str = "%d %b %Y %H:%M UTC") -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime(fmt)
    except Exception:
        return value


def _nominal_title(data: dict) -> str:
    date_text = data.get("nominal_date", "")
    term = str(data.get("term", "")).upper()
    try:
        dt = datetime.strptime(date_text, "%Y-%m-%d")
        date_text = dt.strftime("%d %b %Y")
    except Exception:
        pass
    return f"{date_text} · {term} UTC".strip(" ·")


def _clean_profile(levels: Sequence[dict]) -> Profile:
    rows = []
    for row in levels:
        p = row.get("pressure_hpa")
        z = row.get("height_m")
        t = row.get("temperature_c")
        td = row.get("dewpoint_c")
        ws = row.get("wind_speed_ms")
        wd = row.get("wind_direction_deg")

        if not (_finite(p) and _finite(z) and _finite(t)):
            continue
        p = float(p)
        if not 10 <= p <= 1100:
            continue

        rows.append((
            p,
            float(z),
            float(t),
            float(td) if _finite(td) else np.nan,
            float(ws) if _finite(ws) else np.nan,
            float(wd) if _finite(wd) else np.nan,
        ))

    if len(rows) < 10:
        raise ValueError("Profile contains too few valid levels for rendering.")

    rows.sort(key=lambda r: r[0], reverse=True)
    deduped = []
    last_p = None
    for row in rows:
        p = row[0]
        if last_p is not None and abs(p - last_p) < 0.05:
            continue
        deduped.append(row)
        last_p = p

    arr = np.asarray(deduped, dtype=float)
    return Profile(
        p_hpa=arr[:, 0],
        z_m=arr[:, 1],
        t_c=arr[:, 2],
        td_c=arr[:, 3],
        ws_ms=arr[:, 4],
        wd_deg=arr[:, 5],
    )


def _interp_logp(p: np.ndarray, values: np.ndarray, targets: Iterable[float]) -> np.ndarray:
    targets = np.asarray(tuple(targets), dtype=float)
    mask = np.isfinite(p) & np.isfinite(values) & (p > 0)
    if mask.sum() < 2:
        return np.full(len(targets), np.nan)

    p0 = p[mask]
    v0 = values[mask]
    order = np.argsort(p0)
    p0 = p0[order]
    v0 = v0[order]

    result = np.interp(np.log(targets), np.log(p0), v0, left=np.nan, right=np.nan)
    in_range = (targets >= np.nanmin(p0)) & (targets <= np.nanmax(p0))
    result[~in_range] = np.nan
    return result


def _wind_components(profile: Profile):
    mask = np.isfinite(profile.ws_ms) & np.isfinite(profile.wd_deg)
    u = np.full_like(profile.ws_ms, np.nan, dtype=float)
    v = np.full_like(profile.ws_ms, np.nan, dtype=float)
    if mask.any():
        uq, vq = mpcalc.wind_components(
            profile.ws_ms[mask] * units("m/s"),
            profile.wd_deg[mask] * units.degree,
        )
        u[mask] = uq.to("m/s").magnitude
        v[mask] = vq.to("m/s").magnitude
    return u, v


def _marker_uv(item: dict | None, unit="knots"):
    if not item:
        return None
    if _finite(item.get("u_ms")) and _finite(item.get("v_ms")):
        u = float(item["u_ms"]) * units("m/s")
        v = float(item["v_ms"]) * units("m/s")
        return float(u.to(unit).magnitude), float(v.to(unit).magnitude)
    if _finite(item.get("speed_ms")) and _finite(item.get("direction_deg")):
        u, v = mpcalc.wind_components(
            float(item["speed_ms"]) * units("m/s"),
            float(item["direction_deg"]) * units.degree,
        )
        return float(u.to(unit).magnitude), float(v.to(unit).magnitude)
    return None


def _archive_paths(data: dict, diagnostics_root: Path):
    nominal = data.get("nominal_date")
    term = str(data.get("term", "unknown"))
    try:
        dt = datetime.strptime(nominal, "%Y-%m-%d")
        archive_dir = diagnostics_root / f"{dt.year:04d}" / f"{dt.month:02d}"
        stem = f"{dt:%Y%m%d}_{term}"
    except Exception:
        archive_dir = diagnostics_root / "unknown"
        stem = f"unknown_{term}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    return {
        "skewt": archive_dir / f"{stem}_skewt.png",
        "lowlevel": archive_dir / f"{stem}_lowlevel.png",
        "skewt_zoom": archive_dir / f"{stem}_skewt_zoom.png",
        "thetae": archive_dir / f"{stem}_thetae.png",
        "hodograph": archive_dir / f"{stem}_hodograph.png",
    }


def _latest_paths(diagnostics_root: Path):
    return {
        "skewt": diagnostics_root / "latest_skewt.png",
        "lowlevel": diagnostics_root / "latest_lowlevel.png",
        "skewt_zoom": diagnostics_root / "latest_skewt_zoom.png",
        "thetae": diagnostics_root / "latest_thetae.png",
        "hodograph": diagnostics_root / "latest_hodograph.png",
    }


def _plot_pressure_bounds(profile: Profile):
    surface_p = float(np.nanmax(profile.p_hpa))
    p_bottom = min(1050.0, math.ceil(surface_p / 10.0) * 10.0)
    min_p = float(np.nanmin(profile.p_hpa))
    p_top = max(200.0, math.floor(min_p / 25.0) * 25.0)
    return surface_p, p_bottom, p_top


def _pressure_to_agl_km(profile: Profile, pressures: Iterable[float]) -> np.ndarray:
    pressures = np.asarray(tuple(pressures), dtype=float)
    z = _interp_logp(profile.p_hpa, profile.z_m, pressures)
    surface = np.nanmin(profile.z_m)
    return (z - surface) / 1000.0


def _safe_text(value, fmt="{:.0f}", default="—"):
    return fmt.format(value) if _finite(value) else default


def _theta_profiles(profile: Profile):
    mask = np.isfinite(profile.p_hpa) & np.isfinite(profile.t_c) & np.isfinite(profile.td_c)
    if mask.sum() < 4:
        return None
    p = profile.p_hpa[mask] * units.hPa
    t = profile.t_c[mask] * units.degC
    td = profile.td_c[mask] * units.degC
    z = profile.z_m[mask]
    theta = mpcalc.potential_temperature(p, t).to("kelvin").magnitude
    thetae = mpcalc.equivalent_potential_temperature(p, t, td).to("kelvin").magnitude
    order = np.argsort(z)
    return z[order], theta[order], thetae[order]


def _metpy_parameters(data: dict) -> dict:
    return data.get("parameters", {}).get("metpy", {}) or {}


def _inversion_layers(data: dict) -> list:
    inv = data.get("parameters", {}).get("inversions", {}) or {}
    if inv.get("available"):
        return inv.get("layers", []) or []
    return []


# -----------------------------------------------------------------------------
# RENDERERS
# -----------------------------------------------------------------------------

def render_skewt(data: dict, output: Path):
    profile = _clean_profile(data.get("levels", []))
    surface_p, p_bottom, p_top = _plot_pressure_bounds(profile)

    visible = (profile.p_hpa >= p_top) & (profile.p_hpa <= p_bottom)
    p = profile.p_hpa[visible] * units.hPa
    t = profile.t_c[visible] * units.degC
    td_values = profile.td_c[visible]
    td = td_values * units.degC

    fig = plt.figure(figsize=(7.8, 7.4), dpi=170)
    fig.subplots_adjust(left=0.08, right=0.84, top=0.89, bottom=0.09)
    skew = SkewT(fig, rotation=45)
    ax = skew.ax
    ax.set_facecolor(BG)

    skew.plot_dry_adiabats(alpha=0.28, linewidth=0.55, color="#C7A69A")
    skew.plot_moist_adiabats(alpha=0.28, linewidth=0.55, color="#7DB6D8")
    skew.plot_mixing_lines(alpha=0.28, linewidth=0.5, color="#86BE9A")

    # Subtle inversion shading.
    for layer in _inversion_layers(data):
        bp = layer.get("base_pressure_hpa")
        tp = layer.get("top_pressure_hpa")
        if _finite(bp) and _finite(tp):
            bp, tp = float(bp), float(tp)
            if tp <= p_bottom and bp >= p_top:
                ax.axhspan(tp, bp, facecolor=INV_FILL, edgecolor="none", alpha=0.07, zorder=0)

    # Main profiles.
    skew.plot(p, t, color=TEMP, linewidth=2.25, label="Temperature")
    td_mask = np.isfinite(td_values)
    if td_mask.any():
        skew.plot(p[td_mask], td[td_mask], color=DEW, linewidth=2.05, label="Dew point")

    # Most-unstable parcel and CAPE/CIN shading.
    try:
        parcel_mask = np.isfinite(profile.p_hpa) & np.isfinite(profile.t_c) & np.isfinite(profile.td_c)
        if parcel_mask.sum() >= 4:
            p_all = profile.p_hpa[parcel_mask] * units.hPa
            t_all = profile.t_c[parcel_mask] * units.degC
            td_all = profile.td_c[parcel_mask] * units.degC
            mu_p, mu_t, mu_td = mpcalc.most_unstable_parcel(p_all, t_all, td_all)
            parcel = mpcalc.parcel_profile(p_all, mu_t, mu_td).to("degC")
            keep = (p_all.magnitude >= p_top) & (p_all.magnitude <= p_bottom)
            skew.plot(p_all[keep], parcel[keep], color=PARCEL, linewidth=1.35, linestyle="--", alpha=0.9, label="MU parcel")
            try:
                skew.shade_cape(p_all[keep], t_all[keep], parcel[keep], alpha=0.14, color=CAPE_FILL)
                skew.shade_cin(p_all[keep], t_all[keep], parcel[keep], td_all[keep], alpha=0.10, color=CIN_FILL)
            except Exception:
                pass
    except Exception as exc:
        print("Skew-T parcel warning:", exc)

    # Standard pressure guides.
    for level in (925, 850, 700, 500, 300, 200):
        if p_top <= level <= p_bottom:
            ax.axhline(level, color=GRID_MAJOR, lw=0.75, ls=(0, (4, 4)), zorder=0)

    # Wind barbs.
    u_ms, v_ms = _wind_components(profile)
    barb_targets = [x for x in BARB_PRESSURES if p_top <= x <= p_bottom]
    ui = _interp_logp(profile.p_hpa, u_ms, barb_targets)
    vi = _interp_logp(profile.p_hpa, v_ms, barb_targets)
    good = np.isfinite(ui) & np.isfinite(vi)
    if good.any():
        skew.plot_barbs(
            np.asarray(barb_targets)[good] * units.hPa,
            (ui[good] * units("m/s")).to("knots"),
            (vi[good] * units("m/s")).to("knots"),
            xloc=1.045,
            sizes={"emptybarb": 0.075, "spacing": 0.18, "height": 0.34},
            linewidth=0.75,
            color=INK,
        )

    # Height labels beside key pressure levels.
    height_targets = [x for x in (925, 850, 700, 500, 300, 200) if p_top <= x <= p_bottom]
    heights_agl = _pressure_to_agl_km(profile, height_targets)
    for lev, hgt in zip(height_targets, heights_agl):
        if _finite(hgt):
            ax.text(1.105, lev, f"{hgt:.1f} km", transform=ax.get_yaxis_transform(), ha="left", va="center", fontsize=8.3, color=MUTED)

    temp_candidates = np.concatenate([
        profile.t_c[visible][np.isfinite(profile.t_c[visible])],
        profile.td_c[visible][np.isfinite(profile.td_c[visible])],
    ])
    if temp_candidates.size:
        xmin = math.floor((np.nanmin(temp_candidates) - 8) / 5.0) * 5.0
        xmax = math.ceil((np.nanmax(temp_candidates) + 10) / 5.0) * 5.0
    else:
        xmin, xmax = -40, 35

    ax.set_ylim(p_bottom, p_top)
    ax.set_xlim(xmin, xmax)
    ax.set_xlabel("Temperature (°C)")
    ax.set_ylabel("Pressure (hPa)")
    ax.grid(True, which="major", color=GRID, linewidth=0.55, alpha=0.9)

    station = data.get("station_name", "Ljubljana")
    station_id = data.get("station", 14015)
    nominal = _nominal_title(data)
    launch = _utc_text(data.get("launch_time"), "%H:%M UTC")

    ax.set_title("Operational Skew-T", loc="left", pad=18)
    ax.text(0.0, 1.012, f"{station} ({station_id})  ·  {nominal}  ·  launch {launch}", transform=ax.transAxes,
            ha="left", va="bottom", fontsize=9.5, color=MUTED)
    ax.text(1.025, 1.012, "Wind", transform=ax.transAxes, ha="left", va="bottom", fontsize=9, color=MUTED)
    ax.text(1.105, 1.012, "Height AGL", transform=ax.transAxes, ha="left", va="bottom", fontsize=9, color=MUTED)

    metpy_data = _metpy_parameters(data)
    summary_lines = []
    if _finite(metpy_data.get("mucape_jkg")):
        summary_lines.append(f"MUCAPE {_safe_text(metpy_data.get('mucape_jkg'), '{:.0f}')} J/kg")
    if _finite(metpy_data.get("mucin_jkg")):
        summary_lines.append(f"MUCIN {_safe_text(metpy_data.get('mucin_jkg'), '{:.0f}')} J/kg")
    if _finite(metpy_data.get("freezing_level_msl_m")):
        summary_lines.append(f"0 °C {_safe_text(metpy_data.get('freezing_level_msl_m'), '{:.0f}')} m MSL")
    if _finite(metpy_data.get("pwat_mm")):
        summary_lines.append(f"PWAT {_safe_text(metpy_data.get('pwat_mm'), '{:.1f}')} mm")
    if summary_lines:
        ax.text(0.99, 0.98, "\n".join(summary_lines), transform=ax.transAxes, ha="right", va="top",
                fontsize=8.7, color=INK,
                bbox=dict(facecolor="#F8FAFD", edgecolor=GRID, boxstyle="round,pad=0.35", alpha=0.92))

    legend = ax.legend(loc="upper center", bbox_to_anchor=(0.47, 1.07), frameon=False,
                       ncol=3, fontsize=8.8, handlelength=2.7, columnspacing=1.2)
    for text in legend.get_texts():
        text.set_color(MUTED)

    fig.savefig(output, bbox_inches="tight", pad_inches=0.12)
    plt.close(fig)


def render_lowlevel(data: dict, output: Path):
    profile = _clean_profile(data.get("levels", []))
    surface_p, p_bottom, _ = _plot_pressure_bounds(profile)
    p_top = max(700.0, math.floor(float(np.nanmin(profile.p_hpa)) / 25.0) * 25.0)

    visible = (profile.p_hpa >= p_top) & (profile.p_hpa <= p_bottom)
    p = profile.p_hpa[visible]
    t = profile.t_c[visible]
    td = profile.td_c[visible]

    fig, ax = plt.subplots(figsize=(7.7, 7.1), dpi=170)
    fig.subplots_adjust(left=0.10, right=0.78, top=0.88, bottom=0.09)
    ax.set_facecolor(BG)

    # subtle background highlighting for inversion layers
    for layer in _inversion_layers(data):
        bp = layer.get("base_pressure_hpa")
        tp = layer.get("top_pressure_hpa")
        if _finite(bp) and _finite(tp):
            bp, tp = float(bp), float(tp)
            if tp <= p_bottom and bp >= p_top:
                ax.axhspan(tp, bp, facecolor=INV_FILL, edgecolor="none", alpha=0.07, zorder=0)

    ax.plot(t, p, color=TEMP, linewidth=2.35, label="Temperature")
    td_mask = np.isfinite(td)
    if td_mask.any():
        ax.plot(td[td_mask], p[td_mask], color=DEW, linewidth=2.15, label="Dew point")

    pressure_ticks = [lev for lev in LOWLEVEL_PRESSURES if p_top <= lev <= p_bottom]
    for lev in pressure_ticks:
        ax.axhline(lev, color=GRID_MAJOR, lw=0.75, ls=(0, (4, 4)), zorder=0)

    vals = np.concatenate([t[np.isfinite(t)], td[np.isfinite(td)]]) if td_mask.any() else t[np.isfinite(t)]
    xmin = math.floor((np.nanmin(vals) - 3) / 2.5) * 2.5
    xmax = math.ceil((np.nanmax(vals) + 3) / 2.5) * 2.5
    xspan = xmax - xmin

    # Right-side wind column.
    u_ms, v_ms = _wind_components(profile)
    wind_targets = pressure_ticks
    ui = _interp_logp(profile.p_hpa, u_ms, wind_targets)
    vi = _interp_logp(profile.p_hpa, v_ms, wind_targets)
    x_barb = xmax + 0.12 * xspan
    good = np.isfinite(ui) & np.isfinite(vi)
    if good.any():
        ax.barbs(np.full(np.sum(good), x_barb), np.asarray(wind_targets)[good],
                 (ui[good] * units("m/s")).to("knots").magnitude,
                 (vi[good] * units("m/s")).to("knots").magnitude,
                 length=5.5, linewidth=0.75, color=INK, clip_on=False)

    # Height text column.
    heights_agl = _pressure_to_agl_km(profile, pressure_ticks)
    x_height = xmax + 0.30 * xspan
    for lev, hgt in zip(pressure_ticks, heights_agl):
        if _finite(hgt):
            ax.text(x_height, lev, f"{hgt:.2f} km", ha="left", va="center", fontsize=8.2, color=MUTED, clip_on=False)

    ax.set_ylim(p_bottom, p_top)
    ax.set_xlim(xmin, xmax + 0.55 * xspan)
    ax.set_xlabel("Temperature / dew point (°C)")
    ax.set_ylabel("Pressure (hPa)")
    ax.set_yticks(pressure_ticks)
    ax.grid(True, axis="x", color=GRID, linewidth=0.55, alpha=0.9)

    station = data.get("station_name", "Ljubljana")
    station_id = data.get("station", 14015)
    nominal = _nominal_title(data)
    ax.set_title("Low-level thermodynamic profile · surface–700 hPa", loc="left", pad=18)
    ax.text(0.0, 1.012, f"{station} ({station_id})  ·  {nominal}", transform=ax.transAxes,
            ha="left", va="bottom", fontsize=9.5, color=MUTED)
    ax.text(0.83, 1.012, "Wind", transform=ax.transAxes, ha="left", va="bottom", fontsize=9, color=MUTED)
    ax.text(0.93, 1.012, "Height AGL", transform=ax.transAxes, ha="left", va="bottom", fontsize=9, color=MUTED)

    legend = ax.legend(loc="upper left", frameon=False, fontsize=8.8)
    for text in legend.get_texts():
        text.set_color(MUTED)

    fig.savefig(output, bbox_inches="tight", pad_inches=0.12)
    plt.close(fig)


def render_thetae(data: dict, output: Path):
    profile = _clean_profile(data.get("levels", []))
    theta_pack = _theta_profiles(profile)
    if theta_pack is None:
        raise ValueError("Not enough valid levels for theta/theta-e profile.")
    z_m, theta, thetae = theta_pack
    surface = np.nanmin(z_m)
    z_agl_km = (z_m - surface) / 1000.0
    mask = np.isfinite(z_agl_km) & np.isfinite(theta) & np.isfinite(thetae) & (z_agl_km >= 0) & (z_agl_km <= 8)
    if mask.sum() < 4:
        raise ValueError("Not enough valid levels below 8 km for theta/theta-e plot.")

    z = z_agl_km[mask]
    th = theta[mask]
    the = thetae[mask]
    order = np.argsort(z)
    z, th, the = z[order], th[order], the[order]

    fig, ax = plt.subplots(figsize=(7.2, 6.8), dpi=170)
    fig.subplots_adjust(left=0.11, right=0.96, top=0.88, bottom=0.10)
    ax.set_facecolor(BG)

    # Highlight layers where theta-e decreases with height.
    if len(z) >= 3:
        grad = np.gradient(the, z)
        unstable = grad < 0
        start = None
        for i, flag in enumerate(unstable):
            if flag and start is None:
                start = i
            if start is not None and (not flag or i == len(unstable) - 1):
                end = i if not flag else i
                z0 = z[max(0, start)]
                z1 = z[min(len(z) - 1, end)]
                if z1 > z0:
                    ax.axhspan(z0, z1, facecolor=BOUNDARY_FILL, edgecolor="none", alpha=0.5, zorder=0)
                start = None

    ax.plot(th, z, color=THETA, linewidth=2.2, label="θ")
    ax.plot(the, z, color=THETAE, linewidth=2.2, label="θe")

    for y in range(1, 9):
        ax.axhline(y, color=GRID_MAJOR if y in (2, 4, 6, 8) else GRID, lw=0.6, ls=(0, (4, 4)), zorder=0)

    xmin = math.floor((min(np.nanmin(th), np.nanmin(the)) - 2) / 5.0) * 5.0
    xmax = math.ceil((max(np.nanmax(th), np.nanmax(the)) + 2) / 5.0) * 5.0
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(0, 8)
    ax.set_xlabel("Potential temperature (K)")
    ax.set_ylabel("Height AGL (km)")
    ax.grid(True, axis="x", color=GRID, linewidth=0.55, alpha=0.9)

    station = data.get("station_name", "Ljubljana")
    station_id = data.get("station", 14015)
    nominal = _nominal_title(data)
    ax.set_title("θ / θe profile · 0–8 km AGL", loc="left", pad=18)
    ax.text(0.0, 1.012, f"{station} ({station_id})  ·  {nominal}", transform=ax.transAxes,
            ha="left", va="bottom", fontsize=9.5, color=MUTED)
    ax.text(0.99, 0.02, "Blue shading: layers with decreasing θe", transform=ax.transAxes,
            ha="right", va="bottom", fontsize=8.1, color=MUTED)

    legend = ax.legend(loc="upper left", frameon=False, fontsize=8.8)
    for text in legend.get_texts():
        text.set_color(MUTED)

    fig.savefig(output, bbox_inches="tight", pad_inches=0.12)
    plt.close(fig)


def render_hodograph(data: dict, output: Path):
    profile = _clean_profile(data.get("levels", []))
    u_ms, v_ms = _wind_components(profile)

    surface_height = float(np.nanmin(profile.z_m))
    z_agl_km = (profile.z_m - surface_height) / 1000.0
    mask = (
        np.isfinite(z_agl_km)
        & np.isfinite(u_ms)
        & np.isfinite(v_ms)
        & (z_agl_km >= 0)
        & (z_agl_km <= 12)
    )
    if mask.sum() < 4:
        raise ValueError("Not enough valid wind levels below 12 km for hodograph.")

    z = z_agl_km[mask]
    u = (u_ms[mask] * units("m/s")).to("knots").magnitude
    v = (v_ms[mask] * units("m/s")).to("knots").magnitude

    order = np.argsort(z)
    z, u, v = z[order], u[order], v[order]

    max_component = max(np.nanmax(np.abs(u)), np.nanmax(np.abs(v)), 20.0)
    component_range = max(30.0, math.ceil((max_component + 6.0) / 10.0) * 10.0)

    fig, ax = plt.subplots(figsize=(6.7, 6.5), dpi=170)
    fig.subplots_adjust(left=0.08, right=0.96, top=0.88, bottom=0.10)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    h = Hodograph(ax, component_range=component_range)
    h.add_grid(increment=10, color=GRID_MAJOR, linewidth=0.65, alpha=0.85)
    h.add_grid(increment=5, color=GRID, linewidth=0.4, alpha=0.55)

    for z0, z1, color, label in HODO_SEGMENTS:
        seg = (z >= z0) & (z <= z1)
        if seg.sum() >= 2:
            h.plot(u[seg], v[seg], color=color, linewidth=2.8, label=label)

    for target in (1, 3, 6, 9, 12):
        if z.min() <= target <= z.max():
            ut = np.interp(target, z, u)
            vt = np.interp(target, z, v)
            ax.scatter([ut], [vt], s=24, facecolor=BG, edgecolor=INK, linewidth=1.0, zorder=6)
            ax.annotate(f"{target} km", (ut, vt), xytext=(5, 5), textcoords="offset points", fontsize=8, color=MUTED)

    metpy_data = _metpy_parameters(data)
    bunkers = metpy_data.get("bunkers", {})
    marker_specs = (
        ("right_mover", "RM", "s", RM_COLOR),
        ("left_mover", "LM", "D", LM_COLOR),
        ("mean_wind_0_6km", "Mean 0–6 km", "o", MEAN_COLOR),
    )
    for key, label, marker, color in marker_specs:
        point = _marker_uv(bunkers.get(key))
        if point is None:
            continue
        ax.scatter([point[0]], [point[1]], s=48, marker=marker,
                   facecolor=BG, edgecolor=color, linewidth=1.7,
                   label=label, zorder=8)

    ax.axhline(0, color=GRID_MAJOR, lw=0.8)
    ax.axvline(0, color=GRID_MAJOR, lw=0.8)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("u wind (kt)")
    ax.set_ylabel("v wind (kt)")

    station = data.get("station_name", "Ljubljana")
    station_id = data.get("station", 14015)
    nominal = _nominal_title(data)
    ax.set_title("Hodograph · 0–12 km AGL", loc="left", pad=18)
    ax.text(0.0, 1.012, f"{station} ({station_id})  ·  {nominal}", transform=ax.transAxes,
            ha="left", va="bottom", fontsize=9.5, color=MUTED)

    handles, labels = ax.get_legend_handles_labels()
    if handles:
        legend = ax.legend(handles, labels, loc="upper right", frameon=False, fontsize=8.5, handlelength=2.4)
        for text in legend.get_texts():
            text.set_color(MUTED)

    shear = metpy_data.get("bulk_shear", {})
    srh = metpy_data.get("srh", {})
    footer = []
    for key, label in (("0_1km", "0–1"), ("0_3km", "0–3"), ("0_6km", "0–6")):
        item = shear.get(key, {})
        if _finite(item.get("magnitude_ms")):
            kt = (float(item["magnitude_ms"]) * units("m/s")).to("knots").magnitude
            footer.append(f"{label} km shear {kt:.0f} kt")
    rm_srh = srh.get("0_3km_right_mover", {})
    if _finite(rm_srh.get("total_m2s2")):
        footer.append(f"0–3 km SRH(RM) {float(rm_srh['total_m2s2']):.0f} m²/s²")

    if footer:
        ax.text(0.0, -0.105, "   ·   ".join(footer), transform=ax.transAxes,
                ha="left", va="top", fontsize=8.5, color=MUTED)

    fig.savefig(output, bbox_inches="tight", pad_inches=0.14)
    plt.close(fig)


# -----------------------------------------------------------------------------
# ENTRY POINTS
# -----------------------------------------------------------------------------

def _render_all_for_data(data: dict, paths: dict[str, Path]):
    render_skewt(data, paths["skewt"])
    render_lowlevel(data, paths["lowlevel"])
    # compatibility copy for current Apps Script, which still looks for skewt_zoom
    shutil.copyfile(paths["lowlevel"], paths["skewt_zoom"])
    render_thetae(data, paths["thetae"])
    render_hodograph(data, paths["hodograph"])


def render_products(input_path: str | os.PathLike, diagnostics_root="diagnostics"):
    input_path = Path(input_path)
    diagnostics_root = Path(diagnostics_root)
    diagnostics_root.mkdir(parents=True, exist_ok=True)

    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    latest = _latest_paths(diagnostics_root)
    archive = _archive_paths(data, diagnostics_root)

    _render_all_for_data(data, archive)
    _render_all_for_data(data, latest)

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest = {
        "version": 2,
        "station": data.get("station"),
        "station_name": data.get("station_name"),
        "sounding_id": data.get("sounding_id"),
        "nominal_date": data.get("nominal_date"),
        "term": data.get("term"),
        "launch_time": data.get("launch_time"),
        "processed_at": data.get("processed_at"),
        "rendered_at": generated_at,
        "latest": {k: v.as_posix() for k, v in latest.items()},
        "archive": {k: v.as_posix() for k, v in archive.items()},
        # compatibility aliases for existing Apps Script
        "skewt": latest["skewt"].as_posix(),
        "skewt_zoom": latest["skewt_zoom"].as_posix(),
        "lowlevel": latest["lowlevel"].as_posix(),
        "thetae": latest["thetae"].as_posix(),
        "hodograph": latest["hodograph"].as_posix(),
    }
    with (diagnostics_root / "latest_products.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("Rendered latest products to", diagnostics_root)
    return manifest


def backfill_products(data_root="data", diagnostics_root="diagnostics"):
    data_root = Path(data_root)
    diagnostics_root = Path(diagnostics_root)
    diagnostics_root.mkdir(parents=True, exist_ok=True)

    count = 0
    failures = []
    for path in sorted(data_root.rglob("*.json")):
        if path.name in {"latest.json", "status.json", "latest_products.json"}:
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "levels" not in data:
                continue
            archive = _archive_paths(data, diagnostics_root)
            _render_all_for_data(data, archive)
            count += 1
            print(f"Backfilled {path}")
        except Exception as exc:
            failures.append((str(path), str(exc)))
            print(f"Backfill failed for {path}: {exc}")

    print(f"Backfilled {count} soundings.")
    if failures:
        print("Failures:")
        for path, exc in failures:
            print(f"- {path}: {exc}")
    return {"count": count, "failures": failures}


def main():
    parser = argparse.ArgumentParser(description="Render improved LJLM sounding graphics.")
    parser.add_argument("--input", default="data/latest.json", help="Input sounding JSON")
    parser.add_argument("--diagnostics", default="diagnostics", help="Output diagnostics directory")
    parser.add_argument("--data-root", default="data", help="Data root for backfill mode")
    parser.add_argument("--backfill", action="store_true", help="Backfill all archived sounding JSON files")
    args = parser.parse_args()

    if args.backfill:
        backfill_products(args.data_root, args.diagnostics)
    else:
        render_products(args.input, args.diagnostics)


if __name__ == "__main__":
    main()
