#!/usr/bin/env python3
"""Manually build a bounded-range C-LAEF public pressure-level sounding."""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claef_point_forecast import atomic_json, utc
from claef_pressure_profile import (
    ProbeError, STANDARD_GRAVITY, dew_point_c, extract_product, wind_from_uv,
)
from icon_d2_forecast import publish_latest, verification_time
from probe_claef import parse_time

ROOT = Path(__file__).resolve().parent / 'models' / 'claef'


def validate_run(run, lead_hours, publish_verification=False):
    if run.tzinfo is None or run.utcoffset() != timedelta(0):
        raise ValueError('Run must be in UTC')
    if run.minute or run.second or run.microsecond:
        raise ValueError('Run must be an exact UTC hour')
    if not isinstance(lead_hours, int) or not 0 <= lead_hours <= 60:
        raise ValueError('Lead must be an integer 0–60 h')
    if publish_verification:
        verification_time(run, lead_hours)


def build_sounding(product):
    """Adapt validated extraction without interpolation or parcel diagnostics."""
    run = parse_time(product['reference_time'])
    valid = run + timedelta(hours=product['lead_hours'])
    if parse_time(product['valid_time']) != valid:
        raise ProbeError('Valid time differs from reference time + lead.')
    surface = product['surface']
    speed, direction = wind_from_uv(surface['10u'], surface['10v'])
    surface_row = {
        'pressure_hpa': surface['sp'] / 100,
        'height_m': surface['zsurf'] / STANDARD_GRAVITY,
        'temperature_c': surface['2t'],
        'dewpoint_c': dew_point_c(surface['2t'], surface['2r']),
        'relative_humidity_pct': surface['2r'],
        'u_ms': surface['10u'], 'v_ms': surface['10v'],
        'wind_speed_ms': speed, 'wind_direction_deg': direction,
        'omega_pa_s': None, 'below_model_surface': False,
        'level_type': 'model_surface', 'temperature_height_agl_m': 2,
        'wind_height_agl_m': 10,
    }
    mapping = {'temperature': 'temperature_c', 'dew_point': 'dewpoint_c',
               'relative_humidity': 'relative_humidity_pct', 'u': 'u_ms', 'v': 'v_ms',
               'wind_speed': 'wind_speed_ms', 'wind_direction': 'wind_direction_deg',
               'pressure_hpa': 'pressure_hpa', 'height_m': 'height_m', 'omega_pa_s': 'omega_pa_s'}
    levels, excluded = [surface_row], []
    for row in product['levels']:
        if row['pressure_hpa'] * 100 > surface['sp']:
            excluded.append(row['pressure_hpa'])
            continue
        levels.append({**{target: row[source] for source, target in mapping.items()},
                       'below_model_surface': False, 'level_type': 'pressure'})
    actual = {row['pressure_hpa']: row for row in levels[1:]}
    return {
        'profile_type': 'model', 'model': 'C-LAEF',
        'run_time': utc(run.isoformat()), 'valid_time': utc(valid.isoformat()),
        'lead_hours': product['lead_hours'], 'station': 14015, 'station_name': 'Ljubljana',
        'requested_latitude': product['requested_location']['latitude'],
        'requested_longitude': product['requested_location']['longitude'],
        'grid_latitude': product['grid_point']['latitude'],
        'grid_longitude': product['grid_point']['longitude'],
        'sounding_id': f'claef_{run:%Y%m%d%H}_f{product["lead_hours"]:03d}',
        'nominal_date': valid.strftime('%Y-%m-%d'), 'term': valid.strftime('%H'),
        'processed_at': utc(datetime.now(timezone.utc).isoformat()),
        'source': product['source'], 'source_url': product['source']['url'],
        'diagnostic_notes': [
            'Public 11-level pressure product, 1000–300 hPa; no missing-level interpolation.',
            'Synthetic model surface row: pressure/height at terrain, temperature and humidity at 2 m, wind at 10 m.',
            'Pressure levels below model surface are excluded from rendering.',
            'Sparse profile: parcel CAPE/CIN, tropopause, Bunkers/SRH and detailed inversions unavailable.',
            'Geopotential height uses standard gravity; Magnus dew point is approximate.',
        ],
        'diagnostics_available': {'parcel': False},
        'pressure_levels_hpa': product['pressure_levels_hpa'],
        'excluded_pressure_levels_hpa': excluded,
        'source_variables': product['source_variables'],
        'remote_access': product['remote_access'], 'levels': levels,
        'parameters': {
            'standard_levels': {f'{short}{p}': actual.get(p, {}).get(field)
                                for p in (850, 700, 500)
                                for short, field in (('t', 'temperature_c'), ('td', 'dewpoint_c'), ('rh', 'relative_humidity_pct'))},
            'metpy': {}, 'inversions': {'available': False, 'layers': [], 'reason': 'Sparse public pressure-level profile'},
        },
    }


def generate(run, lead_hours=12, publish_verification=False, root=ROOT):
    validate_run(run, lead_hours, publish_verification)
    product = extract_product(lead_hours=lead_hours, reference_time=utc(run.isoformat()), sounding=True)
    if parse_time(product['reference_time']) != run:
        raise ProbeError('Extraction returned a different cycle; refusing substitution.')
    profile = build_sounding(product)
    directory = Path(root) / run.strftime('%Y%m%d%H')
    output = directory / f'ljlm_f{lead_hours:03d}.json'
    atomic_json(output, profile)
    from render_ljlm import render_products
    manifest = render_products(output, diagnostics_root=directory / 'diagnostics' / f'f{lead_hours:03d}')
    if publish_verification:
        publish_latest(output, manifest, Path(root))
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, help='Explicit UTC run timestamp')
    parser.add_argument('--lead-hours', type=int, default=12)
    parser.add_argument('--publish-verification', action='store_true')
    args = parser.parse_args(argv)
    try:
        run = parse_time(args.run)
        validate_run(run, args.lead_hours, args.publish_verification)
    except (ValueError, ProbeError) as error:
        parser.error(str(error))
    try:
        print(f'Saved {generate(run, args.lead_hours, args.publish_verification)}')
    except (ProbeError, OSError) as error:
        parser.exit(1, f'C-LAEF extraction failed: {error}\n')


if __name__ == '__main__':
    main()
