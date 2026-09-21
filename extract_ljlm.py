import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.parse import unquote

import numpy as np

from eccodes import (
    codes_bufr_new_from_file,
    codes_get,
    codes_get_array,
    codes_release,
    codes_set,
)

import metpy.calc as mpcalc
from metpy.units import units


# ============================================================
# NASTAVITVE
# ============================================================

DWD_URL = (
    "https://opendata.dwd.de/weather/"
    "weather_reports/radiosonde/bufr/"
)

WMO_BLOCK = 14
WMO_STATION = 15

OUTPUT_LATEST = "data/latest.json"
CLIMATOLOGY_FILE = "climatology/daily_climatology.json"

MAX_CANDIDATE_FILES = 250


# ============================================================
# PRENOS
# ============================================================

def download(url):
    req = Request(
        url,
        headers={"User-Agent": "ljlm-sounding/1.0"}
    )

    with urlopen(req, timeout=60) as response:
        return response.read()


def get_directory_listing():
    return download(DWD_URL).decode(
        "utf-8",
        errors="ignore"
    )


def find_candidate_files():
    html = get_directory_listing()

    files = re.findall(
        r'href="([^"]*temp_bufr[^"]*\.bin)"',
        html,
        flags=re.IGNORECASE
    )

    files = [
        f for f in files
        if "latest" not in f.lower()
    ]

    files = sorted(
        set(files),
        reverse=True
    )

    return files[:MAX_CANDIDATE_FILES]


# ============================================================
# BUFR POMOŽNE FUNKCIJE
# ============================================================

def safe_get(handle, key, default=None):
    try:
        return codes_get(handle, key)
    except Exception:
        return default


def safe_array(handle, key):
    try:
        return list(
            codes_get_array(handle, key)
        )
    except Exception:
        return []


def valid_number(value):
    try:
        return (
            value is not None
            and math.isfinite(float(value))
            and abs(float(value)) < 1e20
        )
    except Exception:
        return False


# ============================================================
# BRANJE LJUBLJANSKE SONDAŽE
# ============================================================

def read_ljlm_from_bufr(path):

    with open(path, "rb") as f:

        while True:

            handle = None

            try:
                handle = codes_bufr_new_from_file(f)
            except Exception:
                break

            if handle is None:
                break

            try:
                codes_set(handle, "unpack", 1)

                block = safe_get(
                    handle,
                    "blockNumber"
                )

                station = safe_get(
                    handle,
                    "stationNumber"
                )

                if (
                    block != WMO_BLOCK
                    or station != WMO_STATION
                ):
                    continue

                year = safe_get(handle, "year")
                month = safe_get(handle, "month")
                day = safe_get(handle, "day")
                hour = safe_get(handle, "hour", 0)
                minute = safe_get(handle, "minute", 0)

                launch_time = datetime(
                    int(year),
                    int(month),
                    int(day),
                    int(hour),
                    int(minute),
                    tzinfo=timezone.utc
                )

                latitude = safe_get(
                    handle,
                    "latitude"
                )

                longitude = safe_get(
                    handle,
                    "longitude"
                )

                pressure = safe_array(
                    handle,
                    "pressure"
                )

                temperature = safe_array(
                    handle,
                    "airTemperature"
                )

                dewpoint = safe_array(
                    handle,
                    "dewpointTemperature"
                )

                height = safe_array(
                    handle,
                    "nonCoordinateGeopotentialHeight"
                )

                wind_speed = safe_array(
                    handle,
                    "windSpeed"
                )

                wind_direction = safe_array(
                    handle,
                    "windDirection"
                )

                raw_counts = {
                    "pressure": len(pressure),
                    "temperature": len(temperature),
                    "dewpoint": len(dewpoint),
                    "height": len(height),
                    "wind_speed": len(wind_speed),
                    "wind_direction": len(wind_direction),
                }

                profile_lengths = [
                    len(temperature),
                    len(dewpoint),
                    len(height),
                    len(wind_speed),
                    len(wind_direction),
                ]

                profile_lengths = [
                    n for n in profile_lengths
                    if n > 0
                ]

                if not profile_lengths:
                    return None

                n = min(profile_lengths)

                levels = []

                for i in range(n):

                    if i >= len(pressure):
                        break

                    p = pressure[i]
                    t = temperature[i]
                    td = dewpoint[i]
                    z = height[i]
                    ws = wind_speed[i]
                    wd = wind_direction[i]

                    if not valid_number(p):
                        continue

                    p_hpa = float(p) / 100.0

                    if (
                        p_hpa <= 0
                        or p_hpa > 1100
                    ):
                        continue

                    t_c = (
                        float(t) - 273.15
                        if valid_number(t)
                        else None
                    )

                    td_c = (
                        float(td) - 273.15
                        if valid_number(td)
                        else None
                    )

                    rh_pct = None

                    if (
                        t_c is not None
                        and td_c is not None
                    ):
                        try:
                            rh = (
                                mpcalc.relative_humidity_from_dewpoint(
                                    t_c * units.degC,
                                    td_c * units.degC
                                )
                            )

                            rh_pct = float(
                                rh.to("percent").magnitude
                            )

                        except Exception:
                            rh_pct = None

                    levels.append(
                        {
                            "pressure_hpa":
                                round(p_hpa, 2),

                            "height_m":
                                round(float(z), 1)
                                if valid_number(z)
                                else None,

                            "temperature_c":
                                round(t_c, 2)
                                if t_c is not None
                                else None,

                            "dewpoint_c":
                                round(td_c, 2)
                                if td_c is not None
                                else None,

                            "relative_humidity_pct":
                                round(rh_pct, 1)
                                if rh_pct is not None
                                else None,

                            "wind_speed_ms":
                                round(float(ws), 2)
                                if valid_number(ws)
                                else None,

                            "wind_direction_deg":
                                round(float(wd), 1)
                                if valid_number(wd)
                                else None,
                        }
                    )

                levels.sort(
                    key=lambda x:
                        x["pressure_hpa"],
                    reverse=True
                )

                return {
                    "station": 14015,
                    "station_name": "Ljubljana",

                    "launch_time":
                        launch_time
                        .isoformat()
                        .replace("+00:00", "Z"),

                    "latitude": latitude,
                    "longitude": longitude,

                    "qc": {
                        "raw_counts": raw_counts,

                        "profile_level_count":
                            len(levels),

                        "discarded_unmatched_pressure_values":
                            max(
                                0,
                                len(pressure) - n
                            ),
                    },

                    "levels": levels,
                }

            except Exception as exc:
                print(
                    "BUFR message error:",
                    exc
                )

            finally:

                if handle is not None:
                    codes_release(handle)

    return None


# ============================================================
# TERMIN SONDAŽE
# ============================================================

def determine_term(launch_time):

    # 00 UTC sonda je lahko izpuščena
    # okoli 23:30 UTC prejšnjega dne.

    if launch_time.hour >= 18:

        term = "00"

        nominal_date = (
            launch_time
            + timedelta(days=1)
        ).date()

    elif launch_time.hour < 6:

        term = "00"
        nominal_date = launch_time.date()

    else:

        term = "12"
        nominal_date = launch_time.date()

    return term, nominal_date


# ============================================================
# KATERI TERMIN TRENUTNO IŠČEMO?
# ============================================================

def expected_term(now):

    # Po 18 UTC že iščemo naslednjo 00 UTC sondažo.
    # Od 06 do 18 UTC iščemo 12 UTC.
    # Med 00 in 06 UTC iščemo tekočo 00 UTC.

    if now.hour >= 18:

        return (
            "00",
            (now + timedelta(days=1)).date()
        )

    elif now.hour >= 6:

        return (
            "12",
            now.date()
        )

    else:

        return (
            "00",
            now.date()
        )


# ============================================================
# INTERPOLACIJA NA TLAČNI NIVO
# ============================================================

def value_at_pressure(
    levels,
    field,
    target
):

    usable = [
        x for x in levels
        if x.get(field) is not None
    ]

    exact = [
        x for x in usable
        if abs(
            x["pressure_hpa"] - target
        ) <= 0.1
    ]

    if exact:

        return {
            "value":
                exact[0][field],

            "method":
                "measured",

            "pressure_hpa":
                exact[0]["pressure_hpa"],
        }

    above = None
    below = None

    for level in usable:

        p = level["pressure_hpa"]

        if p > target:

            if (
                above is None
                or p < above["pressure_hpa"]
            ):
                above = level

        elif p < target:

            if (
                below is None
                or p > below["pressure_hpa"]
            ):
                below = level

    if (
        above is None
        or below is None
    ):
        return None

    p1 = above["pressure_hpa"]
    p2 = below["pressure_hpa"]

    if abs(p1 - p2) > 100:
        return None

    v1 = above[field]
    v2 = below[field]

    fraction = (
        math.log(target / p1)
        / math.log(p2 / p1)
    )

    value = (
        v1
        + fraction * (v2 - v1)
    )

    return {
        "value":
            round(value, 2),

        "method":
            "log_pressure_interpolation",

        "between_hpa":
            [p1, p2],
    }


def simple_pressure_value(
    levels,
    field,
    target
):

    result = value_at_pressure(
        levels,
        field,
        target
    )

    if result is None:
        return None

    return result["value"]


# ============================================================
# VIŠINA PRI TLAKU
# ============================================================

def height_at_pressure(
    pressure,
    height,
    target_pressure
):

    try:

        p = np.asarray(
            pressure.to("hPa").magnitude,
            dtype=float
        )

        z = np.asarray(
            height.to("meter").magnitude,
            dtype=float
        )

        target = float(
            target_pressure
            .to("hPa")
            .magnitude
        )

        for i in range(len(p) - 1):

            p1 = p[i]
            p2 = p[i + 1]

            if p1 >= target >= p2:

                if p1 == p2:
                    return float(z[i])

                fraction = (
                    math.log(target / p1)
                    / math.log(p2 / p1)
                )

                return float(
                    z[i]
                    + fraction
                    * (z[i + 1] - z[i])
                )

    except Exception:
        pass

    return None


# ============================================================
# TEMPERATURA NA VIŠINI
# ============================================================

def temperature_at_height(
    levels,
    target_height
):

    rows = [
        x for x in levels
        if (
            x["height_m"] is not None
            and x["temperature_c"] is not None
        )
    ]

    rows.sort(
        key=lambda x:
            x["height_m"]
    )

    for i in range(len(rows) - 1):

        z1 = rows[i]["height_m"]
        z2 = rows[i + 1]["height_m"]

        if z1 <= target_height <= z2:

            t1 = rows[i]["temperature_c"]
            t2 = rows[i + 1]["temperature_c"]

            if z2 == z1:
                return t1

            fraction = (
                (target_height - z1)
                / (z2 - z1)
            )

            return (
                t1
                + fraction
                * (t2 - t1)
            )

    return None


# ============================================================
# LAPSE RATE
# ============================================================

def lapse_rate_height_layer(
    levels,
    bottom_m,
    top_m
):

    t_bottom = temperature_at_height(
        levels,
        bottom_m
    )

    t_top = temperature_at_height(
        levels,
        top_m
    )

    if (
        t_bottom is None
        or t_top is None
        or top_m <= bottom_m
    ):
        return None

    depth_km = (
        top_m - bottom_m
    ) / 1000.0

    return round(
        (t_bottom - t_top)
        / depth_km,
        2
    )


def lapse_rate_pressure_layer(
    levels,
    bottom_pressure,
    top_pressure
):

    t_bottom = simple_pressure_value(
        levels,
        "temperature_c",
        bottom_pressure
    )

    t_top = simple_pressure_value(
        levels,
        "temperature_c",
        top_pressure
    )

    z_bottom = simple_pressure_value(
        levels,
        "height_m",
        bottom_pressure
    )

    z_top = simple_pressure_value(
        levels,
        "height_m",
        top_pressure
    )

    if (
        t_bottom is None
        or t_top is None
        or z_bottom is None
        or z_top is None
    ):
        return None

    depth_km = (
        z_top - z_bottom
    ) / 1000.0

    if depth_km <= 0:
        return None

    return round(
        (t_bottom - t_top)
        / depth_km,
        2
    )


# ============================================================
# METPY PROFIL
# ============================================================

def prepare_metpy_profile(levels):

    rows = [
        x for x in levels
        if (
            x["pressure_hpa"] is not None
            and x["temperature_c"] is not None
            and x["dewpoint_c"] is not None
            and x["height_m"] is not None
        )
    ]

    if len(rows) < 10:
        return None

    rows.sort(
        key=lambda x:
            x["pressure_hpa"],
        reverse=True
    )

    unique_rows = []
    seen = set()

    for row in rows:

        p = row["pressure_hpa"]

        if p in seen:
            continue

        seen.add(p)
        unique_rows.append(row)

    rows = unique_rows

    p = np.array(
        [x["pressure_hpa"] for x in rows]
    ) * units.hPa

    t = np.array(
        [x["temperature_c"] for x in rows]
    ) * units.degC

    td = np.array(
        [x["dewpoint_c"] for x in rows]
    ) * units.degC

    z = np.array(
        [x["height_m"] for x in rows]
    ) * units.meter

    return {
        "pressure": p,
        "temperature": t,
        "dewpoint": td,
        "height": z,
    }


# ============================================================
# VETROVNI PROFIL
# ============================================================

def prepare_wind_profile(levels):

    rows = [
        x for x in levels
        if (
            x["pressure_hpa"] is not None
            and x["height_m"] is not None
            and x["wind_speed_ms"] is not None
            and x["wind_direction_deg"] is not None
        )
    ]

    if len(rows) < 2:
        return None

    rows.sort(
        key=lambda x:
            x["pressure_hpa"],
        reverse=True
    )

    unique_rows = []
    seen = set()

    for row in rows:

        p = row["pressure_hpa"]

        if p in seen:
            continue

        seen.add(p)
        unique_rows.append(row)

    rows = unique_rows

    p = np.array(
        [x["pressure_hpa"] for x in rows]
    ) * units.hPa

    z = np.array(
        [x["height_m"] for x in rows]
    ) * units.meter

    speed = np.array(
        [x["wind_speed_ms"] for x in rows]
    ) * units("m/s")

    direction = np.array(
        [x["wind_direction_deg"] for x in rows]
    ) * units.degree

    u, v = mpcalc.wind_components(
        speed,
        direction
    )

    return {
        "pressure": p,
        "height": z,
        "u": u,
        "v": v,
    }


# ============================================================
# POMOŽNE METPY FUNKCIJE
# ============================================================

def safe_parameter(name, function):

    try:
        return function()

    except Exception as exc:

        print(
            f"MetPy warning ({name}):",
            exc
        )

        return None


def quantity_value(
    quantity,
    unit=None,
    digits=2
):

    if quantity is None:
        return None

    try:

        if unit is not None:
            quantity = quantity.to(unit)

        value = float(
            np.asarray(
                quantity.magnitude
            ).squeeze()
        )

        if not math.isfinite(value):
            return None

        return round(value, digits)

    except Exception:
        return None


def add_level_result(
    result,
    prefix,
    level_result,
    pressure_profile,
    height_profile,
    surface_height
):

    if level_result is None:
        return

    try:
        level_pressure, level_temperature = level_result
    except Exception:
        return

    p_hpa = quantity_value(
        level_pressure,
        "hPa"
    )

    t_c = quantity_value(
        level_temperature,
        "degC"
    )

    if p_hpa is None:
        return

    z_msl = height_at_pressure(
        pressure_profile,
        height_profile,
        level_pressure
    )

    z_agl = (
        z_msl - surface_height
        if z_msl is not None
        else None
    )

    result[
        f"{prefix}_pressure_hpa"
    ] = p_hpa

    result[
        f"{prefix}_temperature_c"
    ] = t_c

    result[
        f"{prefix}_height_msl_m"
    ] = (
        round(z_msl, 0)
        if z_msl is not None
        else None
    )

    result[
        f"{prefix}_height_agl_m"
    ] = (
        round(z_agl, 0)
        if z_agl is not None
        else None
    )


# ============================================================
# IZRAČUN PARAMETROV
# ============================================================

def calculate_metpy_parameters(levels):

    result = {}

    thermo = prepare_metpy_profile(levels)

    if thermo is None:
        return result

    p = thermo["pressure"]
    t = thermo["temperature"]
    td = thermo["dewpoint"]
    z = thermo["height"]

    surface_height = float(
        z[0].to("meter").magnitude
    )

    # PWAT
    pwat = safe_parameter(
        "PWAT",
        lambda:
            mpcalc.precipitable_water(
                p,
                td
            )
    )

    result["pwat_mm"] = quantity_value(
        pwat,
        "millimeter"
    )

    # LCL
    lcl = safe_parameter(
        "LCL",
        lambda:
            mpcalc.lcl(
                p[0],
                t[0],
                td[0]
            )
    )

    add_level_result(
        result,
        "lcl",
        lcl,
        p,
        z,
        surface_height
    )

    # Surface parcel
    parcel_profile = safe_parameter(
        "parcel profile",
        lambda:
            mpcalc.parcel_profile(
                p,
                t[0],
                td[0]
            )
    )

    # LFC
    lfc = (
        safe_parameter(
            "LFC",
            lambda:
                mpcalc.lfc(
                    p,
                    t,
                    td,
                    parcel_temperature_profile=
                        parcel_profile
                )
        )
        if parcel_profile is not None
        else None
    )

    add_level_result(
        result,
        "lfc",
        lfc,
        p,
        z,
        surface_height
    )

    # EL
    el = (
        safe_parameter(
            "EL",
            lambda:
                mpcalc.el(
                    p,
                    t,
                    td,
                    parcel_temperature_profile=
                        parcel_profile
                )
        )
        if parcel_profile is not None
        else None
    )

    add_level_result(
        result,
        "el",
        el,
        p,
        z,
        surface_height
    )

    # Lifted Index
    if parcel_profile is not None:

        li = safe_parameter(
            "Lifted Index",
            lambda:
                mpcalc.lifted_index(
                    p,
                    t,
                    parcel_profile
                )
        )

        result["lifted_index_c"] = (
            quantity_value(
                li,
                "delta_degC"
            )
        )

    # K-index
    k_index = safe_parameter(
        "K Index",
        lambda:
            mpcalc.k_index(
                p,
                t,
                td
            )
    )

    result["k_index_c"] = (
        quantity_value(
            k_index,
            "degC"
        )
    )

    # Total Totals
    total_totals = safe_parameter(
        "Total Totals",
        lambda:
            mpcalc.total_totals_index(
                p,
                t,
                td
            )
    )

    result["total_totals_c"] = (
        quantity_value(
            total_totals,
            "delta_degC"
        )
    )

    # SBCAPE/CIN
    sb = safe_parameter(
        "SBCAPE/CIN",
        lambda:
            mpcalc.surface_based_cape_cin(
                p,
                t,
                td
            )
    )

    if sb is not None:

        cape, cin = sb

        result["sbcape_jkg"] = quantity_value(
            cape,
            "joule / kilogram"
        )

        result["sbcin_jkg"] = quantity_value(
            cin,
            "joule / kilogram"
        )

    # MLCAPE/CIN
    ml = safe_parameter(
        "MLCAPE/CIN",
        lambda:
            mpcalc.mixed_layer_cape_cin(
                p,
                t,
                td,
                depth=100 * units.hPa
            )
    )

    if ml is not None:

        cape, cin = ml

        result["mlcape_jkg"] = quantity_value(
            cape,
            "joule / kilogram"
        )

        result["mlcin_jkg"] = quantity_value(
            cin,
            "joule / kilogram"
        )

    # MU parcela
    mu_parcel = safe_parameter(
        "Most Unstable Parcel",
        lambda:
            mpcalc.most_unstable_parcel(
                p,
                t,
                td,
                depth=300 * units.hPa
            )
    )

    if mu_parcel is not None:

        try:

            (
                mu_pressure,
                mu_temperature,
                mu_dewpoint,
                mu_index
            ) = mu_parcel

            result[
                "mu_parcel_pressure_hpa"
            ] = quantity_value(
                mu_pressure,
                "hPa"
            )

            result[
                "mu_parcel_temperature_c"
            ] = quantity_value(
                mu_temperature,
                "degC"
            )

            result[
                "mu_parcel_dewpoint_c"
            ] = quantity_value(
                mu_dewpoint,
                "degC"
            )

            mu_height = height_at_pressure(
                p,
                z,
                mu_pressure
            )

            result[
                "mu_parcel_height_msl_m"
            ] = (
                round(mu_height, 0)
                if mu_height is not None
                else None
            )

            result[
                "mu_parcel_height_agl_m"
            ] = (
                round(
                    mu_height
                    - surface_height,
                    0
                )
                if mu_height is not None
                else None
            )

        except Exception as exc:

            print(
                "MetPy warning "
                "(MU parcel details):",
                exc
            )

    # MUCAPE/CIN
    mu = safe_parameter(
        "MUCAPE/CIN",
        lambda:
            mpcalc.most_unstable_cape_cin(
                p,
                t,
                td,
                depth=300 * units.hPa
            )
    )

    if mu is not None:

        cape, cin = mu

        result["mucape_jkg"] = quantity_value(
            cape,
            "joule / kilogram"
        )

        result["mucin_jkg"] = quantity_value(
            cin,
            "joule / kilogram"
        )

    # Freezing level
    freezing_level = None

    for i in range(len(t) - 1):

        t1 = float(
            t[i].to("degC").magnitude
        )

        t2 = float(
            t[i + 1].to("degC").magnitude
        )

        z1 = float(
            z[i].to("meter").magnitude
        )

        z2 = float(
            z[i + 1].to("meter").magnitude
        )

        if t1 >= 0 and t2 < 0:

            fraction = (
                (0 - t1)
                / (t2 - t1)
            )

            freezing_level = (
                z1
                + fraction
                * (z2 - z1)
            )

            break

    result[
        "freezing_level_msl_m"
    ] = (
        round(freezing_level, 0)
        if freezing_level is not None
        else None
    )

    result[
        "freezing_level_agl_m"
    ] = (
        round(
            freezing_level
            - surface_height,
            0
        )
        if freezing_level is not None
        else None
    )

    # Lapse rates
    result[
        "lapse_rate_0_3km_c_per_km"
    ] = lapse_rate_height_layer(
        levels,
        surface_height,
        surface_height + 3000
    )

    result[
        "lapse_rate_850_500_c_per_km"
    ] = lapse_rate_pressure_layer(
        levels,
        850,
        500
    )

    result[
        "lapse_rate_700_500_c_per_km"
    ] = lapse_rate_pressure_layer(
        levels,
        700,
        500
    )

    # Wind shear
    wind = prepare_wind_profile(levels)

    if wind is not None:

        wp = wind["pressure"]
        wz = wind["height"]
        u = wind["u"]
        v = wind["v"]

        bottom = wz[0]

        for depth_km in [1, 3, 6]:

            shear = safe_parameter(
                f"{depth_km} km shear",

                lambda depth_km=depth_km:
                    mpcalc.bulk_shear(
                        wp,
                        u,
                        v,
                        height=wz,
                        bottom=bottom,
                        depth=
                            depth_km
                            * units.kilometer
                    )
            )

            if shear is None:
                continue

            u_shear, v_shear = shear

            magnitude = np.sqrt(
                u_shear ** 2
                + v_shear ** 2
            )

            result[
                f"shear_0_{depth_km}km_ms"
            ] = quantity_value(
                magnitude,
                "m/s"
            )

    return result


# ============================================================
# ALI JE SONDAŽA ŽE SHRANJENA?
# ============================================================

def archive_path(
    nominal_date,
    term
):

    return os.path.join(
        "data",
        f"{nominal_date.year:04d}",
        f"{nominal_date.month:02d}",
        (
            f"{nominal_date:%Y%m%d}"
            f"_{term}.json"
        )
    )


def sounding_already_saved(
    nominal_date,
    term,
    launch_time
):

    path = archive_path(
        nominal_date,
        term
    )

    if not os.path.exists(path):
        return False

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:
            old = json.load(f)

        return (
            old.get("launch_time")
            == launch_time
        )

    except Exception:
        return False


# ============================================================
# KLIMATOLOŠKI PERCENTILI
# ============================================================

def empirical_percentile(value, distribution):
    """
    Empirični percentil glede na dejanske zgodovinske vrednosti.

    Pri izenačenih vrednostih uporabimo srednji rang:
    delež vrednosti pod aktualno vrednostjo + polovica deleža
    vrednosti, ki so ji enake.
    """

    if not valid_number(value):
        return None

    values = np.asarray(
        [
            float(x)
            for x in distribution
            if valid_number(x)
        ],
        dtype=float
    )

    if len(values) == 0:
        return None

    value = float(value)

    lower = np.sum(values < value)
    equal = np.sum(
        np.isclose(
            values,
            value,
            rtol=0.0,
            atol=1e-9
        )
    )

    percentile = (
        lower + 0.5 * equal
    ) / len(values) * 100.0

    return round(
        float(percentile),
        1
    )


def load_climatology_for_date(
    nominal_date
):
    """
    Prebere klimatologijo za koledarski dan nominalnega termina.
    """

    if not os.path.exists(
        CLIMATOLOGY_FILE
    ):
        print(
            "Climatology file not found:",
            CLIMATOLOGY_FILE
        )
        return None

    try:

        with open(
            CLIMATOLOGY_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            climatology = json.load(f)

        key = nominal_date.strftime(
            "%m-%d"
        )

        return (
            climatology
            .get("daily", {})
            .get(key)
        )

    except Exception as exc:

        print(
            "Climatology read warning:",
            exc
        )

        return None


def climatology_result(
    value,
    climatology_parameter
):
    """
    Sestavi rezultat aktualna vrednost + empirični percentil
    + glavne klimatološke referenčne vrednosti.
    """

    if (
        not valid_number(value)
        or climatology_parameter is None
    ):
        return None

    distribution = (
        climatology_parameter
        .get("distribution", [])
    )

    percentile = empirical_percentile(
        value,
        distribution
    )

    result = {
        "value": round(
            float(value),
            2
        ),

        "percentile":
            percentile,

        "n":
            climatology_parameter
            .get("n"),

        "p10":
            climatology_parameter
            .get("p10"),

        "p50":
            climatology_parameter
            .get("p50"),

        "p90":
            climatology_parameter
            .get("p90"),

        "p99":
            climatology_parameter
            .get("p99"),

        "historical_min":
            climatology_parameter
            .get("min"),

        "historical_min_date":
            climatology_parameter
            .get("min_date"),

        "historical_max":
            climatology_parameter
            .get("max"),

        "historical_max_date":
            climatology_parameter
            .get("max_date"),
    }

    return result


def calculate_climatology_comparison(
    standard_levels,
    metpy_parameters,
    nominal_date
):
    """
    Primerja aktualno sondažo z zgodovinsko klimatologijo
    za isti del leta (±15-dnevno okno).
    """

    day_climatology = (
        load_climatology_for_date(
            nominal_date
        )
    )

    if day_climatology is None:
        return {
            "available": False,
            "calendar_day":
                nominal_date.strftime(
                    "%m-%d"
                ),
        }

    current_values = {}

    for pressure in [850, 700, 500]:

        item = standard_levels.get(
            f"t{pressure}"
        )

        current_values[
            f"t{pressure}"
        ] = (
            item.get("value")
            if item is not None
            else None
        )

    current_values[
        "pwat_mm"
    ] = metpy_parameters.get(
        "pwat_mm"
    )

    current_values[
        "freezing_level_msl_m"
    ] = metpy_parameters.get(
        "freezing_level_msl_m"
    )

    current_values[
        "lapse_rate_850_500_c_per_km"
    ] = metpy_parameters.get(
        "lapse_rate_850_500_c_per_km"
    )

    current_values[
        "lapse_rate_700_500_c_per_km"
    ] = metpy_parameters.get(
        "lapse_rate_700_500_c_per_km"
    )

    parameters = {}

    for name, value in (
        current_values.items()
    ):

        result = climatology_result(
            value,
            day_climatology.get(name)
        )

        if result is not None:
            parameters[name] = result

    return {
        "available": True,

        "calendar_day":
            nominal_date.strftime(
                "%m-%d"
            ),

        "reference_period":
            "1996-2025",

        "window":
            "±15 calendar days",

        "percentile_method":
            "empirical_midrank",

        "historical_time_caveat":
            (
                "Most historical Ljubljana "
                "soundings were nominally 06 UTC "
                "and are not fully time-equivalent "
                "to the current 00/12 UTC schedule."
            ),

        "parameters":
            parameters,
    }


# ============================================================
# SHRANJEVANJE
# ============================================================

def save_json(
    profile,
    source_file,
    term,
    nominal_date
):

    standard_levels = {}

    for pressure in [850, 700, 500]:

        standard_levels[
            f"t{pressure}"
        ] = value_at_pressure(
            profile["levels"],
            "temperature_c",
            pressure
        )

        standard_levels[
            f"td{pressure}"
        ] = value_at_pressure(
            profile["levels"],
            "dewpoint_c",
            pressure
        )

        standard_levels[
            f"rh{pressure}"
        ] = value_at_pressure(
            profile["levels"],
            "relative_humidity_pct",
            pressure
        )

    profile["source"] = (
        "DWD Open Data BUFR"
    )

    profile["source_file"] = (
        unquote(source_file)
    )

    profile["retrieved_at"] = (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )

    profile["term"] = term

    profile["nominal_date"] = (
        nominal_date.isoformat()
    )

    metpy_parameters = (
        calculate_metpy_parameters(
            profile["levels"]
        )
    )

    climatology_comparison = (
        calculate_climatology_comparison(
            standard_levels,
            metpy_parameters,
            nominal_date
        )
    )

    profile["parameters"] = {
        "standard_levels":
            standard_levels,

        "metpy":
            metpy_parameters,

        "climatology":
            climatology_comparison,
    }

    os.makedirs(
        "data",
        exist_ok=True
    )

    with open(
        OUTPUT_LATEST,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            profile,
            f,
            ensure_ascii=False,
            indent=2
        )

    path = archive_path(
        nominal_date,
        term
    )

    os.makedirs(
        os.path.dirname(path),
        exist_ok=True
    )

    with open(
        path,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            profile,
            f,
            ensure_ascii=False,
            indent=2
        )

    return path


# ============================================================
# KRATEK IZPIS
# ============================================================

def print_summary(profile):

    p = profile["parameters"]
    standard = p["standard_levels"]
    metpy = p["metpy"]

    print()
    print("=" * 60)
    print("LJUBLJANA SOUNDING")
    print("=" * 60)

    print(
        "Launch:",
        profile["launch_time"]
    )

    print(
        "Nominal:",
        profile["nominal_date"],
        profile["term"],
        "UTC"
    )

    print(
        "Levels:",
        profile["qc"][
            "profile_level_count"
        ]
    )

    print()
    print(
        "T850:",
        standard.get("t850")
    )

    print(
        "T700:",
        standard.get("t700")
    )

    print(
        "T500:",
        standard.get("t500")
    )

    print(
        "PWAT:",
        metpy.get("pwat_mm"),
        "mm"
    )

    print(
        "MLCAPE:",
        metpy.get("mlcape_jkg"),
        "J/kg"
    )

    print(
        "MUCAPE:",
        metpy.get("mucape_jkg"),
        "J/kg"
    )

    print(
        "MU parcel:",
        metpy.get(
            "mu_parcel_pressure_hpa"
        ),
        "hPa /",
        metpy.get(
            "mu_parcel_height_agl_m"
        ),
        "m AGL"
    )

    print(
        "0-6 km shear:",
        metpy.get(
            "shear_0_6km_ms"
        ),
        "m/s"
    )

    climatology = p.get(
        "climatology",
        {}
    )

    if climatology.get("available"):

        print()
        print("--- CLIMATOLOGY ---")

        clim_parameters = (
            climatology.get(
                "parameters",
                {}
            )
        )

        for name in [
            "t850",
            "t700",
            "t500",
            "pwat_mm",
            "freezing_level_msl_m",
            "lapse_rate_850_500_c_per_km",
            "lapse_rate_700_500_c_per_km",
        ]:

            item = clim_parameters.get(
                name
            )

            if item is None:
                continue

            print(
                f"{name}:",
                item.get("value"),
                "| percentile:",
                item.get("percentile"),
                "| N:",
                item.get("n")
            )

    print("=" * 60)


# ============================================================
# GLAVNI PROGRAM
# ============================================================

def main():

    now = datetime.now(
        timezone.utc
    )

    wanted_term, wanted_date = (
        expected_term(now)
    )

    print(
        "Current UTC:",
        now.isoformat()
    )

    print(
        "Looking for:",
        wanted_date,
        wanted_term,
        "UTC"
    )

    candidates = find_candidate_files()

    print(
        "Candidate DWD packages:",
        len(candidates)
    )

    for number, filename in enumerate(
        candidates,
        start=1
    ):

        print(
            f"[{number}/{len(candidates)}] "
            f"{unquote(filename)}"
        )

        try:

            content = download(
                DWD_URL + filename
            )

        except Exception as exc:

            print(
                "Download failed:",
                exc
            )

            continue

        tmp_path = None

        try:

            with tempfile.NamedTemporaryFile(
                suffix=".bufr",
                delete=False
            ) as tmp:

                tmp.write(content)
                tmp_path = tmp.name

            profile = read_ljlm_from_bufr(
                tmp_path
            )

            if profile is None:
                continue

            launch = datetime.fromisoformat(
                profile[
                    "launch_time"
                ].replace(
                    "Z",
                    "+00:00"
                )
            )

            term, nominal_date = (
                determine_term(
                    launch
                )
            )

            print(
                "LJLM found:",
                profile["launch_time"],
                "->",
                nominal_date,
                term
            )

            # Ne sprejmemo stare sondaže.
            if (
                term != wanted_term
                or nominal_date != wanted_date
            ):

                print(
                    "Not the requested term."
                )

                continue

            # Ne zapisujemo iste sondaže ponovno.
            if sounding_already_saved(
                nominal_date,
                term,
                profile["launch_time"]
            ):

                print()
                print(
                    "Sounding already archived."
                )

                print(
                    "Nothing to update."
                )

                return

            archive_file = save_json(
                profile,
                filename,
                term,
                nominal_date
            )

            print_summary(profile)

            print()
            print(
                "Saved:",
                OUTPUT_LATEST
            )

            print(
                "Archive:",
                archive_file
            )

            return

        finally:

            if (
                tmp_path
                and os.path.exists(
                    tmp_path
                )
            ):
                os.remove(tmp_path)

    print()
    print(
        "Requested LJLM sounding "
        "is not available yet."
    )


if __name__ == "__main__":
    main()
