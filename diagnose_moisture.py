import re
import os
import tempfile
from urllib.request import urlopen, Request

from eccodes import (
    codes_bufr_new_from_file,
    codes_get,
    codes_get_array,
    codes_release,
    codes_set,
)

DWD_URL = (
    "https://opendata.dwd.de/weather/"
    "weather_reports/radiosonde/bufr/"
)

WMO_BLOCK = 14
WMO_STATION = 15
MAX_FILES = 250


def download(url):
    req = Request(
        url,
        headers={"User-Agent": "ljlm-sounding/1.0"}
    )
    with urlopen(req, timeout=60) as response:
        return response.read()


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


def get_candidate_files():

    html = download(DWD_URL).decode(
        "utf-8",
        errors="ignore"
    )

    files = re.findall(
        r'href="([^"]*temp_bufr[^"]*\.bin)"',
        html,
        flags=re.IGNORECASE
    )

    files = [
        f for f in files
        if "latest" not in f.lower()
    ]

    return sorted(
        set(files),
        reverse=True
    )[:MAX_FILES]


def celsius(value):
    try:
        if value > 1e10:
            return None
        return value - 273.15
    except Exception:
        return None


def print_row(i, p, t, td):

    p_value = (
        p[i] / 100.0
        if i < len(p)
        else None
    )

    t_value = (
        celsius(t[i])
        if i < len(t)
        else None
    )

    td_value = (
        celsius(td[i])
        if i < len(td)
        else None
    )

    print(
        f"{i:5d} | "
        f"p={str(round(p_value, 2) if p_value is not None else None):>9} hPa | "
        f"T={str(round(t_value, 2) if t_value is not None else None):>8} C | "
        f"Td={str(round(td_value, 2) if td_value is not None else None):>8} C"
    )


def diagnose(handle):

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

    print()
    print("=" * 80)
    print("LJUBLJANA 14015")
    print("=" * 80)

    print(
        "Launch:",
        safe_get(handle, "year"),
        safe_get(handle, "month"),
        safe_get(handle, "day"),
        safe_get(handle, "hour"),
        safe_get(handle, "minute")
    )

    print()
    print("ARRAY LENGTHS")
    print("-" * 80)

    print("pressure             :", len(pressure))
    print("airTemperature       :", len(temperature))
    print("dewpointTemperature  :", len(dewpoint))
    print("height               :", len(height))
    print("windSpeed            :", len(wind_speed))
    print("windDirection        :", len(wind_direction))

    print()
    print("=" * 80)
    print("FIRST 40 VALUES")
    print("=" * 80)

    for i in range(
        min(
            40,
            max(
                len(pressure),
                len(temperature),
                len(dewpoint)
            )
        )
    ):
        print_row(
            i,
            pressure,
            temperature,
            dewpoint
        )

    print()
    print("=" * 80)
    print("VALUES AROUND 850 hPa")
    print("=" * 80)

    for i, value in enumerate(pressure):

        try:
            p_hpa = value / 100.0
        except Exception:
            continue

        if 800 <= p_hpa <= 900:

            start = max(
                0,
                i - 5
            )

            end = min(
                len(pressure),
                i + 6
            )

            for j in range(
                start,
                end
            ):
                print_row(
                    j,
                    pressure,
                    temperature,
                    dewpoint
                )

            print("-" * 80)

    print()
    print("=" * 80)
    print("LAST 40 VALUES")
    print("=" * 80)

    maximum = max(
        len(pressure),
        len(temperature),
        len(dewpoint)
    )

    start = max(
        0,
        maximum - 40
    )

    for i in range(
        start,
        maximum
    ):
        print_row(
            i,
            pressure,
            temperature,
            dewpoint
        )

    print()
    print("=" * 80)
    print("END OF DIAGNOSTICS")
    print("=" * 80)


def scan_bufr(path):

    with open(path, "rb") as f:

        while True:

            handle = None

            try:
                handle = (
                    codes_bufr_new_from_file(f)
                )
            except Exception:
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
                    block == WMO_BLOCK
                    and station == WMO_STATION
                ):

                    diagnose(handle)
                    return True

            except Exception as exc:

                print(
                    "BUFR error:",
                    exc
                )

            finally:

                if handle is not None:
                    codes_release(handle)

    return False


def main():

    print(
        "Searching latest Ljubljana sounding..."
    )

    files = get_candidate_files()

    print(
        "Candidate packages:",
        len(files)
    )

    for number, filename in enumerate(
        files,
        start=1
    ):

        print(
            f"[{number}/{len(files)}] "
            f"{filename}"
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

            if scan_bufr(tmp_path):
                return

        finally:

            if (
                tmp_path
                and os.path.exists(tmp_path)
            ):
                os.remove(tmp_path)

    print(
        "Ljubljana 14015 not found."
    )


if __name__ == "__main__":
    main()
