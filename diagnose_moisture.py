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
    codes_keys_iterator_new,
    codes_keys_iterator_next,
    codes_keys_iterator_get_name,
    codes_keys_iterator_delete,
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

    files = sorted(
        set(files),
        reverse=True
    )

    return files[:MAX_FILES]


def print_array(handle, key):

    values = safe_array(handle, key)

    print()
    print("=" * 60)
    print(key)
    print("=" * 60)

    print("Number of values:", len(values))

    if values:
        print("First 30 values:")

        for i, value in enumerate(values[:30]):
            print(
                f"{i:3d}: {value}"
            )


def diagnose_handle(handle):

    print()
    print("#" * 70)
    print("LJUBLJANA WMO 14015 FOUND")
    print("#" * 70)

    year = safe_get(handle, "year")
    month = safe_get(handle, "month")
    day = safe_get(handle, "day")
    hour = safe_get(handle, "hour")
    minute = safe_get(handle, "minute")

    print(
        "Launch:",
        year,
        month,
        day,
        hour,
        minute
    )

    # --------------------------------------------------------
    # 1. Poskusimo najpogostejša imena spremenljivk
    # --------------------------------------------------------

    candidate_keys = [
        "relativeHumidity",
        "dewpointTemperature",
        "dewpointTemperatureAt2M",
        "specificHumidity",
        "mixingRatio",
        "waterVapourMixingRatio",
        "humidityMixingRatio",
        "airTemperature",
        "pressure",
    ]

    print()
    print(
        "TESTING COMMON MOISTURE KEYS"
    )

    for key in candidate_keys:
        print_array(handle, key)

    # --------------------------------------------------------
    # 2. Pregled vseh BUFR ključev
    # --------------------------------------------------------

    print()
    print("#" * 70)
    print(
        "ALL KEYS CONTAINING HUMID / DEW / VAPOUR / MIX / MOIST"
    )
    print("#" * 70)

    iterator = None

    found_keys = set()

    try:

        iterator = codes_keys_iterator_new(
            handle
        )

        while codes_keys_iterator_next(
            iterator
        ):

            key = codes_keys_iterator_get_name(
                iterator
            )

            key_lower = key.lower()

            if (
                "humid" in key_lower
                or "dew" in key_lower
                or "vapour" in key_lower
                or "vapor" in key_lower
                or "mix" in key_lower
                or "moist" in key_lower
            ):

                if key in found_keys:
                    continue

                found_keys.add(key)

                print()
                print("KEY:", key)

                values = safe_array(
                    handle,
                    key
                )

                print(
                    "Number of values:",
                    len(values)
                )

                if values:
                    print(
                        "First 20:",
                        values[:20]
                    )

    except Exception as exc:

        print(
            "Key iterator error:",
            exc
        )

    finally:

        if iterator is not None:
            codes_keys_iterator_delete(
                iterator
            )

    print()
    print("#" * 70)
    print("DIAGNOSTICS FINISHED")
    print("#" * 70)


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

                    diagnose_handle(handle)

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
        "Searching for latest Ljubljana sounding..."
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

            found = scan_bufr(
                tmp_path
            )

            if found:
                return

        finally:

            if (
                tmp_path
                and os.path.exists(tmp_path)
            ):
                os.remove(tmp_path)

    print()
    print(
        "Ljubljana WMO 14015 "
        "was not found."
    )


if __name__ == "__main__":
    main()
