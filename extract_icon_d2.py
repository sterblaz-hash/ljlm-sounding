#!/usr/bin/env python3
"""Build one native ICON-D2 model sounding without writing observational products."""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

import metpy.calc as mpcalc
from metpy.units import units

from icon_d2_native import (
    DWD_BASE, add_native_heights, dwd_model_level_url, fetch_bytes,
    fetch_dwd_native_profile, parse_utc, utc_iso,
)
from extract_ljlm import calculate_metpy_parameters, calculate_inversions, value_at_pressure
from render_ljlm import render_products
from icon_d2_forecast import publish_latest, verification_time

ROOT = Path(__file__).resolve().parent / 'models' / 'icon-d2'
LATITUDE = 46.06562
LONGITUDE = 14.51221


def select_run(lead_hours: int) -> datetime:
    """Find the newest published cycle with the requested forecast pressure file."""
    now = datetime.now(timezone.utc)
    cycle = now.replace(hour=now.hour // 3 * 3, minute=0, second=0, microsecond=0)
    for offset in range(9):
        run = cycle - timedelta(hours=3 * offset)
        try:
            listing = fetch_bytes(f'{DWD_BASE}/{run.hour:02d}/p/').decode('utf-8')
        except HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        if dwd_model_level_url(run, lead_hours, 'p', 65).rsplit('/', 1)[1] in listing:
            return run
    raise RuntimeError('No current ICON-D2 run has the requested forecast lead')


def validate_levels(levels: list[dict]) -> None:
    if len(levels) < 10:
        raise ValueError('Too few native levels for sounding diagnostics')
    for row in levels:
        for key in ('pressure_hpa', 'height_m', 'temperature_c', 'dewpoint_c', 'wind_speed_ms', 'wind_direction_deg'):
            if row.get(key) is None or not math.isfinite(row[key]):
                raise ValueError(f'Missing or nonfinite {key} at model level {row.get("model_level")}')
    for low, high in zip(levels, levels[1:]):
        if low['pressure_hpa'] <= high['pressure_hpa'] or low['height_m'] >= high['height_m']:
            raise ValueError('Profile pressure/height ordering is invalid')


def build_profile(run: datetime, lead_hours: int, workers: int) -> dict:
    valid = run + timedelta(hours=lead_hours)
    profile = fetch_dwd_native_profile(LATITUDE, LONGITUDE, run, valid, 100.0, workers)
    add_native_heights(profile, workers)
    levels = profile['levels']
    validate_levels(levels)
    for row in levels:
        row['relative_humidity_pct'] = round(float(mpcalc.relative_humidity_from_dewpoint(
            row['temperature_c'] * units.degC, row['dewpoint_c'] * units.degC).magnitude) * 100, 2)
    profile.update(
        profile_type='model', model='ICON-D2', station=14015, station_name='Ljubljana',
        latitude=LATITUDE, longitude=LONGITUDE,
        sounding_id=f'icon-d2_{run:%Y%m%d%H}_f{lead_hours:03d}',
        nominal_date=valid.strftime('%Y-%m-%d'), term=valid.strftime('%H'),
        processed_at=utc_iso(datetime.now(timezone.utc)), source_url=DWD_BASE,
        diagnostic_notes=[
            'Native model levels on the DWD regular latitude/longitude grid; nearest grid point.',
            'Surface-based diagnostics and AGL heights use the lowest full model level as their base.',
            'No observed surface level is inserted; model terrain may differ from station elevation.',
            'Profile requested to 100 hPa with extra native levels above for interpolation.',
        ],
    )
    profile['source_url_template'] = dwd_model_level_url(run, lead_hours, '{variable}', 65).replace('_65_', '_{level}_')
    profile['parameters'] = {
        'standard_levels': {f'{short}{p}': value_at_pressure(levels, field, p)
                            for p in (850, 700, 500)
                            for short, field in (('t', 'temperature_c'), ('td', 'dewpoint_c'), ('rh', 'relative_humidity_pct'))},
        'metpy': calculate_metpy_parameters(levels),
        'inversions': calculate_inversions(levels),
    }
    return profile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', help='UTC run timestamp; default: newest published run')
    parser.add_argument('--lead-hours', type=int, default=3)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--publish-verification', action='store_true',
                        help='Publish latest valid 00/12 slot; requires explicit 00/12 run and +12 h')
    args = parser.parse_args()
    if not 0 <= args.lead_hours <= 48 or not 1 <= args.workers <= 16:
        parser.error('lead-hours must be 0..48 and workers must be 1..16')
    if args.publish_verification:
        if not args.run:
            parser.error('--publish-verification requires an explicit --run')
        try:
            verification_time(parse_utc(args.run), args.lead_hours)
        except ValueError as exc:
            parser.error(str(exc))
    run = parse_utc(args.run) if args.run else select_run(args.lead_hours)
    if run.hour % 3 or run.minute or run.second or run.microsecond:
        parser.error('run must be an exact three-hour UTC cycle')
    print(f'Fetching ICON-D2 {utc_iso(run)} +{args.lead_hours:03d}h', flush=True)
    profile = build_profile(run, args.lead_hours, args.workers)
    directory = ROOT / run.strftime('%Y%m%d%H')
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f'ljlm_f{args.lead_hours:03d}.json'
    temp = output.with_suffix('.json.tmp')
    temp.write_text(json.dumps(profile, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    temp.replace(output)
    manifest = render_products(output, diagnostics_root=directory / 'diagnostics' / f'f{args.lead_hours:03d}')
    if args.publish_verification:
        publish_latest(output, manifest, ROOT)
    print(f'Saved {output}', flush=True)


if __name__ == '__main__':
    main()
