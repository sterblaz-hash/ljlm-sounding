"""
Diagnostika GPS/trajektorije za LJLM (WMO 14015).

Skript pregleda trenutno dostopne DWD radiosonde BUFR pakete,
poišče najnovejšo sondažo Ljubljana in izpiše ključe/arrays,
ki bi lahko vsebovali položaj balona ali horizontalni premik.

Ne spreminja data/ in ničesar ne zapisuje v arhiv.
"""

import os
import re
import tempfile
from urllib.parse import unquote

import numpy as np

import extract_ljlm as core

from eccodes import (
    codes_bufr_new_from_file,
    codes_get,
    codes_get_array,
    codes_get_size,
    codes_release,
    codes_set,
    codes_keys_iterator_delete,
    codes_keys_iterator_get_name,
    codes_keys_iterator_new,
    codes_keys_iterator_next,
)


SEARCH_WORDS = (
    "latitude",
    "longitude",
    "displacement",
    "timeperiod",
    "timeincrement",
    "height",
    "geopotential",
)


def is_ljlm(handle):
    try:
        block = int(codes_get(handle, "blockNumber"))
        station = int(codes_get(handle, "stationNumber"))
        return block == 14 and station == 15
    except Exception:
        return False


def useful_key(name):
    low = name.lower()
    return any(word in low for word in SEARCH_WORDS)


def safe_values(handle, key):
    try:
        size = codes_get_size(handle, key)
    except Exception:
        size = None

    try:
        if size is not None and size > 1:
            arr = codes_get_array(handle, key)
            return arr
        return codes_get(handle, key)
    except Exception as exc:
        return f"<cannot read: {exc}>"


def summarize(value, max_items=12):
    if isinstance(value, str):
        return value

    if np.isscalar(value):
        try:
            return float(value)
        except Exception:
            return value

    try:
        arr = np.asarray(value)
        n = len(arr)
        head = arr[:max_items].tolist()
        tail = arr[-3:].tolist() if n > max_items else []
        return {
            "count": n,
            "first": head,
            "last": tail,
        }
    except Exception:
        return repr(value)


def inspect_file(path, source_name):
    with open(path, "rb") as f:
        message_number = 0

        while True:
            try:
                h = codes_bufr_new_from_file(f)
            except Exception as exc:
                print("BUFR read warning:", exc)
                break

            if h is None:
                break

            message_number += 1

            try:
                try:
                    codes_set(h, "unpack", 1)
                except Exception:
                    continue

                if not is_ljlm(h):
                    continue

                print()
                print("=" * 72)
                print("LJLM FOUND")
                print("=" * 72)
                print("Source:", unquote(source_name))
                print("BUFR message:", message_number)

                for key in (
                    "year", "month", "day",
                    "hour", "minute",
                    "latitude", "longitude",
                ):
                    try:
                        print(
                            f"{key}:",
                            summarize(
                                safe_values(h, key)
                            )
                        )
                    except Exception:
                        pass

                print()
                print("POSSIBLE TRAJECTORY KEYS")
                print("-" * 72)

                seen = set()
                iterator = codes_keys_iterator_new(h)

                try:
                    while codes_keys_iterator_next(iterator):
                        name = codes_keys_iterator_get_name(
                            iterator
                        )

                        if (
                            name in seen
                            or not useful_key(name)
                        ):
                            continue

                        seen.add(name)

                        value = safe_values(h, name)

                        print(
                            name,
                            "=>",
                            summarize(value)
                        )
                finally:
                    codes_keys_iterator_delete(iterator)

                print()
                print("=" * 72)
                print(
                    "Če vidiš večvrednostne latitude/longitude ali "
                    "latitudeDisplacement/longitudeDisplacement, "
                    "lahko verjetno rekonstruirava pot balona."
                )
                print("=" * 72)

                return True

            finally:
                codes_release(h)

    return False


def main():
    candidates = core.find_candidate_files()

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
            content = core.download(
                core.DWD_URL + filename
            )
        except Exception as exc:
            print("Download failed:", exc)
            continue

        tmp_path = None

        try:
            with tempfile.NamedTemporaryFile(
                suffix=".bufr",
                delete=False
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name

            if inspect_file(
                tmp_path,
                filename
            ):
                return

        except Exception as exc:
            print("Package warning:", exc)

        finally:
            if (
                tmp_path
                and os.path.exists(tmp_path)
            ):
                os.remove(tmp_path)

    print()
    print(
        "LJLM was not found in the currently scanned "
        "DWD packages."
    )


if __name__ == "__main__":
    main()
