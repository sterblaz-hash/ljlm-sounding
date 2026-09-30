"""Run selection and latest products for the two LJLM +12 h verification slots."""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def verification_time(run: datetime, lead_hours: int) -> datetime:
    if run.tzinfo is None or run.utcoffset() != timedelta(0):
        raise ValueError('Verification run must be in UTC')
    if run.hour not in (0, 12) or run.minute or run.second or run.microsecond:
        raise ValueError('Verification requires an exact 00 or 12 UTC run')
    if lead_hours != 12:
        raise ValueError('Verification requires a genuine +12 h forecast')
    return run + timedelta(hours=12)


def choose_run(cycle: str, run_date: str = '', *, now: datetime | None = None,
               scheduled: bool = False) -> datetime:
    """Choose a fixed cycle by date, independent of DWD availability."""
    if cycle not in ('00', '12'):
        raise ValueError('Only cycles 00 and 12 are supported')
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = date.fromisoformat(run_date) if run_date else now.date()
    run = datetime(day.year, day.month, day.day, int(cycle), tzinfo=timezone.utc)
    # Use the most recent scheduled occurrence, even if Actions starts after midnight.
    due = run + timedelta(hours=1, minutes=20) if scheduled else run
    if not run_date and due > now:
        run -= timedelta(days=1)
    if run > now:
        raise ValueError('Run date/cycle is in the future')
    verification_time(run, 12)
    return run


def publish_latest(output: Path, manifest: dict, root: Path) -> Path:
    """Publish a complete slot after rendering; historical reruns cannot regress it."""
    profile = json.loads(output.read_text(encoding='utf-8'))
    run = datetime.fromisoformat(profile['run_time'].replace('Z', '+00:00'))
    valid = verification_time(run, profile['lead_hours'])
    if datetime.fromisoformat(profile['valid_time'].replace('Z', '+00:00')) != valid:
        raise ValueError('Valid time does not match run +12 h')
    for key in ('model', 'run_time', 'valid_time', 'lead_hours', 'sounding_id'):
        if manifest.get(key) != profile.get(key):
            raise ValueError(f'Graphics manifest disagrees with profile: {key}')
    destination = root / 'latest' / valid.strftime('%H')
    existing = destination / 'sounding.json'
    if existing.exists():
        previous = json.loads(existing.read_text(encoding='utf-8'))
        if datetime.fromisoformat(previous['valid_time'].replace('Z', '+00:00')) > valid:
            print(f'Keeping newer latest forecast: {existing}')
            return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Prepare every file before touching the published slot. The workflow commits
    # the complete directory in one commit; the manifest is installed last locally.
    with tempfile.TemporaryDirectory(prefix='.forecast-', dir=destination.parent) as tmp:
        stage = Path(tmp)
        shutil.copyfile(output, stage / 'sounding.json')
        latest = {}
        for kind, source in manifest['latest'].items():
            name = f'{kind}.png'
            shutil.copyfile(source, stage / name)
            latest[kind] = (destination / name).relative_to(root.parent.parent).as_posix()
        published = dict(manifest)
        published['archive'] = {
            kind: Path(source).resolve().relative_to(root.resolve().parent.parent).as_posix()
            for kind, source in manifest['archive'].items()
        }
        published['latest'] = latest
        published.update(latest)
        published['sounding'] = (destination / 'sounding.json').relative_to(root.parent.parent).as_posix()
        published['verification_date'] = valid.strftime('%Y-%m-%d')
        published['verification_term'] = valid.strftime('%H')
        (stage / 'latest_products.json').write_text(
            json.dumps(published, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        destination.mkdir(exist_ok=True)
        for path in sorted(stage.iterdir(), key=lambda p: p.name == 'latest_products.json'):
            path.replace(destination / path.name)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cycle', required=True, choices=('00', '12'))
    parser.add_argument('--run-date', default='', help='UTC run date YYYY-MM-DD')
    parser.add_argument('--scheduled', action='store_true')
    args = parser.parse_args()
    run = choose_run(args.cycle, args.run_date, scheduled=args.scheduled)
    print(run.strftime('%Y-%m-%dT%H:%M:%SZ'))


if __name__ == '__main__':
    main()
