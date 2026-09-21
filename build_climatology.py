import io
import json
import math
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
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

OUTPUT_FILE = (
    "climatology/daily_climatology.json"
)

STATION_ID = "SIM00014015"
STATION_NAME = "Ljubljana / Bežigrad"


# ============================================================
# POMOŽNE FUNKCIJE
# ============================================================

def valid_number(value):
    try:
        return (
            value is not None
            and math.isfinite(float(value))
        )
    except Exception:
        return False


def download(url):
    print("Downloading IGRA archive...")

    req = Request(
        url,
        headers={
            "User-Agent": "ljlm-sounding/1.0"
        }
    )

    with urlopen(
        req,
        timeout=120
    ) as response:
        return response.read()


# ============================================================
# IGRA PARSER
# ============================================================

def parse_header(line):
    """
    IGRA v2 profile header.

    Returns only the fields that we need.
    """

    if not line.startswith("#"):
        return None

    try:
        station = line[1:12].strip()

        year = int(line[13:17])
        month = int(line[18:20])
        day = int(line[21:23])
        hour = int(line[24:26])

        num_levels = int(
            line[32:36]
        )

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
    Parse one IGRA level.

    Relevant fixed-width fields:

    pressure:
        columns 10-15
        Pa

    geopotential height:
        columns 17-21
        metres

    temperature:
        columns 23-27
        tenths degC

    dewpoint depression:
        columns 35-39
        tenths degC

    Missing values are normally -9999 / -8888.
    """

    try:

        pressure_raw = int(
            line[9:15]
        )

        height_raw = int(
            line[16:21]
        )

        temperature_raw = int(
            line[22:27]
        )

        dewpoint_dep_raw = int(
            line[34:39]
        )

    except Exception:
        return None

    pressure = None
    height = None
    temperature = None
    dewpoint = None

    if pressure_raw > 0:
        pressure = (
            pressure_raw / 100.0
        )

    if height_raw not in (
        -9999,
        -8888
    ):
        height = float(height_raw)

    if temperature_raw not in (
        -9999,
        -8888
    ):
        temperature = (
            temperature_raw / 10.0
        )

    if (
        temperature is not None
        and dewpoint_dep_raw
        not in (-9999, -8888)
    ):
        dewpoint = (
            temperature
            - dewpoint_dep_raw / 10.0
        )

    if pressure is None:
        return None

    if (
        pressure <= 0
        or pressure > 1100
    ):
        return None

    return {
        "pressure_hpa": pressure,
        "height_m": height,
        "temperature_c": temperature,
        "dewpoint_c": dewpoint,
    }


# ============================================================
# BRANJE VSEH PROFILOV
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

        level_lines = lines[
            i + 1:
            i + 1 + n
        ]

        i += n + 1

        if header["station"] != STATION_ID:
            continue

        year = header["year"]

        if (
            year < START_YEAR
            or year > END_YEAR
        ):
            continue

        try:
            profile_date = date(
                year,
                header["month"],
                header["day"]
            )
        except ValueError:
            continue

        levels = []

        for level_line in level_lines:

            level = parse_level(
                level_line
            )

            if level is not None:
                levels.append(level)

        if len(levels) < 10:
            continue

        levels.sort(
            key=lambda x:
                x["pressure_hpa"],
            reverse=True
        )

        profiles.append(
            {
                "date": profile_date,
                "hour": header["hour"],
                "levels": levels,
            }
        )

    return profiles


# ============================================================
# INTERPOLACIJA NA TLAČNI NIVO
# ============================================================

def interpolate_pressure(
    levels,
    field,
    target_pressure
):

    usable = [
        x for x in levels
        if x.get(field) is not None
    ]

    if not usable:
        return None

    # Najprej neposredna meritev.
    exact = [
        x for x in usable
        if abs(
            x["pressure_hpa"]
            - target_pressure
        ) <= 0.1
    ]

    if exact:
        return exact[0][field]

    above = None
    below = None

    for level in usable:

        p = level["pressure_hpa"]

        if p > target_pressure:

            if (
                above is None
                or p
                < above["pressure_hpa"]
            ):
                above = level

        elif p < target_pressure:

            if (
                below is None
                or p
                > below["pressure_hpa"]
            ):
                below = level

    if (
        above is None
        or below is None
    ):
        return None

    p1 = above["pressure_hpa"]
    p2 = below["pressure_hpa"]

    # Ne interpoliramo čez ogromne luknje.
    if abs(p1 - p2) > 100:
        return None

    v1 = above[field]
    v2 = below[field]

    fraction = (
        math.log(
            target_pressure / p1
        )
        / math.log(p2 / p1)
    )

    return (
        v1
        + fraction
        * (v2 - v1)
    )


# ============================================================
# VIŠINA NIČTE IZOTERME
# ============================================================

def freezing_level(levels):

    rows = [
        x for x in levels
        if (
            x["temperature_c"]
            is not None
            and x["height_m"]
            is not None
        )
    ]

    rows.sort(
        key=lambda x:
            x["height_m"]
    )

    if len(rows) < 2:
        return None

    # Iščemo prvi prehod iz pozitivne
    # v negativno temperaturo nad postajo.

    for i in range(
        len(rows) - 1
    ):

        t1 = rows[i][
            "temperature_c"
        ]

        t2 = rows[i + 1][
            "temperature_c"
        ]

        z1 = rows[i]["height_m"]
        z2 = rows[i + 1]["height_m"]

        if (
            t1 >= 0
            and t2 < 0
            and z2 > z1
        ):

            fraction = (
                (0.0 - t1)
                / (t2 - t1)
            )

            return (
                z1
                + fraction
                * (z2 - z1)
            )

    return None


# ============================================================
# PWAT
# ============================================================

def calculate_pwat(levels):

    rows = [
        x for x in levels
        if (
            x["pressure_hpa"]
            is not None
            and x["dewpoint_c"]
            is not None
        )
    ]

    if len(rows) < 10:
        return None

    rows.sort(
        key=lambda x:
            x["pressure_hpa"],
        reverse=True
    )

    # Odstranimo podvojene tlake.
    unique = []
    seen = set()

    for row in rows:

        p = round(
            row["pressure_hpa"],
            2
        )

        if p in seen:
            continue

        seen.add(p)
        unique.append(row)

    if len(unique) < 10:
        return None

    p = np.array(
        [
            x["pressure_hpa"]
            for x in unique
        ]
    ) * units.hPa

    td = np.array(
        [
            x["dewpoint_c"]
            for x in unique
        ]
    ) * units.degC

    try:

        pwat = (
            mpcalc.precipitable_water(
                p,
                td
            )
        )

        value = float(
            pwat
            .to("millimeter")
            .magnitude
        )

        if not math.isfinite(value):
            return None

        return value

    except Exception:
        return None


# ============================================================
# LAPSE RATE
# ============================================================

def pressure_lapse_rate(
    levels,
    bottom_pressure,
    top_pressure
):

    t_bottom = interpolate_pressure(
        levels,
        "temperature_c",
        bottom_pressure
    )

    t_top = interpolate_pressure(
        levels,
        "temperature_c",
        top_pressure
    )

    z_bottom = interpolate_pressure(
        levels,
        "height_m",
        bottom_pressure
    )

    z_top = interpolate_pressure(
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

    return (
        (t_bottom - t_top)
        / depth_km
    )


# ============================================================
# PARAMETRI POSAMEZNE SONDAŽE
# ============================================================

def calculate_profile_parameters(
    profile
):

    levels = profile["levels"]

    result = {
        "date": profile["date"],
        "hour": profile["hour"],
    }

    result["t850"] = (
        interpolate_pressure(
            levels,
            "temperature_c",
            850
        )
    )

    result["t700"] = (
        interpolate_pressure(
            levels,
            "temperature_c",
            700
        )
    )

    result["t500"] = (
        interpolate_pressure(
            levels,
            "temperature_c",
            500
        )
    )

    result["pwat_mm"] = (
        calculate_pwat(levels)
    )

    result[
        "freezing_level_msl_m"
    ] = freezing_level(levels)

    result[
        "lapse_rate_850_500_c_per_km"
    ] = pressure_lapse_rate(
        levels,
        850,
        500
    )

    result[
        "lapse_rate_700_500_c_per_km"
    ] = pressure_lapse_rate(
        levels,
        700,
        500
    )

    return result


# ============================================================
# DEDUPLIKACIJA
# ============================================================

def deduplicate_daily(records):
    """
    V starejšem arhivu so lahko za isti
    datum podvojeni ali skoraj podvojeni
    profili.

    Za klimatologijo želimo največ eno
    reprezentativno vrednost na dan.

    Če je več profilov istega dne,
    vzamemo mediano posameznega parametra.
    """

    grouped = defaultdict(list)

    for record in records:
        grouped[
            record["date"]
        ].append(record)

    parameters = [
        "t850",
        "t700",
        "t500",
        "pwat_mm",
        "freezing_level_msl_m",
        "lapse_rate_850_500_c_per_km",
        "lapse_rate_700_500_c_per_km",
    ]

    output = []

    for day, day_records in sorted(
        grouped.items()
    ):

        result = {
            "date": day
        }

        for parameter in parameters:

            values = [
                r[parameter]
                for r in day_records
                if valid_number(
                    r.get(parameter)
                )
            ]

            if values:

                result[parameter] = (
                    float(
                        np.median(values)
                    )
                )

            else:
                result[parameter] = None

        output.append(result)

    return output


# ============================================================
# KOLEDARSKA RAZDALJA
# ============================================================

def climatological_day(
    month,
    day
):
    """
    Uporabimo prestopno referenčno leto,
    da obstaja tudi 29. februar.
    """

    return date(
        2000,
        month,
        day
    )


def circular_day_distance(
    d1,
    d2
):

    delta = abs(
        (d1 - d2).days
    )

    return min(
        delta,
        366 - delta
    )


# ============================================================
# STATISTIKA
# ============================================================

def percentile_statistics(
    values
):

    values = np.asarray(
        values,
        dtype=float
    )

    values = values[
        np.isfinite(values)
    ]

    if len(values) == 0:
        return None

    percentiles = np.percentile(
        values,
        [
            1,
            10,
            25,
            50,
            75,
            90,
            99,
        ]
    )

    return {
        "n": int(len(values)),

        "p1":
            round(
                float(percentiles[0]),
                2
            ),

        "p10":
            round(
                float(percentiles[1]),
                2
            ),

        "p25":
            round(
                float(percentiles[2]),
                2
            ),

        "p50":
            round(
                float(percentiles[3]),
                2
            ),

        "p75":
            round(
                float(percentiles[4]),
                2
            ),

        "p90":
            round(
                float(percentiles[5]),
                2
            ),

        "p99":
            round(
                float(percentiles[6]),
                2
            ),

        "min":
            round(
                float(np.min(values)),
                2
            ),

        "max":
            round(
                float(np.max(values)),
                2
            ),
    }


# ============================================================
# DATUM EKSTREMA
# ============================================================

def extreme_dates(
    pool,
    parameter
):

    valid = [
        r for r in pool
        if valid_number(
            r.get(parameter)
        )
    ]

    if not valid:
        return {
            "min_date": None,
            "max_date": None,
        }

    minimum = min(
        valid,
        key=lambda r:
            r[parameter]
    )

    maximum = max(
        valid,
        key=lambda r:
            r[parameter]
    )

    return {
        "min_date":
            minimum[
                "date"
            ].isoformat(),

        "max_date":
            maximum[
                "date"
            ].isoformat(),
    }


# ============================================================
# DNEVNA KLIMATOLOGIJA
# ============================================================

def build_daily_climatology(
    records
):

    parameters = [
        "t850",
        "t700",
        "t500",
        "pwat_mm",
        "freezing_level_msl_m",
        "lapse_rate_850_500_c_per_km",
        "lapse_rate_700_500_c_per_km",
    ]

    result = {}

    start = date(
        2000,
        1,
        1
    )

    for day_number in range(366):

        target = (
            start
            + timedelta(
                days=day_number
            )
        )

        key = target.strftime(
            "%m-%d"
        )

        pool = []

        for record in records:

            record_day = (
                climatological_day(
                    record[
                        "date"
                    ].month,
                    record[
                        "date"
                    ].day
                )
            )

            distance = (
                circular_day_distance(
                    target,
                    record_day
                )
            )

            if distance <= WINDOW_DAYS:
                pool.append(record)

        result[key] = {}

        for parameter in parameters:

            values = [
                r[parameter]
                for r in pool
                if valid_number(
                    r.get(parameter)
                )
            ]

            stats = (
                percentile_statistics(
                    values
                )
            )

            if stats is None:
                continue

            dates = extreme_dates(
                pool,
                parameter
            )

            stats.update(dates)

            result[key][
                parameter
            ] = stats

    return result


# ============================================================
# 10-DNEVNO GLAJENJE
# ============================================================

def smooth_climatology(
    climatology
):

    keys = list(
        climatology.keys()
    )

    percentile_names = [
        "p1",
        "p10",
        "p25",
        "p50",
        "p75",
        "p90",
        "p99",
    ]

    parameters = [
        "t850",
        "t700",
        "t500",
        "pwat_mm",
        "freezing_level_msl_m",
        "lapse_rate_850_500_c_per_km",
        "lapse_rate_700_500_c_per_km",
    ]

    n_days = len(keys)

    smoothed = {}

    # 10-dnevno centrirano glajenje:
    # pet dni nazaj + tekoči dan +
    # štirje dnevi naprej = 10 dni.

    offsets = list(
        range(-5, 5)
    )

    for i, key in enumerate(keys):

        smoothed[key] = {}

        for parameter in parameters:

            if (
                parameter
                not in climatology[key]
            ):
                continue

            original = (
                climatology[key][parameter]
            )

            output = dict(original)

            for percentile
            in percentile_names:

                values = []

                for offset in offsets:

                    j = (
                        i + offset
                    ) % n_days

                    other_key = keys[j]

                    other = (
                        climatology
                        .get(
                            other_key,
                            {}
                        )
                        .get(parameter)
                    )

                    if (
                        other is not None
                        and valid_number(
                            other.get(
                                percentile
                            )
                        )
                    ):

                        values.append(
                            other[
                                percentile
                            ]
                        )

                if values:

                    output[
                        percentile
                    ] = round(
                        float(
                            np.mean(values)
                        ),
                        2
                    )

            # min/max in njuni datumi
            # namenoma ostanejo NEGLAJENI.

            smoothed[key][
                parameter
            ] = output

    return smoothed


# ============================================================
# QC POVZETEK
# ============================================================

def print_qc(records):

    parameters = [
        "t850",
        "t700",
        "t500",
        "pwat_mm",
        "freezing_level_msl_m",
        "lapse_rate_850_500_c_per_km",
        "lapse_rate_700_500_c_per_km",
    ]

    print()
    print("=" * 60)
    print("CLIMATOLOGY QC")
    print("=" * 60)

    print(
        "Daily records:",
        len(records)
    )

    for parameter in parameters:

        valid = [
            r
            for r in records
            if valid_number(
                r.get(parameter)
            )
        ]

        print()
        print(parameter)
        print(
            "  valid:",
            len(valid)
        )

        if valid:

            minimum = min(
                valid,
                key=lambda r:
                    r[parameter]
            )

            maximum = max(
                valid,
                key=lambda r:
                    r[parameter]
            )

            print(
                "  min:",
                round(
                    minimum[parameter],
                    2
                ),
                minimum["date"]
            )

            print(
                "  max:",
                round(
                    maximum[parameter],
                    2
                ),
                maximum["date"]
            )

    print("=" * 60)


# ============================================================
# GLAVNI PROGRAM
# ============================================================

def main():

    raw_zip = download(
        IGRA_URL
    )

    print(
        "Downloaded:",
        round(
            len(raw_zip)
            / 1024
            / 1024,
            2
        ),
        "MB"
    )

    with zipfile.ZipFile(
        io.BytesIO(raw_zip)
    ) as archive:

        names = archive.namelist()

        if not names:
            raise RuntimeError(
                "IGRA ZIP is empty."
            )

        filename = names[0]

        print(
            "Reading:",
            filename
        )

        text = (
            archive
            .read(filename)
            .decode(
                "ascii",
                errors="ignore"
            )
        )

    profiles = read_profiles(text)

    print(
        "Profiles 1996-2025:",
        len(profiles)
    )

    calculated = []

    total = len(profiles)

    for i, profile in enumerate(
        profiles,
        start=1
    ):

        if (
            i == 1
            or i % 500 == 0
            or i == total
        ):

            print(
                f"Processing "
                f"{i}/{total}..."
            )

        calculated.append(
            calculate_profile_parameters(
                profile
            )
        )

    daily = deduplicate_daily(
        calculated
    )

    print_qc(daily)

    print()
    print(
        "Building ±15-day "
        "climatological windows..."
    )

    raw_climatology = (
        build_daily_climatology(
            daily
        )
    )

    print(
        "Applying 10-day smoothing..."
    )

    climatology = (
        smooth_climatology(
            raw_climatology
        )
    )

    output = {
        "metadata": {
            "station_id":
                STATION_ID,

            "station_name":
                STATION_NAME,

            "reference_period":
                (
                    f"{START_YEAR}-"
                    f"{END_YEAR}"
                ),

            "window_days":
                WINDOW_DAYS,

            "window_description":
                "±15 calendar days",

            "smoothing_days":
                10,

            "smoothing_description":
                (
                    "10-day moving mean "
                    "of percentile curves; "
                    "extremes are not smoothed"
                ),

            "daily_deduplication":
                (
                    "median of profiles "
                    "for the same calendar date"
                ),

            "historical_time_caveat":
                (
                    "Most historical Ljubljana "
                    "soundings were nominally "
                    "06 UTC and are not fully "
                    "time-equivalent to the "
                    "current 00/12 UTC schedule."
                ),

            "pwat_method":
                (
                    "MetPy precipitable_water "
                    "from available dewpoint "
                    "profile"
                ),

            "created_utc":
                datetime.utcnow()
                .isoformat()
                + "Z",
        },

        "daily":
            climatology,
    }

    import os

    os.makedirs(
        "climatology",
        exist_ok=True
    )

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            output,
            f,
            ensure_ascii=False,
            indent=2
        )

    print()
    print("=" * 60)
    print(
        "Saved:",
        OUTPUT_FILE
    )
    print("=" * 60)

    # Primer 21. september
    sample = (
        climatology
        .get(
            "09-21",
            {}
        )
    )

    print()
    print(
        "Example climatology "
        "for 21 September:"
    )

    for parameter, stats in (
        sample.items()
    ):

        print()
        print(parameter)

        print(
            "  P10:",
            stats.get("p10")
        )

        print(
            "  P50:",
            stats.get("p50")
        )

        print(
            "  P90:",
            stats.get("p90")
        )

        print(
            "  MIN:",
            stats.get("min"),
            stats.get("min_date")
        )

        print(
            "  MAX:",
            stats.get("max"),
            stats.get("max_date")
        )


if __name__ == "__main__":
    main()
