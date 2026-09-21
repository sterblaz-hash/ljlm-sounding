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

                # --------------------------------------------
                # ČAS
                # --------------------------------------------

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

                # --------------------------------------------
                # PROFILNI NIZI
                # --------------------------------------------

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

                # DWD BUFR ima v tem primeru eno dodatno
                # osamljeno tlačno vrednost na koncu.
                #
                # Profil omejimo na skupno dolžino dejanskih
                # meteoroloških profilnih spremenljivk.

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

                    # ----------------------------------------
                    # Relativna vlaga iz T in Td
                    # ----------------------------------------

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

                    level = {
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

                    levels.append(level)

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
# VREDNOST NA DOLOČENEM TLAKU
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


# ============================================================
# METPY TERMO PROFIL
# ============================================================

def prepare_metpy_profile(levels):

    rows = []

    for level in levels:

        if (
            level["pressure_hpa"] is None
            or level["temperature_c"] is None
            or level["dewpoint_c"] is None
            or level["height_m"] is None
        ):
            continue

        rows.append(level)

    if len(rows) < 10:
        return None

    # Profil mora biti od visokega proti nizkemu tlaku.
    rows.sort(
        key=lambda x: x["pressure_hpa"],
        reverse=True
    )

    pressure = np.array(
        [
            x["pressure_hpa"]
            for x in rows
        ],
        dtype=float
    ) * units.hPa

    temperature = np.array(
        [
            x["temperature_c"]
            for x in rows
        ],
        dtype=float
    ) * units.degC

    dewpoint = np.array(
        [
            x["dewpoint_c"]
            for x in rows
        ],
        dtype=float
    ) * units.degC

    height = np.array(
        [
            x["height_m"]
            for x in rows
        ],
        dtype=float
    ) * units.meter

    return {
        "rows": rows,
        "pressure": pressure,
        "temperature": temperature,
        "dewpoint": dewpoint,
        "height": height,
    }


# ============================================================
# VETERNI PROFIL
# ============================================================

def prepare_wind_profile(levels):

    rows = []

    for level in levels:

        if (
            level["pressure_hpa"] is None
            or level["height_m"] is None
            or level["wind_speed_ms"] is None
            or level["wind_direction_deg"] is None
        ):
            continue

        rows.append(level)

    if len(rows) < 2:
        return None

    rows.sort(
        key=lambda x: x["pressure_hpa"],
        reverse=True
    )

    pressure = np.array(
        [
            x["pressure_hpa"]
            for x in rows
        ],
        dtype=float
    ) * units.hPa

    height = np.array(
        [
            x["height_m"]
            for x in rows
        ],
        dtype=float
    ) * units.meter

    speed = np.array(
        [
            x["wind_speed_ms"]
            for x in rows
        ],
        dtype=float
    ) * units("m/s")

    direction = np.array(
        [
            x["wind_direction_deg"]
            for x in rows
        ],
        dtype=float
    ) * units.degree

    u, v = mpcalc.wind_components(
        speed,
        direction
    )

    return {
        "pressure": pressure,
        "height": height,
        "u": u,
        "v": v,
    }


# ============================================================
# VARNO IZVAJANJE METPY
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
            quantity.magnitude
        )

        if not math.isfinite(value):
            return None

        return round(
            value,
            digits
        )

    except Exception:
        return None


# ============================================================
# METPY PARAMETRI
# ============================================================

def calculate_metpy_parameters(levels):

    result = {}

    thermo = prepare_metpy_profile(
        levels
    )

    if thermo is None:
        return result

    p = thermo["pressure"]
    t = thermo["temperature"]
    td = thermo["dewpoint"]
    z = thermo["height"]

    # --------------------------------------------------------
    # PWAT
    # --------------------------------------------------------

    pwat = safe_parameter(
        "PWAT",
        lambda:
            mpcalc.precipitable_water(
                p,
                td
            )
    )

    result["pwat_mm"] = (
        quantity_value(
            pwat,
            "millimeter"
        )
    )

    # --------------------------------------------------------
    # LCL
    # --------------------------------------------------------

    lcl = safe_parameter(
        "LCL",
        lambda:
            mpcalc.lcl(
                p[0],
                t[0],
                td[0]
            )
    )

    if lcl is not None:

        lcl_p, lcl_t = lcl

        result["lcl_pressure_hpa"] = (
            quantity_value(
                lcl_p,
                "hPa"
            )
        )

        result["lcl_temperature_c"] = (
            quantity_value(
                lcl_t,
                "degC"
            )
        )

    # --------------------------------------------------------
    # LFC
    # --------------------------------------------------------

    lfc = safe_parameter(
        "LFC",
        lambda:
            mpcalc.lfc(
                p,
                t,
                td
            )
    )

    if lfc is not None:

        lfc_p, lfc_t = lfc

        result["lfc_pressure_hpa"] = (
            quantity_value(
                lfc_p,
                "hPa"
            )
        )

        result["lfc_temperature_c"] = (
            quantity_value(
                lfc_t,
                "degC"
            )
        )

    # --------------------------------------------------------
    # EL
    # --------------------------------------------------------

    el = safe_parameter(
        "EL",
        lambda:
            mpcalc.el(
                p,
                t,
                td
            )
    )

    if el is not None:

        el_p, el_t = el

        result["el_pressure_hpa"] = (
            quantity_value(
                el_p,
                "hPa"
            )
        )

        result["el_temperature_c"] = (
            quantity_value(
                el_t,
                "degC"
            )
        )

    # --------------------------------------------------------
    # SURFACE-BASED CAPE / CIN
    # --------------------------------------------------------

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

        result["sbcape_jkg"] = (
            quantity_value(
                cape,
                "joule / kilogram"
            )
        )

        result["sbcin_jkg"] = (
            quantity_value(
                cin,
                "joule / kilogram"
            )
        )

    # --------------------------------------------------------
    # NIČTA IZOTERMA
    #
    # Poiščemo prvo spremembo T iz >= 0 na < 0 pri vzpenjanju.
    # --------------------------------------------------------

    freezing_level = None

    for i in range(
        len(t) - 1
    ):

        t1 = (
            t[i]
            .to("degC")
            .magnitude
        )

        t2 = (
            t[i + 1]
            .to("degC")
            .magnitude
        )

        z1 = (
            z[i]
            .to("meter")
            .magnitude
        )

        z2 = (
            z[i + 1]
            .to("meter")
            .magnitude
        )

        if (
            t1 >= 0
            and t2 < 0
        ):

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

    result["freezing_level_m"] = (
        round(
            float(freezing_level),
            0
        )
        if freezing_level is not None
        else None
    )

    # --------------------------------------------------------
    # VETROVNI STRIG
    # --------------------------------------------------------

    wind = prepare_wind_profile(
        levels
    )

    if wind is not None:

        wp = wind["pressure"]
        wz = wind["height"]
        u = wind["u"]
        v = wind["v"]

        surface_height = wz[0]

        for depth_km in [1, 3, 6]:

            shear = safe_parameter(
                f"{depth_km} km shear",
                lambda depth_km=depth_km:
                    mpcalc.bulk_shear(
                        wp,
                        u,
                        v,
                        height=wz,
                        bottom=surface_height,
                        depth=(
                            depth_km
                            * units.kilometer
                        )
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
# TERMIN SONDAŽE
# ============================================================

def determine_term(launch_time):

    # Ljubljana 00 UTC je lahko dejansko izpuščena
    # že prejšnji koledarski dan okoli 23:30 UTC.

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
# SHRANJEVANJE
# ============================================================

def save_json(
    profile,
    source_file
):

    launch = datetime.fromisoformat(
        profile["launch_time"].replace(
            "Z",
            "+00:00"
        )
    )

    term, nominal_date = (
        determine_term(launch)
    )

    # --------------------------------------------------------
    # Standardni nivoji
    # --------------------------------------------------------

    standard_levels = {}

    for pressure in [
        850,
        700,
        500
    ]:

        standard_levels[
            f"t{pressure}"
        ] = value_at_pressure(
            profile["levels"],
            "temperature_c",
            pressure
        )

    standard_levels["td850"] = (
        value_at_pressure(
            profile["levels"],
            "dewpoint_c",
            850
        )
    )

    standard_levels["rh850"] = (
        value_at_pressure(
            profile["levels"],
            "relative_humidity_pct",
            850
        )
    )

    profile["source"] = (
        "DWD Open Data BUFR"
    )

    profile["source_file"] = (
        unquote(source_file)
    )

    profile["retrieved_at"] = (
        datetime.now(
            timezone.utc
        )
        .isoformat()
        .replace("+00:00", "Z")
    )

    profile["term"] = term

    profile["nominal_date"] = (
        nominal_date.isoformat()
    )

    profile["parameters"] = {
        "standard_levels":
            standard_levels,

        "metpy":
            calculate_metpy_parameters(
                profile["levels"]
            ),
    }

    # --------------------------------------------------------
    # latest.json
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # ARHIV
    # --------------------------------------------------------

    archive_dir = os.path.join(
        "data",
        f"{nominal_date.year:04d}",
        f"{nominal_date.month:02d}"
    )

    os.makedirs(
        archive_dir,
        exist_ok=True
    )

    archive_file = os.path.join(
        archive_dir,
        (
            f"{nominal_date:%Y%m%d}"
            f"_{term}.json"
        )
    )

    with open(
        archive_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            profile,
            f,
            ensure_ascii=False,
            indent=2
        )

    return archive_file


# ============================================================
# IZPIS REZULTATOV
# ============================================================

def print_results(profile):

    parameters = (
        profile["parameters"]
    )

    standard = (
        parameters[
            "standard_levels"
        ]
    )

    metpy = (
        parameters["metpy"]
    )

    print()
    print("=" * 60)
    print(
        "METEOROLOGICAL PARAMETERS"
    )
    print("=" * 60)

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
        "Td850:",
        standard.get("td850")
    )

    print(
        "RH850:",
        standard.get("rh850")
    )

    print(
        "PWAT:",
        metpy.get("pwat_mm"),
        "mm"
    )

    print(
        "Freezing level:",
        metpy.get(
            "freezing_level_m"
        ),
        "m"
    )

    print(
        "LCL:",
        metpy.get(
            "lcl_pressure_hpa"
        ),
        "hPa"
    )

    print(
        "LFC:",
        metpy.get(
            "lfc_pressure_hpa"
        ),
        "hPa"
    )

    print(
        "EL:",
        metpy.get(
            "el_pressure_hpa"
        ),
        "hPa"
    )

    print(
        "SBCAPE:",
        metpy.get(
            "sbcape_jkg"
        ),
        "J/kg"
    )

    print(
        "SBCIN:",
        metpy.get(
            "sbcin_jkg"
        ),
        "J/kg"
    )

    print(
        "0-1 km shear:",
        metpy.get(
            "shear_0_1km_ms"
        ),
        "m/s"
    )

    print(
        "0-3 km shear:",
        metpy.get(
            "shear_0_3km_ms"
        ),
        "m/s"
    )

    print(
        "0-6 km shear:",
        metpy.get(
            "shear_0_6km_ms"
        ),
        "m/s"
    )


# ============================================================
# GLAVNI PROGRAM
# ============================================================

def main():

    now = datetime.now(
        timezone.utc
    )

    print(
        "Current UTC time:",
        now.isoformat()
    )

    print()
    print(
        "TEST MODE:"
    )

    print(
        "The newest available Ljubljana "
        "sounding will be analysed."
    )

    print()
    print(
        "Searching DWD radiosonde "
        "BUFR packages..."
    )

    candidates = (
        find_candidate_files()
    )

    print(
        f"Found {len(candidates)} "
        "candidate packages."
    )

    for number, filename in enumerate(
        candidates,
        start=1
    ):

        print(
            f"[{number}/"
            f"{len(candidates)}] "
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

            profile = (
                read_ljlm_from_bufr(
                    tmp_path
                )
            )

            if profile is None:
                continue

            print()
            print(
                "LJUBLJANA 14015 FOUND"
            )

            print(
                "Launch:",
                profile["launch_time"]
            )

            print(
                "Profile levels:",
                len(
                    profile["levels"]
                )
            )

            print(
                "QC:",
                profile["qc"]
            )

            # --------------------------------------------
            # TESTNA VERZIJA:
            # ne preverjamo pričakovanega 00/12 termina.
            # Analiziramo najnovejšo najdeno sondažo.
            # --------------------------------------------

            archive_file = save_json(
                profile,
                filename
            )

            print_results(
                profile
            )

            print()
            print(
                "Nominal date:",
                profile["nominal_date"]
            )

            print(
                "Term:",
                profile["term"]
            )

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
                os.remove(
                    tmp_path
                )

    print()
    print(
        "LJLM 14015 was not found."
    )


if __name__ == "__main__":
    main()
