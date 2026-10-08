"""Build compact trajectory windows from existing profiles; no extraction calculations."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path

VERSION = 1
STATION = {"station": 14015, "station_name": "Ljubljana"}
BASE = Path("trajectories/ljlm")
WINDOWS = {"7d": 7, "30d": 30, "90d": 90}
FIELDS = ("time_s", "pressure_hpa", "height_m", "latitude", "longitude")


def finite_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def record_time(record):
    if "valid_time" in record:
        return datetime.fromisoformat(record["valid_time"].replace("Z", "+00:00"))
    return datetime.fromisoformat(f"{record['nominal_date']}T{record['term']}:00:00+00:00")


def extract_record(source):
    trajectory = source.get("trajectory") or {}
    if not trajectory.get("available") or source.get("term") not in ("00", "12"):
        return None
    points = []
    for point in trajectory.get("points", []):
        if not all(isinstance(point.get(k), (int, float)) and not isinstance(point[k], bool)
                   and math.isfinite(point[k]) for k in FIELDS):
            continue
        if abs(point["latitude"]) <= 90 and abs(point["longitude"]) <= 180:
            points.append({k: point[k] for k in FIELDS})
    if len(points) < 2:
        return None
    compact = [points[0]]
    for point in points[1:-1]:
        if point["time_s"] - compact[-1]["time_s"] >= 40:
            compact.append(point)
    compact.append(points[-1])
    return {"sounding_id": source.get("sounding_id") or source["nominal_date"].replace("-", "") + "_" + source["term"],
            "valid_time": iso(record_time(source)), "launch_time": source.get("launch_time"),
            "term": source["term"], "duration_s": finite_number(trajectory.get("duration_s")),
            "max_height_m": finite_number(trajectory.get("max_height_m")), "points": compact}


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
    if record is None:
        return
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
            if record is not None:
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
            if record is not None:
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
    start = now - timedelta(days=max(WINDOWS.values()))
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
    from build_dashboard_data import build_manifest as central_manifest
    return central_manifest(root)


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
