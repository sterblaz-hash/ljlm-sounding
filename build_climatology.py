import io
import json
import math
import os
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from urllib.request import Request, urlopen

import numpy as np
import metpy.calc as mpcalc
from metpy.units import units


# ============================================================
# NASTAVITVE
# ============================================================

IGRA_URL = (
    "https://www.ncei.noaa.gov/pub/data/igra/"
    "data/data-por/SIM00014015-data.txt.zip"
)

START_YEAR = 1996
END_YEAR = 2025
WINDOW_DAYS = 15
SMOOTHING_DAYS = 10

OUTPUT_FILE = "climatology/daily_climatology.json"

STATION_ID = "SIM00014015"
STATION_NAME = "Ljubljana / Bežigrad"

# Največja dovoljena tlačna vrzel pri interpolaciji na standardni nivo.
MAX_PRESSURE_GAP_HPA = 100.0

# Največja dovoljena višinska vrzel pri interpolaciji vetra.
MAX_WIND_HEIGHT_GAP_M = 1500.0

STANDARD_WIND_LEVELS = (925, 850, 700, 500, 300)

PARAMETERS = [
    "t850",
    "t700",
    "t500",
    "pwat_mm",
    "freezing_level_msl_m",
    "lapse_rate_850_500_c_per_km",
    "lapse_rate_700_500_c_per_km",
    "q_surface_gkg",
    "q925_gkg",
    "q850_gkg",
    "ivt_kg_m_s",
    "wind_speed_925_ms",
    "wind_speed_850_ms",
    "wind_speed_700_ms",
    "wind_speed_500_ms",
    "wind_speed_300_ms",
    "shear_0_1km_ms",
    "shear_0_3km_ms",
    "shear_0_6km_ms",
    "shear_sfc_700_ms",
    "z500_m",
    "thickness_1000_500_m",
    "thickness_925_500_m",
    "mucape_jkg",
]

PARAMETER_METADATA = {
    "t850": {"label": "T 850 hPa", "unit": "°C"},
    "t700": {"label": "T 700 hPa", "unit": "°C"},
    "t500": {"label": "T 500 hPa", "unit": "°C"},
    "pwat_mm": {"label": "PWAT", "unit": "mm"},
    "freezing_level_msl_m": {"label": "Freezing level", "unit": "m MSL"},
    "lapse_rate_850_500_c_per_km": {
        "label": "Lapse rate 850–500 hPa",
        "unit": "°C/km",
    },
    "lapse_rate_700_500_c_per_km": {
        "label": "Lapse rate 700–500 hPa",
        "unit": "°C/km",
    },
    "q_surface_gkg": {"label": "q surface", "unit": "g/kg"},
    "q925_gkg": {"label": "q 925 hPa", "unit": "g/kg"},
    "q850_gkg": {"label": "q 850 hPa", "unit": "g/kg"},
    "ivt_kg_m_s": {"label": "IVT magnitude", "unit": "kg m⁻¹ s⁻¹"},
    "wind_speed_925_ms": {"label": "Wind 925 hPa", "unit": "m/s"},
    "wind_speed_850_ms": {"label": "Wind 850 hPa", "unit": "m/s"},
    "wind_speed_700_ms": {"label": "Wind 700 hPa", "unit": "m/s"},
    "wind_speed_500_ms": {"label": "Wind 500 hPa", "unit": "m/s"},
    "wind_speed_300_ms": {"label": "Wind 300 hPa", "unit": "m/s"},
    "shear_0_1km_ms": {"label": "Bulk shear 0–1 km", "unit": "m/s"},
    "shear_0_3km_ms": {"label": "Bulk shear 0–3 km", "unit": "m/s"},
    "shear_0_6km_ms": {"label": "Bulk shear 0–6 km", "unit": "m/s"},
    "shear_sfc_700_ms": {"label": "Bulk shear surface–700 hPa", "unit": "m/s"},
    "z500_m": {"label": "Z500", "unit": "m"},
    "thickness_1000_500_m": {"label": "1000–500 hPa thickness", "unit": "m"},
    "thickness_925_500_m": {"label": "925–500 hPa thickness", "unit": "m"},
    "mucape_jkg": {"label": "MUCAPE", "unit": "J/kg"},
}


# ============================================================
# POMOŽNE FUNKCIJE
# ============================================================

def valid_number(value):
    try:
        return value is not None and math.isfinite(float(value))
    except Exception:
        return False


def download(url):
    print("Downloading IGRA archive...")

    req = Request(
        url,
        headers={"User-Agent": "ljlm-sounding/2.0"},
    )

    with urlopen(req, timeout=120) as response:
        return response.read()


def _missing_int(value):
    return value in (-9999, -8888)


# ============================================================
# IGRA PARSER
# ============================================================

def parse_header(line):
    """IGRA v2 profile header."""

    if not line.startswith("#"):
        return None

    try:
        station = line[1:12].strip()
        year = int(line[13:17])
        month = int(line[18:20])
        day = int(line[21:23])
        hour = int(line[24:26])
        num_levels = int(line[32:36])

        return {
            "station": station,
            "year": year,
            "month": month,
            "day": day,
            "hour": hour,
            "num_levels": num_levels,
        }

    except Exception:
        return None


def parse_level(line):
    """
    Parse one IGRA v2 sounding level.

    IGRA native units:
      pressure             Pa
      geopotential height  m
      temperature          0.1 °C
      dewpoint depression  0.1 °C
      wind direction       degrees
      wind speed           0.1 m/s
    """

    try:
        pressure_raw = int(line[9:15])
        height_raw = int(line[16:21])
        temperature_raw = int(line[22:27])
        dewpoint_dep_raw = int(line[34:39])
        wind_direction_raw = int(line[40:45])
        wind_speed_raw = int(line[46:51])
    except Exception:
        return None

    pressure = None
    height = None
    temperature = None
    dewpoint = None
    wind_direction = None
    wind_speed = None
    u_ms = None
    v_ms = None

    if pressure_raw > 0:
        pressure = pressure_raw / 100.0

    if not _missing_int(height_raw):
        height = float(height_raw)

    if not _missing_int(temperature_raw):
        temperature = temperature_raw / 10.0

    if (
        temperature is not None
        and not _missing_int(dewpoint_dep_raw)
    ):
        dewpoint = temperature - dewpoint_dep_raw / 10.0

    if (
        not _missing_int(wind_direction_raw)
        and 0 <= wind_direction_raw <= 360
    ):
        wind_direction = float(wind_direction_raw)

    if (
        not _missing_int(wind_speed_raw)
        and wind_speed_raw >= 0
    ):
        wind_speed = wind_speed_raw / 10.0

    # Meteorological direction: direction FROM which wind blows.
    if wind_direction is not None and wind_speed is not None:
        angle = math.radians(wind_direction)
        u_ms = -wind_speed * math.sin(angle)
        v_ms = -wind_speed * math.cos(angle)

    if pressure is None or pressure <= 0 or pressure > 1100:
        return None

    return {
        "pressure_hpa": pressure,
        "height_m": height,
        "temperature_c": temperature,
        "dewpoint_c": dewpoint,
        "wind_direction_deg": wind_direction,
        "wind_speed_ms": wind_speed,
        "u_ms": u_ms,
        "v_ms": v_ms,
    }


# ============================================================
# BRANJE PROFILOV
# ============================================================

def read_profiles(text):
    lines = text.splitlines()
    profiles = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if not line.startswith("#"):
            i += 1
            continue

        header = parse_header(line)

        if header is None:
            i += 1
            continue

        n = header["num_levels"]
        level_lines = lines[i + 1:i + 1 + n]
        i += n + 1

        if header["station"] != STATION_ID:
            continue

        year = header["year"]

        if year < START_YEAR or year > END_YEAR:
            continue

        try:
            profile_date = date(
                year,
                header["month"],
                header["day"],
            )
        except ValueError:
            continue

        levels = []

        for level_line in level_lines:
            level = parse_level(level_line)

            if level is not None:
                levels.append(level)

        if len(levels) < 10:
            continue

        levels.sort(
            key=lambda x: x["pressure_hpa"],
            reverse=True,
        )

        profiles.append({
            "date": profile_date,
            "hour": header["hour"],
            "levels": levels,
        })

    return profiles


# ============================================================
# INTERPOLACIJA
# ============================================================

def interpolate_pressure(
    levels,
    field,
    target_pressure,
    max_gap_hpa=MAX_PRESSURE_GAP_HPA,
):
    """Log-pressure interpolation of one scalar field."""

    usable = [
        x for x in levels
        if valid_number(x.get(field))
    ]

    if not usable:
        return None

    exact = [
        x for x in usable
        if abs(x["pressure_hpa"] - target_pressure) <= 0.1
    ]

    if exact:
        return float(exact[0][field])

    above = None
    below = None

    for level in usable:
        p = level["pressure_hpa"]

        if p > target_pressure:
            if above is None or p < above["pressure_hpa"]:
                above = level

        elif p < target_pressure:
            if below is None or p > below["pressure_hpa"]:
                below = level

    if above is None or below is None:
        return None

    p1 = above["pressure_hpa"]
    p2 = below["pressure_hpa"]

    if abs(p1 - p2) > max_gap_hpa:
        return None

    v1 = float(above[field])
    v2 = float(below[field])

    fraction = (
        math.log(target_pressure / p1)
        / math.log(p2 / p1)
    )

    return v1 + fraction * (v2 - v1)


def interpolate_height(rows, field, target_height_m):
    """Linear interpolation of wind components in geometric height."""

    usable = [
        x for x in rows
        if valid_number(x.get("height_m"))
        and valid_number(x.get(field))
    ]

    if not usable:
        return None

    usable.sort(key=lambda x: x["height_m"])

    exact = [
        x for x in usable
        if abs(x["height_m"] - target_height_m) <= 1.0
    ]

    if exact:
        return float(exact[0][field])

    lower = None
    upper = None

    for row in usable:
        z = row["height_m"]

        if z < target_height_m:
            lower = row

        elif z > target_height_m:
            upper = row
            break

    if lower is None or upper is None:
        return None

    z1 = float(lower["height_m"])
    z2 = float(upper["height_m"])

    if z2 <= z1 or (z2 - z1) > MAX_WIND_HEIGHT_GAP_M:
        return None

    v1 = float(lower[field])
    v2 = float(upper[field])

    fraction = (target_height_m - z1) / (z2 - z1)

    return v1 + fraction * (v2 - v1)


# ============================================================
# TERMODINAMIKA
# ============================================================

def freezing_level(levels):
    rows = [
        x for x in levels
        if valid_number(x.get("temperature_c"))
        and valid_number(x.get("height_m"))
    ]

    rows.sort(key=lambda x: x["height_m"])

    if len(rows) < 2:
        return None

    for i in range(len(rows) - 1):
        t1 = float(rows[i]["temperature_c"])
        t2 = float(rows[i + 1]["temperature_c"])
        z1 = float(rows[i]["height_m"])
        z2 = float(rows[i + 1]["height_m"])

        if t1 >= 0 and t2 < 0 and z2 > z1:
            fraction = (0.0 - t1) / (t2 - t1)
            return z1 + fraction * (z2 - z1)

    return None


def calculate_pwat(levels):
    all_pressure_rows = [
        x for x in levels
        if valid_number(x.get("pressure_hpa"))
    ]

    if not all_pressure_rows:
        return None

    surface_pressure = max(
        float(x["pressure_hpa"])
        for x in all_pressure_rows
    )

    rows = [
        x for x in levels
        if valid_number(x.get("pressure_hpa"))
        and valid_number(x.get("dewpoint_c"))
    ]

    if len(rows) < 10:
        return None

    rows.sort(
        key=lambda x: x["pressure_hpa"],
        reverse=True,
    )

    unique = []
    seen = set()

    for row in rows:
        p_key = round(float(row["pressure_hpa"]), 2)

        if p_key in seen:
            continue

        seen.add(p_key)
        unique.append(row)

    if len(unique) < 10:
        return None

    bottom_p = float(unique[0]["pressure_hpa"])
    top_p = float(unique[-1]["pressure_hpa"])

    if surface_pressure - bottom_p > 50:
        return None

    if top_p > 300:
        return None

    p = np.array(
        [x["pressure_hpa"] for x in unique],
        dtype=float,
    ) * units.hPa

    td = np.array(
        [x["dewpoint_c"] for x in unique],
        dtype=float,
    ) * units.degC

    try:
        value = float(
            mpcalc.precipitable_water(p, td)
            .to("millimeter")
            .magnitude
        )

        if not math.isfinite(value):
            return None

        if value <= 0 or value > 100:
            return None

        return value

    except Exception:
        return None


def specific_humidity_gkg(pressure_hpa, dewpoint_c):
    if not valid_number(pressure_hpa) or not valid_number(dewpoint_c):
        return None

    try:
        q = mpcalc.specific_humidity_from_dewpoint(
            float(pressure_hpa) * units.hPa,
            float(dewpoint_c) * units.degC,
        )

        value = float(q.to("dimensionless").magnitude) * 1000.0

        if not math.isfinite(value) or value < 0 or value > 40:
            return None

        return value

    except Exception:
        return None


def q_at_pressure(levels, target_pressure):
    td = interpolate_pressure(
        levels,
        "dewpoint_c",
        target_pressure,
    )

    if td is None:
        return None

    return specific_humidity_gkg(
        target_pressure,
        td,
    )


def q_surface(levels):
    all_rows = [
        x for x in levels
        if valid_number(x.get("pressure_hpa"))
    ]

    moist_rows = [
        x for x in levels
        if valid_number(x.get("pressure_hpa"))
        and valid_number(x.get("dewpoint_c"))
    ]

    if not all_rows or not moist_rows:
        return None

    surface_pressure = max(
        float(x["pressure_hpa"])
        for x in all_rows
    )

    moist_surface = max(
        moist_rows,
        key=lambda x: x["pressure_hpa"],
    )

    if surface_pressure - float(moist_surface["pressure_hpa"]) > 50:
        return None

    return specific_humidity_gkg(
        moist_surface["pressure_hpa"],
        moist_surface["dewpoint_c"],
    )


def pressure_lapse_rate(
    levels,
    bottom_pressure,
    top_pressure,
):
    t_bottom = interpolate_pressure(
        levels,
        "temperature_c",
        bottom_pressure,
    )

    t_top = interpolate_pressure(
        levels,
        "temperature_c",
        top_pressure,
    )

    z_bottom = interpolate_pressure(
        levels,
        "height_m",
        bottom_pressure,
    )

    z_top = interpolate_pressure(
        levels,
        "height_m",
        top_pressure,
    )

    if (
        t_bottom is None
        or t_top is None
        or z_bottom is None
        or z_top is None
    ):
        return None

    depth_km = (z_top - z_bottom) / 1000.0

    if depth_km <= 0:
        return None

    return (t_bottom - t_top) / depth_km


def calculate_mucape(levels):
    """
    Historical MUCAPE from the IGRA profile.

    QC:
      - at least 10 common P/T/Td levels,
      - thermodynamic profile begins within 50 hPa of sounding bottom,
      - valid moisture/temperature reaches at least 300 hPa.
    """

    all_p = [
        float(x["pressure_hpa"])
        for x in levels
        if valid_number(x.get("pressure_hpa"))
    ]

    if not all_p:
        return None

    surface_pressure = max(all_p)

    rows = [
        x for x in levels
        if valid_number(x.get("pressure_hpa"))
        and valid_number(x.get("temperature_c"))
        and valid_number(x.get("dewpoint_c"))
    ]

    rows.sort(
        key=lambda x: x["pressure_hpa"],
        reverse=True,
    )

    unique = []
    seen = set()

    for row in rows:
        key = round(float(row["pressure_hpa"]), 2)

        if key in seen:
            continue

        seen.add(key)
        unique.append(row)

    if len(unique) < 10:
        return None

    if surface_pressure - float(unique[0]["pressure_hpa"]) > 50:
        return None

    if float(unique[-1]["pressure_hpa"]) > 300:
        return None

    p = np.array(
        [x["pressure_hpa"] for x in unique],
        dtype=float,
    ) * units.hPa

    t = np.array(
        [x["temperature_c"] for x in unique],
        dtype=float,
    ) * units.degC

    td = np.array(
        [x["dewpoint_c"] for x in unique],
        dtype=float,
    ) * units.degC

    try:
        cape, _cin = mpcalc.most_unstable_cape_cin(
            p,
            t,
            td,
        )

        value = float(
            cape.to("joule / kilogram").magnitude
        )

        if not math.isfinite(value) or value < 0:
            return None

        # Zelo širok sanity check.
        if value > 10000:
            return None

        return value

    except Exception:
        return None


# ============================================================
# VETER
# ============================================================

def wind_speed_at_pressure(levels, target_pressure):
    u = interpolate_pressure(
        levels,
        "u_ms",
        target_pressure,
    )

    v = interpolate_pressure(
        levels,
        "v_ms",
        target_pressure,
    )

    if u is None or v is None:
        return None

    speed = math.hypot(u, v)

    if not math.isfinite(speed) or speed < 0 or speed > 120:
        return None

    return speed


def bulk_shear(levels, depth_m):
    """
    Vector bulk shear from the lowest valid wind level
    to target height AGL.
    """

    wind_rows = [
        x for x in levels
        if valid_number(x.get("height_m"))
        and valid_number(x.get("u_ms"))
        and valid_number(x.get("v_ms"))
    ]

    if len(wind_rows) < 2:
        return None

    wind_rows.sort(key=lambda x: x["height_m"])

    surface = wind_rows[0]
    surface_z = float(surface["height_m"])

    # Avoid using a first wind observation that is far above the sounding base.
    all_heights = [
        float(x["height_m"])
        for x in levels
        if valid_number(x.get("height_m"))
    ]

    if all_heights and surface_z - min(all_heights) > 500:
        return None

    target_z = surface_z + float(depth_m)

    u_top = interpolate_height(
        wind_rows,
        "u_ms",
        target_z,
    )
    v_top = interpolate_height(
        wind_rows,
        "v_ms",
        target_z,
    )

    if u_top is None or v_top is None:
        return None

    du = u_top - float(surface["u_ms"])
    dv = v_top - float(surface["v_ms"])

    shear = math.hypot(du, dv)

    if not math.isfinite(shear) or shear < 0 or shear > 120:
        return None

    return shear



def surface_to_pressure_shear(levels, target_pressure):
    """
    Vector bulk shear from the lowest valid wind level to a pressure surface.

    The lowest valid wind observation is accepted only when it lies within
    500 m of the sounding base. The top wind is obtained by u/v interpolation
    in log-pressure coordinates using the same pressure-gap QC as the
    standard-level wind diagnostics.
    """

    wind_rows = [
        x for x in levels
        if valid_number(x.get("height_m"))
        and valid_number(x.get("pressure_hpa"))
        and valid_number(x.get("u_ms"))
        and valid_number(x.get("v_ms"))
    ]

    if len(wind_rows) < 2:
        return None

    wind_rows.sort(key=lambda x: x["height_m"])

    surface = wind_rows[0]
    surface_z = float(surface["height_m"])

    all_heights = [
        float(x["height_m"])
        for x in levels
        if valid_number(x.get("height_m"))
    ]

    if all_heights and surface_z - min(all_heights) > 500:
        return None

    surface_pressure = float(surface["pressure_hpa"])
    if surface_pressure <= float(target_pressure):
        return None

    u_top = interpolate_pressure(levels, "u_ms", target_pressure)
    v_top = interpolate_pressure(levels, "v_ms", target_pressure)

    if u_top is None or v_top is None:
        return None

    du = float(u_top) - float(surface["u_ms"])
    dv = float(v_top) - float(surface["v_ms"])

    shear = math.hypot(du, dv)

    if not math.isfinite(shear) or shear < 0 or shear > 120:
        return None

    return shear


def calculate_ivt(levels):
    """
    IVT magnitude:
        | 1/g ∫ q V dp |

    Integration uses thermo levels with valid Td and interpolated u/v.
    Profile must begin near the sounding bottom and reach at least 300 hPa.
    """

    all_p = [
        float(x["pressure_hpa"])
        for x in levels
        if valid_number(x.get("pressure_hpa"))
    ]

    if not all_p:
        return None

    surface_pressure = max(all_p)

    samples = []

    for row in levels:
        if (
            not valid_number(row.get("pressure_hpa"))
            or not valid_number(row.get("dewpoint_c"))
        ):
            continue

        p = float(row["pressure_hpa"])

        u = interpolate_pressure(
            levels,
            "u_ms",
            p,
        )
        v = interpolate_pressure(
            levels,
            "v_ms",
            p,
        )

        if u is None or v is None:
            continue

        q_gkg = specific_humidity_gkg(
            p,
            row["dewpoint_c"],
        )

        if q_gkg is None:
            continue

        samples.append(
            (p, q_gkg / 1000.0, u, v)
        )

    if len(samples) < 8:
        return None

    # Deduplicate by pressure.
    dedup = {}

    for p, q, u, v in samples:
        dedup[round(p, 2)] = (p, q, u, v)

    samples = list(dedup.values())

    # Ascending pressure = top -> bottom, positive dp.
    samples.sort(key=lambda x: x[0])

    top_p = samples[0][0]
    bottom_p = samples[-1][0]

    if surface_pressure - bottom_p > 75:
        return None

    if top_p > 300:
        return None

    p_pa = np.array(
        [x[0] * 100.0 for x in samples],
        dtype=float,
    )
    q = np.array([x[1] for x in samples], dtype=float)
    u = np.array([x[2] for x in samples], dtype=float)
    v = np.array([x[3] for x in samples], dtype=float)

    try:
        integrate = getattr(np, "trapezoid", np.trapz)

        g = 9.80665

        ivt_u = float(
            integrate(q * u, p_pa) / g
        )
        ivt_v = float(
            integrate(q * v, p_pa) / g
        )

        magnitude = math.hypot(ivt_u, ivt_v)

        if not math.isfinite(magnitude):
            return None

        if magnitude < 0 or magnitude > 2500:
            return None

        return magnitude

    except Exception:
        return None


# ============================================================
# GEOPOTENCIAL / THICKNESS
# ============================================================

def geopotential_height_at_pressure(levels, target_pressure):
    value = interpolate_pressure(
        levels,
        "height_m",
        target_pressure,
    )

    if value is None:
        return None

    if not math.isfinite(value):
        return None

    return value


def thickness(levels, bottom_pressure, top_pressure):
    z_bottom = geopotential_height_at_pressure(
        levels,
        bottom_pressure,
    )

    z_top = geopotential_height_at_pressure(
        levels,
        top_pressure,
    )

    if z_bottom is None or z_top is None:
        return None

    value = z_top - z_bottom

    if not math.isfinite(value) or value <= 0:
        return None

    return value


# ============================================================
# PARAMETRI POSAMEZNE SONDAŽE
# ============================================================

def calculate_profile_parameters(profile):
    levels = profile["levels"]

    result = {
        "date": profile["date"],
        "hour": profile["hour"],
    }

    # Temperature
    result["t850"] = interpolate_pressure(
        levels,
        "temperature_c",
        850,
    )
    result["t700"] = interpolate_pressure(
        levels,
        "temperature_c",
        700,
    )
    result["t500"] = interpolate_pressure(
        levels,
        "temperature_c",
        500,
    )

    # Moisture / stability
    result["pwat_mm"] = calculate_pwat(levels)
    result["freezing_level_msl_m"] = freezing_level(levels)

    result["lapse_rate_850_500_c_per_km"] = pressure_lapse_rate(
        levels,
        850,
        500,
    )
    result["lapse_rate_700_500_c_per_km"] = pressure_lapse_rate(
        levels,
        700,
        500,
    )

    result["q_surface_gkg"] = q_surface(levels)
    result["q925_gkg"] = q_at_pressure(levels, 925)
    result["q850_gkg"] = q_at_pressure(levels, 850)
    result["mucape_jkg"] = calculate_mucape(levels)

    # Integrated moisture transport
    result["ivt_kg_m_s"] = calculate_ivt(levels)

    # Standard-level wind speed
    for level in STANDARD_WIND_LEVELS:
        result[f"wind_speed_{level}_ms"] = wind_speed_at_pressure(
            levels,
            level,
        )

    # Bulk shear
    result["shear_0_1km_ms"] = bulk_shear(levels, 1000)
    result["shear_0_3km_ms"] = bulk_shear(levels, 3000)
    result["shear_0_6km_ms"] = bulk_shear(levels, 6000)

    # Robust historical lower-tropospheric analogue.
    # Kept separate from true 0–3 km AGL shear.
    result["shear_sfc_700_ms"] = surface_to_pressure_shear(
        levels,
        700,
    )

    # Synoptic / thickness
    result["z500_m"] = geopotential_height_at_pressure(levels, 500)

    # 1000 hPa can be below ground at Ljubljana; in that case interpolation
    # naturally returns None. 925–500 hPa is kept as the robust local analogue.
    result["thickness_1000_500_m"] = thickness(
        levels,
        1000,
        500,
    )
    result["thickness_925_500_m"] = thickness(
        levels,
        925,
        500,
    )

    return result


# ============================================================
# DEDUPLIKACIJA
# ============================================================

def deduplicate_daily(records):
    """
    Največ ena reprezentativna vrednost posameznega parametra
    na koledarski dan. Če je profilov več, uporabimo mediano.
    """

    grouped = defaultdict(list)

    for record in records:
        grouped[record["date"]].append(record)

    output = []

    for day, day_records in sorted(grouped.items()):
        result = {"date": day}

        for parameter in PARAMETERS:
            values = [
                r[parameter]
                for r in day_records
                if valid_number(r.get(parameter))
            ]

            result[parameter] = (
                float(np.median(values))
                if values
                else None
            )

        output.append(result)

    return output


# ============================================================
# KOLEDARSKA RAZDALJA
# ============================================================

def climatological_day(month, day):
    return date(2000, month, day)


def circular_day_distance(d1, d2):
    delta = abs((d1 - d2).days)
    return min(delta, 366 - delta)


# ============================================================
# STATISTIKA
# ============================================================

def percentile_statistics(values):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]

    if len(values) == 0:
        return None

    percentile_levels = [
        1, 10, 25, 50, 75, 90, 95, 99
    ]

    percentiles = np.percentile(
        values,
        percentile_levels,
    )

    output = {
        "n": int(len(values)),
        "min": round(float(np.min(values)), 2),
        "max": round(float(np.max(values)), 2),
        "mean": round(float(np.mean(values)), 2),
        "distribution": [
            round(float(value), 4)
            for value in np.sort(values)
        ],
    }

    for level, value in zip(
        percentile_levels,
        percentiles,
    ):
        output[f"p{level}"] = round(
            float(value),
            2,
        )

    return output


def mucape_statistics(values):
    stats = percentile_statistics(values)

    if stats is None:
        return None

    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]

    if len(arr) == 0:
        return None

    stats.update({
        "fraction_gt_0": round(float(np.mean(arr > 0)), 4),
        "fraction_gt_100": round(float(np.mean(arr > 100)), 4),
        "fraction_gt_500": round(float(np.mean(arr > 500)), 4),
        "fraction_gt_1000": round(float(np.mean(arr > 1000)), 4),
        "count_gt_100": int(np.sum(arr > 100)),
        "count_gt_500": int(np.sum(arr > 500)),
        "count_gt_1000": int(np.sum(arr > 1000)),
    })

    return stats


def extreme_dates(pool, parameter):
    valid = [
        r for r in pool
        if valid_number(r.get(parameter))
    ]

    if not valid:
        return {
            "min_date": None,
            "max_date": None,
        }

    minimum = min(
        valid,
        key=lambda r: r[parameter],
    )

    maximum = max(
        valid,
        key=lambda r: r[parameter],
    )

    return {
        "min_date": minimum["date"].isoformat(),
        "max_date": maximum["date"].isoformat(),
    }


# ============================================================
# DNEVNA KLIMATOLOGIJA
# ============================================================

def build_daily_climatology(records):
    result = {}
    start = date(2000, 1, 1)

    for day_number in range(366):
        target = start + timedelta(days=day_number)
        key = target.strftime("%m-%d")

        pool = []

        for record in records:
            record_day = climatological_day(
                record["date"].month,
                record["date"].day,
            )

            distance = circular_day_distance(
                target,
                record_day,
            )

            if distance <= WINDOW_DAYS:
                pool.append(record)

        result[key] = {}

        for parameter in PARAMETERS:
            values = [
                r[parameter]
                for r in pool
                if valid_number(r.get(parameter))
            ]

            if parameter == "mucape_jkg":
                stats = mucape_statistics(values)
            else:
                stats = percentile_statistics(values)

            if stats is None:
                continue

            stats.update(
                extreme_dates(
                    pool,
                    parameter,
                )
            )

            result[key][parameter] = stats

    return result


# ============================================================
# 10-DNEVNO GLAJENJE
# ============================================================

def smooth_climatology(climatology):
    keys = list(climatology.keys())
    n_days = len(keys)

    percentile_names = [
        "p1",
        "p10",
        "p25",
        "p50",
        "p75",
        "p90",
        "p95",
        "p99",
    ]

    mucape_fraction_names = [
        "fraction_gt_0",
        "fraction_gt_100",
        "fraction_gt_500",
        "fraction_gt_1000",
    ]

    # 10-dnevno centrirano glajenje:
    # 5 dni nazaj + tekoči dan + 4 dni naprej.
    offsets = list(range(-5, 5))

    smoothed = {}

    for i, key in enumerate(keys):
        smoothed[key] = {}

        for parameter in PARAMETERS:
            if parameter not in climatology[key]:
                continue

            original = climatology[key][parameter]
            output = dict(original)

            fields_to_smooth = list(percentile_names)

            if parameter == "mucape_jkg":
                fields_to_smooth += mucape_fraction_names

            for field in fields_to_smooth:
                values = []

                for offset in offsets:
                    j = (i + offset) % n_days
                    other_key = keys[j]

                    other = (
                        climatology
                        .get(other_key, {})
                        .get(parameter)
                    )

                    if (
                        other is not None
                        and valid_number(other.get(field))
                    ):
                        values.append(other[field])

                if values:
                    decimals = 4 if field.startswith("fraction_") else 2

                    output[field] = round(
                        float(np.mean(values)),
                        decimals,
                    )

            # n, distribution, min/max, mean, counts and extreme dates
            # remain unsmoothed.
            smoothed[key][parameter] = output

    return smoothed


# ============================================================
# QC POVZETEK
# ============================================================

def print_qc(records):
    print()
    print("=" * 72)
    print("CLIMATOLOGY QC")
    print("=" * 72)
    print("Daily records:", len(records))

    for parameter in PARAMETERS:
        valid = [
            r for r in records
            if valid_number(r.get(parameter))
        ]

        print()
        print(parameter)
        print("  valid:", len(valid))

        if valid:
            minimum = min(
                valid,
                key=lambda r: r[parameter],
            )
            maximum = max(
                valid,
                key=lambda r: r[parameter],
            )

            print(
                "  min:",
                round(float(minimum[parameter]), 2),
                minimum["date"],
            )
            print(
                "  max:",
                round(float(maximum[parameter]), 2),
                maximum["date"],
            )

    # Primerjava strogega 0–3 km AGL striga z robustnejšim
    # surface–700 hPa strigom.
    shear_pairs = [
        (
            float(r["shear_0_3km_ms"]),
            float(r["shear_sfc_700_ms"]),
        )
        for r in records
        if valid_number(r.get("shear_0_3km_ms"))
        and valid_number(r.get("shear_sfc_700_ms"))
    ]

    print()
    print("=" * 72)
    print("SHEAR QC COMPARISON")
    print("=" * 72)

    n_03 = sum(
        1 for r in records
        if valid_number(r.get("shear_0_3km_ms"))
    )
    n_sfc700 = sum(
        1 for r in records
        if valid_number(r.get("shear_sfc_700_ms"))
    )

    print("  0–3 km valid:", n_03)
    print("  surface–700 hPa valid:", n_sfc700)
    print("  overlap:", len(shear_pairs))

    if shear_pairs:
        arr = np.asarray(shear_pairs, dtype=float)
        differences = arr[:, 1] - arr[:, 0]

        print(
            "  mean(sfc–700 minus 0–3 km):",
            round(float(np.mean(differences)), 2),
            "m/s",
        )
        print(
            "  median difference:",
            round(float(np.median(differences)), 2),
            "m/s",
        )

        p10, p50, p90 = np.percentile(differences, [10, 50, 90])
        print(
            "  difference P10/P50/P90:",
            round(float(p10), 2),
            "/",
            round(float(p50), 2),
            "/",
            round(float(p90), 2),
            "m/s",
        )

        if (
            len(shear_pairs) >= 2
            and np.std(arr[:, 0]) > 0
            and np.std(arr[:, 1]) > 0
        ):
            correlation = np.corrcoef(arr[:, 0], arr[:, 1])[0, 1]
            print(
                "  correlation:",
                round(float(correlation), 3),
            )

    # Poseben MUCAPE pregled.
    cape = np.asarray(
        [
            r["mucape_jkg"]
            for r in records
            if valid_number(r.get("mucape_jkg"))
        ],
        dtype=float,
    )

    if len(cape):
        print()
        print("=" * 72)
        print("MUCAPE QC")
        print("=" * 72)
        print("  valid:", len(cape))
        print("  > 0 J/kg:", round(float(np.mean(cape > 0)) * 100, 1), "%")
        print("  > 100 J/kg:", round(float(np.mean(cape > 100)) * 100, 1), "%")
        print("  > 500 J/kg:", round(float(np.mean(cape > 500)) * 100, 1), "%")
        print("  > 1000 J/kg:", round(float(np.mean(cape > 1000)) * 100, 1), "%")

    print()
    print("=" * 72)


# ============================================================
# GLAVNI PROGRAM
# ============================================================

def main():
    raw_zip = download(IGRA_URL)

    print(
        "Downloaded:",
        round(len(raw_zip) / 1024 / 1024, 2),
        "MB",
    )

    with zipfile.ZipFile(
        io.BytesIO(raw_zip)
    ) as archive:
        names = archive.namelist()

        if not names:
            raise RuntimeError("IGRA ZIP is empty.")

        filename = names[0]
        print("Reading:", filename)

        text = (
            archive
            .read(filename)
            .decode("ascii", errors="ignore")
        )

    profiles = read_profiles(text)

    print(
        f"Profiles {START_YEAR}-{END_YEAR}:",
        len(profiles),
    )

    calculated = []
    total = len(profiles)

    for i, profile in enumerate(profiles, start=1):
        if i == 1 or i % 500 == 0 or i == total:
            print(f"Processing {i}/{total}...")

        calculated.append(
            calculate_profile_parameters(profile)
        )

    daily = deduplicate_daily(calculated)

    print_qc(daily)

    print()
    print(
        f"Building ±{WINDOW_DAYS}-day climatological windows..."
    )

    raw_climatology = build_daily_climatology(daily)

    print(
        f"Applying {SMOOTHING_DAYS}-day smoothing..."
    )

    climatology = smooth_climatology(
        raw_climatology
    )

    output = {
        "metadata": {
            "schema_version": 2,
            "station_id": STATION_ID,
            "station_name": STATION_NAME,
            "reference_period": f"{START_YEAR}-{END_YEAR}",
            "window_days": WINDOW_DAYS,
            "window_description": f"±{WINDOW_DAYS} calendar days",
            "smoothing_days": SMOOTHING_DAYS,
            "smoothing_description": (
                "10-day moving mean of percentile curves; "
                "extremes, raw distributions, means, counts and n are not smoothed."
            ),
            "distribution_description": (
                "Sorted unsmoothed empirical values from the climatological window; "
                "used for exact empirical percentile ranks."
            ),
            "daily_deduplication": (
                "Median of profiles for the same calendar date."
            ),
            "historical_time_caveat": (
                "Most historical Ljubljana soundings were nominally 06 UTC "
                "and are not fully time-equivalent to the current 00/12 UTC schedule."
            ),
            "pwat_method": (
                "MetPy precipitable_water from the IGRA dewpoint profile."
            ),
            "pwat_qc": (
                "Dewpoint profile must begin within 50 hPa of the sounding bottom "
                "and extend to at least 300 hPa."
            ),
            "specific_humidity_method": (
                "MetPy specific_humidity_from_dewpoint."
            ),
            "wind_method": (
                "IGRA wind direction/speed converted to u/v; standard-level wind "
                "speed is derived after log-pressure interpolation of u and v."
            ),
            "shear_method": (
                "Vector bulk shear from the lowest valid wind level to "
                "1, 3 and 6 km AGL using linear interpolation in height. "
                "Surface–700 hPa shear is stored separately and uses "
                "log-pressure interpolation of u/v at 700 hPa."
            ),
            "ivt_method": (
                "Magnitude of 1/g integral(q*V dp), using IGRA dewpoint levels "
                "and pressure-interpolated wind components."
            ),
            "mucape_method": (
                "MetPy most_unstable_cape_cin from common P/T/Td levels. "
                "Profile must begin within 50 hPa of sounding bottom and reach 300 hPa."
            ),
            "mucape_distribution_note": (
                "Because MUCAPE has a large point mass at zero, occurrence fractions "
                "above 0, 100, 500 and 1000 J/kg are stored in addition to percentiles."
            ),
            "thickness_note": (
                "1000–500 hPa thickness is only produced when 1000 hPa is physically "
                "available in the observed profile. 925–500 hPa thickness is included "
                "as a more robust Ljubljana observational analogue."
            ),
            "parameter_metadata": PARAMETER_METADATA,
            "created_utc": (
                datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            ),
        },
        "daily": climatology,
    }

    os.makedirs(
        os.path.dirname(OUTPUT_FILE),
        exist_ok=True,
    )

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            output,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print()
    print("=" * 72)
    print("Saved:", OUTPUT_FILE)
    print("=" * 72)

    # Kratek testni izpis.
    sample = climatology.get("09-21", {})

    print()
    print("Example climatology for 21 September:")

    for parameter, stats in sample.items():
        print()
        print(parameter)
        print("  N:", stats.get("n"))
        print("  P10:", stats.get("p10"))
        print("  P50:", stats.get("p50"))
        print("  P90:", stats.get("p90"))

        if parameter == "mucape_jkg":
            print("  P95:", stats.get("p95"))
            print("  >100:", stats.get("fraction_gt_100"))
            print("  >500:", stats.get("fraction_gt_500"))
            print("  >1000:", stats.get("fraction_gt_1000"))

        print(
            "  MIN:",
            stats.get("min"),
            stats.get("min_date"),
        )
        print(
            "  MAX:",
            stats.get("max"),
            stats.get("max_date"),
        )


if __name__ == "__main__":
    main()
