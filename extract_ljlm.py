import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.parse import unquote

from eccodes import (
    codes_bufr_new_from_file,
    codes_get,
    codes_get_array,
    codes_release,
    codes_set,
)


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

# Koliko najnovejših DWD paketov pregledamo.
# Za začetek pustimo nekaj rezerve.
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


# ============================================================
# DWD DATOTEKE
# ============================================================

def find_candidate_files():
    """
    Poišče najnovejše DWD TEMP BUFR pakete.

    Pomembno:
    za URL ohranimo originalno kodirano ime (%2C),
    da se izognemo težavam pri prenosu.
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

    return files[:MAX_CANDIDATE_FILES]


# ============================================================
# POMOŽNE FUNKCIJE ZA BUFR
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
# BRANJE LJUBLJANSKE SONDAŽE IZ BUFR
# ============================================================

def read_ljlm_from_bufr(path):

    with open(path, "rb") as f:

        while True:

            handle = None

            try:
                handle = codes_bufr_new_from_file(f)

            except Exception as exc:
                print(
                    "BUFR read warning:",
                    exc
                )
                break

            if handle is None:
                break

            try:

                codes_set(
                    handle,
                    "unpack",
                    1
                )

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
                # ČAS SONDAŽE
                # --------------------------------------------

                year = safe_get(handle, "year")
                month = safe_get(handle, "month")
                day = safe_get(handle, "day")

                hour = safe_get(
                    handle,
                    "hour",
                    0
                )

                minute = safe_get(
                    handle,
                    "minute",
                    0
                )

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
                # PROFIL
                # --------------------------------------------

                pressure = safe_array(
                    handle,
                    "pressure"
                )

                temperature = safe_array(
                    handle,
                    "airTemperature"
                )

                height = safe_array(
                    handle,
                    "nonCoordinateGeopotentialHeight"
                )

                rh = safe_array(
                    handle,
                    "relativeHumidity"
                )

                wind_speed = safe_array(
                    handle,
                    "windSpeed"
                )

                wind_direction = safe_array(
                    handle,
                    "windDirection"
                )

                n = max(
                    len(pressure),
                    len(temperature),
                    len(height),
                    len(rh),
                    len(wind_speed),
                    len(wind_direction)
                )

                levels = []

                for i in range(n):

                    p = (
                        pressure[i]
                        if i < len(pressure)
                        else None
                    )

                    t = (
                        temperature[i]
                        if i < len(temperature)
                        else None
                    )

                    z = (
                        height[i]
                        if i < len(height)
                        else None
                    )

                    r = (
                        rh[i]
                        if i < len(rh)
                        else None
                    )

                    ws = (
                        wind_speed[i]
                        if i < len(wind_speed)
                        else None
                    )

                    wd = (
                        wind_direction[i]
                        if i < len(wind_direction)
                        else None
                    )

                    if not valid_number(p):
                        continue

                    p_hpa = float(p) / 100.0

                    if (
                        p_hpa <= 0
                        or p_hpa > 1100
                    ):
                        continue

                    level = {
                        "pressure_hpa":
                            round(p_hpa, 2),

                        "height_m":
                            round(float(z), 1)
                            if valid_number(z)
                            else None,

                        "temperature_c":
                            round(
                                float(t) - 273.15,
                                2
                            )
                            if valid_number(t)
                            else None,

                        "relative_humidity_pct":
                            round(float(r), 1)
                            if valid_number(r)
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
# T850
# ============================================================

def temperature_at_pressure(
    levels,
    target=850.0
):

    usable = [
        x for x in levels
        if x["temperature_c"] is not None
    ]

    # --------------------------------------------
    # Neposredna meritev
    # --------------------------------------------

    exact = [
        x for x in usable
        if abs(
            x["pressure_hpa"] - target
        ) <= 0.1
    ]

    if exact:

        return {
            "value_c":
                exact[0]["temperature_c"],
            "method":
                "measured",
            "pressure_hpa":
                exact[0]["pressure_hpa"]
        }

    # --------------------------------------------
    # Interpolacija
    # --------------------------------------------

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

    # Ne interpoliramo čez zelo veliko vrzel.
    if abs(p1 - p2) > 100:
        return None

    t1 = above["temperature_c"]
    t2 = below["temperature_c"]

    fraction = (
        math.log(target / p1)
        / math.log(p2 / p1)
    )

    value = (
        t1
        + fraction * (t2 - t1)
    )

    return {
        "value_c":
            round(value, 2),
        "method":
            "log_pressure_interpolation",
        "between_hpa":
            [p1, p2]
    }


# ============================================================
# DOLOČITEV PRIČAKOVANEGA TERMINA
# ============================================================

def expected_sounding_time(now=None):
    """
    Vrne pričakovani sinoptični termin:
    00 ali 12 UTC.

    Ljubljanska sonda se lahko dejansko izpusti
    približno 30 minut pred terminom.

    Primer:
    20. 9. 23:30 UTC -> termin 21. 9. 00 UTC.
    """

    if now is None:
        now = datetime.now(timezone.utc)

    if now.hour < 6:

        expected = now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )

        term = "00"

    elif now.hour < 18:

        expected = now.replace(
            hour=12,
            minute=0,
            second=0,
            microsecond=0
        )

        term = "12"

    else:

        expected = (
            now
            + timedelta(days=1)
        ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )

        term = "00"

    return expected, term


# ============================================================
# PREVERJANJE SVEŽINE SONDAŽE
# ============================================================

def sounding_matches_expected_term(
    launch_time,
    expected_time
):
    """
    Dovolimo čas izpusta od 90 minut pred
    nominalnim terminom do 3 ure po njem.

    To pokrije npr.:
    23:30 -> 00 UTC
    11:30 -> 12 UTC
    """

    start = (
        expected_time
        - timedelta(minutes=90)
    )

    end = (
        expected_time
        + timedelta(hours=3)
    )

    return start <= launch_time <= end


# ============================================================
# SHRANJEVANJE JSON
# ============================================================

def save_json(
    profile,
    source_file,
    term
):

    t850 = temperature_at_pressure(
        profile["levels"],
        850.0
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

    profile["parameters"] = {
        "t850": t850
    }

    os.makedirs(
        "data",
        exist_ok=True
    )

    # --------------------------------------------
    # latest.json
    # --------------------------------------------

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

    # --------------------------------------------
    # Arhiv
    # --------------------------------------------

    launch = datetime.fromisoformat(
        profile["launch_time"]
        .replace("Z", "+00:00")
    )

    # Datum arhiva naj pripada nominalnemu terminu.
    # 23:30 prejšnjega dne je torej naslednji dan 00 UTC.

    if term == "00" and launch.hour >= 18:

        archive_date = (
            launch
            + timedelta(days=1)
        )

    else:

        archive_date = launch

    archive_dir = os.path.join(
        "data",
        f"{archive_date.year:04d}",
        f"{archive_date.month:02d}"
    )

    os.makedirs(
        archive_dir,
        exist_ok=True
    )

    archive_file = os.path.join(
        archive_dir,
        f"{archive_date:%Y%m%d}_{term}.json"
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
# GLAVNI PROGRAM
# ============================================================

def main():

    now = datetime.now(timezone.utc)

    expected_time, term = (
        expected_sounding_time(now)
    )

    print(
        "Current UTC time:",
        now.isoformat()
    )

    print(
        "Expected sounding term:",
        expected_time.isoformat(),
        f"({term} UTC)"
    )

    print()
    print(
        "Searching DWD radiosonde BUFR packages..."
    )

    candidates = find_candidate_files()

    print(
        f"Found {len(candidates)} "
        "candidate packages."
    )

    # --------------------------------------------
    # Pregled paketov
    # --------------------------------------------

    for number, filename in enumerate(
        candidates,
        start=1
    ):

        display_name = unquote(filename)

        print(
            f"[{number}/"
            f"{len(candidates)}] "
            f"{display_name}"
        )

        url = DWD_URL + filename

        try:

            content = download(url)

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

            # ------------------------------------
            # Našli smo LJLM
            # ------------------------------------

            launch = datetime.fromisoformat(
                profile["launch_time"]
                .replace("Z", "+00:00")
            )

            print()
            print(
                "LJUBLJANA 14015 FOUND"
            )

            print(
                "Launch:",
                profile["launch_time"]
            )

            # ------------------------------------
            # Preverimo termin
            # ------------------------------------

            if not sounding_matches_expected_term(
                launch,
                expected_time
            ):

                print()
                print(
                    "Sounding does not belong "
                    "to the expected term."
                )

                print(
                    "Expected:",
                    expected_time.isoformat()
                )

                print(
                    "Found:",
                    launch.isoformat()
                )

                print(
                    "The current latest.json "
                    "will NOT be changed."
                )

                # To NI napaka.
                # Naslednji scheduled run bo
                # poskusil ponovno.
                return

            # ------------------------------------
            # Prava sondaža
            # ------------------------------------

            archive_file = save_json(
                profile,
                filename,
                term
            )

            t850 = profile[
                "parameters"
            ]["t850"]

            print()
            print(
                "Correct sounding term found."
            )

            if t850:

                print(
                    "T850:",
                    t850["value_c"],
                    "°C",
                    f"({t850['method']})"
                )

            print(
                "Levels:",
                len(profile["levels"])
            )

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
                and os.path.exists(tmp_path)
            ):

                os.remove(tmp_path)

    # --------------------------------------------
    # LJLM sploh ni bilo med paketi
    # --------------------------------------------

    print()
    print(
        "LJLM 14015 was not found "
        "in the available recent packages."
    )

    print(
        "The current latest.json "
        "will NOT be changed."
    )

    # Tudi to pri avtomatskem preverjanju
    # ni razlog za rdeč workflow.
    # Naslednji zagon poskusi ponovno.
    return


if __name__ == "__main__":
    main()
