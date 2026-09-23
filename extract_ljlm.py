import json
import math
import os
import re
import tempfile
import time
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

# Cache DWD packages that have already been scanned and do not contain LJLM.
# This avoids repeatedly downloading/decoding the same irrelevant BUFR files.
DWD_SCAN_CACHE_FILE = "data/dwd_scan_cache.json"
DWD_SCAN_CACHE_RETENTION_HOURS = 72

# Operational availability/status file used by the future web application.
STATUS_FILE = "data/status.json"
STATUS_RETENTION_DAYS = 14

# Regular status lifecycle. A term becomes "waiting" at the main scheduled
# check and "missing" only after the reserve check has also had a chance
# to find it. Times are UTC and match the GitHub Actions schedule.
STATUS_MAIN_CHECK_MINUTE = 50
STATUS_RESERVE_00_HOUR = 1
STATUS_RESERVE_12_HOUR = 13

# During scheduled runs, once the expected 00/12 UTC LJLM sounding is found,
# scan a short tail of older packages as well. This preserves nearby duplicate
# copies and allows the script to keep the most complete profile.
SCHEDULED_EARLY_STOP_TAIL_PACKAGES = 12

# Regular launches are normally near 23:30 and 11:30 UTC.
# Profiles outside this tolerance are preserved as special soundings.
REGULAR_LAUNCH_TOLERANCE_MINUTES = 120

# Inversion diagnostics. Surface to 700 hPa is intentionally sensitive
# because thin/weak low-level inversions can be operationally important.
INVERSION_GRID_STEP_M = 10.0
INVERSION_SMOOTH_WINDOW_M = 50.0
INVERSION_LOWER_BOUND_HPA = 700.0
INVERSION_UPPER_BOUND_HPA = 300.0
INVERSION_LOW_MIN_DEPTH_M = 20.0
INVERSION_LOW_MIN_DELTA_T_C = 0.10
INVERSION_MID_MIN_DEPTH_M = 50.0
INVERSION_MID_MIN_DELTA_T_C = 0.50
INVERSION_MERGE_GAP_M = 40.0
INVERSION_MERGE_COOLING_C = 0.15


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


def is_manual_run():
    event_name = os.environ.get(
        "GITHUB_EVENT_NAME",
        "manual_local"
    )

    return (
        event_name == "workflow_dispatch"
        or event_name == "manual_local"
    )


def dwd_filename_time(filename):
    """Return UTC timestamp embedded in a DWD package filename, if present."""
    decoded = unquote(filename)

    match = re.search(
        r'_(\d{14})_',
        decoded
    )

    if match is None:
        return None

    try:
        return datetime.strptime(
            match.group(1),
            "%Y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_dwd_scan_cache(now=None):
    """Load and prune the persistent cache of already scanned DWD packages."""
    if now is None:
        now = datetime.now(timezone.utc)

    empty = {
        "version": 1,
        "updated_at": None,
        "packages": {},
    }

    if not os.path.exists(DWD_SCAN_CACHE_FILE):
        return empty

    try:
        with open(
            DWD_SCAN_CACHE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)
    except Exception as exc:
        print("WARNING: DWD scan cache could not be read:", exc)
        return empty

    packages = data.get("packages", {})
    if not isinstance(packages, dict):
        return empty

    cutoff = now - timedelta(
        hours=DWD_SCAN_CACHE_RETENTION_HOURS
    )

    kept = {}

    for filename, item in packages.items():
        file_time = dwd_filename_time(filename)

        # Keep unparseable names rather than deleting potentially useful
        # cache entries. Normal DWD filenames are pruned by embedded time.
        if file_time is None or file_time >= cutoff:
            kept[filename] = item

    return {
        "version": 1,
        "updated_at": data.get("updated_at"),
        "packages": kept,
    }


def save_dwd_scan_cache(cache):
    os.makedirs("data", exist_ok=True)

    cache["version"] = 1
    cache["updated_at"] = (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )

    with open(
        DWD_SCAN_CACHE_FILE,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            cache,
            f,
            indent=2,
            ensure_ascii=False,
            sort_keys=True
        )


def update_dwd_scan_cache(
    cache,
    filename,
    profile=None
):
    """Record a successfully scanned package.

    Positive packages are remembered for diagnostics but are deliberately
    not skipped on later runs: DWD can expose duplicate copies and the code
    should still be able to select the most complete LJLM profile.
    """
    item = {
        "scanned_at": (
            datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "contains_ljlm": profile is not None,
    }

    if profile is not None:
        item["launch_time"] = profile.get("launch_time")
        item["profile_level_count"] = (
            profile.get("qc", {})
            .get("profile_level_count")
        )

    cache.setdefault("packages", {})[filename] = item


def find_candidate_files():
    """Return only recent DWD TEMP BUFR packages.

    Scheduled GitHub Actions runs use a short 3-hour window because the
    regular 00/12 UTC soundings normally arrive shortly after the launch.
    A manual workflow_dispatch run uses a 24-hour catch-up window, useful
    for delayed or special soundings.

    Filtering is done from the timestamp embedded in the DWD filename,
    before any BUFR file is downloaded or opened with ecCodes.
    """

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

    event_name = os.environ.get(
        "GITHUB_EVENT_NAME",
        "manual_local"
    )

    manual_run = is_manual_run()

    search_hours = 24 if manual_run else 3

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=search_hours)

    recent_files = []
    timestamped_files = 0

    for filename in files:
        # Example:
        # Z__C_EDZW_20260922131201_...temp_bufr....bin
        file_time = dwd_filename_time(filename)

        if file_time is None:
            continue

        timestamped_files += 1

        if cutoff <= file_time <= now + timedelta(minutes=10):
            recent_files.append(filename)

    print(
        "GitHub event:",
        event_name
    )

    print(
        "Search mode:",
        "manual catch-up" if manual_run else "scheduled"
    )

    print(
        "DWD search window:",
        f"{search_hours} h"
    )

    print(
        "DWD TEMP packages listed:",
        len(files)
    )

    print(
        "Packages with readable timestamp:",
        timestamped_files
    )

    print(
        "Packages selected before download:",
        len(recent_files)
    )

    if recent_files:
        return recent_files

    # Safety fallback: if DWD changes the filename convention or the
    # directory timestamp cannot be parsed, retain the old behaviour.
    print(
        "WARNING: no recent timestamped packages found; "
        "using legacy fallback."
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


def build_trajectory(
    latitude,
    longitude,
    pressure,
    height,
    time_period,
    latitude_displacement,
    longitude_displacement
):
    """
    Reconstructs the ascent trajectory from BUFR displacement arrays.

    The DWD LJLM BUFR contains approximately 2-second trajectory data.
    We keep a decimated track (about every 10 s) plus positions near
    standard pressure levels. A time reset or the maximum-height point
    prevents a later/descent segment from being joined blindly.
    """

    if not (
        valid_number(latitude)
        and valid_number(longitude)
    ):
        return {
            "available": False,
            "reason": "missing launch coordinates"
        }

    arrays = [
        pressure,
        height,
        time_period,
        latitude_displacement,
        longitude_displacement,
    ]

    lengths = [
        len(x) for x in arrays
        if x
    ]

    if len(lengths) < 5:
        return {
            "available": False,
            "reason": "trajectory arrays missing"
        }

    n = min(lengths)

    rows = []

    previous_time = None

    for i in range(n):
        p = pressure[i]
        z = height[i]
        tp = time_period[i]
        dlat = latitude_displacement[i]
        dlon = longitude_displacement[i]

        if not all(
            valid_number(x)
            for x in [p, z, tp, dlat, dlon]
        ):
            continue

        tp = float(tp)

        # A time reset usually marks another BUFR segment.
        if (
            previous_time is not None
            and tp < previous_time
        ):
            break

        previous_time = tp

        p_hpa = float(p) / 100.0

        if not (0 < p_hpa <= 1100):
            continue

        rows.append({
            "time_s": round(tp, 1),
            "pressure_hpa": round(p_hpa, 2),
            "height_m": round(float(z), 1),
            "latitude": round(
                float(latitude) + float(dlat),
                6
            ),
            "longitude": round(
                float(longitude) + float(dlon),
                6
            ),
        })

    if len(rows) < 2:
        return {
            "available": False,
            "reason": "insufficient trajectory points"
        }

    # For the operational trajectory product use the ascent only.
    max_index = max(
        range(len(rows)),
        key=lambda i: rows[i]["height_m"]
    )

    ascent = rows[:max_index + 1]

    # Roughly 20-second display spacing, while always keeping the
    # first and last ascent point.
    display_points = []

    last_kept_time = None

    for point in ascent:
        if (
            last_kept_time is None
            or point["time_s"] - last_kept_time >= 20
        ):
            display_points.append(point)
            last_kept_time = point["time_s"]

    if display_points[-1] != ascent[-1]:
        display_points.append(ascent[-1])

    standard_levels = {}

    for target in [925, 850, 700, 500, 300, 250, 200]:
        nearest = min(
            ascent,
            key=lambda x:
                abs(x["pressure_hpa"] - target)
        )

        if abs(
            nearest["pressure_hpa"] - target
        ) <= 5:
            standard_levels[str(target)] = nearest

    return {
        "available": True,
        "source": "BUFR latitude/longitude displacement",
        "segment": "ascent",
        "raw_point_count": len(ascent),
        "display_point_count": len(display_points),
        "duration_s": ascent[-1]["time_s"],
        "max_height_m": max(
            x["height_m"] for x in ascent
        ),
        "start": ascent[0],
        "end": ascent[-1],
        "standard_levels": standard_levels,
        "points": display_points,
    }


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

                time_period = safe_array(
                    handle,
                    "timePeriod"
                )

                latitude_displacement = safe_array(
                    handle,
                    "latitudeDisplacement"
                )

                longitude_displacement = safe_array(
                    handle,
                    "longitudeDisplacement"
                )

                trajectory = build_trajectory(
                    latitude,
                    longitude,
                    pressure,
                    height,
                    time_period,
                    latitude_displacement,
                    longitude_displacement
                )

                raw_counts = {
                    "pressure": len(pressure),
                    "temperature": len(temperature),
                    "dewpoint": len(dewpoint),
                    "height": len(height),
                    "wind_speed": len(wind_speed),
                    "wind_direction": len(wind_direction),
                    "time_period": len(time_period),
                    "latitude_displacement":
                        len(latitude_displacement),
                    "longitude_displacement":
                        len(longitude_displacement),
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

                    theta_k = None
                    theta_e_k = None

                    if t_c is not None:
                        try:
                            theta_k = float(
                                mpcalc.potential_temperature(
                                    p_hpa * units.hPa,
                                    t_c * units.degC
                                ).to("kelvin").magnitude
                            )
                        except Exception:
                            theta_k = None

                    if t_c is not None and td_c is not None:
                        try:
                            theta_e_k = float(
                                mpcalc.equivalent_potential_temperature(
                                    p_hpa * units.hPa,
                                    t_c * units.degC,
                                    td_c * units.degC
                                ).to("kelvin").magnitude
                            )
                        except Exception:
                            theta_e_k = None

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

                            "potential_temperature_k":
                                round(theta_k, 2)
                                if theta_k is not None
                                else None,

                            "equivalent_potential_temperature_k":
                                round(theta_e_k, 2)
                                if theta_e_k is not None
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

                    "trajectory": trajectory,

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

def classify_sounding(launch_time):
    """
    Classifies the current LJLM schedule.

    Regular 00 UTC: expected launch around 23:30 UTC previous day.
    Regular 12 UTC: expected launch around 11:30 UTC.
    A +/- 2 hour window is allowed. Everything else is preserved
    as a special sounding and cannot overwrite a regular sounding.
    """

    candidates = []

    # Candidate regular 12 UTC on launch calendar day.
    nominal_12 = datetime(
        launch_time.year,
        launch_time.month,
        launch_time.day,
        11,
        30,
        tzinfo=timezone.utc
    )

    candidates.append((
        abs(
            (launch_time - nominal_12)
            .total_seconds()
        ),
        "12",
        launch_time.date()
    ))

    # 00 UTC launch is normally 23:30 on the previous calendar day.
    nominal_date_00 = (
        launch_time + timedelta(days=1)
    ).date()

    nominal_00 = datetime(
        launch_time.year,
        launch_time.month,
        launch_time.day,
        23,
        30,
        tzinfo=timezone.utc
    )

    candidates.append((
        abs(
            (launch_time - nominal_00)
            .total_seconds()
        ),
        "00",
        nominal_date_00
    ))

    # Also allow an after-midnight 00 UTC launch.
    midnight = datetime(
        launch_time.year,
        launch_time.month,
        launch_time.day,
        0,
        0,
        tzinfo=timezone.utc
    )

    candidates.append((
        abs(
            (launch_time - midnight)
            .total_seconds()
        ),
        "00",
        launch_time.date()
    ))

    difference_s, term, nominal_date = min(
        candidates,
        key=lambda x: x[0]
    )

    if difference_s <= REGULAR_LAUNCH_TOLERANCE_MINUTES * 60:
        return {
            "kind": "regular",
            "term": term,
            "nominal_date": nominal_date,
            "launch_offset_minutes":
                round(difference_s / 60.0, 1),
        }

    return {
        "kind": "special",
        "term": (
            "special_"
            + launch_time.strftime("%H%M")
        ),
        "nominal_date": launch_time.date(),
        "launch_offset_minutes": None,
    }


def determine_term(launch_time):
    classification = classify_sounding(
        launch_time
    )

    return (
        classification["term"],
        classification["nominal_date"]
    )


# ============================================================
# KATERI TERMIN TRENUTNO IŠČEMO?
# ============================================================

def expected_term(now):
    """
    Vrne zadnji termin, ki ga je ob trenutnem času smiselno
    pričakovati v DWD Open Data.

    00 UTC sonda je izpuščena približno ob 23:30 UTC
    prejšnjega dne, 12 UTC pa približno ob 11:30 UTC.
    Dodamo približno eno uro rezerve za prihod podatkov v DWD.
    """

    # Od 12:30 UTC naprej iščemo današnjo 12 UTC sondažo.
    if (
        now.hour > 12
        or (
            now.hour == 12
            and now.minute >= 30
        )
    ):
        return "12", now.date()

    # Od 00:30 do 12:29 UTC iščemo današnjo 00 UTC sondažo.
    if (
        now.hour > 0
        or (
            now.hour == 0
            and now.minute >= 30
        )
    ):
        return "00", now.date()

    # Med 00:00 in 00:29 UTC je varneje še uporabiti
    # včerajšnjo 12 UTC sondažo.
    return "12", (now - timedelta(days=1)).date()


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
# VETROVNE POMOŽNE FUNKCIJE
# ============================================================

def wind_at_pressure(levels, target):
    """Veter na tlačni ploskvi; u/v interpolacija v log(p)."""
    rows = [
        x for x in levels
        if x.get("pressure_hpa") is not None
        and x.get("wind_speed_ms") is not None
        and x.get("wind_direction_deg") is not None
    ]
    if not rows:
        return None

    rows.sort(key=lambda x: x["pressure_hpa"], reverse=True)

    def uv(row):
        speed = row["wind_speed_ms"] * units("m/s")
        direction = row["wind_direction_deg"] * units.degree
        u, v = mpcalc.wind_components(speed, direction)
        return float(u.to("m/s").magnitude), float(v.to("m/s").magnitude)

    exact = [x for x in rows if abs(x["pressure_hpa"] - target) <= 0.1]

    if exact:
        u_ms, v_ms = uv(exact[0])
        method = "measured"
    else:
        above = below = None
        for row in rows:
            p = row["pressure_hpa"]
            if p > target and (above is None or p < above["pressure_hpa"]):
                above = row
            elif p < target and (below is None or p > below["pressure_hpa"]):
                below = row

        if above is None or below is None:
            return None

        p1, p2 = above["pressure_hpa"], below["pressure_hpa"]
        if abs(p1 - p2) > 100:
            return None

        u1, v1 = uv(above)
        u2, v2 = uv(below)
        f = math.log(target / p1) / math.log(p2 / p1)
        u_ms = u1 + f * (u2 - u1)
        v_ms = v1 + f * (v2 - v1)
        method = "log_pressure_uv_interpolation"

    speed_ms = math.sqrt(u_ms ** 2 + v_ms ** 2)
    direction_deg = math.degrees(math.atan2(-u_ms, -v_ms)) % 360.0

    return {
        "pressure_hpa": target,
        "speed_ms": round(speed_ms, 2),
        "direction_deg": round(direction_deg, 1),
        "u_ms": round(u_ms, 2),
        "v_ms": round(v_ms, 2),
        "method": method,
    }


def vector_speed_direction(u_component, v_component):
    u_ms = quantity_value(u_component, "m/s")
    v_ms = quantity_value(v_component, "m/s")
    if u_ms is None or v_ms is None:
        return None

    speed_ms = math.sqrt(u_ms ** 2 + v_ms ** 2)
    direction_deg = math.degrees(math.atan2(-u_ms, -v_ms)) % 360.0

    return {
        "speed_ms": round(speed_ms, 2),
        "direction_deg": round(direction_deg, 1),
        "u_ms": round(u_ms, 2),
        "v_ms": round(v_ms, 2),
    }



# ============================================================
# TRANSPORT VLAGE / IVT
# ============================================================

def calculate_moisture_transport(levels):
    """
    Izračuna:
    - specifično vlago q in q*V na 850 hPa
    - IVT skozi skupni razpoložljivi sloj od dna profila
      do 300 hPa.

    IVT = (1/g) * integral(q * V dp)
    Enota: kg m-1 s-1.
    """

    rows = [
        x for x in levels
        if (
            x.get("pressure_hpa") is not None
            and x.get("dewpoint_c") is not None
            and x.get("wind_speed_ms") is not None
            and x.get("wind_direction_deg") is not None
        )
    ]

    if len(rows) < 10:
        return None

    rows.sort(
        key=lambda x: x["pressure_hpa"],
        reverse=True
    )

    # Odstrani podvojene tlake.
    unique_rows = []
    seen = set()

    for row in rows:
        p = float(row["pressure_hpa"])

        if p in seen:
            continue

        seen.add(p)
        unique_rows.append(row)

    rows = unique_rows

    result = {}

    # --------------------------------------------------------
    # SPECIFIČNA VLAGA q IN RAZMERJE MEŠANOSTI r
    # pri tleh, 925 hPa in 850 hPa
    # --------------------------------------------------------

    def humidity_from_p_td(p_hpa, td_c):
        p_q = float(p_hpa) * units.hPa
        td_q = float(td_c) * units.degC

        vapor_pressure = mpcalc.saturation_vapor_pressure(
            td_q
        )

        mixing_ratio = mpcalc.mixing_ratio(
            vapor_pressure,
            p_q
        )

        specific_humidity = (
            mpcalc.specific_humidity_from_mixing_ratio(
                mixing_ratio
            )
        )

        return (
            float(
                specific_humidity
                .to("dimensionless")
                .magnitude
            ),
            float(
                mixing_ratio
                .to("dimensionless")
                .magnitude
            )
        )

    def humidity_at_pressure(target):
        exact = [
            x for x in rows
            if abs(
                float(x["pressure_hpa"]) - target
            ) <= 0.1
        ]

        if exact:
            q, r = humidity_from_p_td(
                exact[0]["pressure_hpa"],
                exact[0]["dewpoint_c"]
            )

            return {
                "pressure_hpa": target,
                "specific_humidity_gkg":
                    round(q * 1000.0, 2),
                "mixing_ratio_gkg":
                    round(r * 1000.0, 2),
                "method": "measured",
            }

        above = None
        below = None

        for row in rows:
            p_row = float(row["pressure_hpa"])

            if p_row > target:
                if (
                    above is None
                    or p_row
                    < float(above["pressure_hpa"])
                ):
                    above = row

            elif p_row < target:
                if (
                    below is None
                    or p_row
                    > float(below["pressure_hpa"])
                ):
                    below = row

        if above is None or below is None:
            return None

        p1 = float(above["pressure_hpa"])
        p2 = float(below["pressure_hpa"])

        if abs(p1 - p2) > 100:
            return None

        q1, r1 = humidity_from_p_td(
            p1,
            above["dewpoint_c"]
        )
        q2, r2 = humidity_from_p_td(
            p2,
            below["dewpoint_c"]
        )

        fraction = (
            math.log(target / p1)
            / math.log(p2 / p1)
        )

        q = q1 + fraction * (q2 - q1)
        r = r1 + fraction * (r2 - r1)

        return {
            "pressure_hpa": target,
            "specific_humidity_gkg":
                round(q * 1000.0, 2),
            "mixing_ratio_gkg":
                round(r * 1000.0, 2),
            "method":
                "log_pressure_interpolation",
        }

    # "Surface" pomeni najnižji uporaben nivo skupnega
    # profila p/Td/veter.
    surface_row = rows[0]

    surface_q, surface_r = humidity_from_p_td(
        surface_row["pressure_hpa"],
        surface_row["dewpoint_c"]
    )

    humidity_profile = {
        "surface": {
            "pressure_hpa":
                round(
                    float(
                        surface_row["pressure_hpa"]
                    ),
                    2
                ),
            "height_m":
                surface_row.get("height_m"),
            "specific_humidity_gkg":
                round(surface_q * 1000.0, 2),
            "mixing_ratio_gkg":
                round(surface_r * 1000.0, 2),
            "method":
                "lowest_common_profile_level",
        },
        "925": humidity_at_pressure(925.0),
        "850": humidity_at_pressure(850.0),
    }

    q_surface = (
        humidity_profile["surface"]
        .get("specific_humidity_gkg")
    )

    q925 = (
        humidity_profile.get("925") or {}
    ).get("specific_humidity_gkg")

    r_surface = (
        humidity_profile["surface"]
        .get("mixing_ratio_gkg")
    )

    r925 = (
        humidity_profile.get("925") or {}
    ).get("mixing_ratio_gkg")

    humidity_profile["surface_to_925"] = {
        "delta_q_925_minus_surface_gkg":
            round(q925 - q_surface, 2)
            if (
                q925 is not None
                and q_surface is not None
            )
            else None,

        "delta_mixing_ratio_925_minus_surface_gkg":
            round(r925 - r_surface, 2)
            if (
                r925 is not None
                and r_surface is not None
            )
            else None,
    }

    result["humidity_profile"] = (
        humidity_profile
    )

    # --------------------------------------------------------
    # 850 hPa moisture transport
    # --------------------------------------------------------

    def moisture_state(row):
        p = float(row["pressure_hpa"]) * units.hPa
        td = float(row["dewpoint_c"]) * units.degC
        speed = float(row["wind_speed_ms"]) * units("m/s")
        direction = float(row["wind_direction_deg"]) * units.degree

        vapor_pressure = (
            mpcalc.saturation_vapor_pressure(
                td
            )
        )

        mixing_ratio = mpcalc.mixing_ratio(
            vapor_pressure,
            p
        )

        q = (
            mpcalc.specific_humidity_from_mixing_ratio(
                mixing_ratio
            )
        )

        u, v = mpcalc.wind_components(
            speed,
            direction
        )

        return (
            float(q.to("dimensionless").magnitude),
            float(u.to("m/s").magnitude),
            float(v.to("m/s").magnitude)
        )

    target = 850.0
    exact = [
        x for x in rows
        if abs(float(x["pressure_hpa"]) - target) <= 0.1
    ]

    q850 = u850 = v850 = None
    method850 = None

    if exact:
        q850, u850, v850 = moisture_state(exact[0])
        method850 = "measured"

    else:
        above = None
        below = None

        for row in rows:
            p = float(row["pressure_hpa"])

            if p > target:
                if (
                    above is None
                    or p < float(above["pressure_hpa"])
                ):
                    above = row

            elif p < target:
                if (
                    below is None
                    or p > float(below["pressure_hpa"])
                ):
                    below = row

        if above is not None and below is not None:
            p1 = float(above["pressure_hpa"])
            p2 = float(below["pressure_hpa"])

            if abs(p1 - p2) <= 100:
                q1, u1, v1 = moisture_state(above)
                q2, u2, v2 = moisture_state(below)

                f = (
                    math.log(target / p1)
                    / math.log(p2 / p1)
                )

                q850 = q1 + f * (q2 - q1)
                u850 = u1 + f * (u2 - u1)
                v850 = v1 + f * (v2 - v1)
                method850 = "log_pressure_interpolation"

    if (
        q850 is not None
        and u850 is not None
        and v850 is not None
    ):
        speed850 = math.sqrt(
            u850 ** 2 + v850 ** 2
        )

        flux_u = q850 * u850
        flux_v = q850 * v850

        flux_mag = math.sqrt(
            flux_u ** 2 + flux_v ** 2
        )

        transport_to_deg = (
            math.degrees(
                math.atan2(flux_u, flux_v)
            ) % 360.0
        )

        result["moisture_transport_850"] = {
            "pressure_hpa": 850.0,
            "specific_humidity_gkg":
                round(q850 * 1000.0, 2),

            "wind_speed_ms":
                round(speed850, 2),

            "qv_magnitude_kgkg_ms":
                round(flux_mag, 5),

            "qv_u_kgkg_ms":
                round(flux_u, 5),

            "qv_v_kgkg_ms":
                round(flux_v, 5),

            # Smer, KAMOR se vlaga prenaša.
            "transport_to_direction_deg":
                round(transport_to_deg, 1),

            "method":
                method850,
        }

    # --------------------------------------------------------
    # IVT: spodnji razpoložljivi nivo -> 300 hPa
    # --------------------------------------------------------

    ivt_rows = [
        row for row in rows
        if float(row["pressure_hpa"]) >= 300.0
    ]

    if len(ivt_rows) < 10:
        result["ivt"] = {
            "available": False
        }
        return result

    # Če ni točno 300 hPa, profil končamo na zadnjem nivoju
    # tik nad 300 hPa. Pri LJLM je 300 hPa praviloma prisoten.
    p_pa = []
    q_values = []
    u_values = []
    v_values = []

    for row in ivt_rows:
        try:
            q, u_ms, v_ms = moisture_state(row)
        except Exception:
            continue

        p_pa.append(
            float(row["pressure_hpa"]) * 100.0
        )
        q_values.append(q)
        u_values.append(u_ms)
        v_values.append(v_ms)

    if len(p_pa) < 10:
        result["ivt"] = {
            "available": False
        }
        return result

    p_pa = np.asarray(p_pa, dtype=float)
    q_values = np.asarray(q_values, dtype=float)
    u_values = np.asarray(u_values, dtype=float)
    v_values = np.asarray(v_values, dtype=float)

    # Profil je urejen od visokega proti nizkemu tlaku.
    # np.trapezoid bi zato dal negativen integral; obrnemo predznak.
    gravity = 9.80665

    ivt_u = (
        -np.trapezoid(
            q_values * u_values,
            p_pa
        )
        / gravity
    )

    ivt_v = (
        -np.trapezoid(
            q_values * v_values,
            p_pa
        )
        / gravity
    )

    ivt_mag = math.sqrt(
        ivt_u ** 2 + ivt_v ** 2
    )

    ivt_to_deg = (
        math.degrees(
            math.atan2(ivt_u, ivt_v)
        ) % 360.0
    )

    result["ivt"] = {
        "available": True,

        "layer":
            "lowest_common_level_to_300hpa",

        "bottom_pressure_hpa":
            round(float(p_pa[0]) / 100.0, 2),

        "top_pressure_hpa":
            round(float(p_pa[-1]) / 100.0, 2),

        "magnitude_kg_m1_s1":
            round(float(ivt_mag), 1),

        "u_kg_m1_s1":
            round(float(ivt_u), 1),

        "v_kg_m1_s1":
            round(float(ivt_v), 1),

        # Smer, KAMOR je usmerjen IVT.
        "transport_to_direction_deg":
            round(float(ivt_to_deg), 1),

        "integration":
            "pressure_coordinate_trapezoidal",

        "formula":
            "IVT=(1/g)*integral(q*V dp)",
    }

    return result


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


def nonnegative_cape_value(quantity, label, qc_list):
    """
    CAPE is physically non-negative. If the numerical calculation
    returns a negative value, publish 0 J/kg and retain the raw value
    in QC metadata.
    """
    value = quantity_value(
        quantity,
        "joule / kilogram"
    )

    if value is None:
        return None

    if value < 0:
        qc_list.append({
            "parameter": label,
            "raw_value_jkg": round(float(value), 2),
            "published_value_jkg": 0.0,
            "reason": "negative numerical CAPE corrected to zero",
        })
        return 0.0

    return value

def calculate_metpy_parameters(levels):

    result = {}

    cape_qc = []
    result["qc"] = {
        "cape_corrections": cape_qc
    }

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


    # Moisture transport / IVT
    moisture_transport = safe_parameter(
        "moisture transport / IVT",
        lambda:
            calculate_moisture_transport(
                levels
            )
    )

    if moisture_transport is not None:
        result["moisture_transport"] = (
            moisture_transport
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

        result["sbcape_jkg"] = nonnegative_cape_value(
            cape,
            "SBCAPE",
            cape_qc
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

        result["mlcape_jkg"] = nonnegative_cape_value(
            cape,
            "MLCAPE",
            cape_qc
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

        result["mucape_jkg"] = nonnegative_cape_value(
            cape,
            "MUCAPE",
            cape_qc
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

    # WIND
    result["standard_winds"] = {}

    for pressure_level in [925, 850, 700, 500, 300, 250, 200]:
        item = wind_at_pressure(levels, pressure_level)
        if item is not None:
            result["standard_winds"][str(pressure_level)] = item

    wind = prepare_wind_profile(levels)

    if wind is not None:
        wp = wind["pressure"]
        wz = wind["height"]
        u = wind["u"]
        v = wind["v"]
        bottom = wz[0]

        result["bulk_shear"] = {}

        for depth_km in [1, 3, 6]:
            shear = safe_parameter(
                f"{depth_km} km shear",
                lambda depth_km=depth_km: mpcalc.bulk_shear(
                    wp, u, v,
                    height=wz,
                    bottom=bottom,
                    depth=depth_km * units.kilometer
                )
            )

            if shear is None:
                continue

            u_shear, v_shear = shear
            magnitude = np.sqrt(u_shear ** 2 + v_shear ** 2)

            magnitude_ms = quantity_value(magnitude, "m/s")
            u_ms = quantity_value(u_shear, "m/s")
            v_ms = quantity_value(v_shear, "m/s")

            result[f"shear_0_{depth_km}km_ms"] = magnitude_ms
            result["bulk_shear"][f"0_{depth_km}km"] = {
                "magnitude_ms": magnitude_ms,
                "u_ms": u_ms,
                "v_ms": v_ms,
            }

        bunkers = safe_parameter(
            "Bunkers storm motion",
            lambda: mpcalc.bunkers_storm_motion(wp, u, v, wz)
        )

        rm_u = rm_v = lm_u = lm_v = None

        if bunkers is not None:
            try:
                right_mover, left_mover, mean_wind = bunkers
                rm_u, rm_v = right_mover
                lm_u, lm_v = left_mover
                mean_u, mean_v = mean_wind

                result["bunkers"] = {
                    "right_mover": vector_speed_direction(rm_u, rm_v),
                    "left_mover": vector_speed_direction(lm_u, lm_v),
                    "mean_wind_0_6km": vector_speed_direction(mean_u, mean_v),
                }
            except Exception as exc:
                print("MetPy warning (Bunkers details):", exc)

        result["srh"] = {}

        for depth_km in [1, 3]:
            for label, storm_u, storm_v in [
                ("right_mover", rm_u, rm_v),
                ("left_mover", lm_u, lm_v),
            ]:
                if storm_u is None or storm_v is None:
                    continue

                srh = safe_parameter(
                    f"SRH 0-{depth_km} km {label}",
                    lambda depth_km=depth_km, storm_u=storm_u, storm_v=storm_v:
                        mpcalc.storm_relative_helicity(
                            wz, u, v,
                            depth=depth_km * units.kilometer,
                            bottom=wz[0],
                            storm_u=storm_u,
                            storm_v=storm_v
                        )
                )

                if srh is None:
                    continue

                try:
                    positive, negative, total = srh
                    result["srh"][f"0_{depth_km}km_{label}"] = {
                        "positive_m2s2": quantity_value(
                            positive, "meter**2 / second**2"
                        ),
                        "negative_m2s2": quantity_value(
                            negative, "meter**2 / second**2"
                        ),
                        "total_m2s2": quantity_value(
                            total, "meter**2 / second**2"
                        ),
                    }
                except Exception as exc:
                    print(
                        f"MetPy warning (SRH 0-{depth_km} km {label}):",
                        exc
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

        same_launch = (
            old.get("launch_time")
            == launch_time
        )

        metpy = old.get("parameters", {}).get("metpy", {})

        has_climatology = (
            old.get("parameters", {})
               .get("climatology", {})
               .get("available")
            is True
        )

        has_wind_upgrade = (
            isinstance(metpy.get("standard_winds"), dict)
            and isinstance(metpy.get("bulk_shear"), dict)
            and "bunkers" in metpy
            and isinstance(metpy.get("srh"), dict)
        )

        moisture_transport = metpy.get(
            "moisture_transport",
            {}
        )

        has_moisture_transport_upgrade = (
            isinstance(
                moisture_transport.get(
                    "humidity_profile"
                ),
                dict
            )
            and isinstance(
                moisture_transport.get(
                    "moisture_transport_850"
                ),
                dict
            )
            and isinstance(
                moisture_transport.get("ivt"),
                dict
            )
        )

        change = (
            old.get("parameters", {})
               .get("change", {})
        )

        has_change_upgrade = (
            isinstance(
                change.get("previous_term"),
                dict
            )
            and isinstance(
                change.get(
                    "previous_day_same_term"
                ),
                dict
            )
        )

        has_trajectory_upgrade = (
            isinstance(
                old.get("trajectory"),
                dict
            )
            and old.get(
                "trajectory", {}
            ).get("available")
            is True
        )

        has_cape_qc_upgrade = (
            isinstance(
                metpy.get("qc", {}).get(
                    "cape_corrections"
                ),
                list
            )
        )

        old_levels = old.get("levels", [])
        has_theta_upgrade = (
            bool(old_levels)
            and "potential_temperature_k" in old_levels[0]
            and "equivalent_potential_temperature_k" in old_levels[0]
        )

        inversion_block = (
            old.get("parameters", {}).get("inversions", {})
        )
        has_inversion_upgrade = (
            inversion_block.get("available") is True
            and isinstance(inversion_block.get("layers"), list)
            and isinstance(inversion_block.get("summary"), dict)
        )

        return (
            same_launch
            and has_climatology
            and has_wind_upgrade
            and has_moisture_transport_upgrade
            and has_change_upgrade
            and has_trajectory_upgrade
            and has_cape_qc_upgrade
            and has_theta_upgrade
            and has_inversion_upgrade
        )

    except Exception:
        return False


# ============================================================
# OPERATIVNI STATUS SONDAŽ
# ============================================================

def iso_utc(dt):
    return (
        dt.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def status_key(nominal_date, term):
    return f"{nominal_date:%Y-%m-%d}_{term}"


def regular_status_times(nominal_date, term):
    """Return (waiting_from, missing_from) for a regular 00/12 UTC term."""
    if term == "00":
        waiting_from = datetime(
            nominal_date.year,
            nominal_date.month,
            nominal_date.day,
            0,
            STATUS_MAIN_CHECK_MINUTE,
            tzinfo=timezone.utc,
        )
        missing_from = datetime(
            nominal_date.year,
            nominal_date.month,
            nominal_date.day,
            STATUS_RESERVE_00_HOUR,
            STATUS_MAIN_CHECK_MINUTE,
            tzinfo=timezone.utc,
        )
    elif term == "12":
        waiting_from = datetime(
            nominal_date.year,
            nominal_date.month,
            nominal_date.day,
            12,
            STATUS_MAIN_CHECK_MINUTE,
            tzinfo=timezone.utc,
        )
        missing_from = datetime(
            nominal_date.year,
            nominal_date.month,
            nominal_date.day,
            STATUS_RESERVE_12_HOUR,
            STATUS_MAIN_CHECK_MINUTE,
            tzinfo=timezone.utc,
        )
    else:
        raise ValueError(f"Unsupported regular term: {term}")

    return waiting_from, missing_from


def load_status_file():
    empty = {
        "version": 1,
        "station": 14015,
        "station_name": "Ljubljana",
        "updated_at": None,
        "terms": {},
        "special_soundings": {},
    }

    if not os.path.exists(STATUS_FILE):
        return empty

    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print("WARNING: status.json could not be read:", exc)
        return empty

    if not isinstance(data, dict):
        return empty

    terms = data.get("terms", {})
    specials = data.get("special_soundings", {})

    if not isinstance(terms, dict):
        terms = {}
    if not isinstance(specials, dict):
        specials = {}

    empty.update(data)
    empty["terms"] = terms
    empty["special_soundings"] = specials
    return empty


def archive_status_metadata(nominal_date, term):
    """Return compact metadata for an archived sounding, or None."""
    path = archive_path(nominal_date, term)

    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            profile = json.load(f)
    except Exception as exc:
        print("WARNING: archived sounding could not be read:", path, exc)
        return None

    return {
        "launch_time": profile.get("launch_time"),
        "retrieved_at": profile.get("retrieved_at"),
        "archive_file": path.replace(os.sep, "/"),
        "profile_level_count": (
            profile.get("qc", {}).get("profile_level_count")
        ),
    }


def prune_status_data(data, now):
    cutoff_date = (now - timedelta(days=STATUS_RETENTION_DAYS)).date()

    kept_terms = {}
    for key, entry in data.get("terms", {}).items():
        try:
            nominal_date = datetime.strptime(
                entry.get("nominal_date", ""),
                "%Y-%m-%d",
            ).date()
        except Exception:
            # Keep unknown legacy entries rather than deleting them blindly.
            kept_terms[key] = entry
            continue

        if nominal_date >= cutoff_date:
            kept_terms[key] = entry

    kept_specials = {}
    for key, entry in data.get("special_soundings", {}).items():
        launch_text = entry.get("launch_time")
        try:
            launch_dt = datetime.fromisoformat(
                launch_text.replace("Z", "+00:00")
            )
        except Exception:
            kept_specials[key] = entry
            continue

        if launch_dt.date() >= cutoff_date:
            kept_specials[key] = entry

    data["terms"] = kept_terms
    data["special_soundings"] = kept_specials


def set_regular_term_status(data, nominal_date, term, now):
    key = status_key(nominal_date, term)
    waiting_from, missing_from = regular_status_times(
        nominal_date,
        term,
    )

    # Do not create a future term before its first operational check.
    if now < waiting_from:
        return

    old = data.setdefault("terms", {}).get(key, {})
    archived = archive_status_metadata(nominal_date, term)

    if archived is not None:
        entry = {
            "status": "ok",
            "nominal_date": nominal_date.isoformat(),
            "term": term,
            "waiting_from": iso_utc(waiting_from),
            "missing_after": iso_utc(missing_from),
            "checked_at": iso_utc(now),
            **archived,
        }

        # Preserve the first time this term entered the status file.
        if old.get("first_seen_at"):
            entry["first_seen_at"] = old["first_seen_at"]
        else:
            entry["first_seen_at"] = iso_utc(now)

        data["terms"][key] = entry
        return

    new_status = "missing" if now >= missing_from else "waiting"

    entry = {
        "status": new_status,
        "nominal_date": nominal_date.isoformat(),
        "term": term,
        "waiting_from": iso_utc(waiting_from),
        "missing_after": iso_utc(missing_from),
        "checked_at": iso_utc(now),
        "first_seen_at": old.get("first_seen_at", iso_utc(now)),
    }

    if new_status == "missing":
        entry["missing_since"] = old.get(
            "missing_since",
            iso_utc(missing_from),
        )

    data["terms"][key] = entry


def update_status_file(now, items):
    """Build the operational 00/12 status file from archive + current scan."""
    data = load_status_file()

    data["version"] = 1
    data["station"] = 14015
    data["station_name"] = "Ljubljana"

    # Explicitly register regular soundings found by this run. This matters
    # for manual catch-up runs and makes status recovery independent of the
    # exact current clock time.
    for item in items:
        term = item.get("term")
        nominal_date = item.get("nominal_date")
        profile = item.get("profile", {})

        if term in ("00", "12") and nominal_date is not None:
            key = status_key(nominal_date, term)
            waiting_from, missing_from = regular_status_times(
                nominal_date,
                term,
            )
            old = data.setdefault("terms", {}).get(key, {})
            archived = archive_status_metadata(nominal_date, term)

            if archived is None:
                archived = {
                    "launch_time": profile.get("launch_time"),
                    "retrieved_at": profile.get("retrieved_at"),
                    "archive_file": archive_path(
                        nominal_date,
                        term,
                    ).replace(os.sep, "/"),
                    "profile_level_count": (
                        profile.get("qc", {}).get(
                            "profile_level_count"
                        )
                    ),
                }

            data["terms"][key] = {
                "status": "ok",
                "nominal_date": nominal_date.isoformat(),
                "term": term,
                "waiting_from": iso_utc(waiting_from),
                "missing_after": iso_utc(missing_from),
                "checked_at": iso_utc(now),
                "first_seen_at": old.get(
                    "first_seen_at",
                    iso_utc(now),
                ),
                **archived,
            }

        elif term and term.startswith("special_"):
            launch_time = profile.get("launch_time")
            if launch_time:
                special_key = launch_time
                data.setdefault("special_soundings", {})[
                    special_key
                ] = {
                    "status": "special",
                    "launch_time": launch_time,
                    "nominal_date": (
                        nominal_date.isoformat()
                        if nominal_date is not None
                        else None
                    ),
                    "term": term,
                    "archive_file": archive_path(
                        nominal_date,
                        term,
                    ).replace(os.sep, "/")
                    if nominal_date is not None
                    else None,
                    "checked_at": iso_utc(now),
                }

    # Reconcile yesterday and today against the on-disk archive. This lets a
    # later run repair waiting/missing states even if the profile came from an
    # earlier run.
    today = now.date()
    for day_offset in (1, 0):
        date_to_check = today - timedelta(days=day_offset)
        for term in ("00", "12"):
            set_regular_term_status(
                data,
                date_to_check,
                term,
                now,
            )

    prune_status_data(data, now)

    current_term, current_date = expected_term(now)
    current_key = status_key(current_date, current_term)
    current_entry = data.get("terms", {}).get(current_key)

    data["current_expected"] = {
        "key": current_key,
        "nominal_date": current_date.isoformat(),
        "term": current_term,
        "status": (
            current_entry.get("status")
            if isinstance(current_entry, dict)
            else "not_due"
        ),
    }

    data["updated_at"] = iso_utc(now)

    os.makedirs("data", exist_ok=True)
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    print()
    print("=" * 60)
    print("STATUS SUMMARY")
    print("=" * 60)
    print("Current expected:", current_key)
    print(
        "Current status:",
        data["current_expected"]["status"],
    )

    recent = sorted(
        data.get("terms", {}).items(),
        reverse=True,
    )[:4]

    for key, entry in reversed(recent):
        print(key, "->", entry.get("status"))

    print(
        "Special soundings in status window:",
        len(data.get("special_soundings", {})),
    )
    print("=" * 60)

    return data


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
# PRIMERJAVA S PREJŠNJIMI SONDAŽAMI
# ============================================================

def nested_get(data, path):
    """Varno prebere gnezdeno vrednost iz slovarja."""
    current = data

    for key in path:
        if not isinstance(current, dict):
            return None

        current = current.get(key)

        if current is None:
            return None

    return current


def previous_term_reference(nominal_date, term):
    """Vrne datum/termin neposredno prejšnje sondaže."""
    if term == "12":
        return nominal_date, "00"

    return nominal_date - timedelta(days=1), "12"


def previous_day_reference(nominal_date, term):
    """Vrne isti termin prejšnjega dne."""
    return nominal_date - timedelta(days=1), term


def load_archived_sounding(nominal_date, term):
    path = archive_path(
        nominal_date,
        term
    )

    if not os.path.exists(path):
        return None, path

    try:
        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f), path

    except Exception as exc:
        print(
            "Previous sounding read warning:",
            path,
            exc
        )
        return None, path


COMPARISON_PARAMETERS = {
    "t850_c": (
        "T850",
        ["parameters", "standard_levels", "t850", "value"],
        "degC"
    ),
    "t700_c": (
        "T700",
        ["parameters", "standard_levels", "t700", "value"],
        "degC"
    ),
    "t500_c": (
        "T500",
        ["parameters", "standard_levels", "t500", "value"],
        "degC"
    ),
    "pwat_mm": (
        "PWAT",
        ["parameters", "metpy", "pwat_mm"],
        "mm"
    ),
    "freezing_level_msl_m": (
        "Freezing level",
        ["parameters", "metpy", "freezing_level_msl_m"],
        "m"
    ),
    "mlcape_jkg": (
        "MLCAPE",
        ["parameters", "metpy", "mlcape_jkg"],
        "J/kg"
    ),
    "mucape_jkg": (
        "MUCAPE",
        ["parameters", "metpy", "mucape_jkg"],
        "J/kg"
    ),
    "shear_0_6km_ms": (
        "0-6 km shear",
        ["parameters", "metpy", "shear_0_6km_ms"],
        "m/s"
    ),
    "q_surface_gkg": (
        "q surface",
        [
            "parameters", "metpy", "moisture_transport",
            "humidity_profile", "surface",
            "specific_humidity_gkg"
        ],
        "g/kg"
    ),
    "q925_gkg": (
        "q925",
        [
            "parameters", "metpy", "moisture_transport",
            "humidity_profile", "925",
            "specific_humidity_gkg"
        ],
        "g/kg"
    ),
    "q850_gkg": (
        "q850",
        [
            "parameters", "metpy", "moisture_transport",
            "humidity_profile", "850",
            "specific_humidity_gkg"
        ],
        "g/kg"
    ),
    "ivt_kg_m1_s1": (
        "IVT",
        [
            "parameters", "metpy", "moisture_transport",
            "ivt", "magnitude_kg_m1_s1"
        ],
        "kg m-1 s-1"
    ),
    "lapse_rate_850_500_c_per_km": (
        "Lapse rate 850-500",
        [
            "parameters", "metpy",
            "lapse_rate_850_500_c_per_km"
        ],
        "degC/km"
    ),
}


def build_comparison(current_profile, old_profile):
    """
    Primerja trenutno sondažo s starejšo.
    Delta = current - previous.
    """

    if old_profile is None:
        return {
            "available": False
        }

    values = {}

    for key, (
        label,
        path,
        unit
    ) in COMPARISON_PARAMETERS.items():

        current_value = nested_get(
            current_profile,
            path
        )

        previous_value = nested_get(
            old_profile,
            path
        )

        if (
            not valid_number(current_value)
            or not valid_number(previous_value)
        ):
            continue

        current_value = float(current_value)
        previous_value = float(previous_value)

        values[key] = {
            "label": label,
            "current": round(current_value, 2),
            "previous": round(previous_value, 2),
            "delta": round(
                current_value - previous_value,
                2
            ),
            "unit": unit,
        }

    return {
        "available": True,
        "previous_launch_time":
            old_profile.get("launch_time"),
        "previous_nominal_date":
            old_profile.get("nominal_date"),
        "previous_term":
            old_profile.get("term"),
        "delta_definition":
            "current_minus_previous",
        "parameters":
            values,
    }


def calculate_previous_comparisons(
    current_profile,
    nominal_date,
    term
):
    """
    Dve primerjavi za redna termina:
    1) neposredno prejšnji termin
    2) isti termin prejšnjega dne

    Izredne sondaže so ohranjene, vendar jih ne tlačimo v
    redno 00/12 primerjalno zaporedje.
    """

    if term not in ("00", "12"):
        return {
            "previous_term": {
                "available": False,
                "reason": "special sounding"
            },
            "previous_day_same_term": {
                "available": False,
                "reason": "special sounding"
            },
        }

    prev_term_date, prev_term = (
        previous_term_reference(
            nominal_date,
            term
        )
    )

    prev_day_date, prev_day_term = (
        previous_day_reference(
            nominal_date,
            term
        )
    )

    prev_term_profile, prev_term_path = (
        load_archived_sounding(
            prev_term_date,
            prev_term
        )
    )

    prev_day_profile, prev_day_path = (
        load_archived_sounding(
            prev_day_date,
            prev_day_term
        )
    )

    previous_term = build_comparison(
        current_profile,
        prev_term_profile
    )

    previous_term.update({
        "requested_nominal_date":
            prev_term_date.isoformat(),
        "requested_term":
            prev_term,
        "archive_file":
            prev_term_path,
    })

    previous_day = build_comparison(
        current_profile,
        prev_day_profile
    )

    previous_day.update({
        "requested_nominal_date":
            prev_day_date.isoformat(),
        "requested_term":
            prev_day_term,
        "archive_file":
            prev_day_path,
    })

    return {
        "previous_term":
            previous_term,
        "previous_day_same_term":
            previous_day,
    }


# ============================================================
# INVERZIJE / THETA / THETA-E
# ============================================================

def _moving_average(values, window_points):
    if window_points <= 1:
        return values.copy()
    if window_points % 2 == 0:
        window_points += 1
    half = window_points // 2
    padded = np.pad(values, (half, half), mode="edge")
    kernel = np.ones(window_points, dtype=float) / window_points
    return np.convolve(padded, kernel, mode="valid")


def _inversion_temperature_strength(delta_t_c, depth_m):
    """Describes the temperature inversion itself, not parcel capping."""
    if delta_t_c >= 1.0:
        return "strong"
    if delta_t_c >= 0.5:
        return "moderate"
    return "weak"


def _inversion_boundary_strength(delta_td_c, delta_theta_e_k):
    """Separate moisture/thermodynamic-boundary description."""
    magnitudes = []
    if valid_number(delta_td_c):
        magnitudes.append(abs(float(delta_td_c)))
    if valid_number(delta_theta_e_k):
        magnitudes.append(abs(float(delta_theta_e_k)))

    if not magnitudes:
        return "unknown"

    magnitude = max(magnitudes)
    if magnitude >= 3.0:
        return "strong"
    if magnitude >= 1.0:
        return "moderate"
    return "weak"


def calculate_inversions(levels):
    """
    Detect temperature inversions on a 10-m interpolated height grid.

    Surface--700 hPa: high sensitivity (20 m, +0.10 C).
    700--300 hPa: selective (50 m, +0.50 C).
    Above 300 hPa is deliberately excluded; tropopause structure will
    be handled separately.

    Temperature-inversion strength and moisture/thermodynamic-boundary
    strength are kept separate. No layer is labelled a 'cap' here;
    capping should later be assessed together with parcel/CIN diagnostics.
    """
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
        return {"available": False, "reason": "too few valid p/T/z levels"}

    z_obs = np.array([x[0] for x in clean], dtype=float)
    p_obs = np.array([x[1] for x in clean], dtype=float)
    t_obs = np.array([x[2] for x in clean], dtype=float)
    td_obs = np.array([x[3] for x in clean], dtype=float)
    station_z = float(z_obs[0])

    idx_300 = np.where(p_obs >= INVERSION_UPPER_BOUND_HPA)[0]
    if len(idx_300) < 2:
        return {"available": False, "reason": "profile does not reach 300 hPa"}

    top_z = float(z_obs[idx_300[-1]])
    grid_z = np.arange(station_z, top_z + INVERSION_GRID_STEP_M,
                       INVERSION_GRID_STEP_M)
    grid_t = np.interp(grid_z, z_obs, t_obs)
    grid_p = np.exp(np.interp(grid_z, z_obs, np.log(p_obs)))

    finite_td = np.isfinite(td_obs)
    if finite_td.sum() >= 2:
        grid_td = np.interp(grid_z, z_obs[finite_td], td_obs[finite_td])
    else:
        grid_td = np.full_like(grid_z, np.nan)

    window_points = max(1, round(INVERSION_SMOOTH_WINDOW_M /
                                 INVERSION_GRID_STEP_M))
    smooth_t = _moving_average(grid_t, window_points)
    smooth_td = _moving_average(grid_td, window_points)

    theta = (smooth_t + 273.15) * (1000.0 / grid_p) ** (287.05 / 1004.0)
    theta_e = np.full_like(theta, np.nan)
    for i in range(len(grid_z)):
        if not np.isfinite(smooth_td[i]):
            continue
        try:
            theta_e[i] = float(
                mpcalc.equivalent_potential_temperature(
                    grid_p[i] * units.hPa,
                    smooth_t[i] * units.degC,
                    smooth_td[i] * units.degC
                ).to("kelvin").magnitude
            )
        except Exception:
            pass

    # Raw contiguous layers where temperature rises with height.
    raw = []
    start = None
    for i, delta in enumerate(np.diff(smooth_t)):
        if delta > 0:
            if start is None:
                start = i
        elif start is not None:
            raw.append([start, i])
            start = None
    if start is not None:
        raw.append([start, len(smooth_t) - 1])

    # Merge tiny gaps so noise does not split one physical inversion.
    merged = []
    for segment in raw:
        if not merged:
            merged.append(segment[:])
            continue
        previous = merged[-1]
        gap_depth = grid_z[segment[0]] - grid_z[previous[1]]
        gap_cooling = smooth_t[previous[1]] - smooth_t[segment[0]]
        if (gap_depth <= INVERSION_MERGE_GAP_M and
                gap_cooling <= INVERSION_MERGE_COOLING_C):
            previous[1] = segment[1]
        else:
            merged.append(segment[:])

    layers = []
    rejected = 0
    for i0, i1 in merged:
        depth = float(grid_z[i1] - grid_z[i0])
        delta_t = float(smooth_t[i1] - smooth_t[i0])
        base_p = float(grid_p[i0])
        top_p = float(grid_p[i1])

        if base_p >= INVERSION_LOWER_BOUND_HPA:
            keep = (depth >= INVERSION_LOW_MIN_DEPTH_M and
                    delta_t >= INVERSION_LOW_MIN_DELTA_T_C)
            regime = "surface-700 hPa high sensitivity"
        else:
            keep = (top_p >= INVERSION_UPPER_BOUND_HPA and
                    depth >= INVERSION_MID_MIN_DEPTH_M and
                    delta_t >= INVERSION_MID_MIN_DELTA_T_C)
            regime = "700-300 hPa selective"

        if not keep:
            rejected += 1
            continue

        delta_td = None
        if np.isfinite(smooth_td[i0]) and np.isfinite(smooth_td[i1]):
            delta_td = float(smooth_td[i1] - smooth_td[i0])

        delta_theta_e = None
        if np.isfinite(theta_e[i0]) and np.isfinite(theta_e[i1]):
            delta_theta_e = float(theta_e[i1] - theta_e[i0])

        layer = {
            "type": "surface" if (grid_z[i0] - station_z) <= 50 else "elevated",
            "regime": regime,
            "base_msl_m": round(float(grid_z[i0]), 0),
            "top_msl_m": round(float(grid_z[i1]), 0),
            "base_agl_m": round(float(grid_z[i0] - station_z), 0),
            "top_agl_m": round(float(grid_z[i1] - station_z), 0),
            "base_pressure_hpa": round(base_p, 1),
            "top_pressure_hpa": round(top_p, 1),
            "depth_m": round(depth, 0),
            "base_temperature_c": round(float(smooth_t[i0]), 2),
            "top_temperature_c": round(float(smooth_t[i1]), 2),
            "delta_temperature_c": round(delta_t, 2),
            "temperature_gradient_c_per_km": round(1000.0 * delta_t / depth, 2),
            "temperature_inversion_strength": _inversion_temperature_strength(delta_t, depth),
            "base_theta_k": round(float(theta[i0]), 2),
            "top_theta_k": round(float(theta[i1]), 2),
            "delta_theta_k": round(float(theta[i1] - theta[i0]), 2),
            "base_dewpoint_c": round(float(smooth_td[i0]), 2) if np.isfinite(smooth_td[i0]) else None,
            "top_dewpoint_c": round(float(smooth_td[i1]), 2) if np.isfinite(smooth_td[i1]) else None,
            "delta_dewpoint_c": round(delta_td, 2) if delta_td is not None else None,
            "base_theta_e_k": round(float(theta_e[i0]), 2) if np.isfinite(theta_e[i0]) else None,
            "top_theta_e_k": round(float(theta_e[i1]), 2) if np.isfinite(theta_e[i1]) else None,
            "delta_theta_e_k": round(delta_theta_e, 2) if delta_theta_e is not None else None,
            "thermodynamic_boundary_strength": _inversion_boundary_strength(delta_td, delta_theta_e),
        }
        layers.append(layer)

    low_layers = [x for x in layers if x["base_pressure_hpa"] >= INVERSION_LOWER_BOUND_HPA]
    summary = {
        "total_count": len(layers),
        "surface_to_700_hpa_count": len(low_layers),
        "surface_to_700_hpa_total_depth_m": round(sum(x["depth_m"] for x in low_layers), 0),
        "strongest_low_level_delta_temperature_c": (
            max((x["delta_temperature_c"] for x in low_layers), default=None)
        ),
        "strongest_low_level_gradient_c_per_km": (
            max((x["temperature_gradient_c_per_km"] for x in low_layers), default=None)
        ),
        "rejected_candidate_count": rejected,
    }

    return {
        "available": True,
        "method": {
            "vertical_grid_m": INVERSION_GRID_STEP_M,
            "temperature_smoothing_m": INVERSION_SMOOTH_WINDOW_M,
            "surface_to_700_hpa": {
                "mode": "high_sensitivity",
                "minimum_depth_m": INVERSION_LOW_MIN_DEPTH_M,
                "minimum_delta_temperature_c": INVERSION_LOW_MIN_DELTA_T_C,
            },
            "700_to_300_hpa": {
                "mode": "selective",
                "minimum_depth_m": INVERSION_MID_MIN_DEPTH_M,
                "minimum_delta_temperature_c": INVERSION_MID_MIN_DELTA_T_C,
            },
            "above_300_hpa": "excluded; tropopause diagnostics handled separately",
            "merge_gap_m": INVERSION_MERGE_GAP_M,
            "merge_max_intervening_cooling_c": INVERSION_MERGE_COOLING_C,
        },
        "interpretation_note": (
            "Temperature inversion strength and thermodynamic/moisture boundary "
            "strength are separate diagnostics. No inversion is automatically "
            "classified as a convective cap; parcel and CIN context is required."
        ),
        "summary": summary,
        "layers": layers,
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

    launch_dt = datetime.fromisoformat(
        profile["launch_time"].replace(
            "Z",
            "+00:00"
        )
    )

    profile["sounding_classification"] = (
        classify_sounding(launch_dt)
    )

    profile["sounding_classification"][
        "nominal_date"
    ] = nominal_date.isoformat()

    profile["sounding_id"] = (
        nominal_date.strftime("%Y%m%d")
        + "_"
        + term
    )

    metpy_parameters = (
        calculate_metpy_parameters(
            profile["levels"]
        )
    )

    inversion_diagnostics = calculate_inversions(
        profile["levels"]
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

        "inversions":
            inversion_diagnostics,
    }

    profile["parameters"]["change"] = (
        calculate_previous_comparisons(
            profile,
            nominal_date,
            term
        )
    )

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

    inversions = p.get("inversions", {})
    if inversions.get("available"):
        inv_summary = inversions.get("summary", {})
        print(
            "Inversions surface-700 hPa:",
            inv_summary.get("surface_to_700_hpa_count"),
            "| total depth:",
            inv_summary.get("surface_to_700_hpa_total_depth_m"),
            "m"
        )

    standard_winds = metpy.get("standard_winds", {})

    if standard_winds:
        print()
        print("--- WIND ---")

        for pressure_level in ["925", "850", "700", "500", "300", "250", "200"]:
            item = standard_winds.get(pressure_level)
            if item is None:
                continue
            print(
                f"Wind {pressure_level}:",
                item.get("direction_deg"),
                "deg /",
                item.get("speed_ms"),
                "m/s"
            )

    right_mover = (
        metpy.get("bunkers", {})
             .get("right_mover")
    )

    if right_mover:
        print(
            "Bunkers RM:",
            right_mover.get("direction_deg"),
            "deg /",
            right_mover.get("speed_ms"),
            "m/s"
        )

    srh = metpy.get("srh", {})

    for depth_km in [1, 3]:
        item = srh.get(f"0_{depth_km}km_right_mover")
        if item is not None:
            print(
                f"SRH 0-{depth_km} km RM:",
                item.get("total_m2s2"),
                "m2/s2"
            )

    moisture_transport = metpy.get(
        "moisture_transport",
        {}
    )

    humidity_profile = moisture_transport.get(
        "humidity_profile",
        {}
    )

    mt850 = moisture_transport.get(
        "moisture_transport_850"
    )

    ivt = moisture_transport.get(
        "ivt"
    )

    if (
        humidity_profile
        or mt850 is not None
        or ivt is not None
    ):
        print()
        print("--- MOISTURE TRANSPORT ---")

    if humidity_profile:
        for label in ["surface", "925", "850"]:
            item = humidity_profile.get(label)
            if item is None:
                continue

            print(
                f"Humidity {label}:",
                "q =",
                item.get(
                    "specific_humidity_gkg"
                ),
                "g/kg | r =",
                item.get(
                    "mixing_ratio_gkg"
                ),
                "g/kg"
            )

        delta = humidity_profile.get(
            "surface_to_925",
            {}
        )

        print(
            "925 - surface:",
            "Delta q =",
            delta.get(
                "delta_q_925_minus_surface_gkg"
            ),
            "g/kg | Delta r =",
            delta.get(
                "delta_mixing_ratio_925_minus_surface_gkg"
            ),
            "g/kg"
        )

    if mt850 is not None:
        print(
            "q850:",
            mt850.get(
                "specific_humidity_gkg"
            ),
            "g/kg"
        )

        print(
            "qV850:",
            mt850.get(
                "qv_magnitude_kgkg_ms"
            ),
            "kg/kg m/s | toward",
            mt850.get(
                "transport_to_direction_deg"
            ),
            "deg"
        )

    if (
        ivt is not None
        and ivt.get("available")
    ):
        print(
            "IVT:",
            ivt.get(
                "magnitude_kg_m1_s1"
            ),
            "kg m-1 s-1 | toward",
            ivt.get(
                "transport_to_direction_deg"
            ),
            "deg"
        )

        print(
            "IVT layer:",
            ivt.get(
                "bottom_pressure_hpa"
            ),
            "to",
            ivt.get(
                "top_pressure_hpa"
            ),
            "hPa"
        )

    change = p.get(
        "change",
        {}
    )

    if change:
        print()
        print("--- CHANGE ---")

        for title, key in [
            (
                "Previous term",
                "previous_term"
            ),
            (
                "Previous day same term",
                "previous_day_same_term"
            ),
        ]:
            item = change.get(key, {})

            print()
            print(
                title + ":",
                item.get(
                    "requested_nominal_date"
                ),
                item.get(
                    "requested_term"
                ),
                "UTC"
            )

            if not item.get("available"):
                print(
                    "  archived sounding "
                    "not available"
                )
                continue

            values = item.get(
                "parameters",
                {}
            )

            for parameter in [
                "t850_c",
                "t700_c",
                "t500_c",
                "pwat_mm",
                "freezing_level_msl_m",
                "q_surface_gkg",
                "q925_gkg",
                "q850_gkg",
                "ivt_kg_m1_s1",
                "shear_0_6km_ms",
                "mlcape_jkg",
                "mucape_jkg",
            ]:
                value = values.get(parameter)

                if value is None:
                    continue

                delta = value.get("delta")
                sign = (
                    "+"
                    if (
                        delta is not None
                        and delta > 0
                    )
                    else ""
                )

                print(
                    " ",
                    value.get("label") + ":",
                    value.get("current"),
                    "| prev",
                    value.get("previous"),
                    "| delta",
                    f"{sign}{delta}",
                    value.get("unit")
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

    run_started = time.perf_counter()
    directory_started = time.perf_counter()

    now = datetime.now(
        timezone.utc
    )

    print(
        "Current UTC:",
        now.isoformat()
    )

    manual_run = is_manual_run()

    if manual_run:
        expected_term_name = None
        expected_nominal_date = None
    else:
        expected_term_name, expected_nominal_date = expected_term(now)

    print(
        "Mode:",
        "manual catch-up (regular + special)"
        if manual_run
        else (
            "scheduled target "
            f"{expected_nominal_date} {expected_term_name} UTC"
        )
    )

    scan_cache = load_dwd_scan_cache(now)

    candidates = find_candidate_files()
    directory_elapsed = time.perf_counter() - directory_started

    print(
        "Candidate DWD packages:",
        len(candidates)
    )
    print(
        "Directory/filter time:",
        f"{directory_elapsed:.1f} s"
    )

    downloaded_count = 0
    downloaded_bytes = 0
    download_seconds = 0.0
    bufr_seconds = 0.0
    packages_with_ljlm = 0
    packages_skipped_cache = 0
    early_stop_used = False
    early_stop_remaining = None

    found = {}

    for number, filename in enumerate(
        candidates,
        start=1
    ):

        cached = scan_cache.get(
            "packages", {}
        ).get(filename)

        # Negative cache entries are safe to skip. Positive LJLM packages
        # are rescanned so duplicate/updated profiles can still be compared.
        if (
            isinstance(cached, dict)
            and cached.get("contains_ljlm") is False
        ):
            packages_skipped_cache += 1
            print(
                f"[{number}/{len(candidates)}] "
                f"{unquote(filename)}"
                "  -> cached: no LJLM"
            )

            if early_stop_remaining is not None:
                early_stop_remaining -= 1
                if early_stop_remaining <= 0:
                    early_stop_used = True
                    print(
                        "Scheduled early-stop: expected sounding found "
                        "and tail packages checked."
                    )
                    break

            continue

        print(
            f"[{number}/{len(candidates)}] "
            f"{unquote(filename)}"
        )

        try:
            download_started = time.perf_counter()
            content = download(
                DWD_URL + filename
            )
            one_download_s = time.perf_counter() - download_started
            download_seconds += one_download_s
            downloaded_count += 1
            downloaded_bytes += len(content)
            print(
                "  download:",
                f"{len(content) / 1024:.1f} KiB",
                f"in {one_download_s:.2f} s"
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

            bufr_started = time.perf_counter()
            profile = read_ljlm_from_bufr(
                tmp_path
            )
            one_bufr_s = time.perf_counter() - bufr_started
            bufr_seconds += one_bufr_s
            print(
                "  BUFR scan:",
                f"{one_bufr_s:.2f} s"
            )

            update_dwd_scan_cache(
                scan_cache,
                filename,
                profile
            )

            if profile is None:
                if early_stop_remaining is not None:
                    early_stop_remaining -= 1
                    if early_stop_remaining <= 0:
                        early_stop_used = True
                        print(
                            "Scheduled early-stop: expected sounding found "
                            "and tail packages checked."
                        )
                        break
                continue

            launch = datetime.fromisoformat(
                profile[
                    "launch_time"
                ].replace(
                    "Z",
                    "+00:00"
                )
            )

            classification = (
                classify_sounding(
                    launch
                )
            )

            term = classification["term"]
            nominal_date = (
                classification["nominal_date"]
            )

            packages_with_ljlm += 1

            print(
                "LJLM found:",
                profile["launch_time"],
                "->",
                nominal_date,
                term,
                classification["kind"]
            )

            if (
                not manual_run
                and term == expected_term_name
                and nominal_date == expected_nominal_date
            ):
                # Reset the tail on every matching duplicate. This gives
                # nearby duplicate packages a chance to provide a fuller
                # profile while still avoiding a complete 3-hour scan.
                early_stop_remaining = (
                    SCHEDULED_EARLY_STOP_TAIL_PACKAGES
                )

            # Same launch may occur in more than one DWD package.
            # Keep the copy with the largest profile.
            key = profile["launch_time"]

            old = found.get(key)

            if (
                old is None
                or profile.get(
                    "qc", {}
                ).get(
                    "profile_level_count",
                    0
                )
                > old["profile"].get(
                    "qc", {}
                ).get(
                    "profile_level_count",
                    0
                )
            ):
                found[key] = {
                    "profile": profile,
                    "filename": filename,
                    "term": term,
                    "nominal_date":
                        nominal_date,
                }

            if early_stop_remaining is not None:
                early_stop_remaining -= 1
                if early_stop_remaining <= 0:
                    early_stop_used = True
                    print(
                        "Scheduled early-stop: expected sounding found "
                        "and tail packages checked."
                    )
                    break

        except Exception as exc:
            print(
                "Package warning:",
                exc
            )

        finally:
            if (
                tmp_path
                and os.path.exists(
                    tmp_path
                )
            ):
                os.remove(tmp_path)

    # Persist all successful negative scans even if no LJLM profile was found.
    save_dwd_scan_cache(scan_cache)

    items = sorted(
        found.values(),
        key=lambda item:
            item["profile"]["launch_time"]
    )

    if not items:
        update_status_file(
            now,
            items
        )

        print()
        print(
            "No LJLM sounding found "
            "in candidate packages."
        )
        return

    saved_count = 0

    for item in items:
        profile = item["profile"]
        term = item["term"]
        nominal_date = item["nominal_date"]

        if sounding_already_saved(
            nominal_date,
            term,
            profile["launch_time"]
        ):
            print(
                "Already complete:",
                profile["launch_time"],
                term
            )
            continue

        archive_file = save_json(
            profile,
            item["filename"],
            term,
            nominal_date
        )

        saved_count += 1

        print()
        print(
            "Saved new/updated sounding:",
            archive_file
        )

    # Rebuild regular comparisons after all newly discovered
    # profiles are on disk.
    if saved_count:
        for item in items:
            profile = item["profile"]
            term = item["term"]
            nominal_date = item[
                "nominal_date"
            ]

            if term not in ("00", "12"):
                continue

            save_json(
                profile,
                item["filename"],
                term,
                nominal_date
            )

    # Ensure latest.json is the newest profile from this scan.
    newest = items[-1]

    save_json(
        newest["profile"],
        newest["filename"],
        newest["term"],
        newest["nominal_date"]
    )

    update_status_file(
        now,
        items
    )

    print_summary(
        newest["profile"]
    )

    print()
    print(
        "Unique LJLM launches found:",
        len(items)
    )

    print(
        "New/updated soundings:",
        saved_count
    )

    print(
        "Newest:",
        newest["profile"]["launch_time"],
        newest["term"]
    )

    total_elapsed = time.perf_counter() - run_started

    print()
    print("=" * 60)
    print("PERFORMANCE SUMMARY")
    print("=" * 60)
    print("Candidate packages:", len(candidates))
    print("Packages skipped by cache:", packages_skipped_cache)
    print("Packages downloaded:", downloaded_count)
    print("Downloaded data:", f"{downloaded_bytes / (1024 * 1024):.2f} MiB")
    print("Packages containing LJLM:", packages_with_ljlm)
    print("Scheduled early-stop used:", early_stop_used)
    print("Unique LJLM launches:", len(items))
    print("Directory/filter:", f"{directory_elapsed:.1f} s")
    print("Downloads total:", f"{download_seconds:.1f} s")
    print("BUFR scans total:", f"{bufr_seconds:.1f} s")
    print("Other processing:", f"{max(0.0, total_elapsed - directory_elapsed - download_seconds - bufr_seconds):.1f} s")
    print("TOTAL:", f"{total_elapsed:.1f} s")
    print("=" * 60)

if __name__ == "__main__":
    import sys

    try:
        main()

        # Pomembno pri GitHub Actions:
        # ecCodes/eccodeslib se lahko pri normalnem zaključevanju
        # interpreterja sesuje z:
        #   free(): invalid pointer
        #
        # Do tega pride šele po uspešno končanem main().
        # os._exit(0) preskoči problematično native cleanup fazo.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    except Exception:
        # Prave Python napake morajo še vedno povzročiti
        # neuspešen GitHub Actions workflow.
        import traceback

        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
