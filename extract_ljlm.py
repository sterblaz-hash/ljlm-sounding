import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
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
# LJUBLJANA RADIOSONDE - DWD BUFR
# WMO 14015
# ============================================================

DWD_URL = "https://opendata.dwd.de/weather/weather_reports/radiosonde/bufr/"
WMO_BLOCK = 14
WMO_STATION = 15

OUTPUT_LATEST = "data/latest.json"


def download(url):
    """Download a file and return its bytes."""
    req = Request(
        url,
        headers={"User-Agent": "ljlm-sounding/1.0"}
    )
    with urlopen(req, timeout=60) as response:
        return response.read()


def get_directory_listing():
    """Return HTML directory listing from DWD."""
    return download(DWD_URL).decode("utf-8", errors="ignore")


def find_candidate_files():
    """
    Find recent TEMP BUFR packages in the DWD directory.

    We deliberately inspect only recent files because LJLM should
    appear in one of the packages around the 00/12 UTC sounding.
    """
    html = get_directory_listing()

    import re

    files = re.findall(
        r'href="([^"]*temp_bufr[^"]*\.bin)"',
        html,
        flags=re.IGNORECASE,
    )

    # Decode %2C etc. and remove "latest", because latest does not
    # necessarily contain Ljubljana.
    files = [unquote(f) for f in files]
    files = [
        f for f in files
        if "latest" not in f.lower()
    ]

    # Filenames contain YYYYMMDDHHMMSS, so sorting by name also
    # gives us chronological order.
    files = sorted(set(files), reverse=True)

    # Searching the most recent packages is normally sufficient.
    # We keep a generous number for robustness.
    return files[:120]


def safe_get(handle, key, default=None):
    try:
        return codes_get(handle, key)
    except Exception:
        return default


def safe_array(handle, key):
    try:
        return list(codes_get_array(handle, key))
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


def read_ljlm_from_bufr(path):
    """Search one BUFR file for WMO 14015."""

    with open(path, "rb") as f:
        while True:
            try:
                handle = codes_bufr_new_from_file(f)
            except Exception:
                # Some DWD packages may contain a malformed/truncated
                # message. Skip the remainder of this package.
                break

            if handle is None:
                break

            try:
                codes_set(handle, "unpack", 1)

                block = safe_get(handle, "blockNumber")
                station = safe_get(handle, "stationNumber")

                if block != WMO_BLOCK or station != WMO_STATION:
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
                    tzinfo=timezone.utc,
                )

                latitude = safe_get(handle, "latitude")
                longitude = safe_get(handle, "longitude")

                pressure = safe_array(handle, "pressure")
                temperature = safe_array(handle, "airTemperature")
                height = safe_array(
                    handle,
                    "nonCoordinateGeopotentialHeight"
                )
                rh = safe_array(handle, "relativeHumidity")
                wind_speed = safe_array(handle, "windSpeed")
                wind_direction = safe_array(handle, "windDirection")

                n = max(
                    len(pressure),
                    len(temperature),
                    len(height),
                    len(rh),
                    len(wind_speed),
                    len(wind_direction),
                )

                levels = []

                for i in range(n):

                    p = pressure[i] if i < len(pressure) else None
                    t = temperature[i] if i < len(temperature) else None
                    z = height[i] if i < len(height) else None
                    r = rh[i] if i < len(rh) else None
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

                    # Ignore clearly impossible pressure values.
                    if p_hpa <= 0 or p_hpa > 1100:
                        continue

                    level = {
                        "pressure_hpa": round(p_hpa, 2),
                        "height_m": (
                            round(float(z), 1)
                            if valid_number(z)
                            else None
                        ),
                        "temperature_c": (
                            round(float(t) - 273.15, 2)
                            if valid_number(t)
                            else None
                        ),
                        "relative_humidity_pct": (
                            round(float(r), 1)
                            if valid_number(r)
                            else None
                        ),
                        "wind_speed_ms": (
                            round(float(ws), 2)
                            if valid_number(ws)
                            else None
                        ),
                        "wind_direction_deg": (
                            round(float(wd), 1)
                            if valid_number(wd)
                            else None
                        ),
                    }

                    levels.append(level)

                # Sort from surface toward upper atmosphere.
                levels.sort(
                    key=lambda x: x["pressure_hpa"],
                    reverse=True
                )

                return {
                    "station": 14015,
                    "station_name": "Ljubljana",
                    "launch_time": launch_time.isoformat()
                    .replace("+00:00", "Z"),
                    "latitude": latitude,
                    "longitude": longitude,
                    "levels": levels,
                }

            except Exception as exc:
                print(f"BUFR message error: {exc}")

            finally:
                codes_release(handle)

    return None


def temperature_at_pressure(levels, target=850.0):
    """
    Return temperature at target pressure.

    If an actual observation exists very close to target pressure,
    use it directly. Otherwise interpolate logarithmically in pressure.
    """

    usable = [
        x for x in levels
        if x["temperature_c"] is not None
    ]

    # Exact / effectively exact level first.
    exact = [
        x for x in usable
        if abs(x["pressure_hpa"] - target) <= 0.1
    ]

    if exact:
        return {
            "value_c": exact[0]["temperature_c"],
            "method": "measured",
            "pressure_hpa": exact[0]["pressure_hpa"],
        }

    above = None
    below = None

    for level in usable:
        p = level["pressure_hpa"]

        if p > target:
            if above is None or p < above["pressure_hpa"]:
                above = level

        elif p < target:
            if below is None or p > below["pressure_hpa"]:
                below = level

    if above is None or below is None:
        return None

    p1 = above["pressure_hpa"]
    p2 = below["pressure_hpa"]

    # Avoid interpolation across a very large vertical data gap.
    if abs(p1 - p2) > 100:
        return None

    t1 = above["temperature_c"]
    t2 = below["temperature_c"]

    fraction = (
        math.log(target / p1)
        / math.log(p2 / p1)
    )

    value = t1 + fraction * (t2 - t1)

    return {
        "value_c": round(value, 2),
        "method": "log_pressure_interpolation",
        "between_hpa": [p1, p2],
    }


def save_json(profile, source_file):
    """Save latest sounding and archive copy."""

    t850 = temperature_at_pressure(
        profile["levels"],
        850.0
    )

    profile["source"] = "DWD Open Data BUFR"
    profile["source_file"] = source_file
    profile["retrieved_at"] = (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )

    profile["parameters"] = {
        "t850": t850
    }

    os.makedirs("data", exist_ok=True)

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

    launch = datetime.fromisoformat(
        profile["launch_time"].replace("Z", "+00:00")
    )

    archive_dir = os.path.join(
        "data",
        f"{launch.year:04d}",
        f"{launch.month:02d}",
    )

    os.makedirs(
        archive_dir,
        exist_ok=True
    )

    term = "00" if launch.hour < 6 or launch.hour >= 18 else "12"

    archive_file = os.path.join(
        archive_dir,
        f"{launch:%Y%m%d}_{term}.json"
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


def main():

    print("Searching DWD radiosonde BUFR packages...")
    candidates = find_candidate_files()

    print(f"Found {len(candidates)} candidate packages.")

    for number, filename in enumerate(candidates, start=1):

        url = DWD_URL + filename

        print(
            f"[{number}/{len(candidates)}] "
            f"{filename}"
        )

        try:
            content = download(url)
        except Exception as exc:
            print(f"Download failed: {exc}")
            continue

        tmp_path = None

        try:
            with tempfile.NamedTemporaryFile(
                suffix=".bufr",
                delete=False
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name

            profile = read_ljlm_from_bufr(tmp_path)

            if profile is None:
                continue

            print()
            print("LJUBLJANA 14015 FOUND")
            print(
                "Launch:",
                profile["launch_time"]
            )

            archive_file = save_json(
                profile,
                filename
            )

            t850 = profile["parameters"]["t850"]

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
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    print("LJLM 14015 was not found.")
    sys.exit(1)


if __name__ == "__main__":
    main()
