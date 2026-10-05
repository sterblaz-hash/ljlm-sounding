"""Extract compact annual percentile curves without recalculating climatology."""
import argparse
from datetime import date, timedelta
import math
from pathlib import Path
import re

from build_dashboard_data import read_json, write_json

FIELDS = ('n', 'p10', 'p25', 'p50', 'p75', 'p90')
META = ('station_id', 'station_name', 'reference_period', 'window_days',
        'window_description', 'smoothing_days', 'smoothing_description',
        'created_utc', 'historical_time_caveat', 'daily_deduplication')


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def build(root):
    source = read_json(root / 'climatology/daily_climatology.json')
    metadata = source['metadata']
    daily = source['daily']
    # A leap-year calendar gives Feb 29 its own position in the annual cycle.
    days = [(date(2000, 1, 1) + timedelta(days=i)).strftime('%m-%d') for i in range(366)]
    parameters = []
    for key, definition in sorted(metadata.get('parameter_metadata', {}).items()):
        if not re.fullmatch(r'[a-z][a-z0-9_]*', key):
            continue
        if not any(isinstance(day.get(key), dict) for day in daily.values()):
            continue
        path = f'climatology/dashboard/{key}.json'
        rows = []
        for day in days:
            item = daily.get(day, {}).get(key) or {}
            rows.append({'day': day, **{field: number(item.get(field)) for field in FIELDS}})
        write_json(root / path, {'version': 1, 'parameter': key, 'days': rows})
        parameters.append({'key': key, 'label': definition.get('label', key),
                           'unit': definition.get('unit', ''), 'path': path})
    index = {'version': 1, **{k: metadata[k] for k in META if k in metadata},
             'parameters': parameters}
    write_json(root / 'climatology/dashboard/index.json', index)
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    build(parser.parse_args().root)


if __name__ == '__main__':
    main()
