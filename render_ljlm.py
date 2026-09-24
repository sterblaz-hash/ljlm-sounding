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
    rows.sort(
        key=lambda x: (
            -x["p"],
            x["z"],
        )
    )

    # Remove obvious duplicate pressure-height rows.
    cleaned = []
    seen = set()

    for row in rows:
        key = (
            round(row["p"], 2),
            round(row["z"], 1),
        )

        if key in seen:
            continue

        seen.add(key)
        cleaned.append(row)

    return cleaned


def profile_arrays(profile):
    rows = clean_levels(profile)

    if len(rows) < 10:
        raise ValueError(
            "Too few valid sounding levels."
        )

    p = np.array(
        [r["p"] for r in rows],
        dtype=float,
    )

    z = np.array(
        [r["z"] for r in rows],
        dtype=float,
    )

    t = np.array(
        [r["t"] for r in rows],
        dtype=float,
    )

    td = np.array(
        [r["td"] for r in rows],
        dtype=float,
    )

    ws = np.array(
        [r["ws"] for r in rows],
        dtype=float,
    )

    wd = np.array(
        [r["wd"] for r in rows],
        dtype=float,
    )

    return (
        rows,
        p,
        z,
        t,
        td,
        ws,
        wd,
    )


def wind_components_knots(
    ws_ms,
    wd_deg,
):
    mask = (
        np.isfinite(ws_ms)
        & np.isfinite(wd_deg)
    )

    u = np.full_like(
        ws_ms,
        np.nan,
        dtype=float,
    )

    v = np.full_like(
        ws_ms,
        np.nan,
        dtype=float,
    )

    if mask.any():
        uq, vq = (
            mpcalc.wind_components(
                ws_ms[mask]
                * units("m/s"),

                wd_deg[mask]
                * units.degree,
            )
        )

        u[mask] = (
            uq.to("knots")
            .magnitude
        )

        v[mask] = (
            vq.to("knots")
            .magnitude
        )

    return u, v


def nearest_indices_for_pressures(
    p,
    targets,
):
    indices = []

    for target in targets:
        candidates = np.where(
            np.isfinite(p)
        )[0]

        if len(candidates) == 0:
            continue

        idx = candidates[
            np.argmin(
                np.abs(
                    p[candidates]
                    - target
                )
            )
        ]

        if (
            abs(
                p[idx]
                - target
            )
            <= max(
                8.0,
                target * 0.015,
            )
        ):
            indices.append(
                int(idx)
            )

    return sorted(
        set(indices)
    )


def title_text(profile):
    station = (
        profile.get("station_name")
        or "Ljubljana"
    )

    station_id = (
        profile.get("station")
        or 14015
    )

    date = (
        profile.get("nominal_date")
        or ""
    )

    term = (
        profile.get("term")
        or ""
    )

    launch = (
        profile.get("launch_time")
        or ""
    )

    launch_text = (
        launch
        .replace("T", " ")
        .replace("Z", " UTC")
    )

    return (
        (
            f"{station} "
            f"({station_id}) · "
            f"{date} · "
            f"{term} UTC"
        ),
        (
            f"Launch "
            f"{launch_text}"
        ),
    )


def output_paths(
    profile,
    outdir,
):
    nominal = str(
        profile.get(
            "nominal_date"
        )
        or "unknown-date"
    )

    term = str(
        profile.get("term")
        or "unknown"
    ).replace(
        "/",
        "-",
    )

    compact = nominal.replace(
        "-",
        "",
    )

    try:
        yyyy, mm, _ = (
            nominal.split("-")
        )
    except ValueError:
        yyyy = "unknown"
        mm = "unknown"

    archive_dir = (
        Path(outdir)
        / yyyy
        / mm
    )

    archive_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    prefix = (
        f"{compact}_{term}"
    )

    return {
        "latest_skewt":
            Path(outdir)
            / "latest_skewt.png",

        "latest_hodograph":
            Path(outdir)
            / "latest_hodograph.png",

        "archive_skewt":
            archive_dir
            / f"{prefix}_skewt.png",

        "archive_hodograph":
            archive_dir
            / f"{prefix}_hodograph.png",

        "manifest":
            Path(outdir)
            / "latest_products.json",
    }


def safe_param(
    dct,
    *keys,
):
    node = dct

    for key in keys:
        if not isinstance(
            node,
            dict,
        ):
            return None

        node = node.get(key)

    return node


# ---------------------------------------------------------------------
# SKEW-T
# ---------------------------------------------------------------------

def render_skewt(
    profile,
    destination,
):
    (
        rows,
        p,
        z,
        t,
        td,
        ws,
        wd,
    ) = profile_arrays(
        profile
    )

    p_q = (
        p
        * units.hPa
    )

    t_q = (
        t
        * units.degC
    )

    td_q = (
        td
        * units.degC
    )

    fig = plt.figure(
        figsize=(
            8.7,
            9.8,
        ),
        dpi=DPI,
        facecolor=BG,
    )

    skew = SkewT(
        fig,
        rotation=45,
        rect=(
            0.08,
            0.08,
            0.77,
            0.84,
        ),
    )

    ax = skew.ax

    ax.set_facecolor(
        BG
    )

    skew.plot(
        p_q,
        t_q,
        color=TEMP,
        linewidth=2.6,
        label="Temperature",
    )

    finite_td = (
        np.isfinite(td)
    )

    if finite_td.sum() >= 2:
        skew.plot(
            p_q[finite_td],
            td_q[finite_td],
            color=DEW,
            linewidth=2.4,
            label="Dew point",
        )

    # Surface parcel profile.
    try:
        if finite_td[0]:
            parcel = (
                mpcalc
                .parcel_profile(
                    p_q,
                    t_q[0],
                    td_q[0],
                )
                .to("degC")
            )

            skew.plot(
                p_q,
                parcel,
                color=PARCEL,
                linewidth=1.6,
                linestyle="--",
                alpha=0.9,
                label="Surface parcel",
            )

    except Exception as exc:
        print(
            "Skew-T parcel warning:",
            exc,
        )

    # Thermodynamic background.
    try:
        skew.plot_dry_adiabats(
            linewidth=0.55,
            alpha=0.35,
            color="#b9a994",
        )

        skew.plot_moist_adiabats(
            linewidth=0.55,
            alpha=0.30,
            color="#92b7ad",
        )

        skew.plot_mixing_lines(
            linewidth=0.5,
            alpha=0.28,
            color="#7da6b5",
        )

    except Exception as exc:
        print(
            "Skew-T background warning:",
            exc,
        )

    # 0 °C reference.
    try:
        skew.ax.axvline(
            0,
            color="#89919f",
            linewidth=0.9,
            linestyle="--",
            alpha=0.8,
        )
    except Exception:
        pass

    # Wind barbs.
    u, v = (
        wind_components_knots(
            ws,
            wd,
        )
    )

    barb_targets = [
        1000,
        950,
        900,
        850,
        800,
        750,
        700,
        650,
        600,
        550,
        500,
        450,
        400,
        350,
        300,
        250,
        200,
        150,
        100,
    ]

    barb_idx = [
        i
        for i
        in nearest_indices_for_pressures(
            p,
            barb_targets,
        )
        if (
            np.isfinite(u[i])
            and np.isfinite(v[i])
        )
    ]

    if barb_idx:
        try:
            skew.plot_barbs(
                p_q[barb_idx],

                u[barb_idx]
                * units.knots,

                v[barb_idx]
                * units.knots,

                xloc=1.035,
                length=5.7,
                linewidth=0.7,
                color=WIND,
            )

        except TypeError:
            skew.plot_barbs(
                p_q[barb_idx],

                u[barb_idx]
                * units.knots,

                v[barb_idx]
                * units.knots,

                xloc=1.035,
            )

    # Existing inversion diagnostics.
    inversions = (
        safe_param(
            profile,
            "parameters",
            "inversions",
        )
        or {}
    )

    if isinstance(
        inversions,
        dict,
    ):
        layers = (
            inversions.get(
                "layers",
                [],
            )
        )
    else:
        layers = []

    for layer in layers:
        base_p = (
            layer.get(
                "base_pressure_hpa"
            )
        )

        top_p = (
            layer.get(
                "top_pressure_hpa"
            )
        )

        if not (
            valid_number(base_p)
            and valid_number(top_p)
        ):
            continue

        ax.axhspan(
            float(top_p),
            float(base_p),
            color=INV,
            alpha=0.14,
            zorder=0,
        )

    # Standard pressure references.
    for level in (
        850,
        700,
        500,
        300,
    ):
        ax.axhline(
            level,
            color=GRID,
            linewidth=0.7,
            linestyle=":",
            zorder=0,
        )

    ax.set_ylim(
        1000,
        100,
    )

    ax.set_xlim(
        -45,
        40,
    )

    ax.grid(False)

    ax.tick_params(
        labelsize=9,
        colors=TEXT,
    )

    ax.set_xlabel(
        "Temperature (°C)",
        fontsize=10,
        color=TEXT,
    )

    ax.set_ylabel(
        "Pressure (hPa)",
        fontsize=10,
        color=TEXT,
    )

    for spine in (
        ax.spines.values()
    ):
        spine.set_color(
            "#b7bec9"
        )

        spine.set_linewidth(
            0.8
        )

    main_title, subtitle = (
        title_text(profile)
    )

    fig.text(
        0.08,
        0.965,
        "Skew-T log-p",
        ha="left",
        va="top",
        fontsize=15,
        fontweight="bold",
        color=TEXT,
    )

    fig.text(
        0.08,
        0.94,
        main_title,
        ha="left",
        va="top",
        fontsize=10.5,
        color=TEXT,
    )

    fig.text(
        0.08,
        0.918,
        subtitle,
        ha="left",
        va="top",
        fontsize=8.7,
        color=MUTED,
    )

    handles, labels = (
        ax.get_legend_handles_labels()
    )

    if handles:
        ax.legend(
            handles,
            labels,
            loc="upper left",
            frameon=False,
            fontsize=8.5,
        )

    fig.text(
        0.89,
        0.50,
        "Wind (kt)",
        rotation=90,
        ha="center",
        va="center",
        fontsize=8.5,
        color=MUTED,
    )

    destination = Path(
        destination
    )

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fig.savefig(
        destination,
        dpi=DPI,
        bbox_inches="tight",
        facecolor=BG,
        pad_inches=0.18,
    )

    plt.close(fig)


# ---------------------------------------------------------------------
# HODOGRAPH
# ---------------------------------------------------------------------

def render_hodograph(
    profile,
    destination,
):
    (
        rows,
        p,
        z,
        t,
        td,
        ws,
        wd,
    ) = profile_arrays(
        profile
    )

    u, v = (
        wind_components_knots(
            ws,
            wd,
        )
    )

    surface_z = (
        np.nanmin(z)
    )

    agl_km = (
        z
        - surface_z
    ) / 1000.0

    mask = (
        np.isfinite(u)
        & np.isfinite(v)
        & np.isfinite(agl_km)
        & (agl_km >= 0)
        & (agl_km <= 12.0)
    )

    if mask.sum() < 3:
        raise ValueError(
            "Too few valid wind "
            "levels for hodograph."
        )

    u2 = u[mask]
    v2 = v[mask]
    z2 = agl_km[mask]

    # Reduce very dense profile.
    if len(u2) > 300:
        keep = np.unique(
            np.linspace(
                0,
                len(u2) - 1,
                300,
            ).astype(int)
        )

        u2 = u2[keep]
        v2 = v2[keep]
        z2 = z2[keep]

    max_component = float(
        np.nanmax(
            np.abs(
                np.concatenate(
                    [
                        u2,
                        v2,
                    ]
                )
            )
        )
    )

    component_range = max(
        30.0,
        math.ceil(
            (
                max_component
                + 7.0
            )
            / 10.0
        )
        * 10.0,
    )

    fig = plt.figure(
        figsize=(
            7.4,
            7.4,
        ),
        dpi=DPI,
        facecolor=BG,
    )

    ax = fig.add_axes(
        (
            0.11,
            0.12,
            0.78,
            0.78,
        )
    )

    ax.set_facecolor(
        BG
    )

    h = Hodograph(
        ax,
        component_range=
        component_range,
    )

    h.add_grid(
        increment=10,
        color=GRID,
        linewidth=0.7,
        alpha=0.9,
    )

    layer_specs = [
        (
            0.0,
            1.0,
            "#214f9b",
            3.1,
            "0–1 km",
        ),
        (
            1.0,
            3.0,
            "#2a8b72",
            2.8,
            "1–3 km",
        ),
        (
            3.0,
            6.0,
            "#c68a26",
            2.6,
            "3–6 km",
        ),
        (
            6.0,
            12.1,
            "#7a8290",
            2.2,
            "6–12 km",
        ),
    ]

    for (
        zmin,
        zmax,
        color,
        lw,
        label,
    ) in layer_specs:

        lm = (
            (z2 >= zmin)
            & (z2 <= zmax)
        )

        if lm.sum() >= 2:
            ax.plot(
                u2[lm],
                v2[lm],
                color=color,
                linewidth=lw,
                solid_capstyle="round",
                label=label,
            )

    # Mark height levels.
    for level_km in (
        1,
        3,
        6,
        9,
        12,
    ):
        idx = int(
            np.argmin(
                np.abs(
                    z2
                    - level_km
                )
            )
        )

        if (
            abs(
                z2[idx]
                - level_km
            )
            <= 0.5
        ):
            ax.scatter(
                [u2[idx]],
                [v2[idx]],
                s=27,
                color=TEXT,
                zorder=5,
            )

            ax.annotate(
                f"{level_km} km",
                (
                    u2[idx],
                    v2[idx],
                ),
                xytext=(
                    5,
                    5,
                ),
                textcoords=
                    "offset points",
                fontsize=8,
                color=TEXT,
            )

    metpy = (
        safe_param(
            profile,
            "parameters",
            "metpy",
        )
        or {}
    )

    # Bunkers RM / LM.
    bunkers = (
        metpy.get(
            "bunkers",
            {},
        )
        if isinstance(
            metpy,
            dict,
        )
        else {}
    )

    markers = [
        (
            "RM",
            bunkers.get(
                "right_mover"
            ),
            TEMP,
        ),
        (
            "LM",
            bunkers.get(
                "left_mover"
            ),
            DEW,
        ),
    ]

    for (
        label,
        item,
        color,
    ) in markers:

        if not isinstance(
            item,
            dict,
        ):
            continue

        if valid_number(
            item.get("u_kt")
        ):
            uu = item.get("u_kt")
        else:
            uu = item.get("u_ms")

        if valid_number(
            item.get("v_kt")
        ):
            vv = item.get("v_kt")
        else:
            vv = item.get("v_ms")

        if not (
            valid_number(uu)
            and valid_number(vv)
        ):
            continue

        uu = float(uu)
        vv = float(vv)

        if item.get("u_kt") is None:
            uu *= 1.94384449

        if item.get("v_kt") is None:
            vv *= 1.94384449

        ax.scatter(
            [uu],
            [vv],
            marker="x",
            s=65,
            linewidths=2,
            color=color,
            zorder=7,
        )

        ax.annotate(
            label,
            (
                uu,
                vv,
            ),
            xytext=(
                6,
                -11,
            ),
            textcoords=
                "offset points",
            fontsize=8.5,
            fontweight="bold",
            color=color,
        )

    ax.axhline(
        0,
        color="#9aa2ae",
        linewidth=0.8,
    )

    ax.axvline(
        0,
        color="#9aa2ae",
        linewidth=0.8,
    )

    ax.set_aspect(
        "equal",
        adjustable="box",
    )

    ax.set_xlabel(
        "u wind (kt)",
        color=TEXT,
        fontsize=10,
    )

    ax.set_ylabel(
        "v wind (kt)",
        color=TEXT,
        fontsize=10,
    )

    ax.tick_params(
        colors=TEXT,
        labelsize=9,
    )

    for spine in (
        ax.spines.values()
    ):
        spine.set_color(
            "#b7bec9"
        )

        spine.set_linewidth(
            0.8
        )

    main_title, subtitle = (
        title_text(profile)
    )

    fig.text(
        0.08,
        0.965,
        "Hodograph",
        ha="left",
        va="top",
        fontsize=15,
        fontweight="bold",
        color=TEXT,
    )

    fig.text(
        0.08,
        0.938,
        main_title,
        ha="left",
        va="top",
        fontsize=10.5,
        color=TEXT,
    )

    ax.legend(
        loc="upper right",
        frameon=False,
        fontsize=8.5,
    )

    # Compact diagnostics footer.
    srh = (
        metpy.get(
            "srh",
            {},
        )
        if isinstance(
            metpy,
            dict,
        )
        else {}
    )

    footer = []

    s06 = (
        metpy.get(
            "shear_0_6km_ms"
        )
        if isinstance(
            metpy,
            dict,
        )
        else None
    )

    if valid_number(s06):
        footer.append(
            (
                "0–6 km shear "
                f"{float(s06):.1f} m/s"
            )
        )

    if isinstance(
        srh,
        dict,
    ):
        srh3 = srh.get(
            "0_3km_right_mover"
        )

        if (
            isinstance(
                srh3,
                dict,
            )
            and valid_number(
                srh3.get(
                    "total_m2s2"
                )
            )
        ):
            footer.append(
                (
                    "0–3 km SRH "
                    f"{float(srh3['total_m2s2']):.0f} "
                    "m²/s²"
                )
            )

    if footer:
        fig.text(
            0.5,
            0.035,
            "   ·   ".join(
                footer
            ),
            ha="center",
            va="center",
            fontsize=8.5,
            color=MUTED,
        )

    destination = Path(
        destination
    )

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fig.savefig(
        destination,
        dpi=DPI,
        bbox_inches="tight",
        facecolor=BG,
        pad_inches=0.18,
    )

    plt.close(fig)


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------

def render_all(
    profile,
    outdir="diagnostics",
):
    outdir = Path(
        outdir
    )

    outdir.mkdir(
        parents=True,
        exist_ok=True,
    )

    paths = output_paths(
        profile,
        outdir,
    )

    render_skewt(
        profile,
        paths[
            "archive_skewt"
        ],
    )

    render_hodograph(
        profile,
        paths[
            "archive_hodograph"
        ],
    )

    shutil.copy2(
        paths[
            "archive_skewt"
        ],
        paths[
            "latest_skewt"
        ],
    )

    shutil.copy2(
        paths[
            "archive_hodograph"
        ],
        paths[
            "latest_hodograph"
        ],
    )

    manifest = {
        "station":
            profile.get(
                "station"
            ),

        "station_name":
            profile.get(
                "station_name"
            ),

        "nominal_date":
            profile.get(
                "nominal_date"
            ),

        "term":
            profile.get(
                "term"
            ),

        "launch_time":
            profile.get(
                "launch_time"
            ),

        "processed_at":
            profile.get(
                "processed_at"
            ),

        "skewt":
            str(
                paths[
                    "latest_skewt"
                ]
            ).replace(
                os.sep,
                "/",
            ),

        "hodograph":
            str(
                paths[
                    "latest_hodograph"
                ]
            ).replace(
                os.sep,
                "/",
            ),

        "archive_skewt":
            str(
                paths[
                    "archive_skewt"
                ]
            ).replace(
                os.sep,
                "/",
            ),

        "archive_hodograph":
            str(
                paths[
                    "archive_hodograph"
                ]
            ).replace(
                os.sep,
                "/",
            ),
    }

    with open(
        paths["manifest"],
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            manifest,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(
        "Rendered:",
        paths[
            "latest_skewt"
        ],
    )

    print(
        "Rendered:",
        paths[
            "latest_hodograph"
        ],
    )

    print(
        "Manifest:",
        paths[
            "manifest"
        ],
    )

    return paths


def main():
    parser = (
        argparse.ArgumentParser(
            description=(
                "Render static LJLM "
                "Skew-T and hodograph "
                "PNG products."
            )
        )
    )

    parser.add_argument(
        "--input",
        default=
            "data/latest.json",
        help=(
            "Processed LJLM "
            "sounding JSON."
        ),
    )

    parser.add_argument(
        "--outdir",
        default="diagnostics",
        help="Output directory.",
    )

    args = parser.parse_args()

    profile = load_profile(
        args.input
    )

    render_all(
        profile,
        args.outdir,
    )


if __name__ == "__main__":
    main()
