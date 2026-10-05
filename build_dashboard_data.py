"""Build compact OBS summaries; standard library only, no sounding calculations."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path

VERSION = 1
STATION = {"station": 14015, "station_name": "Ljubljana"}
BASE = Path("timeseries/ljlm")
WINDOWS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
META = ("sounding_id", "nominal_date", "term", "launch_time", "processed_at")
DIRECT = """pwat_mm freezing_level_msl_m lifted_index_c sbcape_jkg sbcin_jkg
mlcape_jkg mlcin_jkg mucape_jkg mucin_jkg lapse_rate_850_500_c_per_km
lapse_rate_700_500_c_per_km shear_0_1km_ms shear_0_3km_ms shear_0_6km_ms
shear_sfc_700_ms z500_m thickness_925_500_m""".split()
PATHS = {key: ("parameters", "metpy", key) for key in DIRECT}
for level in (850, 700, 500):
    for prefix in ("t", "td"):
        PATHS[f"{prefix}{level}_c"] = ("parameters", "standard_levels", f"{prefix}{level}", "value")
MOISTURE = ("parameters", "metpy", "moisture_transport")
for level in ("surface", "925", "850", "700"):
    key = "q_surface_gkg" if level == "surface" else f"q{level}_gkg"
    PATHS[key] = (*MOISTURE, "humidity_profile", level, "specific_humidity_gkg")
PATHS["ivt_kg_m1_s1"] = (*MOISTURE, "ivt", "magnitude_kg_m1_s1")
PATHS["ivt_direction_deg"] = (*MOISTURE, "ivt", "transport_to_direction_deg")
for barrier in ("dinaric_west", "julian_south", "julian_central", "kamnik_savinja"):
    path = (*MOISTURE, "orographic_cross_barrier", "barriers", barrier, "terrain_capped_ivt")
    PATHS[f"{barrier}_signed_ivt"] = (*path, "signed_cross_barrier_kg_m1_s1")
    PATHS[f"{barrier}_signed_fraction"] = (*path, "signed_fraction")
VARIABLES = list(PATHS)


def metric(source, path):
    for key in path:
        if not isinstance(source, dict) or source.get("available") is False:
            return None
        source = source.get(key)
    return source if isinstance(source, (int, float)) and not isinstance(source, bool) and math.isfinite(source) else None


def record_time(record):
    """Use nominal UTC time: midnight launches often occur the previous day."""
    return datetime.fromisoformat(f"{record['nominal_date']}T{record['term']}:00:00+00:00")


def extract_record(source):
    record = {key: source.get(key) for key in META}
    if record["sounding_id"] is None and record["nominal_date"] and record["term"]:
        record["sounding_id"] = record["nominal_date"].replace("-", "") + "_" + record["term"]
    if not isinstance(record['sounding_id'], str) or not record['sounding_id']:
        raise ValueError("Missing sounding_id")
    record["valid_time"] = iso(record_time(record))
    record.update({key: metric(source, path) for key, path in PATHS.items()})
    return record


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    """Atomic replacement; preserve files whose content has not changed."""
    content = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"
    if path.exists() and path.read_text() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(content)
    temporary.replace(path)


def sorted_records(records):
    by_id = {r['sounding_id']: r for r in records}
    return sorted(by_id.values(), key=lambda r: (record_time(r), r['sounding_id']))


def month_path(root, record):
    return root / BASE / "archive" / record_time(record).strftime("%Y/%m.json")


def save_month(path, records):
    records = sorted_records(records)
    write_json(path, {"version": VERSION, **STATION, "record_count": len(records), "records": records})


def update(root, source):
    record = extract_record(read_json(source))
    path = month_path(root, record)
    records = read_json(path)['records'] if path.exists() else []
    save_month(path, [*records, record])


def sync_recent(root, now, hours):
    """Merge regular archive terms in an inclusive nominal UTC window.

    Archive filenames encode nominal date/term, so enumerate only eligible
    00/12 UTC paths instead of reading every historical profile.
    """
    if hours <= 0:
        raise ValueError("Recent synchronization hours must be positive")
    start = now - timedelta(hours=hours)
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    groups = {}
    while day <= now:
        for term in ("00", "12"):
            nominal = day.replace(hour=int(term))
            if not start <= nominal <= now:
                continue
            source = root / "data" / nominal.strftime("%Y/%m") / (
                nominal.strftime("%Y%m%d") + f"_{term}.json"
            )
            if not source.is_file():
                continue
            profile = read_json(source)
            if profile.get("term") not in ("00", "12"):
                continue
            if not start <= record_time(profile) <= now:
                continue
            record = extract_record(profile)
            groups.setdefault(month_path(root, record), []).append(record)
        day += timedelta(days=1)
    for path, recovered in groups.items():
        existing = read_json(path)["records"] if path.exists() else []
        save_month(path, [*existing, *recovered])


def backfill(root):
    # Process one source month at a time, bounding memory as the archive grows.
    for directory in sorted((root / "data").glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]")):
        groups = {}
        for source in sorted(directory.glob("*.json")):
            record = extract_record(read_json(source))
            groups.setdefault(month_path(root, record), []).append(record)
        for path, records in groups.items():
            save_month(path, records)
    latest = root / "data/latest.json"
    if latest.is_file():
        update(root, latest)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def filter_window(records, end, days):
    start = end - timedelta(days=days)
    return sorted_records(r for r in records if start <= record_time(r) <= end)


def build_windows(root, now):
    start = now - timedelta(days=365)
    month = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    records = []
    while month <= now:
        path = root / BASE / "archive" / month.strftime("%Y/%m.json")
        if path.is_file():
            records.extend(read_json(path)['records'])
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    for label, days in WINDOWS.items():
        selected = filter_window(records, now, days)
        write_json(root / BASE / f"latest_{label}.json", {
            "version": VERSION, **STATION, "generated_at": iso(now),
            "start_time": iso(now - timedelta(days=days)), "end_time": iso(now),
            "record_count": len(selected), "records": selected,
        })


def build_manifest(root):
    def existing(candidates):
        return {key: str(path) for key, path in candidates.items() if (root / path).is_file()}

    diagnostics = existing({"index": Path("diagnostics/latest_products.json"), **{
        name: Path(f"diagnostics/latest_{name}.png")
        for name in ("skewt", "skewt_zoom", "lowlevel", "thetae", "hodograph")}})
    obs = existing({"latest_profile": Path("data/latest.json"), "status": Path("data/status.json")})
    obs['diagnostics'] = diagnostics
    slots = {}
    for term in ("00", "12"):
        base = Path("models/icon-d2/latest") / term
        products = existing({"profile": base / "sounding.json", "diagnostics": base / "latest_products.json"})
        if products:
            slots[term] = products
    windows = existing({label: BASE / f"latest_{label}.json" for label in WINDOWS})
    return {"version": VERSION, **STATION, "observations": obs,
            "icon_d2": {"lead_hours": 12, "slots": slots},
            "timeseries": {"available": bool(windows), "windows": windows, "variables": VARIABLES}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--update", type=Path)
    mode.add_argument("--backfill", action="store_true")
    mode.add_argument("--sync-recent-hours", type=int)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--as-of", help="UTC window end (ISO timestamp); defaults to current time")
    args = parser.parse_args()
    now = datetime.fromisoformat(args.as_of.replace('Z', '+00:00')) if args.as_of else datetime.now(timezone.utc)
    if now.tzinfo is None:
        parser.error("--as-of must include a timezone")
    now = now.astimezone(timezone.utc)
    if args.sync_recent_hours is not None and args.sync_recent_hours <= 0:
        parser.error("--sync-recent-hours must be positive")
    if args.backfill:
        backfill(args.root)
    elif args.sync_recent_hours is not None:
        sync_recent(args.root, now, args.sync_recent_hours)
    else:
        update(args.root, args.root / args.update)
    build_windows(args.root, now)
    write_json(args.root / "data/dashboard_manifest.json", build_manifest(args.root))


if __name__ == "__main__":
    main()
