import os
import tempfile
from datetime import datetime, timezone
from urllib.parse import unquote

import extract_ljlm as core


def collect_all_ljlm():
    """
    Pregleda vse BUFR pakete, ki so trenutno še v DWD imeniku,
    in vrne po eno najboljšo/najnovejšo kopijo vsake LJLM sondaže.
    """
    html = core.get_directory_listing()

    import re
    files = re.findall(
        r'href="([^"]*temp_bufr[^"]*\.bin)"',
        html,
        flags=re.IGNORECASE
    )

    files = sorted(set(
        f for f in files
        if "latest" not in f.lower()
    ))

    print("DWD packages currently available:", len(files))

    found = {}

    for number, filename in enumerate(files, start=1):
        print(
            f"[{number}/{len(files)}] "
            f"{unquote(filename)}"
        )

        try:
            content = core.download(
                core.DWD_URL + filename
            )
        except Exception as exc:
            print("  Download failed:", exc)
            continue

        tmp_path = None

        try:
            with tempfile.NamedTemporaryFile(
                suffix=".bufr",
                delete=False
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name

            profile = core.read_ljlm_from_bufr(
                tmp_path
            )

            if profile is None:
                continue

            launch = datetime.fromisoformat(
                profile["launch_time"].replace(
                    "Z", "+00:00"
                )
            )

            term, nominal_date = (
                core.determine_term(launch)
            )

            key = (
                nominal_date.isoformat(),
                term
            )

            print(
                "  LJLM:",
                profile["launch_time"],
                "->",
                nominal_date,
                term,
                "UTC"
            )

            # Če je ista nominalna sondaža prisotna v več DWD
            # paketih, obdržimo kopijo z največ profilnimi nivoji.
            old = found.get(key)

            if (
                old is None
                or profile.get("qc", {}).get(
                    "profile_level_count", 0
                )
                > old["profile"].get(
                    "qc", {}
                ).get(
                    "profile_level_count", 0
                )
            ):
                found[key] = {
                    "profile": profile,
                    "source_file": filename,
                    "nominal_date": nominal_date,
                    "term": term,
                }

        except Exception as exc:
            print("  Package warning:", exc)

        finally:
            if (
                tmp_path
                and os.path.exists(tmp_path)
            ):
                os.remove(tmp_path)

    return found


def save_profiles_in_time_order(found):
    """
    Najprej shrani profile kronološko, nato jih še enkrat
    preračuna kronološko, da se pravilno dopolnijo primerjave
    previous_term in previous_day_same_term.
    """
    items = sorted(
        found.values(),
        key=lambda item: (
            item["nominal_date"],
            item["term"]
        )
    )

    if not items:
        print()
        print("No LJLM soundings found.")
        return

    print()
    print("=" * 70)
    print("LJLM SOUNDINGS FOUND")
    print("=" * 70)

    for item in items:
        print(
            item["nominal_date"],
            item["term"],
            "UTC | launch",
            item["profile"]["launch_time"],
            "| levels",
            item["profile"].get(
                "qc", {}
            ).get(
                "profile_level_count"
            )
        )

    print()
    print("First pass: writing/recalculating all profiles.")

    for item in items:
        path = core.save_json(
            item["profile"],
            item["source_file"],
            item["term"],
            item["nominal_date"]
        )

        print("  Saved:", path)

    # Drugi prehod je namenoma narejen po tem, ko so vsi arhivi
    # že na disku. Tako najstarejši/novejši profili dobijo vse
    # možne 12- in 24-urne primerjave.
    print()
    print("Second pass: rebuilding change comparisons.")

    for item in items:
        path = core.save_json(
            item["profile"],
            item["source_file"],
            item["term"],
            item["nominal_date"]
        )

        print("  Updated:", path)

    # latest.json naj na koncu res kaže najnovejšo najdeno sondažo.
    newest = items[-1]

    core.save_json(
        newest["profile"],
        newest["source_file"],
        newest["term"],
        newest["nominal_date"]
    )

    print()
    print("=" * 70)
    print(
        "Backfill complete:",
        len(items),
        "unique LJLM soundings."
    )
    print(
        "Range:",
        items[0]["nominal_date"],
        items[0]["term"],
        "UTC ->",
        items[-1]["nominal_date"],
        items[-1]["term"],
        "UTC"
    )
    print("=" * 70)


def main():
    print(
        "Recent LJLM backfill started:",
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    found = collect_all_ljlm()
    save_profiles_in_time_order(found)


if __name__ == "__main__":
    main()
